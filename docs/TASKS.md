# Implementation Task List
## Stateful Personal Assistant — Multi-Tool AI Agent

| Field | Value |
|---|---|
| **Version** | 1.0 |
| **Date** | 2026-05-21 |
| **Status** | Active |
| **Spec** | SPEC.md v1.1 |
| **Design** | DESIGN.md v1.1 |

Tasks are ordered so every dependency is listed before the task that needs it.
Phases: Foundation → Persistence → Auth → Tools → Graph → Streaming → Quota → API → Infrastructure → Integration Tests.

---

## Phase 1 — Foundation

---

### T-001: Project dependencies and toolchain configuration

**Description:** Pin all runtime and dev dependencies; configure ruff, mypy (strict), and pytest. This is the first task; nothing can be installed or linted until it exists.

**Files:**
- `requirements.txt` — create (all runtime deps, pinned to exact versions)
- `pyproject.toml` — create (ruff rules, mypy strict settings, pytest config, coverage threshold ≥ 80%)

**Dependencies:** none

**Acceptance Criteria:**
- `pip install -r requirements.txt` completes without error inside a fresh venv.
- `ruff check .` and `mypy .` both exit 0 on an empty `src/` tree.
- `pytest --co -q` (collect-only) exits 0 with no test files present.
- All key packages present: `langgraph`, `langchain`, `langchain-openai`, `fastapi`, `uvicorn`, `sqlalchemy[asyncio]`, `asyncpg`, `redis[hiredis]`, `alembic`, `argon2-cffi`, `PyJWT[crypto]`, `simpleeval`, `tavily-python`, `structlog`, `pydantic-settings`, `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx`. _(Note: `python-jose[cryptography]` replaced by `PyJWT[crypto]` — CVE-2024-33664 (RSA-key-as-HMAC-secret bypass) and python-jose unmaintained since 2023.)_

**Tests:** none (toolchain task)

---

### T-002: Application settings with startup validation

**Description:** Implement `config/settings.py` as a Pydantic `BaseSettings` class that reads every required environment variable, fails fast on missing required vars, and validates `FALLBACK_MODELS` JSON at import time (FR-33 startup validation).

**Files:**
- `src/config/settings.py` — implement
- `.env.example` — create (documents every variable with placeholder values, no real secrets)

**Dependencies:** T-001

**Acceptance Criteria:**
- Starting the app with a missing required var (e.g., `OPENAI_API_KEY`) raises a `ValidationError` before any route is registered.
- `FALLBACK_MODELS` set to `"not-json"` raises `ValidationError` with a parse-error message and exits non-zero (FR-33).
- `FALLBACK_MODELS` absent → `settings.fallback_enabled` is `False`; a warning is logged.
- `FALLBACK_MODELS='[]'` → `settings.fallback_enabled` is `False`; a warning is logged.
- `FALLBACK_MODELS='["groq/llama-3-70b"]'` → `settings.fallback_enabled` is `True`, list has one entry.
- Pool sizes `DB_POOL_MIN`, `DB_POOL_MAX`, `REDIS_POOL_MIN`, `REDIS_POOL_MAX` are read and exposed (NFR-7).
- `LOG_LEVEL` defaults to `"INFO"`.
- `CORS_ORIGINS` is a list of strings.

**Tests:**
- `tests/unit/test_settings.py::test_missing_required_var_raises`
- `tests/unit/test_settings.py::test_fallback_models_malformed_json_raises`
- `tests/unit/test_settings.py::test_fallback_models_absent_sets_disabled`
- `tests/unit/test_settings.py::test_fallback_models_empty_list_sets_disabled`
- `tests/unit/test_settings.py::test_fallback_models_valid_sets_enabled`
- `tests/unit/test_settings.py::test_pool_sizes_read_from_env`

---

### T-003: Async database engine and session factory

**Description:** Set up SQLAlchemy async engine with `asyncpg`, configure the connection pool, and expose a `get_db_session()` async context manager. Pool parameters are sourced from settings (NFR-7).

**Files:**
- `src/persistence/database.py` — implement

**Dependencies:** T-002

**Acceptance Criteria:**
- `create_engine()` uses `asyncpg` dialect and the pool sizes from `settings`.
- `get_db_session()` yields an `AsyncSession` and commits/rolls back on exit.
- Connection pooling parameters visible in `engine.pool.size()` and `engine.pool._max_overflow`.
- No synchronous SQLAlchemy calls anywhere in this module.

**Tests:**
- `tests/unit/test_database.py::test_pool_size_matches_settings`
- `tests/unit/test_database.py::test_session_rollback_on_exception`

---

### T-004: Async Redis client and connection pool

**Description:** Implement `persistence/redis_client.py` as a module-level singleton that returns an async Redis connection pool. Pool parameters come from settings (NFR-7, NFR-8).

**Files:**
- `src/persistence/redis_client.py` — implement

**Dependencies:** T-002

**Acceptance Criteria:**
- `get_redis()` returns a `redis.asyncio.Redis` client connected to `settings.REDIS_URL`.
- Pool min/max sizes match `settings.REDIS_POOL_MIN` / `settings.REDIS_POOL_MAX`.
- `get_redis()` called twice returns the same singleton instance.
- Module-level `close_redis()` cleanly closes the pool (for graceful shutdown, NFR-9).

**Tests:**
- `tests/unit/test_redis_client.py::test_pool_size_matches_settings`
- `tests/unit/test_redis_client.py::test_singleton_returns_same_instance`

---

### T-005: Structured JSON logging

**Description:** Configure `structlog` with a JSON processor chain that auto-injects `request_id`, `user_id`, and `session_id` context variables. Log level is sourced from settings (NFR-18).

**Files:**
- `src/utils/logging.py` — implement

**Dependencies:** T-002

**Acceptance Criteria:**
- `configure_logging()` must be called once at app startup.
- Every log line is valid JSON with at least `level`, `event`, and `timestamp` fields.
- `bind_request_context(request_id, user_id, session_id)` makes those keys appear on all subsequent log lines in the same async task context.
- Setting `LOG_LEVEL=DEBUG` in env causes debug messages to appear; `LOG_LEVEL=WARNING` suppresses info messages.
- Passwords, tokens, and API key patterns must never appear (enforced by a processor that strips keys matching `password`, `token`, `api_key`).

**Tests:**
- `tests/unit/test_logging.py::test_log_line_is_valid_json`
- `tests/unit/test_logging.py::test_request_context_injected`
- `tests/unit/test_logging.py::test_log_level_respected`

---

### T-006: Input sanitisation helpers

**Description:** Implement the three-step sanitisation pipeline in `utils/sanitise.py`: reject null bytes, normalise Unicode to NFC, strip leading/trailing whitespace. This is used as a Pydantic validator and as middleware (NFR-15, DESIGN §9.6).

**Files:**
- `src/utils/sanitise.py` — implement

**Dependencies:** T-001

**Acceptance Criteria:**
- `sanitise_string(s)` raises `ValueError` for any string containing `\x00`.
- `sanitise_string(s)` normalises `"é"` (precomposed) and `"é"` (decomposed) to the same NFC output.
- `sanitise_string(s)` strips leading/trailing whitespace without altering internal whitespace.
- `sanitise_string(s)` returns the string unchanged if it is already clean.

**Tests:**
- `tests/unit/test_sanitise.py::test_null_byte_raises`
- `tests/unit/test_sanitise.py::test_unicode_nfc_normalisation`
- `tests/unit/test_sanitise.py::test_whitespace_stripped`
- `tests/unit/test_sanitise.py::test_clean_string_unchanged`

---

## Phase 2 — Persistence: Models, Migrations, Repositories

---

### T-007: SQLAlchemy ORM model definitions

**Description:** Define all five ORM table models matching the DDL in DESIGN §5.2. Every column, constraint, index, and relationship must be declared. No migration yet — models only.

**Files:**
- `src/persistence/models/user.py` — implement (`users`, `refresh_tokens`)
- `src/persistence/models/conversation.py` — implement (`conversations`)
- `src/persistence/models/message.py` — implement (`messages`)
- `src/persistence/models/hitl.py` — implement (`hitl_approvals`, `hitl_audit_log`)
- `src/persistence/models/token.py` — implement (`refresh_tokens` if split from user model; otherwise confirm placement)

**Dependencies:** T-003

**Acceptance Criteria:**
- All columns match DESIGN DDL types exactly (UUID PKs, TIMESTAMPTZ, BOOLEAN, INET, TEXT, VARCHAR with correct lengths).
- `conversations.title` has `VARCHAR(100)` per DDL (REVIEW-30 defers the 60-char constraint to a later migration).
- `hitl_audit_log` has no `onupdate` hook — append-only enforced at DB level via `REVOKE UPDATE, DELETE ... FROM app_user`.
- `mypy .` reports zero errors on these files.
- `ruff check .` reports zero errors on these files.

**Tests:**
- `tests/unit/test_models.py::test_user_model_columns`
- `tests/unit/test_models.py::test_hitl_audit_log_columns`
- `tests/unit/test_models.py::test_conversations_indexes`

