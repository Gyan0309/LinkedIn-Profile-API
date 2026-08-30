"""HTTP client for LinkedIn's internal Voyager API.

Everything that makes an authenticated Voyager request work lives here: the header
set, the CSRF pairing, the retry policy, and the refusal to retry a block.

The retry policy is deliberately asymmetric. A 429 means "slow down" and is worth
backing off into. HTTP 999 means "we have decided you are a bot" and is not --
retrying a block is how a throttled account becomes a banned one. So a block trips
a circuit breaker that stops outbound traffic entirely for a cooling-off period,
and every caller during that window gets a clean 503 explaining why.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.errors import (
    LinkedInBlocked,
    LinkedInRateLimited,
    ProfileNotFound,
    QueryRejected,
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

# Voyager's rest.li query syntax uses literal parentheses, colons, commas and
# asterisks. Percent-encoding them yields a 400, so they are passed through.
RESTLI_SAFE_CHARS = "(),:*~!"


def _short(url: str) -> str:
    """A log-friendly label for a Voyager URL.

    Full URLs are long, repetitive, and carry the query string -- which is
    exactly where a secret would hide. The path plus the interesting query
    parameter is what actually identifies a call.
    """
    trimmed = url.replace(VOYAGER_BASE + "/", "")
    path, _, query = trimmed.partition("?")
    for marker in ("sectionType:", "vanityName:", "memberIdentity="):
        if marker in query:
            fragment = query.split(marker, 1)[1]
            value = re.split(r"[,)&]", fragment)[0]
            return f"{path}[{marker.rstrip(':=')}={value}]"
    return path


class OutboundLimiter:
    """Token bucket over outbound LinkedIn requests, refilling continuously.

    This matters more than the inbound limit. Inbound limits protect the service
    from callers; this one protects the LinkedIn account from the service, and the
    account is the part that cannot be re-provisioned in thirty seconds.
    """

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
                # Small jitter even when we have budget: a burst of perfectly
                # evenly spaced requests is itself a bot signature.
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

    def __init__(self, settings: Settings, sessions: SessionManager) -> None:
        self._settings = settings
        self._sessions = sessions
        self._limiter = OutboundLimiter(settings.outbound_max_per_minute)
        self.breaker = CircuitBreaker()
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
            url = f"{url}?{urlencode(params, safe=RESTLI_SAFE_CHARS)}"

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
        """GET a LinkedIn URL as text.

        Used only to read JS asset bundles and the HTML that lists them, never to
        extract profile data. See queryids.py for why that distinction matters.
        """
        response = await self._request(url, accept="text/html,application/javascript,*/*")
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

    async def _request(self, url: str, *, accept: str | None = None) -> httpx.Response:
        last_error: Exception | None = None
        session_retried = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.breaker.check()
            await self._limiter.acquire()

            # Resolved per attempt so a mid-retry session refresh is picked up.
            headers = await self._authenticated_headers()
            if accept is not None:
                headers["Accept"] = accept

            call_started = time.monotonic()
            try:
                response = await self._client.get(url, headers=headers)
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

            # A dead cookie. Worth exactly one re-acquisition, not a retry loop.
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

            if status == 404:
                raise ProfileNotFound("LinkedIn has no profile at that identifier.")

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
        """Exponential backoff with full jitter.

        Full jitter rather than fixed: several concurrent section fetches failing
        together would otherwise retry in lockstep and reproduce the burst that
        caused the throttle in the first place.
        """
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        ceiling = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        await asyncio.sleep(random.uniform(0, ceiling))
