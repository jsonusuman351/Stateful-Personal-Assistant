"""Unit tests for the HITL timeout sweeper (T-045).

Covers:
- expire_stale_approvals() returning newly expired approval objects.
- Audit log insertion with decision='timeout' for each expired approval.
- HTTP 410 when POST /sessions/{id}/approve targets an approval already
  marked expired by the sweeper.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    """Set required env vars and clear settings cache before each test."""
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    from src.config.settings import get_settings

    get_settings.cache_clear()


# ── Repository tests ──────────────────────────────────────────────────────────


async def test_expired_approval_marked_in_db() -> None:
    """expire_stale_approvals returns the HITLApproval objects it just expired.

    The UPDATE ... RETURNING is mocked to simulate what the DB returns when
    it finds approvals where expires_at < NOW() (i.e. past the 10-minute
    deadline) and used=FALSE — exactly the scenario described by FR-11.
    """
    from src.persistence.models.hitl import HITLApproval
    from src.persistence.repositories.hitl_repo import HITLRepository

    approval_id = uuid.uuid4()

    mock_approval = MagicMock(spec=HITLApproval)
    mock_approval.id = approval_id
    mock_approval.conversation_id = uuid.uuid4()
    mock_approval.user_id = uuid.uuid4()
    mock_approval.tool_name = "web_search"
    mock_approval.expired = True

    # Simulate UPDATE ... RETURNING yielding one row
    mock_result = MagicMock()
    mock_result.scalars.return_value = [mock_approval]

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    repo = HITLRepository(mock_session)
    expired = await repo.expire_stale_approvals()

    assert len(expired) == 1
    assert expired[0].id == approval_id
    assert expired[0].expired is True
    # The repo must issue exactly one UPDATE statement
    mock_session.execute.assert_called_once()


async def test_timeout_audit_log_inserted() -> None:
    """insert_audit_log is called with decision='timeout' for each expired approval.

    This mirrors what _hitl_timeout_sweeper does after expire_stale_approvals
    returns: for every expired approval it inserts an audit-log row so the
    timeout decision is permanently recorded (FR-32).
    """
    from src.persistence.models.hitl import HITLApproval, HITLAuditLog
    from src.persistence.repositories.hitl_repo import HITLRepository

    approval_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_approval = MagicMock(spec=HITLApproval)
    mock_approval.id = approval_id
    mock_approval.conversation_id = conversation_id
    mock_approval.user_id = user_id
    mock_approval.tool_name = "web_search"
    mock_approval.expired = True

    # expire_stale_approvals execute result
    mock_expire_result = MagicMock()
    mock_expire_result.scalars.return_value = [mock_approval]

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_expire_result)
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    repo = HITLRepository(mock_session)

    # Simulate sweeper: expire then insert audit log for each expired approval
    expired = await repo.expire_stale_approvals()
    for approval in expired:
        await repo.insert_audit_log(
            approval_id=approval.id,
            user_id=approval.user_id,
            conversation_id=approval.conversation_id,
            tool_name=approval.tool_name,
            decision="timeout",
            request_ip=None,
            decision_reason=None,
        )

    mock_session.add.assert_called_once()
    added: HITLAuditLog = mock_session.add.call_args[0][0]
    assert added.decision == "timeout"
    assert added.approval_id == approval_id
    assert added.conversation_id == conversation_id
    assert added.user_id == user_id
    assert added.tool_name == "web_search"
    assert added.request_ip is None
    mock_session.flush.assert_called_once()


# ── API integration test ──────────────────────────────────────────────────────


async def test_approve_expired_id_returns_410() -> None:
    """POST /sessions/{id}/approve with an expired approval_id returns HTTP 410.

    The sweeper sets expired=TRUE on stale approvals. A user who submits the
    HITL form after the 10-minute window must receive 410 (FR-11).
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.dependencies import TokenPayload, get_db, require_auth_user
    from src.api.middleware import register_exception_handlers
    from src.api.routers.sessions import router

    user = TokenPayload({"mode": "auth", "user_id": str(uuid.uuid4()), "jti": "x"})
    approval_id = str(uuid.uuid4())

    mock_existing = MagicMock()
    mock_existing.used = False
    mock_existing.expired = True  # set by the sweeper after 10-minute window

    mock_hitl_repo = AsyncMock()
    mock_hitl_repo.get_approval = AsyncMock(return_value=mock_existing)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    async def _get_db():  # type: ignore[override]
        yield mock_session

    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    app.dependency_overrides[require_auth_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db

    with patch("src.api.routers.sessions.HITLRepository", return_value=mock_hitl_repo):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/sessions/some-session/approve",
                json={"approval_id": approval_id, "decision": "approve"},
            )

    assert resp.status_code == 410
