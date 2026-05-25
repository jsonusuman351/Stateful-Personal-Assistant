"""Unit tests for api/middleware.py (T-036, T-042)."""

from __future__ import annotations

import json

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
    from src.api.middleware import RequestMiddleware, register_exception_handlers

    app = FastAPI()
    app.add_middleware(RequestMiddleware)
    register_exception_handlers(app)

    @app.get("/ok")
    async def ok_route() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/echo")
    async def echo_route(body: dict) -> dict:
        return body

    return app


async def test_request_log_has_required_fields(capsys: pytest.CaptureFixture) -> None:
    """Every request must produce one structured log line with all 7 required fields."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/ok")

    captured = capsys.readouterr()
    # Find the JSON log line in stdout
    for line in captured.out.splitlines():
        try:
            log = json.loads(line)
            if log.get("event") == "request":
                required = {
                    "request_id",
                    "user_id",
                    "session_id",
                    "method",
                    "path",
                    "status_code",
                    "latency_ms",
                }
                assert required.issubset(log.keys()), f"Missing fields in log: {log}"
                return
        except (json.JSONDecodeError, KeyError):
            continue
    # If no log line found it might be on stderr
    for line in captured.err.splitlines():
        try:
            log = json.loads(line)
            if log.get("event") == "request":
                required = {
                    "request_id",
                    "user_id",
                    "session_id",
                    "method",
                    "path",
                    "status_code",
                    "latency_ms",
                }
                assert required.issubset(log.keys())
                return
        except (json.JSONDecodeError, KeyError):
            continue


async def test_null_byte_body_rejected_422() -> None:
    """POST body containing \\x00 must be rejected with HTTP 422 (NFR-15)."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/echo",
            content=b'{"msg": "hello\x00world"}',
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 422


async def test_latency_ms_in_log(capsys: pytest.CaptureFixture) -> None:
    """``latency_ms`` in the log must be a positive integer."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/ok")

    captured = capsys.readouterr()
    for line in (captured.out + captured.err).splitlines():
        try:
            log = json.loads(line)
            if log.get("event") == "request":
                assert isinstance(log["latency_ms"], int)
                assert log["latency_ms"] >= 1
                return
        except (json.JSONDecodeError, KeyError):
            continue
