"""Shared pytest fixtures for the full test suite (T-047 — test infrastructure).

Unit tests (``tests/unit/``) use ``db_engine`` / ``db_session`` and the mock
fixtures independently.  Integration tests (``tests/integration/``) use the
higher-level ``async_client`` fixture which bundles them together.

Environment variables required for DB-backed tests:
    TEST_DATABASE_URL — async-compatible PostgreSQL URL, e.g.
        ``postgresql+asyncpg://user:pass@localhost/test_db``  # pragma: allowlist secret

Environment variables required for Redis-backed tests (optional; defaults
to ``redis://localhost:6379/15``):
    TEST_REDIS_URL — e.g. ``redis://localhost:6379/15``
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import src.persistence.redis_client as _rc_module
from src.persistence.models.base import Base

# ── Environment defaults for the test app ────────────────────────────────────

_BASE_TEST_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test-key-for-tests-only",  # pragma: allowlist secret
    "REDIS_URL": "redis://localhost:6379/15",
    "JWT_SECRET": "testsecret1testsecret2testsecret3",  # >= 32 chars  # pragma: allowlist secret
    "JWT_ISSUER": "test-issuer",
    "JWT_AUDIENCE": "test-audience",
    "CORS_ORIGINS": '["http://localhost:3000"]',
    "WEATHER_API_KEY": "test-weather-key",  # pragma: allowlist secret
    "TAVILY_API_KEY": "test-tavily-key",  # pragma: allowlist secret
}


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "requires_db: mark test as requiring a database connection")


# ── Database fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[Any, None]:
    """Create an async engine for testing.

    Uses a real PostgreSQL instance if TEST_DATABASE_URL is set, otherwise
    skips tests that require the database.

    The test database should be created separately and must exist before
    tests run. Typically this is set up in a Docker container or local
    PostgreSQL instance.
    """
    db_url = os.environ.get("TEST_DATABASE_URL")

    if not db_url:
        pytest.skip(
            "TEST_DATABASE_URL not set. Set it to a PostgreSQL URL to run database tests. "
            "Example: postgresql+asyncpg://user:password@localhost/test_db"  # pragma: allowlist secret
        )

    # Convert URL to async driver if needed
    if "postgresql://" in db_url and "asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")

    engine = create_async_engine(db_url, echo=False)

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Drop all tables after tests
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: Any) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional AsyncSession for each test.

    Rolls back after each test to ensure isolation between unit tests that
    hit the repository layer directly.
    """
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            yield session
            # Rollback after test for isolation
            await session.rollback()


# ── Redis fixture ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Any, None]:
    """Real Redis client on a test-specific DB index (default: 15).

    Flushes the test DB after each test.  Also overrides the module-level
    singleton in ``src.persistence.redis_client`` so that all production code
    paths (blacklist, rate-limiting, quota, SSE emitter) automatically use the
    test client without any additional patching.

    Skip behaviour: skips the test if Redis is not reachable.
    """
    import redis.asyncio as aioredis

    redis_url = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
    client: aioredis.Redis = aioredis.Redis.from_url(redis_url, decode_responses=True)

    try:
        await client.ping()  # type: ignore[misc]
    except Exception:
        pytest.skip(f"Redis not reachable at {redis_url}")

    # Override the module-level singleton so all production get_redis() calls
    # return the test client for the duration of this fixture.
    original = _rc_module._client
    _rc_module._client = client

    yield client

    # Restore and clean up
    _rc_module._client = original
    await client.flushdb()
    await client.aclose()


# ── Mock external-service fixtures ────────────────────────────────────────────


