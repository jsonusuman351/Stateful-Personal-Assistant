# Stateful Personal Assistant

A production-grade, multi-tool AI agent built on LangGraph and exposed via a FastAPI REST and SSE API. It orchestrates weather lookup, arithmetic calculation, and web search through a structured five-node agent graph with parallel tool execution, human-in-the-loop (HITL) approval for sensitive operations, and real-time streaming with mid-stream reconnection. Conversation state is persisted per authenticated user in PostgreSQL; guest sessions use ephemeral Redis state.

---

## Architecture

### System Context

```mermaid
C4Context
    title System Context — Stateful Personal Assistant
    Person(user, "User", "Guest or authenticated user via web browser")
    System(assistant, "Stateful Personal Assistant", "Multi-tool AI agent with HITL, streaming, and persistent state")
    System_Ext(openai, "OpenAI API", "Primary LLM (gpt-4o-mini)")
    System_Ext(groq, "Groq / Ollama", "Fallback LLMs")
    System_Ext(tavily, "Tavily API", "Web search provider")
    System_Ext(weather, "Weather API", "Current weather data")
    System_Ext(langsmith, "LangSmith", "Tracing and observability")
    System_Ext(render, "Render", "Cloud deployment platform")

    Rel(user, assistant, "Sends queries, approves HITL actions", "HTTPS / SSE")
    Rel(assistant, openai, "LLM inference", "HTTPS")
    Rel(assistant, groq, "Fallback LLM inference", "HTTPS")
    Rel(assistant, tavily, "Web search queries", "HTTPS")
    Rel(assistant, weather, "Weather lookups", "HTTPS")
    Rel(assistant, langsmith, "Execution traces", "HTTPS")
    Rel(render, assistant, "Hosts", "")
```

### LangGraph Agent Graph

The agent is a LangGraph `StateGraph` with exactly five nodes connected by conditional edges:

```mermaid
stateDiagram-v2
    [*] --> router : user message received
    router --> hitl_gate : sensitive tool selected
    router --> tool_executor : non-sensitive tools only
    router --> llm : no tools needed
    hitl_gate --> tool_executor : user approved
    hitl_gate --> error_handler : user denied / timeout
    tool_executor --> llm : at least one tool succeeded
    tool_executor --> error_handler : all tools failed after retries
    llm --> [*] : response synthesised
    llm --> error_handler : LLM failed after retries + fallbacks
    error_handler --> [*] : user-facing error emitted
```

| Node | Responsibility |
|---|---|
| `router` | Parses user intent, selects tools via LLM call |
| `hitl_gate` | Writes approval to DB atomically, suspends graph via `interrupt_after` |
| `tool_executor` | Runs all tools concurrently with `asyncio.gather`, 3-retry backoff per tool |
| `llm` | Synthesises response from tool results with 2-retry + fallback chain |
| `error_handler` | Emits user-facing messages for final failures; no retries here |

### Component Interaction

```mermaid
graph TD
    Client["Browser / API Client"]
    FastAPI["FastAPI API Layer"]
    Auth["Auth Middleware\n(JWT validation, rate limit)"]
    Graph["LangGraph StateGraph"]
    Router["router node"]
    ToolExec["tool_executor node"]
    HITL["hitl_gate node"]
    LLM["llm node"]
    ErrHandler["error_handler node"]
    SSE["SSE Emitter\n(Redis replay buffer)"]
    Checkpointer["PostgreSQL Checkpointer"]
    PG["PostgreSQL"]
    Redis["Redis"]
    OpenAI["OpenAI API"]
    Fallback["Fallback Models\n(Groq / Ollama)"]
    Tools["Tool Registry\n(weather, calc, search)"]

    Client -->|"POST /chat\nGET /chat/stream"| FastAPI
    FastAPI --> Auth
    Auth --> Graph
    Graph --> Router
    Router --> HITL
    Router --> ToolExec
    HITL --> Checkpointer
    HITL --> SSE
    ToolExec --> Tools
    ToolExec --> LLM
    ToolExec --> ErrHandler
    LLM --> OpenAI
    LLM --> Fallback
    LLM --> SSE
    ErrHandler --> SSE
    SSE --> Redis
    SSE --> Client
    Checkpointer --> PG
```

### Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Agent framework | LangGraph |
| LLM / chain tooling | LangChain |
| API layer | FastAPI + Uvicorn |
| State persistence | PostgreSQL 16 (AsyncPostgresSaver) |
| Caching / ephemeral state | Redis 7 |
| Password hashing | Argon2id (argon2-cffi) |
| Safe expression eval | simpleeval |
| Observability | LangSmith + structlog |
| Deployment | Docker / Docker Compose / Render |
| CI/CD | GitHub Actions |

---

## Quickstart

### Prerequisites

- Docker and Docker Compose
- API keys: OpenAI (required), Tavily (optional, for web search), LangSmith (optional, for tracing)

### 1. Clone and configure

```bash
git clone https://github.com/jsonusuman351/Stateful-Personal-Assistant.git
cd Stateful-Personal-Assistant

# Copy the env template and fill in your values
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
OPENAI_API_KEY=sk-...
JWT_SECRET=$(python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")
JWT_ISSUER=stateful-assistant
JWT_AUDIENCE=stateful-assistant-users
```

### 2. Start the full stack

```bash
docker compose up --build
```

This starts PostgreSQL 16, Redis 7, and the FastAPI app. Alembic migrations run automatically on startup. The API is ready when the healthcheck passes:

```bash
# Confirm the stack is healthy
curl http://localhost:8000/readiness
# {"status": "ready", "checks": {"postgres": "ok", "redis": "ok", "alembic_revision": "ok"}}
```

### 3. Try it out

```bash
# Get a guest token (no account required)
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/guest | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Submit a message
RESPONSE=$(curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What is 2 ** 10 + 5 * 3?"}')
echo $RESPONSE
# {"message_id": "...", "session_id": "...", "stream_url": "/api/v1/chat/stream?stream_id=..."}

# Open the SSE stream
STREAM_URL=$(echo $RESPONSE | python -c "import sys,json; print(json.load(sys.stdin)['stream_url'])")
curl -N -H "Authorization: Bearer $TOKEN" "http://localhost:8000$STREAM_URL"
```

### 4. Local development (without Docker)

Requires PostgreSQL 16 and Redis 7 running locally (matching `.env` connection strings).

```bash
# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run Alembic migrations
alembic upgrade head

# Start the API server with auto-reload
uvicorn src.api.main:app --reload --port 8000
```

---

## Running Tests

```bash
# Run the full test suite
pytest

# Run with coverage (must reach ≥ 80%)
pytest --cov=src --cov-report=term-missing

# Run only unit tests (no DB/Redis required)
pytest tests/unit/

# Run only integration tests (requires TEST_DATABASE_URL and TEST_REDIS_URL)
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/assistant_test `# pragma: allowlist secret` \
pytest tests/integration/

# Run a single test
pytest tests/unit/test_graph_edges.py::test_route_after_router_sensitive_tool

# Lint and type-check
ruff check .
mypy .
```

Integration tests that hit real infrastructure require:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/assistant_test  # pragma: allowlist secret
```

All unit tests run without any external services using `AsyncMock` fixtures.

---

## Folder Structure

