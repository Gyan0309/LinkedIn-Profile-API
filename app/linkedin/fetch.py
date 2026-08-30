"""The fetch chain: identity first, then sections, merged per section.

LinkedIn serves the same profile through several generations of endpoint, none
reliably available, so this tries them in order and takes what each gives.

A section no strategy could fetch is named in `sections_unavailable`, so an
empty list always means the person has none, never that we failed to ask.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.errors import (
    LinkedInAPIError,
    LinkedInBlocked,
    ProfileNotFound,
    QueryRejected,
    SessionUnavailable,
    UpstreamUnavailable,
)
from app.extract import components, dash, profileview
from app.extract.common import clean_text, image_from_vector, parse_count, text_of
from app.linkedin import normalize
from app.linkedin.client import VoyagerClient
from app.linkedin.queryids import (
    QUERY_PROFILE_BY_VANITY,
    QUERY_PROFILE_COMPONENTS,
    QueryIdRegistry,
    profile_page_url,
)
from app.logging_config import stage
from app.schema import Connections, Location, Name, Profile

logger = logging.getLogger(__name__)

SOURCE_GRAPHQL = "voyager-graphql"
SOURCE_PROFILEVIEW = "voyager-rest-profileview"
SOURCE_DASH = "voyager-rest-dash"
SOURCE_DASH_SECTIONS = "voyager-dash-collections"

# Makes the identity call resolve the Geo and Industry entities the Profile
# only references by URN. Without it, location and industry come back null.
PROFILE_DECORATION = (
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfile-77"
)

# Our section name -> the sectionType LinkedIn's GraphQL card query expects.
SECTION_TYPES: dict[str, str] = {
    "experience": "experience",
    "education": "education",
    "skills": "skills",
    "certifications": "licenses_and_certifications",
    "languages": "languages",
    "projects": "projects",
    "publications": "publications",
    "honors": "honors",
    "volunteering": "volunteering_experience",
    "courses": "courses",
    "patents": "patents",
    "organizations": "organizations",
}

# Section name -> (component mapper, profileView extractor).
SECTION_MAPPERS = {
    "experience": (components.to_experience, profileview.experience),
    "education": (components.to_education, profileview.education),
    "skills": (components.to_skills, profileview.skills),
    "certifications": (components.to_certifications, profileview.certifications),
    "languages": (components.to_languages, profileview.languages),
    "projects": (components.to_projects, profileview.projects),
    "publications": (components.to_publications, profileview.publications),
    "honors": (components.to_honors, profileview.honors),
    "volunteering": (components.to_volunteering, profileview.volunteering),
    "courses": (components.to_courses, profileview.courses),
    "patents": (components.to_patents, profileview.patents),
    "organizations": (components.to_organizations, profileview.organizations),
}

# Twelve simultaneous requests from one session is a bot signature.
SECTION_CONCURRENCY = 3


@dataclass
class FetchResult:
    profile: Profile
    sources: set[str] = field(default_factory=set)
    sections_unavailable: list[str] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        if not self.sources:
            return "none"
        if len(self.sources) == 1:
            return next(iter(self.sources))
        return "mixed"


class ProfileFetcher:
    """Runs the strategy chain for one profile."""

    def __init__(self, client: VoyagerClient, registry: QueryIdRegistry) -> None:
        self._client = client
        self._registry = registry

    async def fetch(self, public_identifier: str) -> FetchResult:
        """Resolve who the profile is, then fill in what it contains.

        Two phases because sections need the URN identity produces. As one
        chain, a GraphQL failure lost the URN and the dash collections -- which
        need only the URN -- never got a turn.
        """
        result = FetchResult(profile=Profile(public_identifier=public_identifier))
        pending = set(SECTION_MAPPERS)
        chain_started = time.perf_counter()
        stage(logger, "chain", "starting", identifier=public_identifier,
              sections=len(pending))

        # --- Phase 1: who is this? -------------------------------------------
        await self._resolve_identity(public_identifier, result)
        profile_urn = result.profile.profile_urn

        # --- Phase 2: what is on the profile? --------------------------------
        # Dash first: no queryId, no HTML page, typed dates.
        if profile_urn and pending:
            stage(logger, "S1 dash", "section collections", sections=len(pending))
            try:
                await self._fetch_dash_sections(profile_urn, result, pending)
            except (LinkedInBlocked, SessionUnavailable):
                raise
            except LinkedInAPIError as exc:
                stage(logger, "S1 dash", "FAILED, falling through",
                      error=exc.reason, level=logging.WARNING)

        # linkedin.com no longer calls this query, so it runs only when an
        # operator has pinned a working id.
        if profile_urn and pending and self._registry.pinned:
            stage(logger, "S2 graphql", "section cards", missing=len(pending))
            try:
                await self._fetch_sections(profile_urn, result, pending)
            except (LinkedInBlocked, SessionUnavailable):
                raise
            except LinkedInAPIError as exc:
                stage(logger, "S2 graphql", "FAILED, falling through",
                      error=exc.reason, level=logging.WARNING)

        # Retired unevenly. Last because it is least likely to answer.
        if pending:
            stage(logger, "S3 profileview", "legacy REST", missing=len(pending))
            try:
                await self._fetch_profileview(public_identifier, result, pending)
            except (LinkedInBlocked, SessionUnavailable):
                raise
            except LinkedInAPIError as exc:
                stage(logger, "S3 profileview", "FAILED", error=exc.reason,
                      level=logging.WARNING)

        result.sections_unavailable = sorted(pending)
        stage(
            logger,
            "chain",
            "done",
            source=result.source_label,
            served=len(SECTION_MAPPERS) - len(pending),
            unavailable=len(pending),
            ms=int((time.perf_counter() - chain_started) * 1000),
        )
        return result

    # --- Phase 1: identity ---------------------------------------------------

    async def _resolve_identity(
        self, public_identifier: str, result: FetchResult
    ) -> None:
        """Get the top card and the profile URN.

        Every section endpoint is keyed on the URN, so failing here fails
        everything.
        """
        # Dash first: a plain REST call with no queryId behind it.
        try:
            await self._identity_via_dash(public_identifier, result)
            return
        except (LinkedInBlocked, SessionUnavailable, ProfileNotFound):
            raise
        except LinkedInAPIError as exc:
            stage(logger, "identity", "dash lookup failed", error=exc.reason,
                  level=logging.WARNING)

        try:
            await self._identity_via_graphql(public_identifier, result)
        except (LinkedInBlocked, SessionUnavailable, ProfileNotFound):
            raise
        except LinkedInAPIError as exc:
            stage(logger, "identity", "graphql lookup failed", error=exc.reason,
                  level=logging.ERROR)

        if not result.sources:
            raise UpstreamUnavailable(
                f"Could not identify {public_identifier}. Neither the GraphQL "
                "vanity-name lookup nor the dash profile collection answered, so "
                "there is no profile URN to fetch sections with."
            )

    async def _identity_via_graphql(
        self, public_identifier: str, result: FetchResult
    ) -> None:
        # Seeded from this profile's own page so discovery scans the bundles
        # that actually carry the profile queries.
        seed = profile_page_url(public_identifier)
        payload = await self._graphql_with_rediscovery(
            QUERY_PROFILE_BY_VANITY, {"memberIdentity": public_identifier}, seed
        )
        entity = _profile_entity(payload)
        if entity is None:
            raise UpstreamUnavailable(
                "GraphQL returned no profile entity for that identifier."
            )

        _apply_top_card(entity, result.profile, public_identifier)
        result.sources.add(SOURCE_GRAPHQL)
        stage(logger, "identity", "resolved via graphql",
              name=result.profile.name.full or "(none)",
              urn=(result.profile.profile_urn or "(none)")[:44])

    async def _identity_via_dash(
        self, public_identifier: str, result: FetchResult
    ) -> None:
        payload = await self._client.get_voyager(
            "identity/dash/profiles",
            {
                "q": "memberIdentity",
                "memberIdentity": public_identifier,
                # Without a decoration the Profile carries bare URNs -- geoUrn
                # and industryUrn -- and location and industry come back null.
                # This asks LinkedIn to include the Geo and Industry entities
                # alongside, so the URNs resolve to real names.
                "decorationId": PROFILE_DECORATION,
            },
        )
        entity = _profile_entity(payload)
        if entity is None:
            raise UpstreamUnavailable("dash returned no profile entity.")

        _apply_top_card(entity, result.profile, public_identifier)
        result.sources.add(SOURCE_DASH)
        stage(logger, "identity", "resolved via dash",
              name=result.profile.name.full or "(none)",
              urn=(result.profile.profile_urn or "(none)")[:44])

    # --- Phase 2: sections ---------------------------------------------------

    async def _fetch_dash_sections(
        self, profile_urn: str, result: FetchResult, pending: set[str]
    ) -> None:
        """Fetch each missing section from its own dash collection."""
        semaphore = asyncio.Semaphore(SECTION_CONCURRENCY)
        served: list[str] = []

        async def collection(path: str) -> dict[str, Any]:
            payload = await self._client.get_voyager(
                path,
                {
                    "q": "viewee",
                    "profileUrn": profile_urn,
                    # Without this LinkedIn pages at 20 and reports the truth
                    # only in `paging.total`.
                    "count": str(dash.PAGE_SIZE),
                },
            )
            return normalize.resolve(payload) or payload

        async def simple(name: str) -> None:
            async with semaphore:
                try:
                    resolved = await collection(dash.COLLECTIONS[name])
                except (LinkedInBlocked, SessionUnavailable):
                    raise
                except LinkedInAPIError as exc:
                    stage(logger, "  section", name, via="dash",
                          result="unavailable", error=exc.reason)
                    return

                values = dash.EXTRACTORS[name](resolved)
                _record(name, values)

        async def positions() -> None:
            """Experience needs two collections: the groups and the roles."""
            async with semaphore:
                try:
                    groups = await collection(dash.POSITION_GROUPS)
                    roles = await collection(dash.POSITIONS)
                except (LinkedInBlocked, SessionUnavailable):
                    raise
                except LinkedInAPIError as exc:
                    stage(logger, "  section", "experience", via="dash",
                          result="unavailable", error=exc.reason)
                    return

                _record("experience", dash.experience(groups, roles))

        def _record(name: str, values: list[Any]) -> None:
            """A 200 answered the question, including when the answer is none.

            Leaving an empty section pending would report a successful fetch as
            a failure, which is the confusion `sections_unavailable` prevents.
            """
            setattr(result.profile, name, values)
            pending.discard(name)
            served.append(name)
            stage(logger, "  section", name, via="dash",
                  result="ok" if values else "empty", items=len(values))

        tasks = [simple(name) for name in sorted(pending) if name in dash.COLLECTIONS]
        if "experience" in pending:
            tasks.append(positions())

        await asyncio.gather(*tasks)

        if served:
            result.sources.add(SOURCE_DASH_SECTIONS)
            stage(logger, "S2 dash", "served sections", count=len(served),
                  still_missing=len(pending))

    async def _fetch_sections(
        self, profile_urn: str, result: FetchResult, pending: set[str]
    ) -> None:
        semaphore = asyncio.Semaphore(SECTION_CONCURRENCY)
        seed_url = profile_page_url(result.profile.public_identifier)

        async def one(name: str) -> None:
            async with semaphore:
                try:
                    payload = await self._graphql_with_rediscovery(
                        QUERY_PROFILE_COMPONENTS,
                        {
                            "profileUrn": profile_urn,
                            "sectionType": SECTION_TYPES[name],
                            "locale": "en_US",
                        },
                        seed_url,
                    )
                except (LinkedInBlocked, SessionUnavailable):
                    raise
                except LinkedInAPIError as exc:
                    # Left in `pending`, so it is reported as unavailable rather
                    # than returned as an empty list.
                    stage(logger, "  section", name, result="unavailable",
                          error=exc.reason)
                    return

                resolved = normalize.resolve(payload)
                rows = components.rows_from_card(resolved)
                mapper, _ = SECTION_MAPPERS[name]
                values = mapper(rows)
                setattr(result.profile, name, values)
                pending.discard(name)
                stage(logger, "  section", name, result="ok", items=len(values))

        await asyncio.gather(*(one(name) for name in sorted(pending)))

    async def _graphql_with_rediscovery(
        self,
        query_name: str,
        variables: dict[str, str],
        seed_url: str | None = None,
    ) -> dict[str, Any]:
        """Run a GraphQL query, rediscovering the queryId once if rejected."""
        # Captured before the call so a rejection is attributed to the id
        # generation that actually failed.
        generation = self._registry.generation
        stale_id = await self._registry.get(query_name, self._client, seed_url)
        params = {
            "includeWebMetadata": "true",
            "variables": _restli_variables(variables),
            "queryId": stale_id,
        }

        try:
            return await self._client.get_voyager("graphql", params)
        except QueryRejected:
            if self._registry.is_pinned(query_name):
                # Rediscovery would return the same pinned value.
                stage(logger, "queryid", "REJECTED and PINNED - not rediscovering",
                      query=query_name, level=logging.ERROR)
                raise

            rotated = self._registry.invalidate(generation)
            stage(
                logger,
                "queryid",
                "REJECTED - rediscovering" if rotated else "REJECTED - already rotated",
                query=query_name,
                level=logging.WARNING,
            )

            fresh_id = await self._registry.get(query_name, self._client, seed_url)
            if fresh_id == stale_id:
                # Same id, so the query shape is the problem, not the id.
                raise

            params["queryId"] = fresh_id
            return await self._client.get_voyager("graphql", params)

    # --- S2 -----------------------------------------------------------------

    async def _fetch_profileview(
        self, public_identifier: str, result: FetchResult, pending: set[str]
    ) -> None:
        payload = await self._client.get_voyager(
            f"identity/profiles/{public_identifier}/profileView"
        )
        resolved = normalize.resolve(payload) or payload

        filled = False
        for name in sorted(pending):
            _, extractor = SECTION_MAPPERS[name]
            values = extractor(resolved)
            if values:
                setattr(result.profile, name, values)
                pending.discard(name)
                filled = True

        if not result.profile.name.full:
            base = profileview.base_profile(resolved, public_identifier)
            if base.name.full:
                _merge_top_card(result.profile, base)
                filled = True

        # profileView answers all-or-nothing. A response that parsed but yielded
        # nothing at all means the endpoint is retired for this account, not that
        # the member has an empty profile -- so it does not get to claim the source.
        if filled:
            result.sources.add(SOURCE_PROFILEVIEW)
            stage(logger, "S2 profileview", "backfilled sections",
                  still_missing=len(pending))
        else:
            stage(logger, "S2 profileview",
                  "returned 200 but nothing usable - endpoint likely retired")

    # --- S3 -----------------------------------------------------------------

    async def _fetch_dash(self, public_identifier: str, result: FetchResult) -> None:
        """Top card only. Every section stays pending and is reported missing."""
        payload = await self._client.get_voyager(
            "identity/dash/profiles",
            {"q": "memberIdentity", "memberIdentity": public_identifier},
        )
        entity = _profile_entity(payload)
        if entity is None:
            raise UpstreamUnavailable("dash returned no profile entity.")
        _apply_top_card(entity, result.profile, public_identifier)
        result.sources.add(SOURCE_DASH)


# --- payload helpers --------------------------------------------------------


def _restli_variables(variables: dict[str, str]) -> str:
    """Render variables in rest.li tuple syntax: `(key:value,key:value)`."""
    inner = ",".join(f"{key}:{value}" for key, value in variables.items())
    return f"({inner})"


def _profile_entity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Find the Profile entity, matching `$type` since the wrapper key varies."""
    for suffix in ("identity.profile.Profile", ".Profile"):
        candidates = normalize.entities_of_type(payload, suffix)
        for candidate in candidates:
            if candidate.get("firstName") or candidate.get("publicIdentifier"):
                return candidate
        if candidates:
            return candidates[0]

    resolved = normalize.resolve(payload)
    elements = resolved.get("elements")
    if isinstance(elements, list) and elements and isinstance(elements[0], dict):
        return elements[0]
    return None


