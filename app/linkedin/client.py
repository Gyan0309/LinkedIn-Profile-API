"""HTTP client for Voyager: headers, CSRF pairing, retries.

The retry policy is asymmetric on purpose. A 429 means slow down and is worth
backing off into; a 999 means LinkedIn has decided you are a bot and is not.
A block trips a circuit breaker instead, stopping outbound traffic for five
minutes.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.errors import (
    EndpointRetired,
    LinkedInBlocked,
    LinkedInRateLimited,
    ProfileNotFound,
    QueryRejected,
    SessionRejected,
    SessionUnavailable,
    UpstreamUnavailable,
)
from app.linkedin.auth import USER_AGENT, SessionManager
from app.logging_config import stage

logger = logging.getLogger(__name__)

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.5
CIRCUIT_OPEN_SECONDS = 300

# rest.li tuple syntax -- `variables=(vanityName:someone)` -- must keep its
# punctuation; percent-encoding it yields a 400.
RESTLI_SAFE_CHARS = "(),:*~!"

# But only for those parameters. `profileUrn` with literal colons made every
# dash collection answer 400; the same request with `%3A` answered 200.
RESTLI_TUPLE_PARAMS = frozenset({"variables"})

# Public files on a cache, not API calls. Pacing them protects nothing and
# made queryId discovery take a minute.
ASSET_HOST = "static.licdn.com"


def _encode_params(params: dict[str, str]) -> str:
    """Encode per parameter: rest.li tuple syntax survives, the rest is escaped."""
    parts = []
    for key, value in params.items():
        safe = RESTLI_SAFE_CHARS if key in RESTLI_TUPLE_PARAMS else ""
        parts.append(f"{quote(str(key), safe='')}={quote(str(value), safe=safe)}")
    return "&".join(parts)


def _redirect_rejection(location: str) -> SessionRejected:
    """Turn a redirect target into an explanation of what to do about it."""
    target = location.lower()

    if "checkpoint" in target or "challenge" in target:
        return SessionRejected(
            "LinkedIn redirected to a security checkpoint. The account needs "
            "verifying in a browser -- log in there, clear the challenge, then "
            "copy a fresh cookie."
        )

    if "login" in target or "authwall" in target or "uas/" in target:
        return SessionRejected(
            "LinkedIn redirected to the login page, so it did not accept the "
            "session. The usual cause is an incomplete cookie: li_at on its own "
            "is not enough. Copy the whole Cookie header from a real request "
            "(DevTools > Network > any www.linkedin.com request > Request "
            "Headers > cookie) and send it in X-LinkedIn-Cookie."
        )

    return SessionRejected(
        f"LinkedIn redirected the request to {location or 'an unknown location'} "
        "rather than answering it. The session was not accepted."
    )


def _short(url: str) -> str:
    """A short label for a Voyager URL. Full ones are long and hide secrets."""
    trimmed = url.replace(VOYAGER_BASE + "/", "")
    path, _, query = trimmed.partition("?")
    for marker in ("sectionType:", "vanityName:", "memberIdentity="):
        if marker in query:
            fragment = query.split(marker, 1)[1]
            value = re.split(r"[,)&]", fragment)[0]
            return f"{path}[{marker.rstrip(':=')}={value}]"
    return path


class OutboundLimiter:
    """Token bucket over outbound requests. Protects the shared outbound IP."""

    def __init__(self, per_minute: int) -> None:
        self._capacity = max(1, per_minute)
        self._tokens = float(self._capacity)
        self._refill_rate = self._capacity / 60.0
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._updated) * self._refill_rate,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    wait = 0.0
                else:
                    wait = (1.0 - self._tokens) / self._refill_rate
            if wait <= 0.0:
                # Evenly spaced requests are themselves a bot signature.
                await asyncio.sleep(random.uniform(0.05, 0.25))
                return
            await asyncio.sleep(wait)


class CircuitBreaker:
    """Stops outbound traffic after LinkedIn signals a block."""

    def __init__(self, open_seconds: int = CIRCUIT_OPEN_SECONDS) -> None:
        self._open_seconds = open_seconds
        self._opened_at: float | None = None
        self._reason = ""

    def trip(self, reason: str) -> None:
        self._opened_at = time.monotonic()
        self._reason = reason
        logger.error("circuit breaker tripped: %s", reason)

    def reset(self) -> None:
        if self._opened_at is not None:
            logger.info("circuit breaker reset")
        self._opened_at = None
        self._reason = ""

    @property
    def retry_after(self) -> int:
        if self._opened_at is None:
            return 0
        remaining = self._open_seconds - (time.monotonic() - self._opened_at)
        return max(0, int(remaining))

    def check(self) -> None:
        if self._opened_at is None:
            return
        if self.retry_after <= 0:
            self.reset()
            return
        raise LinkedInBlocked(
            f"LinkedIn is currently blocking this host ({self._reason}). "
            "Not retrying until the cooling-off period elapses.",
            retry_after=self.retry_after,
        )

    def status(self) -> dict[str, Any]:
        return {
            "open": self._opened_at is not None and self.retry_after > 0,
            "reason": self._reason or None,
            "retry_after_seconds": self.retry_after,
        }


class VoyagerClient:
    """Authenticated access to Voyager, plus raw fetches for queryId discovery."""

    def __init__(
        self,
        settings: Settings,
        sessions: SessionManager,
        limiter: OutboundLimiter | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        """`limiter` and `breaker` are shared process-wide when supplied.

        Load-bearing: each request builds its own client, so per-client guards
        would reset every time and never guard anything.
        """
        self._settings = settings
        self._sessions = sessions
        self._limiter = limiter or OutboundLimiter(settings.outbound_max_per_minute)
        self.breaker = breaker or CircuitBreaker()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            proxy=settings.outbound_proxy_url or None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- public surface -----------------------------------------------------

    async def get_voyager(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """GET a Voyager endpoint and return the parsed JSON body."""
        url = f"{VOYAGER_BASE}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{_encode_params(params)}"

        response = await self._request(url)

        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamUnavailable(
                f"Voyager returned a non-JSON body for {path} "
                f"(HTTP {response.status_code}, {len(response.content)} bytes)."
            ) from exc

        if not isinstance(body, dict):
            raise UpstreamUnavailable(f"Voyager returned a non-object body for {path}.")
        return body

    async def get_asset(self, url: str) -> str:
        """GET a URL as text. Used for JS bundles only, never profile data."""
        response = await self._request(
            url,
            accept="text/html,application/javascript,*/*",
            # The CDN is a cache of public files; only linkedin.com itself counts.
            paced=ASSET_HOST not in url,
            # Pages redirect legitimately: LinkedIn 302s a profile page to
            # itself to refresh routing cookies. Voyager endpoints never do.
            follow_redirects=True,
        )

        # The login wall arrives as an ordinary 200; the final URL gives it away.
        landed = str(response.url).lower()
        if any(marker in landed for marker in ("/login", "/authwall", "/checkpoint")):
            raise _redirect_rejection(str(response.url))

        return response.text

    # --- request machinery --------------------------------------------------

    async def _authenticated_headers(self) -> dict[str, str]:
        session = await self._sessions.get()
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "Cookie": session.cookie_header,
            "csrf-token": session.csrf_token,
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
            "x-li-track": (
                '{"clientVersion":"1.13.0","osName":"web","timezoneOffset":0,'
                '"deviceFormFactor":"DESKTOP","mpName":"voyager-web"}'
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.linkedin.com/feed/",
        }

    async def _request(
        self,
        url: str,
        *,
        accept: str | None = None,
        paced: bool = True,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        last_error: Exception | None = None
        session_retried = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.breaker.check()
            if paced:
                await self._limiter.acquire()

            # Resolved per attempt so a mid-retry session refresh is picked up.
            headers = await self._authenticated_headers()
            if accept is not None:
                headers["Accept"] = accept

            call_started = time.monotonic()
            try:
                response = await self._client.get(
                    url, headers=headers, follow_redirects=follow_redirects
                )
            except httpx.HTTPError as exc:
                last_error = exc
                stage(logger, "  voyager", _short(url), result="TRANSPORT ERROR",
                      attempt=attempt, detail=str(exc)[:80], level=logging.WARNING)
                if attempt == MAX_ATTEMPTS:
                    break
                await self._backoff(attempt)
                continue

            status = response.status_code
            elapsed_ms = int((time.monotonic() - call_started) * 1000)
            # Every upstream call is logged, because upstream calls are the
            # scarce resource here -- if the count looks wrong, that is the bug.
            stage(
                logger,
                "  voyager",
                _short(url),
                status=status,
                ms=elapsed_ms,
                attempt=attempt,
                level=logging.INFO if status == 200 else logging.WARNING,
            )

            if status == 200:
                self.breaker.reset()
                return response

            # A block. Never retried, and it stops everything else too.
            if status == 999:
                self.breaker.trip("HTTP 999 - host IP flagged as automated")
                raise LinkedInBlocked(
                    "LinkedIn returned HTTP 999: this host is flagged as automated "
                    "traffic. Set OUTBOUND_PROXY_URL to a residential proxy, or run "
                    "from a residential connection.",
                    retry_after=self.breaker.retry_after,
                )

            if status == 403:
                self.breaker.trip("HTTP 403 - session refused")
                raise LinkedInBlocked(
                    "LinkedIn refused the request (403). The session is live but not "
                    "permitted, which usually means the account has been restricted.",
                    retry_after=self.breaker.retry_after,
                )

            # A dead cookie: worth one re-acquisition, not a loop.
            if status == 401:
                if session_retried:
                    stage(logger, "  session", "still 401 after refresh - giving up",
                          level=logging.ERROR)
                    raise SessionUnavailable(
                        "LinkedIn rejected the session (401). The li_at cookie has "
                        "expired -- supply a fresh one."
                    )
                stage(logger, "  session", "401 - re-acquiring once",
                      level=logging.WARNING)
                self._sessions.invalidate()
                session_retried = True
                continue

            # Voyager answers with JSON or not at all; a 3xx is the login flow.
            if status in (301, 302, 303, 307, 308):
                raise _redirect_rejection(response.headers.get("location", ""))

            if status == 404:
                raise ProfileNotFound("LinkedIn has no profile at that identifier.")

            # LinkedIn is retiring the legacy REST endpoints account by account.
            if status == 410:
                raise EndpointRetired(
                    "LinkedIn has retired this endpoint for this account (410). "
                    "The fetch chain will fall through to the next strategy."
                )

            # A rejected query, almost always a queryId LinkedIn has rotated.
            # Surfaced distinctly so the caller can rediscover and retry once.
            if status == 400:
                raise QueryRejected(
                    "Voyager rejected the query (400). The queryId is most likely "
                    "stale."
                )

            if status == 429:
                last_error = LinkedInRateLimited(
                    "LinkedIn rate limited the request (429)."
                )
                if attempt == MAX_ATTEMPTS:
                    break
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            if 500 <= status < 600:
                last_error = UpstreamUnavailable(f"LinkedIn returned HTTP {status}.")
                if attempt == MAX_ATTEMPTS:
                    break
                await self._backoff(attempt)
                continue

            raise UpstreamUnavailable(
                f"Unexpected response from LinkedIn: HTTP {status}."
            )

        if isinstance(last_error, (LinkedInRateLimited, UpstreamUnavailable)):
            raise last_error
        raise UpstreamUnavailable(
            f"LinkedIn was unreachable after {MAX_ATTEMPTS} attempts: {last_error}"
        )

    @staticmethod
    async def _backoff(attempt: int, retry_after: str | None = None) -> None:
        """Exponential backoff with full jitter, so concurrent retries spread out."""
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        ceiling = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        await asyncio.sleep(random.uniform(0, ceiling))