---

### T-008: Alembic setup and initial schema migration

**Description:** Initialise Alembic, configure `env.py` to use the async engine, and write the initial migration that creates all tables from T-007. Every migration must include a working `downgrade()` (NFR-10).

**Files:**
- `alembic/env.py` — implement
- `alembic/script.py.mako` — implement
- `alembic.ini` — implement
- `alembic/versions/<rev>_initial_schema.py` — create

**Dependencies:** T-007

**Acceptance Criteria:**
- `alembic upgrade head` against a fresh PostgreSQL instance creates all tables without error.
- `alembic downgrade -1` after `upgrade head` drops all tables without error.
- `alembic current` shows the head revision after upgrade.
- The application does not accept HTTP traffic until migrations complete successfully (enforced in the lifespan hook in T-035).
- CI runs both `upgrade head` and `downgrade -1` in the test database.

**Tests:**
- `tests/unit/test_migrations.py::test_upgrade_and_downgrade_idempotent`
- `tests/unit/test_migrations.py::test_all_tables_created_after_upgrade`

---

### T-009: User and refresh-token repository

**Description:** Implement `user_repo.py` with typed async methods for user CRUD and token management. All queries must be parameterised (NFR-13).

**Files:**
- `src/persistence/repositories/user_repo.py` — implement

**Dependencies:** T-007, T-003

**Acceptance Criteria:**
- `get_user_by_email(email)` returns `User | None` using a parameterised `SELECT`.
- `create_user(email, password_hash)` inserts and returns the new `User`.
- `increment_failed_logins(user_id)` and `reset_failed_logins(user_id)` update atomically.
- `lock_user(user_id, until)` sets `locked_until`.
- `store_refresh_token(jti, user_id, expires_at)` inserts into `refresh_tokens`.
- `revoke_refresh_token(jti)` sets `revoked_at = NOW()`.
- `get_valid_refresh_token(jti)` returns `None` if `revoked_at IS NOT NULL` or `expires_at < NOW()`.
- No f-string or string-concatenation SQL anywhere in this file.

**Tests:**
- `tests/unit/test_user_repo.py::test_get_user_by_email_found`
- `tests/unit/test_user_repo.py::test_get_user_by_email_not_found`
- `tests/unit/test_user_repo.py::test_refresh_token_revocation`
- `tests/unit/test_user_repo.py::test_no_raw_sql_string_concatenation` (static grep assertion)

---

### T-010: Conversation and message repository

**Description:** Implement `conversation_repo.py` and `message_repo.py` with typed async methods for session CRUD and message history. All queries must be scoped by `user_id` (FR-17).

**Files:**
- `src/persistence/repositories/conversation_repo.py` — implement
- `src/persistence/repositories/message_repo.py` — implement

**Dependencies:** T-007, T-003

**Acceptance Criteria:**
- `list_conversations(user_id, cursor, limit)` returns conversations ordered by `last_accessed DESC`, scoped to `user_id`.
- `get_conversation(user_id, conversation_id)` returns `None` (not raises) when `conversation_id` belongs to a different user.
- `create_conversation(user_id, title)` generates a disambiguated title if a duplicate exists for that user (FR-19, e.g., appends ` (2)`).
- `delete_conversation(user_id, conversation_id)` is a no-op if the ID belongs to another user (caller must enforce 403).
- `save_message(conversation_id, user_id, role, content, tool_name, turn_index)` inserts and increments `conversations.message_count`.
- `list_messages(user_id, conversation_id)` returns messages ordered by `created_at ASC`, scoped to `user_id`.
- `update_last_accessed(conversation_id)` bumps `last_accessed` and increments `access_count`.

**Tests:**
- `tests/unit/test_conversation_repo.py::test_list_scoped_to_user`
- `tests/unit/test_conversation_repo.py::test_get_other_user_returns_none`
- `tests/unit/test_conversation_repo.py::test_title_disambiguation`
- `tests/unit/test_message_repo.py::test_messages_ordered_ascending`

---

### T-011: HITL repository

**Description:** Implement `hitl_repo.py` with atomic approval-ID lifecycle management. All state transitions must use single-statement `UPDATE ... RETURNING` (FR-10, NFR-16).

**Files:**
- `src/persistence/repositories/hitl_repo.py` — implement

**Dependencies:** T-007, T-003

**Acceptance Criteria:**
- `create_approval(conversation_id, user_id, tool_name, thread_id, checkpoint_id)` inserts a row with `expires_at = NOW() + 10 minutes` and returns the `approval_id` UUID.
- `consume_approval(approval_id, session_id)` uses a single `UPDATE ... WHERE id=? AND used=false AND expired=false RETURNING id` — returns the row on success, `None` on failure.
- `expire_stale_approvals()` sets `expired=TRUE` for all rows where `expires_at < NOW()` and `used=FALSE`.
- `insert_audit_log(approval_id, user_id, session_id, tool_name, decision, request_ip, decision_reason)` inserts into `hitl_audit_log` (append-only; no UPDATE/DELETE methods exist in this repo).
- `get_open_approval_ids_for_conversation(conversation_id)` returns IDs where `used=FALSE` and `expired=FALSE` (used by retention job, FR-22).

**Tests:**
- `tests/unit/test_hitl_repo.py::test_consume_approval_atomic`
- `tests/unit/test_hitl_repo.py::test_double_consume_returns_none`
- `tests/unit/test_hitl_repo.py::test_expired_approval_returns_none`
- `tests/unit/test_hitl_repo.py::test_audit_log_inserted`

---

### T-012: LangGraph PostgreSQL checkpointer wiring

**Description:** Wrap `AsyncPostgresSaver` in `persistence/checkpointer.py`, expose a factory that returns a configured checkpointer using the app's `DATABASE_URL`, and run `setup()` on first use (DESIGN §2.1).

**Files:**
- `src/persistence/checkpointer.py` — implement

**Dependencies:** T-003, T-008, T-001

**Acceptance Criteria:**
- `get_checkpointer()` returns an `AsyncPostgresSaver` instance using `settings.DATABASE_URL`.
- `checkpointer.setup()` is called exactly once at app startup (idempotent if called again).
- Thread IDs use the format `"auth|{user_id}|{conversation_id}"` for auth users and `"guest|{sha256_hex}"` for guests (DESIGN §2.1).
- `build_thread_id(user_id, conversation_id)` and `build_guest_thread_id(ip, user_agent)` helpers are exported.

**Tests:**
- `tests/unit/test_checkpointer.py::test_thread_id_auth_format`
- `tests/unit/test_checkpointer.py::test_thread_id_guest_format`
- `tests/unit/test_checkpointer.py::test_guest_thread_id_uses_sha256`

---

## Phase 3 — Authentication

---

### T-013: Password hashing with Argon2id

**Description:** Implement `auth/password.py` with `hash_password()` and `verify_password()` using `argon2-cffi`. Include a `dummy_verify()` for constant-time no-user-found paths (FR-16, DESIGN §9.4).

**Files:**
- `src/auth/password.py` — implement

**Dependencies:** T-001

**Acceptance Criteria:**
- `hash_password(plaintext)` returns a string that does not equal the plaintext.
- `hash_password(plaintext)` output starts with `$argon2id$`.
- `verify_password(plaintext, hashed)` returns `True` for the original plaintext and `False` for any other string.
- `dummy_verify()` calls `ph.verify()` against a fixed dummy hash (takes the same time as a real verify) and discards the result — prevents timing oracle on email-not-found paths.
- `hash_password` parameters: `time_cost=2`, `memory_cost=65536`, `parallelism=2` (DESIGN §9.4).

**Tests:**
- `tests/unit/test_password.py::test_hash_not_equal_plaintext`
- `tests/unit/test_password.py::test_verify_correct_password`
- `tests/unit/test_password.py::test_verify_wrong_password`
- `tests/unit/test_password.py::test_hash_uses_argon2id`
- `tests/unit/test_password.py::test_dummy_verify_returns_false`

---

### T-014: JWT creation and validation

**Description:** Implement `auth/jwt.py` to create and validate JWTs. All standard claims must be enforced (`iss`, `aud`, `exp`, `nbf`, `jti`). Guest and auth tokens follow different claim sets (FR-15, FR-16, DESIGN §9.2).

**Files:**
- `src/auth/jwt.py` — implement

**Dependencies:** T-002, T-001

**Acceptance Criteria:**
- `create_access_token(user_id, jti)` returns a JWT with `iss`, `aud`, `exp=+1h`, `jti`, `user_id` claims.
- `create_guest_token(session_id, jti)` returns a JWT with `mode: "guest"`, `session_id`, `exp=+24h`.
- `create_refresh_token(user_id, jti)` returns a JWT with `exp=+30d`.
- `decode_token(token)` validates signature, `iss`, `aud`, `exp`, and returns claims dict.
- A token with wrong `iss` raises `JWTError`.
- A token with wrong `aud` raises `JWTError`.
- An expired token raises `JWTError`.
- Algorithm `none` is never accepted — pass an explicit `algorithms=["HS256"]` list to `PyJWT`'s `jwt.decode()`; never pass `"none"` or an empty list.