def _apply_top_card(
    entity: dict[str, Any], profile: Profile, public_identifier: str
) -> None:
    """Fill the top card from a dash Profile entity."""
    first = clean_text(entity.get("firstName"))
    last = clean_text(entity.get("lastName"))
    full = " ".join(part for part in (first, last) if part) or None

    profile.public_identifier = (
        clean_text(entity.get("publicIdentifier")) or public_identifier
    )
    profile.profile_urn = clean_text(entity.get("entityUrn")) or profile.profile_urn
    if full:
        profile.name = Name(first=first, last=last, full=full)
    profile.headline = clean_text(entity.get("headline")) or profile.headline
    profile.about = _about_text(entity) or profile.about
    profile.pronouns = _pronouns(entity) or profile.pronouns
    profile.industry = _industry(entity) or profile.industry

    location = _location(entity)
    if location is not None:
        profile.location = location

    picture = image_from_vector(entity.get("profilePicture"))
    if picture:
        profile.profile_picture = picture
    background = image_from_vector(entity.get("backgroundImage"))
    if background:
        profile.background_image = background

    count, capped = parse_count(
        entity.get("connections") or entity.get("connectionsCount")
    )
    if count is not None:
        profile.connections = Connections(count=count, is_capped=capped)
    followers, _ = parse_count(entity.get("followers") or entity.get("followerCount"))
    if followers is not None:
        profile.followers = followers

    profile.premium = bool(entity.get("premium")) or profile.premium
    profile.influencer = bool(entity.get("influencer")) or profile.influencer
    profile.open_to_work = _frame_is(entity, "OPEN_TO_WORK") or profile.open_to_work
    profile.hiring = _frame_is(entity, "HIRING") or profile.hiring


