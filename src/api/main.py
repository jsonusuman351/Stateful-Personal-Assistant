"""App factory: create_app() and lifespan hook (T-035)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


async def _hitl_timeout_sweeper() -> None:
    """Background task: expire stale HITL approvals and write audit log entries.

    Runs every 60 seconds. On each sweep it:
    1. Bulk-updates HITLApprovals where expires_at < NOW() and used=FALSE → expired=TRUE.
    2. Inserts one HITLAuditLog row (decision='timeout') for each newly expired approval.

    asyncio.CancelledError is a BaseException (not Exception) so it propagates
    through the bare ``except Exception`` handler, letting the task cancel cleanly
    on SIGTERM without leaving dangling DB connections (NFR-9).
    """
    from src.persistence.database import get_db_session
    from src.persistence.repositories.hitl_repo import HITLRepository

    while True:
        try:
            async with get_db_session() as session:
                repo = HITLRepository(session)
                expired = await repo.expire_stale_approvals()
                for approval in expired:
                    await repo.insert_audit_log(
                        approval_id=approval.id,
                        user_id=approval.user_id,
                        conversation_id=approval.conversation_id,
                        tool_name=approval.tool_name,
                        decision="timeout",
                        request_ip=None,
                        decision_reason=None,
                    )
                if expired:
                    logger.info("HITL sweeper expired %d approval(s)", len(expired))
        except Exception as exc:
            logger.error("HITL timeout sweeper error: %s", exc)
        await asyncio.sleep(60)


async def _conversation_retention_job() -> None:
    """Background task: daily purge of low-engagement stale conversations.

    Deletes conversations where last_accessed < NOW() - 90 days AND
    access_count < 5, skipping any with open HITL approvals (FR-22,
    REVIEW-31). Uses FOR UPDATE SKIP LOCKED so live sessions are never
    blocked (FR-22).

    asyncio.CancelledError propagates through the bare ``except Exception``
    handler, allowing clean shutdown without dangling DB connections (NFR-9).
    """
    from src.persistence.database import get_db_session
    from src.persistence.repositories.conversation_repo import ConversationRepository

    while True:
        try:
            async with get_db_session() as session:
                repo = ConversationRepository(session)
                count = await repo.delete_stale_conversations()
                if count:
                    logger.info("Retention job purged %d conversation(s)", count)
        except Exception as exc:
            logger.error("Conversation retention job error: %s", exc)
        await asyncio.sleep(86400)  # daily


def _run_alembic_upgrade() -> bool:
    """Run ``alembic upgrade head`` in a subprocess.

    Returns:
        True if migrations applied (or were already up to date); False if the
        migration command failed or could not be launched (e.g. the database
        host is unreachable). Logs the failure at CRITICAL level either way —
        the caller decides whether a failure is fatal (see ``STARTUP_DB_REQUIRED``).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.critical("Alembic migration error: %s", exc)
        return False

    if proc.returncode != 0:
        logger.critical(
            "Alembic migration failed (exit %d): %s",
            proc.returncode,
            proc.stderr,
        )
        return False

    logger.info("Alembic migrations applied: %s", proc.stdout.strip() or "up to date")
    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """App startup and shutdown lifecycle hook.

    Startup order (NFR-10):
    1. Configure structured logging.
    2. Run ``alembic upgrade head``.
    3. Set up LangGraph PostgreSQL checkpointer.
    4. Load tool registry from ``config/tools.yaml``.
    5. Set ``app.state.ready = True``.

    Database resilience: steps 2–3 require the database. If either fails and
    ``STARTUP_DB_REQUIRED`` is True (default, NFR-10), the process exits
    non-zero and never serves traffic. If it is False, the app instead boots in
    DEGRADED mode (``app.state.db_available = False``): the process stays up,
    non-DB endpoints keep working, and DB-backed requests surface a clean 503
    (see the database error handlers in ``register_exception_handlers``). This
    lets a deployment survive a temporarily suspended database (e.g. a Render
    free-tier DB) instead of crash-looping.

    Shutdown (NFR-9):
    1. Dispose DB engine pool.
    2. Close Redis connection pool.
    3. Flush LangSmith callbacks (best-effort).
    """
    from src.config.settings import get_settings
    from src.persistence.checkpointer import setup_checkpointer, teardown_checkpointer
    from src.persistence.database import get_engine
    from src.persistence.redis_client import close_redis
    from src.tools.registry import load_registry
    from src.utils.logging import configure_logging

    configure_logging()
    settings = get_settings()
    app.state.db_available = True

    # 1. Alembic migrations — must succeed before serving DB-backed traffic.
    if not _run_alembic_upgrade():
        if settings.STARTUP_DB_REQUIRED:
            sys.exit(1)
        app.state.db_available = False
        logger.critical(
            "DEGRADED MODE: database unavailable at startup (migrations failed). "
            "Non-DB endpoints will serve; DB-backed requests return 503 until the "
            "database recovers and the app is restarted."
        )

    # 2. LangGraph checkpointer setup (idempotent) — also needs the database.
    if app.state.db_available:
        try:
            await setup_checkpointer()
        except Exception as exc:
            if settings.STARTUP_DB_REQUIRED:
                logger.critical("Checkpointer setup failed: %s", exc)
                raise
            app.state.db_available = False
            logger.critical("DEGRADED MODE: checkpointer setup failed: %s", exc)

    # 3. Tool registry (no database dependency)
    registry_path = Path(__file__).resolve().parent.parent.parent / "config" / "tools.yaml"
    load_registry(registry_path)

    app.state.ready = True

    # 4. Background tasks — both query the database every tick, so only start
    #    them when the database came up. They are skipped in degraded mode.
    sweeper: asyncio.Task[None] | None = None
    retention: asyncio.Task[None] | None = None
    if app.state.db_available:
        sweeper = asyncio.create_task(_hitl_timeout_sweeper(), name="hitl-timeout-sweeper")
        retention = asyncio.create_task(
            _conversation_retention_job(), name="conversation-retention"
        )

    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    # Cancel background tasks first so they release DB connections before
    # the pool is disposed (NFR-9).
    for task in (sweeper, retention):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        await get_engine().dispose()
    except Exception as exc:
        logger.warning("DB pool dispose error (non-fatal): %s", exc)

    try:
        await teardown_checkpointer()
    except Exception as exc:
        logger.warning("Checkpointer teardown error (non-fatal): %s", exc)

    await close_redis()

    if settings.LANGSMITH_API_KEY:
        try:
            from langchain_core.callbacks.manager import (  # type: ignore[attr-defined]
                get_callback_manager,
            )

            get_callback_manager().flush()
        except Exception as exc:
            logger.warning("LangSmith flush error (non-fatal): %s", exc)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance.

    Wires together:
    - Lifespan hook (migrations, checkpointer, registry, logging).
    - CORSMiddleware with origins from ``settings.CORS_ORIGINS``.
    - ``RequestMiddleware`` (request_id, null-byte guard, latency logging).
    - All API routers (auth, chat, sessions, tools, health).
    - Standardised exception handlers (T-042).

    Returns:
        Configured :class:`FastAPI` instance ready for ``uvicorn`` or testing.
    """
    from src.api.middleware import RequestMiddleware, register_exception_handlers
    from src.api.routers.auth import router as auth_router
    from src.api.routers.chat import router as chat_router
    from src.api.routers.health import router as health_router
    from src.api.routers.sessions import router as sessions_router
    from src.api.routers.tools import router as tools_router
    from src.config.settings import get_settings

    settings = get_settings()

    app = FastAPI(title="Stateful Personal Assistant", lifespan=lifespan)

    # CORS — never "*" in production (NFR-14).
    # CORS_ORIGINS is stored as str to avoid pydantic-settings json.loads on
    # list fields; parse it here from JSON array, CSV, or bare URL.
    raw = settings.CORS_ORIGINS.strip()
    if not raw:
        cors_origins: list[str] = []
    elif raw.startswith("["):
        cors_origins = json.loads(raw)
    else:
        cors_origins = [o.strip() for o in raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request logging, null-byte sanitisation, latency timing
    app.add_middleware(RequestMiddleware)

    # API routers
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(sessions_router)
    app.include_router(tools_router)
    app.include_router(health_router)

    # Standardised JSON error responses (T-042)
    register_exception_handlers(app)

    return app
