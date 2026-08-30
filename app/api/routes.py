"""HTTP surface.

Four routes and no more. `/healthz` is for the platform, `/v1/session` is for
whoever operates the service, and `/v1/profile` is the product. `/v1/demo`
exists only so a reviewer with no API key can see what the shape looks like
before deciding whether to ask for one.

Every decision point in `/v1/profile` logs a stage line -- caller tier, resolved
identifier, authorisation, cache, then the outcome -- so the log shows *why* a
request did what it did, not merely that it happened.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request

from app.api.deps import authorise_profile, resolve_caller
from app.linkedin.urls import canonical_profile_url, extract_public_identifier
from app.logging_config import stage
from app.schema import ProfileResponse, ResponseMeta

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz", tags=["ops"], summary="Liveness probe")
async def healthz(request: Request) -> dict[str, object]:
    """Liveness only.

    Deliberately does not touch LinkedIn. A health check that made an upstream
    request would spend the account's budget every time the platform pinged it,
    and would report the service unhealthy for an outage it does not own.
    """
    state = request.app.state
    return {
        "status": "ok",
        "linkedin_session_configured": (
            state.settings.has_cookie_session or state.settings.has_login_credentials
        ),
        "circuit_breaker": state.client.breaker.status(),
        "cache": state.cache.stats(),
    }


@router.get("/v1/session", tags=["ops"], summary="Session diagnostics")
async def session_status(request: Request) -> dict[str, object]:
    """What the operator needs to debug a session, and none of the secret.

    Requires a key: the fingerprint and queryId state are operational detail, not
    something a demo caller has any business reading.
    """
    state = request.app.state
    caller = resolve_caller(request, state.settings)
    authorise_profile(caller, "__session__", state.settings)

    session = state.sessions.current
    stage(logger, "session", "diagnostics read", tier=caller.tier)

    return {
        "session": session.redacted() if session else None,
        "query_ids": state.registry.snapshot(),
        "circuit_breaker": state.client.breaker.status(),
        "cache": state.cache.stats(),
        "caller": state.limiter.snapshot(caller),
    }


@router.get("/v1/demo", tags=["profile"], summary="Profiles callable without a key")
async def demo_profiles(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "demo_profiles": settings.demo_profiles,
        "note": (
            "These identifiers are queryable without an API key. Present "
            "X-API-Key to query any profile URL."
        ),
        "example": (
            f"/v1/profile?url=https://www.linkedin.com/in/{settings.demo_profiles[0]}"
            if settings.demo_profiles
            else None
        ),
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
) -> ProfileResponse:
    state = request.app.state
    started = time.perf_counter()

    caller = resolve_caller(request, state.settings)
    stage(logger, "caller", tier=caller.tier, id=caller.identifier)

    public_identifier = extract_public_identifier(url)
    stage(logger, "resolved", identifier=public_identifier)

    authorise_profile(caller, public_identifier, state.settings)
    state.limiter.check(caller)
    stage(logger, "authorised", **state.limiter.snapshot(caller))

    cache_status = "miss"
    cached = None if refresh else await state.cache.get(public_identifier)

    if cached is not None:
        cache_status = "hit"
        profile, source, unavailable = cached
        stage(logger, "cache", "HIT - no LinkedIn call", identifier=public_identifier)
    else:
        reason = "refresh requested" if refresh else "not cached"
        stage(logger, "cache", f"MISS ({reason}) - fetching upstream")
        result = await state.fetcher.fetch(public_identifier)
        profile = result.profile
        source = result.source_label
        unavailable = result.sections_unavailable
        await state.cache.set(public_identifier, (profile, source, unavailable))

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
