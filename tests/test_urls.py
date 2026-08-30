"""URL parsing.

Callers paste whatever their browser gave them, so these cases are the shapes
that actually turn up rather than the one canonical form.
"""

from __future__ import annotations

import pytest

from app.errors import InvalidProfileURL
from app.linkedin.urls import canonical_profile_url, extract_public_identifier


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/in/ada-sundqvist", "ada-sundqvist"),
        ("https://www.linkedin.com/in/ada-sundqvist/", "ada-sundqvist"),
        ("http://www.linkedin.com/in/ada-sundqvist", "ada-sundqvist"),
        ("www.linkedin.com/in/ada-sundqvist", "ada-sundqvist"),
        ("linkedin.com/in/ada-sundqvist", "ada-sundqvist"),
        # Locale subdomains: LinkedIn serves the same profile from every country.
        ("https://se.linkedin.com/in/ada-sundqvist", "ada-sundqvist"),
        ("https://uk.linkedin.com/in/ada-sundqvist/", "ada-sundqvist"),
        # Tracking parameters survive every copy-paste from the app.
        (
            "https://www.linkedin.com/in/ada-sundqvist?originalSubdomain=se&trk=abc",
            "ada-sundqvist",
        ),
        ("https://www.linkedin.com/in/ada-sundqvist/#experience", "ada-sundqvist"),
        # Profile subsections, which the share sheet sometimes hands out.
        (
            "https://www.linkedin.com/in/ada-sundqvist/detail/experience/",
            "ada-sundqvist",
        ),
        (
            "https://www.linkedin.com/in/ada-sundqvist/recent-activity/all/",
            "ada-sundqvist",
        ),
        # Legacy /pub/ form; the trailing groups are the old member-id encoding.
        ("https://www.linkedin.com/pub/ada-sundqvist/1/b2/3c4", "ada-sundqvist"),
        # A bare identifier, which is what our own meta block emits.
        ("ada-sundqvist", "ada-sundqvist"),
    ],
)
def test_accepts_real_world_url_shapes(raw: str, expected: str) -> None:
    assert extract_public_identifier(raw) == expected


def test_percent_encoded_unicode_slug() -> None:
    """LinkedIn allows non-ASCII vanity names and percent-encodes them in URLs."""
    raw = "https://www.linkedin.com/in/andr%C3%A9-m%C3%BCller-1a2b3c"
    assert extract_public_identifier(raw) == "andré-müller-1a2b3c"


def test_unicode_slug_passed_through_undecoded() -> None:
    raw = "https://www.linkedin.com/in/åsa-lindström"
    assert extract_public_identifier(raw) == "åsa-lindström"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("https://www.linkedin.com/company/kestrel-systems", "company page"),
        ("https://www.linkedin.com/school/kth", "school page"),
        ("https://www.linkedin.com/jobs/view/123456", "job listing"),
        ("https://www.linkedin.com/feed/", "the feed"),
        ("https://www.linkedin.com/pulse/some-article", "an article"),
    ],
)
def test_rejects_non_profile_paths_by_name(raw: str, fragment: str) -> None:
    """The error should say what was actually pasted, not just 'invalid'."""
    with pytest.raises(InvalidProfileURL) as excinfo:
        extract_public_identifier(raw)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://example.com/in/ada-sundqvist",
        "https://linkedin.com.evil.test/in/ada-sundqvist",
        "https://www.linkedin.com/",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/unknown-section/ada",
    ],
)
def test_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(InvalidProfileURL):
        extract_public_identifier(raw)


def test_lookalike_host_is_not_linkedin() -> None:
    """`linkedin.com.evil.test` must not be treated as a LinkedIn host."""
    with pytest.raises(InvalidProfileURL) as excinfo:
        extract_public_identifier("https://linkedin.com.evil.test/in/ada")
    assert "Not a linkedin.com URL" in str(excinfo.value)


def test_canonical_url_round_trips() -> None:
    url = canonical_profile_url("ada-sundqvist")
    assert url == "https://www.linkedin.com/in/ada-sundqvist"
    assert extract_public_identifier(url) == "ada-sundqvist"
