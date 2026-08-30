"""The fetch strategy chain.

LinkedIn exposes the same profile through several generations of endpoint, none
of which is reliably available. The GraphQL cards are what linkedin.com itself
calls today. The legacy `profileView` REST endpoint still answers for some
accounts and regions and not others. The dash REST collection answers when the
other two do not.

So rather than picking one and hoping, this fetches through a chain and merges
per section. If GraphQL returns experience and education but drops
certifications, `profileView` backfills only the certifications -- the caller
gets the fullest profile available rather than the intersection of what one
endpoint happened to serve.

What it will not do is quietly paper over a gap. A section that no strategy could
fetch is named in `sections_unavailable`, so an empty `certifications` list always
means the person has none, never that we failed to ask.
"""

from __future__ import annotations

import asyncio
import logging
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
from app.extract import components, profileview
from app.extract.common import clean_text, image_from_vector, parse_count, text_of
from app.linkedin import normalize
from app.linkedin.client import VoyagerClient
from app.linkedin.queryids import (
    QUERY_PROFILE_BY_VANITY,
    QUERY_PROFILE_COMPONENTS,
    QueryIdRegistry,
)
from app.schema import Connections, Location, Name, Profile

logger = logging.getLogger(__name__)

SOURCE_GRAPHQL = "voyager-graphql"
SOURCE_PROFILEVIEW = "voyager-rest-profileview"
SOURCE_DASH = "voyager-rest-dash"

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

# Concurrent section fetches. Kept low deliberately: twelve simultaneous requests
# from one session is a recognisable automation signature, and the outbound
# limiter would serialise them anyway.
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
        result = FetchResult(profile=Profile(public_identifier=public_identifier))
        pending = set(SECTION_MAPPERS)

        # --- S1: GraphQL profile cards --------------------------------------
        try:
            await self._fetch_graphql(public_identifier, result, pending)
        except (LinkedInBlocked, SessionUnavailable):
            # Both are terminal for every strategy, so falling through would only
            # repeat the same failure two more times. A block would additionally
            # deepen itself: each request into a closed door counts against us.
            # And reporting "everything failed" when the real cause is an
            # unconfigured cookie sends the operator hunting the wrong problem.
            raise
        except ProfileNotFound:
            raise
        except LinkedInAPIError as exc:
            logger.warning("graphql strategy failed for %s: %s", public_identifier, exc)

        # --- S2: legacy profileView -----------------------------------------
        if pending or not result.profile.name.full:
            try:
                await self._fetch_profileview(public_identifier, result, pending)
            except (LinkedInBlocked, SessionUnavailable):
                raise
            except ProfileNotFound:
                if not result.sources:
                    raise
            except LinkedInAPIError as exc:
                logger.warning(
                    "profileView strategy failed for %s: %s", public_identifier, exc
                )

        # --- S3: dash REST ---------------------------------------------------
        if not result.sources:
            try:
                await self._fetch_dash(public_identifier, result)
            except (LinkedInBlocked, SessionUnavailable):
                raise
            except LinkedInAPIError as exc:
                logger.warning("dash strategy failed for %s: %s", public_identifier, exc)

        if not result.sources:
            raise UpstreamUnavailable(
                f"Every fetch strategy failed for {public_identifier}. LinkedIn "
                "returned no usable profile data."
            )

        result.sections_unavailable = sorted(pending)
        return result

    # --- S1 -----------------------------------------------------------------

    async def _fetch_graphql(
        self, public_identifier: str, result: FetchResult, pending: set[str]
    ) -> None:
        payload = await self._graphql_with_rediscovery(
            QUERY_PROFILE_BY_VANITY, {"vanityName": public_identifier}
        )
        entity = _profile_entity(payload)
        if entity is None:
            raise UpstreamUnavailable(
                "GraphQL returned no profile entity for that identifier."
            )

        _apply_top_card(entity, result.profile, public_identifier)
        result.sources.add(SOURCE_GRAPHQL)

        profile_urn = result.profile.profile_urn
        if not profile_urn:
            # Without the URN there is nothing to key the section queries on.
            logger.warning(
                "no profile URN resolved for %s; skipping section cards",
                public_identifier,
            )
            return

        await self._fetch_sections(profile_urn, result, pending)

    async def _fetch_sections(
        self, profile_urn: str, result: FetchResult, pending: set[str]
    ) -> None:
        semaphore = asyncio.Semaphore(SECTION_CONCURRENCY)

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
                    )
                except (LinkedInBlocked, SessionUnavailable):
                    raise
                except LinkedInAPIError as exc:
                    # Left in `pending`, so it is reported as unavailable rather
                    # than returned as an empty list.
                    logger.info("section %s unavailable via graphql: %s", name, exc)
                    return

                resolved = normalize.resolve(payload)
                rows = components.rows_from_card(resolved)
                mapper, _ = SECTION_MAPPERS[name]
                setattr(result.profile, name, mapper(rows))
                pending.discard(name)

        await asyncio.gather(*(one(name) for name in sorted(pending)))

    async def _graphql_with_rediscovery(
        self, query_name: str, variables: dict[str, str]
    ) -> dict[str, Any]:
        """Run a GraphQL query, rediscovering the queryId once if it is rejected.

        Bounded to a single retry on purpose. If the freshly discovered id is also
        rejected then the problem is the query shape, not the id, and looping
        would only burn requests against an account that cannot afford them.
        """
        params = {
            "includeWebMetadata": "true",
            "variables": _restli_variables(variables),
            "queryId": await self._registry.get(query_name),
        }
        try:
            return await self._client.get_voyager("graphql", params)
        except QueryRejected:
            logger.warning("queryId for %s rejected; rediscovering once", query_name)
            self._registry.invalidate()
            params["queryId"] = await self._registry.get(query_name)
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

    # --- S3 -----------------------------------------------------------------

    async def _fetch_dash(self, public_identifier: str, result: FetchResult) -> None:
        """Last resort: the dash collection, which returns the top card only.

        Deliberately not treated as a full answer. It gets us a name, headline and
        picture when nothing else responds, and every section stays pending so the
        response says plainly that the detail is missing.
        """
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
    """Find the Profile entity in a payload, wherever LinkedIn put it.

    Matched on the `$type` suffix rather than a fixed key path: the wrapper key
    differs between the vanity-name query, the dash collection and the card
    responses, but the entity type is the same in all three.
    """
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
    industry = entity.get("industry")
    if isinstance(industry, dict):
        return clean_text(industry.get("name")) or text_of(industry)
    return clean_text(industry)


def _location(entity: dict[str, Any]) -> Location | None:
    geo = entity.get("geoLocation")
    raw = None
    country = None
    if isinstance(geo, dict):
        inner = geo.get("geo") if isinstance(geo.get("geo"), dict) else geo
        raw = clean_text(inner.get("defaultLocalizedName")) or text_of(inner)
        country = clean_text(inner.get("country"))
    raw = (
        raw
        or clean_text(entity.get("geoLocationName"))
        or clean_text(entity.get("locationName"))
    )
    if not raw and not country:
        return None
    return Location(raw=raw, country=country)


def _frame_is(entity: dict[str, Any], marker: str) -> bool:
    """LinkedIn signals #OpenToWork and #Hiring through the photo frame type.

    There is no boolean field for either; the badge is a rendering concern to
    LinkedIn, so the frame enum is the only place the fact appears.
    """
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