**Tests:**
- `tests/unit/test_jwt.py::test_access_token_claims`
- `tests/unit/test_jwt.py::test_guest_token_claims`
- `tests/unit/test_jwt.py::test_wrong_iss_rejected`
- `tests/unit/test_jwt.py::test_wrong_aud_rejected`
- `tests/unit/test_jwt.py::test_expired_token_rejected`

---

### T-015: Access token JTI blacklist

**Description:** Implement `auth/blacklist.py` using Redis with TTL equal to the token's remaining lifetime. Every protected endpoint checks this before allowing access (FR-16, DESIGN §9.2).

**Files:**
- `src/auth/blacklist.py` — implement

**Dependencies:** T-004, T-014

**Acceptance Criteria:**
- `blacklist_token(jti, ttl_seconds)` writes `blacklist:jti:{jti}` → `"1"` with the given TTL (NFR-8 requires explicit TTL).
- `is_blacklisted(jti)` returns `True` if the key exists, `False` otherwise — O(1) Redis GET.
- After TTL expires, `is_blacklisted(jti)` returns `False` (no stale blacklist entries).
- `blacklist_token` called twice on the same `jti` does not raise (idempotent).

**Tests:**
- `tests/unit/test_blacklist.py::test_blacklisted_token_detected`
- `tests/unit/test_blacklist.py::test_unlisted_token_allowed`
- `tests/unit/test_blacklist.py::test_ttl_set_on_write`

---

### T-016: Login rate limiting and brute-force lockout

**Description:** Implement `auth/rate_limit.py` with per-IP and per-email Redis sliding-window counters, and a soft-lock at 5 consecutive failures. Counters expire automatically (FR-30, DESIGN §5.3 Redis key namespace).

**Files:**
- `src/auth/rate_limit.py` — implement

**Dependencies:** T-004, T-002

**Acceptance Criteria:**
- `check_ip_rate_limit(ip_hash)` returns `(allowed: bool, retry_after: int)`. Blocks after 10 attempts per 15 minutes.
- `check_email_rate_limit(email_hash)` returns `(allowed: bool, retry_after: int)`. Blocks after 10 attempts per 15 minutes.
- `record_failed_attempt(email_hash)` increments the email counter and checks for soft-lock threshold (5 failures → lock for `AUTH_LOCKOUT_MINUTES`).
- `is_email_locked(email_hash)` returns `(locked: bool, retry_after: int)`.
- `reset_attempt_counters(email_hash)` clears both email and IP counters on successful login (FR-30e).
- All Redis keys have explicit TTLs matching the window duration (NFR-8).
- During lockout, `is_email_locked` returns HTTP 429 — the same response body as quota-exceeded, not 401.

**Tests:**
- `tests/unit/test_rate_limit.py::test_ip_rate_limit_blocks_at_threshold`
- `tests/unit/test_rate_limit.py::test_email_soft_lock_after_five_failures`
- `tests/unit/test_rate_limit.py::test_lockout_returns_429_not_401`
- `tests/unit/test_rate_limit.py::test_successful_login_resets_counters`

---

## Phase 4 — Tools

---

### T-017: BaseTool Protocol definition

**Description:** Define `BaseTool` as a `@runtime_checkable` Protocol in `tools/base.py`. This is the extensibility interface (FR-1, DESIGN §2.5).

**Files:**
- `src/tools/base.py` — implement

**Dependencies:** T-001

**Acceptance Criteria:**
- `BaseTool` is a `Protocol` decorated with `@runtime_checkable`.
- Required attributes: `name: str`, `description: str`, `input_schema: dict`, `is_sensitive: bool`.
- Required method: `async execute(self, tool_input: dict) -> dict`.
- A class implementing all attributes and the method passes `isinstance(instance, BaseTool)`.
- A class missing any attribute or with a sync `execute` fails `isinstance`.

**Tests:**
- `tests/unit/test_tools.py::test_conforming_class_is_base_tool`
- `tests/unit/test_tools.py::test_missing_attribute_fails_isinstance`

---

### T-018: CalculatorTool

**Description:** Implement `CalculatorTool` using `simpleeval`. Must not use `eval()`, `exec()`, or `compile()`. Must reject all non-arithmetic input (FR-3, NFR-11, DESIGN §2.5).

**Files:**
- `src/tools/calculator.py` — implement

**Dependencies:** T-017

**Acceptance Criteria:**
- `await tool.execute({"expression": "2 ** 10 + 5 * 3"})` returns `{"result": 1039}`.
- `await tool.execute({"expression": "__import__('os').system('ls')"})` raises `ValueError` without executing any system call.
- `await tool.execute({"expression": "sqrt(16)"})` returns `{"result": 4.0}` (math functions allowed).
- `tool.is_sensitive` is `False`.
- Static AST scan of `calculator.py` confirms zero calls to `eval`, `exec`, or `compile` (NFR-11).
- `mypy` reports zero errors on this file.

**Tests:**
- `tests/unit/test_tools.py::test_calculator_arithmetic`
- `tests/unit/test_tools.py::test_calculator_injection_raises`
- `tests/unit/test_tools.py::test_calculator_math_functions`
- `tests/unit/test_tools.py::test_calculator_no_eval_exec` (AST scan)

---

### T-019: WeatherTool

**Description:** Implement `WeatherTool` that calls an external weather API (e.g., Open-Meteo or OpenWeatherMap) and returns `temperature`, `condition`, `humidity`. Missing API key returns a config error, not a runtime crash (SC-6). `is_sensitive = False` (FR-2).

**Files:**
- `src/tools/weather.py` — implement

**Dependencies:** T-017, T-002

**Acceptance Criteria:**
- `await tool.execute({"location": "London"})` against a mocked API returns a dict with keys `temperature`, `condition`, `humidity`, `location` within 3 seconds (NFR-3).
- When the weather API returns a non-2xx response, the tool raises a `ToolExecutionError` (not crashes) with a human-readable message.
- `tool.is_sensitive` is `False`.
- If `WEATHER_API_KEY` is missing or empty, `execute()` returns `{"error": "Weather API not configured"}` immediately (no crash).
- Uses `asyncio.to_thread` or an async HTTP client — no blocking I/O on the event loop.

**Tests:**
- `tests/unit/test_tools.py::test_weather_success_mock`
- `tests/unit/test_tools.py::test_weather_api_error_raises_tool_error`
- `tests/unit/test_tools.py::test_weather_missing_key_returns_config_error`

---

### T-020: WebSearchTool

**Description:** Implement `WebSearchTool` backed by Tavily. Apply relevance filtering (≥ 0.7), cap at 5 results, truncate to 2000-token budget. `is_sensitive = True` triggers HITL. Uses `asyncio.to_thread` for the sync Tavily client (REVIEW-28, FR-4, DESIGN §2.5).

**Files:**
- `src/tools/web_search.py` — implement

**Dependencies:** T-017, T-002

**Acceptance Criteria:**
- A mock returning 10 results (5 above 0.7, 5 below) yields exactly 5 results in output (FR-4).
- A mock returning 8 results all above 0.7 yields at most 5 (MAX_RESULTS cap).
- Combined snippet character count does not exceed `TOKEN_BUDGET * 4` characters (approx 8000 chars for 2000 tokens).
- `tool.is_sensitive` is `True`.
- `RELEVANCE_THRESHOLD`, `MAX_RESULTS`, and `TOKEN_BUDGET` are configurable class attributes.
- Missing `TAVILY_API_KEY` → `execute()` returns `{"error": "Web search not configured"}` (SC-6).
- `execute()` uses `await asyncio.to_thread(...)` for the sync Tavily client call.

**Tests:**
- `tests/unit/test_tools.py::test_web_search_relevance_filter`
- `tests/unit/test_tools.py::test_web_search_max_results_cap`
- `tests/unit/test_tools.py::test_web_search_token_budget_truncation`
- `tests/unit/test_tools.py::test_web_search_is_sensitive`
- `tests/unit/test_tools.py::test_web_search_missing_key_returns_config_error`

---

### T-021: Tool registry loader

**Description:** Implement `tools/registry.py` to load tools from `config/tools.yaml` at startup. Validate that every configured class implements `BaseTool`. Expose a `GET /tools` compatible dict. (FR-1, FR-5, FR-33, DESIGN §2.5).

**Files:**
- `src/tools/registry.py` — implement
- `config/tools.yaml` — create

**Dependencies:** T-017, T-018, T-019, T-020

**Acceptance Criteria:**
- `load_registry(config_path)` returns `dict[str, BaseTool]` with entries for `weather`, `calculator`, `web_search`.
- Adding a new entry to `tools.yaml` (pointing to a mock tool class) causes it to appear in the returned dict without modifying any other file (FR-1, FR-5).
- A `tools.yaml` entry pointing to a class that does not implement `BaseTool` raises a `RegistryError` at startup.
- `get_tool_list()` returns `list[dict]` with keys `name`, `description`, `is_sensitive` only — no internal config fields (FR-33).
- `load_registry()` validates the YAML path is within the project root (REVIEW-20 mitigation).

