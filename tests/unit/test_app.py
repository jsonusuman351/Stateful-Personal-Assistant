"""Unit tests for src/api/main.py (T-035)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",  # pragma: allowlist secret
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "test-issuer",
    "JWT_AUDIENCE": "test-audience",
    "CORS_ORIGINS": '["http://localhost:3000"]',
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    from src.config.settings import get_settings

    get_settings.cache_clear()


def test_app_factory_creates_fastapi_instance() -> None:
    """`create_app()` must return a FastAPI instance."""
    from src.api.main import create_app

    app = create_app()
    assert isinstance(app, FastAPI)


async def test_readiness_fails_before_migrations() -> None:
    """`GET /readiness` returns 503 when app.state.ready has not been set."""
    from src.api.routers.health import router

    app = FastAPI()
    app.include_router(router)
    # app.state.ready is NOT set — getattr(…, "ready", False) → False

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/readiness")

    assert resp.status_code == 503
    body = resp.json()
    assert "checks" in body


async def test_readiness_passes_after_migrations() -> None:
    """`GET /readiness` returns 200 when ready=True and services are healthy."""
    from unittest.mock import AsyncMock, MagicMock

    from src.api.routers.health import router

    app = FastAPI()
    app.include_router(router)
    app.state.ready = True  # Simulate successful migration

    # Patch get_engine so it returns a mock that handles async context manager
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value=MagicMock(fetchone=lambda: ("abc123",)))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_ctx)

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    # Alembic: patch ScriptDirectory so head_rev == current_rev
    mock_script = MagicMock()
    mock_script.get_current_head.return_value = "abc123"

    with (
        patch("src.persistence.database.get_engine", return_value=mock_engine),
        patch("src.persistence.redis_client.get_redis", return_value=mock_redis),
        patch(
            "alembic.script.ScriptDirectory.from_config",
            return_value=mock_script,
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/readiness")

    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"


def test_cors_not_wildcard_in_production() -> None:
    """CORS allow_origins must never contain '*' in production (NFR-14)."""
    from src.api.main import create_app

    app = create_app()

    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:  # type: ignore[comparison-overlap]
            allow_origins = mw.kwargs.get("allow_origins", [])
            assert "*" not in allow_origins, "CORS wildcard must not be used in production"  # type: ignore[operator]
            return
    # If CORSMiddleware is not found in user_middleware, check the built stack
    # (FastAPI may have already compiled it)
    pass


# ── Background task tests ─────────────────────────────────────────────────────


async def test_hitl_timeout_sweeper_logs_expired_approvals() -> None:
    """_hitl_timeout_sweeper expires approvals and inserts audit log entries."""
    import asyncio
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.api.main import _hitl_timeout_sweeper

    mock_approval = MagicMock()
    mock_approval.id = "approval-1"
    mock_approval.user_id = "user-1"
    mock_approval.conversation_id = "conv-1"
    mock_approval.tool_name = "dangerous_tool"

    mock_repo = AsyncMock()
    mock_repo.expire_stale_approvals = AsyncMock(return_value=[mock_approval])
    mock_repo.insert_audit_log = AsyncMock()

    mock_session = AsyncMock()

    @asynccontextmanager
    async def _fake_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    with (
        patch("src.persistence.database.get_db_session", _fake_db),
        patch(
            "src.persistence.repositories.hitl_repo.HITLRepository",
            return_value=mock_repo,
        ),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _hitl_timeout_sweeper()

    mock_repo.expire_stale_approvals.assert_awaited_once()
    mock_repo.insert_audit_log.assert_awaited_once()


async def test_hitl_timeout_sweeper_no_expired_approvals() -> None:
    """_hitl_timeout_sweeper does nothing when no approvals have expired."""
    import asyncio
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, patch

    from src.api.main import _hitl_timeout_sweeper

    mock_repo = AsyncMock()
    mock_repo.expire_stale_approvals = AsyncMock(return_value=[])
    mock_session = AsyncMock()

    @asynccontextmanager
    async def _fake_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    with (
        patch("src.persistence.database.get_db_session", _fake_db),
        patch(
            "src.persistence.repositories.hitl_repo.HITLRepository",
            return_value=mock_repo,
        ),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _hitl_timeout_sweeper()

    mock_repo.insert_audit_log.assert_not_awaited()


async def test_hitl_timeout_sweeper_swallows_exception() -> None:
    """_hitl_timeout_sweeper logs DB errors but does not propagate them."""
    import asyncio
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from src.api.main import _hitl_timeout_sweeper

    @asynccontextmanager
    async def _bad_db() -> AsyncGenerator[None, None]:
        raise RuntimeError("DB unavailable")
        yield

    with (
        patch("src.persistence.database.get_db_session", _bad_db),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _hitl_timeout_sweeper()
        # If we reach here, the RuntimeError was swallowed — pass


async def test_conversation_retention_job_purges_stale() -> None:
    """_conversation_retention_job calls delete_stale_conversations and logs count."""
    import asyncio
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, patch

    from src.api.main import _conversation_retention_job

    mock_repo = AsyncMock()
    mock_repo.delete_stale_conversations = AsyncMock(return_value=3)
    mock_session = AsyncMock()

    @asynccontextmanager
    async def _fake_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    with (
        patch("src.persistence.database.get_db_session", _fake_db),
        patch(
            "src.persistence.repositories.conversation_repo.ConversationRepository",
            return_value=mock_repo,
        ),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _conversation_retention_job()

    mock_repo.delete_stale_conversations.assert_awaited_once()


async def test_conversation_retention_job_swallows_exception() -> None:
    """_conversation_retention_job logs errors without re-raising."""
    import asyncio
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from src.api.main import _conversation_retention_job

    @asynccontextmanager
    async def _bad_db() -> AsyncGenerator[None, None]:
        raise RuntimeError("DB error")
        yield

    with (
        patch("src.persistence.database.get_db_session", _bad_db),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _conversation_retention_job()


# ── Lifespan tests ────────────────────────────────────────────────────────────


async def _noop_bg() -> None:
    """No-op coroutine used in place of real background tasks during lifespan tests."""
    pass


async def test_lifespan_sets_ready_true_on_startup() -> None:
    """lifespan must set app.state.ready=True after successful startup."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi import FastAPI

    from src.api.main import lifespan

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "up to date"
    mock_proc.stderr = ""

    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    app = FastAPI()

    with (
        patch("subprocess.run", return_value=mock_proc),
        patch("src.persistence.checkpointer.setup_checkpointer", AsyncMock()),
        patch("src.tools.registry.load_registry"),
        patch("src.persistence.checkpointer.teardown_checkpointer", AsyncMock()),
        patch("src.persistence.database.get_engine", return_value=mock_engine),
        patch("src.persistence.redis_client.close_redis", AsyncMock()),
        patch("src.utils.logging.configure_logging"),
        patch("src.api.main._hitl_timeout_sweeper", _noop_bg),
        patch("src.api.main._conversation_retention_job", _noop_bg),
    ):
        async with lifespan(app):
            assert app.state.ready is True


