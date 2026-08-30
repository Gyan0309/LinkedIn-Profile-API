"""Extract profile sections from Voyager's GraphQL profile-card responses.

The GraphQL path returns *rendered component trees*, not typed entities. A
position is not `{title, companyName, startDate}`; it is an `entityComponent`
carrying the strings the web UI paints -- a title, a subtitle packing several
fields around a middle dot, and a caption holding a human-readable date range.

That is worth being explicit about, because it sets the accuracy ceiling for this
path. Structured dates have to be recovered from "Jan 2020 - Present - 3 yrs",
and a company name has to be separated from an employment type inside one string.
`common.parse_date_range` is deliberately conservative about it.

The legacy REST path in `profileview.py` carries real typed fields and is more
faithful where it is available. The fetch chain prefers whichever it can get, and
`meta.source` tells the caller which one they received.

Everything here is defensive by construction. LinkedIn changes component shapes
without notice, so a shape we do not recognise yields no row rather than a
partial row -- and the section is then reported in `sections_unavailable`
instead of being silently returned as empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.extract.common import (
    clean_text,
    image_from_vector,
    parse_date_range,
    parse_duration_months,
    split_on_dot,
    text_of,
)
from app.schema import (
    Certification,
    Course,
    Education,
    Honor,
    Image,
    Language,
    Organization,
    Patent,
    Position,
    Project,
    Publication,
    Skill,
    Volunteering,
)

MAX_TREE_DEPTH = 30


@dataclass
class EntityRow:
    """One card entry, flattened out of its component tree."""

    title: str | None = None
    subtitle: str | None = None
    caption: str | None = None
    metadata: str | None = None
    description: list[str] = field(default_factory=list)
    url: str | None = None
    urn: str | None = None
    image: Image | None = None
    children: list[EntityRow] = field(default_factory=list)

    @property
    def description_text(self) -> str | None:
        joined = "\n".join(line for line in self.description if line)
        return joined or None

    def is_meaningful(self) -> bool:
        """A row with no title and no subtitle is a layout artefact, not data."""
        return bool(self.title or self.subtitle)


# --- tree walking -----------------------------------------------------------


def rows_from_card(payload: dict[str, Any]) -> list[EntityRow]:
    """Every top-level entity row in a resolved profile-card payload."""
    components = _top_level_entity_components(payload)
    rows = [_row_from_entity(component) for component in components]
    return [row for row in rows if row.is_meaningful()]


def _top_level_entity_components(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find the outermost entityComponents, without descending into their children.

    Stopping at the first level matters: a position's sub-roles are themselves
    entityComponents, and collecting them all flat would turn one job with three
    promotions into four unrelated jobs.
    """
    if depth > MAX_TREE_DEPTH or node is None:
        return []

    if isinstance(node, dict):
        entity = node.get("entityComponent")
        if isinstance(entity, dict):
            return [entity]
        found: list[dict[str, Any]] = []
        for key, value in node.items():
            if key in ("subComponents", "$type", "entityUrn"):
                continue
            found.extend(_top_level_entity_components(value, depth + 1))
        return found

    if isinstance(node, list):
        found = []
        for item in node:
            found.extend(_top_level_entity_components(item, depth + 1))
        return found

    return []


def _row_from_entity(entity: dict[str, Any]) -> EntityRow:
    row = EntityRow(
        title=text_of(entity.get("titleV2")) or text_of(entity.get("title")),
        subtitle=text_of(entity.get("subtitle")),
        caption=text_of(entity.get("caption")),
        metadata=text_of(entity.get("metadata")),
        url=clean_text(entity.get("textActionTarget"))
        or clean_text(entity.get("navigationUrl")),
        urn=clean_text(entity.get("trackingUrn")) or clean_text(entity.get("entityUrn")),
        image=image_from_vector(entity.get("image")),
    )

    sub = entity.get("subComponents")
    if sub is not None:
        _absorb_subcomponents(sub, row, depth=0)

    return row