**Tests:**
- `tests/unit/test_tools.py::test_registry_loads_all_tools`
- `tests/unit/test_tools.py::test_registry_extensible_with_mock_tool`
- `tests/unit/test_tools.py::test_registry_invalid_tool_raises`
- `tests/unit/test_tools.py::test_get_tool_list_excludes_internal_fields`

---

## Phase 5 — Agent State and Graph

---

### T-022: AgentState TypedDict

**Description:** Define all TypedDicts in `agents/state.py` exactly as specified in DESIGN §2.2. Reducers must be correct: `add_messages` for `messages`, replace-on-write lambdas for `tool_calls` and `tool_results`.

**Files:**
- `src/agents/state.py` — implement

**Dependencies:** T-001

**Acceptance Criteria:**
- `AgentState`, `ToolCall`, `ToolResult`, `ApprovalState`, `ErrorState` all exist and are importable.
- `AgentState["messages"]` annotated with `add_messages` reducer.
- `AgentState["tool_calls"]` annotated with `lambda _, new: new` reducer.
- `AgentState["tool_results"]` annotated with `lambda _, new: new` reducer.
- `mypy --strict` reports zero errors on this file.
- `thread_id` is present in `AgentState` (per DESIGN §2.2, despite REVIEW-17 noting redundancy — deferred).

**Tests:**
- `tests/unit/test_state.py::test_messages_reducer_appends`
- `tests/unit/test_state.py::test_tool_calls_reducer_replaces`
- `tests/unit/test_state.py::test_agent_state_fields_present`

---

### T-023: Graph edge (routing) functions

**Description:** Implement all four conditional edge functions in `graph/edges.py`: `route_after_router`, `route_after_hitl`, `route_after_tools`, `route_after_llm`. Each must match the logic in DESIGN §2.4 exactly.

**Files:**
- `src/graph/edges.py` — implement

**Dependencies:** T-022

**Acceptance Criteria:**
- `route_after_router`: empty `tool_calls` → `"llm_direct"`; sensitive tool present → `"hitl"`; non-sensitive tools only → `"tool"`; `error` in state → `"error"` (default guard).
- `route_after_hitl`: `hitl_decision="approve"` → `"approved"`; `"deny"` → `"denied"`; missing/unknown → `"error"`.
- `route_after_tools`: all tool results have non-null `error` → `"error"`; at least one success → `"success"`.
- `route_after_llm`: `error` in state → `"error"`; last message is non-empty `AIMessage` → `"success"`; empty/missing last message → `"error"`.
- All four functions are typed: `(state: AgentState) -> str`.
- `mypy --strict` reports zero errors.

**Tests:**
- `tests/unit/test_graph_edges.py::test_router_edge_no_tools`
- `tests/unit/test_graph_edges.py::test_router_edge_sensitive_tool`
- `tests/unit/test_graph_edges.py::test_router_edge_non_sensitive`
- `tests/unit/test_graph_edges.py::test_hitl_edge_approve`
- `tests/unit/test_graph_edges.py::test_hitl_edge_deny`
- `tests/unit/test_graph_edges.py::test_hitl_edge_missing_decision`
- `tests/unit/test_graph_edges.py::test_tools_edge_all_failed`
- `tests/unit/test_graph_edges.py::test_tools_edge_partial_success`
- `tests/unit/test_graph_edges.py::test_llm_edge_empty_response`

---

### T-024: `router` node

**Description:** Implement the `router` node in `graph/nodes/router.py`. Resets `tool_calls`/`tool_results` each turn, calls the LLM for tool selection, writes selected `ToolCall` list to state. Does not emit SSE directly (DESIGN §2.3).

**Files:**
- `src/graph/nodes/router.py` — implement

**Dependencies:** T-022, T-021, T-002

**Acceptance Criteria:**
- Node resets `tool_calls = []` and `tool_results = []` (replace-on-write) at the start of every invocation.
- LLM tool-selection call uses `settings.OPENAI_DEFAULT_MODEL`.
- For a message that needs no tool, returns `{"tool_calls": []}`.
- For a message that needs weather, returns `{"tool_calls": [ToolCall(tool_name="weather", ...)]}`; `is_sensitive` is read from the registry.
- `active_model` is read from `state["active_model"]`, not hardcoded.
- Function signature: `async def router_node(state: AgentState, config: RunnableConfig) -> dict`.

**Tests:**
- `tests/unit/test_graph_nodes.py::test_router_no_tool_needed`
- `tests/unit/test_graph_nodes.py::test_router_selects_weather_tool`
- `tests/unit/test_graph_nodes.py::test_router_resets_tool_state`

---

### T-025: `hitl_gate` node

**Description:** Implement the `hitl_gate` node. Must perform atomic DB write in a single transaction, set `pending_approval` in state on success, set `error` on failure. Does not emit SSE — `runner.py` handles that (FR-9, DESIGN §2.3).

**Files:**
- `src/graph/nodes/hitl_gate.py` — implement

**Dependencies:** T-022, T-011, T-003

**Acceptance Criteria:**
- Generates a UUID v4 `approval_id`.
- Writes `approval_id`, `expires_at = NOW() + 10 min`, `thread_id`, `checkpoint_id` to `hitl_approvals` within a single `BEGIN...COMMIT` transaction.
- On successful commit: returns `{"pending_approval": ApprovalState(...)}` — does not emit SSE.
- On DB error: returns `{"error": ErrorState(code="HITL_DB_ERROR", ...)}` — does not raise.
- `pending_approval.expires_at` is an ISO8601 string.
- Graph suspends automatically after this node via `interrupt_after=["hitl_gate"]` (configured in `graph.py`, not here).

**Tests:**
- `tests/unit/test_graph_nodes.py::test_hitl_gate_writes_approval_to_db`
- `tests/unit/test_graph_nodes.py::test_hitl_gate_sets_pending_approval_state`
- `tests/unit/test_graph_nodes.py::test_hitl_gate_db_error_sets_error_state`

---

### T-026: `tool_executor` node

**Description:** Implement `tool_executor` with concurrent execution via `asyncio.gather`, per-tool `asyncio.wait_for` timeout of 3s, and inline retry with 1s/2s/4s backoff (FR-7, FR-27, DESIGN §2.3).

**Files:**
- `src/graph/nodes/tool_executor.py` — implement

**Dependencies:** T-022, T-021

**Acceptance Criteria:**
- Dispatches all `state["tool_calls"]` concurrently via `asyncio.gather`.
- Total elapsed time for two tools (each with 0.1s mock delay) is < 0.3s (parallel, not sequential) (FR-7).
- Per-tool coroutine retries up to 3 times on any exception, with `asyncio.sleep` delays of 1s, 2s, 4s between attempts (FR-27).
- After 3 failures, `ToolResult.error` is set to the last exception message; the node does NOT raise.
- Delays use `asyncio.sleep`, not `time.sleep` (no event loop blocking).
- Returns `{"tool_results": [...]}` — replace-on-write.

**Tests:**
- `tests/unit/test_graph_nodes.py::test_tool_executor_parallel_dispatch`
- `tests/unit/test_graph_nodes.py::test_tool_executor_retry_on_failure`
- `tests/unit/test_graph_nodes.py::test_tool_executor_retry_delays`
- `tests/unit/test_graph_nodes.py::test_tool_executor_exhausted_retries_sets_error`

---

### T-027: `llm` node

**Description:** Implement the `llm` node with streaming, 2-retry primary backoff (2s/4s), fallback chain from `FALLBACK_MODELS`, token quota recording, and empty-response detection (FR-24, FR-28, DESIGN §2.3).

**Files:**
- `src/graph/nodes/llm.py` — implement

**Dependencies:** T-022, T-002

**Acceptance Criteria:**
- Calls active model with streaming; appends completed `AIMessage` to `messages`.
- Retries primary model up to 2 times with `asyncio.sleep` delays of 2s, 4s on HTTP 429/5xx/timeout.
- After 2 primary failures, iterates through `FALLBACK_MODELS` in order, each attempted once.
- Empty string or whitespace-only response treated as failure → tries next fallback.
- All primary + fallback failures → `{"error": ErrorState(code="ALL_MODELS_FAILED", retryable=False)}`.
- Token counts from OpenAI response are extracted and forwarded to the quota module.
- `asyncio.wait_for(..., timeout=30.0)` wraps each model call (FR-28, 30-second timeout).

**Tests:**
- `tests/unit/test_graph_nodes.py::test_llm_node_success`
- `tests/unit/test_graph_nodes.py::test_llm_node_retries_primary_twice`
- `tests/unit/test_graph_nodes.py::test_llm_node_fallback_chain`
- `tests/unit/test_graph_nodes.py::test_llm_node_all_failed_error`
- `tests/unit/test_graph_nodes.py::test_llm_node_empty_response_triggers_fallback`

---

### T-028: `error_handler` node

**Description:** Implement the `error_handler` node to emit user-facing `AIMessage` responses based on `state["error"].code`. All retries have already been exhausted upstream; this node does not retry (DESIGN §2.3).

