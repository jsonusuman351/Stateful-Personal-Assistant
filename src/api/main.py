"""App factory: create_app() and lifespan hook (T-035)."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """App startup and shutdown lifecycle hook.

    Startup order (NFR-10):
    1. Configure structured logging.
    2. Run ``alembic upgrade head`` — ``sys.exit(1)`` on failure.
    3. Set up LangGraph PostgreSQL checkpointer.
    4. Load tool registry from ``config/tools.yaml``.
    5. Set ``app.state.ready = True``.

    Shutdown (NFR-9):
    1. Dispose DB engine pool.
    2. Close Redis connection pool.
    3. Flush LangSmith callbacks (best-effort).
    """
    from src.config.settings import get_settings
    from src.persistence.checkpointer import setup_checkpointer
    from src.persistence.database import get_engine
    from src.persistence.redis_client import close_redis
    from src.tools.registry import load_registry
    from src.utils.logging import configure_logging

    configure_logging()
    settings = get_settings()

    # 1. Alembic migrations — process must not serve traffic if they fail
    try:
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            logger.critical(
                "Alembic migration failed (exit %d): %s",
                proc.returncode,
                proc.stderr,
            )
            sys.exit(1)
        logger.info("Alembic migrations applied: %s", proc.stdout.strip() or "up to date")
    except SystemExit:
        raise
    except Exception as exc:
        logger.critical("Alembic migration error: %s", exc)
        sys.exit(1)

    # 2. LangGraph checkpointer setup (idempotent)
    await setup_checkpointer()

    # 3. Tool registry
    registry_path = (
        Path(__file__).resolve().parent.parent.parent / "config" / "tools.yaml"
    )
    load_registry(registry_path)

    app.state.ready = True

    # 4. Background tasks — started after ready so they can use DB/Redis
    sweeper = asyncio.create_task(
        _hitl_timeout_sweeper(), name="hitl-timeout-sweeper"
    )

    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    # Cancel background tasks first so they release DB connections before
    # the pool is disposed (NFR-9).
    sweeper.cancel()
    try:
        await sweeper
    except asyncio.CancelledError:
        pass

    try:
        await get_engine().dispose()
    except Exception as exc:
        logger.warning("DB pool dispose error (non-fatal): %s", exc)

    await close_redis()

    if settings.LANGSMITH_API_KEY:
        try:
            from langchain_core.callbacks.manager import get_callback_manager

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

    # CORS — never "*" in production (NFR-14)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
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
