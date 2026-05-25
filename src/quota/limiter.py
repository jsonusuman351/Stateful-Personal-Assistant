"""Sliding-window quota counters per-user and per-IP using Redis (T-034, FR-26, FR-31)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import redis.asyncio as aioredis

from src.config.settings import get_settings
from src.persistence.redis_client import get_redis

OPENAI_MODELS: frozenset[str] = frozenset(
    {
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4",
        "gpt-4-turbo",
        "gpt-4-turbo-preview",
        "gpt-3.5-turbo",
        "gpt-3.5-turbo-16k",
        "o1",
        "o1-mini",
        "o1-preview",
        "o3-mini",
    }
)

_WINDOW_SECONDS: dict[str, int] = {
    "4h": 4 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
}


@dataclass
class QuotaStatus:
    """Result of a quota check."""

    allowed: bool
    quota_type: str | None = None
    current: int | None = None
    limit: int | None = None
    reset_at: str | None = None


def _compute_reset_at(window: str) -> str:
    """Return an ISO8601 timestamp representing when the given window resets."""
    delta = datetime.timedelta(seconds=_WINDOW_SECONDS[window])
    return (datetime.datetime.utcnow() + delta).isoformat() + "Z"


async def check_and_increment(
    user_id_or_session_id: str,
    ip_hash: str | None,
    model: str,
    token_count: int,
    redis_client: aioredis.Redis | None = None,
) -> QuotaStatus:
    """Check all quota windows and increment counters if the request is allowed.

    Args:
        user_id_or_session_id: Authenticated user ID or guest session ID.
        ip_hash:               SHA-256 of the client IP; ``None`` for auth users.
                               When provided, per-IP request counters are also
                               checked — either limit triggers a deny (FR-31).
        model:                 The model being invoked.  Non-OpenAI models skip
                               quota entirely (FR-26).
        token_count:           Estimated token count to charge against token
                               windows on a successful call.
        redis_client:          Injected Redis client; defaults to the singleton.

    Returns:
        ``QuotaStatus(allowed=True)`` if all windows pass, or
        ``QuotaStatus(allowed=False, ...)`` with details on the first window that
        was exceeded.
    """
    if model not in OPENAI_MODELS:
        return QuotaStatus(allowed=True)

    settings = get_settings()
    client = redis_client or get_redis()
    uid = user_id_or_session_id

    req_limits: dict[str, int] = {
        "4h": settings.QUOTA_4H_REQUESTS,
        "24h": settings.QUOTA_24H_REQUESTS,
        "7d": settings.QUOTA_7D_REQUESTS,
    }
    tok_limits: dict[str, int] = {
        "4h": settings.QUOTA_4H_TOKENS,
        "24h": settings.QUOTA_24H_TOKENS,
        "7d": settings.QUOTA_7D_TOKENS,
    }

    # Build ordered check table: (key, quota_type_label, limit, window)
    checks: list[tuple[str, str, int, str]] = []
    for window in ("4h", "24h", "7d"):
        checks.append(
            (f"quota:{uid}:{window}:requests", f"{window}_requests", req_limits[window], window)
        )
        checks.append(
            (f"quota:{uid}:{window}:tokens", f"{window}_tokens", tok_limits[window], window)
        )
    if ip_hash is not None:
        for window in ("4h", "24h", "7d"):
            checks.append(
                (
                    f"quota:ip:{ip_hash}:{window}:requests",
                    f"ip_{window}_requests",
                    req_limits[window],
                    window,
                )
            )

    # GET all current values in one sequential pass
    raw_values: dict[str, int] = {}
    for key, _, _, _ in checks:
        raw = await client.get(key)
        raw_values[key] = int(raw) if raw else 0

    # Check all limits before any mutation
    for key, quota_type, limit, window in checks:
        current = raw_values[key]
        if current >= limit:
            return QuotaStatus(
                allowed=False,
                quota_type=quota_type,
                current=current,
                limit=limit,
                reset_at=_compute_reset_at(window),
            )

    # All windows passed — increment and set TTL on first creation
    for key, _, _, window in checks:
        if "tokens" in key:
            await client.incrby(key, token_count)
        else:
            await client.incr(key)
        if raw_values[key] == 0:
            await client.expire(key, _WINDOW_SECONDS[window])

    return QuotaStatus(allowed=True)
