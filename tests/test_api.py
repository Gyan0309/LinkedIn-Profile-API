"""The HTTP surface: the gate, the cache, and how failures reach the caller.

The fetcher is stubbed throughout. What is under test here is everything around
it -- who is allowed to ask, what the cache does, and whether a failure arrives
as a typed error or as a plausible-looking empty profile.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import RateLimiter
from app.cache import TTLCache
from app.errors import LinkedInBlocked, ProfileNotFound
from app.linkedin.fetch import FetchResult
from app.main import app
from app.schema import Name, Position, Profile

DEMO = "demo-person"
KEY = "test-key-alpha"


class StubFetcher:
    """Stands in for the strategy chain, and counts how often it was asked."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def fetch(self, public_identifier: str) -> FetchResult:
        self.calls.append(public_identifier)
        if self.error is not None:
            raise self.error
        profile = Profile(
            public_identifier=public_identifier,
            profile_urn="urn:li:fsd_profile:SYNTHETIC",
            name=Name(first="Demo", last="Person", full="Demo Person"),
            headline="Synthetic profile",
            experience=[Position(title="Engineer", company="Kestrel Systems")],
        )
        return FetchResult(
            profile=profile,
            sources={"voyager-graphql"},
            sections_unavailable=["patents"],
        )


@pytest.fixture
def api(settings):
    """A live app with a stubbed fetcher and deterministic settings."""
    with TestClient(app) as client:
        fetcher = StubFetcher()
        app.state.settings = settings
        app.state.fetcher = fetcher
        app.state.cache = TTLCache(settings.cache_ttl_seconds)
        app.state.limiter = RateLimiter(
            demo_per_hour=settings.demo_rate_limit_per_hour,
            keyed_per_hour=settings.keyed_rate_limit_per_hour,
        )
        yield client, fetcher


# --- the gate ---------------------------------------------------------------


def test_demo_profile_is_reachable_without_a_key(api) -> None:
    client, _ = api
    response = client.get(f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}")

    assert response.status_code == 200
    assert response.json()["profile"]["name"]["full"] == "Demo Person"


def test_arbitrary_profile_is_refused_without_a_key(api) -> None:
    client, fetcher = api
    response = client.get("/v1/profile?url=https://www.linkedin.com/in/someone-else")

    assert response.status_code == 403
    assert response.json()["error"] == "demo_scope_exceeded"
    # The gate must run before the fetch, not after it.
    assert fetcher.calls == []


def test_arbitrary_profile_is_allowed_with_a_key(api) -> None:
    client, _ = api
    response = client.get(
        "/v1/profile?url=https://www.linkedin.com/in/someone-else",
        headers={"X-API-Key": KEY},
    )

    assert response.status_code == 200
    assert response.json()["meta"]["public_identifier"] == "someone-else"


def test_unknown_key_is_rejected(api) -> None:
    client, fetcher = api
    response = client.get(
        f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}",
        headers={"X-API-Key": "not-a-real-key"},
    )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_api_key"
    assert fetcher.calls == []


def test_demo_tier_is_rate_limited(api) -> None:
    client, _ = api
    url = f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}&refresh=true"

    for _ in range(3):  # demo_rate_limit_per_hour is 3 in the test settings
        assert client.get(url).status_code == 200

    limited = client.get(url)
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0


# --- the response contract --------------------------------------------------


def test_meta_reports_source_and_unavailable_sections(api) -> None:
    client, _ = api
    body = client.get(f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}").json()

    assert body["meta"]["source"] == "voyager-graphql"
    assert body["meta"]["cache"] == "miss"
    assert body["meta"]["sections_unavailable"] == ["patents"]
    assert body["meta"]["profile_url"] == f"https://www.linkedin.com/in/{DEMO}"
    assert body["meta"]["duration_ms"] >= 0


