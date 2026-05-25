"""SSE emitter: formats events and pushes to Redis replay buffer (T-032, NFR-8)."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

_TTL_NORMAL = 300  # 5 minutes — non-HITL streams
_TTL_HITL = 900  # 15 minutes — HITL streams (approval pending)


class SSEEmitter:
    """Formats SSE events and appends them to a per-stream Redis list.

    Each event is stored as a complete SSE block (id/event/data lines).
    The Redis key TTL is extended on every emit; HITL streams get a longer
    TTL to survive the human approval window (NFR-8).
    """

    def __init__(self, stream_id: str, redis_client: aioredis.Redis) -> None:
        self._stream_id = stream_id
        self._redis = redis_client
        self._key = f"stream:{stream_id}:events"
        self._is_hitl = False

    @staticmethod
    def _format(event_id: int, event_type: str, payload: dict[str, Any]) -> str:
        data = json.dumps(payload)
        return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append a formatted SSE event to the Redis list and extend its TTL.

        Calling ``emit("approval_required", ...)`` upgrades the stream to HITL
        mode, switching the TTL from 5 min to 15 min for all subsequent emits.
        """
        if event_type == "approval_required":
            self._is_hitl = True

        ttl = _TTL_HITL if self._is_hitl else _TTL_NORMAL
        event_id: int = await self._redis.llen(self._key) + 1  # type: ignore[misc]
        text = self._format(event_id, event_type, payload)

        await self._redis.rpush(self._key, text)  # type: ignore[misc]
        await self._redis.expire(self._key, ttl)