def _about_text(entity: dict[str, Any]) -> str | None:
    for key in ("summary", "about"):
        value = entity.get(key)
        found = clean_text(value) if isinstance(value, str) else text_of(value)
        if found:
            return found
    return None


def _pronouns(entity: dict[str, Any]) -> str | None:
    for key in ("standardizedPronoun", "customPronoun", "pronoun"):
        found = clean_text(entity.get(key))
        if found:
            return found
    return None


def _industry(entity: dict[str, Any]) -> str | None:
    """Industry name, never the bare URN -- a URN looks like data but says nothing."""
    industry = entity.get("industry")
    if isinstance(industry, dict):
        return clean_text(industry.get("name")) or text_of(industry)
    if isinstance(industry, str) and not industry.startswith("urn:"):
        return clean_text(industry)
    return None


def _location(entity: dict[str, Any]) -> Location | None:
    """Location from the resolved Geo entity.

    `locationName` on the Profile is null in practice; the value lives on the
    Geo entity the decoration pulls in.
    """
    raw = city = country = None

    geo = entity.get("geoLocation")
    if isinstance(geo, dict):
        inner = geo.get("geo") if isinstance(geo.get("geo"), dict) else geo
        raw = clean_text(inner.get("defaultLocalizedName")) or text_of(inner)
        without_country = clean_text(inner.get("defaultLocalizedNameWithoutCountryName"))
        if without_country and without_country != raw:
            # The two differ, so the shorter one is everything but the country
            # and its first component is the city.
            city = without_country.split(",")[0].strip() or None
        elif without_country and without_country == raw:
            # They match, which means LinkedIn had no country to strip: the
            # profile is country-only ("India"), and there is no city.
            country = raw
        resolved_country = inner.get("country")
        if isinstance(resolved_country, dict):
            country = (
                clean_text(resolved_country.get("defaultLocalizedName")) or country
            )

    raw = (
        raw
        or clean_text(entity.get("geoLocationName"))
        or clean_text(entity.get("locationName"))
    )

    # `location.countryCode` is a plain two-letter code and is present even when
    # the Geo entity was not resolved.
    country_code = None
    location = entity.get("location")
    if isinstance(location, dict):
        country_code = clean_text(location.get("countryCode"))

    # The country name is the tail of the localised string when nothing resolved.
    if not country and raw and "," in raw:
        country = raw.rsplit(",", 1)[-1].strip() or None

    if not any((raw, city, country, country_code)):
        return None
    return Location(raw=raw, city=city, country=country, country_code=country_code)


def _frame_is(entity: dict[str, Any], marker: str) -> bool:
    """#OpenToWork and #Hiring only appear as a photo frame type, not a flag."""
    frame = entity.get("profilePictureFrameType") or entity.get(
        "profilePictureFrameTypeUrn"
    )
    return marker in str(frame).upper()


def _merge_top_card(target: Profile, source: Profile) -> None:
    """Fill only the fields the target is still missing."""
    if not target.name.full:
        target.name = source.name
    for attribute in (
        "headline",
        "about",
        "industry",
        "pronouns",
        "profile_picture",
        "background_image",
        "followers",
        "profile_urn",
    ):
        if not getattr(target, attribute, None):
            value = getattr(source, attribute, None)
            if value:
                setattr(target, attribute, value)
    if not target.location.raw:
        target.location = source.location
    if target.connections.count is None:
        target.connections = source.connections
