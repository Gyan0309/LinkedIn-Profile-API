"""The public response contract, designed rather than mirrored from Voyager.

`meta.sections_unavailable` is the load-bearing part: an empty list always means
the person has none, never that the fetch failed.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DatePart(BaseModel):
    """A LinkedIn date. Day is almost never present; month often is not either."""

    year: int | None = None
    month: int | None = None
    day: int | None = None

    def is_empty(self) -> bool:
        return self.year is None and self.month is None and self.day is None


class ImageSize(BaseModel):
    width: int | None = None
    height: int | None = None
    url: str


class Image(BaseModel):
    """All sizes LinkedIn offers, rather than one we picked for the caller."""

    sizes: list[ImageSize] = Field(default_factory=list)

    @property
    def largest(self) -> str | None:
        if not self.sizes:
            return None
        return max(self.sizes, key=lambda s: s.width or 0).url


class Location(BaseModel):
    raw: str | None = None
    city: str | None = None
    country: str | None = None
    country_code: str | None = None


class Name(BaseModel):
    first: str | None = None
    last: str | None = None
    full: str | None = None


class Connections(BaseModel):
    count: int | None = None
    # LinkedIn caps the displayed count at 500+; the exact number is not exposed.
    is_capped: bool = False


class Position(BaseModel):
    title: str | None = None
    company: str | None = None
    company_url: str | None = None
    company_urn: str | None = None
    company_logo: Image | None = None
    employment_type: str | None = None
    location: str | None = None
    description: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None
    is_current: bool = False
    duration_months: int | None = None
    # A single stint at one employer with several role changes. LinkedIn groups
    # these under one company card and flattening them would lose the promotion.
    sub_positions: list[Position] = Field(default_factory=list)


class Education(BaseModel):
    school: str | None = None
    school_url: str | None = None
    school_urn: str | None = None
    school_logo: Image | None = None
    degree: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    activities: str | None = None
    description: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None


class Skill(BaseModel):
    name: str
    endorsement_count: int | None = None


class Certification(BaseModel):
    name: str | None = None
    issuer: str | None = None
    issuer_url: str | None = None
    issuer_logo: Image | None = None
    issue_date: DatePart | None = None
    expiry_date: DatePart | None = None
    credential_id: str | None = None
    credential_url: str | None = None


class Language(BaseModel):
    name: str
    proficiency: str | None = None


class Project(BaseModel):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None


class Publication(BaseModel):
    name: str | None = None
    publisher: str | None = None
    description: str | None = None
    url: str | None = None
    published_on: DatePart | None = None


class Honor(BaseModel):
    title: str | None = None
    issuer: str | None = None
    description: str | None = None
    issued_on: DatePart | None = None


class Volunteering(BaseModel):
    role: str | None = None
    organization: str | None = None
    cause: str | None = None
    description: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None


class Course(BaseModel):
    name: str | None = None
    number: str | None = None


class Patent(BaseModel):
    title: str | None = None
    issuer: str | None = None
    number: str | None = None
    description: str | None = None
    issued_on: DatePart | None = None


class Organization(BaseModel):
    name: str | None = None
    position: str | None = None
    description: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None


class Profile(BaseModel):
    public_identifier: str
    profile_urn: str | None = None
    name: Name = Field(default_factory=Name)
    headline: str | None = None
    about: str | None = None
    pronouns: str | None = None
    location: Location = Field(default_factory=Location)
    industry: str | None = None

    profile_picture: Image | None = None
    background_image: Image | None = None

    connections: Connections = Field(default_factory=Connections)
    followers: int | None = None

    open_to_work: bool = False
    hiring: bool = False
    premium: bool = False
    influencer: bool = False

    experience: list[Position] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    honors: list[Honor] = Field(default_factory=list)
    volunteering: list[Volunteering] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)
    patents: list[Patent] = Field(default_factory=list)
    organizations: list[Organization] = Field(default_factory=list)


class ResponseMeta(BaseModel):
    profile_url: str
    public_identifier: str
    profile_urn: str | None = None
    fetched_at: datetime
    duration_ms: int
    cache: str = Field(description="hit or miss")
    source: str = Field(
        description=(
            "Which fetch strategy served this response: voyager-graphql, "
            "voyager-rest-profileview, voyager-rest-dash, or mixed"
        )
    )
    sections_unavailable: list[str] = Field(
        default_factory=list,
        description=(
            "Sections that could not be fetched. Distinct from a section that was "
            "fetched successfully and is empty, which returns []."
        ),
    )


class ProfileResponse(BaseModel):
    meta: ResponseMeta
    profile: Profile


class ErrorResponse(BaseModel):
    error: str = Field(description="Stable machine-readable reason code")
    message: str = Field(description="Human-readable explanation")
    retry_after_seconds: int | None = None
