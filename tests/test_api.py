"""The HTTP surface: the session gate, the cache, and how failures reach callers.

The fetch is stubbed throughout. What is under test is everything around it --
who may ask, what the cache keeps separate, and whether a failure arrives as a
typed error or as a plausible-looking empty profile.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import RateLimiter
from app.cache import TTLCache
from app.errors import LinkedInBlocked, ProfileNotFound
from app.linkedin.client import CircuitBreaker, OutboundLimiter
from app.linkedin.fetch import FetchResult
from app.linkedin.queryids import QueryIdRegistry
from app.main import app
from app.schema import Name, Position, Profile

PROFILE = "someone-real"
COOKIE_A = 'li_at=AAAAsession; JSESSIONID="ajax:1111111111111111111"'
COOKIE_B = 'li_at=BBBBsession; JSESSIONID="ajax:2222222222222222222"'


class StubFetch:
    """Stands in for `routes._fetch`, recording the session it was handed."""

    def __init__(self, *, error: Exception | None = None, label: str = "stub") -> None:
        self.error = error
        self.label = label
        self.cookies: list[str] = []

    async def __call__(self, state, public_identifier: str, cookie: str):
        self.cookies.append(cookie)
        if self.error is not None:
            raise self.error
        return FetchResult(
            profile=Profile(
                public_identifier=public_identifier,
                profile_urn="urn:li:fsd_profile:SYNTHETIC",
                name=Name(first="Demo", last="Person", full=f"Seen by {self.label}"),
                headline="Synthetic profile",
                experience=[Position(title="Engineer", company="Kestrel Systems")],
            ),
            sources={"voyager-graphql"},
            sections_unavailable=["patents"],
        )


@pytest.fixture
def api(settings, monkeypatch):
    """A live app with a stubbed fetch and deterministic settings."""
    with TestClient(app) as client:
        fetch = StubFetch()
        monkeypatch.setattr("app.api.routes._fetch", fetch)
        app.state.settings = settings
        app.state.cache = TTLCache(settings.cache_ttl_seconds)
        app.state.limiter = RateLimiter(per_hour=settings.rate_limit_per_hour)
        app.state.registry = QueryIdRegistry(settings)
        app.state.outbound = OutboundLimiter(600)
        app.state.breaker = CircuitBreaker()
        yield client, fetch


def fetch_profile(client, cookie: str | None = COOKIE_A, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/v1/profile?url=https://www.linkedin.com/in/{PROFILE}"
    if query:
        url = f"{url}&{query}"
    headers = {"X-LinkedIn-Cookie": cookie} if cookie else {}
    return client.get(url, headers=headers)


# --- the session gate -------------------------------------------------------


def test_a_request_without_a_session_is_refused(api) -> None:
    """The cookie is the credential. There is no other way in."""
    client, fetch = api
    response = fetch_profile(client, cookie=None)

    assert response.status_code == 503
    assert response.json()["error"] == "linkedin_session_unavailable"
    assert "X-LinkedIn-Cookie" in response.json()["message"]
    # Refused before any upstream work.
    assert fetch.cookies == []


def test_a_request_with_a_session_succeeds(api) -> None:
    client, fetch = api
    response = fetch_profile(client)

    assert response.status_code == 200
    assert fetch.cookies == [COOKIE_A]


def test_no_api_key_is_required(api) -> None:
    """The API key was removed with the server-side session it protected."""
    client, _ = api
    assert fetch_profile(client).status_code == 200


def test_any_profile_is_queryable(api) -> None:
    """No demo allowlist: callers spend their own budget on whatever they like."""
    client, _ = api
    response = client.get(
        "/v1/profile?url=https://www.linkedin.com/in/anyone-at-all",
        headers={"X-LinkedIn-Cookie": COOKIE_A},
    )
    assert response.status_code == 200


def test_callers_are_rate_limited(api) -> None:
    client, _ = api

    for _ in range(3):  # rate_limit_per_hour is 3 in the test settings
        assert fetch_profile(client, refresh="true").status_code == 200

    limited = fetch_profile(client, refresh="true")
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0


# --- the response contract --------------------------------------------------


def test_meta_reports_source_and_unavailable_sections(api) -> None:
    client, _ = api
    body = fetch_profile(client).json()

    assert body["meta"]["source"] == "voyager-graphql"
    assert body["meta"]["cache"] == "miss"
    assert body["meta"]["sections_unavailable"] == ["patents"]
    assert body["meta"]["profile_url"] == f"https://www.linkedin.com/in/{PROFILE}"


def test_empty_section_and_unavailable_section_are_distinguishable(api) -> None:
    """The distinction the whole schema exists to preserve."""
    client, _ = api
    body = fetch_profile(client).json()

    # Fetched successfully, genuinely empty.
    assert body["profile"]["skills"] == []
    assert "skills" not in body["meta"]["sections_unavailable"]

    # Could not be fetched. Also [], but flagged.
    assert body["profile"]["patents"] == []
    assert "patents" in body["meta"]["sections_unavailable"]


# --- the cache --------------------------------------------------------------


def test_second_call_is_served_from_cache(api) -> None:
    client, fetch = api

    first = fetch_profile(client).json()
    second = fetch_profile(client).json()

    assert first["meta"]["cache"] == "miss"
    assert second["meta"]["cache"] == "hit"
    assert len(fetch.cookies) == 1


def test_refresh_bypasses_the_cache(api) -> None:
    client, fetch = api

    fetch_profile(client)
    refreshed = fetch_profile(client, refresh="true").json()

    assert refreshed["meta"]["cache"] == "miss"
    assert len(fetch.cookies) == 2


def test_url_variants_share_one_cache_entry(api) -> None:
    """Keyed on the identifier, not the raw URL a caller happened to paste."""
    client, fetch = api

    fetch_profile(client)
    second = client.get(
        f"/v1/profile?url=https://se.linkedin.com/in/{PROFILE}/?trk=abc",
        headers={"X-LinkedIn-Cookie": COOKIE_A},
    )

    assert second.json()["meta"]["cache"] == "hit"
    assert len(fetch.cookies) == 1


def test_two_sessions_do_not_share_a_cache_entry(api) -> None:
    """The privacy property.

    LinkedIn shows different accounts different amounts of the same profile, so
    serving caller B a result fetched with caller A's session would hand B
    exactly the data their own session was not entitled to see.
    """
    client, fetch = api

    first = fetch_profile(client, cookie=COOKIE_A).json()
    second = fetch_profile(client, cookie=COOKIE_B).json()

    assert first["meta"]["cache"] == "miss"
    assert second["meta"]["cache"] == "miss"  # not A's cached answer
    assert fetch.cookies == [COOKIE_A, COOKIE_B]


# --- failures ---------------------------------------------------------------


def test_invalid_url_is_a_400_naming_the_problem(api) -> None:
    client, _ = api
    response = client.get(
        "/v1/profile?url=https://www.linkedin.com/company/kestrel",
        headers={"X-LinkedIn-Cookie": COOKIE_A},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_profile_url"
    assert "company page" in response.json()["message"]


def test_missing_url_parameter_is_a_400(api) -> None:
    client, _ = api
    response = client.get("/v1/profile", headers={"X-LinkedIn-Cookie": COOKIE_A})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_block_surfaces_as_503_with_retry_after(api, monkeypatch) -> None:
    client, _ = api
    monkeypatch.setattr(
        "app.api.routes._fetch",
        StubFetch(error=LinkedInBlocked("host is flagged", retry_after=300)),
    )

    response = fetch_profile(client)

    assert response.status_code == 503
    assert response.json()["error"] == "linkedin_blocked"
    assert response.json()["retry_after_seconds"] == 300
    assert response.headers["Retry-After"] == "300"


def test_missing_profile_is_a_404(api, monkeypatch) -> None:
    client, _ = api
    monkeypatch.setattr(
        "app.api.routes._fetch", StubFetch(error=ProfileNotFound("no such profile"))
    )

    assert fetch_profile(client).status_code == 404


def test_a_failure_is_never_dressed_up_as_an_empty_profile(api, monkeypatch) -> None:
    """A 200 with an empty profile is indistinguishable from a real sparse one."""
    client, _ = api
    monkeypatch.setattr(
        "app.api.routes._fetch", StubFetch(error=LinkedInBlocked("blocked"))
    )

    response = fetch_profile(client)

    assert response.status_code != 200
    assert "profile" not in response.json()


# --- ops --------------------------------------------------------------------


def test_healthz_does_not_touch_linkedin(api) -> None:
    client, fetch = api
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["stores_credentials"] is False
    assert fetch.cookies == []


def test_session_diagnostics_are_open_and_hold_no_secret(api) -> None:
    """Nothing here describes anybody's credentials, so nothing gates it."""
    client, _ = api
    body = client.get("/v1/session").json()

    assert body["stores_credentials"] is False
    assert "circuit_breaker" in body
    assert "li_at" not in client.get("/v1/session").text


def test_the_linkedin_cookie_scheme_is_advertised(api) -> None:
    """So /docs offers a field for it instead of silently refusing."""
    client, _ = api
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]

    assert schemes["LinkedInSession"]["name"] == "X-LinkedIn-Cookie"
    assert schemes["LinkedInSession"]["in"] == "header"
