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
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
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
            AsyncMock(
                return_value=QuotaStatus(allowed=False, quota_type="hourly")
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
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
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
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
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/chat/stream?stream_id=expired-stream-id")

    assert resp.status_code == 410
