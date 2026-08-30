"""Settings, all optional. The service holds no credentials of its own."""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # How hard one caller may lean on us.
    rate_limit_per_hour: int = 60

    # How hard we may lean on LinkedIn. Every caller shares one outbound IP and
    # LinkedIn blocks an IP as a unit, so one heavy user can break it for all.
    outbound_max_per_minute: int = 30

    cache_ttl_seconds: int = 21600
    outbound_proxy_url: str = ""
    linkedin_query_ids_json: str = ""

    log_level: str = "INFO"
    log_colour: str = "auto"

    @property
    def pinned_query_ids(self) -> dict[str, str]:
        """queryIds pinned via env, overriding runtime discovery."""
        if not self.linkedin_query_ids_json.strip():
            return {}
        try:
            parsed = json.loads(self.linkedin_query_ids_json)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
