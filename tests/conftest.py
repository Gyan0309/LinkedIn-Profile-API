"""Shared test fixtures.

Nothing in this suite touches the network or needs a LinkedIn account. The
environment is scrubbed of every credential variable before settings are built,
so a developer with a populated `.env` gets the same run as CI does with none --
a test that passes only on the machine that has the cookie is not a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"

CREDENTIAL_VARS = (
    "LINKEDIN_LI_AT",
    "LINKEDIN_JSESSIONID",
    "LINKEDIN_EMAIL",
    "LINKEDIN_PASSWORD",
    "API_KEYS",
    "DEMO_PROFILES",
    "OUTBOUND_PROXY_URL",
    "LINKEDIN_QUERY_IDS_JSON",
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
    """Settings with no credentials and a predictable demo allowlist."""
    return Settings(
        _env_file=None,
        linkedin_li_at="",
        api_keys=["test-key-alpha"],
        demo_profiles=["demo-person"],
        cache_ttl_seconds=60,
        demo_rate_limit_per_hour=3,
        keyed_rate_limit_per_hour=10,
        outbound_max_per_minute=600,
    )
