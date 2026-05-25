"""Unit tests for async database engine and session factory (T-003)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import Settings
from src.persistence.database import create_db_engine, get_db_session

# Minimal env vars that satisfy all required Settings fields.
_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test-key",  # pragma: allowlist secret
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/testdb",  # pragma: allowlist secret
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "stateful-assistant",
    "JWT_AUDIENCE": "stateful-assistant-users",
}


@pytest.fixture()
def base_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return a Settings instance with all required fields and no .env file."""
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestPoolSize:
    def test_pool_size_matches_settings(
        self, monkeypatch: pytest.MonkeyPatch, base_settings: Settings
    ) -> None:
        """Engine pool_size and max_overflow must reflect DB_POOL_MIN / DB_POOL_MAX.

        pool_size  = DB_POOL_MIN
        max_overflow = DB_POOL_MAX - DB_POOL_MIN
        """
        monkeypatch.setenv("DB_POOL_MIN", "3")
        monkeypatch.setenv("DB_POOL_MAX", "12")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        engine = create_db_engine(settings)

        assert engine.pool.size() == 3  # type: ignore[attr-defined]
        assert engine.pool._max_overflow == 9  # type: ignore[attr-defined]  # 12 - 3


class TestSessionRollback:
    async def test_session_rollback_on_exception(self) -> None:
        """get_db_session() must rollback and re-raise when the caller raises."""
        mock_session = AsyncMock(spec=AsyncSession)
        # Simulate the AsyncSession async-context-manager protocol.
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)  # do not suppress

        mock_factory = MagicMock(return_value=mock_session)

        with patch(
            "src.persistence.database._get_session_factory",
            return_value=mock_factory,
        ):
            with pytest.raises(RuntimeError, match="db-test-error"):
                async with get_db_session() as _session:
                    raise RuntimeError("db-test-error")

        mock_session.rollback.assert_awaited_once()
        mock_session.commit.assert_not_awaited()
