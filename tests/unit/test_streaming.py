"""Unit tests for streaming events, emitter, and replay buffer (T-031 to T-033)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

# ── T-031: SSE event models ───────────────────────────────────────────────────


def test_thinking_event_schema() -> None:
    """ThinkingEvent must validate required fields and set event_type discriminator."""
    from src.streaming.events import ThinkingEvent

    ev = ThinkingEvent(node="router", elapsed_ms=42)
    assert ev.event_type == "thinking"
    assert ev.node == "router"
    assert ev.elapsed_ms == 42


def test_error_event_schema() -> None:
    """ErrorEvent must validate required fields and set event_type discriminator."""
    from src.streaming.events import ErrorEvent

    ev = ErrorEvent(code="ALL_MODELS_FAILED", message="All models failed.", retryable=True)
    assert ev.event_type == "error"
    assert ev.code == "ALL_MODELS_FAILED"
    assert ev.retryable is True


def test_event_missing_field_raises() -> None:
    """Missing required field must raise ValidationError on model_validate()."""
    from src.streaming.events import ThinkingEvent

    with pytest.raises(ValidationError):
        ThinkingEvent.model_validate({"node": "router"})  # elapsed_ms missing


# ── T-032: SSEEmitter ─────────────────────────────────────────────────────────


def _mock_redis(llen_return: int = 0) -> MagicMock:
    """Return a mock aioredis.Redis with llen/rpush/expire as AsyncMocks."""
    r = MagicMock()
    r.llen = AsyncMock(return_value=llen_return)
    r.rpush = AsyncMock(return_value=1)
    r.expire = AsyncMock(return_value=True)
    return r


async def test_emit_appends_to_redis() -> None:
    """emit() must RPUSH a formatted SSE event to the correct Redis key."""
    from src.streaming.emitter import SSEEmitter

    redis = _mock_redis(llen_return=0)
    emitter = SSEEmitter("stream-abc", redis)
    await emitter.emit("thinking", {"node": "router", "elapsed_ms": 10})

    redis.rpush.assert_called_once()
    key, text = redis.rpush.call_args.args
    assert key == "stream:stream-abc:events"
    assert "event: thinking" in text
    assert '"node": "router"' in text


async def test_emit_increments_id() -> None:
    """Event id must be LLEN + 1 so ids are monotonically increasing."""
    from src.streaming.emitter import SSEEmitter

    redis = _mock_redis(llen_return=2)  # 2 existing events → next id is 3
    emitter = SSEEmitter("s", redis)
    await emitter.emit("token", {"content": "hi"})

    _, text = redis.rpush.call_args.args
    assert "id: 3" in text


async def test_emit_sets_ttl() -> None:
    """Non-HITL emit must set EXPIRE to 300 s (5 min)."""
    from src.streaming.emitter import SSEEmitter

    redis = _mock_redis()
    emitter = SSEEmitter("s", redis)
    await emitter.emit("thinking", {"node": "router", "elapsed_ms": 0})

    redis.expire.assert_called_once_with("stream:s:events", 300)


async def test_ttl_extended_on_each_emit() -> None:
    """EXPIRE must be called on every emit, extending the TTL each time."""
    from src.streaming.emitter import SSEEmitter

    redis = _mock_redis()
    emitter = SSEEmitter("s", redis)
    await emitter.emit("thinking", {"node": "router", "elapsed_ms": 0})
    await emitter.emit("token", {"content": "hello"})

    assert redis.expire.call_count == 2


# ── T-033: replay buffer ──────────────────────────────────────────────────────


async def test_replay_returns_events_after_id() -> None:
    """get_events_after must return only events whose id > last_event_id."""
    stored = [
        "id: 1\nevent: thinking\ndata: {}\n\n",
        "id: 2\nevent: token\ndata: {}\n\n",
        "id: 3\nevent: done\ndata: {}\n\n",
    ]
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)
    # lrange(key, 2, -1) → events at indices 2+ → ids 3+
    redis.lrange = AsyncMock(return_value=[stored[2]])

    from src.streaming.replay import get_events_after

    result = await get_events_after("s", last_event_id=2, redis_client=redis)

    assert result == [stored[2]]
    redis.lrange.assert_called_once_with("stream:s:events", 2, -1)


async def test_replay_expired_stream_returns_none() -> None:
    """get_events_after must return None when the Redis key does not exist."""
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)

    from src.streaming.replay import get_events_after

    result = await get_events_after("s", last_event_id=1, redis_client=redis)

    assert result is None


async def test_replay_cross_session_rejected() -> None:
    """validate_stream_ownership must return False for a different owner."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value="user-1")

    from src.streaming.replay import validate_stream_ownership

    assert await validate_stream_ownership("s", "user-2", redis_client=redis) is False
    assert await validate_stream_ownership("s", "user-1", redis_client=redis) is True
