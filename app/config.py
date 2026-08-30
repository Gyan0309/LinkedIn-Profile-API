"""Runtime configuration.

Every secret arrives through the environment. Nothing in this module has a default
that would work in production by accident — an unset credential yields an empty
string, and the session layer refuses to start rather than silently degrading.
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LinkedIn session ---------------------------------------------------
    linkedin_li_at: str = ""
    linkedin_jsessionid: str = ""
    linkedin_email: str = ""
    linkedin_password: str = ""

    # --- This API's own auth ------------------------------------------------
    api_keys: list[str] = Field(default_factory=list)
    demo_profiles: list[str] = Field(
        default_factory=lambda: ["williamhgates", "satyanadella", "jeffweiner08"]
    )

    # --- Operational --------------------------------------------------------
    outbound_proxy_url: str = ""
    linkedin_query_ids_json: str = ""
    cache_ttl_seconds: int = 21600
    demo_rate_limit_per_hour: int = 20
    keyed_rate_limit_per_hour: int = 300
    outbound_max_per_minute: int = 30
    log_level: str = "INFO"

    @field_validator("api_keys", "demo_profiles", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept `a,b,c` from the environment as a list.

        pydantic-settings would otherwise try to JSON-decode a bare comma string
        for a list-typed field and fail on the first non-JSON value.
        """
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def pinned_query_ids(self) -> dict[str, str]:
        """queryIds pinned via env, overriding runtime discovery. Empty when unset."""
        if not self.linkedin_query_ids_json.strip():
            return {}
        try:
            parsed = json.loads(self.linkedin_query_ids_json)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}

    @property
    def has_cookie_session(self) -> bool:
        return bool(self.linkedin_li_at.strip())

    @property
    def has_login_credentials(self) -> bool:
        return bool(self.linkedin_email.strip() and self.linkedin_password.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
