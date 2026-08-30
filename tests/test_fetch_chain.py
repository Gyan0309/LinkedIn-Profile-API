"""The fetch chain: identity, then sections, merged and honestly reported.

The order follows what linkedin.com actually calls, captured from its own
traffic rather than assumed.
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
    SOURCE_DASH,
    SOURCE_DASH_SECTIONS,
    SOURCE_PROFILEVIEW,
    ProfileFetcher,
)
from app.linkedin.queryids import QueryIdRegistry
from tests.conftest import load_fixture

VOYAGER = "https://www.linkedin.com/voyager/api"
DASH_PROFILES = f"{VOYAGER}/identity/dash/profiles"
DASH_SECTION = f"{VOYAGER}/identity/dash/profile"
# Matches the section collections but NOT `.../dash/profiles`, the identity
# endpoint -- a plain startswith on `profile` swallows both, and respx is
# first-match-wins, so the catch-all would answer the identity call.
SECTION_RE = (
    r"https://www\.linkedin\.com/voyager/api/identity/dash/profile(?!s\?)[A-Za-z]+"
)
PROFILEVIEW = f"{VOYAGER}/identity/profiles/ada-sundqvist-synthetic/profileView"
GRAPHQL = f"{VOYAGER}/graphql"

SLUG = "ada-sundqvist-synthetic"
URN = "urn:li:fsd_profile:ACoAAB000SYNTHETIC"
COOKIE = 'li_at=synthetic; JSESSIONID="ajax:1234567890123456789"'


@pytest.fixture
def fetcher(monkeypatch: pytest.MonkeyPatch) -> ProfileFetcher:
    async def no_wait(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(VoyagerClient, "_backoff", staticmethod(no_wait))
    monkeypatch.setattr("app.linkedin.client.OutboundLimiter.acquire", no_wait)

    settings = Settings(_env_file=None)
    client = VoyagerClient(settings, SessionManager(settings, cookie_override=COOKIE))
    return ProfileFetcher(client, QueryIdRegistry(settings))


def collection(*entities: dict) -> dict:
    """A dash collection response in the normalized envelope LinkedIn returns."""
    urns = [e["entityUrn"] for e in entities]
    return {
        "data": {
            "*elements": urns,
            "paging": {"count": 100, "start": 0, "total": len(urns)},
        },
        "included": list(entities),
    }


def position(
    entity_id: str,
    title: str,
    company: str,
    company_urn: str,
    start: dict,
    end: dict | None = None,
) -> dict:
    item = {
        "entityUrn": f"urn:li:fsd_profilePosition:{entity_id}",
        "$type": "com.linkedin.voyager.dash.identity.profile.Position",
        "title": title,
        "companyName": company,
        "companyUrn": company_urn,
        "employmentTypeUrn": "urn:li:fsd_employmentType:12",
        "locationName": "Stockholm, Sweden",
        "dateRange": {"start": start},
    }
    if end:
        item["dateRange"]["end"] = end
    return item


def identity_ok():
    return respx.get(url__startswith=DASH_PROFILES).mock(
        return_value=httpx.Response(200, json=load_fixture("graphql_profile.json"))
    )


def mock_sections(fallback: httpx.Response | None = None, **named):
    """Register the section collections, specific routes before the catch-all.

    respx is first-match-wins, so the order matters. Returns routes by name.
    """
    routes = {}
    for name, payload in named.items():
        routes[name] = respx.get(url__startswith=f"{DASH_SECTION}{name}").mock(
            return_value=httpx.Response(200, json=payload)
        )
    respx.get(url__regex=SECTION_RE).mock(
        return_value=fallback or httpx.Response(200, json=collection())
    )
    # The GraphQL identity fallback seeds discovery from the profile page, and
    # a test must never reach for the real network to find that out.
    respx.get(url__startswith="https://www.linkedin.com/in/").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(400))
    return routes


# --- phase 1: identity ------------------------------------------------------


@respx.mock
async def test_identity_comes_from_dash_without_a_queryid(
    fetcher: ProfileFetcher,
) -> None:
    """The dash profile collection is a plain REST call, so it leads."""
    identity = identity_ok()
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    mock_sections()

    result = await fetcher.fetch(SLUG)

    assert identity.called
    assert result.profile.name.full == "Ada Sundqvist"
    assert result.profile.profile_urn == URN
    # Nothing is pinned, so the GraphQL path is not even attempted.
    assert all("graphql" not in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_a_failure_to_identify_stops_the_whole_fetch(
    fetcher: ProfileFetcher,
) -> None:
    """Every section endpoint is keyed on the URN, so this is terminal."""
    respx.get(url__startswith=DASH_PROFILES).mock(return_value=httpx.Response(500))
    # The GraphQL identity fallback still gets its turn, and fails too.
    mock_sections(fallback=httpx.Response(500))

    with pytest.raises(UpstreamUnavailable) as excinfo:
        await fetcher.fetch(SLUG)

    assert "no profile URN" in str(excinfo.value)


# --- phase 2: sections via dash ---------------------------------------------


@respx.mock
async def test_dash_collections_serve_the_sections(fetcher: ProfileFetcher) -> None:
    identity_ok()
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    mock_sections(
        Positions=collection(
            position(
                    "1", "Staff Engineer", "Kestrel", "urn:li:fsd_company:1",
                    {"month": 3, "year": 2022},
                )
        ),
        Skills=collection(
            {"entityUrn": "urn:li:fsd_profileSkill:1", "name": "Go"},
            {"entityUrn": "urn:li:fsd_profileSkill:2", "name": "PostgreSQL"},
        ),
    )

    result = await fetcher.fetch(SLUG)

    assert SOURCE_DASH_SECTIONS in result.sources
    assert [s.name for s in result.profile.skills] == ["Go", "PostgreSQL"]
    assert result.profile.experience[0].title == "Staff Engineer"
    # Typed dates, not recovered from rendered text.
    assert result.profile.experience[0].start.month == 3
    assert result.profile.experience[0].start.year == 2022
    assert result.profile.experience[0].employment_type == "Full-time"


@respx.mock
async def test_page_size_is_requested_so_long_lists_are_not_truncated(
    fetcher: ProfileFetcher,
) -> None:
    """Without `count`, LinkedIn pages at 20 and a 21-skill profile loses one.

    That is a wrong answer that looks perfectly well-formed, which is exactly
    the kind that needs a test rather than a comment.
    """
    identity_ok()
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    skills = mock_sections(Skills=collection())["Skills"]

    await fetcher.fetch(SLUG)

    assert skills.called
    assert skills.calls[0].request.url.params["count"] == "100"
    assert skills.calls[0].request.url.params["q"] == "viewee"


@respx.mock
async def test_several_roles_at_one_employer_stay_grouped(
    fetcher: ProfileFetcher,
) -> None:
    """A position group carries no roles, so the grouping is rebuilt by join.

    Flattening would lose the fact that a promotion was one continuous tenure.
    """
    identity_ok()
    mock_sections(
        PositionGroups=collection(
                    {
                        "entityUrn": "urn:li:fsd_profilePositionGroup:1",
                        "companyName": "Acumant",
                        "companyUrn": "urn:li:fsd_company:75039165",
                        "dateRange": {"start": {"month": 9, "year": 2024}},
                    }
                ),
        Positions=collection(
                    position(
                        "1", "Associate Software Engineer", "Acumant",
                        "urn:li:fsd_company:75039165",
                        {"month": 4, "year": 2025}, {"month": 7, "year": 2025},
                    ),
                    position(
                        "2", "Technical Trainee", "Acumant",
                        "urn:li:fsd_company:75039165",
                        {"month": 9, "year": 2024}, {"month": 4, "year": 2025},
                    ),
                ),
    )
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))

    result = await fetcher.fetch(SLUG)

    assert len(result.profile.experience) == 1
    stint = result.profile.experience[0]
    assert stint.company == "Acumant"
    assert [role.title for role in stint.sub_positions] == [
        "Associate Software Engineer",
        "Technical Trainee",
    ]
    assert (stint.start.year, stint.start.month) == (2024, 9)


@respx.mock
async def test_a_single_role_is_not_wrapped_in_a_pointless_group(
    fetcher: ProfileFetcher,
) -> None:
    identity_ok()
    mock_sections(
        PositionGroups=collection(
                    {
                        "entityUrn": "urn:li:fsd_profilePositionGroup:1",
                        "companyName": "Kestrel",
                        "companyUrn": "urn:li:fsd_company:1",
                        "dateRange": {"start": {"month": 3, "year": 2022}},
                    }
                ),
        Positions=collection(
                    position(
                        "1", "Staff Engineer", "Kestrel", "urn:li:fsd_company:1",
                        {"month": 3, "year": 2022},
                    )
                ),
    )
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))

    result = await fetcher.fetch(SLUG)

    assert len(result.profile.experience) == 1
    assert result.profile.experience[0].sub_positions == []
    # No end date on a listed position means it is still held.
    assert result.profile.experience[0].is_current is True


# --- gaps and fallbacks -----------------------------------------------------


@respx.mock
async def test_sections_dash_could_not_serve_are_named(
    fetcher: ProfileFetcher,
) -> None:
    """An empty list must never mean "we failed"."""
    identity_ok()
    mock_sections(
        fallback=httpx.Response(500),
        Skills=collection({"entityUrn": "urn:li:fsd_profileSkill:1", "name": "Go"}),
    )
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))

    result = await fetcher.fetch(SLUG)

    assert result.profile.skills  # served
    assert "skills" not in result.sections_unavailable
    for missing in ("education", "certifications", "languages", "patents"):
        assert missing in result.sections_unavailable


@respx.mock
async def test_profileview_backfills_what_dash_could_not_serve(
    fetcher: ProfileFetcher,
) -> None:
    """Per-section merge: the caller gets the union, not the intersection."""
    identity_ok()
    mock_sections(
        fallback=httpx.Response(500),
        Skills=collection({"entityUrn": "urn:li:fsd_profileSkill:1", "name": "Go"}),
    )
    respx.get(PROFILEVIEW).mock(
        return_value=httpx.Response(200, json=load_fixture("profileview_dense.json"))
    )

    result = await fetcher.fetch(SLUG)

    assert result.source_label == "mixed"
    assert SOURCE_DASH_SECTIONS in result.sources
    assert SOURCE_PROFILEVIEW in result.sources
    # Skills came from dash; education exists only in the profileView fixture.
    assert [s.name for s in result.profile.skills] == ["Go"]
    assert result.profile.education[0].school == "KTH Royal Institute of Technology"


@respx.mock
async def test_identity_only_reports_every_section_as_unavailable(
    fetcher: ProfileFetcher,
) -> None:
    """The state the live service was stuck in before the collections existed."""
    identity_ok()
    mock_sections(fallback=httpx.Response(500))
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))

    result = await fetcher.fetch(SLUG)

    assert result.source_label == SOURCE_DASH
    assert result.profile.name.full == "Ada Sundqvist"
    assert len(result.sections_unavailable) == 12


@respx.mock
async def test_a_block_stops_the_chain(fetcher: ProfileFetcher) -> None:
    """Falling through after a 999 would just deepen the block."""
    identity = respx.get(url__startswith=DASH_PROFILES).mock(
        return_value=httpx.Response(999)
    )
    profileview = respx.get(PROFILEVIEW).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(LinkedInBlocked):
        await fetcher.fetch(SLUG)

    assert identity.call_count == 1
    assert profileview.call_count == 0


async def test_a_missing_session_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.linkedin.client.OutboundLimiter.acquire", no_wait)

    settings = Settings(_env_file=None)
    fetcher = ProfileFetcher(
        VoyagerClient(settings, SessionManager(settings)), QueryIdRegistry(settings)
    )

    with pytest.raises(SessionUnavailable) as excinfo:
        await fetcher.fetch(SLUG)

    assert "X-LinkedIn-Cookie" in str(excinfo.value)


# --- the GraphQL card path, now demoted -------------------------------------


@respx.mock
async def test_graphql_cards_are_skipped_when_no_queryid_is_pinned(
    fetcher: ProfileFetcher,
) -> None:
    """linkedin.com no longer calls that query, so it is not worth a request.

    Attempting it unpinned cost twelve requests per profile and learned nothing.
    """
    identity_ok()
    mock_sections(
    )
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    graphql = respx.get(url__startswith=GRAPHQL).mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )

    await fetcher.fetch(SLUG)

    assert graphql.call_count == 0


@respx.mock
async def test_graphql_cards_are_tried_when_a_queryid_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pin is an explicit statement that the operator has a working id."""

    async def no_wait(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(VoyagerClient, "_backoff", staticmethod(no_wait))
    monkeypatch.setattr("app.linkedin.client.OutboundLimiter.acquire", no_wait)

    settings = Settings(
        _env_file=None,
        linkedin_query_ids_json=json.dumps(
            {"voyagerIdentityDashProfileComponents": "b" * 32}
        ),
    )
    fetcher = ProfileFetcher(
        VoyagerClient(settings, SessionManager(settings, cookie_override=COOKIE)),
        QueryIdRegistry(settings),
    )

    identity_ok()
    mock_sections(fallback=httpx.Response(500))
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    respx.get(url__startswith="https://www.linkedin.com/in/").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )
    graphql = respx.get(url__startswith=GRAPHQL).mock(
        return_value=httpx.Response(200, json=load_fixture("graphql_skills_card.json"))
    )

    result = await fetcher.fetch(SLUG)

    assert graphql.call_count > 0
    assert [s.name for s in result.profile.skills][0] == "Distributed Systems"


