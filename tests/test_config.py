"""Settings loading, including from a real .env file.

The rest of the suite constructs Settings directly with `_env_file=None`, which
skips the dotenv source entirely. That blind spot hid a real failure: a
comma-separated list in a .env file raised SettingsError before any validator
ran, so the app could pass every test and still refuse to start in production.
These tests exercise the dotenv path specifically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_csv_lists_load_from_a_dotenv_file(tmp_path: Path) -> None:
    env = write_env(
        tmp_path,
        "API_KEYS=key-one,key-two\nDEMO_PROFILES=alice,bob,carol\n",
    )

    settings = Settings(_env_file=env)

    assert settings.api_keys == ["key-one", "key-two"]
    assert settings.demo_profiles == ["alice", "bob", "carol"]


def test_single_value_list_needs_no_comma(tmp_path: Path) -> None:
    env = write_env(tmp_path, "API_KEYS=only-one-key\n")
    assert Settings(_env_file=env).api_keys == ["only-one-key"]


def test_empty_list_values_are_empty_not_a_blank_entry(tmp_path: Path) -> None:
    env = write_env(tmp_path, "API_KEYS=\n")
    assert Settings(_env_file=env).api_keys == []


def test_whitespace_around_entries_is_stripped(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEMO_PROFILES= alice , bob ,, carol \n")
    assert Settings(_env_file=env).demo_profiles == ["alice", "bob", "carol"]


def test_credentials_load_from_dotenv(tmp_path: Path) -> None:
    env = write_env(
        tmp_path,
        "LINKEDIN_LI_AT=cookie-value\nOUTBOUND_MAX_PER_MINUTE=7\n",
    )

    settings = Settings(_env_file=env)

    assert settings.has_cookie_session is True
    assert settings.has_login_credentials is False
    assert settings.outbound_max_per_minute == 7


def test_login_path_needs_both_email_and_password(tmp_path: Path) -> None:
    """A half-filled credential pair must not look like a usable session."""
    env = write_env(tmp_path, "LINKEDIN_PASSWORD=only-a-password\n")
    settings = Settings(_env_file=env)

    assert settings.has_login_credentials is False
    assert settings.has_cookie_session is False


def test_a_comment_only_env_file_yields_defaults(tmp_path: Path) -> None:
    env = write_env(tmp_path, "# nothing set here\n\n")
    settings = Settings(_env_file=env)

    assert settings.api_keys == []
    assert settings.demo_profiles == [
        "williamhgates",
        "satyanadella",
        "jeffweiner08",
    ]


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