**Files:**
- `src/graph/nodes/error_handler.py` — implement

**Dependencies:** T-022

**Acceptance Criteria:**
- `HITL_DENIED` → appends cancellation `AIMessage` with friendly message.
- `TOOL_ERROR` / `TOOL_TIMEOUT` → appends `AIMessage` describing which tools failed by name.
- `ALL_MODELS_FAILED` / `NO_FALLBACK_CONFIGURED` → appends `AIMessage` with generic failure and `retryable=false`.
- `HITL_TIMEOUT` → appends `AIMessage` with timeout message and hint to re-submit (FR-11).
- Does not raise; always returns `{"messages": [...]}`.
- `error` field in state is cleared after handling (set to `None`) to prevent infinite loops.

**Tests:**
- `tests/unit/test_graph_nodes.py::test_error_handler_hitl_denied`
- `tests/unit/test_graph_nodes.py::test_error_handler_tool_failure`
- `tests/unit/test_graph_nodes.py::test_error_handler_all_models_failed`

---

### T-029: StateGraph construction and compilation

**Description:** Wire all nodes and edges into a compiled `StateGraph` in `agents/graph.py`, configure `interrupt_after=["hitl_gate"]`, and attach the PostgreSQL checkpointer (DESIGN §2.1).

**Files:**
- `src/agents/graph.py` — implement

**Dependencies:** T-023, T-024, T-025, T-026, T-027, T-028, T-012

**Acceptance Criteria:**
- Graph contains exactly five nodes: `router`, `hitl_gate`, `tool_executor`, `llm`, `error_handler` (FR-6).
- All conditional edges are registered: `router → {hitl, tool, llm_direct, error}`, `hitl_gate → {approved, denied, error}`, `tool_executor → {success, error}`, `llm → {success, error}`, `error_handler → END` (FR-6).
- `builder.compile(checkpointer=checkpointer, interrupt_after=["hitl_gate"])` is the final compilation call.
- `get_graph()` returns the compiled graph as a module-level singleton.
- `mypy --strict` reports zero errors.

**Tests:**
- `tests/unit/test_graph_edges.py::test_graph_has_five_nodes`
- `tests/unit/test_graph_edges.py::test_graph_compiled_with_interrupt_after`
- `tests/unit/test_graph_edges.py::test_all_conditional_edges_registered`

---

### T-030: Graph runner (`run_turn` and `resume_turn`)

**Description:** Implement `agents/runner.py` with `run_turn()` and `resume_turn()`. These functions iterate `graph.astream_events()` and translate graph events to SSE via the emitter. LangSmith metadata is injected here (NFR-17, DESIGN §2.6, §8.1).

**Files:**
- `src/agents/runner.py` — implement

**Dependencies:** T-029, T-030 (depends on T-031 for SSE emitter — see T-031)

**Dependencies:** T-029

**Acceptance Criteria:**
- `run_turn(graph, input_state, config, sse_emitter)` iterates `astream_events(version="v2")` and maps events per DESIGN §2.6 table.
- `on_chain_start` → `sse_emitter.emit("thinking", {node, elapsed_ms})`.
- `on_chat_model_stream` → `sse_emitter.emit("token", {content})`.
- `on_tool_end` → `sse_emitter.emit("tool_result", {tool, result})`.
- `on_chain_end` for `hitl_gate` with `pending_approval` → `sse_emitter.emit("approval_required", ...)`.
- Graph terminates → `sse_emitter.emit("done", {message_id})`.
- `resume_turn(graph, resume_state, config, sse_emitter)` calls `graph.ainvoke` with `hitl_decision` injected, then opens a new `astream_events` iteration for the resumed portion.
- LangSmith config with `run_name`, `tags`, `metadata` (NFR-17) is set on every `astream_events` call.
- LangSmith failures are caught and logged at `WARNING` — they never surface to the caller (NFR-17).
- First `thinking` event is emitted within 500ms of `run_turn` being called (NFR-2).

**Tests:**
- `tests/unit/test_runner.py::test_thinking_event_emitted_on_chain_start`
- `tests/unit/test_runner.py::test_token_event_emitted_on_llm_stream`
- `tests/unit/test_runner.py::test_done_event_emitted_at_graph_end`
- `tests/unit/test_runner.py::test_langsmith_failure_does_not_propagate`

---

## Phase 6 — Streaming

---

### T-031: SSE event Pydantic models

**Description:** Define Pydantic models for each of the six SSE event types in `streaming/events.py` (FR-13). These models enforce the payload schema at emit time.

**Files:**
- `src/streaming/events.py` — implement

**Dependencies:** T-001

**Acceptance Criteria:**
- Models defined: `ThinkingEvent`, `TokenEvent`, `ToolResultEvent`, `ApprovalRequiredEvent`, `ErrorEvent`, `DoneEvent`.
- Each model has an `event_type: Literal[...]` discriminator field.
- `ThinkingEvent` has `node: str` and `elapsed_ms: int`.
- `ErrorEvent` has `code: str`, `message: str`, `retryable: bool`.
- `DoneEvent` has `message_id: str`.
- All models validate with `mypy --strict` and `model_validate()` raises on missing required fields.

**Tests:**
- `tests/unit/test_streaming.py::test_thinking_event_schema`
- `tests/unit/test_streaming.py::test_error_event_schema`
- `tests/unit/test_streaming.py::test_event_missing_field_raises`

---

### T-032: SSE emitter with Redis replay buffer

**Description:** Implement `streaming/emitter.py`. Formats events as SSE text (`id:`, `event:`, `data:`), pushes them to a Redis list with an auto-incrementing `id`, and manages TTL per NFR-8.

**Files:**
- `src/streaming/emitter.py` — implement

**Dependencies:** T-031, T-004

**Acceptance Criteria:**
- `SSEEmitter.emit(event_type, payload)` appends a formatted SSE event to `stream:{stream_id}:events` Redis list.
- Each event gets a monotonically increasing integer `id` within the stream (implemented as Redis `LLEN` + 1).
- Every `RPUSH` is followed by `EXPIRE` to extend TTL (NFR-8 dynamic extension).
- Default TTL for non-HITL streams: 5 minutes after `done` event.
- Default TTL for HITL streams: 15 minutes from stream creation, extended on every emit.
- `emit()` is async and does not block the event loop.
- Keep-alive comment lines (`: keep-alive`) are written directly to the SSE response, not stored in Redis replay buffer (DESIGN §6.2).

**Tests:**
- `tests/unit/test_streaming.py::test_emit_appends_to_redis`
- `tests/unit/test_streaming.py::test_emit_increments_id`
- `tests/unit/test_streaming.py::test_emit_sets_ttl`
- `tests/unit/test_streaming.py::test_ttl_extended_on_each_emit`

---

### T-033: SSE replay buffer reader

**Description:** Implement `streaming/replay.py` to read events from the Redis replay buffer for reconnecting clients. Validates stream ownership and handles expired streams (FR-14, NFR-5).

**Files:**
- `src/streaming/replay.py` — implement

**Dependencies:** T-032

**Acceptance Criteria:**
- `get_events_after(stream_id, last_event_id)` returns all events from the Redis list with `id > last_event_id`, in order.
- Returns `[]` (not raises) if `last_event_id` is 0 or the list is empty.
- Returns `None` if the Redis key does not exist (stream expired → caller returns HTTP 410).
- `validate_stream_ownership(stream_id, user_id_or_session_id)` returns `True` if the stream belongs to the requester (checked against a `stream:{stream_id}:owner` Redis key written at stream creation).
- `validate_stream_ownership` returns `False` for cross-session access (FR-14, returns HTTP 403 to caller).
- `start_keepalive_loop(response, interval=15)` emits `: keep-alive\n\n` every 15 seconds without storing in the replay buffer.

**Tests:**
- `tests/unit/test_streaming.py::test_replay_returns_events_after_id`
- `tests/unit/test_streaming.py::test_replay_expired_stream_returns_none`
- `tests/unit/test_streaming.py::test_replay_cross_session_rejected`

---

## Phase 7 — Quota

---

### T-034: Sliding-window OpenAI quota enforcement

**Description:** Implement `quota/limiter.py` with per-user (or per-guest-session) and per-IP sliding-window counters for the three quota windows. Fallback model calls must not be counted (FR-26, FR-31, DESIGN §5.3).

**Files:**
- `src/quota/limiter.py` — implement

**Dependencies:** T-004, T-002

**Acceptance Criteria:**
- `check_and_increment(user_id_or_session_id, ip_hash, model, token_count)` atomically checks all three windows (4h, 24h, 7d) and increments if all pass.
- Returns `QuotaStatus(allowed=True)` or `QuotaStatus(allowed=False, quota_type, current, limit, reset_at)` (FR-26).
- Fallback model calls (`model` not in `OPENAI_MODELS`) skip quota check and return `allowed=True`.
- Guest sessions checked per `session_id` AND per `ip_hash` — either limit triggers 429 (FR-31).
- All Redis keys have explicit TTLs matching window duration (NFR-8).
- 21st call within 4h window returns `allowed=False` with `quota_type="4h_requests"`.

