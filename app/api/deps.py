"""Caller-facing gate: who may ask for what, and how often.

The service is a public HTTPS endpoint sitting in front of one LinkedIn session,
so an open proxy would hand anyone the ability to spend that account's request
budget -- and the account is the part that cannot be re-provisioned in thirty
seconds. The compromise is a two-tier gate.

Without a key, a caller may query a small fixed allowlist of well-known public
profiles. That is enough to evaluate the API end to end -- paste a URL, get JSON
back -- without letting the internet aim the session wherever it likes.

With a key, any profile URL is allowed, at a higher rate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import Request

from app.config import Settings
from app.errors import AuthenticationRequired, CallerRateLimited, DemoScopeExceeded


@dataclass
class Caller:
    """The resolved identity and entitlements of one request."""

    keyed: bool
    identifier: str

    @property
    def tier(self) -> str:
        return "keyed" if self.keyed else "demo"


@dataclass
class _Window:
    count: int = 0
    resets_at: float = 0.0


@dataclass
class RateLimiter:
    """Fixed-window per-caller limiter.

    A fixed window rather than a sliding one: the failure mode of a fixed window
    is that a caller can burst across a boundary, and the outbound limiter in
    client.py absorbs that anyway. It is not worth more machinery here.
    """

    demo_per_hour: int
    keyed_per_hour: int
    _windows: dict[str, _Window] = field(default_factory=dict)

    def limit_for(self, caller: Caller) -> int:
        return self.keyed_per_hour if caller.keyed else self.demo_per_hour

    def check(self, caller: Caller) -> None:
        limit = self.limit_for(caller)
        now = time.time()
        window = self._windows.setdefault(caller.identifier, _Window())

        if now >= window.resets_at:
            window.count = 0
            window.resets_at = now + 3600

        if window.count >= limit:
            raise CallerRateLimited(
                f"Rate limit of {limit} requests/hour reached for the "
                f"{caller.tier} tier.",
                retry_after=max(1, int(window.resets_at - now)),
            )
        window.count += 1

    def snapshot(self, caller: Caller) -> dict[str, object]:
        window = self._windows.get(caller.identifier)
        return {
            "tier": caller.tier,
            "limit_per_hour": self.limit_for(caller),
            "used": window.count if window else 0,
        }


def resolve_caller(request: Request, settings: Settings) -> Caller:
    """Identify the caller from its API key, or fall back to the demo tier."""
    presented = (request.headers.get("x-api-key") or "").strip()

    if presented:
        if presented not in settings.api_keys:
            raise AuthenticationRequired("The API key presented is not recognised.")
        # Keyed callers are limited per key, not per IP, so a team behind one NAT
        # is not throttled as though it were a single caller.
        return Caller(keyed=True, identifier=f"key:{presented[:8]}")

    client_host = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # Render terminates TLS upstream, so the real client is first in the list.
        client_host = forwarded.split(",")[0].strip() or client_host

    return Caller(keyed=False, identifier=f"ip:{client_host}")


def authorise_profile(
    caller: Caller, public_identifier: str, settings: Settings
) -> None:
    """Keyed callers may query anything; demo callers only the allowlist."""
    if caller.keyed:
        return

    allowlist = {name.lower() for name in settings.demo_profiles}
    if public_identifier.lower() in allowlist:
        return

    raise DemoScopeExceeded(
        "Without an API key this service serves only its demo profiles "
        f"({', '.join(sorted(allowlist)) or 'none configured'}). Present a valid "
        "X-API-Key header to query arbitrary profiles."
    )
