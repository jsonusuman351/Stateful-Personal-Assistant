"""Health and readiness check endpoints (T-041, DESIGN §6.4, §6.5)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> dict[str, str]:
    """Liveness probe: always 200 if the process is alive."""
    return {"status": "ok"}


@router.get("/readiness")
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe: 200 only after migrations and when DB + Redis are healthy.

    Checks performed:
    - ``app.state.ready``: set by lifespan after ``alembic upgrade head``.
    - postgres: executes ``SELECT 1``.
    - redis: sends ``PING``.
    - alembic_revision: current DB revision equals head.
    """
    if not getattr(request.app.state, "ready", False):
        return JSONResponse(
            status_code=503,
            content={
                "checks": {
                    "postgres": "not_checked",
                    "redis": "not_checked",
                    "alembic_revision": "not_checked",
                }
            },
        )

    checks: dict[str, Any] = {}

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    try:
        from src.persistence.database import get_engine

        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"

    # ── Redis ─────────────────────────────────────────────────────────────────
    try:
        from src.persistence.redis_client import get_redis

        await get_redis().ping()  # type: ignore[misc]
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    # ── Alembic revision ──────────────────────────────────────────────────────
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        from src.persistence.database import get_engine

        cfg = AlembicConfig("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        head_rev = script.get_current_head()

        async with get_engine().connect() as conn:
            result = await conn.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.fetchone()
        current_rev = row[0] if row else None

        checks["alembic_revision"] = (
            "ok"
            if current_rev == head_rev
            else f"stale (current={current_rev}, head={head_rev})"
        )
    except Exception as exc:
        checks["alembic_revision"] = f"error: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"checks": checks},
    )
