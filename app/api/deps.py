"""Caller-facing gate: a per-IP rate limit, and nothing else.

An API key and demo allowlist used to live here to stop strangers spending our
LinkedIn budget. With callers supplying their own sessions there is no such
budget, so they gated nothing. The cookie is the credential now.

The rate limit stays because the outbound IP really is shared, and LinkedIn
blocks an IP as a unit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.security import APIKeyHeader

from app.errors import CallerRateLimited

linkedin_cookie_scheme = APIKeyHeader(
    name="X-LinkedIn-Cookie",
    scheme_name="LinkedInSession",
    auto_error=False,
    description=(
        "Your LinkedIn session: the whole Cookie header from a logged-in browser "
        "request (DevTools > Network > any www.linkedin.com request > Request "
        "Headers > cookie). This service stores no credentials of its own, so "
        "every request needs one. It is never logged and never written to disk."
    ),
)


def caller_cookie(request: Request) -> str:
    """The LinkedIn session a caller supplied, if any."""
    return (request.headers.get("x-linkedin-cookie") or "").strip()


def caller_user_agent(request: Request) -> str:
    """The browser the caller's cookie came from.

    Optional, but sending it is what stops LinkedIn seeing a second device
    using a stolen session and invalidating the cookie everywhere.
    """
    return request.headers.get("x-linkedin-ua") or ""


def caller_timezone(request: Request) -> str:
    """The caller's UTC offset in hours, as their browser reports it."""
    return request.headers.get("x-linkedin-tz") or ""


@dataclass
class Caller:
    """Who is asking, for rate-limiting purposes only."""

    identifier: str


@dataclass
class _Window:
    count: int = 0
    resets_at: float = 0.0


@dataclass
class RateLimiter:
    """Fixed-window per-caller limiter. The outbound limiter absorbs bursts."""

    per_hour: int
    _windows: dict[str, _Window] = field(default_factory=dict)

    def check(self, caller: Caller) -> None:
        now = time.time()
        window = self._windows.setdefault(caller.identifier, _Window())

        if now >= window.resets_at:
            window.count = 0
            window.resets_at = now + 3600

        if window.count >= self.per_hour:
            raise CallerRateLimited(
                f"Rate limit of {self.per_hour} requests/hour reached.",
                retry_after=max(1, int(window.resets_at - now)),
            )
        window.count += 1

    def snapshot(self, caller: Caller) -> dict[str, object]:
        window = self._windows.get(caller.identifier)
        return {
            "limit_per_hour": self.per_hour,
            "used": window.count if window else 0,
        }


def resolve_caller(request: Request) -> Caller:
    """Identify the caller by address, for rate limiting."""
    client_host = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # TLS is terminated upstream, so the real client is first in the list.
        client_host = forwarded.split(",")[0].strip() or client_host
    return Caller(identifier=f"ip:{client_host}")
