"""The strategy chain: fallback, per-section merge, and honest gaps.

These are the tests behind the README's central claim -- that a partial failure
degrades to a smaller answer plus an explicit list of what is missing, rather
than to a wrong answer that looks complete.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.errors import LinkedInBlocked, SessionUnavailable, UpstreamUnavailable
from app.linkedin.auth import SessionManager
from app.linkedin.client import VoyagerClient
from app.linkedin.fetch import (
    SOURCE_GRAPHQL,
    SOURCE_PROFILEVIEW,
    ProfileFetcher,
)
from app.linkedin.queryids import QueryIdRegistry
from tests.conftest import load_fixture

GRAPHQL = "https://www.linkedin.com/voyager/api/graphql"
PROFILEVIEW = (
    "https://www.linkedin.com/voyager/api/identity/profiles/"
    "ada-sundqvist-synthetic/profileView"
)
DASH = "https://www.linkedin.com/voyager/api/identity/dash/profiles"

SLUG = "ada-sundqvist-synthetic"

PINNED = json.dumps(
    {
        "voyagerIdentityDashProfiles": "a" * 32,
        "voyagerIdentityDashProfileComponents": "b" * 32,
    }
)


@pytest.fixture
def fetcher(monkeypatch: pytest.MonkeyPatch) -> ProfileFetcher:
    async def no_wait(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(VoyagerClient, "_backoff", staticmethod(no_wait))
    monkeypatch.setattr("app.linkedin.client.OutboundLimiter.acquire", no_wait)

    settings = Settings(
        _env_file=None,
        linkedin_li_at="synthetic-cookie",
        # Pinned so no test in this file performs queryId discovery over the wire.
        linkedin_query_ids_json=PINNED,
    )
    client = VoyagerClient(settings, SessionManager(settings))
    return ProfileFetcher(client, QueryIdRegistry(settings, client))


def graphql_router(*, sections: dict[str, str] | None = None, profile: bool = True):
    """Route GraphQL calls by what the `variables` tuple asks for."""
    sections = sections or {}

    def handler(request: httpx.Request) -> httpx.Response:
        variables = request.url.params.get("variables", "")
        if "vanityName" in variables:
            if not profile:
                return httpx.Response(400)
            return httpx.Response(200, json=load_fixture("graphql_profile.json"))
        for section_type, fixture_name in sections.items():
            if f"sectionType:{section_type}" in variables:
                return httpx.Response(200, json=load_fixture(fixture_name))
        # Anything not explicitly served is a section LinkedIn would not give us.
        return httpx.Response(400)

    return handler


@respx.mock
async def test_graphql_serves_everything_it_can(fetcher: ProfileFetcher) -> None:
    respx.get(url__startswith=GRAPHQL).mock(
        side_effect=graphql_router(
            sections={
                "experience": "graphql_experience_card.json",
                "skills": "graphql_skills_card.json",
            }
        )
    )
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(404))

    result = await fetcher.fetch(SLUG)

    assert result.source_label == SOURCE_GRAPHQL
    assert result.profile.name.full == "Ada Sundqvist"
    assert result.profile.pronouns == "SHE_HER"
    assert result.profile.open_to_work is True
    assert result.profile.premium is True
    assert result.profile.location.raw == "Stockholm, Stockholm County, Sweden"
    assert result.profile.industry == "Software Development"

    assert [p.company for p in result.profile.experience] == [
        "Kestrel Systems",
        "Halstead Payments",
    ]
    assert [s.name for s in result.profile.skills][0] == "Distributed Systems"


@respx.mock
async def test_sections_graphql_could_not_serve_are_named(
    fetcher: ProfileFetcher,
) -> None:
    """The sections that 400'd must be reported, not returned as empty lists."""
    respx.get(url__startswith=GRAPHQL).mock(
        side_effect=graphql_router(sections={"experience": "graphql_experience_card.json"})
    )
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(404))

    result = await fetcher.fetch(SLUG)

    assert result.profile.experience  # served
    assert "experience" not in result.sections_unavailable
    for missing in ("skills", "education", "certifications", "languages", "patents"):
        assert missing in result.sections_unavailable


@respx.mock
async def test_profileview_backfills_only_the_missing_sections(
    fetcher: ProfileFetcher,
) -> None:
    """GraphQL wins where it answered; the legacy endpoint fills the rest."""
    respx.get(url__startswith=GRAPHQL).mock(
        side_effect=graphql_router(sections={"experience": "graphql_experience_card.json"})
    )
    respx.get(PROFILEVIEW).mock(
        return_value=httpx.Response(200, json=load_fixture("profileview_dense.json"))
    )

    result = await fetcher.fetch(SLUG)

    assert result.source_label == "mixed"
    assert result.sources == {SOURCE_GRAPHQL, SOURCE_PROFILEVIEW}

    # Experience came from GraphQL: the grouped company survives, which the
    # profileView fixture does not model.
    assert result.profile.experience[0].sub_positions

    # These only exist in the profileView fixture.
    assert [e.school for e in result.profile.education] == [
        "KTH Royal Institute of Technology"
    ]
    assert result.profile.certifications[0].credential_id == "CKA-SYNTHETIC-0001"
    assert result.sections_unavailable == []


@respx.mock
async def test_falls_back_entirely_when_graphql_is_unavailable(
    fetcher: ProfileFetcher,
) -> None:
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(500))
    respx.get(PROFILEVIEW).mock(
        return_value=httpx.Response(200, json=load_fixture("profileview_dense.json"))
    )

    result = await fetcher.fetch(SLUG)

    assert result.source_label == SOURCE_PROFILEVIEW
    assert result.profile.name.full == "Ada Sundqvist"
    assert result.sections_unavailable == []