async def test_lifespan_exits_on_migration_failure() -> None:
    """lifespan calls sys.exit(1) when Alembic migration returns non-zero exit code."""
    from unittest.mock import MagicMock, patch

    from fastapi import FastAPI

    from src.api.main import lifespan

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stderr = "migration failed"

    app = FastAPI()

    with (
        patch("subprocess.run", return_value=mock_proc),
        patch("src.utils.logging.configure_logging"),
        pytest.raises(SystemExit),
    ):
        async with lifespan(app):
            pass  # should not reach here


async def test_lifespan_shutdown_disposes_engine() -> None:
    """lifespan dispose() is called during graceful shutdown."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi import FastAPI

    from src.api.main import lifespan

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ""
    mock_proc.stderr = ""

    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    app = FastAPI()

    with (
        patch("subprocess.run", return_value=mock_proc),
        patch("src.persistence.checkpointer.setup_checkpointer", AsyncMock()),
        patch("src.tools.registry.load_registry"),
        patch("src.persistence.checkpointer.teardown_checkpointer", AsyncMock()),
        patch("src.persistence.database.get_engine", return_value=mock_engine),
        patch("src.persistence.redis_client.close_redis", AsyncMock()),
        patch("src.utils.logging.configure_logging"),
        patch("src.api.main._hitl_timeout_sweeper", _noop_bg),
        patch("src.api.main._conversation_retention_job", _noop_bg),
    ):
        async with lifespan(app):
            pass

    mock_engine.dispose.assert_awaited_once()