def _absorb_subcomponents(node: Any, row: EntityRow, *, depth: int) -> None:
    """Fold a subComponents tree into descriptions and child rows."""
    if depth > MAX_TREE_DEPTH or node is None:
        return

    if isinstance(node, list):
        for item in node:
            _absorb_subcomponents(item, row, depth=depth + 1)
        return

    if not isinstance(node, dict):
        return

    # A nested entity is a child row (a sub-role, or a grouped credential).
    nested = node.get("entityComponent")
    if isinstance(nested, dict):
        child = _row_from_entity(nested)
        if child.is_meaningful():
            row.children.append(child)
        return

    # Free text: the description body, or an inline insight line. Insight lines
    # carry the endorsement counts and mutual-connection notes, so they are worth
    # the same as a description -- but only when the component holds text
    # directly. When it wraps further structure, keep descending.
    for key in ("textComponent", "insightComponent"):
        inner = node.get(key)
        if inner is None:
            continue
        body = text_of(inner)
        if body:
            row.description.append(body)
        else:
            _absorb_subcomponents(inner, row, depth=depth + 1)

    if node.get("fixedListComponent") is not None:
        _absorb_subcomponents(node["fixedListComponent"], row, depth=depth + 1)

    for key, value in node.items():
        if key in ("textComponent", "insightComponent", "fixedListComponent",
                   "entityComponent", "$type", "entityUrn"):
            continue
        _absorb_subcomponents(value, row, depth=depth + 1)


# --- section mapping --------------------------------------------------------


def to_experience(rows: list[EntityRow]) -> list[Position]:
    """Map rows to positions, preserving multi-role stints at one employer.

    LinkedIn renders several roles at the same company as one card with the
    company in the title and the roles as children. Flattening that would lose
    the fact that it was one continuous tenure with promotions, so the grouping is
    kept and the parent carries the company while `sub_positions` carry the roles.
    """
    positions: list[Position] = []

    for row in rows:
        child_roles = [child for child in row.children if _looks_like_role(child)]

        if child_roles:
            company = row.title
            company_logo = row.image
            company_url = row.url
            sub_positions = [
                _position_from_row(child, company=company, logo=None, url=company_url)
                for child in child_roles
            ]
            start = sub_positions[-1].start if sub_positions else None
            end = sub_positions[0].end if sub_positions else None
            positions.append(
                Position(
                    title=sub_positions[0].title if sub_positions else None,
                    company=company,
                    company_url=company_url,
                    company_urn=row.urn,
                    company_logo=company_logo,
                    location=row.metadata,
                    start=start,
                    end=end,
                    is_current=any(p.is_current for p in sub_positions),
                    duration_months=parse_duration_months(row.caption),
                    description=row.description_text,
                    sub_positions=sub_positions,
                )
            )
            continue

        positions.append(_position_from_row(row, company=None, logo=row.image, url=row.url))

    return positions


def _looks_like_role(row: EntityRow) -> bool:
    """A child that carries its own date caption is a role, not a description."""
    if not row.title:
        return False
    start, _, is_current = parse_date_range(row.caption)
    return start is not None or is_current


def _position_from_row(
    row: EntityRow, *, company: str | None, logo: Image | None, url: str | None
) -> Position:
    # Subtitle packs company and employment type: "Acme Corp - Full-time".
    parts = split_on_dot(row.subtitle)
    resolved_company = company or (parts[0] if parts else clean_text(row.subtitle))
    employment_type = parts[1] if len(parts) > 1 else None

    start, end, is_current = parse_date_range(row.caption)

    # Metadata packs location and arrangement: "London, UK - Hybrid".
    meta_parts = split_on_dot(row.metadata)
    location = meta_parts[0] if meta_parts else clean_text(row.metadata)

    return Position(
        title=row.title,
        company=resolved_company,
        company_url=url,
        company_urn=row.urn,
        company_logo=logo,
        employment_type=employment_type,
        location=location,
        description=row.description_text,
        start=start,
        end=end,
        is_current=is_current,
        duration_months=parse_duration_months(row.caption),
    )


