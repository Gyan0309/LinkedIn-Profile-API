"""HTTP surface: the page, health, service state, and the profile endpoint."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Header, Query, Request, Security
from fastapi.responses import HTMLResponse

from app.api.deps import (
    caller_cookie,
    caller_timezone,
    caller_user_agent,
    linkedin_cookie_scheme,
    resolve_caller,
)
from app.errors import SessionUnavailable
from app.linkedin.auth import SessionManager, fingerprint_of
from app.linkedin.client import VoyagerClient
from app.linkedin.fetch import ProfileFetcher
from app.linkedin.urls import canonical_profile_url, extract_public_identifier
from app.logging_config import stage
from app.schema import ProfileResponse, ResponseMeta

logger = logging.getLogger(__name__)

router = APIRouter()

INDEX_PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"


@router.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    """The web UI. Takes a cookie and a URL, calls this same API."""
    return HTMLResponse(INDEX_PAGE.read_text(encoding="utf-8"))


@router.get("/healthz", tags=["ops"], summary="Liveness probe")
async def healthz(request: Request) -> dict[str, object]:
    """Liveness. Never touches LinkedIn -- that would spend a caller's budget."""
    state = request.app.state
    return {
        "status": "ok",
        "stores_credentials": False,
        "circuit_breaker": state.breaker.status(),
        "cache": state.cache.stats(),
    }


@router.get("/v1/session", tags=["ops"], summary="Operational state")
async def session_status(request: Request) -> dict[str, object]:
    """Circuit breaker, queryId state, cache stats. Open: no secrets here."""
    state = request.app.state
    caller = resolve_caller(request)
    return {
        "stores_credentials": False,
        "query_ids": state.registry.snapshot(),
        "circuit_breaker": state.breaker.status(),
        "cache": state.cache.stats(),
        "caller": state.limiter.snapshot(caller),
    }


@router.get(
    "/v1/profile",
    tags=["profile"],
    response_model=ProfileResponse,
    summary="Fetch a LinkedIn profile as structured JSON",
)
async def get_profile(
    request: Request,
    url: str = Query(
        ...,
        description="A LinkedIn profile URL, or a bare public identifier.",
        examples=["https://www.linkedin.com/in/williamhgates"],
    ),
    refresh: bool = Query(
        False, description="Bypass the cache and refetch from LinkedIn."
    ),
    _cookie: str | None = Security(linkedin_cookie_scheme),
    _ua: str | None = Header(
        None,
        alias="X-LinkedIn-UA",
        description=(
            "The User-Agent of the browser your cookie came from. Optional, but "
            "send it: LinkedIn ties a session to a device fingerprint, and a "
            "mismatch reads as a stolen cookie and gets the session killed."
        ),
    ),
    _tz: str | None = Header(
        None,
        alias="X-LinkedIn-TZ",
        description="Your UTC offset in hours, e.g. 5.5. Defaults to 0.",
    ),
) -> ProfileResponse:
    state = request.app.state
    started = time.perf_counter()

    caller = resolve_caller(request)
    state.limiter.check(caller)

    public_identifier = extract_public_identifier(url)
    stage(logger, "resolved", identifier=public_identifier, caller=caller.identifier)

    cookie = caller_cookie(request)
    if not cookie:
        raise SessionUnavailable(
            "This service stores no LinkedIn credentials, so it needs yours. "
            "Send the whole Cookie header from a logged-in browser request in "
            "the X-LinkedIn-Cookie header. See the README, 'Getting a session'."
        )

    # Keyed per session: two accounts see different amounts of the same
    # profile, so a shared key would leak one caller's view to another.
    session_key = fingerprint_of(cookie)
    cache_key = f"{session_key}:{public_identifier}"

    cache_status = "miss"
    cached = None if refresh else await state.cache.get(cache_key)

    if cached is not None:
        cache_status = "hit"
        profile, source, unavailable = cached
        stage(logger, "cache", "HIT - no LinkedIn call", identifier=public_identifier)
    else:
        reason = "refresh requested" if refresh else "not cached"
        stage(
            logger,
            "cache",
            f"MISS ({reason}) - fetching upstream",
            session=session_key,
        )
        result = await _fetch(
            state,
            public_identifier,
            cookie,
            caller_user_agent(request),
            caller_timezone(request),
        )
        profile = result.profile
        source = result.source_label
        unavailable = result.sections_unavailable
        await state.cache.set(cache_key, (profile, source, unavailable))

    duration_ms = int((time.perf_counter() - started) * 1000)
    stage(
        logger,
        "served",
        profile.name.full or "(no name)",
        source=source,
        cache=cache_status,
        unavailable=len(unavailable),
        ms=duration_ms,
    )
    if unavailable:
        stage(
            logger,
            "gaps",
            "sections that could NOT be fetched",
            sections=",".join(unavailable),
            level=logging.WARNING,
        )

    return ProfileResponse(
        meta=ResponseMeta(
            profile_url=canonical_profile_url(public_identifier),
            public_identifier=public_identifier,
            profile_urn=profile.profile_urn,
            fetched_at=datetime.now(UTC),
            duration_ms=duration_ms,
            cache=cache_status,
            source=source,
            sections_unavailable=unavailable,
        ),
        profile=profile,
    )


async def _fetch(
    state,
    public_identifier: str,
    cookie: str,
    user_agent: str = "",
    timezone_offset: str = "",
):
    """Run the chain with the caller's session.

    A client per request, since httpx binds to one cookie set. The limiter,
    breaker and registry are borrowed from app state -- they describe LinkedIn
    and our shared IP, not the caller.
    """
    sessions = SessionManager(
        state.settings,
        cookie_override=cookie,
        user_agent=user_agent,
        timezone_offset=timezone_offset,
    )
    client = VoyagerClient(
        state.settings, sessions, limiter=state.outbound, breaker=state.breaker
    )
    try:
        return await ProfileFetcher(client, state.registry).fetch(public_identifier)
    finally:
        await client.aclose()
