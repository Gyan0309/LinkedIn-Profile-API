"""Hold a LinkedIn session, supplied per request as a Cookie header.

The service stores none of its own. Three alternatives were tried and removed:
an env var (made the deployment an open proxy), scripted login (401 even for
correct credentials), and a bare `li_at` (302 to the login page).

A session also carries the browser it came from. LinkedIn binds `li_at` to a
device fingerprint, so replaying it under a different User-Agent looks like a
stolen cookie and gets the session invalidated everywhere -- including in the
caller's own browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.errors import SessionUnavailable
from app.logging_config import stage

logger = logging.getLogger(__name__)

# Only used when the caller sends no User-Agent of their own. Any fixed string
# is a guess at someone else's browser, which is the problem, so callers should
# always send theirs.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

MAX_USER_AGENT_LENGTH = 400

# Most specific first: an Edge UA also contains "Chrome/".
_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Chrome/", "Chrome"),
    ("Firefox/", "Firefox"),
    ("Safari/", "Safari"),
)

_PLATFORMS = (
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("Android", "Android"),
    ("CrOS", "Chrome OS"),
    ("Linux", "Linux"),
)


def clean_user_agent(raw: str) -> str:
    """Sanitise a caller-supplied User-Agent, falling back to the default.

    Printable ASCII only: this string goes straight into an outbound header, so
    a stray newline would be header injection.
    """
    agent = "".join(c for c in raw.strip() if " " <= c <= "~")
    return agent[:MAX_USER_AGENT_LENGTH] or DEFAULT_USER_AGENT


def parse_timezone_offset(raw: str) -> float:
    """Hours east of UTC, as the caller's browser reports them."""
    try:
        offset = float(raw.strip())
    except (TypeError, ValueError):
        return 0.0
    return offset if -12.0 <= offset <= 14.0 else 0.0


def platform_of(user_agent: str) -> str:
    """The `sec-ch-ua-platform` value implied by a User-Agent."""
    for token, name in _PLATFORMS:
        if token in user_agent:
            return name
    return "Unknown"


def browser_label(user_agent: str) -> str:
    """Short "Chrome-140-on-Windows" for logs; the full string is noise."""
    for token, name in _BROWSERS:
        if token in user_agent:
            version = user_agent.split(token, 1)[1].split(".", 1)[0]
            return f"{name}-{version}-on-{platform_of(user_agent)}"
    return "unrecognised"


@dataclass(frozen=True)
class LinkedInSession:
    """A usable member session. Never logged, never returned to a caller."""

    li_at: str
    jsessionid: str  # unquoted; the csrf-token header uses it as-is
    cookie_header: str
    source: str
    user_agent: str = DEFAULT_USER_AGENT
    timezone_offset: float = 0.0
    acquired_at: float = field(default_factory=time.time)

    @property
    def csrf_token(self) -> str:
        return self.jsessionid

    @property
    def age_seconds(self) -> float:
        return time.time() - self.acquired_at

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.cookie_header)

    def redacted(self) -> dict[str, object]:
        """Enough to debug a session, none of the secret."""
        if len(self.li_at) > 12:
            hint = f"{self.li_at[:4]}...{self.li_at[-4:]}"
        else:
            hint = "..."
        return {
            "source": self.source,
            "li_at_fingerprint": hint,
            "cookies_supplied": self.cookie_header.count("="),
            "session": self.fingerprint,
            # The one field worth reading when a session keeps dying: if this
            # is not the caller's real browser, LinkedIn will invalidate it.
            "browser": browser_label(self.user_agent),
            "tz_offset": self.timezone_offset,
            "age_seconds": round(self.age_seconds, 1),
        }


def clean_cookie_header(raw: str) -> str:
    """Normalise a pasted Cookie header: strips a leading `Cookie:`, quotes, wraps."""
    cleaned = raw.strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned.split(":", 1)[1]
    cleaned = cleaned.strip().strip("'\"")
    return " ".join(cleaned.split())


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a Cookie header into a name -> value mapping."""
    jar: dict[str, str] = {}
    for pair in clean_cookie_header(raw).split(";"):
        name, sep, value = pair.partition("=")
        if not sep:
            continue
        key = name.strip()
        if key:
            jar[key] = value.strip()
    return jar


def _synthesise_jsessionid() -> str:
    """Mint a JSESSIONID. The CSRF check only needs it to match the cookie we send."""
    return "ajax:" + "".join(random.choices("0123456789", k=19))


def fingerprint_of(cookie_header: str) -> str:
    """Session id for the cache key. Two accounts see different profile data."""
    return hashlib.sha256(cookie_header.encode("utf-8")).hexdigest()[:16]


class SessionManager:
    """Resolves the session once and holds it until something invalidates it."""

    def __init__(
        self,
        settings: Settings,
        cookie_override: str = "",
        user_agent: str = "",
        timezone_offset: str = "",
    ) -> None:
        self._settings = settings
        self._override = cookie_override.strip()
        self._user_agent = clean_user_agent(user_agent)
        self._timezone_offset = parse_timezone_offset(timezone_offset)
        self._session: LinkedInSession | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> LinkedInSession | None:
        return self._session

    async def get(self) -> LinkedInSession:
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            self._session = self._acquire()
            stage(logger, "session", "acquired", **self._session.redacted())
            return self._session

    def invalidate(self) -> None:
        """Drop the cached session. Called on a 401, which is a dead cookie."""
        if self._session is not None:
            stage(
                logger,
                "session",
                "invalidated",
                age_s=round(self._session.age_seconds),
                level=logging.WARNING,
            )
        self._session = None

    def _acquire(self) -> LinkedInSession:
        supplied = self._override

        if not supplied:
            raise SessionUnavailable(
                "No LinkedIn session supplied. This service stores no "
                "credentials of its own, so send the whole Cookie header from a "
                "logged-in browser request in the X-LinkedIn-Cookie header. See "
                "the README, 'Getting a session'."
            )

        raw = clean_cookie_header(supplied)
        jar = parse_cookie_header(raw)

        li_at = jar.get("li_at", "")
        if not li_at:
            raise SessionUnavailable(
                "The supplied cookie contains no li_at, so it is not a logged-in "
                "session. Copy the whole Cookie header from a request to "
                "www.linkedin.com, not a fragment of it."
            )

        jsessionid = jar.get("JSESSIONID", "").strip('"')
        if jsessionid:
            cookie_header = raw
        else:
            jsessionid = _synthesise_jsessionid()
            cookie_header = f'{raw}; JSESSIONID="{jsessionid}"'
            logger.warning("LINKEDIN cookie had no JSESSIONID; one was minted.")

        return LinkedInSession(
            li_at=li_at,
            jsessionid=jsessionid,
            cookie_header=cookie_header,
            source="caller-header",
            user_agent=self._user_agent,
            timezone_offset=self._timezone_offset,
        )
