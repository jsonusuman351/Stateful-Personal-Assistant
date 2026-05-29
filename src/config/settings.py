"""Pydantic BaseSettings: reads env vars and validates at startup."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from pydantic import computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


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
    # Stored as a raw string so pydantic-settings never attempts json.loads on
    # it. Parsed to list[str] by parse_cors_origins() in src/api/main.py.
    # Accepts: bare URL, CSV, or JSON array — all handled at usage time.
    CORS_ORIGINS: str = ""

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