**Tests:**
- `tests/unit/test_quota.py::test_quota_allows_up_to_limit`
- `tests/unit/test_quota.py::test_quota_blocks_at_threshold`
- `tests/unit/test_quota.py::test_quota_response_has_reset_at`
- `tests/unit/test_quota.py::test_fallback_model_skips_quota`
- `tests/unit/test_quota.py::test_guest_ip_quota_independent_of_session`

---

## Phase 8 — API Layer ✅

> **Complete** — all 8 tasks shipped in commit `c86ec42` (2026-05-24). 37/37 tests pass.

---

### T-035: FastAPI app factory and lifespan ✅

**Description:** Implement `api/main.py` with `create_app()`. The lifespan hook must run Alembic migrations, set up the checkpointer, initialise the tool registry, configure logging, and wire CORS. Graceful shutdown (NFR-9, NFR-10).

**Files:**
- `src/api/main.py` — implement

**Dependencies:** T-002, T-003, T-004, T-005, T-008, T-012, T-021

**Acceptance Criteria:**
- `create_app()` returns a FastAPI instance with all routers included.
- `lifespan` hook runs `alembic upgrade head` before yielding; if it fails, the process exits non-zero and does not serve traffic (NFR-10).
- `lifespan` calls `checkpointer.setup()`, `load_registry()`, `configure_logging()`.
- `lifespan` shutdown block closes DB pool, Redis pool, and flushes LangSmith callbacks (NFR-9).
- CORS `allow_origins` reads from `settings.CORS_ORIGINS`; never `"*"` in production (NFR-14).
- `GET /health` returns 200 before migrations (liveness check).
- `GET /readiness` returns 200 only after migrations succeed and DB + Redis are reachable (NFR-10, DESIGN §6.5).

**Tests:**
- `tests/unit/test_app.py::test_app_factory_creates_fastapi_instance`
- `tests/unit/test_app.py::test_readiness_fails_before_migrations`
- `tests/unit/test_app.py::test_readiness_passes_after_migrations`
- `tests/unit/test_app.py::test_cors_not_wildcard_in_production`

---

### T-036: Request middleware (logging, sanitisation, timing) ✅

**Description:** Implement `api/middleware.py` with a Starlette middleware that: generates `request_id`, binds structlog context, sanitises all string fields in request bodies, records latency, and logs the structured request line (NFR-15, NFR-18, DESIGN §9.6).

**Files:**
- `src/api/middleware.py` — implement

**Dependencies:** T-005, T-006, T-035

**Acceptance Criteria:**
- Every request produces exactly one `INFO`-level log line with all seven required fields (NFR-18): `request_id`, `user_id`, `session_id`, `method`, `path`, `status_code`, `latency_ms`.
- A request body containing `\x00` is rejected with HTTP 422 before reaching any route handler (NFR-15).
- `latency_ms` is positive and measured from request start to first byte of response.
- Passwords and tokens in request bodies are never logged (filter applied by structlog processor from T-005).

**Tests:**
- `tests/unit/test_middleware.py::test_request_log_has_required_fields`
- `tests/unit/test_middleware.py::test_null_byte_body_rejected_422`
- `tests/unit/test_middleware.py::test_latency_ms_in_log`

---

### T-037: FastAPI dependency injection ✅

**Description:** Implement `api/dependencies.py` with `get_current_user`, `require_auth_user`, `get_db`, and `get_redis`. JWT validation, blacklist check, and `iss`/`aud` verification happen here (DESIGN §9.2).

**Files:**
- `src/api/dependencies.py` — implement

**Dependencies:** T-014, T-015, T-003, T-004, T-035

**Acceptance Criteria:**
- `get_current_user(token)` decodes the JWT, validates all 8 claims in DESIGN §9.2 order, checks blacklist, and returns `TokenPayload`.
- A missing `Authorization` header → `get_current_user` returns a guest payload (not 401) so guest endpoints work.
- `require_auth_user` raises `HTTPException(403)` when called with a guest token.
- Any JWT validation failure → HTTP 401 with no detail about which check failed.
- `get_db` yields an `AsyncSession` via the database session factory from T-003.
- `get_redis` returns the Redis singleton from T-004.

**Tests:**
- `tests/unit/test_auth.py::test_valid_token_returns_payload`
- `tests/unit/test_auth.py::test_blacklisted_token_returns_401`
- `tests/unit/test_auth.py::test_guest_token_blocked_by_require_auth`
- `tests/unit/test_auth.py::test_missing_token_returns_guest_payload`

---

### T-038: Auth router (`/auth/guest`, `/auth/login`, `/auth/refresh`, `/auth/logout`) ✅

**Description:** Implement all four auth endpoints in `api/routers/auth.py`. Login must apply rate limiting, brute-force lockout, constant-time hash comparison, and token rotation on refresh (FR-15, FR-16, FR-30).

**Files:**
- `src/api/routers/auth.py` — implement

**Dependencies:** T-013, T-014, T-015, T-016, T-009, T-037

**Acceptance Criteria:**
- `POST /auth/guest` returns 200 with a JWT containing `mode: "guest"`, valid UUID v4 `session_id`, no DB row created (FR-15).
- `POST /auth/login` with valid credentials returns `access_token` and `refresh_token` with correct expiry and `jti` claims (FR-16).
- `POST /auth/login` with invalid email returns HTTP 401; with invalid password returns HTTP 401; both responses have identical bodies and similar timing (FR-16b).
- 11th login attempt from same IP within 15 minutes returns HTTP 429 (FR-30a).
- 5 consecutive failures for same email lock it; locked login returns HTTP 429 (FR-30c).
- `POST /auth/refresh` with a valid refresh token returns new `access_token` + rotated `refresh_token`; old token returns 401 on reuse (FR-16).
- `POST /auth/logout` adds access token `jti` to blacklist; subsequent request with that token returns 401 (FR-16d).

**Tests:**
- `tests/unit/test_auth.py::test_guest_token_issued`
- `tests/unit/test_auth.py::test_login_success`
- `tests/unit/test_auth.py::test_login_invalid_password_identical_response`
- `tests/unit/test_auth.py::test_login_ip_rate_limit`
- `tests/unit/test_auth.py::test_login_email_soft_lock`
- `tests/unit/test_auth.py::test_refresh_token_rotation`
- `tests/unit/test_auth.py::test_logout_blacklists_token`

---

### T-039: Chat router (`POST /chat`, `GET /chat/stream`) ✅

**Description:** Implement the two-step SSE handshake in `api/routers/chat.py`. `POST /chat` validates quota and returns HTTP 202 with `stream_url`; `GET /chat/stream` opens the SSE response and calls `run_turn` (FR-12, FR-13, FR-26).

**Files:**
- `src/api/routers/chat.py` — implement

**Dependencies:** T-030, T-032, T-033, T-034, T-037, T-035

**Acceptance Criteria:**
- `POST /chat` validates quota before starting the graph; returns 429 with all four fields on quota exceeded (FR-26).
- `POST /chat` returns HTTP 202 (not 200) with `{message_id, session_id, stream_url}` — does not open a streaming connection (FR-12).
- `POST /chat` with `session_id=null` creates a new conversation.
- `GET /chat/stream?stream_id=X` validates stream ownership; returns 403 if owned by another user (FR-14).
- `GET /chat/stream` sends `Content-Type: text/event-stream` and calls `run_turn`.
- `GET /chat/stream` with `Last-Event-ID: N` header replays events `id > N` without re-executing graph nodes (FR-14).
- First `thinking` SSE event arrives within 500ms of the SSE connection opening (NFR-2).
- Expired `stream_id` returns HTTP 410 (FR-21).

**Tests:**
- `tests/unit/test_chat.py::test_post_chat_returns_202`
- `tests/unit/test_chat.py::test_post_chat_quota_exceeded_returns_429`
- `tests/unit/test_chat.py::test_get_stream_wrong_user_returns_403`
- `tests/unit/test_chat.py::test_get_stream_expired_returns_410`

---

### T-040: Sessions router (`GET /sessions`, `GET /sessions/{id}/messages`, `DELETE /sessions/{id}`, `POST /sessions/{id}/approve`, `POST /sessions/{id}/model`) ✅

**Description:** Implement all session management and HITL approval endpoints in `api/routers/sessions.py`. Enforce data isolation on every operation (FR-17, FR-19, FR-20, FR-10, FR-25).

**Files:**
- `src/api/routers/sessions.py` — implement

**Dependencies:** T-010, T-011, T-037, T-004, T-030

