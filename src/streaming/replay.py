"""Replay buffer reader, ownership validation, and keep-alive (T-033, FR-14)."""

from __future__ import annotations

import asyncio
from typing import Any

import redis.asyncio as aioredis

from src.persistence.redis_client import get_redis


async def get_events_after(
    stream_id: str,
    last_event_id: int,
    redis_client: aioredis.Redis | None = None,
) -> list[str] | None:
    """Return buffered SSE event strings with id > ``last_event_id``.

    Args:
        stream_id:     Stream identifier.
        last_event_id: Last event id the client received (from ``Last-Event-ID``
                       header).  Pass 0 to indicate no prior events received.
        redis_client:  Optional injected client; defaults to the module singleton.

    Returns:
        ``None``  — Redis key absent → stream expired; caller should return
                    HTTP 410.
        ``[]``    — ``last_event_id`` is 0 (initial connect handled elsewhere),
                    or no events stored after the requested id.
        ``list``  — Ordered SSE event strings with id > ``last_event_id``.
    """
    if last_event_id == 0:
        return []

    client = redis_client or get_redis()
    key = f"stream:{stream_id}:events"

    if not await client.exists(key):
        return None

    # Events are stored sequentially: event with id n is at list index n-1.
    # We want id > last_event_id → start at index last_event_id (0-based).
    raw: list[str] = await client.lrange(key, last_event_id, -1)
    return raw


async def validate_stream_ownership(
    stream_id: str,
    user_id_or_session_id: str,
    redis_client: aioredis.Redis | None = None,
) -> bool:
    """Return True only if the stream was created by ``user_id_or_session_id``.

    Ownership is recorded at stream creation under ``stream:{stream_id}:owner``.
    Returns False for cross-session access (FR-14 → caller returns HTTP 403).
    """
    client = redis_client or get_redis()
    owner_key = f"stream:{stream_id}:owner"
    owner: str | None = await client.get(owner_key)
    return owner == user_id_or_session_id


async def start_keepalive_loop(response: Any, interval: int = 15) -> None:
    """Emit SSE keep-alive comments to ``response`` every ``interval`` seconds.

    Keep-alive lines are written directly to the HTTP response and are NOT
    stored in the Redis replay buffer (DESIGN §6.2).
    """
    while True:
        await asyncio.sleep(interval)
        await response.write(b": keep-alive\n\n")