def test_empty_section_and_unavailable_section_are_distinguishable(api) -> None:
    """The distinction the whole schema exists to preserve."""
    client, _ = api
    body = client.get(f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}").json()

    # Fetched successfully, genuinely empty.
    assert body["profile"]["skills"] == []
    assert "skills" not in body["meta"]["sections_unavailable"]

    # Could not be fetched. Also [], but flagged.
    assert body["profile"]["patents"] == []
    assert "patents" in body["meta"]["sections_unavailable"]


def test_second_call_is_served_from_cache(api) -> None:
    client, fetcher = api
    url = f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}"

    first = client.get(url).json()
    second = client.get(url).json()

    assert first["meta"]["cache"] == "miss"
    assert second["meta"]["cache"] == "hit"
    # One upstream fetch, not two.
    assert fetcher.calls == [DEMO]


def test_refresh_bypasses_the_cache(api) -> None:
    client, fetcher = api
    base = f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}"

    client.get(base)
    refreshed = client.get(f"{base}&refresh=true").json()

    assert refreshed["meta"]["cache"] == "miss"
    assert len(fetcher.calls) == 2


def test_url_variants_share_one_cache_entry(api) -> None:
    """Cache is keyed on the identifier, not the raw URL a caller happened to paste."""
    client, fetcher = api

    client.get(f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}")
    second = client.get(f"/v1/profile?url=https://se.linkedin.com/in/{DEMO}/?trk=abc")

    assert second.json()["meta"]["cache"] == "hit"
    assert len(fetcher.calls) == 1


# --- failures ---------------------------------------------------------------


def test_invalid_url_is_a_400_naming_the_problem(api) -> None:
    client, _ = api
    response = client.get("/v1/profile?url=https://www.linkedin.com/company/kestrel")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_profile_url"
    assert "company page" in response.json()["message"]


def test_missing_url_parameter_is_a_400(api) -> None:
    client, _ = api
    response = client.get("/v1/profile")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_block_surfaces_as_503_with_retry_after(api, settings) -> None:
    client, _ = api
    app.state.fetcher = StubFetcher(
        error=LinkedInBlocked("host is flagged", retry_after=300)
    )

    response = client.get(f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}")

    assert response.status_code == 503
    assert response.json()["error"] == "linkedin_blocked"
    assert response.json()["retry_after_seconds"] == 300
    assert response.headers["Retry-After"] == "300"


def test_missing_profile_is_a_404(api) -> None:
    client, _ = api
    app.state.fetcher = StubFetcher(error=ProfileNotFound("no such profile"))

    response = client.get(f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}")

    assert response.status_code == 404
    assert response.json()["error"] == "profile_not_found"


def test_a_failure_is_never_dressed_up_as_an_empty_profile(api) -> None:
    """A 200 with an empty profile is indistinguishable from a real sparse one."""
    client, _ = api
    app.state.fetcher = StubFetcher(error=LinkedInBlocked("blocked"))

    response = client.get(f"/v1/profile?url=https://www.linkedin.com/in/{DEMO}")

    assert response.status_code != 200
    assert "profile" not in response.json()


# --- ops --------------------------------------------------------------------


def test_healthz_does_not_touch_linkedin(api) -> None:
    client, fetcher = api
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert fetcher.calls == []


def test_demo_listing_is_public(api) -> None:
    client, _ = api
    body = client.get("/v1/demo").json()

    assert body["demo_profiles"] == [DEMO]
    assert DEMO in body["example"]


def test_session_diagnostics_require_a_key(api) -> None:
    client, _ = api

    assert client.get("/v1/session").status_code == 403

    keyed = client.get("/v1/session", headers={"X-API-Key": KEY})
    assert keyed.status_code == 200
    assert keyed.json()["caller"]["tier"] == "keyed"


def test_session_diagnostics_never_leak_the_cookie(api, settings) -> None:
    client, _ = api
    body = client.get("/v1/session", headers={"X-API-Key": KEY}).text

    assert "li_at" not in body or "li_at_fingerprint" in body
    assert settings.linkedin_li_at not in body or settings.linkedin_li_at == ""
