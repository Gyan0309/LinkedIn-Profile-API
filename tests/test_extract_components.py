"""The GraphQL component-tree extractors.

These cover the lossy path: dates recovered from rendered captions, company names
separated from employment types inside one subtitle string, and multi-role
stints that must not be flattened into unrelated jobs.
"""

from __future__ import annotations

from app.extract import components
from app.extract.common import parse_date_range, parse_duration_months, split_on_dot
from app.linkedin import normalize


def rows(fixture, name: str):
    return components.rows_from_card(normalize.resolve(fixture(name)))


# --- caption parsing --------------------------------------------------------


def test_parses_month_year_range() -> None:
    start, end, current = parse_date_range("Jan 2020 - Mar 2022")
    assert (start.year, start.month) == (2020, 1)
    assert (end.year, end.month) == (2022, 3)
    assert current is False


def test_present_marks_current_and_leaves_end_none() -> None:
    start, end, current = parse_date_range("Mar 2023 - Present - 1 yr 6 mos")
    assert (start.year, start.month) == (2023, 3)
    assert end is None
    assert current is True


def test_year_only_range() -> None:
    start, end, _ = parse_date_range("2018 - 2022")
    assert start.year == 2018 and start.month is None
    assert end.year == 2022


def test_duration_digits_are_not_mistaken_for_years() -> None:
    """"3 yrs 2 mos" must not leak into the parsed dates."""
    start, end, _ = parse_date_range("Jan 2020 - Mar 2022 - 2 yrs 3 mos")
    assert start.year == 2020
    assert end.year == 2022


def test_unparseable_caption_returns_none_rather_than_guessing() -> None:
    start, end, current = parse_date_range("Sometime a while back")
    assert start is None and end is None and current is False
    assert parse_date_range(None) == (None, None, False)


def test_duration_extraction() -> None:
    assert parse_duration_months("Aug 2020 - Present - 4 yrs 1 mo") == 49
    assert parse_duration_months("Jan 2021 - Jan 2022 - 1 yr") == 12
    assert parse_duration_months("Jan 2021 - Mar 2021 - 3 mos") == 3
    assert parse_duration_months("no duration here") is None


def test_split_on_middle_dot() -> None:
    assert split_on_dot("Halstead Payments · Full-time") == [
        "Halstead Payments",
        "Full-time",
    ]
    assert split_on_dot(None) == []


# --- row extraction ---------------------------------------------------------


def test_top_level_rows_do_not_include_nested_roles(fixture) -> None:
    """One grouped company plus one simple job is two rows, not four."""
    extracted = rows(fixture, "graphql_experience_card.json")
    assert len(extracted) == 2
    assert extracted[0].title == "Kestrel Systems"
    assert extracted[1].title == "Backend Engineer"


def test_description_is_absorbed_from_text_subcomponents(fixture) -> None:
    extracted = rows(fixture, "graphql_experience_card.json")
    assert extracted[1].description_text == "Ledger reconciliation and settlement."


def test_grouped_company_keeps_its_roles_as_sub_positions(fixture) -> None:
    positions = components.to_experience(rows(fixture, "graphql_experience_card.json"))
    grouped = positions[0]

    assert grouped.company == "Kestrel Systems"
    assert grouped.company_url.endswith("/company/kestrel-systems/")
    assert grouped.company_urn == "urn:li:fsd_company:1000001"
    assert [p.title for p in grouped.sub_positions] == [
        "Staff Platform Engineer",
        "Platform Engineer",
    ]
    # The stint spans the earliest start to the latest end, and is still running.
    assert (grouped.start.year, grouped.start.month) == (2020, 8)
    assert grouped.end is None
    assert grouped.is_current is True
    assert grouped.duration_months == 49


def test_sub_positions_inherit_the_company(fixture) -> None:
    positions = components.to_experience(rows(fixture, "graphql_experience_card.json"))
    for role in positions[0].sub_positions:
        assert role.company == "Kestrel Systems"


def test_simple_position_splits_subtitle(fixture) -> None:
    positions = components.to_experience(rows(fixture, "graphql_experience_card.json"))
    simple = positions[1]

    assert simple.title == "Backend Engineer"
    assert simple.company == "Halstead Payments"
    assert simple.employment_type == "Full-time"
    # Metadata packs location and arrangement; only the location is kept.
    assert simple.location == "Gothenburg, Sweden"
    assert simple.is_current is False
    assert (simple.end.year, simple.end.month) == (2022, 3)


def test_company_logo_rebuilt_from_nested_image_attributes(fixture) -> None:
    positions = components.to_experience(rows(fixture, "graphql_experience_card.json"))
    logo = positions[0].company_logo

    assert [s.width for s in logo.sizes] == [100, 400]
    assert logo.sizes[0].url.startswith("https://media.example-cdn.test/dms/image/kestrel/")


def test_skills_read_endorsements_from_either_insight_or_text(fixture) -> None:
    skills = components.to_skills(rows(fixture, "graphql_skills_card.json"))

    assert [s.name for s in skills] == ["Distributed Systems", "Go", "PostgreSQL"]
    assert skills[0].endorsement_count == 31
    # Thousands separators must survive.
    assert skills[1].endorsement_count == 1204
    # No insight line is not zero endorsements; it is unknown.
    assert skills[2].endorsement_count is None


def test_empty_card_yields_no_rows() -> None:
    assert components.rows_from_card({}) == []
    assert components.rows_from_card({"elements": []}) == []


def test_layout_artefacts_without_title_or_subtitle_are_dropped() -> None:
    payload = {
        "elements": [
            {"components": {"entityComponent": {"caption": {"text": "Jan 2020"}}}},
            {"components": {"entityComponent": {"titleV2": {"text": {"text": "Real"}}}}},
        ]
    }
    extracted = components.rows_from_card(payload)
    assert [row.title for row in extracted] == ["Real"]


def test_education_splits_degree_from_field_on_comma() -> None:
    payload = {
        "elements": [
            {
                "components": {
                    "entityComponent": {
                        "titleV2": {"text": {"text": "KTH"}},
                        "subtitle": {"text": "MSc, Computer Science"},
                        "caption": {"text": "2014 - 2019"},
                    }
                }
            }
        ]
    }
    entries = components.to_education(components.rows_from_card(payload))
    assert entries[0].school == "KTH"
    assert entries[0].degree == "MSc"
    assert entries[0].field_of_study == "Computer Science"
    assert entries[0].start.year == 2014


def test_certification_expiry_only_parsed_when_caption_says_so() -> None:
    with_expiry = {
        "elements": [
            {
                "components": {
                    "entityComponent": {
                        "titleV2": {"text": {"text": "CKA"}},
                        "subtitle": {"text": "The Linux Foundation"},
                        "caption": {"text": "Issued May 2021 - Expires May 2024"},
                    }
                }
            }
        ]
    }
    certs = components.to_certifications(components.rows_from_card(with_expiry))
    assert certs[0].issue_date.year == 2021
    assert certs[0].expiry_date.year == 2024

    no_expiry = {
        "elements": [
            {
                "components": {
                    "entityComponent": {
                        "titleV2": {"text": {"text": "CKA"}},
                        "caption": {"text": "Issued May 2021"},
                    }
                }
            }
        ]
    }
    certs = components.to_certifications(components.rows_from_card(no_expiry))
    assert certs[0].issue_date.year == 2021
    assert certs[0].expiry_date is None
