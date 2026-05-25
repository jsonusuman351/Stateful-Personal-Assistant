"""Unit tests for api/routers/tools.py (T-041)."""

from __future__ import annotations

from unittest.mock import patch

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
    from src.api.routers.tools import router

    app = FastAPI()
    app.include_router(router)
    return app


_MOCK_TOOLS = [
    {"name": "calculator", "description": "Performs arithmetic calculations.", "is_sensitive": False},
    {"name": "weather", "description": "Fetches current weather data.", "is_sensitive": False},
    {"name": "web_search", "description": "Searches the web.", "is_sensitive": True},
]


async def test_get_tools_returns_all_registered() -> None:
    """`GET /tools` returns every tool in the registry."""
    app = _make_app()

    with patch("src.tools.registry.get_tool_list", return_value=_MOCK_TOOLS):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/tools")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == len(_MOCK_TOOLS)
    names = {t["name"] for t in body}
    assert names == {"calculator", "weather", "web_search"}


async def test_get_tools_no_internal_fields() -> None:
    """`GET /tools` items must only expose name, description, is_sensitive (FR-33)."""
    app = _make_app()

    with patch("src.tools.registry.get_tool_list", return_value=_MOCK_TOOLS):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/tools")

    assert resp.status_code == 200
    allowed_keys = {"name", "description", "is_sensitive"}
    for item in resp.json():
        assert set(item.keys()) == allowed_keys, f"Unexpected keys: {set(item.keys()) - allowed_keys}"
