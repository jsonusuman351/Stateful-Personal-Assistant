"""LangGraph AsyncPostgresSaver wiring (T-012)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from src.config.settings import get_settings

if TYPE_CHECKING:
    # Import only for type-checkers; runtime import is lazy in _create_saver()
    # to avoid pulling in psycopg (requires libpq) at module-import time.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# ── Module-level lazy singletons ──────────────────────────────────────────────
# Initialised on first call so that import does not require a live database.

_checkpointer: AsyncPostgresSaver | None = None
_setup_done: bool = False


def _to_psycopg_url(database_url: str) -> str:
    """Strip the SQLAlchemy driver prefix so psycopg3 can parse the URL.

    ``AsyncPostgresSaver`` uses psycopg3 internally and expects a plain
    ``postgresql://`` URL, not the ``postgresql+asyncpg://`` form that
    SQLAlchemy uses for the asyncpg driver.

    If the URL does not contain the prefix (e.g. already in standard form),
    it is returned unchanged.
    """
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _create_saver(conn_str: str) -> Any:
    """Instantiate ``AsyncPostgresSaver`` from a psycopg-compatible connection string.

    Isolated into its own function so tests can patch it without triggering
    the psycopg / libpq import chain.  Production code should call
    :func:`get_checkpointer` instead.
    """
    # Lazy import: deferred here to avoid loading psycopg (requires libpq)
    # at module-import time.  Python caches the module after first import, so
    # subsequent calls incur only a dict lookup in sys.modules.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415

    return AsyncPostgresSaver.from_conn_string(conn_str)


def get_checkpointer() -> AsyncPostgresSaver:
    """Return the module-level ``AsyncPostgresSaver`` singleton.

    Creates the instance on first call using ``settings.DATABASE_URL``.
    The URL is converted from SQLAlchemy's asyncpg dialect to the plain
    PostgreSQL form expected by psycopg3 (see :func:`_to_psycopg_url`).

    Call :func:`setup_checkpointer` during app startup before the first graph
    invocation to ensure LangGraph's checkpoint tables exist (DESIGN §2.1).
    """
    global _checkpointer
    if _checkpointer is None:
        settings = get_settings()
        conn_str = _to_psycopg_url(settings.DATABASE_URL)
        _checkpointer = _create_saver(conn_str)
    return _checkpointer


async def setup_checkpointer() -> None:
    """Run ``checkpointer.setup()`` exactly once to create LangGraph tables.

    Idempotent: subsequent calls return immediately without re-running setup.
    Must be called during the app lifespan startup before any graph
    invocations (DESIGN §2.1, T-035 lifespan hook).
    """
    global _setup_done
    if _setup_done:
        return
    await get_checkpointer().setup()
    _setup_done = True


def build_thread_id(user_id: str, conversation_id: str) -> str:
    """Build a LangGraph thread ID for an authenticated user session.

    Format: ``auth|{user_id}|{conversation_id}``

    The ``|`` delimiter is unambiguous with UUID v4 values, which contain
    only hex digits and hyphens and never the pipe character (DESIGN §2.1).

    Args:
        user_id: The authenticated user's UUID string.
        conversation_id: The conversation's UUID string.

    Returns:
        A pipe-delimited thread ID string prefixed with ``auth|``.
    """
    return f"auth|{user_id}|{conversation_id}"


def build_guest_thread_id(ip: str, user_agent: str) -> str:
    """Build a LangGraph thread ID for a guest session using a SHA-256 fingerprint.

    Format: ``guest|{sha256_hex}``

    The SHA-256 digest is computed over the concatenation of the client IP
    address and User-Agent string (DESIGN §2.1).  This is deterministic: the
    same IP+UA combination always produces the same thread ID, enabling
    LangGraph to resume guest state across reconnections within a session.

    Args:
        ip: The client's IP address as a string.
        user_agent: The client's User-Agent header value.

    Returns:
        A thread ID string prefixed with ``guest|`` followed by a 64-character
        lowercase hex SHA-256 digest.
    """
    fingerprint = hashlib.sha256(f"{ip}{user_agent}".encode()).hexdigest()
    return f"guest|{fingerprint}"
