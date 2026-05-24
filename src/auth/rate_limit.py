"""Login rate limiter: per-IP, per-email, and soft-lock lockout logic."""

from __future__ import annotations

from src.config.settings import get_settings
from src.persistence.redis_client import get_redis

_RATE_LIMIT_WINDOW: int = 15 * 60  # 15 minutes in seconds
_RATE_LIMIT_THRESHOLD: int = 10
_SOFT_LOCK_THRESHOLD: int = 5

_IP_KEY_PREFIX = "auth:ratelimit:ip:"
_EMAIL_KEY_PREFIX = "auth:ratelimit:email:"
_LOCKOUT_KEY_PREFIX = "auth:lockout:"


async def check_ip_rate_limit(ip_hash: str) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds) for the given IP.

    Blocks after 10 login attempts within 15 minutes (FR-30a).
    Returns ``(False, ttl)`` when blocked, ``(True, 0)`` when allowed.
    """
    redis = get_redis()
    key = f"{_IP_KEY_PREFIX}{ip_hash}"
    count_raw = await redis.get(key)
    if count_raw is not None and int(count_raw) >= _RATE_LIMIT_THRESHOLD:
        ttl = await redis.ttl(key)
        return False, max(ttl, 0)
    return True, 0


async def check_email_rate_limit(email_hash: str) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds) for the given email.

    Blocks after 10 login attempts within 15 minutes (FR-30b).
    Returns ``(False, ttl)`` when blocked, ``(True, 0)`` when allowed.
    """
    redis = get_redis()
    key = f"{_EMAIL_KEY_PREFIX}{email_hash}"
    count_raw = await redis.get(key)
    if count_raw is not None and int(count_raw) >= _RATE_LIMIT_THRESHOLD:
        ttl = await redis.ttl(key)
        return False, max(ttl, 0)
    return True, 0


async def record_failed_attempt(email_hash: str) -> None:
    """Increment the per-email failure counter and set a soft-lock at 5 failures.

    First increment creates the key with a 15-minute TTL (NFR-8).
    At 5 failures, sets ``auth:lockout:{email_hash}`` for AUTH_LOCKOUT_MINUTES (FR-30c).
    """
    redis = get_redis()
    settings = get_settings()
    key = f"{_EMAIL_KEY_PREFIX}{email_hash}"

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _RATE_LIMIT_WINDOW)

    if count >= _SOFT_LOCK_THRESHOLD:
        lockout_key = f"{_LOCKOUT_KEY_PREFIX}{email_hash}"
        lockout_ttl = settings.AUTH_LOCKOUT_MINUTES * 60
        await redis.set(lockout_key, "1", ex=lockout_ttl)


async def is_email_locked(email_hash: str) -> tuple[bool, int]:
    """Return (locked, retry_after_seconds) for the given email.

    Reads ``auth:lockout:{email_hash}``; returns ``(True, ttl)`` when locked,
    ``(False, 0)`` otherwise.
    """
    redis = get_redis()
    key = f"{_LOCKOUT_KEY_PREFIX}{email_hash}"
    ttl = await redis.ttl(key)
    if ttl > 0:
        return True, ttl
    return False, 0


async def reset_attempt_counters(email_hash: str) -> None:
    """Delete the email rate-limit counter and lockout key on successful login.

    Called by the login handler on successful authentication to clear any
    accumulated failure state (FR-30e).
    """
    redis = get_redis()
    await redis.delete(
        f"{_EMAIL_KEY_PREFIX}{email_hash}",
        f"{_LOCKOUT_KEY_PREFIX}{email_hash}",
    )
