"""Typed failures, each mapping to exactly one HTTP status.

The point of naming these is that the API never has to guess what went wrong. A
dead session and a blocked IP both stop the request, but they are different
problems for whoever is operating the service, and the response says which.
"""

from __future__ import annotations


class LinkedInAPIError(Exception):
    """Base for every failure this service raises deliberately."""

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
    """No usable LinkedIn session — nothing configured, or the cookie is dead."""

    status_code = 503
    reason = "linkedin_session_unavailable"


class CredentialsRejected(SessionUnavailable):
    """LinkedIn refused the email/password pair outright.

    Separated from the generic session failure because the remedy is completely
    different -- a wrong password is fixed by fixing the password, not by
    hunting for an expired cookie. Repeated failed logins are also themselves a
    risk signal to LinkedIn, so this one should stop a human, not trigger a retry.
    """

    reason = "linkedin_credentials_rejected"


class ChallengeRequired(SessionUnavailable):
    """LinkedIn interrupted login with a challenge.

    Deliberately terminal. We do not solve challenges or CAPTCHAs; a human logs in
    from a browser and supplies the resulting cookie instead.
    """

    reason = "linkedin_challenge_required"


class LinkedInBlocked(LinkedInAPIError):
    """LinkedIn refused the request outright — HTTP 999, or a 403 on a live session.

    Never retried. Retrying a block is how a throttled account becomes a banned one.
    """

    status_code = 503
    reason = "linkedin_blocked"


class LinkedInRateLimited(LinkedInAPIError):
    """LinkedIn returned 429. Retried with backoff internally; surfaced if it persists."""

    status_code = 503
    reason = "linkedin_rate_limited"


class UpstreamUnavailable(LinkedInAPIError):
    """Every fetch strategy failed for reasons that were not a block."""

    status_code = 503
    reason = "upstream_unavailable"


class QueryIdDiscoveryFailed(LinkedInAPIError):
    status_code = 503
    reason = "query_id_discovery_failed"


# --- Caller-facing (this API's own gate), not LinkedIn's ---------------------


class AuthenticationRequired(LinkedInAPIError):
    status_code = 401
    reason = "invalid_api_key"


class DemoScopeExceeded(LinkedInAPIError):
    status_code = 403
    reason = "demo_scope_exceeded"


class CallerRateLimited(LinkedInAPIError):
    status_code = 429
    reason = "rate_limited"


class QueryRejected(LinkedInAPIError):
    """Voyager rejected the GraphQL query itself, typically a stale queryId.

    Distinct from a transport failure because it is actionable: the queryId cache
    is invalidated and the call is retried once against freshly discovered ids.
    """

    status_code = 503
    reason = "query_rejected"