@respx.mock
async def test_dash_is_the_last_resort_and_admits_what_it_lacks(
    fetcher: ProfileFetcher,
) -> None:
    """Dash gets a top card only, so every section stays flagged as unavailable."""
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(500))
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=DASH).mock(
        return_value=httpx.Response(200, json=load_fixture("graphql_profile.json"))
    )

    result = await fetcher.fetch(SLUG)

    assert result.source_label == "voyager-rest-dash"
    assert result.profile.name.full == "Ada Sundqvist"
    assert len(result.sections_unavailable) == 12


@respx.mock
async def test_every_strategy_failing_raises_rather_than_returning_a_shell(
    fetcher: ProfileFetcher,
) -> None:
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(500))
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=DASH).mock(return_value=httpx.Response(500))

    with pytest.raises(UpstreamUnavailable):
        await fetcher.fetch(SLUG)


@respx.mock
async def test_a_block_stops_the_chain_instead_of_trying_the_next_endpoint(
    fetcher: ProfileFetcher,
) -> None:
    """Falling through to S2 after a 999 would just deepen the block."""
    graphql = respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(999))
    profileview = respx.get(PROFILEVIEW).mock(return_value=httpx.Response(200, json={}))
    dash = respx.get(url__startswith=DASH).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(LinkedInBlocked):
        await fetcher.fetch(SLUG)

    assert graphql.call_count == 1
    assert profileview.call_count == 0
    assert dash.call_count == 0


async def test_missing_session_is_reported_as_such_not_as_a_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every strategy needs the same session, so a missing one is terminal.

    Reporting `upstream_unavailable` here would send an operator hunting a
    LinkedIn outage when the real problem is an unset environment variable.
    """

    async def no_wait(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.linkedin.client.OutboundLimiter.acquire", no_wait)

    settings = Settings(_env_file=None, linkedin_li_at="", linkedin_email="")
    client = VoyagerClient(settings, SessionManager(settings))
    fetcher = ProfileFetcher(client, QueryIdRegistry(settings, client))

    with pytest.raises(SessionUnavailable) as excinfo:
        await fetcher.fetch(SLUG)

    assert excinfo.value.reason == "linkedin_session_unavailable"
    assert "LINKEDIN_LI_AT" in str(excinfo.value)


@respx.mock
async def test_profileview_that_yields_nothing_does_not_claim_the_source(
    fetcher: ProfileFetcher,
) -> None:
    """A retired endpoint returns 200 with nothing useful; that is not an answer."""
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(500))
    respx.get(PROFILEVIEW).mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )
    respx.get(url__startswith=DASH).mock(
        return_value=httpx.Response(200, json=load_fixture("graphql_profile.json"))
    )

    result = await fetcher.fetch(SLUG)

    assert SOURCE_PROFILEVIEW not in result.sources
    assert result.source_label == "voyager-rest-dash"


@respx.mock
async def test_stale_query_id_is_rediscovered_once_then_retried(
    fetcher: ProfileFetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected queryId self-heals instead of failing the request."""
    rediscoveries: list[str] = []

    async def fake_discover(self) -> None:
        rediscoveries.append("discovered")
        self._discovered = {
            "voyagerIdentityDashProfiles": "c" * 32,
            "voyagerIdentityDashProfileComponents": "d" * 32,
        }
        self._discovered_at = 1e12  # far future, so the cache reads as fresh

    monkeypatch.setattr(QueryIdRegistry, "_discover", fake_discover)
    # Unpin so the registry has to resolve through discovery.
    monkeypatch.setattr(
        type(fetcher._registry), "pinned", property(lambda self: {})
    )

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query_id = request.url.params.get("queryId", "")
        variables = request.url.params.get("variables", "")
        calls.append(query_id)
        if "vanityName" not in variables:
            return httpx.Response(400)
        # The first id is stale; the rediscovered one works.
        if query_id.startswith("c"):
            return httpx.Response(200, json=load_fixture("graphql_profile.json"))
        return httpx.Response(400)

    respx.get(url__startswith=GRAPHQL).mock(side_effect=handler)
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(404))

    result = await fetcher.fetch(SLUG)

    assert result.profile.name.full == "Ada Sundqvist"
    assert rediscoveries, "a rejected queryId should trigger rediscovery"


@respx.mock
async def test_rediscovery_is_bounded_to_one_retry(
    fetcher: ProfileFetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the fresh id is also rejected, stop -- do not loop burning requests."""
    attempts: list[str] = []

    async def fake_discover(self) -> None:
        self._discovered = {
            "voyagerIdentityDashProfiles": "e" * 32,
            "voyagerIdentityDashProfileComponents": "f" * 32,
        }
        self._discovered_at = 1e12

    monkeypatch.setattr(QueryIdRegistry, "_discover", fake_discover)
    monkeypatch.setattr(type(fetcher._registry), "pinned", property(lambda self: {}))

    def handler(request: httpx.Request) -> httpx.Response:
        if "vanityName" in request.url.params.get("variables", ""):
            attempts.append(request.url.params.get("queryId", ""))
        return httpx.Response(400)

    respx.get(url__startswith=GRAPHQL).mock(side_effect=handler)
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=DASH).mock(return_value=httpx.Response(500))

    with pytest.raises(UpstreamUnavailable):
        await fetcher.fetch(SLUG)

    # The vanity lookup is tried exactly twice: original, then rediscovered.
    assert len(attempts) == 2
