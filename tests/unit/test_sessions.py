"""Unit tests for api/routers/sessions.py (T-040)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
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
    from src.api.routers.sessions import router

    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    return app


def _mock_db_session() -> tuple[MagicMock, Callable[[], AsyncGenerator[MagicMock, None]]]:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    async def _get_db() -> AsyncGenerator[MagicMock, None]:
        yield session

    return session, _get_db


async def test_get_sessions_guest_forbidden() -> None:
    """Guest tokens are rejected with 403 for `GET /sessions`."""
    from src.api.dependencies import TokenPayload, get_current_user

    guest = TokenPayload({"mode": "guest", "session_id": "sess1", "jti": ""})
    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: guest

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/sessions")

    assert resp.status_code == 403


async def test_get_sessions_pagination() -> None:
    """`GET /sessions` returns a paginated sessions list for an auth user."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user_id = str(uuid.uuid4())
    user = TokenPayload({"mode": "auth", "user_id": user_id, "jti": "x"})

    mock_conv = MagicMock()
    mock_conv.id = uuid.uuid4()
    mock_conv.title = "Test session"
    mock_conv.last_accessed = datetime(2024, 1, 1, 12, 0, 0)
    mock_conv.message_count = 5
    mock_conv.access_count = 2

    mock_repo = AsyncMock()
    mock_repo.list_conversations = AsyncMock(return_value=([mock_conv], None))

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.sessions.ConversationRepository", return_value=mock_repo):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert "sessions" in body
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["title"] == "Test session"
    assert body["next_cursor"] is None


async def test_get_messages_cross_user_forbidden() -> None:
    """`GET /sessions/{id}/messages` returns 403 for an invalid session UUID."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/sessions/not-a-valid-uuid/messages")

    assert resp.status_code == 403


async def test_approve_concurrent_409() -> None:
    """Concurrent HITL approval — second consumer loses the race and gets 409."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    approval_id = str(uuid.uuid4())

    mock_existing = MagicMock()
    mock_existing.used = False
    mock_existing.expired = False

    mock_hitl_repo = AsyncMock()
    mock_hitl_repo.get_approval = AsyncMock(return_value=mock_existing)
    mock_hitl_repo.consume_approval = AsyncMock(return_value=None)  # Race lost

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.sessions.HITLRepository", return_value=mock_hitl_repo):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/sessions/some-session/approve",
                json={"approval_id": approval_id, "decision": "approve"},
            )

    assert resp.status_code == 409


async def test_approve_replayed_id_410() -> None:
    """Already-consumed HITL approval_id returns 410."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    approval_id = str(uuid.uuid4())

    mock_existing = MagicMock()
    mock_existing.used = True  # Already consumed
    mock_existing.expired = False

    mock_hitl_repo = AsyncMock()
    mock_hitl_repo.get_approval = AsyncMock(return_value=mock_existing)

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.sessions.HITLRepository", return_value=mock_hitl_repo):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/sessions/some-session/approve",
                json={"approval_id": approval_id, "decision": "approve"},
            )

    assert resp.status_code == 410


async def test_model_switch_invalid_name_422() -> None:
    """`POST /sessions/{id}/model` with an unregistered model name returns 422."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/sessions/some-session-id/model",
            json={"model": "not-a-real-model-xyz-99999"},
        )

    assert resp.status_code == 422
