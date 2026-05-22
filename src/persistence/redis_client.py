"""Redis async pool and singleton accessor (T-004)."""

from __future__ import annotations

import redis.asyncio as aioredis

from src.config.settings import Settings, get_settings

# ── Module-level lazy singletons ──────────────────────────────────────────────
# Initialised on first call so that import does not require live env vars
# (tests can reset these globals before each test).
_client: aioredis.Redis | None = None
_pool: aioredis.ConnectionPool | None = None


def create_redis_pool(settings: Settings) -> aioredis.ConnectionPool:
    """Create a configured async Redis connection pool from settings.

    ``max_connections`` is capped at ``REDIS_POOL_MAX`` (NFR-7).
    ``REDIS_POOL_MIN`` is attached as ``pool.min_connections``; redis-py's
    ``ConnectionPool`` has no native min-connections concept, but the attribute
    makes the configured lower bound visible for monitoring and tests.
    """
    pool: aioredis.ConnectionPool = aioredis.ConnectionPool.from_url(
        settings.REDIS_URL,
        max_connections=settings.REDIS_POOL_MAX,
        decode_responses=True,
    )
    # Attach min_connections as a custom attribute (not natively supported by
    # redis-py) so that the pool is self-describing and testable.
    pool.min_connections = settings.REDIS_POOL_MIN  # type: ignore[attr-defined]
    return pool


def get_redis() -> aioredis.Redis:
    """Return the module-level Redis singleton, creating it on first call.

    Reads settings from the cached ``get_settings()`` singleton, so the
    environment must be fully configured before the first call.
    Subsequent calls return the same ``Redis`` instance backed by the same
    connection pool (NFR-7 pool reuse).
    """
    global _client, _pool
    if _client is None:
        _pool = create_redis_pool(get_settings())
        _client = aioredis.Redis(connection_pool=_pool)
    return _client


async def close_redis() -> None:
    """Close the Redis connection pool for graceful shutdown (NFR-9).

    Safe to call even if the pool was never initialised. After this call,
    the next ``get_redis()`` invocation creates a fresh pool.
    """
    global _client, _pool
    if _client is not None:
        await _client.aclose()
        _client = None
        _pool = None