```
Stateful-Personal-Assistant/
├── src/
│   ├── api/                 # HTTP layer
│   │   ├── main.py          # App factory: create_app(), lifespan hook
│   │   ├── dependencies.py  # FastAPI Depends(): get_current_user, get_db, get_redis
│   │   ├── middleware.py    # Request logging, input sanitisation, timing
│   │   └── routers/
│   │       ├── auth.py      # /auth/login, /refresh, /logout, /guest
│   │       ├── chat.py      # POST /chat, GET /chat/stream (SSE)
│   │       ├── sessions.py  # /sessions CRUD, /approve, /model
│   │       ├── tools.py     # GET /tools registry
│   │       └── health.py    # GET /health, GET /readiness
│   │
│   ├── agents/              # LangGraph wiring
│   │   ├── state.py         # AgentState TypedDict and sub-TypedDicts
│   │   ├── graph.py         # StateGraph construction, compile, checkpointer
│   │   └── runner.py        # run_turn() and resume_turn() — SSE emission via astream_events
│   │
│   ├── graph/               # Node and edge implementations
│   │   ├── edges.py         # Conditional edge functions (route_after_*)
│   │   └── nodes/
│   │       ├── router.py        # Intent parsing, tool selection
│   │       ├── tool_executor.py # Concurrent dispatch with per-tool retry
│   │       ├── hitl_gate.py     # Atomic DB write + interrupt_after suspension
│   │       ├── llm.py           # LLM synthesis with fallback chain
│   │       └── error_handler.py # User-facing error emission
│   │
│   ├── tools/               # Tool implementations and registry
│   │   ├── base.py          # BaseTool Protocol (runtime_checkable)
│   │   ├── registry.py      # YAML-driven loader; validates at startup
│   │   ├── weather.py       # WeatherTool (is_sensitive=False)
│   │   ├── calculator.py    # CalculatorTool using simpleeval (no eval/exec)
│   │   └── web_search.py    # WebSearchTool via Tavily (is_sensitive=True → HITL)
│   │
│   ├── persistence/         # Data access layer — no business logic
│   │   ├── database.py      # SQLAlchemy async engine, session factory
│   │   ├── redis_client.py  # Redis async pool singleton
│   │   ├── checkpointer.py  # AsyncPostgresSaver wiring
│   │   ├── models/          # ORM table definitions
│   │   └── repositories/    # One repo per aggregate (user, conversation, message, hitl)
│   │
│   ├── auth/                # Authentication
│   │   ├── jwt.py           # JWT creation and claim validation
│   │   ├── password.py      # Argon2id hash/verify
│   │   ├── blacklist.py     # Access token jti blacklist (Redis)
│   │   └── rate_limit.py    # Login rate limiter (per-IP, per-email, lockout)
│   │
│   ├── streaming/           # SSE emission and replay
│   │   ├── events.py        # Pydantic models for each SSE event type
│   │   ├── emitter.py       # Event formatting and Redis buffer push
│   │   └── replay.py        # Replay buffer reader with TTL management
│   │
│   ├── quota/               # Token and request quota enforcement
│   │   └── limiter.py       # Sliding-window counters (per-user + per-IP) in Redis
│   │
│   ├── config/
│   │   └── settings.py      # Pydantic BaseSettings; validates all env vars at startup
│   │
│   └── utils/
│       ├── logging.py       # structlog processor chain, request_id injection
│       └── sanitise.py      # Null-byte rejection, NFC normalisation, whitespace strip
│
├── tests/
│   ├── unit/                # Fast tests; no external services needed
│   ├── integration/         # Require TEST_DATABASE_URL (real Postgres + Redis)
│   └── fixtures/            # MockOpenAI, MockTavily, MockWeather
│
├── alembic/                 # Database migrations (each has upgrade + downgrade)
├── config/
│   └── tools.yaml           # Tool registry: add new tools here without touching agent code
├── docs/
│   ├── SPEC.md              # Functional and non-functional requirements
│   ├── DESIGN.md            # Architecture decisions and system design
│   └── TASKS.md             # Implementation task list
├── graphify-out/
│   └── GRAPH_REPORT.md      # Auto-generated knowledge graph — read this before tracing raw files
├── .env.example             # Documents every environment variable
├── Dockerfile               # Multi-stage production build
└── docker-compose.yml       # Local dev: API + PostgreSQL + Redis
```

**Key seam:** `agents/runner.py` is the only place that calls `sse_emitter.emit()`. Nodes are pure state-transform functions — they never write to the SSE channel directly. This keeps nodes idempotent with respect to LangGraph checkpoint replay.

---

## API Reference

**Base URL:** `http://localhost:8000/api/v1`

All error responses share this schema:

```json
{
  "error": {
    "code": "SCREAMING_SNAKE_CASE",
    "message": "Human-readable description",
    "retryable": true,
    "retry_after_seconds": 30
  }
}
```

### Authentication

#### `POST /auth/guest` — Get a guest token

No body required. Returns a 24-hour JWT with `mode: "guest"`. No database row is created.

```bash
curl -X POST http://localhost:8000/api/v1/auth/guest
```

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 86400,
  "mode": "guest"
}
```

#### `POST /auth/login` — Authenticated login

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "secret"}' # pragma: allowlist secret
```

```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "access_token_expires_in": 3600
}
```

Returns `401` for invalid credentials (identical body for wrong email and wrong password — no enumeration). Returns `429` after 10 failed attempts per IP or 5 per email (15-minute lockout).

