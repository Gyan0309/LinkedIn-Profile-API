"""The profile top card: location, industry, flags.

Both came back null until the decoration was added: the Profile entity carries
only `geoUrn` and `industryUrn`, and the names live on separate entities.
"""

from __future__ import annotations

from app.linkedin.fetch import _apply_top_card
from app.schema import Profile


def profile_entity(**overrides) -> dict:
    """A dash Profile with its Geo and Industry already resolved.

    This is what the payload looks like *after* normalize.py inflates the URN
    references, which is the form _apply_top_card actually receives.
    """
    entity = {
        "entityUrn": "urn:li:fsd_profile:ACoAAB000",
        "publicIdentifier": "someone",
        "firstName": "Ada",
        "lastName": "Sundqvist",
        "headline": "Platform Engineer",
        "industryUrn": "urn:li:fsd_industry:96",
        "industry": {"name": "Information Technology & Services"},
        "location": {"countryCode": "IN"},
        "geoLocation": {
            "geo": {
                "entityUrn": "urn:li:fsd_geo:104869687",
                "defaultLocalizedName": "Noida, Uttar Pradesh, India",
                "defaultLocalizedNameWithoutCountryName": "Noida, Uttar Pradesh",
            }
        },
    }
    entity.update(overrides)
    return entity


def build(**overrides) -> Profile:
    profile = Profile(public_identifier="someone")
    _apply_top_card(profile_entity(**overrides), profile, "someone")
    return profile


def test_a_city_level_location_is_split_into_parts() -> None:
    location = build().location

    assert location.raw == "Noida, Uttar Pradesh, India"
    assert location.city == "Noida"
    assert location.country == "India"
    assert location.country_code == "IN"


def test_a_country_only_location_has_no_city() -> None:
    """When the two localised names match, LinkedIn had no country to strip.

    That is a precise signal that the profile is country-only, and inventing a
    city from "India" would be worse than leaving it null.
    """
    location = build(
        geoLocation={
            "geo": {
                "entityUrn": "urn:li:fsd_geo:102713980",
                "defaultLocalizedName": "India",
                "defaultLocalizedNameWithoutCountryName": "India",
            }
        }
    ).location

    assert location.raw == "India"
    assert location.city is None
    assert location.country == "India"
    assert location.country_code == "IN"


def test_industry_resolves_to_a_name() -> None:
    assert build().industry == "Information Technology & Services"


def test_an_unresolved_industry_is_none_not_a_urn() -> None:
    """A bare URN reads as data while telling a consumer nothing."""
    profile = build(industry="urn:li:fsd_industry:96")

    assert profile.industry is None


def test_a_missing_geo_entity_still_yields_the_country_code() -> None:
    """The code sits on the Profile itself and survives an unresolved Geo."""
    location = build(geoLocation={"geoUrn": "urn:li:fsd_geo:104869687"}).location

    assert location is not None
    assert location.country_code == "IN"
    assert location.raw is None


def test_no_location_data_at_all_is_none() -> None:
    profile = Profile(public_identifier="someone")
    entity = profile_entity()
    del entity["geoLocation"]
    del entity["location"]

    _apply_top_card(entity, profile, "someone")

    # An empty Location object rather than invented values.
    assert profile.location.raw is None
    assert profile.location.country_code is None


def test_the_name_and_headline_come_through() -> None:
    profile = build()

    assert profile.name.full == "Ada Sundqvist"
    assert profile.headline == "Platform Engineer"
    assert profile.profile_urn == "urn:li:fsd_profile:ACoAAB000"
