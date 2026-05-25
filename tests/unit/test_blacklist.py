"""Unit tests for the access token JTI blacklist (T-015)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.auth.blacklist import blacklist_token, is_blacklisted


def _make_redis(exists_return: int = 0) -> AsyncMock:
    """Return a mock Redis client with configurable EXISTS response."""
    mock = AsyncMock()
    mock.set = AsyncMock(return_value=True)
    mock.exists = AsyncMock(return_value=exists_return)
    mock.ttl = AsyncMock(return_value=300)
    return mock


class TestBlacklistToken:
    async def test_blacklisted_token_detected(self) -> None:
        """After blacklist_token, is_blacklisted must return True."""
        redis = _make_redis(exists_return=1)

        with patch("src.auth.blacklist.get_redis", return_value=redis):
            await blacklist_token("jti-001", ttl_seconds=3600)
            result = await is_blacklisted("jti-001")

        assert result is True

    async def test_unlisted_token_allowed(self) -> None:
        """is_blacklisted returns False for a jti not in the blacklist."""
        redis = _make_redis(exists_return=0)

        with patch("src.auth.blacklist.get_redis", return_value=redis):
            result = await is_blacklisted("jti-unknown")

        assert result is False

    async def test_ttl_set_on_write(self) -> None:
        """blacklist_token must pass the given TTL to Redis SET."""
        redis = _make_redis()

        with patch("src.auth.blacklist.get_redis", return_value=redis):
            await blacklist_token("jti-002", ttl_seconds=1800)

        redis.set.assert_awaited_once_with("blacklist:jti:jti-002", "1", ex=1800)

    async def test_blacklist_token_idempotent(self) -> None:
        """Calling blacklist_token twice on the same jti must not raise."""
        redis = _make_redis()

        with patch("src.auth.blacklist.get_redis", return_value=redis):
            await blacklist_token("jti-003", ttl_seconds=3600)
            await blacklist_token("jti-003", ttl_seconds=3600)

        assert redis.set.await_count == 2
