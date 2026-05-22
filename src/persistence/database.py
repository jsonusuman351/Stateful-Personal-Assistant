"""SQLAlchemy async engine, session factory, and connection-pool configuration (T-003)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config.settings import Settings, get_settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    """Create and return a configured async SQLAlchemy engine using asyncpg.

    Pool sizing follows NFR-7:
    - ``pool_size`` = ``DB_POOL_MIN`` — connections kept permanently open.
    - ``max_overflow`` = ``DB_POOL_MAX - DB_POOL_MIN`` — extra connections allowed
      beyond the permanent pool before requests start blocking.

    ``pool_pre_ping=True`` tests each borrowed connection with a lightweight
    roundtrip before handing it to the caller, recovering silently from stale
    connections after network interruptions.

    The engine is not connected at creation time; the first ``get_db_session``
    call triggers pool initialisation.
    """
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_MIN,
        max_overflow=settings.DB_POOL_MAX - settings.DB_POOL_MIN,
        pool_pre_ping=True,
    )


# ── Module-level lazy singletons ──────────────────────────────────────────────
# Initialised on first call so that import does not require live env vars
# (tests can patch these globals before the first call).

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the module-level engine singleton, creating it on first call.

    Reads settings from the cached ``get_settings()`` singleton, so the
    environment must be fully configured before the first call.
    """
    global _engine
    if _engine is None:
        _engine = create_db_engine(get_settings())
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the module-level session factory singleton.

    Separated from ``get_engine`` so tests can patch this function and inject
    a mock factory without touching the engine.
    """
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a transactional ``AsyncSession``.

    Commits the session on clean exit; rolls back and re-raises on any
    exception. The underlying SQLAlchemy session is closed automatically
    when the context exits, returning the connection to the pool.

    Usage::

        async with get_db_session() as session:
            session.add(some_model)
            # commit is automatic on clean exit

    Raises:
        Exception: Any exception raised inside the block is re-raised after
            the rollback, so callers always see the original error.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
