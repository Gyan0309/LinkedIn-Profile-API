"""Extract profile sections from the legacy `profileView` REST response.

Where the GraphQL cards carry rendered strings, this endpoint carries typed
fields: `timePeriod.startDate.month` is an integer, `companyName` is a company
name and nothing else. Nothing has to be parsed back out of display text, so
where this endpoint is available the data is strictly more faithful.

It is not always available. LinkedIn has been retiring it unevenly -- live for
some accounts and regions, gone for others -- which is exactly why the fetch
chain treats it as one strategy among several rather than the answer.
"""

from __future__ import annotations

from typing import Any

from app.extract.common import (
    clean_text,
    date_from_voyager,
    image_from_vector,
    parse_count,
)
from app.schema import (
    Certification,
    Connections,
    Course,
    Education,
    Honor,
    Language,
    Location,
    Name,
    Organization,
    Patent,
    Position,
    Profile,
    Project,
    Publication,
    Skill,
    Volunteering,
)


def section(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Elements of one `*View` block, tolerating either nesting the API returns.

    With the normalized accept header the views hang off `data`; without it they
    sit at the root. Both shapes turn up depending on the endpoint variant, so
    both are checked rather than assuming one.
    """
    for container in (payload.get("data"), payload):
        if not isinstance(container, dict):
            continue
        view = container.get(key)
        if isinstance(view, dict):
            elements = view.get("elements")
            if isinstance(elements, list):
                return [item for item in elements if isinstance(item, dict)]
        if isinstance(view, list):
            return [item for item in view if isinstance(item, dict)]
    return []


def _country_code(raw: dict[str, Any]) -> str | None:
    """The ISO country code, buried two levels down and often absent entirely."""
    location = raw.get("location")
    if not isinstance(location, dict):
        return None
    basic = location.get("basicLocation")
    if not isinstance(basic, dict):
        return None
    return clean_text(basic.get("countryCode"))


def base_profile(payload: dict[str, Any], public_identifier: str) -> Profile:
    """The top card: name, headline, about, location, images, counts."""
    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw = root.get("profile") if isinstance(root.get("profile"), dict) else root
    if not isinstance(raw, dict):
        raw = {}

    mini = raw.get("miniProfile") if isinstance(raw.get("miniProfile"), dict) else {}

    first = clean_text(raw.get("firstName")) or clean_text(mini.get("firstName"))
    last = clean_text(raw.get("lastName")) or clean_text(mini.get("lastName"))
    full = " ".join(part for part in (first, last) if part) or None

    connections_count, capped = parse_count(root.get("connectionsCount"))

    return Profile(
        public_identifier=clean_text(mini.get("publicIdentifier")) or public_identifier,
        profile_urn=clean_text(raw.get("entityUrn")) or clean_text(mini.get("entityUrn")),
        name=Name(first=first, last=last, full=full),
        headline=clean_text(raw.get("headline")) or clean_text(mini.get("occupation")),
        about=clean_text(raw.get("summary")),
        location=Location(
            raw=clean_text(raw.get("geoLocationName"))
            or clean_text(raw.get("locationName")),
            country=clean_text(raw.get("geoCountryName")),
            country_code=_country_code(raw),
        ),
        industry=clean_text(raw.get("industryName")),
        profile_picture=image_from_vector(mini.get("picture") or raw.get("picture")),
        background_image=image_from_vector(
            mini.get("backgroundImage") or raw.get("backgroundPicture")
        ),
        connections=Connections(count=connections_count, is_capped=capped),
        followers=parse_count(root.get("followersCount"))[0],
        influencer=bool(mini.get("influencer") or raw.get("influencer")),
        premium=bool(mini.get("premium") or raw.get("premium")),
    )


def experience(payload: dict[str, Any]) -> list[Position]:
    out: list[Position] = []
    for item in section(payload, "positionView"):
        period = item.get("timePeriod") if isinstance(item.get("timePeriod"), dict) else {}
        end = date_from_voyager(period.get("endDate"))
        company = item.get("company") if isinstance(item.get("company"), dict) else {}
        out.append(
            Position(
                title=clean_text(item.get("title")),
                company=clean_text(item.get("companyName")),
                company_urn=clean_text(item.get("companyUrn")),
                company_logo=image_from_vector(company.get("miniCompany") or company),
                employment_type=clean_text(item.get("employmentStatus"))
                or clean_text(company.get("employeeCountRange")),
                location=clean_text(item.get("locationName")),
                description=clean_text(item.get("description")),
                start=date_from_voyager(period.get("startDate")),
                end=end,
                # No end date on a listed position means it is still held.
                is_current=end is None,
            )
        )
    return out


def education(payload: dict[str, Any]) -> list[Education]:
    out: list[Education] = []
    for item in section(payload, "educationView"):
        period = item.get("timePeriod") if isinstance(item.get("timePeriod"), dict) else {}
        school = item.get("school") if isinstance(item.get("school"), dict) else {}
        out.append(
            Education(
                school=clean_text(item.get("schoolName")),
                school_urn=clean_text(item.get("schoolUrn")),
                school_logo=image_from_vector(school),
                degree=clean_text(item.get("degreeName")),
                field_of_study=clean_text(item.get("fieldOfStudy")),
                grade=clean_text(item.get("grade")),
                activities=clean_text(item.get("activities")),
                description=clean_text(item.get("description")),
                start=date_from_voyager(period.get("startDate")),
                end=date_from_voyager(period.get("endDate")),
            )
        )
    return out


def skills(payload: dict[str, Any]) -> list[Skill]:
    out: list[Skill] = []
    for item in section(payload, "skillView"):
        name = clean_text(item.get("name"))
        if name:
            out.append(Skill(name=name))
    return out


def certifications(payload: dict[str, Any]) -> list[Certification]:
    out: list[Certification] = []
    for item in section(payload, "certificationView"):
        period = item.get("timePeriod") if isinstance(item.get("timePeriod"), dict) else {}
        company = item.get("company") if isinstance(item.get("company"), dict) else {}
        out.append(
            Certification(
                name=clean_text(item.get("name")),
                issuer=clean_text(item.get("authority")),
                issuer_logo=image_from_vector(company),
                credential_id=clean_text(item.get("licenseNumber")),
                credential_url=clean_text(item.get("url")),
                issue_date=date_from_voyager(period.get("startDate")),
                expiry_date=date_from_voyager(period.get("endDate")),
            )
        )
    return out


def languages(payload: dict[str, Any]) -> list[Language]:
    out: list[Language] = []
    for item in section(payload, "languageView"):
        name = clean_text(item.get("name"))
        if name:
            out.append(Language(name=name, proficiency=clean_text(item.get("proficiency"))))
    return out


def projects(payload: dict[str, Any]) -> list[Project]:
    out: list[Project] = []
    for item in section(payload, "projectView"):
        period = item.get("timePeriod") if isinstance(item.get("timePeriod"), dict) else {}
        out.append(
            Project(
                name=clean_text(item.get("title")),
                description=clean_text(item.get("description")),
                url=clean_text(item.get("url")),
                start=date_from_voyager(period.get("startDate")),
                end=date_from_voyager(period.get("endDate")),
            )
        )
    return out


def publications(payload: dict[str, Any]) -> list[Publication]:
    out: list[Publication] = []
    for item in section(payload, "publicationView"):
        out.append(
            Publication(
                name=clean_text(item.get("name")),
                publisher=clean_text(item.get("publisher")),
                description=clean_text(item.get("description")),
                url=clean_text(item.get("url")),
                published_on=date_from_voyager(item.get("date")),
            )
        )
    return out


def honors(payload: dict[str, Any]) -> list[Honor]:
    out: list[Honor] = []
    for item in section(payload, "honorView"):
        out.append(
            Honor(
                title=clean_text(item.get("title")),
                issuer=clean_text(item.get("issuer")),
                description=clean_text(item.get("description")),
                issued_on=date_from_voyager(item.get("issueDate")),
            )
        )
    return out


def volunteering(payload: dict[str, Any]) -> list[Volunteering]:
    out: list[Volunteering] = []
    for item in section(payload, "volunteerExperienceView"):
        period = item.get("timePeriod") if isinstance(item.get("timePeriod"), dict) else {}
        out.append(
            Volunteering(
                role=clean_text(item.get("role")),
                organization=clean_text(item.get("companyName")),
                cause=clean_text(item.get("cause")),
                description=clean_text(item.get("description")),
                start=date_from_voyager(period.get("startDate")),
                end=date_from_voyager(period.get("endDate")),
            )
        )
    return out


def courses(payload: dict[str, Any]) -> list[Course]:
    out: list[Course] = []
    for item in section(payload, "courseView"):
        name = clean_text(item.get("name"))
        if name:
            out.append(Course(name=name, number=clean_text(item.get("number"))))
    return out


def patents(payload: dict[str, Any]) -> list[Patent]:
    out: list[Patent] = []
    for item in section(payload, "patentView"):
        out.append(
            Patent(
                title=clean_text(item.get("title")),
                issuer=clean_text(item.get("issuer")),
                number=clean_text(item.get("number")),
                description=clean_text(item.get("description")),
                issued_on=date_from_voyager(item.get("issueDate")),
            )
        )
    return out


def organizations(payload: dict[str, Any]) -> list[Organization]:
    out: list[Organization] = []
    for item in section(payload, "organizationView"):
        period = item.get("timePeriod") if isinstance(item.get("timePeriod"), dict) else {}
        out.append(
            Organization(
                name=clean_text(item.get("name")),
                position=clean_text(item.get("position")),
                description=clean_text(item.get("description")),
                start=date_from_voyager(period.get("startDate")),
                end=date_from_voyager(period.get("endDate")),
            )
        )
    return out
