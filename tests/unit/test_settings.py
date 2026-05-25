"""Unit tests for application settings startup validation (T-002)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.settings import Settings

# Minimal env vars that satisfy all required fields.
_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test-key",  # pragma: allowlist secret
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/testdb",  # pragma: allowlist secret
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "stateful-assistant",
    "JWT_AUDIENCE": "stateful-assistant-users",
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all required env vars and clear FALLBACK_MODELS before each test.

    ``_env_file=None`` is passed to every Settings() call in this module so
    that a local ``.env`` file cannot override monkeypatched values.
    """
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    """Apply additional env overrides then return a fresh, isolated Settings instance."""
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    # _env_file=None prevents reading any local .env file during tests.
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestMissingRequiredVar:
    def test_missing_required_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent required env var raises ValidationError before any route is registered."""
        monkeypatch.delenv("OPENAI_API_KEY")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]


class TestFallbackModels:
    def test_fallback_models_malformed_json_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-JSON FALLBACK_MODELS raises ValidationError with a descriptive message."""
        with pytest.raises(ValidationError, match="FALLBACK_MODELS must be valid JSON"):
            _settings(monkeypatch, FALLBACK_MODELS="not-json")

    def test_fallback_models_absent_sets_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent FALLBACK_MODELS results in fallback_enabled=False."""
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)
        s = _settings(monkeypatch)
        assert s.fallback_enabled is False

    def test_fallback_models_empty_list_sets_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FALLBACK_MODELS='[]' results in fallback_enabled=False."""
        s = _settings(monkeypatch, FALLBACK_MODELS="[]")
        assert s.fallback_enabled is False

    def test_fallback_models_valid_sets_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-empty JSON array enables fallback and exposes the parsed list."""
        s = _settings(monkeypatch, FALLBACK_MODELS='["groq/llama-3-70b"]')
        assert s.fallback_enabled is True
        assert s.fallback_model_list == ["groq/llama-3-70b"]


class TestPoolSizes:
    def test_pool_sizes_read_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB and Redis pool sizes are read from env vars and exposed on Settings."""
        s = _settings(
            monkeypatch,
            DB_POOL_MIN="3",
            DB_POOL_MAX="15",
            REDIS_POOL_MIN="4",
            REDIS_POOL_MAX="20",
        )
        assert s.DB_POOL_MIN == 3
        assert s.DB_POOL_MAX == 15
        assert s.REDIS_POOL_MIN == 4
        assert s.REDIS_POOL_MAX == 20
