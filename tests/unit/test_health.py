"""Unit tests for api/routers/health.py (T-041)."""

from __future__ import annotations

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


def _make_app(ready: bool = True) -> FastAPI:
    from src.api.routers.health import router

    app = FastAPI()
    app.include_router(router)
    if ready:
        app.state.ready = True
    return app


def _make_engine_mock(conn_side_effects: list) -> MagicMock:
    """Build an engine mock whose connect() calls use the given side_effects list."""
    contexts = []
    for conn in conn_side_effects:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        contexts.append(ctx)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(side_effect=contexts)
    return mock_engine


async def test_liveness_always_200() -> None:
    """`GET /health` returns 200 regardless of migration status."""
    app = _make_app(ready=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_503_on_db_failure() -> None:
    """`GET /readiness` returns 503 when the database is unreachable."""
    app = _make_app(ready=True)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(side_effect=Exception("connection refused"))

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    mock_script = MagicMock()
    mock_script.get_current_head = MagicMock(return_value="head-rev")

    with (
        patch("src.persistence.database.get_engine", return_value=mock_engine),
        patch("src.persistence.redis_client.get_redis", return_value=mock_redis),
        patch("alembic.script.ScriptDirectory.from_config", return_value=mock_script),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/readiness")

    assert resp.status_code == 503
    body = resp.json()
    assert "checks" in body
    assert "error" in body["checks"]["postgres"]


async def test_readiness_503_on_stale_migration() -> None:
    """`GET /readiness` returns 503 when the Alembic revision is behind head."""
    app = _make_app(ready=True)

    # First connect: postgres SELECT 1
    mock_conn_pg = AsyncMock()
    mock_conn_pg.execute = AsyncMock(return_value=MagicMock())

    # Second connect: alembic SELECT version_num — returns a stale revision
    stale_result = MagicMock()
    stale_result.fetchone = MagicMock(return_value=("stale-rev",))
    mock_conn_alembic = AsyncMock()
    mock_conn_alembic.execute = AsyncMock(return_value=stale_result)

    mock_engine = _make_engine_mock([mock_conn_pg, mock_conn_alembic])

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    mock_script = MagicMock()
    mock_script.get_current_head = MagicMock(return_value="head-rev")

    with (
        patch("src.persistence.database.get_engine", return_value=mock_engine),
        patch("src.persistence.redis_client.get_redis", return_value=mock_redis),
        patch("alembic.script.ScriptDirectory.from_config", return_value=mock_script),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/readiness")

    assert resp.status_code == 503
    body = resp.json()
    assert "stale" in body["checks"]["alembic_revision"]
