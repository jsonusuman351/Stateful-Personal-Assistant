"""Unit tests for standardised exception handlers in api/middleware.py (T-042)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
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

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise-401")
    async def raise_401() -> None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    @app.post("/raise-422")
    async def raise_422(body: dict[str, Any]) -> dict[str, Any]:
        return body

    @app.get("/raise-429")
    async def raise_429() -> None:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "QUOTA_EXCEEDED",
                "message": "Too many requests.",
                "retryable": True,
                "retry_after_seconds": 60,
            },
        )

    @app.get("/raise-500")
    async def raise_500() -> None:
        raise ValueError("Something went wrong")

    return app


def _assert_standard_schema(body: dict[str, Any]) -> None:
    assert "error" in body, f"Missing 'error' key: {body}"
    error = body["error"]
    assert "code" in error
    assert "message" in error
    assert "retryable" in error
    assert "retry_after_seconds" in error


async def test_401_uses_standard_schema() -> None:
    """HTTP 401 responses must use the standardised error schema (FR-29)."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/raise-401")

    assert resp.status_code == 401
    _assert_standard_schema(resp.json())
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_422_uses_standard_schema() -> None:
    """HTTP 422 responses must use the standardised error schema (FR-29)."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/raise-422",
            content=b"not-valid-json",
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 422
    _assert_standard_schema(resp.json())
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_429_includes_retry_after() -> None:
    """HTTP 429 must include a non-null retry_after_seconds value (FR-29)."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/raise-429")

    assert resp.status_code == 429
    _assert_standard_schema(resp.json())
    assert resp.json()["error"]["retry_after_seconds"] == 60


async def test_500_uses_standard_schema() -> None:
    """Unhandled exceptions must return 500 with the standardised schema (FR-29)."""
    app = _make_app()
    # Starlette's ServerErrorMiddleware re-raises after handling, so we suppress
    # the exception propagation at the transport level to inspect the response.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        resp = await c.get("/raise-500")

    assert resp.status_code == 500
    _assert_standard_schema(resp.json())
    assert resp.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
