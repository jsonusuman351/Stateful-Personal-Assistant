"""Shared test fixtures for pytest (T-047 — test infrastructure).

This module provides fixtures used across all test modules: async client, mock DB,
mock Redis, mock external APIs, and other shared test utilities.

For unit tests of the repository layer, a real PostgreSQL instance is recommended.
Tests can connect to a test database via the TEST_DATABASE_URL environment variable.
If not set, tests using db_session will be skipped.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.persistence.models.base import Base


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "requires_db: mark test as requiring a database connection"
    )


@pytest_asyncio.fixture
async def db_engine():
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
            "Example: postgresql+asyncpg://user:password@localhost/test_db"
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
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional AsyncSession for each test.

    Rolls back after each test to ensure isolation.
    """
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            yield session
            # Rollback after test for isolation
            await session.rollback()
