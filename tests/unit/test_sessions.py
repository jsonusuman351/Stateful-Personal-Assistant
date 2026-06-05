"""Unit tests for api/routers/sessions.py (T-040)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from typing import Any
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/sessions/some-session-id/model",
            json={"model": "not-a-real-model-xyz-99999"},
        )

    assert resp.status_code == 422


async def test_get_messages_returns_list() -> None:
    """`GET /sessions/{id}/messages` returns messages when conversation exists."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    user = TokenPayload({"mode": "auth", "user_id": user_id, "jti": "x"})

    mock_msg = MagicMock()
    mock_msg.id = uuid.uuid4()
    mock_msg.role = "user"
    mock_msg.content = "Hello"
    mock_msg.tool_name = None
    mock_msg.turn_index = 0
    mock_msg.created_at = datetime(2024, 1, 1, 12, 0, 0)

    mock_msg_repo = AsyncMock()
    mock_msg_repo.list_messages = AsyncMock(return_value=[mock_msg])

    # The endpoint resolves the conversation first (is_deleted-aware ownership
    # check) before returning messages, so ConversationRepository must be mocked.
    mock_conv_repo = AsyncMock()
    mock_conv_repo.get_conversation = AsyncMock(return_value=MagicMock())

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with (
        patch("src.api.routers.sessions.MessageRepository", return_value=mock_msg_repo),
        patch("src.api.routers.sessions.ConversationRepository", return_value=mock_conv_repo),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/sessions/{session_id}/messages")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "Hello"


async def test_get_messages_empty_conv_not_found_returns_403() -> None:
    """`GET /sessions/{id}/messages` returns 403 when messages empty and conv missing."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    user = TokenPayload({"mode": "auth", "user_id": user_id, "jti": "x"})

    mock_msg_repo = AsyncMock()
    mock_msg_repo.list_messages = AsyncMock(return_value=[])

    mock_conv_repo = AsyncMock()
    mock_conv_repo.get_conversation = AsyncMock(return_value=None)

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with (
        patch("src.api.routers.sessions.MessageRepository", return_value=mock_msg_repo),
        patch("src.api.routers.sessions.ConversationRepository", return_value=mock_conv_repo),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/sessions/{session_id}/messages")

    assert resp.status_code == 403


async def test_delete_session_success_returns_204() -> None:
    """`DELETE /sessions/{id}` returns 204 when the session exists and belongs to user."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    user = TokenPayload({"mode": "auth", "user_id": user_id, "jti": "x"})

    mock_conv = MagicMock()
    mock_repo = AsyncMock()
    mock_repo.get_conversation = AsyncMock(return_value=mock_conv)
    mock_repo.soft_delete_conversation = AsyncMock()

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.sessions.ConversationRepository", return_value=mock_repo):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/sessions/{session_id}")

    assert resp.status_code == 204
    # DELETE is a soft delete — sets is_deleted=True, does not hard-delete the row
    mock_repo.soft_delete_conversation.assert_awaited_once()


async def test_delete_session_invalid_uuid_returns_403() -> None:
    """`DELETE /sessions/{id}` returns 403 for a non-UUID session_id."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete("/sessions/not-a-uuid")

    assert resp.status_code == 403


async def test_delete_session_not_owned_returns_403() -> None:
    """`DELETE /sessions/{id}` returns 403 when the conversation belongs to another user."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    user = TokenPayload({"mode": "auth", "user_id": user_id, "jti": "x"})

    mock_repo = AsyncMock()
    mock_repo.get_conversation = AsyncMock(return_value=None)  # not found for this user

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.sessions.ConversationRepository", return_value=mock_repo):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/sessions/{session_id}")

    assert resp.status_code == 403


