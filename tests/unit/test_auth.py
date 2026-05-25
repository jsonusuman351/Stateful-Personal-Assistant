"""Unit tests for api/dependencies.py and api/routers/auth.py (T-037, T-038)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",
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
    from src.api.routers.auth import router

    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    return app


def _mock_db_session() -> tuple[MagicMock, Any]:
    """Return (mock_session, mock_dep) suitable for overriding get_db."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    async def _get_db() -> AsyncGenerator[MagicMock, None]:
        yield session

    return session, _get_db


# ── T-037: dependency injection tests ─────────────────────────────────────────


async def test_valid_token_returns_payload() -> None:
    """`get_current_user` returns a payload with correct claims for a valid token."""
    import uuid

    from src.api.dependencies import get_current_user
    from src.auth.jwt import create_access_token

    user_id = str(uuid.uuid4())
    jti = str(uuid.uuid4())
    token = create_access_token(user_id, jti)

    with patch("src.api.dependencies.is_blacklisted", AsyncMock(return_value=False)):
        from fastapi.security import HTTPAuthorizationCredentials

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        payload = await get_current_user(credentials=creds)

    assert payload.user_id == user_id
    assert payload.jti == jti
    assert not payload.is_guest


async def test_blacklisted_token_returns_401() -> None:
    """`get_current_user` raises HTTP 401 when the JTI is blacklisted."""
    import uuid

    from fastapi import HTTPException

    from src.api.dependencies import get_current_user
    from src.auth.jwt import create_access_token

    user_id = str(uuid.uuid4())
    jti = str(uuid.uuid4())
    token = create_access_token(user_id, jti)

    with patch("src.api.dependencies.is_blacklisted", AsyncMock(return_value=True)):
        from fastapi.security import HTTPAuthorizationCredentials

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=creds)

    assert exc_info.value.status_code == 401


async def test_guest_token_blocked_by_require_auth() -> None:
    """`require_auth_user` raises HTTP 403 for a guest token."""
    from fastapi import HTTPException

    from src.api.dependencies import TokenPayload, require_auth_user

    guest = TokenPayload({"mode": "guest", "session_id": "abc", "jti": "x"})
    with pytest.raises(HTTPException) as exc_info:
        await require_auth_user(user=guest)

    assert exc_info.value.status_code == 403


async def test_missing_token_returns_guest_payload() -> None:
    """`get_current_user` returns a guest payload when no Authorization header is sent."""
    from src.api.dependencies import get_current_user

    payload = await get_current_user(credentials=None)

    assert payload.is_guest
    assert payload.user_id is None


# ── T-038: auth router tests ──────────────────────────────────────────────────