#### `POST /auth/refresh` — Rotate refresh token

```bash
curl -X POST http://localhost:8000/api/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "eyJ..."}'
```

#### `POST /auth/logout` — Invalidate access token

```bash
curl -X POST http://localhost:8000/api/v1/auth/logout \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "eyJ..."}'
```

Returns `204`. The access token's `jti` is added to the Redis blacklist for its remaining lifetime.

---

### Chat (two-step SSE handshake)

Chat follows a two-step pattern: submit → stream. This decouples message submission from stream consumption and enables robust reconnection without re-sending the message.

#### Step 1: `POST /chat` — Submit a message

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the weather in Tokyo?", "session_id": null}'
```

```json
{
  "message_id": "uuid-v4",
  "session_id": "uuid-v4",
  "stream_url": "/api/v1/chat/stream?stream_id=uuid-v4"
}
```

`session_id: null` creates a new session. Pass an existing UUID to continue a prior conversation.

Returns `429` when quota is exceeded: `{"quota_type": "4h_requests", "current": 20, "limit": 20, "reset_at": "ISO8601"}`.

#### Step 2: `GET /chat/stream?stream_id={uuid}` — Open SSE stream

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/chat/stream?stream_id=<uuid>"
```

```
id: 1
event: thinking
data: {"node": "router", "elapsed_ms": 42}

id: 2
event: thinking
data: {"node": "tool_executor", "elapsed_ms": 150}

id: 3
event: tool_result
data: {"tool": "weather", "result": {"temperature": "22°C", "condition": "Sunny"}}

id: 4
event: thinking
data: {"node": "llm", "elapsed_ms": 680}

id: 5
event: token
data: {"content": "The current weather in Tokyo is sunny and 22°C."}

id: 6
event: done
data: {"message_id": "uuid-v4"}
```

**SSE event types:**

| Event | Payload | When |
|---|---|---|
| `thinking` | `{node: str, elapsed_ms: int}` | Entry of each graph node |
| `token` | `{content: str}` | Each incremental LLM output token |
| `tool_result` | `{tool: str, result: any}` | After each tool completes |
| `approval_required` | `{approval_id: str, tool: str, description: str}` | When HITL gate suspends |
| `error` | `{code: str, message: str, retryable: bool}` | Any failure |
| `done` | `{message_id: str}` | Turn complete |

**Reconnection:** Supply `Last-Event-ID: <int>` to replay missed events without re-executing graph nodes. Events are buffered in Redis for 5 minutes (15 minutes for active HITL flows). Returns `403` if the stream belongs to a different user, `410` if the buffer has expired.

---

### Sessions

#### `GET /sessions` — List sessions (authenticated users only)

```bash
curl "http://localhost:8000/api/v1/sessions?limit=20" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "items": [
    {
      "session_id": "uuid-v4",
      "title": "What is the weather in Tokyo?",
      "created_at": "2026-05-20T10:00:00Z",
      "last_accessed": "2026-05-20T11:30:00Z",
      "message_count": 6
    }
  ],
  "next_cursor": "opaque-string-or-null"
}
```

#### `GET /sessions/{id}/messages` — Message history

```bash
curl "http://localhost:8000/api/v1/sessions/$SESSION_ID/messages" \
  -H "Authorization: Bearer $TOKEN"
```

#### `DELETE /sessions/{id}` — Delete a session

```bash
curl -X DELETE "http://localhost:8000/api/v1/sessions/$SESSION_ID" \
  -H "Authorization: Bearer $TOKEN"
```

#### `POST /sessions/{id}/approve` — Approve or deny a HITL action

```bash
curl -X POST "http://localhost:8000/api/v1/sessions/$SESSION_ID/approve" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approval_id": "uuid-v4", "decision": "approve"}'
```

Returns `409` if another approval request is already in progress (concurrent lock), `410` if the `approval_id` has been used or expired.

---

### Tools

#### `GET /tools` — List registered tools (all users)

```bash
curl http://localhost:8000/api/v1/tools -H "Authorization: Bearer $TOKEN"
```

