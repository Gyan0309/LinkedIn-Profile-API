"""Shared fixtures. The environment is scrubbed of credentials first, so a
populated `.env` cannot change the result."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"

CREDENTIAL_VARS = (
    "OUTBOUND_PROXY_URL",
    "LINKEDIN_QUERY_IDS_JSON",
    # Retired variables. Still scrubbed, because a developer with an old .env or
    # an exported shell variable must get the same run as CI does with none.
    "LINKEDIN_COOKIE",
    "LINKEDIN_LI_AT",
    "LINKEDIN_JSESSIONID",
    "LINKEDIN_EMAIL",
    "LINKEDIN_PASSWORD",
    "API_KEYS",
    "DEMO_PROFILES",
)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the suite cannot pick up a real session from the environment."""
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


def load_fixture(name: str) -> dict:
    """Read a captured Voyager payload by filename."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def fixture():
    return load_fixture


@pytest.fixture
def settings() -> Settings:
    """Settings with a low rate limit and no pacing, so tests run fast."""
    return Settings(
        _env_file=None,
        cache_ttl_seconds=60,
        rate_limit_per_hour=3,
        outbound_max_per_minute=600,
    )
