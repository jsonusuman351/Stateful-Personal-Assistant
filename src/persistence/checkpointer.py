"""LangGraph AsyncPostgresSaver wiring (T-012)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from src.config.settings import get_settings

if TYPE_CHECKING:
    # Import only for type-checkers; runtime import is lazy in _create_saver()
    # to avoid pulling in psycopg (requires libpq) at module-import time.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# ── Module-level singletons (populated by setup_checkpointer) ─────────────────

_checkpointer: AsyncPostgresSaver | None = None
_setup_done: bool = False
# Holds the async context manager returned by from_conn_string so it can be
# exited cleanly on shutdown via teardown_checkpointer().
_saver_cm: Any = None


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
    """Return the async context manager from ``AsyncPostgresSaver.from_conn_string``.

    Isolated into its own function so tests can patch it without triggering
    the psycopg / libpq import chain.  Callers must enter the returned context
    manager (via ``__aenter__``) to obtain the actual saver instance.
    Production code should call :func:`setup_checkpointer` instead.
    """
    # Lazy import: deferred here to avoid loading psycopg (requires libpq)
    # at module-import time.  Python caches the module after first import, so
    # subsequent calls incur only a dict lookup in sys.modules.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415

    return AsyncPostgresSaver.from_conn_string(conn_str)


def get_checkpointer() -> AsyncPostgresSaver:
    """Return the ``AsyncPostgresSaver`` singleton initialised by :func:`setup_checkpointer`.

    Raises:
        RuntimeError: if :func:`setup_checkpointer` has not been called yet.
    """
    if _checkpointer is None:
        raise RuntimeError(
            "Checkpointer has not been initialised. "
            "Call setup_checkpointer() during app startup before using get_checkpointer()."
        )
    return _checkpointer


async def setup_checkpointer() -> None:
    """Enter the ``AsyncPostgresSaver`` context manager and run ``setup()`` once.

    Enters the async context manager returned by :func:`_create_saver` to
    obtain the actual ``AsyncPostgresSaver`` instance, stores it in the
    module-level singleton, then calls ``setup()`` to create LangGraph's
    checkpoint tables.

    Idempotent: subsequent calls return immediately without re-running setup.
    Must be called during the app lifespan startup before any graph
    invocations (DESIGN §2.1, T-035 lifespan hook).
    """
    global _checkpointer, _setup_done, _saver_cm
    if _setup_done:
        return
    settings = get_settings()
    conn_str = _to_psycopg_url(settings.DATABASE_URL)
    cm = _create_saver(conn_str)
    # Enter the context manager to get the live AsyncPostgresSaver instance.
    # The context manager keeps the psycopg connection open for the app lifetime;
    # teardown_checkpointer() exits it on shutdown.
    _checkpointer = await cm.__aenter__()
    _saver_cm = cm
    await _checkpointer.setup()
    _setup_done = True


async def teardown_checkpointer() -> None:
    """Exit the ``AsyncPostgresSaver`` context manager and reset singletons.

    Must be called during app lifespan shutdown (after the last graph
    invocation) to close the psycopg connection cleanly (NFR-9).

    Safe to call even when :func:`setup_checkpointer` was never called.
    """
    global _checkpointer, _setup_done, _saver_cm
    if _saver_cm is not None:
        await _saver_cm.__aexit__(None, None, None)
    _checkpointer = None
    _setup_done = False
    _saver_cm = None


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
