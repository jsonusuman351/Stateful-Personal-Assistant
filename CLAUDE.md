# CLAUDE.md

## Before Implementing Anything

Read these docs before writing or modifying any code:

- `docs/SPEC.md` — functional requirements and scope
- `docs/DESIGN.md` — architecture decisions
- `docs/TASKS.md` — current task list and priorities

## Navigating the Codebase

Read `graphify-out/GRAPH_REPORT.md` before tracing raw files.

**God nodes (high blast radius — touch carefully):**

| Node | Edges | Location |
|---|---|---|
| `Conversation` | 78 | `src/persistence/models/conversation.py` |
| `TokenPayload` | 76 | `src/api/dependencies.py` |
| `HITLApproval` | 59 | `src/persistence/models/hitl.py` |
| `HITLRepository` | 58 | `src/persistence/repositories/hitl_repo.py` |
| `get_settings()` | 56 | `src/config/settings.py` — called in 30+ modules, always via `@lru_cache` |
| `ConversationRepository` | 53 | `src/persistence/repositories/conversation_repo.py` |
| `AgentState` | 45 | `src/agents/state.py` |

**Key design constraints:**

- **Nodes must not emit SSE directly.** All SSE emission is in `agents/runner.py` via `astream_events()`. Emitting inside a node breaks checkpoint idempotency.
- **HITL atomicity.** `hitl_gate` writes `approval_id` to DB in a single committed transaction before returning. `approval_required` SSE is emitted *after* the commit.
- **Settings singleton.** Never create `Settings()` in request handlers — use `get_settings()` which is `@lru_cache`d.
- **Thread ID format.** Auth: `auth|{user_id}|{conversation_id}`. Guest: `guest|{sha256_hex}`.
- **Data isolation.** Every DB query on user-owned tables must be scoped by `user_id` from the validated JWT.

## Module Map

```
src/
  config/settings.py          — Pydantic BaseSettings, all env vars, @lru_cache
  auth/
    password.py               — Argon2id hash/verify, dummy_verify()
    jwt.py                    — create/decode access, refresh, guest tokens
    blacklist.py              — Redis JTI blacklist (O(1) per request)
    rate_limit.py             — per-IP + per-email sliding window, soft-lock
  persistence/
    database.py               — SQLAlchemy async engine + get_db_session()
    redis_client.py           — async Redis pool singleton
    checkpointer.py           — AsyncPostgresSaver wiring, build_thread_id()
    models/                   — ORM: user.py, conversation.py, message.py, hitl.py
    repositories/             — user_repo.py, conversation_repo.py, message_repo.py, hitl_repo.py
  agents/
    state.py                  — AgentState TypedDict, reducers
    graph.py                  — StateGraph construction + compile
    runner.py                 — run_turn(), resume_turn(), SSE event mapping
  graph/
    edges.py                  — route_after_router/hitl/tools/llm
    nodes/
      router.py               — intent parsing, tool selection
      hitl_gate.py            — atomic DB write + approval_id, no SSE
      tool_executor.py        — asyncio.gather, 3s timeout, 3-retry backoff
      llm.py                  — streaming, 2-retry primary, fallback chain
      error_handler.py        — user-facing error messages by error code
  tools/
    base.py                   — BaseTool Protocol (runtime_checkable)
    calculator.py             — simpleeval, no eval/exec
    weather.py                — asyncio.to_thread, is_sensitive=False
    web_search.py             — Tavily, relevance≥0.7, max 5 results, is_sensitive=True
    registry.py               — load_registry() from config/tools.yaml
  streaming/
    events.py                 — Pydantic SSE event models (6 types)
    emitter.py                — Redis replay buffer, dynamic TTL
    replay.py                 — get_events_after(), validate_stream_ownership()
  quota/limiter.py            — sliding-window Redis counters (4h/24h/7d)
  api/
    main.py                   — create_app(), lifespan (migrations → checkpointer → registry)
    middleware.py             — request_id, null-byte guard, latency log, error handlers
    dependencies.py           — get_current_user(), require_auth_user(), get_db(), get_redis()
    routers/
      auth.py                 — /auth/guest, /login, /refresh, /logout
      chat.py                 — POST /chat (202), GET /chat/stream (SSE)
      sessions.py             — CRUD + /approve + /model switch
      tools.py                — GET /tools
      health.py               — GET /health, GET /readiness
  utils/
    logging.py                — structlog JSON, bind_request_context()
    sanitise.py               — null-byte reject, NFC normalise, strip whitespace

tests/
  unit/                       — mocked dependencies, fast
  integration/                — real PostgreSQL + Redis, mocked OpenAI/Tavily/Weather
  fixtures/                   — mock_openai.py, mock_tavily.py, mock_weather.py
  conftest.py                 — async_client, db_session, redis_client, mock fixtures
```