```json
{
  "tools": [
    {"name": "weather",    "description": "Get current weather for a location.", "is_sensitive": false},
    {"name": "calculator", "description": "Evaluate a mathematical expression.",  "is_sensitive": false},
    {"name": "web_search", "description": "Search the web for current information.", "is_sensitive": true}
  ]
}
```

`is_sensitive: true` means the tool requires HITL approval before execution.

---

### Health

```bash
curl http://localhost:8000/health      # liveness — always 200 if process is alive
curl http://localhost:8000/readiness   # readiness — checks Postgres, Redis, Alembic revision
```

---

## Environment Variables

Copy `.env.example` to `.env` and populate before running.

### Required

| Variable | Description | Example |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API key | `sk-...` |
| `DATABASE_URL` | asyncpg connection string | `postgresql+asyncpg://<user>:<pass>@localhost:5432/assistant` |
| `REDIS_URL` | Redis connection string | `redis://localhost:6379/0` |
| `JWT_SECRET` | HS256 signing key, ≥32 bytes base64 | See generator below |
| `JWT_ISSUER` | JWT `iss` claim | `stateful-assistant` |
| `JWT_AUDIENCE` | JWT `aud` claim | `stateful-assistant-users` |

Generate a secure `JWT_SECRET`:

```bash
python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

### Optional

| Variable | Description | Default |
|---|---|---|
| `TAVILY_API_KEY` | Tavily web search API key | — (web_search tool returns config error if absent) |
| `WEATHER_API_KEY` | Weather API key | — (weather tool returns config error if absent) |
| `LANGSMITH_API_KEY` | LangSmith tracing key | — (tracing disabled if absent) |
| `OPENAI_DEFAULT_MODEL` | Primary LLM model name | `gpt-4o-mini` |
| `FALLBACK_MODELS` | JSON array of fallback model IDs | `""` (disabled) |
| `AUTH_LOCKOUT_MINUTES` | Login brute-force lockout duration | `15` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `CORS_ORIGINS` | JSON array of allowed CORS origins | `["http://localhost:3000"]` |

### Connection pools (NFR-7)

| Variable | Description | Default |
|---|---|---|
| `DB_POOL_MIN` | PostgreSQL pool minimum connections | `2` |
| `DB_POOL_MAX` | PostgreSQL pool maximum connections | `10` |
| `REDIS_POOL_MIN` | Redis pool minimum connections | `2` |
| `REDIS_POOL_MAX` | Redis pool maximum connections | `10` |

### Quota windows (FR-26)

| Variable | Description | Default |
|---|---|---|
| `QUOTA_4H_REQUESTS` | Max OpenAI requests per 4-hour window | `20` |
| `QUOTA_24H_REQUESTS` | Max OpenAI requests per 24-hour window | `100` |
| `QUOTA_7D_REQUESTS` | Max OpenAI requests per 7-day window | `500` |
| `QUOTA_4H_TOKENS` | Max OpenAI tokens per 4-hour window | `50000` |
| `QUOTA_24H_TOKENS` | Max OpenAI tokens per 24-hour window | `200000` |
| `QUOTA_7D_TOKENS` | Max OpenAI tokens per 7-day window | `1000000` |

---

## Deployment

The project deploys to [Render](https://render.com) via GitHub Actions CI/CD.

### CI pipeline (`.github/workflows/ci.yml`)

Every push to `main` runs sequentially:

1. **Lint** — `ruff check .`
2. **Type-check** — `mypy --strict`
3. **Secrets scan** — `detect-secrets`
4. **Tests** — `pytest --cov` (coverage gate ≥ 80%)
5. **Docker build + healthcheck** — builds the multi-stage image and runs `/readiness`
6. **Migrations** — `alembic upgrade head && alembic downgrade -1`

### Deploy pipeline (`.github/workflows/deploy.yml`)

Triggered only after all CI jobs pass:

1. Builds and pushes the Docker image to GHCR
2. Deploys to Render via the deploy hook

### Adding a new tool

1. Create `src/tools/your_tool.py` implementing the `BaseTool` Protocol.
2. Add an entry to `config/tools.yaml`:
   ```yaml
   - module: src.tools.your_tool
     class: YourTool
   ```
3. Write unit tests in `tests/unit/test_tools.py`.

No changes to the agent graph, router, or any existing tool are required.

---

## Troubleshooting

### `ValidationError` on startup — missing environment variable

```
pydantic_settings.env_settings.SettingsError: ... field required
```

Check that `.env` is present and all required variables are set. The app exits before accepting traffic if any required var is missing.

---

### Migrations fail on startup (`alembic upgrade head` exits non-zero)

The app will refuse to start and log the Alembic error. Common causes:

- **Wrong `DATABASE_URL`** — verify the host, port, database name, and credentials.
- **`asyncpg` driver prefix missing** — the URL must start with `postgresql+asyncpg://`, not `postgresql://`.
- **Database not reachable** — run `docker compose up postgres` before starting the API standalone.

