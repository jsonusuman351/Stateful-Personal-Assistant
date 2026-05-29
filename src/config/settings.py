"""Pydantic BaseSettings: reads env vars and validates at startup."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from pydantic import computed_field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource

logger = logging.getLogger(__name__)


class _TolerantEnvSource(EnvSettingsSource):
    """EnvSettingsSource that accepts CSV or bare URL for list[str] fields.

    Pydantic-settings normally calls json.loads() on all complex-typed env vars
    before validators run, so CORS_ORIGINS=https://example.com crashes with
    JSONDecodeError. This subclass intercepts list[str] fields and converts
    CSV / bare URLs to a Python list before the JSON decoder is invoked.
    """

    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        """Handle CSV / bare URL values for list[str] fields."""
        if value_is_complex and isinstance(value, str):
            v = value.strip()
            if not v:
                # Return None so the field is omitted from the result dict
                # and the field default (e.g. []) is used instead.
                return None
            if v.startswith("["):
                # Looks like JSON — let the parent decode it normally.
                return super().prepare_field_value(field_name, field, v, value_is_complex)
            # CSV or bare single URL — convert directly to list, skip json.loads.
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    """Application configuration sourced entirely from environment variables.

    Required fields raise ``ValidationError`` at instantiation if absent, ensuring
    the app fails fast before accepting HTTP traffic (FR-33 startup validation).

    In tests, instantiate directly and pass ``_env_file=None`` to prevent
    pydantic-settings from reading the local ``.env`` file::

        s = Settings(_env_file=None)  # type: ignore[call-arg]
    """

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        # Belt-and-suspenders: skip empty-string env vars and use field defaults.
        "env_ignore_empty": True,
    }

    # ── Required secrets (no defaults — absent → ValidationError) ─────────────
    OPENAI_API_KEY: str
    DATABASE_URL: str
    REDIS_URL: str
    JWT_SECRET: str
    JWT_ISSUER: str
    JWT_AUDIENCE: str

    # ── Optional external APIs ────────────────────────────────────────────────
    TAVILY_API_KEY: str = ""
    WEATHER_API_KEY: str = ""
    LANGSMITH_API_KEY: str = ""

    # ── LLM configuration ─────────────────────────────────────────────────────
    OPENAI_DEFAULT_MODEL: str = "gpt-4o-mini"
    # JSON array string, e.g. '["groq/llama-3-70b"]'. Absent or '[]' → disabled.
    FALLBACK_MODELS: str | None = None

    # ── Auth ──────────────────────────────────────────────────────────────────
    AUTH_LOCKOUT_MINUTES: int = 15

    # ── Connection pools (NFR-7) ──────────────────────────────────────────────
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10
    REDIS_POOL_MIN: int = 2
    REDIS_POOL_MAX: int = 10

    # ── Logging (NFR-18) ──────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── CORS (NFR-14) ─────────────────────────────────────────────────────────
    # Accepts JSON array, CSV, or bare URL — see _TolerantEnvSource above.
    CORS_ORIGINS: list[str] = []

    # ── Quota windows (FR-26, FR-31) ──────────────────────────────────────────
    QUOTA_4H_REQUESTS: int = 20
    QUOTA_24H_REQUESTS: int = 100
    QUOTA_7D_REQUESTS: int = 500
    QUOTA_4H_TOKENS: int = 50_000
    QUOTA_24H_TOKENS: int = 200_000
    QUOTA_7D_TOKENS: int = 1_000_000

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("FALLBACK_MODELS", mode="before")
    @classmethod
    def _validate_fallback_json(cls, v: Any) -> str | None:
        """Reject FALLBACK_MODELS values that are not a valid JSON array (FR-33)."""
        if v is None or v == "":
            return None
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"FALLBACK_MODELS must be valid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("FALLBACK_MODELS must be a JSON array")
        if not all(isinstance(item, str) for item in parsed):
            raise ValueError("FALLBACK_MODELS must be a JSON array of strings")
        # v is a non-empty str at this point; explicit annotation avoids warn_return_any.
        validated: str = v
        return validated

    @model_validator(mode="after")
    def _warn_fallback_disabled(self) -> Settings:
        """Log a warning when fallback is implicitly disabled by configuration."""
        if self.FALLBACK_MODELS is None:
            logger.warning("FALLBACK_MODELS not configured; model fallback disabled")
        elif not json.loads(self.FALLBACK_MODELS):
            logger.warning("FALLBACK_MODELS is an empty list; model fallback disabled")
        return self

    # ── Computed fields (derived from FALLBACK_MODELS) ────────────────────────

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fallback_enabled(self) -> bool:
        """True when at least one fallback model is configured."""
        if not self.FALLBACK_MODELS:
            return False
        return bool(json.loads(self.FALLBACK_MODELS))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fallback_model_list(self) -> list[str]:
        """Parsed list of fallback model identifiers in priority order."""
        if not self.FALLBACK_MODELS:
            return []
        models: list[str] = json.loads(self.FALLBACK_MODELS)
        return models

    # ── Custom env source ─────────────────────────────────────────────────────

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        secrets_dir_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Replace the default EnvSettingsSource with our tolerant subclass."""
        return (
            init_settings,
            _TolerantEnvSource(settings_cls),
            dotenv_settings,
            secrets_dir_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Called once during app lifespan startup; raises ``ValidationError`` if any
    required env var is absent. Tests should bypass this cache and instantiate
    ``Settings(_env_file=None)`` directly with monkeypatched env vars.
    """
    # pydantic-settings reads required fields from the environment, not __init__
    # kwargs — mypy's stubs model them as required positional args, hence the ignore.
    return Settings()  # type: ignore[call-arg]