## Architecture

**Graph topology (5 nodes):**
```
START → router ──(no tool)──────────────────────────→ llm → END
               ──(non-sensitive tool)──→ tool_executor → llm → END
               ──(sensitive tool)──→ hitl_gate* → tool_executor → llm → END
               └──(any error)──→ error_handler → END

* graph suspends at hitl_gate (interrupt_after), resumes on POST /approve
```

**State schema** (`AgentState`):
- `messages` — `add_messages` reducer (append)
- `tool_calls` / `tool_results` — replace-on-write reducer (reset each turn)
- `pending_approval` — `ApprovalState | None`
- `error` — `ErrorState | None`
- `thread_id`, `active_model`

**Persistence:**
- Auth users: LangGraph checkpoints in PostgreSQL via `AsyncPostgresSaver`
- Guests: state in Redis under `guest:{session_id_hash}:state` (24h TTL)
- Thread ID: `auth|{user_id}|{conversation_id}` or `guest|{sha256_hex}`

**Redis key namespaces:**
- `stream:{stream_id}:events` — SSE replay buffer (5min / 15min TTL)
- `stream:{stream_id}:owner` — ownership validation
- `blacklist:jti:{jti}` — access token blacklist
- `quota:{user_id}:{window}` — sliding-window counters
- `guest_quota:{ip_hash}:{window}` — guest per-IP quota
- `auth:rate:{ip_hash}:{window}` — login rate limit
- `auth:lock:{email_hash}` — brute-force soft-lock
- `hitl:lock:{approval_id}` — distributed HITL lock (SET NX, 120s TTL)

**SSE flow (two-step):**
1. `POST /chat` → validates quota → returns 202 `{message_id, stream_url}`
2. `GET /chat/stream?stream_id=X` → opens SSE → calls `run_turn()` → events flow

**Security:**
- Passwords: Argon2id (`time_cost=2, memory_cost=65536, parallelism=2`)
- JWTs: HS256, all standard claims (`iss`, `aud`, `exp`, `nbf`, `jti`), PyJWT (not python-jose — CVE-2024-33664)
- Input: null-byte reject → NFC normalise → whitespace strip (middleware + Pydantic validators)

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10 (pyproject ≥3.10; design targets 3.11) |
| Agent framework | LangGraph |
| LLM / chain tooling | LangChain + langchain-openai |
| API layer | FastAPI (async throughout) |
| State persistence | PostgreSQL + SQLAlchemy asyncpg |
| Caching / ephemeral state | Redis (redis-py async) |
| Migrations | Alembic |
| Deployment | Docker / Docker Compose → Render |

## Commands

```bash
pip install -r requirements.txt      # install deps
docker compose up --build            # full dev env (API + DB + Redis)
uvicorn src.api.main:app --reload --port 8000  # API only (DB+Redis must be up)
pytest                               # all tests
pytest tests/unit/                   # unit tests only
pytest tests/integration/            # integration tests (needs TEST_DATABASE_URL + REDIS_URL)
pytest tests/test_foo.py::test_bar   # single test
ruff check .                         # lint
mypy .                               # type-check (strict)
alembic upgrade head                 # run migrations
alembic downgrade -1                 # rollback one
```

## Coding Standards

Mandatory:

- **Type hints** — every function/method signature, parameters and return type
- **Docstrings** — every public function, method, class; one-liner fine for simple cases
- **Tests** — all business logic has pytest tests; tests mirror `src/` layout in `tests/`
- **No raw SQL** — SQLAlchemy ORM or parameterised statements only; no f-string SQL
- **No eval/exec** — especially in CalculatorTool; `simpleeval` only