---

### Redis `maxmemory-policy` eviction removes quota counters

If quota counters disappear unexpectedly, Redis is evicting keys under memory pressure. Configure Redis with:

```
maxmemory-policy noeviction
```

or `volatile-ttl` (evicts only keys with TTLs, which excludes quota counters that have window-duration TTLs). On Render, set this in the Redis configuration panel.

---

### `409 Conflict` on HITL approval

Two concurrent `POST /sessions/{id}/approve` requests arrived for the same `approval_id`. The Redis distributed lock (`SET NX PX 120000`) is held by the first request. Retry after the first request completes (within 120 seconds).

---

### `410 Gone` on SSE reconnect

The SSE replay buffer TTL has expired:
- Non-HITL streams: expire 5 minutes after the `done` event.
- HITL streams: expire 15 minutes after the request was initiated.

The user must re-submit their query. Authenticated users retain their conversation history in PostgreSQL; only the current-turn replay buffer is lost.

---

### Render cold-start timeout (first request fails)

Render's free/starter tier spins down idle instances. The first request after a cold start may take up to 30 seconds. The `/health` endpoint can be used to keep the instance warm via an uptime monitor (e.g., UptimeRobot pinging every 5 minutes).

---

### `ALL_MODELS_FAILED` SSE error

All models in the fallback chain exhausted. Check:
- `OPENAI_API_KEY` is valid and has available quota.
- `FALLBACK_MODELS` contains valid model identifiers if configured.
- The LangSmith trace (if enabled) shows where the failure chain started.

---

### Web search returns no results

`WebSearchTool` filters results below a relevance score of 0.7. A query that returns only low-confidence Tavily results will produce an empty result set. The LLM will indicate it could not find relevant information. Check `TAVILY_API_KEY` is set and valid if all web search queries fail.

---

## Contribution Guidelines

### Coding standards (mandatory)

- **Type hints** — every function and method signature must include full type annotations for parameters and return type.
- **Docstrings** — every public function, method, and class requires a docstring. One-liners are fine for simple cases; use Google style for complex ones.
- **Tests** — all business logic must have `pytest` tests. Test files mirror the source layout under `tests/`. No untested business logic ships.
- **No `eval` / `exec`** — the calculator uses `simpleeval`. Any dynamic code evaluation is a security violation.
- **No raw SQL strings** — all database access via SQLAlchemy ORM or parameterised statements. No f-string or `+`-concatenated SQL.
- **No secrets in source** — all secrets via environment variables. The CI secrets scan will block the merge if a pattern is detected.

### Before opening a PR

```bash
# Must all pass cleanly
ruff check .
mypy .
pytest --cov=src --cov-fail-under=80
```

### Read the map first

Before tracing raw files, read `graphify-out/GRAPH_REPORT.md` for the architecture overview and knowledge graph of the codebase. The god nodes (`Conversation`, `TokenPayload`, `HITLRepository`, `get_settings()`) are high-coupling points — changes there have broad blast radius.

Key design constraints to understand before making changes:

- **Nodes must not emit SSE directly.** All SSE emission happens in `agents/runner.py` via `astream_events()`. Emitting inside a node breaks checkpoint idempotency.
- **HITL atomicity.** `hitl_gate` must write the `approval_id` to the database and update state in a single committed transaction before returning. The `approval_required` SSE event is emitted *after* the commit.
- **Settings singleton.** `get_settings()` is called across 30+ modules. Cache it (`@lru_cache`) — do not create new `Settings()` instances in request handlers.
- **Thread ID format.** Auth users: `auth|{user_id}|{conversation_id}`. Guests: `guest|{sha256_hex}`. The `|` delimiter is unambiguous with UUID v4 values.
