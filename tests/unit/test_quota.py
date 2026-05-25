"""Unit tests for quota/limiter.py (T-034)."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── shared env setup ──────────────────────────────────────────────────────────

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "test-issuer",
    "JWT_AUDIENCE": "test-audience",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    from src.config.settings import get_settings

    get_settings.cache_clear()


# ── helpers ───────────────────────────────────────────────────────────────────


def _mock_redis(get_returns: dict[str, int] | None = None) -> MagicMock:
    """Return a mock Redis client; get_returns maps key → integer value."""
    r = MagicMock()
    get_returns = get_returns or {}

    async def _get(key: str) -> bytes | None:
        val = get_returns.get(key)
        return str(val).encode() if val is not None else None

    r.get = AsyncMock(side_effect=_get)
    r.incr = AsyncMock(return_value=1)
    r.incrby = AsyncMock(return_value=10)
    r.expire = AsyncMock(return_value=True)
    return r


# ── T-034 tests ───────────────────────────────────────────────────────────────


async def test_quota_allows_up_to_limit() -> None:
    """All counters absent (0) → allowed=True and counters are incremented."""
    r = _mock_redis()

    from src.quota.limiter import check_and_increment

    result = await check_and_increment("user-1", None, "gpt-4o-mini", 100, redis_client=r)

    assert result.allowed is True
    r.incr.assert_called()
    r.incrby.assert_called()


async def test_quota_blocks_at_threshold() -> None:
    """21st call (4h requests = 20 = limit) must return allowed=False."""
    r = _mock_redis({"quota:user-1:4h:requests": 20})

    from src.quota.limiter import check_and_increment

    result = await check_and_increment("user-1", None, "gpt-4o-mini", 100, redis_client=r)

    assert result.allowed is False
    assert result.quota_type == "4h_requests"
    assert result.current == 20
    assert result.limit == 20
    r.incr.assert_not_called()
    r.incrby.assert_not_called()


async def test_quota_response_has_reset_at() -> None:
    """Denied QuotaStatus must carry a parseable ISO8601 reset_at timestamp."""
    r = _mock_redis({"quota:user-1:4h:requests": 20})

    from src.quota.limiter import check_and_increment

    result = await check_and_increment("user-1", None, "gpt-4o-mini", 100, redis_client=r)

    assert result.reset_at is not None
    datetime.datetime.fromisoformat(result.reset_at.rstrip("Z"))  # must not raise


async def test_fallback_model_skips_quota() -> None:
    """Non-OpenAI model must skip quota and make no Redis calls."""
    r = _mock_redis()

    from src.quota.limiter import check_and_increment

    result = await check_and_increment("user-1", None, "groq/llama-3-70b", 100, redis_client=r)

    assert result.allowed is True
    r.get.assert_not_called()


async def test_guest_ip_quota_independent_of_session() -> None:
    """IP quota at limit blocks even when session counters are fine."""
    r = _mock_redis({"quota:ip:abc123:4h:requests": 20})

    from src.quota.limiter import check_and_increment

    result = await check_and_increment("session-xyz", "abc123", "gpt-4o-mini", 100, redis_client=r)

    assert result.allowed is False
    assert result.quota_type == "ip_4h_requests"
    r.incr.assert_not_called()
    r.incrby.assert_not_called()