async def test_approve_invalid_uuid_returns_422() -> None:
    """`POST /sessions/{id}/approve` returns 422 for a non-UUID approval_id."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/sessions/some-session/approve",
            json={"approval_id": "not-a-uuid", "decision": "approve"},
        )

    assert resp.status_code == 422


async def test_approve_not_found_returns_410() -> None:
    """`POST /sessions/{id}/approve` returns 410 when approval_id does not exist."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    approval_id = str(uuid.uuid4())

    mock_hitl_repo = AsyncMock()
    mock_hitl_repo.get_approval = AsyncMock(return_value=None)

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.sessions.HITLRepository", return_value=mock_hitl_repo):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/sessions/some-session/approve",
                json={"approval_id": approval_id, "decision": "approve"},
            )

    assert resp.status_code == 410


async def test_approve_success_returns_200() -> None:
    """`POST /sessions/{id}/approve` returns 200 on successful approval."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user

    user_id = str(uuid.uuid4())
    user = TokenPayload({"mode": "auth", "user_id": user_id, "jti": "x"})
    approval_id = str(uuid.uuid4())

    mock_approval = MagicMock()
    mock_approval.used = False
    mock_approval.expired = False
    mock_approval.id = uuid.UUID(approval_id)
    mock_approval.conversation_id = uuid.uuid4()
    mock_approval.tool_name = "send_email"
    mock_approval.thread_id = "thread-1"

    mock_hitl_repo = AsyncMock()
    mock_hitl_repo.get_approval = AsyncMock(return_value=mock_approval)
    mock_hitl_repo.consume_approval = AsyncMock(return_value=mock_approval)
    mock_hitl_repo.insert_audit_log = AsyncMock()

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    def _close_task_coro(coro: Any, **kwargs: Any) -> MagicMock:
        coro.close()  # prevent "coroutine never awaited" warning
        return MagicMock()

    with (
        patch("src.api.routers.sessions.HITLRepository", return_value=mock_hitl_repo),
        patch("asyncio.create_task", side_effect=_close_task_coro),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/sessions/some-session/approve",
                json={"approval_id": approval_id, "decision": "approve"},
            )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_model_switch_valid_model_returns_200() -> None:
    """`POST /sessions/{id}/model` with a valid model returns 200."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user
    from src.quota.limiter import OPENAI_MODELS

    valid_model = next(iter(OPENAI_MODELS))
    user_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    user = TokenPayload({"mode": "auth", "user_id": user_id, "jti": "x"})

    mock_conv = MagicMock()
    mock_repo = AsyncMock()
    mock_repo.get_conversation = AsyncMock(return_value=mock_conv)

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with (
        patch("src.api.routers.sessions.ConversationRepository", return_value=mock_repo),
        patch("src.persistence.redis_client.get_redis", return_value=mock_redis),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/sessions/{session_id}/model",
                json={"model": valid_model},
            )

    assert resp.status_code == 200
    assert resp.json()["active_model"] == valid_model


async def test_model_switch_conv_not_found_returns_403() -> None:
    """`POST /sessions/{id}/model` returns 403 when conversation not owned by user."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user
    from src.quota.limiter import OPENAI_MODELS

    valid_model = next(iter(OPENAI_MODELS))
    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    session_id = str(uuid.uuid4())

    mock_repo = AsyncMock()
    mock_repo.get_conversation = AsyncMock(return_value=None)

    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.sessions.ConversationRepository", return_value=mock_repo):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/sessions/{session_id}/model",
                json={"model": valid_model},
            )

    assert resp.status_code == 403


async def test_model_switch_invalid_session_uuid_returns_403() -> None:
    """`POST /sessions/{id}/model` returns 403 for a non-UUID session_id."""
    from src.api.dependencies import TokenPayload, get_db, require_auth_user
    from src.quota.limiter import OPENAI_MODELS

    valid_model = next(iter(OPENAI_MODELS))
    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    _, mock_get_db = _mock_db_session()
    app = _make_app()
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = mock_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/sessions/not-a-uuid/model",
            json={"model": valid_model},
        )

    assert resp.status_code == 403