**Acceptance Criteria:**
- `GET /sessions` with guest token returns HTTP 403 (FR-19).
- `GET /sessions` returns sessions ordered by `last_accessed DESC` with all five required fields (FR-19).
- `GET /sessions?limit=20` with 25 sessions returns 20 items and a `next_cursor` (FR-19).
- `GET /sessions/{id}/messages` with another user's token returns HTTP 403 — same status as auth failure, no distinguishing detail (FR-20).
- `GET /sessions/{id}/messages` with an invalid UUID format returns HTTP 403 (FR-20).
- `DELETE /sessions/{id}` with another user's token returns HTTP 403 (FR-17).
- `POST /sessions/{id}/approve`: two concurrent requests with the same `approval_id` — one returns 200, the other returns HTTP 409 (FR-10 distributed lock).
- `POST /sessions/{id}/approve` with already-used `approval_id` returns HTTP 410 (FR-10).
- `POST /sessions/{id}/model` with unregistered model name returns HTTP 422 (FR-25).

**Tests:**
- `tests/unit/test_sessions.py::test_get_sessions_guest_forbidden`
- `tests/unit/test_sessions.py::test_get_sessions_pagination`
- `tests/unit/test_sessions.py::test_get_messages_cross_user_forbidden`
- `tests/unit/test_sessions.py::test_approve_concurrent_409`
- `tests/unit/test_sessions.py::test_approve_replayed_id_410`
- `tests/unit/test_sessions.py::test_model_switch_invalid_name_422`

---

### T-041: Tools router (`GET /tools`) and health router (`GET /health`, `GET /readiness`) ✅

**Description:** Implement `api/routers/tools.py` and `api/routers/health.py`. `/tools` returns the registered tool list; `/readiness` checks PostgreSQL, Redis, and Alembic revision (FR-33, DESIGN §6.4, §6.5, SPEC §12.20).

**Files:**
- `src/api/routers/tools.py` — implement
- `src/api/routers/health.py` — implement

**Dependencies:** T-021, T-003, T-004, T-008, T-035

**Acceptance Criteria:**
- `GET /tools` returns JSON array with `name`, `description`, `is_sensitive` for all registered tools; no internal config fields (FR-33).
- `GET /tools` accessible to both guest and authenticated users (DESIGN §9.3).
- `GET /health` always returns `{"status": "ok"}` with HTTP 200 if the process is running.
- `GET /readiness` returns HTTP 200 with `checks: {postgres: "ok", redis: "ok", alembic_revision: "ok"}` when all services are healthy.
- `GET /readiness` returns HTTP 503 when PostgreSQL is unreachable, including the error message in the response body (DESIGN §6.5).
- `GET /readiness` returns HTTP 503 when Alembic head revision does not match the running revision.

**Tests:**
- `tests/unit/test_tools_endpoint.py::test_get_tools_returns_all_registered`
- `tests/unit/test_tools_endpoint.py::test_get_tools_no_internal_fields`
- `tests/unit/test_health.py::test_liveness_always_200`
- `tests/unit/test_health.py::test_readiness_503_on_db_failure`
- `tests/unit/test_health.py::test_readiness_503_on_stale_migration`

---

### T-042: Standardised error response schema ✅

**Description:** Implement a FastAPI exception handler that maps all non-SSE errors to the standard `{"error": {code, message, retryable, retry_after_seconds}}` schema. No endpoint may return a bare HTTP error (FR-29).

**Files:**
- `src/api/middleware.py` — extend (add exception handlers)

**Dependencies:** T-035, T-036

**Acceptance Criteria:**
- HTTP 401, 403, 404, 410, 422, 429, 500 all return bodies that deserialise to the four-field schema.
- `retry_after_seconds` is `null` for non-retryable errors, a positive integer for 429/503.
- `code` is `SCREAMING_SNAKE_CASE`.
- FastAPI's default 422 validation error body is replaced by this schema.
- Pydantic `ValidationError` on request bodies returns HTTP 422 with the standard schema (FR-29).

**Tests:**
- `tests/unit/test_errors.py::test_401_uses_standard_schema`
- `tests/unit/test_errors.py::test_422_uses_standard_schema`
- `tests/unit/test_errors.py::test_429_includes_retry_after`
- `tests/unit/test_errors.py::test_500_uses_standard_schema`

---

## Phase 9 — Infrastructure

---

### T-043: Docker and Docker Compose for local development ✅

**Description:** Write a multi-stage `Dockerfile` and `docker-compose.yml` that bring up FastAPI + PostgreSQL + Redis as a single `docker compose up --build` command.

**Files:**
- `Dockerfile` — create
- `docker-compose.yml` — create

**Dependencies:** T-035

**Acceptance Criteria:**
- `docker compose up --build` starts all three services without manual setup.
- FastAPI container depends on `postgres` and `redis` health checks before starting.
- Dockerfile uses a multi-stage build: `builder` stage installs deps, `runtime` stage copies only the venv.
- No secret values are baked into the Dockerfile; all are passed via `environment:` from `.env`.
- `GET /readiness` returns 200 within 60 seconds of `docker compose up`.

**Tests:** manual smoke test (not automated)

---

### T-044: GitHub Actions CI pipeline ✅

**Description:** Write the CI workflow that runs lint, type-check, secrets scan, migrations, and tests on every push to `main` and on every PR. Deployment to Render is gated on all stages (G-7, SC-9, NFR-12).

**Files:**
- `.github/workflows/ci.yml` — create
- `.secrets.baseline` — create (detect-secrets baseline)

**Dependencies:** T-001, T-008, T-043

**Acceptance Criteria:**
- Stages in order: `lint` (ruff), `typecheck` (mypy --strict), `secrets-scan` (detect-secrets), `test` (pytest with coverage ≥ 80%), `migrations` (alembic upgrade head + downgrade -1).
- `test` stage runs against a real PostgreSQL and Redis service container, not mocks.
- Pipeline fails if `mypy` reports any error (SC-10).
- Pipeline fails if `detect-secrets` finds any new secret beyond the committed baseline (NFR-12).
- Deploy step to Render is triggered only when all previous stages pass on `main`.

**Tests:** CI itself is the test

---

### T-045: HITL timeout background task

**Description:** Implement the HITL timeout sweeper as an `asyncio` background task started in the app lifespan. Marks expired approvals, emits timeout audit log entries, and is safe to run concurrently (FR-11, FR-32).

**Files:**
- `src/persistence/repositories/hitl_repo.py` — extend (add `expire_stale_approvals`)
- `src/api/main.py` — extend (register background task in lifespan)

**Dependencies:** T-011, T-035

**Acceptance Criteria:**
- Background task runs every 60 seconds.
- Calls `hitl_repo.expire_stale_approvals()` which sets `expired=TRUE` for rows where `expires_at < NOW()` and `used=FALSE`.
- Inserts a `hitl_audit_log` entry with `decision="timeout"` for each newly expired approval.
- Task is cancelled cleanly on `SIGTERM` without leaving dangling DB connections (NFR-9).
- A test mocks time past 10 minutes and verifies `expired=TRUE` is set (FR-11).

**Tests:**
- `tests/unit/test_hitl_timeout.py::test_expired_approval_marked_in_db`
- `tests/unit/test_hitl_timeout.py::test_timeout_audit_log_inserted`
- `tests/unit/test_hitl_timeout.py::test_approve_expired_id_returns_410`

---

### T-046: Conversation retention background job

**Description:** Implement the daily retention job that purges low-engagement conversations older than 90 days. Uses `FOR UPDATE SKIP LOCKED` for atomic deletion. Skips conversations with open HITL approvals (FR-22, DESIGN REVIEW-31).

**Files:**
- `src/persistence/repositories/conversation_repo.py` — extend (add `delete_stale_conversations`)
- `src/api/main.py` — extend (schedule daily job in lifespan)

**Dependencies:** T-010, T-011, T-035

**Acceptance Criteria:**
- Deletes only conversations where `last_accessed < NOW() - 90 days` AND `access_count < 5`.
- Does NOT delete conversations with open HITL approvals (`hitl_approvals.used=FALSE AND expired=FALSE`) (FR-22, REVIEW-31 fix).
- Uses `FOR UPDATE SKIP LOCKED` to skip in-progress sessions (FR-22).
- Test seeds four conversations covering all condition combinations plus one with open HITL; only the one meeting both purge conditions (no open HITL) is deleted.
- `DELETE /sessions/{id}` by the user bypasses retention logic and always deletes (FR-22).

**Tests:**
- `tests/integration/test_retention.py::test_only_stale_low_access_deleted`
- `tests/integration/test_retention.py::test_open_hitl_skipped`
- `tests/integration/test_retention.py::test_in_progress_session_skipped`
- `tests/integration/test_retention.py::test_user_manual_delete_succeeds`

---

## Phase 10 — Integration Tests

---

### T-047: Test infrastructure (fixtures and mocks)

**Description:** Set up `tests/conftest.py` with shared pytest-asyncio fixtures: async test client, mock DB session, mock Redis, mock OpenAI, mock Tavily, mock weather API. These fixtures are used by all integration tests.

**Files:**
- `tests/conftest.py` — implement
- `tests/fixtures/mock_openai.py` — implement
- `tests/fixtures/mock_tavily.py` — implement
- `tests/fixtures/mock_weather.py` — implement

**Dependencies:** T-035, T-039, T-040