async def test_guest_token_issued() -> None:
    """`POST /auth/guest` returns 200 with a guest JWT and session_id."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/guest")

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "session_id" in body
    assert body["token_type"] == "bearer"

    # Verify the token is a valid guest JWT
    from src.auth.jwt import decode_token

    claims = decode_token(body["access_token"])
    assert claims["mode"] == "guest"
    assert claims["session_id"] == body["session_id"]


async def test_login_success() -> None:
    """`POST /auth/login` with valid credentials returns access + refresh tokens."""
    import uuid

    from src.auth.password import hash_password

    user_id = uuid.uuid4()
    password = "correct-password"
    hashed = hash_password(password)

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.password_hash = hashed

    mock_repo = AsyncMock()
    mock_repo.get_user_by_email = AsyncMock(return_value=mock_user)
    mock_repo.reset_failed_logins = AsyncMock()
    mock_repo.store_refresh_token = AsyncMock()

    app = _make_app()
    _, mock_get_db = _mock_db_session()
    app.dependency_overrides[__import__("src.api.dependencies", fromlist=["get_db"]).get_db] = mock_get_db

    with (
        patch("src.api.routers.auth.UserRepository", return_value=mock_repo),
        patch("src.api.routers.auth.check_ip_rate_limit", AsyncMock(return_value=(True, 0))),
        patch("src.api.routers.auth.is_email_locked", AsyncMock(return_value=(False, 0))),
        patch("src.api.routers.auth.check_email_rate_limit", AsyncMock(return_value=(True, 0))),
        patch("src.api.routers.auth.reset_attempt_counters", AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/auth/login",
                json={"email": "user@example.com", "password": password},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body


async def test_login_invalid_password_identical_response() -> None:
    """Wrong-email and wrong-password login attempts must return identical 401 bodies."""
    import uuid

    from src.auth.password import hash_password

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.password_hash = hash_password("real-password")

    # Wrong password path
    mock_repo = AsyncMock()
    mock_repo.get_user_by_email = AsyncMock(return_value=mock_user)
    mock_repo.increment_failed_logins = AsyncMock()

    app = _make_app()
    _, mock_get_db = _mock_db_session()
    from src.api.dependencies import get_db
    app.dependency_overrides[get_db] = mock_get_db

    with (
        patch("src.api.routers.auth.UserRepository", return_value=mock_repo),
        patch("src.api.routers.auth.check_ip_rate_limit", AsyncMock(return_value=(True, 0))),
        patch("src.api.routers.auth.is_email_locked", AsyncMock(return_value=(False, 0))),
        patch("src.api.routers.auth.check_email_rate_limit", AsyncMock(return_value=(True, 0))),
        patch("src.api.routers.auth.record_failed_attempt", AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp_bad_pass = await c.post(
                "/auth/login",
                json={"email": "user@example.com", "password": "wrong-password"},
            )

    # Unknown email path (get_user_by_email returns None)
    mock_repo2 = AsyncMock()
    mock_repo2.get_user_by_email = AsyncMock(return_value=None)

    with (
        patch("src.api.routers.auth.UserRepository", return_value=mock_repo2),
        patch("src.api.routers.auth.check_ip_rate_limit", AsyncMock(return_value=(True, 0))),
        patch("src.api.routers.auth.is_email_locked", AsyncMock(return_value=(False, 0))),
        patch("src.api.routers.auth.check_email_rate_limit", AsyncMock(return_value=(True, 0))),
        patch("src.api.routers.auth.record_failed_attempt", AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp_bad_email = await c.post(
                "/auth/login",
                json={"email": "nonexistent@example.com", "password": "any"},
            )

    assert resp_bad_pass.status_code == 401
    assert resp_bad_email.status_code == 401
    # Bodies must be identical (FR-16b)
    assert resp_bad_pass.json() == resp_bad_email.json()


async def test_login_ip_rate_limit() -> None:
    """11th login attempt from same IP returns HTTP 429 (FR-30a)."""
    app = _make_app()
    _, mock_get_db = _mock_db_session()
    from src.api.dependencies import get_db
    app.dependency_overrides[get_db] = mock_get_db

    with (
        patch("src.api.routers.auth.check_ip_rate_limit", AsyncMock(return_value=(False, 60))),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/auth/login",
                json={"email": "a@b.com", "password": "p"},
            )

    assert resp.status_code == 429


async def test_login_email_soft_lock() -> None:
    """5 consecutive failures lock the email; locked attempt returns HTTP 429 (FR-30c)."""
    app = _make_app()
    _, mock_get_db = _mock_db_session()
    from src.api.dependencies import get_db
    app.dependency_overrides[get_db] = mock_get_db

    with (
        patch("src.api.routers.auth.check_ip_rate_limit", AsyncMock(return_value=(True, 0))),
        patch("src.api.routers.auth.is_email_locked", AsyncMock(return_value=(True, 300))),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/auth/login",
                json={"email": "locked@example.com", "password": "p"},
            )

    assert resp.status_code == 429


async def test_refresh_token_rotation() -> None:
    """Valid refresh token returns new access + refresh pair; old token is revoked (FR-16)."""
    import uuid

    from src.auth.jwt import create_refresh_token

    user_id = str(uuid.uuid4())
    jti = str(uuid.uuid4())
    refresh_token = create_refresh_token(user_id, jti)

    stored_token = MagicMock()
    stored_token.user_id = uuid.UUID(user_id)

    mock_repo = AsyncMock()
    mock_repo.get_valid_refresh_token = AsyncMock(return_value=stored_token)
    mock_repo.revoke_refresh_token = AsyncMock()
    mock_repo.store_refresh_token = AsyncMock()

    app = _make_app()
    _, mock_get_db = _mock_db_session()
    from src.api.dependencies import get_db
    app.dependency_overrides[get_db] = mock_get_db

    with patch("src.api.routers.auth.UserRepository", return_value=mock_repo):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/auth/refresh", json={"refresh_token": refresh_token})

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    # Old token must have been revoked
    mock_repo.revoke_refresh_token.assert_called_once()


async def test_logout_blacklists_token() -> None:
    """`POST /auth/logout` adds the access token JTI to the blacklist (FR-16d)."""
    import uuid

    from src.auth.jwt import create_access_token

    user_id = str(uuid.uuid4())
    jti = str(uuid.uuid4())
    access_token = create_access_token(user_id, jti)

    app = _make_app()

    with (
        patch("src.api.dependencies.is_blacklisted", AsyncMock(return_value=False)),
        patch("src.api.routers.auth.blacklist_token", AsyncMock()) as mock_blacklist,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/auth/logout",
                headers={"Authorization": f"Bearer {access_token}"},
            )

    assert resp.status_code == 204
    mock_blacklist.assert_called_once()
    call_args = mock_blacklist.call_args
    assert call_args[0][0] == jti  # first positional arg is jti
