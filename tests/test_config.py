"""Settings loading from a real .env file.

The rest of the suite passes `_env_file=None`, which skips the dotenv source --
a blind spot that once hid a startup failure every other test passed through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

RETIRED = (
    "LINKEDIN_COOKIE",
    "LINKEDIN_LI_AT",
    "LINKEDIN_EMAIL",
    "LINKEDIN_PASSWORD",
    "API_KEYS",
    "DEMO_PROFILES",
)


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_numbers_load_from_a_dotenv_file(tmp_path: Path) -> None:
    env = write_env(
        tmp_path,
        "OUTBOUND_MAX_PER_MINUTE=7\nRATE_LIMIT_PER_HOUR=11\nLOG_LEVEL=WARNING\n",
    )

    settings = Settings(_env_file=env)

    assert settings.outbound_max_per_minute == 7
    assert settings.rate_limit_per_hour == 11
    assert settings.log_level == "WARNING"


def test_a_comment_only_env_file_yields_defaults(tmp_path: Path) -> None:
    settings = Settings(_env_file=write_env(tmp_path, "# nothing set here\n\n"))

    assert settings.outbound_max_per_minute == 30
    assert settings.rate_limit_per_hour == 60
    assert settings.log_colour == "auto"


def test_retired_variables_are_ignored_not_fatal(tmp_path: Path) -> None:
    """A developer's stale .env must not crash the app, or resurrect a setting.

    `extra="ignore"` handles the first. The second matters more: nothing should
    be able to reintroduce a server-side credential by leaving a line behind.
    """
    body = "".join(f"{name}=leftover-value\n" for name in RETIRED)
    settings = Settings(_env_file=write_env(tmp_path, body))

    for name in RETIRED:
        assert not hasattr(settings, name.lower())


def test_settings_expose_no_credential_fields() -> None:
    """The service stores nothing. This asserts that structurally."""
    fields = set(Settings.model_fields)

    for forbidden in ("linkedin_cookie", "api_keys", "linkedin_password"):
        assert forbidden not in fields


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"voyagerX": "abc123"}', {"voyagerX": "abc123"}),
        ("", {}),
        ("   ", {}),
        ("not json at all", {}),
        ('["a", "list"]', {}),
    ],
)
def test_pinned_query_ids_never_raises_on_bad_input(raw: str, expected: dict) -> None:
    """A malformed pin falls back to discovery rather than crashing startup."""
    settings = Settings(_env_file=None, linkedin_query_ids_json=raw)
    assert settings.pinned_query_ids == expected
