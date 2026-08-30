"""Extract profile sections from Voyager's dash REST collections.

    GET /voyager/api/identity/dash/profileSkills?q=viewee&profileUrn=<urn>&count=100

All thirteen return 200, need no queryId, and never touch an HTML page -- which
is what drew HTTP 999 on the GraphQL path. Dates arrive as integers, so nothing
has to be parsed out of rendered text.
"""

from __future__ import annotations

from typing import Any

from app.extract.common import (
    clean_text,
    date_from_voyager,
    image_from_vector,
    months_between,
)
from app.schema import (
    Certification,
    Course,
    Education,
    Honor,
    Language,
    Organization,
    Patent,
    Position,
    Project,
    Publication,
    Skill,
    Volunteering,
)

# Required. Without it LinkedIn pages at 20 and reports the truth only in
# `paging.total`, so a 21-skill profile silently returns 20.
PAGE_SIZE = 100

COLLECTIONS: dict[str, str] = {
    "education": "identity/dash/profileEducations",
    "skills": "identity/dash/profileSkills",
    "certifications": "identity/dash/profileCertifications",
    "languages": "identity/dash/profileLanguages",
    "projects": "identity/dash/profileProjects",
    "publications": "identity/dash/profilePublications",
    "honors": "identity/dash/profileHonors",
    "volunteering": "identity/dash/profileVolunteerExperiences",
    "courses": "identity/dash/profileCourses",
    "patents": "identity/dash/profilePatents",
    "organizations": "identity/dash/profileOrganizations",
}

# Groups carry the employer and overall tenure, positions carry the roles.
# Neither nests the other, so grouping is rebuilt by joining on companyUrn.
POSITION_GROUPS = "identity/dash/profilePositionGroups"
POSITIONS = "identity/dash/profilePositions"

# Confirmed against a live profile. The rest are unmapped rather than guessed.
EMPLOYMENT_TYPES = {
    "12": "Full-time",
    "19": "Apprenticeship",
}


def _employment_label(urn: str) -> str | None:
    """A readable type, or None. A bare URN reads as data but says nothing."""
    return EMPLOYMENT_TYPES.get(urn.rsplit(":", 1)[-1])


