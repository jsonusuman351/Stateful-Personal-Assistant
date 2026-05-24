"""Access token jti blacklist backed by Redis."""

from __future__ import annotations

from src.persistence.redis_client import get_redis

_KEY_PREFIX = "blacklist:jti:"


async def blacklist_token(jti: str, ttl_seconds: int) -> None:
    """Add *jti* to the blacklist with an explicit TTL.

    Key: ``blacklist:jti:{jti}``  Value: ``"1"``
    Idempotent: calling twice does not raise; TTL is refreshed on the second call.
    """
    redis = get_redis()
    await redis.set(f"{_KEY_PREFIX}{jti}", "1", ex=ttl_seconds)


async def is_blacklisted(jti: str) -> bool:
    """Return True if *jti* is present in the blacklist, False otherwise.

    O(1) Redis EXISTS — safe to call on every authenticated request (DESIGN §9.2 step 8).
    """
    redis = get_redis()
    return bool(await redis.exists(f"{_KEY_PREFIX}{jti}"))