**Acceptance Criteria:**
- `async_client` fixture provides an `httpx.AsyncClient` pointed at the test app with mocked external services.
- `mock_openai` fixture intercepts all OpenAI API calls and returns configurable streamed responses.
- `mock_tavily` fixture intercepts Tavily calls and returns a configurable result list with scores.
- `mock_weather` fixture intercepts weather API calls.
- `db_session` fixture rolls back after each test (no cross-test contamination).
- `redis_client` fixture uses a real Redis instance (test-specific DB index, flushed after each test).

**Tests:** (fixtures; used by all subsequent test files)

---

### T-048: Full chat flow integration test

**Description:** End-to-end test of the normal (no-tool) and non-sensitive tool path: `POST /chat` → `GET /chat/stream` → consume all SSE events → assert event sequence and content (FR-8, FR-12, FR-13, NFR-1, NFR-2).

**Files:**
- `tests/integration/test_chat_flow.py` — implement

**Dependencies:** T-047

**Acceptance Criteria:**
- `POST /chat` returns HTTP 202 with `stream_url`.
- `GET /chat/stream` opens correctly with `Content-Type: text/event-stream`.
- First SSE event is `thinking` and arrives within 500ms (NFR-2).
- First `token` event arrives within 1500ms of SSE connection opening (NFR-1).
- A complete turn emits at least: one `thinking`, one `token`, one `done` event.
- All events have monotonically increasing integer `id` fields.
- `done` event `message_id` matches the `message_id` from `POST /chat` response.
- For a weather query, a `tool_result` event with `tool: "weather"` appears before the `token` events.

**Tests:**
- `tests/integration/test_chat_flow.py::test_no_tool_chat_full_turn`
- `tests/integration/test_chat_flow.py::test_weather_tool_chat_full_turn`
- `tests/integration/test_chat_flow.py::test_first_thinking_within_500ms`
- `tests/integration/test_chat_flow.py::test_first_token_within_1500ms`
- `tests/integration/test_chat_flow.py::test_event_ids_monotonically_increasing`

---

### T-049: HITL flow integration test

**Description:** End-to-end HITL test: web search query → `approval_required` SSE event → `POST /approve` → graph resumes → `done`. Tests approve and deny paths, concurrency guard, and replay buffer TTL (FR-9, FR-10, FR-11, FR-14, FR-32, NFR-4).

**Files:**
- `tests/integration/test_hitl_flow.py` — implement

**Dependencies:** T-047, T-048

**Acceptance Criteria:**
- Checkpoint is committed to DB before `approval_required` SSE is emitted (FR-9 — verified by DB query between SSE parse and assertion).
- `approval_required` event contains `approval_id` (valid UUID v4), `tool`, and `description`.
- `POST /approve` with `decision: "approve"` resumes graph; next SSE event arrives within 500ms (NFR-4).
- `POST /approve` with `decision: "deny"` resumes graph through `error_handler`; SSE stream emits `error` event with a user-facing message.
- Two concurrent `POST /approve` with the same `approval_id` — one returns 200, the other returns 409 (FR-10).
- A reused `approval_id` (after server restart) returns 410 (FR-10).
- HITL audit log has a row inserted with all required fields within 1 second of approval (FR-32).
- SSE replay buffer is alive at minute 8 of a mocked HITL wait (15-minute TTL, FR-14).

**Tests:**
- `tests/integration/test_hitl_flow.py::test_checkpoint_committed_before_sse`
- `tests/integration/test_hitl_flow.py::test_approve_resumes_graph`
- `tests/integration/test_hitl_flow.py::test_deny_routes_to_error_handler`
- `tests/integration/test_hitl_flow.py::test_concurrent_approve_409`
- `tests/integration/test_hitl_flow.py::test_replayed_approval_id_410`
- `tests/integration/test_hitl_flow.py::test_audit_log_inserted`
- `tests/integration/test_hitl_flow.py::test_replay_buffer_alive_at_8min`

---

### T-050: SSE reconnection integration test

**Description:** Test mid-stream disconnect and replay via `Last-Event-ID`. Verifies replay order, cross-session rejection, and buffer expiry (FR-14, NFR-5).

**Files:**
- `tests/integration/test_sse_reconnect.py` — implement

**Dependencies:** T-047, T-048

**Acceptance Criteria:**
- Client receives events 1–5, disconnects, reconnects with `Last-Event-ID: 5` → receives events 6+ in correct order without re-running graph nodes.
- Reconnect delivers first replayed event within 2 seconds (NFR-5).
- Reconnect with `Last-Event-ID` from a different session returns HTTP 403 (FR-14).
- After stream TTL expires (non-HITL), reconnect with any `Last-Event-ID` returns HTTP 410.
- Graph nodes are NOT re-executed on reconnect (tool mock call count stays at 1).

**Tests:**
- `tests/integration/test_sse_reconnect.py::test_reconnect_replays_missed_events`
- `tests/integration/test_sse_reconnect.py::test_reconnect_within_2s`
- `tests/integration/test_sse_reconnect.py::test_cross_session_reconnect_403`
- `tests/integration/test_sse_reconnect.py::test_expired_stream_reconnect_410`
- `tests/integration/test_sse_reconnect.py::test_graph_not_reexecuted_on_reconnect`

---

### T-051: Session CRUD and data isolation integration test

**Description:** Test the full session lifecycle including list, messages, delete, and cross-user data isolation. Verifies that LangGraph checkpointer re-hydrates context across process restarts (FR-17, FR-18, FR-19, FR-20, SM-9).

**Files:**
- `tests/integration/test_sessions.py` — implement

**Dependencies:** T-047, T-048

**Acceptance Criteria:**
- `GET /sessions` returns only the authenticated user's sessions; other users' sessions are absent.
- `GET /sessions/{id_of_user_B}` with user A's token returns HTTP 403 — identical response body as any other 403 (FR-17).
- `DELETE /sessions/{id_of_user_B}` with user A's token returns HTTP 403 (FR-17).
- `GET /sessions/{id}/messages` returns messages ordered by `created_at ASC` with correct `role` sequence (FR-20).
- Tool-invocation turns have non-null `tool_name` in message list (FR-20).
- Three-turn conversation: process restart between turns 2 and 3; turn 3 response references entities from turn 1 (FR-18, SM-9).
- Guest token → `GET /sessions` returns 403 (FR-19).

**Tests:**
- `tests/integration/test_sessions.py::test_session_list_scoped_to_user`
- `tests/integration/test_sessions.py::test_cross_user_access_403`
- `tests/integration/test_sessions.py::test_message_history_ordered`
- `tests/integration/test_sessions.py::test_conversation_resumption_after_restart`
- `tests/integration/test_sessions.py::test_guest_sessions_forbidden`

---

### T-052: Guest session isolation and expiry integration test

**Description:** Test guest session Redis state, isolation between two guests, and HTTP 410 on key expiry. Verify no guest data written to PostgreSQL (FR-21).

**Files:**
- `tests/integration/test_sessions.py` — extend (or new file `test_guest_sessions.py`)

**Dependencies:** T-047, T-048

**Acceptance Criteria:**
- After a complete guest turn, zero rows exist in `conversations`, `messages` tables for that session.
- Manually expiring the Redis guest key causes `GET /chat/stream?stream_id=...` to return HTTP 410 (FR-21).
- Two guests with different session IDs and IPs have independent quota counters (FR-31).
- A guest SSE stream accessed with another guest's token returns HTTP 403 (FR-21).

**Tests:**
- `tests/integration/test_sessions.py::test_guest_no_db_writes`
- `tests/integration/test_sessions.py::test_guest_expired_key_returns_410`
- `tests/integration/test_sessions.py::test_two_guests_independent_quotas`
- `tests/integration/test_sessions.py::test_guest_stream_cross_session_403`

---

## Summary

| Phase | Tasks | Spec coverage |
|---|---|---|
| Foundation | T-001 – T-006 | SC-2, SC-10, NFR-15, NFR-18 |
| Persistence | T-007 – T-012 | FR-17, FR-18, FR-22, NFR-7, NFR-10, NFR-13 |
| Auth | T-013 – T-016 | FR-15, FR-16, FR-30, NFR-12 |
| Tools | T-017 – T-021 | FR-1 – FR-5, FR-33, NFR-11 |
| Graph | T-022 – T-030 | FR-6 – FR-11, FR-23, FR-24, FR-27, FR-28, NFR-17 |
| Streaming | T-031 – T-033 | FR-12 – FR-14, NFR-2, NFR-5, NFR-8 |
| Quota | T-034 | FR-26, FR-31 |
| API | T-035 – T-042 | FR-12, FR-19, FR-20, FR-25, FR-29, FR-33, NFR-9, NFR-10, NFR-14, NFR-18 |
| Infrastructure | T-043 – T-046 | G-7, SC-1, SC-9, FR-11, FR-22, NFR-12 |
| Integration | T-047 – T-052 | FR-7, FR-8, FR-9, FR-10, FR-13, FR-14, FR-17, FR-18, FR-21, FR-32, NFR-1, NFR-2, NFR-4, NFR-5, SM-3, SM-9 |
| **Total** | **52 tasks** | |
