"""Unit tests for api/routers/chat.py (T-039)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",  # pragma: allowlist secret
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


def _make_app() -> FastAPI:
    from src.api.middleware import register_exception_handlers
    from src.api.routers.chat import router

    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    return app


def _auth_user(user_id: str = "user-123") -> object:
    from src.api.dependencies import TokenPayload

    return TokenPayload({"mode": "auth", "user_id": user_id, "jti": "jti-1"})


def _mock_redis_for_post() -> MagicMock:
    """Build a mock Redis client that handles pipeline writes."""
    mock_pipe = MagicMock()
    mock_pipe.set = MagicMock(return_value=None)
    mock_pipe.execute = AsyncMock(return_value=[True, True])

    pipeline_cm = MagicMock()
    pipeline_cm.__aenter__ = AsyncMock(return_value=mock_pipe)
    pipeline_cm.__aexit__ = AsyncMock(return_value=False)

    mock_redis = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=pipeline_cm)
    return mock_redis


async def test_post_chat_returns_202() -> None:
    """`POST /chat` with a valid message returns 202 with stream metadata."""
    from src.api.dependencies import get_current_user
    from src.quota.limiter import QuotaStatus

    user = _auth_user()
    mock_redis = _mock_redis_for_post()

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: user

    with (
        patch("src.api.routers.chat.get_redis", return_value=mock_redis),
        patch(
            "src.api.routers.chat.check_and_increment",
            AsyncMock(return_value=QuotaStatus(allowed=True)),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/chat", json={"message": "Hello"})

    assert resp.status_code == 202
    body = resp.json()
    assert "message_id" in body
    assert "session_id" in body
    assert "stream_url" in body
    assert body["stream_url"].startswith("/chat/stream?stream_id=")


async def test_post_chat_quota_exceeded_returns_429() -> None:
    """`POST /chat` returns 429 when quota is exhausted."""
    from src.api.dependencies import get_current_user
    from src.quota.limiter import QuotaStatus

    user = _auth_user()
    mock_redis = MagicMock()

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: user

    with (
        patch("src.api.routers.chat.get_redis", return_value=mock_redis),
        patch(
            "src.api.routers.chat.check_and_increment",
            AsyncMock(return_value=QuotaStatus(allowed=False, quota_type="hourly")),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/chat", json={"message": "Hello"})

    assert resp.status_code == 429
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "QUOTA_EXCEEDED"


async def test_get_stream_wrong_user_returns_403() -> None:
    """`GET /chat/stream` returns 403 when stream is owned by a different user."""
    from src.api.dependencies import get_current_user

    user = _auth_user("user-1")
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value="user-2")  # Different owner

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: user

    with patch("src.api.routers.chat.get_redis", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/chat/stream?stream_id=test-stream-id")

    assert resp.status_code == 403


async def test_get_stream_expired_returns_410() -> None:
    """`GET /chat/stream` returns 410 when the stream_id has expired."""
    from src.api.dependencies import get_current_user

    user = _auth_user()
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)  # Expired / not found

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: user

    with patch("src.api.routers.chat.get_redis", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/chat/stream?stream_id=expired-stream-id")

    assert resp.status_code == 410


async def test_post_chat_guest_user_uses_session_id() -> None:
    """`POST /chat` with a guest token derives session_id from the token."""
    from src.api.dependencies import TokenPayload, get_current_user
    from src.quota.limiter import QuotaStatus

    guest = TokenPayload({"mode": "guest", "session_id": "guest-sess-1", "jti": ""})
    mock_redis = _mock_redis_for_post()

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: guest

    with (
        patch("src.api.routers.chat.get_redis", return_value=mock_redis),
        patch(
            "src.api.routers.chat.check_and_increment",
            AsyncMock(return_value=QuotaStatus(allowed=True)),
        ),
        patch("src.api.routers.chat.build_guest_thread_id", return_value="guest-thread"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/chat", json={"message": "Hi"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["session_id"] == "guest-sess-1"


async def test_post_chat_guest_no_session_generates_id() -> None:
    """`POST /chat` with a guest token and no session_id generates a fresh UUID."""
    from src.api.dependencies import TokenPayload, get_current_user
    from src.quota.limiter import QuotaStatus

    guest = TokenPayload({"mode": "guest", "session_id": None, "jti": ""})
    mock_redis = _mock_redis_for_post()

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: guest

    with (
        patch("src.api.routers.chat.get_redis", return_value=mock_redis),
        patch(
            "src.api.routers.chat.check_and_increment",
            AsyncMock(return_value=QuotaStatus(allowed=True)),
        ),
        patch("src.api.routers.chat.build_guest_thread_id", return_value="guest-thread"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/chat", json={"message": "Hi"})

    assert resp.status_code == 202


# ── _LiveEmitter unit tests ───────────────────────────────────────────────────


async def test_live_emitter_emit_enqueues_event() -> None:
    """_LiveEmitter.emit stores the event in Redis and enqueues for streaming."""
    from unittest.mock import AsyncMock, MagicMock

    from src.api.routers.chat import _LiveEmitter

    mock_emitter = AsyncMock()
    mock_emitter._redis = AsyncMock()
    mock_emitter._redis.lindex = AsyncMock(return_value=b"data: {}\n\n")
    mock_emitter._key = "stream:test:events"
    mock_emitter.emit = AsyncMock()

    mock_redis = MagicMock()

    with patch("src.api.routers.chat.SSEEmitter", return_value=mock_emitter):
        live = _LiveEmitter("stream-id", mock_redis)
        await live.emit("thinking", {"node": "router"})

    mock_emitter.emit.assert_awaited_once_with("thinking", {"node": "router"})


async def test_live_emitter_emit_done_puts_sentinel() -> None:
    """_LiveEmitter.emit with event_type='done' enqueues a None sentinel."""
    from unittest.mock import AsyncMock, MagicMock

    from src.api.routers.chat import _LiveEmitter

    mock_emitter = AsyncMock()
    mock_emitter._redis = AsyncMock()
    mock_emitter._redis.lindex = AsyncMock(return_value="data: done\n\n")
    mock_emitter._key = "stream:test:events"
    mock_emitter.emit = AsyncMock()

    mock_redis = MagicMock()

    with patch("src.api.routers.chat.SSEEmitter", return_value=mock_emitter):
        live = _LiveEmitter("stream-id", mock_redis)
        await live.emit("done", {"message_id": "m1"})
        await live._queue.get()
        sentinel = await live._queue.get()

    assert sentinel is None


async def test_live_emitter_stream_yields_chunks() -> None:
    """_LiveEmitter.stream yields encoded bytes for each queued event."""
    from unittest.mock import AsyncMock, MagicMock

    from src.api.routers.chat import _LiveEmitter

    mock_emitter = AsyncMock()
    mock_emitter._redis = AsyncMock()
    mock_emitter._redis.lindex = AsyncMock(return_value="data: token\n\n")
    mock_emitter._key = "stream:test"
    mock_emitter.emit = AsyncMock()

    mock_redis = MagicMock()

    with patch("src.api.routers.chat.SSEEmitter", return_value=mock_emitter):
        live = _LiveEmitter("s", mock_redis)
        await live._queue.put("data: token\n\n")
        await live._queue.put(None)  # sentinel

        chunks = []
        async for chunk in live.stream():
            chunks.append(chunk)

    assert chunks == [b"data: token\n\n"]


# ── stream_chat endpoint tests ────────────────────────────────────────────────


async def test_stream_chat_context_expired_returns_410() -> None:
    """`GET /chat/stream` returns 410 when chat context has expired (no ctx_raw)."""
    from src.api.dependencies import get_current_user

    user = _auth_user("user-99")
    mock_redis = MagicMock()
    # First get: owner check passes, second get: context is None (expired)
    mock_redis.get = AsyncMock(side_effect=["user-99", None])

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: user

    with patch("src.api.routers.chat.get_redis", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/chat/stream?stream_id=some-stream-id")

    assert resp.status_code == 410


async def test_stream_chat_replay_last_event_id() -> None:
    """`GET /chat/stream` with Last-Event-ID header replays buffered events."""

    from src.api.dependencies import get_current_user

    user = _auth_user("user-42")
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value="user-42")

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: user

    with (
        patch("src.api.routers.chat.get_redis", return_value=mock_redis),
        patch(
            "src.api.routers.chat.get_events_after",
            AsyncMock(return_value=["data: token\n\n"]),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                "/chat/stream?stream_id=s1",
                headers={"Last-Event-ID": "3"},
            )

    assert resp.status_code == 200


async def test_stream_chat_replay_expired_stream_410() -> None:
    """`GET /chat/stream` returns 410 when replay store returns None."""
    from src.api.dependencies import get_current_user

    user = _auth_user("user-42")
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value="user-42")

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: user

    with (
        patch("src.api.routers.chat.get_redis", return_value=mock_redis),
        patch(
            "src.api.routers.chat.get_events_after",
            AsyncMock(return_value=None),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                "/chat/stream?stream_id=s1",
                headers={"Last-Event-ID": "5"},
            )

    assert resp.status_code == 410