@respx.mock
async def test_a_section_linkedin_says_is_empty_is_not_reported_as_unavailable(
    fetcher: ProfileFetcher,
) -> None:
    """A 200 with zero elements means the person has none, not that we failed.

    The first version left such a section pending, reporting success as failure.
    """
    identity_ok()
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    mock_sections(
        Skills=collection({"entityUrn": "urn:li:fsd_profileSkill:1", "name": "Go"})
    )

    result = await fetcher.fetch(SLUG)

    # Answered and non-empty.
    assert [s.name for s in result.profile.skills] == ["Go"]
    # Answered and empty: reported as empty, NOT as unavailable.
    assert result.profile.certifications == []
    assert result.profile.languages == []
    assert result.sections_unavailable == []


@respx.mock
async def test_only_a_failure_leaves_a_section_unavailable(
    fetcher: ProfileFetcher,
) -> None:
    """The other side of the same rule: a 400 is still honestly reported."""
    identity_ok()
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    mock_sections(
        fallback=httpx.Response(400),
        Skills=collection({"entityUrn": "urn:li:fsd_profileSkill:1", "name": "Go"}),
    )

    result = await fetcher.fetch(SLUG)

    assert result.profile.skills
    assert "skills" not in result.sections_unavailable
    assert "certifications" in result.sections_unavailable


@respx.mock
async def test_duration_is_computed_from_typed_dates(fetcher: ProfileFetcher) -> None:
    """No rendered "3 yrs 2 mos" string exists on this path, so compute it."""
    identity_ok()
    respx.get(PROFILEVIEW).mock(return_value=httpx.Response(410))
    mock_sections(
        Positions=collection(
            position(
                "1", "Engineer", "Kestrel", "urn:li:fsd_company:1",
                {"month": 1, "year": 2020}, {"month": 12, "year": 2020},
            )
        )
    )

    result = await fetcher.fetch(SLUG)

    # January to December inclusive is twelve months, not eleven.
    assert result.profile.experience[0].duration_months == 12