def to_education(rows: list[EntityRow]) -> list[Education]:
    out: list[Education] = []
    for row in rows:
        # Subtitle is "Bachelor's degree, Computer Science" -- comma separated,
        # unlike the middle-dot elsewhere.
        degree, _, field_of_study = (row.subtitle or "").partition(",")
        start, end, _ = parse_date_range(row.caption)
        out.append(
            Education(
                school=row.title,
                school_url=row.url,
                school_urn=row.urn,
                school_logo=row.image,
                degree=clean_text(degree),
                field_of_study=clean_text(field_of_study),
                description=row.description_text,
                start=start,
                end=end,
            )
        )
    return out


def to_skills(rows: list[EntityRow]) -> list[Skill]:
    out: list[Skill] = []
    for row in rows:
        if not row.title:
            continue
        out.append(Skill(name=row.title, endorsement_count=_endorsements(row)))
    return out


def _endorsements(row: EntityRow) -> int | None:
    """Read an endorsement count out of the insight line under a skill."""
    import re

    for line in [*row.description, row.subtitle or "", row.caption or ""]:
        match = re.search(r"(\d[\d,]*)\s+endorsement", line, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def to_certifications(rows: list[EntityRow]) -> list[Certification]:
    out: list[Certification] = []
    for row in rows:
        issue, expiry = _certification_dates(row.caption)
        credential_id = None
        for line in row.description:
            if "credential id" in line.lower():
                credential_id = line.split(":", 1)[-1].strip() or None
        out.append(
            Certification(
                name=row.title,
                issuer=row.subtitle,
                issuer_logo=row.image,
                issuer_url=row.url,
                issue_date=issue,
                expiry_date=expiry,
                credential_id=credential_id,
                credential_url=row.url,
            )
        )
    return out


def _certification_dates(caption: str | None):
    """Certification captions read "Issued Jan 2021 - Expires Jan 2024"."""
    if not caption:
        return None, None
    lowered = caption.lower()
    if "expires" in lowered or "expired" in lowered:
        start, end, _ = parse_date_range(caption)
        return start, end
    start, _, _ = parse_date_range(caption)
    return start, None


def to_languages(rows: list[EntityRow]) -> list[Language]:
    return [
        Language(name=row.title, proficiency=row.caption or row.subtitle)
        for row in rows
        if row.title
    ]


def to_projects(rows: list[EntityRow]) -> list[Project]:
    out = []
    for row in rows:
        start, end, _ = parse_date_range(row.caption)
        out.append(
            Project(
                name=row.title,
                description=row.description_text,
                url=row.url,
                start=start,
                end=end,
            )
        )
    return out


def to_publications(rows: list[EntityRow]) -> list[Publication]:
    out = []
    for row in rows:
        published, _, _ = parse_date_range(row.caption)
        out.append(
            Publication(
                name=row.title,
                publisher=row.subtitle,
                description=row.description_text,
                url=row.url,
                published_on=published,
            )
        )
    return out


def to_honors(rows: list[EntityRow]) -> list[Honor]:
    out = []
    for row in rows:
        issued, _, _ = parse_date_range(row.caption)
        out.append(
            Honor(
                title=row.title,
                issuer=row.subtitle,
                description=row.description_text,
                issued_on=issued,
            )
        )
    return out


def to_volunteering(rows: list[EntityRow]) -> list[Volunteering]:
    out = []
    for row in rows:
        start, end, _ = parse_date_range(row.caption)
        out.append(
            Volunteering(
                role=row.title,
                organization=row.subtitle,
                cause=row.metadata,
                description=row.description_text,
                start=start,
                end=end,
            )
        )
    return out


def to_courses(rows: list[EntityRow]) -> list[Course]:
    return [Course(name=row.title, number=row.subtitle) for row in rows if row.title]


def to_patents(rows: list[EntityRow]) -> list[Patent]:
    out = []
    for row in rows:
        issued, _, _ = parse_date_range(row.caption)
        out.append(
            Patent(
                title=row.title,
                issuer=row.subtitle,
                description=row.description_text,
                issued_on=issued,
            )
        )
    return out


def to_organizations(rows: list[EntityRow]) -> list[Organization]:
    out = []
    for row in rows:
        start, end, _ = parse_date_range(row.caption)
        out.append(
            Organization(
                name=row.title,
                position=row.subtitle,
                description=row.description_text,
                start=start,
                end=end,
            )
        )
    return out
