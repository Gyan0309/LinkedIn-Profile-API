"""The legacy `profileView` extractors.

This path carries typed fields, so the assertions here are about faithfulness --
a date that arrives as `{"month": 3, "year": 2022}` must come out as month 3,
year 2022, not as a string that happens to render the same.
"""

from __future__ import annotations

from app.extract import profileview
from app.linkedin import normalize


def resolved(fixture, name: str) -> dict:
    payload = fixture(name)
    return normalize.resolve(payload) or payload


def test_base_profile_fields(fixture) -> None:
    profile = profileview.base_profile(
        resolved(fixture, "profileview_dense.json"), "ada-sundqvist-synthetic"
    )

    assert profile.name.first == "Ada"
    assert profile.name.last == "Sundqvist"
    assert profile.name.full == "Ada Sundqvist"
    assert profile.headline == "Platform Engineer at Kestrel Systems"
    assert profile.about.startswith("Distributed systems")
    assert profile.industry == "Software Development"
    assert profile.public_identifier == "ada-sundqvist-synthetic"


def test_base_profile_location_prefers_geo_name(fixture) -> None:
    profile = profileview.base_profile(
        resolved(fixture, "profileview_dense.json"), "ada-sundqvist-synthetic"
    )

    assert profile.location.raw == "Stockholm, Stockholm County, Sweden"
    assert profile.location.country == "Sweden"
    assert profile.location.country_code == "se"


def test_images_rebuild_every_offered_size(fixture) -> None:
    """rootUrl + artifact segment, all sizes, smallest first."""
    profile = profileview.base_profile(
        resolved(fixture, "profileview_dense.json"), "ada-sundqvist-synthetic"
    )

    sizes = profile.profile_picture.sizes
    assert [s.width for s in sizes] == [100, 400, 800]
    assert sizes[0].url == (
        "https://media.example-cdn.test/dms/image/synthetic/"
        "profile_100_100/0/aaa?e=1&v=beta"
    )
    assert profile.profile_picture.largest.endswith("profile_800_800/0/aaa?e=1&v=beta")
    assert profile.background_image.sizes[0].width == 1400


def test_connection_count_capped_flag(fixture) -> None:
    profile = profileview.base_profile(
        resolved(fixture, "profileview_dense.json"), "ada-sundqvist-synthetic"
    )
    assert profile.connections.count == 842
    # LinkedIn stops distinguishing above 500, so anything at or over it is capped.
    assert profile.connections.is_capped is True
    assert profile.followers == 1503


def test_experience_dates_are_structured_not_parsed(fixture) -> None:
    positions = profileview.experience(resolved(fixture, "profileview_dense.json"))

    assert len(positions) == 2
    current, previous = positions

    assert current.title == "Staff Platform Engineer"
    assert current.company == "Kestrel Systems"
    assert current.start.month == 3
    assert current.start.year == 2022
    assert current.end is None
    # No end date on a listed position means it is still held.
    assert current.is_current is True

    assert previous.company == "Halstead Payments"
    assert previous.end.month == 2
    assert previous.end.year == 2022
    assert previous.is_current is False


def test_experience_company_logo_resolves(fixture) -> None:
    positions = profileview.experience(resolved(fixture, "profileview_dense.json"))
    assert positions[0].company_logo.sizes[0].width == 200
    # The second position has no company block at all; that is not an error.
    assert positions[1].company_logo is None


def test_education(fixture) -> None:
    entries = profileview.education(resolved(fixture, "profileview_dense.json"))

    assert len(entries) == 1
    entry = entries[0]
    assert entry.school == "KTH Royal Institute of Technology"
    assert entry.degree == "MSc"
    assert entry.field_of_study == "Computer Science"
    assert entry.grade == "4.6"
    assert entry.activities == "Distributed Systems Reading Group"
    assert entry.start.year == 2014
    assert entry.end.year == 2019


def test_skills_and_languages(fixture) -> None:
    data = resolved(fixture, "profileview_dense.json")

    assert [s.name for s in profileview.skills(data)] == [
        "Distributed Systems",
        "Go",
        "PostgreSQL",
    ]
    languages = profileview.languages(data)
    assert languages[0].name == "Swedish"
    assert languages[0].proficiency == "NATIVE_OR_BILINGUAL"


def test_certifications(fixture) -> None:
    certs = profileview.certifications(resolved(fixture, "profileview_dense.json"))

    assert len(certs) == 1
    cert = certs[0]
    assert cert.name == "Certified Kubernetes Administrator"
    assert cert.issuer == "The Linux Foundation"
    assert cert.credential_id == "CKA-SYNTHETIC-0001"
    assert cert.issue_date.year == 2021
    assert cert.expiry_date.year == 2024


def test_remaining_sections(fixture) -> None:
    data = resolved(fixture, "profileview_dense.json")

    assert profileview.projects(data)[0].name == "wal-replay"
    assert profileview.publications(data)[0].publisher == "SIGOPS Workshop"
    assert profileview.honors(data)[0].title == "Engineering Excellence Award"
    assert profileview.volunteering(data)[0].organization == "Kodcentrum"
    assert profileview.courses(data)[0].number == "ID2221"
    assert profileview.patents(data)[0].number == "SE-SYNTHETIC-1234"
    assert profileview.organizations(data)[0].position == "Organiser"


def test_sparse_profile_yields_empty_sections_not_errors(fixture) -> None:
    """A real profile with nothing filled in must parse cleanly."""
    data = resolved(fixture, "profileview_sparse.json")

    profile = profileview.base_profile(data, "nils-berg-sparse")
    assert profile.name.full == "Nils Berg"
    assert profile.about is None
    assert profile.profile_picture is None

    assert profileview.experience(data) == []
    assert profileview.education(data) == []
    assert profileview.skills(data) == []


def test_absent_view_returns_empty_list(fixture) -> None:
    """A view key that is not in the payload at all is not an error."""
    data = resolved(fixture, "profileview_sparse.json")
    assert profileview.patents(data) == []
    assert profileview.section(data, "nonexistentView") == []


def test_extractors_tolerate_garbage() -> None:
    """Malformed input yields nothing rather than raising."""
    for payload in ({}, {"data": None}, {"data": {"positionView": "nonsense"}}):
        assert profileview.experience(payload) == []
        assert profileview.skills(payload) == []
