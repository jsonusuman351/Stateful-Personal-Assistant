"""Unit tests for login rate limiting and brute-force lockout (T-016)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.auth.rate_limit import (
    check_email_rate_limit,
    check_ip_rate_limit,
    is_email_locked,
    record_failed_attempt,
    reset_attempt_counters,
)

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",  # pragma: allowlist secret
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "test-issuer",
    "JWT_AUDIENCE": "test-audience",
    "AUTH_LOCKOUT_MINUTES": "15",
}


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject required env vars and clear settings cache."""
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    from src.config.settings import get_settings

    get_settings.cache_clear()


def _redis_with_counter(count: int, ttl: int = 600) -> AsyncMock:
    """Return a mock Redis whose GET returns *count* and TTL returns *ttl*."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=str(count).encode())
    mock.ttl = AsyncMock(return_value=ttl)
    mock.incr = AsyncMock(return_value=count)
    mock.expire = AsyncMock(return_value=True)
    mock.set = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=1)
    mock.exists = AsyncMock(return_value=0)
    return mock


def _redis_empty() -> AsyncMock:
    """Return a mock Redis whose GET returns None (no key exists)."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=None)
    mock.ttl = AsyncMock(return_value=-2)  # -2 = key does not exist
    mock.incr = AsyncMock(return_value=1)
    mock.expire = AsyncMock(return_value=True)
    mock.set = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=1)
    mock.exists = AsyncMock(return_value=0)
    return mock


class TestIpRateLimit:
    async def test_ip_rate_limit_allows_below_threshold(self) -> None:
        """check_ip_rate_limit allows requests when count < 10."""
        redis = _redis_with_counter(count=9)
        with patch("src.auth.rate_limit.get_redis", return_value=redis):
            allowed, retry_after = await check_ip_rate_limit("ip-hash-1")
        assert allowed is True
        assert retry_after == 0

    async def test_ip_rate_limit_blocks_at_threshold(self) -> None:
        """check_ip_rate_limit blocks when count >= 10 and returns retry_after."""
        redis = _redis_with_counter(count=10, ttl=300)
        with patch("src.auth.rate_limit.get_redis", return_value=redis):
            allowed, retry_after = await check_ip_rate_limit("ip-hash-2")
        assert allowed is False
        assert retry_after == 300

    async def test_ip_rate_limit_allows_no_key(self) -> None:
        """check_ip_rate_limit allows when no Redis key exists yet."""
        redis = _redis_empty()
        with patch("src.auth.rate_limit.get_redis", return_value=redis):
            allowed, retry_after = await check_ip_rate_limit("ip-hash-3")
        assert allowed is True


class TestEmailRateLimit:
    async def test_email_rate_limit_blocks_at_threshold(self) -> None:
        """check_email_rate_limit blocks when count >= 10."""
        redis = _redis_with_counter(count=10, ttl=500)
        with patch("src.auth.rate_limit.get_redis", return_value=redis):
            allowed, retry_after = await check_email_rate_limit("email-hash-1")
        assert allowed is False
        assert retry_after == 500


class TestRecordFailedAttempt:
    async def test_email_soft_lock_after_five_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After 5 record_failed_attempt calls, is_email_locked returns True."""
        call_count = [0]
        lockout_ttl = [0]

        async def _incr(key: str) -> int:
            call_count[0] += 1
            return call_count[0]

        async def _set(key: str, value: str, ex: int | None = None) -> bool:
            if "lockout" in key:
                lockout_ttl[0] = ex or 0
            return True

        async def _ttl(key: str) -> int:
            if "lockout" in key and lockout_ttl[0] > 0:
                return lockout_ttl[0]
            return -2

        redis = AsyncMock()
        redis.incr = AsyncMock(side_effect=_incr)
        redis.expire = AsyncMock(return_value=True)
        redis.set = AsyncMock(side_effect=_set)
        redis.get = AsyncMock(return_value=None)
        redis.ttl = AsyncMock(side_effect=_ttl)

        with patch("src.auth.rate_limit.get_redis", return_value=redis):
            for _ in range(5):
                await record_failed_attempt("email-hash-x")
            locked_flag, retry_after = await is_email_locked("email-hash-x")

        assert locked_flag is True
        assert retry_after > 0

    async def test_lockout_returns_429_not_401(self) -> None:
        """is_email_locked must signal a 429-class response (locked=True, retry_after>0)."""
        redis = AsyncMock()
        redis.ttl = AsyncMock(return_value=600)

        with patch("src.auth.rate_limit.get_redis", return_value=redis):
            locked, retry_after = await is_email_locked("locked-email-hash")

        # The caller (router) should respond 429, not 401.
        assert locked is True
        assert retry_after > 0


class TestResetCounters:
    async def test_successful_login_resets_counters(self) -> None:
        """reset_attempt_counters deletes both email rate-limit and lockout keys."""
        redis = _redis_empty()

        with patch("src.auth.rate_limit.get_redis", return_value=redis):
            await reset_attempt_counters("email-hash-y")

        deleted_keys = redis.delete.call_args[0]
        assert any("ratelimit:email" in k for k in deleted_keys)
        assert any("lockout" in k for k in deleted_keys)
