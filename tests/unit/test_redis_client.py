"""Unit tests for Redis async client and connection pool (T-004)."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

import src.persistence.redis_client as rc
from src.config.settings import Settings
from src.persistence.redis_client import close_redis, create_redis_pool, get_redis

# Minimal env vars that satisfy all required Settings fields.
_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test-key",  # pragma: allowlist secret
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/testdb",  # pragma: allowlist secret
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "stateful-assistant",
    "JWT_AUDIENCE": "stateful-assistant-users",
}


@pytest.fixture(autouse=True)
def reset_redis_singleton() -> Generator[None, None, None]:
    """Reset module-level singletons before and after every test."""
    rc._client = None
    rc._pool = None
    yield
    rc._client = None
    rc._pool = None


@pytest.fixture()
def base_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return a Settings instance with all required fields and no .env file."""
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestPoolSize:
    def test_pool_size_matches_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pool max_connections must equal REDIS_POOL_MAX; min_connections must equal REDIS_POOL_MIN."""
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("REDIS_POOL_MIN", "3")
        monkeypatch.setenv("REDIS_POOL_MAX", "15")
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        pool = create_redis_pool(settings)

        assert pool.max_connections == 15
        assert pool.min_connections == 3  # type: ignore[attr-defined]


class TestSingleton:
    def test_singleton_returns_same_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_redis() called twice must return the identical Redis instance."""
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        with patch("src.persistence.redis_client.get_settings", return_value=settings):
            client_a = get_redis()
            client_b = get_redis()

        assert client_a is client_b


class TestCloseRedis:
    async def test_close_redis_resets_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """close_redis() must release the pool and allow a fresh singleton on next call."""
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        with patch("src.persistence.redis_client.get_settings", return_value=settings):
            client_before = get_redis()
            assert rc._client is not None

            # Patch aclose so no real network call is made.
            client_before.aclose = AsyncMock()  # type: ignore[method-assign]
            await close_redis()

            assert rc._client is None
            assert rc._pool is None
            client_before.aclose.assert_awaited_once()

    async def test_close_redis_noop_when_not_initialised(self) -> None:
        """close_redis() must not raise when called before get_redis()."""
        assert rc._client is None
        await close_redis()  # should not raise
