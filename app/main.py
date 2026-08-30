"""Application entry point: `uvicorn app.main:app`.

Wiring and error translation only. Every piece with real logic lives behind it --
`app/linkedin/` talks to LinkedIn, `app/extract/` reads what comes back, and
`app/api/` decides who is allowed to ask.

The exception handler is the load-bearing part of this module. Every failure this
service raises deliberately is a `LinkedInAPIError` carrying its own status and a
stable reason code, so a caller can branch on `error` without parsing prose, and
an unexpected exception is reported as an unexpected exception rather than being
dressed up as an empty profile.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import RateLimiter
from app.api.routes import router
from app.cache import TTLCache
from app.config import get_settings
from app.errors import LinkedInAPIError
from app.linkedin.auth import SessionManager
from app.linkedin.client import VoyagerClient
from app.linkedin.fetch import ProfileFetcher
from app.linkedin.queryids import QueryIdRegistry
from app.logging_config import (
    configure_logging,
    get_request_id,
    new_request_id,
    set_request_id,
    stage,
)

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the object graph once, and close the HTTP client on the way out.

    Nothing here contacts LinkedIn. The session is acquired lazily on the first
    real request, so the service starts and reports healthy even when the cookie
    is missing or expired -- that is an operational problem to surface through
    /v1/session, not a reason to refuse to boot.
    """
    app.state.settings = settings
    app.state.sessions = SessionManager(settings)
    app.state.client = VoyagerClient(settings, app.state.sessions)
    app.state.registry = QueryIdRegistry(settings, app.state.client)
    app.state.fetcher = ProfileFetcher(app.state.client, app.state.registry)
    app.state.cache = TTLCache(settings.cache_ttl_seconds)
    app.state.limiter = RateLimiter(
        demo_per_hour=settings.demo_rate_limit_per_hour,
        keyed_per_hour=settings.keyed_rate_limit_per_hour,
    )

    logger.info(
        "started: session=%s keys=%d demo_profiles=%d proxy=%s",
        "configured"
        if settings.has_cookie_session or settings.has_login_credentials
        else "MISSING",
        len(settings.api_keys),
        len(settings.demo_profiles),
        "yes" if settings.outbound_proxy_url else "no",
    )

    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(
    title="LinkedIn Profile API",
    version="1.0.0",
    description=(
        "Structured JSON for a LinkedIn profile URL, read from LinkedIn's "
        "internal Voyager API over authenticated HTTP. No browser automation and "
        "no HTML scraping.\n\n"
        "**Read `meta.sections_unavailable` before trusting an empty list.** A "
        "section listed there could not be fetched; a section absent from it and "
        "returning `[]` genuinely has no entries."
    ),
    lifespan=lifespan,
)

app.include_router(router)


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    """Assign a request id and bookend every request with a log line.

    The id is echoed in the `X-Request-ID` response header, so a caller reporting
    a problem can quote it and land directly on the right lines in the log
    instead of describing what they saw.
    """
    incoming = request.headers.get("x-request-id", "").strip()
    # Honour a caller-supplied id so a trace can span a proxy, but bound it --
    # this value goes into every log line and must not become a payload.
    request_id = incoming[:32] if incoming else new_request_id()
    set_request_id(request_id)

    started = time.perf_counter()
    query = f"?{request.url.query}" if request.url.query else ""
    stage(logger, "-> request", f"{request.method} {request.url.path}{query}")

    try:
        response = await call_next(request)
    except Exception:
        elapsed = int((time.perf_counter() - started) * 1000)
        stage(logger, "<- request", "unhandled exception", ms=elapsed,
              level=logging.ERROR)
        raise

    elapsed = int((time.perf_counter() - started) * 1000)
    level = logging.WARNING if response.status_code >= 500 else logging.INFO
    stage(
        logger,
        "<- request",
        status=response.status_code,
        ms=elapsed,
        level=level,
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(LinkedInAPIError)
async def handle_api_error(_: Request, exc: LinkedInAPIError) -> JSONResponse:
    """Every deliberate failure, rendered with its stable reason code."""
    body: dict[str, Any] = {"error": exc.reason, "message": exc.message}
    headers = {"X-Request-ID": get_request_id()}

    if exc.retry_after is not None:
        body["retry_after_seconds"] = exc.retry_after
        headers["Retry-After"] = str(exc.retry_after)

    # 4xx is the caller's problem and is expected traffic; 5xx is ours.
    level = logging.ERROR if exc.status_code >= 500 else logging.WARNING
    stage(logger, "FAILED", exc.message, error=exc.reason, status=exc.status_code,
          level=level)

    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": "invalid_request",
            "message": "The request parameters were not valid.",
            "detail": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    """An unexpected failure is reported as one.

    Never degraded into a 200 with an empty profile: a caller cannot tell that
    apart from a real person with no work history, and silently returning the
    wrong answer is worse than returning none.
    """
    logger.exception("unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "The service failed to handle the request.",
        },
    )
