# ── Stage 1: builder — install all Python deps into an isolated venv ──────────
FROM python:3.11-slim AS builder

WORKDIR /build

# gcc + libpq-dev: needed to compile asyncpg, argon2-cffi-bindings,
# hiredis, cryptography, and psycopg C extensions.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip --no-cache-dir \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime — minimal image; only the venv and app code ──────────────
FROM python:3.11-slim AS runtime

# libpq5: runtime shared library required by psycopg (used by the LangGraph
# PostgreSQL checkpointer).  asyncpg is libpq-free; hiredis is pure C.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

# Non-root user — no home directory needed; app files are world-readable.
RUN groupadd --gid 1001 --system app \
 && useradd --uid 1001 --gid 1001 --system --no-create-home app

WORKDIR /app

# Copy the fully-built venv from the builder stage.
# The runtime image gains no compiler, no pip cache, no build headers.
COPY --from=builder /opt/venv /opt/venv

# Copy only what the running application needs.
# Tests, docs, and tooling config are deliberately excluded.
COPY --chown=app:app src/       src/
COPY --chown=app:app alembic/   alembic/
COPY --chown=app:app config/    config/
COPY --chown=app:app alembic.ini alembic.ini

# Activate venv; PYTHONUNBUFFERED ensures structlog JSON lines are flushed
# immediately to stdout/stderr without buffering.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# --factory tells uvicorn to call create_app() to obtain the ASGI application.
# CLAUDE.md documents "uvicorn src.api.main:app" for bare-metal local dev, which
# requires app = create_app() at module level.  The --factory form avoids
# modifying main.py while keeping Docker self-contained.
CMD ["uvicorn", "src.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
