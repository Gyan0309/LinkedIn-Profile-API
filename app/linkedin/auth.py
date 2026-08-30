"""Acquire and hold a LinkedIn member session.

Two supported paths, in preference order:

  A. Cookies lifted from a browser (``LINKEDIN_LI_AT``). Recommended, because the
     session is created from a residential IP by a real browser and only *used*
     from the server. A login performed from a datacenter IP is the single most
     reliable way to trigger a challenge, so we avoid performing one at all.

  B. Programmatic login (``LINKEDIN_EMAIL`` / ``LINKEDIN_PASSWORD``) against
     ``/uas/authenticate``. Convenient, and frequently challenged.

On a challenge we stop. This service does not solve CAPTCHAs or work around
verification -- it raises ``ChallengeRequired`` telling the operator to log in
from a browser and supply the cookie, which is the honest end of that road.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.errors import ChallengeRequired, CredentialsRejected, SessionUnavailable
from app.logging_config import stage

logger = logging.getLogger(__name__)

AUTHENTICATE_URL = "https://www.linkedin.com/uas/authenticate"

# Chrome on Windows. LinkedIn rejects obviously non-browser agents outright, and
# a stale major version draws extra scrutiny.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class LinkedInSession:
    """A usable member session. Never logged, never serialised into a response."""

    li_at: str
    jsessionid: str  # stored unquoted; the cookie header re-adds the quotes
    source: str
    acquired_at: float = field(default_factory=time.time)

    @property
    def cookie_header(self) -> str:
        # LinkedIn sends JSESSIONID with literal double quotes around the value and
        # expects them echoed back. The csrf-token header uses the same value
        # *without* quotes -- a mismatch is a silent 403.
        return f'li_at={self.li_at}; JSESSIONID="{self.jsessionid}"'

    @property
    def csrf_token(self) -> str:
        return self.jsessionid

    @property
    def age_seconds(self) -> float:
        return time.time() - self.acquired_at

    def redacted(self) -> dict[str, object]:
        """Everything an operator needs to debug the session, none of the secret."""
        if len(self.li_at) > 12:
            fingerprint = f"{self.li_at[:4]}...{self.li_at[-4:]}"
        else:
            fingerprint = "..."
        return {
            "source": self.source,
            "li_at_fingerprint": fingerprint,
            "acquired_at": self.acquired_at,
            "age_seconds": round(self.age_seconds, 1),
        }


def _synthesise_jsessionid() -> str:
    """Mint a JSESSIONID when only ``li_at`` was supplied.

    The CSRF check compares the ``csrf-token`` header against the JSESSIONID
    cookie *we send*; it does not require the value to be one LinkedIn issued. So
    a self-consistent pair is accepted, and an operator only has to copy one
    cookie instead of two.
    """
    return "ajax:" + "".join(random.choices("0123456789", k=19))


class SessionManager:
    """Resolves a session once and holds it until something invalidates it."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session: LinkedInSession | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> LinkedInSession | None:
        return self._session

    async def get(self) -> LinkedInSession:
        if self._session is not None:
            return self._session
        async with self._lock:
            # Another coroutine may have resolved it while we waited.
            if self._session is not None:
                return self._session
            self._session = await self._acquire()
            stage(logger, "session", "acquired", **self._session.redacted())
            return self._session

    def invalidate(self) -> None:
        """Drop the cached session so the next request re-acquires.

        Called on a 401 from Voyager, which is what a dead cookie looks like.
        """
        if self._session is not None:
            stage(logger, "session", "invalidated",
                  age_s=round(self._session.age_seconds), level=logging.WARNING)
        self._session = None

    async def _acquire(self) -> LinkedInSession:
        settings = self._settings

        if settings.has_cookie_session:
            jsessionid = settings.linkedin_jsessionid.strip().strip('"')
            return LinkedInSession(
                li_at=settings.linkedin_li_at.strip(),
                jsessionid=jsessionid or _synthesise_jsessionid(),
                source="env-cookie",
            )

        if settings.has_login_credentials:
            return await self._login(settings.linkedin_email, settings.linkedin_password)

        raise SessionUnavailable(
            "No LinkedIn session configured. Set LINKEDIN_LI_AT (recommended) or "
            "LINKEDIN_EMAIL and LINKEDIN_PASSWORD. See README 'Getting a session'."
        )

    async def _login(self, email: str, password: str) -> LinkedInSession:
        """Two-step form login: seed cookies, then post credentials back with them."""
        proxy = self._settings.outbound_proxy_url or None
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            proxy=proxy,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            # Step 1 -- seed. The response sets bcookie and JSESSIONID; the login
            # POST is rejected without them.
            await client.get(AUTHENTICATE_URL)
            jsessionid = client.cookies.get("JSESSIONID", "").strip('"')
            if not jsessionid:
                raise SessionUnavailable(
                    "LinkedIn did not issue a JSESSIONID cookie on the login seed "
                    "request. The host IP is most likely blocked; see README "
                    "'Known limitations'."
                )

            # Step 2 -- authenticate. JSESSIONID goes in the body too, quotes included.
            response = await client.post(
                AUTHENTICATE_URL,
                data={
                    "session_key": email,
                    "session_password": password,
                    "JSESSIONID": f'"{jsessionid}"',
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Li-User-Agent": "LIAuthLibrary:0.0.3 com.linkedin.android:4.1.881",
                },
            )

            self._raise_for_login_result(response)

            li_at = client.cookies.get("li_at", "")
            if not li_at:
                raise SessionUnavailable(
                    "Login reported success but no li_at cookie was returned. "
                    "Supply LINKEDIN_LI_AT directly instead."
                )

            return LinkedInSession(
                li_at=li_at,
                jsessionid=client.cookies.get("JSESSIONID", jsessionid).strip('"'),
                source="programmatic-login",
            )

    @staticmethod
    def _raise_for_login_result(response: httpx.Response) -> None:
        """Map the login outcome onto our typed failures."""
        if response.status_code == 401:
            raise CredentialsRejected(
                "LinkedIn rejected the email/password pair (401). Either they are "
                "wrong, or the account requires a browser login. Do not retry in a "
                "loop -- repeated failed logins are themselves a risk signal."
            )
        if response.status_code == 999:
            raise SessionUnavailable(
                "LinkedIn blocked the login request outright (HTTP 999). The host IP "
                "is flagged -- set OUTBOUND_PROXY_URL or supply LINKEDIN_LI_AT directly."
            )

        try:
            body = response.json()
        except ValueError:
            body = {}

        result = str(body.get("login_result", "")).upper()

        if result == "PASS":
            return
        if "CHALLENGE" in result:
            raise ChallengeRequired(
                "LinkedIn issued a login challenge. This service does not solve "
                "challenges. Log in from a browser, then set LINKEDIN_LI_AT from "
                "that session's cookie."
            )
        if result in {"BAD_PASSWORD", "BAD_EMAIL", "BAD_USERNAME"}:
            raise CredentialsRejected(f"LinkedIn login failed: {result}.")
        if result:
            raise SessionUnavailable(f"LinkedIn login failed: {result}.")
        if response.status_code >= 400:
            raise SessionUnavailable(
                f"LinkedIn login failed with HTTP {response.status_code}."
            )