def elements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The element list from a resolved dash collection response."""
    for key in ("elements", "*elements"):
        found = payload.get(key)
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)]

    data = payload.get("data")
    if isinstance(data, dict):
        return elements(data)
    return []


def _text(item: dict[str, Any], key: str) -> str | None:
    """A field's value, falling back to its `multiLocale` twin.

    Every text field ships twice and the plain one is usually, not always, set.
    """
    direct = clean_text(item.get(key))
    if direct:
        return direct

    localised = item.get("multiLocale" + key[0].upper() + key[1:])
    if isinstance(localised, dict):
        for value in localised.values():
            found = clean_text(value)
            if found:
                return found
    return None


def _range(item: dict[str, Any]) -> tuple[Any, Any]:
    """Start and end. Dash calls this `dateRange`; legacy REST said `timePeriod`."""
    span = item.get("dateRange")
    if not isinstance(span, dict):
        span = item.get("timePeriod")
    if not isinstance(span, dict):
        return None, None
    return date_from_voyager(span.get("start")), date_from_voyager(span.get("end"))


def _employment_type(item: dict[str, Any]) -> str | None:
    urn = clean_text(item.get("employmentTypeUrn"))
    if not urn:
        return _text(item, "employmentType")
    return _employment_label(urn)


def experience(
    groups_payload: dict[str, Any], positions_payload: dict[str, Any]
) -> list[Position]:
    """Positions, with several roles at one employer kept grouped.

    A group holds no roles, so they are joined on companyUrn. Flattening would
    make a promotion read as two unrelated jobs.
    """
    positions = [_position(item) for item in elements(positions_payload)]
    positions = [p for p in positions if p.title or p.company]

    raw_positions = elements(positions_payload)
    by_company: dict[str, list[Position]] = {}
    for raw, built in zip(raw_positions, positions, strict=False):
        key = clean_text(raw.get("companyUrn")) or (built.company or "")
        by_company.setdefault(key, []).append(built)

    groups = elements(groups_payload)
    if not groups:
        return positions

    out: list[Position] = []
    claimed: set[str] = set()

    for group in groups:
        company_urn = clean_text(group.get("companyUrn")) or ""
        roles = by_company.get(company_urn, [])
        start, end = _range(group)

        if not roles:
            continue
        claimed.add(company_urn)

        if len(roles) == 1:
            # The group adds nothing over a lone role.
            out.append(roles[0])
            continue

        out.append(
            Position(
                title=roles[0].title,
                company=_text(group, "companyName") or roles[0].company,
                company_urn=company_urn or None,
                location=roles[0].location,
                start=start or roles[-1].start,
                end=end or roles[0].end,
                is_current=any(role.is_current for role in roles),
                duration_months=months_between(
                    start or roles[-1].start, end or roles[0].end
                ),
                sub_positions=roles,
            )
        )

    # Any position whose employer had no group entry still belongs in the answer.
    for company_urn, roles in by_company.items():
        if company_urn not in claimed:
            out.extend(roles)

    return out


def _position(item: dict[str, Any]) -> Position:
    start, end = _range(item)
    return Position(
        title=_text(item, "title"),
        company=_text(item, "companyName"),
        company_urn=clean_text(item.get("companyUrn")),
        company_logo=image_from_vector(item.get("company")),
        employment_type=_employment_type(item),
        location=_text(item, "locationName") or _text(item, "geoLocationName"),
        description=_text(item, "description"),
        start=start,
        end=end,
        # No end date means the member still holds it.
        is_current=end is None,
        duration_months=months_between(start, end),
    )


def education(payload: dict[str, Any]) -> list[Education]:
    out: list[Education] = []
    for item in elements(payload):
        start, end = _range(item)
        out.append(
            Education(
                school=_text(item, "schoolName"),
                school_urn=clean_text(item.get("schoolUrn")),
                school_logo=image_from_vector(item.get("school")),
                degree=_text(item, "degreeName"),
                field_of_study=_text(item, "fieldOfStudy"),
                grade=_text(item, "grade"),
                activities=_text(item, "activities"),
                description=_text(item, "description"),
                start=start,
                end=end,
            )
        )
    return out


def skills(payload: dict[str, Any]) -> list[Skill]:
    out: list[Skill] = []
    for item in elements(payload):
        name = _text(item, "name")
        if name:
            out.append(Skill(name=name))
    return out


def certifications(payload: dict[str, Any]) -> list[Certification]:
    out: list[Certification] = []
    for item in elements(payload):
        start, end = _range(item)
        company = item.get("company") if isinstance(item.get("company"), dict) else {}
        out.append(
            Certification(
                name=_text(item, "name"),
                issuer=_text(item, "authority") or clean_text(company.get("name")),
                issuer_logo=image_from_vector(company),
                credential_id=_text(item, "licenseNumber"),
                credential_url=clean_text(item.get("url")),
                issue_date=start,
                expiry_date=end,
            )
        )
    return out


def languages(payload: dict[str, Any]) -> list[Language]:
    out: list[Language] = []
    for item in elements(payload):
        name = _text(item, "name")
        if name:
            out.append(Language(name=name, proficiency=clean_text(item.get("proficiency"))))
    return out


def projects(payload: dict[str, Any]) -> list[Project]:
    out: list[Project] = []
    for item in elements(payload):
        start, end = _range(item)
        out.append(
            Project(
                name=_text(item, "title") or _text(item, "name"),
                description=_text(item, "description"),
                url=clean_text(item.get("url")),
                start=start,
                end=end,
            )
        )
    return out


def publications(payload: dict[str, Any]) -> list[Publication]:
    out: list[Publication] = []
    for item in elements(payload):
        out.append(
            Publication(
                name=_text(item, "name"),
                publisher=_text(item, "publisher"),
                description=_text(item, "description"),
                url=clean_text(item.get("url")),
                published_on=date_from_voyager(item.get("publishedOn"))
                or date_from_voyager(item.get("date")),
            )
        )
    return out


def honors(payload: dict[str, Any]) -> list[Honor]:
    out: list[Honor] = []
    for item in elements(payload):
        out.append(
            Honor(
                title=_text(item, "title"),
                issuer=_text(item, "issuer"),
                description=_text(item, "description"),
                issued_on=date_from_voyager(item.get("issuedOn"))
                or date_from_voyager(item.get("issueDate")),
            )
        )
    return out


def volunteering(payload: dict[str, Any]) -> list[Volunteering]:
    out: list[Volunteering] = []
    for item in elements(payload):
        start, end = _range(item)
        out.append(
            Volunteering(
                role=_text(item, "role"),
                organization=_text(item, "companyName"),
                cause=clean_text(item.get("cause")),
                description=_text(item, "description"),
                start=start,
                end=end,
            )
        )
    return out


def courses(payload: dict[str, Any]) -> list[Course]:
    out: list[Course] = []
    for item in elements(payload):
        name = _text(item, "name")
        if name:
            out.append(Course(name=name, number=clean_text(item.get("number"))))
    return out


def patents(payload: dict[str, Any]) -> list[Patent]:
    out: list[Patent] = []
    for item in elements(payload):
        out.append(
            Patent(
                title=_text(item, "title"),
                issuer=_text(item, "issuer"),
                number=clean_text(item.get("number")),
                description=_text(item, "description"),
                issued_on=date_from_voyager(item.get("issuedOn"))
                or date_from_voyager(item.get("issueDate")),
            )
        )
    return out


def organizations(payload: dict[str, Any]) -> list[Organization]:
    out: list[Organization] = []
    for item in elements(payload):
        start, end = _range(item)
        out.append(
            Organization(
                name=_text(item, "name"),
                position=_text(item, "position"),
                description=_text(item, "description"),
                start=start,
                end=end,
            )
        )
    return out


# Everything except experience, which needs two collections joined.
EXTRACTORS = {
    "education": education,
    "skills": skills,
    "certifications": certifications,
    "languages": languages,
    "projects": projects,
    "publications": publications,
    "honors": honors,
    "volunteering": volunteering,
    "courses": courses,
    "patents": patents,
    "organizations": organizations,
}
