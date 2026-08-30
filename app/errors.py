"""Typed failures. Each maps to one HTTP status and a stable reason code."""

from __future__ import annotations


class LinkedInAPIError(Exception):
    status_code = 500
    reason = "internal_error"

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class InvalidProfileURL(LinkedInAPIError):
    status_code = 400
    reason = "invalid_profile_url"


class ProfileNotFound(LinkedInAPIError):
    status_code = 404
    reason = "profile_not_found"


class SessionUnavailable(LinkedInAPIError):
    """No usable session: none supplied, or the cookie is dead."""

    status_code = 503
    reason = "linkedin_session_unavailable"


class SessionRejected(SessionUnavailable):
    """Voyager redirected to a login page instead of answering.

    A 3xx from Voyager is never a real redirect, so it always means the session
    was not accepted.
    """

    reason = "linkedin_session_rejected"


class LinkedInBlocked(LinkedInAPIError):
    """HTTP 999, or a 403 on a live session.

    Never retried: retrying a block is how a throttled account becomes a banned
    one.
    """

    status_code = 503
    reason = "linkedin_blocked"


class LinkedInRateLimited(LinkedInAPIError):
    status_code = 503
    reason = "linkedin_rate_limited"


class EndpointRetired(LinkedInAPIError):
    """LinkedIn has withdrawn this endpoint (410). Expected, not an incident."""

    status_code = 503
    reason = "endpoint_retired"


class UpstreamUnavailable(LinkedInAPIError):
    status_code = 503
    reason = "upstream_unavailable"


class QueryRejected(LinkedInAPIError):
    """Voyager rejected the GraphQL query itself, usually a stale queryId."""

    status_code = 503
    reason = "query_rejected"


class QueryIdDiscoveryFailed(LinkedInAPIError):
    status_code = 503
    reason = "query_id_discovery_failed"


class CallerRateLimited(LinkedInAPIError):
    status_code = 429
    reason = "rate_limited"