@pytest.fixture
def mock_openai(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ChatOpenAI in the llm and router nodes with a configurable mock.

    Returns a ``MockOpenAI`` instance.  Call ``.set_response(text)`` to
    configure plain-text responses or ``.set_tool_calls([...])`` to simulate
    tool-selection responses.

    Both graph nodes (``src.graph.nodes.llm`` and ``src.graph.nodes.router``)
    are patched with the same mock so ``call_count`` reflects total LLM calls.
    """
    from tests.fixtures.mock_openai import MockOpenAI

    mock = MockOpenAI()
    mock_class = mock.as_class()
    monkeypatch.setattr("src.graph.nodes.llm.ChatOpenAI", mock_class)
    monkeypatch.setattr("src.graph.nodes.router.ChatOpenAI", mock_class)
    return mock


@pytest.fixture
def mock_tavily(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch WebSearchTool._search_sync with a configurable mock.

    Returns a ``MockTavily`` instance.  Call ``.set_results([...])`` to
    configure the raw result list; ``WebSearchTool`` still applies its own
    relevance filter and truncation on top.
    """
    from tests.fixtures.mock_tavily import MockTavily

    mock = MockTavily()
    monkeypatch.setattr(
        "src.tools.web_search.WebSearchTool._search_sync",
        mock.as_method(),
    )
    return mock


@pytest.fixture
def mock_weather(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch WeatherTool._fetch_sync with a configurable mock.

    Returns a ``MockWeather`` instance.  Call ``.set_weather(temp, cond, hum)``
    to configure the returned weather payload.
    """
    from tests.fixtures.mock_weather import MockWeather

    mock = MockWeather()
    monkeypatch.setattr(
        "src.tools.weather.WeatherTool._fetch_sync",
        mock.as_method(),
    )
    return mock


# ── Integration-test async client ─────────────────────────────────────────────


@pytest_asyncio.fixture
async def async_client(
    db_engine: Any,
    redis_client: Any,
    mock_openai: Any,
    mock_tavily: Any,
    mock_weather: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[Any, None]:
    """AsyncClient pointed at the test app with all external services mocked.

    Brings up the FastAPI application with:
    - A test lifespan that skips Alembic migrations and uses an in-memory
      LangGraph checkpointer (``MemorySaver``).
    - The test DB engine injected via dependency override for ``get_db``.
    - The test Redis client already in place (via ``redis_client`` fixture).
    - OpenAI, Tavily, and Weather APIs replaced by configurable mocks.

    Skip behaviour: skips if ``TEST_DATABASE_URL`` is not set (inherited from
    ``db_engine``).

    Yields:
        ``httpx.AsyncClient`` configured to make requests to the test app.
    """
    from httpx import ASGITransport, AsyncClient

    import src.api.main as _main_module

    # Configure required settings env vars for this test
    db_url = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/test_db")
    env = {**_BASE_TEST_ENV, "DATABASE_URL": db_url}
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)

    from src.config.settings import get_settings

    get_settings.cache_clear()

    # ── Test lifespan: skip migrations, use MemorySaver checkpointer ─────────

    @asynccontextmanager
    async def _test_lifespan(app: Any) -> AsyncGenerator[None, None]:
        """Minimal lifespan for integration tests."""
        from langgraph.checkpoint.memory import MemorySaver

        import src.persistence.checkpointer as cp_module
        from src.agents.graph import get_graph
        from src.tools.registry import load_registry

        registry_path = Path(__file__).resolve().parent.parent / "config" / "tools.yaml"
        if registry_path.exists():
            load_registry(registry_path)

        # Use in-memory checkpointer so tests don't need the psycopg3/libpq chain
        get_graph.cache_clear()
        cp_module._checkpointer = MemorySaver()  # type: ignore[assignment]
        cp_module._setup_done = True
        app.state.ready = True

        yield

        # Teardown: clear singletons so the next test gets a fresh graph
        get_graph.cache_clear()
        cp_module._checkpointer = None
        cp_module._setup_done = False

    # Patch the module-level lifespan *before* create_app() references it
    monkeypatch.setattr(_main_module, "lifespan", _test_lifespan)

    from src.api.main import create_app

    app = create_app()

    # Override the DB session dependency so request handlers use the test engine
    from src.api.dependencies import get_db

    async def _test_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(db_engine, expire_on_commit=False) as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _test_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    # Clean up
    app.dependency_overrides.clear()
    get_settings.cache_clear()
