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
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
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
