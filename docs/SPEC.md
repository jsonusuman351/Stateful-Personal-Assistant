# Software Requirements Specification
## Stateful Personal Assistant — Multi-Tool AI Agent

| Field | Value |
|---|---|
| **Version** | 1.1 |
| **Date** | 2026-05-20 |
| **Status** | Draft (updated post-review) |
| **Author** | Suman Jaiswal |
| **Audience** | Developer (sole contributor) |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Goals and Non-Goals](#3-goals-and-non-goals)
4. [User Personas](#4-user-personas)
5. [Functional Requirements](#5-functional-requirements)
6. [Non-Functional Requirements](#6-non-functional-requirements)
7. [System Constraints](#7-system-constraints)
8. [Assumptions](#8-assumptions)
9. [Out of Scope](#9-out-of-scope)
10. [Success Metrics](#10-success-metrics)
11. [Risks](#11-risks)
12. [Deferred Decisions](#12-deferred-decisions)
13. [Glossary](#13-glossary)

---

## 1. Executive Summary

The Stateful Personal Assistant is a production-grade, multi-tool AI agent built on LangGraph and exposed via a FastAPI REST and SSE API. It orchestrates three tools — weather lookup, arithmetic calculation, and web search — through a structured agent graph that supports parallel tool execution, human-in-the-loop (HITL) approval for sensitive operations, and real-time streaming with mid-stream reconnection. Conversation state is persisted per authenticated user in PostgreSQL; guest sessions use ephemeral Redis state. The system enforces per-window token quotas on the primary OpenAI model and falls back to free open-source models when the primary is unavailable or rate-limited. Deployment targets Render via a GitHub Actions CI/CD pipeline that gates release on lint, type-checking, and mocked pytest suites. Although designed for a single developer's personal use, all code, architecture, and design patterns conform to production-grade standards.

---

## 2. Problem Statement

Existing general-purpose chat assistants lack the ability to combine live external tool results (weather data, computed values, real-time web search) in a single coherent response, pause for user approval before executing sensitive operations, maintain full conversation context across multiple sessions, and degrade gracefully when the primary LLM is unavailable. Building this system serves a dual purpose: solving these gaps for personal daily use and providing a comprehensive learning substrate for LangGraph, LangChain, FastAPI, and production async Python patterns.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- **G-1** — Deliver a working multi-tool agent (weather, calculator, web search) that streams responses incrementally to the user.
- **G-2** — Support HITL approval for sensitive tool calls, with automatic graph suspension and resumption.
- **G-3** — Persist full conversation history for authenticated users and allow resumption of any prior session.
- **G-4** — Implement a dual authentication model: ephemeral guest sessions and persistent personalized accounts with strong security.
- **G-5** — Enforce per-window OpenAI token quotas (4-hour, daily, weekly) with transparent user feedback.
- **G-6** — Fall back automatically to free/open-source models when the primary model is unavailable or rate-limited.
- **G-7** — Deploy the system to Render via a CI/CD pipeline that enforces lint, type checks, and mocked tests.
- **G-8** — Produce code and architecture patterns that are horizontally scalable without requiring a rewrite.

### 3.2 Non-Goals

- **NG-1** — The system will not support file upload, image processing, or document analysis in v1.
- **NG-2** — The system will not expose a multi-user admin dashboard or moderation interface.
- **NG-3** — The system will not include billing, payment processing, or subscription management.
- **NG-4** — The system will not support voice or audio input/output.
- **NG-5** — The system will not implement multi-language or internationalisation support.
- **NG-6** — The system will not build a mobile native application; a web UI is the sole client.
- **NG-7** — The system will not implement a Prometheus metrics endpoint in v1, though the architecture must not preclude adding one later.

---

## 4. User Personas

### Persona 1 — Guest User

| Attribute | Detail |
|---|---|
| **Name** | Alex |
| **Authentication** | None — no login required |
| **Session type** | Ephemeral (Redis-backed, 24-hour TTL) |
| **Primary need** | Run a quick query without signing up |
| **Constraints** | No conversation history; session lost on browser close; same token quotas as authenticated users |
| **HITL support** | Yes — approval prompt delivered via SSE within the active session |

### Persona 2 — Authenticated User

| Attribute | Detail |
|---|---|
| **Name** | Suman |
| **Authentication** | JWT (email + password) with strong auth controls |
| **Session type** | Persistent (PostgreSQL-backed) |
| **Primary need** | Maintain a history of conversations, resume sessions across devices and days, and trust the agent to ask before executing sensitive actions |
| **Constraints** | Subject to per-window OpenAI quotas; can switch to fallback models when quotas are exhausted |
| **HITL support** | Yes — approval persisted in checkpoint; user can approve from any device |

---

## 5. Functional Requirements

Each requirement is stated with a unique identifier (FR-N), a priority (P1 = must-have, P2 = should-have, P3 = nice-to-have), and a testable acceptance criterion.

---

### 5.1 Tool Registry

**FR-1** — Tool Interface Contract  
**Priority:** P1  
The system must define an abstract base class (or `Protocol`) named `BaseTool` with the following required attributes and methods: `name: str`, `description: str`, `input_schema: dict`, `is_sensitive: bool`, and `async execute(input: dict) -> dict`. Any class implementing this interface must be registerable without modifying the agent graph, router, or any existing tool.  
**Acceptance criterion:** A new tool class implementing `BaseTool` can be added and registered by modifying only the tool configuration file and the new tool file itself. All existing tests pass without change.

---

**FR-2** — Weather Lookup Tool  
**Priority:** P1  
The system must provide a `WeatherTool` that accepts a location string and returns current weather data (temperature, condition, humidity) from an external weather API. `WeatherTool.is_sensitive` must be `False`.  
**Acceptance criterion:** Given `{"location": "London"}`, the tool returns a dict containing `temperature`, `condition`, and `humidity` within 3 seconds. A mock test covers the successful path and the API-unavailable error path.

---

**FR-3** — Calculator Tool  
**Priority:** P1  
The system must provide a `CalculatorTool` that evaluates arithmetic expressions passed as strings. The tool must not use Python's built-in `eval()` or `exec()`. It must use a safe expression parser (e.g., `simpleeval` or `asteval`). `CalculatorTool.is_sensitive` must be `False`.  
**Acceptance criterion:** `{"expression": "2 ** 10 + 5 * 3"}` returns `{"result": 1039}`. Passing `{"expression": "__import__('os').system('ls')"}` raises a `ValueError` and does not execute system calls. A test asserts both cases.

---

**FR-4** — Web Search Tool  
**Priority:** P1  
The system must provide a `WebSearchTool` backed by the Tavily API. `WebSearchTool.is_sensitive` must be `True`. Before forwarding results to the LLM, the tool must: (a) discard results with a Tavily relevance score below a configurable threshold (default: `0.7`), (b) retain at most 5 results, and (c) truncate the filtered result set to fit within a configurable token budget (default: 2,000 tokens), preserving title, URL, and snippet for each result.  
**Acceptance criterion:** A mock test supplying 10 results with varying scores confirms that results below 0.7 are excluded, the output contains at most 5 items, and the total character count of the combined snippets does not exceed the token budget ceiling. A separate test confirms `is_sensitive` is `True`.

---

**FR-5** — Tool Extensibility at Startup  
**Priority:** P1  
The system must load the tool registry from a configuration file at application startup. Adding a new tool must require no changes outside of: (1) the new tool's source file, and (2) the tool configuration entry.  
**Acceptance criterion:** A test simulates startup with an additional mock tool entry in the configuration and asserts the mock tool appears in the registered tool list. The system must also expose a `GET /tools` endpoint (see FR-33) that returns all registered tools.

---

### 5.2 Agent Orchestration

**FR-6** — LangGraph StateGraph Topology  
**Priority:** P1  
The agent must be implemented as a LangGraph `StateGraph` containing exactly these named nodes: `router`, `tool_executor`, `hitl_gate`, `llm`, and `error_handler`. The graph must use conditional edges so that: (a) the `router` routes to `hitl_gate` when any selected tool has `is_sensitive = True`, otherwise directly to `tool_executor`; (b) `hitl_gate` routes to `tool_executor` on approval and to `error_handler` on denial or timeout; (c) `tool_executor` routes to `error_handler` on failure and to `llm` on success; (d) `llm` routes to `error_handler` on failure and terminates on success.  
**Acceptance criterion:** A unit test for each conditional edge verifies the correct next node is selected given a mocked graph state representing each branch condition.

---

**FR-7** — Parallel Tool Execution  
**Priority:** P1  
When the `router` selects more than one tool in a single turn, all non-sensitive tools must be dispatched and executed concurrently using `asyncio.gather` or equivalent. The `llm` node must not be invoked until all tool results are collected.  
**Acceptance criterion:** A test issues a query that the mocked router resolves to two non-sensitive tools. Both tool mocks are called, and the `llm` node receives both results in its input before being invoked. Total elapsed time must be less than the sum of the individual tool mock delays.

---

**FR-8** — Thinking Process Indicator  
**Priority:** P1  
The system must emit a `thinking` SSE event at the entry of every LangGraph node transition. The event payload must conform to `{"node": "<node_name>", "elapsed_ms": <int>}` where `elapsed_ms` is the time in milliseconds since the request was received. The first `thinking` event must be emitted within 500 ms of the server receiving the `POST /chat` request.  
**Acceptance criterion:** An integration test opens an SSE stream, issues a multi-tool query, and asserts: (a) the first SSE event type is `thinking`, (b) it arrives within 500 ms, and (c) a `thinking` event is emitted for each of the five graph nodes exercised in the turn.

---

### 5.3 Human-in-the-Loop (HITL)

**FR-9** — HITL Trigger Conditions  
**Priority:** P1  
The `hitl_gate` node must suspend graph execution whenever any tool selected for the current turn has `is_sensitive = True`. Suspension means: (a) the current LangGraph state and the `approval_id` are written to the PostgreSQL checkpointer within a single atomic transaction, (b) the transaction is committed before the graph suspends, and (c) an `approval_required` SSE event is emitted only after the transaction commits. If the transaction fails, the SSE event must not be emitted and an `error` event must be emitted instead.  
**Acceptance criterion:** A test routes a query through `WebSearchTool`. The test asserts: (1) the checkpoint is written and committed before the `approval_required` event is emitted, (2) a mocked database connection failure prevents the `approval_required` event from being emitted, and (3) the `approval_id` and checkpoint are both present in the database after a successful suspension.

---

**FR-10** — HITL Approval Flow with Atomic Consumption and Concurrency Control  
**Priority:** P1  
After receiving an `approval_required` event, the client submits `POST /sessions/{session_id}/approve` with body `{"approval_id": "<uuid>", "decision": "approve" | "deny"}`. The approval endpoint must: (a) acquire a distributed lock on the `approval_id` for the duration of the operation (e.g., `SET NX PX` in Redis); (b) atomically verify the `approval_id` exists, belongs to the session, is not used, and is not expired using a single database operation (e.g., `UPDATE ... WHERE id=? AND used=false AND expired=false RETURNING id`); (c) on successful verification, immediately mark the id as used in the same atomic statement; (d) release the lock; (e) on approval, resume the graph from the checkpoint and execute the tool; (f) on denial, resume from the checkpoint, skip the tool, route to `error_handler`, and return a user-friendly cancellation message. If the lock is already held (concurrent request), return HTTP 409 (Conflict).  
**Acceptance criterion:** Two concurrent `POST /sessions/{session_id}/approve` requests with the same `approval_id` are submitted simultaneously. One succeeds, the other receives HTTP 409. The tool is executed exactly once. A separate test asserts that a replayed `approval_id` (already marked `used=true`) returns HTTP 410 after a server restart.

---

**FR-11** — HITL Approval Timeout  
**Priority:** P1  
If no approval decision is received within 10 minutes of the `approval_required` event being emitted, the pending checkpoint must be marked as `status: expired` in the database. The system must emit an `error` SSE event with `{"code": "HITL_TIMEOUT", "message": "...", "retryable": true}`. The user must be able to re-submit the original query to restart the flow.  
**Acceptance criterion:** A test mocks the timeout by advancing time past 10 minutes and asserts: (a) the checkpoint `status` column is set to `"expired"`, (b) a subsequent `POST /sessions/{session_id}/approve` with the expired `approval_id` returns HTTP 410 (Gone), and (c) a new `POST /chat` with the same query succeeds and emits a fresh `approval_required` event.

---

### 5.4 Streaming

**FR-12** — Two-Step SSE Handshake  
**Priority:** P1  
Chat interaction must follow a two-step pattern: (1) `POST /chat` submits the message and returns HTTP 202 with `{"message_id": "<uuid>", "stream_url": "/api/v1/chat/stream?stream_id=<uuid>"}`. (2) The client opens the SSE stream by issuing `GET /chat/stream?stream_id=<uuid>`. This design decouples message submission from stream consumption, enabling robust reconnection without re-sending the message.  
**Acceptance criterion:** A test confirms that `POST /chat` returns HTTP 202 with a valid `stream_url` and does not open a streaming connection itself. A subsequent `GET` to the returned `stream_url` opens an SSE stream and delivers events.

---

**FR-13** — SSE Event Schema  
**Priority:** P1  
The SSE stream must emit events conforming to the following schema. Every event must carry an `id` field (monotonically increasing integer within the turn) and an `event` field matching one of the five named event types.

| Event type | Required payload fields | Condition |
|---|---|---|
| `thinking` | `node: str`, `elapsed_ms: int` | Emitted at entry of each graph node |
| `token` | `content: str` | Emitted for each incremental LLM output token |
| `tool_result` | `tool: str`, `result: any` | Emitted after each tool completes |
| `approval_required` | `approval_id: str`, `tool: str`, `description: str` | Emitted when HITL gate suspends |
| `error` | `code: str`, `message: str`, `retryable: bool` | Emitted on any recoverable or terminal failure |
| `done` | `message_id: str` | Emitted once when the turn completes |

**Acceptance criterion:** A test consumes a full SSE turn, parses every event, and asserts each event's payload contains exactly the required fields with the correct types.

---

**FR-14** — SSE Mid-Stream Reconnection with HITL Support  
**Priority:** P1  
Every SSE event must include a numeric `id` field. The server must store all events for the current turn in Redis with a dynamic TTL: (a) for streams without an `approval_required` event, the TTL is 5 minutes after the `done` event is emitted; (b) for streams with an `approval_required` event, the TTL is extended to 15 minutes after the request is initiated (to accommodate the 10-minute HITL timeout plus reconnection grace period). Every new event emitted extends the TTL. If a client reconnects with a `Last-Event-ID` header, the server must: (1) validate the `stream_id` belongs to the authenticated user or guest session making the request (return HTTP 403 otherwise); (2) replay all events with an `id` greater than the provided value, in order, without re-executing any graph nodes.  
**Acceptance criterion:** A test simulates a 8-minute HITL wait with a mid-wait disconnect and reconnect. The test asserts: (a) the SSE replay buffer is alive at minute 8 (not expired), (b) events are replayed correctly, (c) a reconnect attempt with `Last-Event-ID` from a different session returns HTTP 403, and (d) after the HITL timeout and no further interaction, the buffer expires after 15 minutes total.

---

### 5.5 Authentication and Session Management

**FR-15** — Guest Session Issuance  
**Priority:** P1  
When a request arrives without any `Authorization` header or session cookie, the system must automatically issue a signed guest JWT with claims `{"mode": "guest", "session_id": "<uuid v4>"}` and an expiry of 24 hours. No user record must be created in PostgreSQL. The token must be returned in the `POST /auth/guest` response and accepted on all subsequent endpoints.  
**Acceptance criterion:** A test calls `POST /auth/guest` without credentials and asserts: (a) HTTP 200, (b) the response contains a JWT, (c) decoding the JWT yields `mode: "guest"`, a valid UUID v4 `session_id`, and correct expiry, and (d) no row exists in the `users` table after the call.

---

**FR-16** — Authenticated User Login, Token Management, and Brute-Force Protection  
**Priority:** P1  
`POST /auth/login` must accept `{"email": str, "password": str}`. Passwords must be hashed using Argon2id (recommended) or bcrypt with cost ≥ 12. The response on successful authentication is `{"access_token": str, "refresh_token": str, "access_token_expires_in": int}`. The access token must expire in 1 hour and include a `jti` (JWT ID) claim. The refresh token must expire in 30 days, be stored server-side, and include a `jti` claim.

**Login security:** (a) On invalid credentials (email not found or password incorrect), the response must be HTTP 401 with identical response body and timing for both cases (use a dummy hash comparison to prevent timing attacks). (b) `POST /auth/login` must be rate-limited per IP and per email (e.g., 10 attempts per 15 minutes; see FR-31). (c) After 5 consecutive failed attempts, the email must be soft-locked for 15 minutes (see FR-31).

**Token refresh:** (a) `POST /auth/refresh` must accept a valid refresh token and return a new access token (with a fresh `jti`). (b) Refresh tokens must be rotated on use: each refresh call invalidates the presented refresh token and issues a new one. (c) The server must maintain a one-to-one mapping of valid refresh tokens per user; presenting a previously-invalidated token must return HTTP 401. (d) On `POST /auth/logout`, the presented access token's `jti` must be added to a Redis-backed blacklist with TTL equal to the token's remaining lifetime. All protected endpoints must check this blacklist.

**JWT claims:** All JWTs must include standard claims: `iss` (issuer, configured value), `aud` (audience, configured value), `nbf` (not-before, optional), and `exp` (expiry). All endpoints must validate `iss` and `aud` on every request; reject tokens with mismatches or missing values.

**Acceptance criterion:** Tests verify: (a) valid credentials return two tokens with correct expiry and `jti` claims; (b) invalid email and invalid password both return HTTP 401 with identical response bodies and timing; (c) refresh token rotation — old token is invalid after refresh; (d) access token blacklist — logout invalidates the access token immediately (tested with a fresh request using the old token); (e) JWT validation — a token with mismatched `iss` or `aud` is rejected with HTTP 401; (f) password hashing — the stored password does not equal the plaintext and uses Argon2id/bcrypt.

---

**FR-17** — Session Data Isolation  
**Priority:** P1  
All database queries for conversation history, checkpoints, and session metadata must be scoped by the `user_id` extracted from the validated JWT. A request with user A's valid token must never return, modify, or delete data belonging to user B.  
**Acceptance criterion:** A test creates sessions for two distinct users. Using user A's token, `GET /sessions` returns only user A's sessions. `GET /sessions/{session_id_of_B}` returns HTTP 403. `DELETE /sessions/{session_id_of_B}` returns HTTP 403. No query in the application uses unscoped `SELECT *` on user-owned tables.

---

**FR-18** — LangGraph Checkpointer Thread Keying  
**Priority:** P1  
LangGraph checkpointer threads must be keyed by the tuple `(user_id, session_id)` for authenticated users and `(guest_session_id_hash, session_id)` for guest users. Resuming a session must re-hydrate the agent state from the latest checkpoint so the LLM receives full prior context.  
**Acceptance criterion:** A test creates a two-turn conversation, terminates the process, restarts it, and resumes the session. The agent's response in turn 3 references context from turns 1 and 2.

---

### 5.6 Conversation History and State Persistence

**FR-19** — Session List Endpoint  
**Priority:** P1  
`GET /sessions` must return a paginated list of the authenticated user's sessions. Each entry must include: `session_id` (UUID v4), `title` (auto-generated from the first user message, truncated to 60 characters, with collision disambiguation), `created_at`, `last_accessed`, and `message_count`. The endpoint must not be accessible to guest users (returns HTTP 403).  
**Acceptance criterion:** A test with 25 seeded sessions calls `GET /sessions?limit=20` and asserts: (a) 20 sessions returned, (b) each entry has the five required fields and UUID v4 format for `session_id`, (c) sessions are ordered by `last_accessed` descending, (d) calling with a guest token returns HTTP 403, and (e) if two sessions have identical titles, one is suffixed with `(2)` to prevent confusion.

---

**FR-20** — Session Message History Endpoint with Scope Validation  
**Priority:** P1  
`GET /sessions/{session_id}/messages` must return the complete ordered message history for the session as an array of `{"role": "user" | "assistant" | "tool", "content": str, "timestamp": ISO8601, "tool_name": str | null}` objects. The endpoint must validate that `session_id` is a UUID v4 and belongs to the authenticated user (HTTP 403 for both cross-user access and invalid UUIDs). The endpoint must not be accessible to guests (HTTP 403).  
**Acceptance criterion:** A test creates a session with 3 turns, calls the endpoint, and asserts: (a) 6 messages returned (user + assistant per turn), (b) messages are ordered by timestamp ascending, (c) role values match the expected sequence, (d) tool-invocation turns include a non-null `tool_name`, (e) calling with a guest token returns HTTP 403, (f) calling with a different user's valid token returns HTTP 403 with no distinction from auth failure, and (g) calling with an invalid UUID format returns HTTP 403.

---

**FR-21** — Guest Session State in Redis  
**Priority:** P1  
Guest session LangGraph state must be stored exclusively in Redis under the key pattern `guest:{session_id_hash}:state` with a TTL of 24 hours. No guest conversation data must be written to PostgreSQL. When the Redis key expires, the session is irrecoverably lost and subsequent requests with that `session_id` must return HTTP 410 (Gone).  
**Acceptance criterion:** A test inspects the database after a guest session and asserts zero rows written to any conversation table. A test expires the Redis key manually and confirms `GET /chat/stream?stream_id=<old_id>` returns HTTP 410. A test with two guest sessions confirms isolation: accessing one session's stream with another session's token returns HTTP 403.

---

**FR-22** — Conversation Retention Background Job with Atomic Deletion  
**Priority:** P2  
A background job must run daily and permanently delete conversation records (including all associated checkpoints and messages) where both of the following conditions are true: (a) `last_accessed` is more than 90 days before the job's execution date, and (b) `access_count` is less than 5. The deletion must be atomic using a single SQL statement with `FOR UPDATE SKIP LOCKED` to prevent deletion of in-progress sessions. Additionally, the job must not delete a session that has an open, non-expired HITL checkpoint. Users may manually delete any conversation at any time via `DELETE /sessions/{session_id}`.  
**Acceptance criterion:** A test seeds four conversations covering all combinations of the two conditions and one with an open HITL checkpoint. The job is invoked directly. Only the conversation meeting both purge conditions (and without an open HITL) is deleted. The other three remain intact. A separate test confirms that an in-progress session is skipped even if both purge conditions are met.

---

### 5.7 LLM Model Selection and Fallback

**FR-23** — Primary Model Configuration  
**Priority:** P1  
The default LLM must be `gpt-4o-mini`, configurable via the environment variable `OPENAI_DEFAULT_MODEL`. The OpenAI API key must be read from `OPENAI_API_KEY`. All calls to OpenAI models must be subject to the rate limits defined in FR-27.  
**Acceptance criterion:** A test confirms that the model name sent in the OpenAI API request payload matches the value of `OPENAI_DEFAULT_MODEL`. A separate test overrides the variable to `gpt-3.5-turbo` and asserts the new model name is used.

---

**FR-24** — Fallback Model Chain and Timeout Handling  
**Priority:** P1  
Fallback models must be specified as an ordered JSON list in the environment variable `FALLBACK_MODELS` (example: `["groq/llama-3-70b", "ollama/mistral"]`). Fallback is triggered automatically when the primary model returns HTTP 429 or HTTP 503. When the primary model call does not return a first token within 30 seconds (timeout), the system must: (a) emit a `thinking` SSE event with `{"node": "queued", "reason": "primary_llm_unavailable"}`; (b) display a popup to the user offering the option to immediately switch to the first fallback model; (c) if the user does not act within 60 seconds, automatically attempt the first fallback model. Rate limits (FR-27) must not apply to fallback model calls.  
**Acceptance criterion:** A test mocks the primary OpenAI endpoint to hang indefinitely. The system retries twice (per FR-29), then at 30 seconds timeout, emits the `queued` thinking event and displays a model-switch popup. The test asserts: (a) the popup is presented, (b) if the user does not act for 60 seconds, the fallback model is attempted automatically, and (c) the fallback call is not counted against quota.

---

**FR-25** — Model Switching Mid-Session  
**Priority:** P2  
`POST /sessions/{session_id}/model` must accept `{"model": str}` where `model` is a value from the registered model list (see FR-33). On success, all subsequent turns in the session must use the specified model. The active model must be persisted in the session record in PostgreSQL for authenticated users. When switching, the full prior conversation context must be included in the next request to the new model.  
**Acceptance criterion:** A test completes one turn with `gpt-4o-mini`, posts a model switch to a mocked fallback model, then issues a second turn. The second LLM call is made to the fallback model and includes the message history from turn 1 in its payload.

---

### 5.8 Rate Limiting and Token Quotas

**FR-26** — Per-Window OpenAI Quota Enforcement  
**Priority:** P1  
The system must enforce the following quotas exclusively on OpenAI model calls, tracked per authenticated `user_id` or per guest `session_id` AND per-IP address (see FR-31), using sliding-window counters stored in Redis.

| Window | Max requests | Max tokens |
|---|---|---|
| 4 hours | 20 | 80,000 |
| 24 hours | 60 | 200,000 |
| 7 days | 200 | 600,000 |

When any quota is exceeded, the API must return HTTP 429 with a JSON body containing `{"quota_type": str, "current": int, "limit": int, "reset_at": ISO8601}`. The client must display a non-technical message and the exact time until quota reset. Fallback model calls must not be counted.  
**Acceptance criterion:** A test issues 21 mocked OpenAI calls within a 4-hour window. The 21st call returns HTTP 429 with all four required fields. The fallback model is then called for the same request and succeeds without incrementing any counter. A separate test confirms that guest quotas also apply per-IP (see FR-31).

---

### 5.9 Error Handling

**FR-27** — Tool Failure Retry with Exponential Backoff  
**Priority:** P1  
On any tool execution failure (exception or non-2xx response from an external API), the `error_handler` node must retry the tool call up to 3 times using exponential backoff with delays of 1 s, 2 s, and 4 s respectively. After 3 failed retries, the agent must emit an `error` SSE event and return a user-facing message such as "I was unable to retrieve that information right now. Please try again shortly." The original exception must be logged at `ERROR` level and traced in LangSmith.  
**Acceptance criterion:** A test mocks the weather API to fail on all calls. The test asserts: (a) exactly 3 retry attempts are made, (b) the delay between attempts matches 1 s / 2 s / 4 s within a 100 ms tolerance, and (c) an `error` SSE event is emitted with `retryable: true` after the third failure.

---

**FR-28** — LLM Failure Retry and Model Fallback  
**Priority:** P1  
On LLM call failure (exception, HTTP 5xx, or timeout after 30 seconds), the system must retry the same model up to 2 times with exponential backoff (2 s, 4 s) before triggering the fallback model chain (FR-24). If all fallback models also fail, the system must emit an `error` SSE event with `retryable: false` and a user-facing message, and terminate the stream.  
**Acceptance criterion:** A test mocks the primary model to fail on every call and all fallback models to fail on every call. The test asserts: (a) 2 retries on the primary, (b) each fallback in `FALLBACK_MODELS` is attempted once, and (c) the final SSE event is `error` with `retryable: false`.

---

**FR-29** — Standardised Error Response Schema  
**Priority:** P1  
All non-SSE API errors must return a JSON body conforming to the following schema. No endpoint may return a bare HTTP error with an empty or plain-text body.

```json
{
  "error": {
    "code": "<SCREAMING_SNAKE_CASE string>",
    "message": "<human-readable string>",
    "retryable": true | false,
    "retry_after_seconds": <int | null>
  }
}
```

**Acceptance criterion:** A test triggers each category of API error (401, 403, 404, 410, 422, 429, 500) and asserts every response body deserialises to the schema above with all four fields present and correctly typed.

---

**FR-30** — Authentication Rate Limiting and Brute-Force Lockout  
**Priority:** P1  
`POST /auth/login` must enforce rate limiting and account lockout to prevent brute-force attacks: (a) per-IP rate limit: max 10 login attempts per 15 minutes from a single IP address; (b) per-email rate limit: max 10 login attempts per 15 minutes for the same email address (even from different IPs); (c) soft-lock: after 5 consecutive failed attempts for the same email, the email must be locked for 15 minutes, rejecting all login requests with HTTP 429 (not 401, to avoid exposing the lock state to attackers); (d) lock duration is configurable via `AUTH_LOCKOUT_MINUTES` environment variable. Quota enforcement is stored in Redis with appropriate TTLs.  
**Acceptance criterion:** Tests verify: (a) 10 failed attempts per IP are allowed before rate limit, 11th returns HTTP 429; (b) 5 failed attempts for one email lock the email for 15 minutes; (c) during lockout, a login attempt returns HTTP 429 with the same response body as a quota-exceeded error (do not distinguish); (d) IP-based and email-based counters are independent; (e) a successful login resets both counters.

---

**FR-31** — Guest Quota Enforcement Per IP Address  
**Priority:** P1  
Guest quotas (FR-26) are tracked per `session_id`. However, to prevent quota bypass by repeatedly obtaining new guest tokens, guest OpenAI quotas must additionally be enforced per source IP address using separate Redis counters keyed by `guest_quota:{ip_hash}:{window}`. When either the session-based or IP-based quota is exceeded, HTTP 429 must be returned. Document the limitation of IP-based enforcement (shared NAT scenarios) as a residual risk.  
**Acceptance criterion:** A test simulates a guest user who exhausts the 4-hour quota (20 requests), obtains a new guest token, and attempts 5 more requests. The IP-based counter prevents these from succeeding (HTTP 429 on the 21st request from that IP, regardless of session token). A separate test confirms that two guests from different IPs have independent quotas.

---

**FR-32** — HITL Audit Log  
**Priority:** P1  
Every HITL decision (approve, deny, or timeout) must be recorded in a dedicated `hitl_audit_log` table with columns: `id` (UUID primary key), `approval_id` (FK), `user_id` (or null for guests), `session_id`, `tool_name`, `decision` (`approve` | `deny` | `timeout`), `decided_at` (timestamp), `request_ip` (client IP), `decision_reason` (optional user-provided text). This table must be append-only (no UPDATE or DELETE). A corresponding log line at `INFO` level must be written per NFR-18.  
**Acceptance criterion:** A test approves a HITL action and asserts: (a) a row is inserted into `hitl_audit_log` with all required fields, (b) the `decided_at` timestamp is within 1 second of the approval request, and (c) the row cannot be updated or deleted (enforced via database permissions or application logic).

---

**FR-33** — Tool Registry Endpoint and FALLBACK_MODELS Startup Validation  
**Priority:** P1  
The system must expose a `GET /tools` endpoint (accessible to both authenticated and guest users) that returns a JSON array of registered tools. Each tool entry must include: `name`, `description`, `is_sensitive` (boolean). Internal configuration fields (API keys, endpoint URLs) must not be exposed.

Additionally, at application startup, the `FALLBACK_MODELS` environment variable must be validated: (a) if not set, log a warning and set `fallback_enabled: false`; (b) if set to malformed JSON, the application must exit with a non-zero code and log the parse error; (c) if set to an empty list, log a warning; (d) if set to a non-empty list, validate that each model identifier is syntactically valid (e.g., `provider/model-name`).  
**Acceptance criterion:** A test calls `GET /tools` and asserts it returns a valid JSON array with the expected fields, excluding internal config. A separate test confirms that malformed `FALLBACK_MODELS` JSON causes the application to fail startup. A third test confirms that the system functions correctly with `fallback_enabled: false` when no fallback models are configured.

---

## 6. Non-Functional Requirements

---

### 6.1 Performance

**NFR-1** — Time to First Token  
The system must emit the first `token` SSE event within **1.5 seconds** of the client opening the `GET /chat/stream` connection, measured on a warm server with no tool calls required (direct LLM response path). This target applies at the p95 percentile under single-user load.  
**Test:** An integration test with a mocked LLM that returns instantly measures the interval from SSE connection open to first `token` event and asserts it is ≤ 1,500 ms.

---

**NFR-2** — Thinking Indicator Latency  
The first `thinking` SSE event must be emitted within **500 ms** of the server receiving the `POST /chat` request, regardless of query complexity or tool count.  
**Test:** A test measures time from HTTP request receipt (mocked server clock) to first `thinking` event and asserts ≤ 500 ms across 10 consecutive calls.

---

**NFR-3** — Tool Execution Latency  
Individual tool execution (excluding retries) must complete within **3 seconds** at the p95 percentile under normal operating conditions. This target is measured from the moment `tool_executor` is entered to the moment it exits.  
**Test:** A load test with mocked tools that introduce a 2.8 s delay confirms 95% of tool calls complete within 3 s.

---

**NFR-4** — HITL Approval Round-Trip  
After the user submits `POST /sessions/{session_id}/approve`, the graph must resume and emit the next SSE event within **500 ms**.  
**Test:** A test records the timestamp of the HTTP response to the approval POST and the timestamp of the subsequent SSE event; asserts the delta is ≤ 500 ms.

---

**NFR-5** — SSE Reconnection Time  
After a client disconnect, a reconnecting client supplying `Last-Event-ID` must begin receiving replayed events within **2 seconds**.  
**Test:** A test simulates disconnect and reconnect and measures the time from the reconnect GET request to the first replayed SSE event.

---

### 6.2 Scalability

**NFR-6** — Stateless Application Process  
The FastAPI process must hold no user-specific state in process memory. All session state, quotas, SSE replay buffers, and checkpoints must reside in PostgreSQL or Redis. Adding a second application instance must not require sticky sessions or any inter-process coordination. LangGraph checkpointer concurrency must be handled via optimistic locking (version conflict detection and retry on the client, or via explicit distributed locks on `(user_id, session_id)` pairs).  
**Test:** Two in-process application instances sharing the same mocked PostgreSQL and Redis are tested by submitting concurrent requests to the same `session_id` to instance A and instance B; the system must either serialize the requests or detect and report a conflict.

---

**NFR-7** — Connection Pooling  
The application must use async connection pooling for both PostgreSQL (`asyncpg` pool, min 2, max 10 connections) and Redis (`redis-py` async pool, min 2, max 10 connections). Connection parameters must be configurable via environment variables `DB_POOL_MIN`, `DB_POOL_MAX`, `REDIS_POOL_MIN`, `REDIS_POOL_MAX`.  
**Test:** A test inspects the pool configuration at startup and asserts pool sizes match the environment variable values.

---

**NFR-8** — Redis Key TTL Enforcement and Dynamic Extension  
Every Redis key written by the application must have an explicit TTL set at write time. No Redis key may be written without a TTL. For HITL scenarios, the SSE replay buffer TTL must be dynamically extended (using Redis `EXPIRE` or `EXPIREAT` on each event write) to ensure the buffer survives the full HITL timeout plus a reconnection grace period. TTL values are as follows: guest session state — 24 hours; SSE replay buffer (non-HITL) — 5 minutes after `done` event; SSE replay buffer (HITL) — minimum 15 minutes from request initiation, extended on every event write; quota counters — matching the window duration; failover queue entries — 60 seconds; approval IDs — 10 minutes.  
**Test:** A test intercepts all Redis key writes during a HITL flow and asserts: (a) each key has a TTL, (b) the TTL is extended on every new event emission, and (c) the buffer survives a 10-minute HITL wait with a mid-wait reconnect.

---

### 6.3 Reliability

**NFR-9** — Graceful Shutdown  
On receiving `SIGTERM`, the application must: (a) stop accepting new connections, (b) allow in-flight SSE streams to emit a `done` or `error` event and close, (c) persist all pending HITL checkpoint states to PostgreSQL, and (d) close all database and Redis connections cleanly. The process must exit within 30 seconds of receiving `SIGTERM`.  
**Test:** A test sends `SIGTERM` to the application mid-stream and asserts the stream emits either `done` or `error` before the connection closes, and all open checkpointer threads are flushed to the database.

---

**NFR-10** — Database Migration on Startup and Rollback Testing  
The application must run Alembic migrations to the latest revision before the first HTTP request is served. If migrations fail, the application must exit with a non-zero code and must not accept traffic. Every Alembic migration file must include a working `downgrade()` function (even if it is a no-op stub with a clear comment). The CI pipeline must run `alembic downgrade -1` after `alembic upgrade head` in the test database and verify the database returns to revision N-1 without error (except for forward-only operations, which may raise `NotImplementedError` with a documented reason).  
**Test:** A test starts the application against a database at revision N-1 and asserts it reaches revision N before `/readiness` returns HTTP 200. A separate test runs the downgrade path and asserts idempotency.

---

### 6.4 Security

**NFR-11** — No Dynamic Code Evaluation in Calculator  
The `CalculatorTool` must not call `eval()`, `exec()`, `compile()`, or any equivalent dynamic code execution primitive. Any expression that is not a valid arithmetic expression (numbers, operators, parentheses, and standard math functions) must raise a `ValueError` before evaluation.  
**Test:** Static analysis (`grep` or AST scan) confirms no `eval`/`exec` calls exist in `CalculatorTool`. A unit test passes an injection string and asserts `ValueError` is raised.

---

**NFR-12** — Secrets Never Hardcoded  
No secret value (API key, database password, JWT secret, etc.) may appear as a literal string in any source file. All secrets must be read from environment variables at runtime. The CI pipeline must scan for hardcoded secrets using `detect-secrets` (or `gitleaks`), with a committed baseline file.  
**Test:** The CI pipeline runs the secrets scanner on every push to `main` and fails if any secret pattern is detected beyond the approved baseline.

---

**NFR-13** — SQL Injection Prevention  
All database interactions must use SQLAlchemy ORM methods or parameterised statements. No SQL query may be constructed by string concatenation or f-string formatting using user-supplied values.  
**Test:** A code review rule (enforced via a custom `ruff` or `pylint` rule or a CI grep) rejects any `text()` call or f-string containing the substring `SELECT`, `INSERT`, `UPDATE`, or `DELETE`.

---

**NFR-14** — CORS Restriction  
In production, the `Access-Control-Allow-Origin` header must be set to the exact frontend origin (e.g., `https://<app>.onrender.com`) and must never be `*`. In development, a configurable `CORS_ORIGINS` environment variable controls the allowed list.  
**Test:** A test issues a cross-origin preflight request from an unlisted origin and asserts the response does not include `Access-Control-Allow-Origin`.

---

**NFR-15** — Input Size Validation and Sanitisation  
All API endpoints must reject request bodies exceeding configurable maximum sizes and sanitise input to prevent injection attacks. Defaults: chat message body ≤ 4,000 characters; session title ≤ 60 characters; approval decision body ≤ 512 bytes. All input must be sanitised by: (a) rejecting null bytes (`\x00`), (b) stripping leading/trailing whitespace, (c) normalising Unicode to NFC form. Request bodies exceeding size limits must return HTTP 422 with the standard error schema.  
**Acceptance criterion:** Tests verify: (a) a chat message of 4,001 characters is rejected with HTTP 422, (b) input with null bytes is rejected with HTTP 422, and (c) multi-byte Unicode is normalised without altering the string's meaning.

---

**NFR-16** — HITL Approval ID Integrity  
The `approval_id` issued in an `approval_required` event must be a version-4 UUID generated server-side. The approval endpoint must validate atomically that: (a) the `approval_id` exists in the database and matches the session, (b) it has not already been used, and (c) it has not expired (FR-11). The validation must use a single database operation (e.g., `UPDATE ... WHERE id=? AND used=false AND expired=false RETURNING id`). Any invalid, replayed, or expired `approval_id` must return HTTP 410 (Gone) without revealing which check failed.  
**Test:** Tests assert: (a) a reused `approval_id` returns HTTP 410, (b) a fabricated UUID returns HTTP 410, and (c) a valid `approval_id` from a different session returns HTTP 403 with the same HTTP status as other 403 errors.

---

### 6.5 Observability

**NFR-17** — LangSmith Tracing  
Every LangGraph run must be traced in LangSmith with the following metadata fields: `run_name` (session ID), `tags` (list of tool names used), and `metadata` containing `user_id`, `session_id`, `model`, and `turn_index`. Tracing must be enabled in production and configurable via `LANGCHAIN_TRACING_V2=true` and `LANGSMITH_API_KEY`. Tracing must be non-blocking (fire-and-forget); failures to reach LangSmith must not propagate to request handlers and must be logged at `WARNING` level.  
**Test:** A test with LangSmith tracing mocked asserts that the LangSmith client is called with a payload containing all five required metadata fields. A separate test mocks LangSmith to fail and asserts the request still completes successfully.

---

**NFR-18** — Structured Request Logging  
Every HTTP request must produce a single structured JSON log line at `INFO` level containing: `request_id` (UUID), `user_id` (or `"guest"`), `session_id`, `method`, `path`, `status_code`, and `latency_ms`. Sensitive fields (passwords, tokens, API keys) must never appear in logs. Log level must be configurable via the `LOG_LEVEL` environment variable (default: `INFO`).  
**Test:** A test issues one request and captures the log output; asserts the log line is valid JSON and contains all seven required fields.

---

## 7. System Constraints

| ID | Constraint |
|---|---|
| **SC-1** | The system must be deployable on Render's free or starter tier without requiring a dedicated server or custom infrastructure. |
| **SC-2** | The backend language is Python 3.11. No other Python version is supported. |
| **SC-3** | The agent framework is LangGraph. LangChain is used for chain/tool tooling only. The agent graph may not be reimplemented in a different framework without a full spec revision. |
| **SC-4** | The API framework is FastAPI with async handlers throughout. Synchronous route handlers are not permitted. |
| **SC-5** | The primary LLM provider is OpenAI. The API key is user-supplied and must not be committed to the repository. |
| **SC-6** | The Tavily API key must be user-supplied via environment variable. A missing key must cause the web search tool to return a configuration error, not a runtime crash. |
| **SC-7** | PostgreSQL is the sole relational database. No other relational database engine may be introduced. |
| **SC-8** | Redis is the sole caching and ephemeral state store. No in-memory alternatives (e.g., Python dicts, `lru_cache` for session data) may substitute for Redis. |
| **SC-9** | The CI/CD platform is GitHub Actions. Deployment to Render must be gated on all CI stages passing. |
| **SC-10** | All public functions, methods, and classes must carry type annotations and docstrings. `mypy` must report zero errors in strict mode. |

---

## 8. Assumptions

| ID | Assumption |
|---|---|
| **A-1** | The system will serve a single user (the developer) during the prototype phase; traffic will not exceed 5 concurrent sessions. |
| **A-2** | The OpenAI API is available and responsive the majority of the time; fallback models handle the minority of downtime cases. |
| **A-3** | The Tavily free tier provides sufficient request volume for personal use. |
| **A-4** | Render's managed PostgreSQL and Redis instances provide adequate durability and availability for a prototype (no multi-region replication required). Redis `maxmemory-policy` is configured as `noeviction` or `volatile-ttl`, not `allkeys-lru` or `allkeys-random`. |
| **A-5** | The developer will supply valid API keys for OpenAI, Tavily, and LangSmith via environment variables before first deployment. |
| **A-6** | The web UI client supports SSE natively (modern browser with `EventSource` API). No IE11 or legacy browser support is required. |
| **A-7** | Fallback models (Groq, Ollama) are accessible from the Render deployment environment without additional firewall configuration. |
| **A-8** | LangSmith's free tier retention policy is sufficient for the observability needs of this prototype. |
| **A-9** | LangGraph concurrency: when multiple requests target the same `(user_id, session_id)` simultaneously, the system either serialises them using a distributed lock or relies on LangGraph's optimistic locking with conflict detection. The chosen strategy is documented in `DESIGN.md`. |

---

## 9. Out of Scope

| ID | Item |
|---|---|
| **OOS-1** | File upload, image analysis, or document processing of any kind. |
| **OOS-2** | Multi-user admin dashboard, user management UI, or content moderation tooling. |
| **OOS-3** | Billing, subscription management, or payment processing. |
| **OOS-4** | Voice input, speech-to-text, or text-to-speech output. |
| **OOS-5** | Push notifications via email, SMS, or mobile. |
| **OOS-6** | Multi-language or internationalisation support. |
| **OOS-7** | Native mobile application (iOS or Android). |
| **OOS-8** | Fine-tuning, custom model training, or prompt dataset management. |
| **OOS-9** | Prometheus `/metrics` endpoint (architecture must not preclude it, but it will not be implemented in v1). |
| **OOS-10** | Multi-region or high-availability deployment topology. |
| **OOS-11** | Any tool beyond weather lookup, calculator, and web search. New tools may be added post-v1 using the extensibility interface (FR-1). |

---

## 10. Success Metrics

| ID | Metric | Target | Measurement method |
|---|---|---|---|
| **SM-1** | Time to first `token` SSE event (p95, single user) | ≤ 1,500 ms | Automated integration test measuring SSE event timestamps |
| **SM-2** | First `thinking` event latency (p95) | ≤ 500 ms | Automated integration test |
| **SM-3** | Tool execution completion rate (non-HITL tools, no network errors) | ≥ 99% | Count of successful tool results vs. total tool invocations in LangSmith |
| **SM-4** | HITL approval flow success rate (user approves, tool executes) | ≥ 95% | Ratio of completed HITL approvals to initiated HITL pauses |
| **SM-5** | SSE reconnection success rate | ≥ 99% | Ratio of successful replays to total reconnect attempts with valid `Last-Event-ID` |
| **SM-6** | CI pipeline pass rate | 100% on `main` | GitHub Actions run history |
| **SM-7** | Test code coverage | ≥ 80% | `pytest --cov` report in CI |
| **SM-8** | `mypy` errors in strict mode | 0 | CI `mypy` step output |
| **SM-9** | Conversation resumption accuracy | Automatable test: agent references prior entities | A two-turn conversation with a named entity in turn 1, resume after restart, send turn 3 with entity reference; assert entity is in LLM response. |
| **SM-10** | Fallback model activation on primary failure | Fallback invoked within 35 s of primary timeout | Automated test asserting fallback call is made after mocked primary timeout |

---

## 11. Risks

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| **R-1** | OpenAI API outage causes complete service degradation | Medium | High | FR-24 fallback model chain; fallover queue; user-facing model-switch popup |
| **R-2** | LangGraph state schema changes in a minor version break existing PostgreSQL checkpoints | Medium | High | Pin LangGraph to an exact version in `requirements.txt`; test checkpoint round-trip on every dependency upgrade |
| **R-3** | Tavily API rate limits or downtime degrade web search for the duration of the outage | Low | Medium | FR-27 retry with backoff; graceful user-facing error message; HITL denial path allows user to skip search |
| **R-4** | Redis eviction of quota counters under memory pressure bypasses cost controls | Medium | High | Configure Render Redis `maxmemory-policy: noeviction` or `volatile-ttl` (see A-4); startup check verifies this (REVIEW-25) |
| **R-5** | Guest session Redis TTL expiry causes loss of in-progress HITL state | Medium | Low | Emit clear user-facing message on HTTP 410; user can restart the query; authenticated users are unaffected |
| **R-6** | Render free tier cold-start latency (up to 30 s) causes the first request to time out | High | Medium | Health-check endpoint keeps the instance warm; document cold-start limitation in deployment notes |
| **R-7** | JWT secret rotation invalidates all active sessions | Low | Medium | Use refresh token rotation (FR-16); short access token TTL (1 h) limits the blast radius; document rotation procedure |
| **R-8** | Accumulating PostgreSQL checkpoints degrade query performance over time | Low | Medium | Retention job (FR-22) purges low-priority old conversations; index on `(user_id, session_id, last_accessed)` |
| **R-9** | Safe expression parser (`simpleeval`/`asteval`) has an unpatched vulnerability | Low | High | Pin parser version; monitor CVE feeds; the parser runs in a restricted scope with no `builtins` |

---

## 12. Deferred Decisions

The following MEDIUM and LOW findings from the spec review (`docs/SPEC_REVIEW.md`) have been deferred for post-v1 implementation or further analysis. Each is tagged with its review finding ID for reference.

### 12.1 Untestable Success Metrics (REVIEW-18, REVIEW-19)

**Items:** SM-9 originally relied on manual verification ("user-reported"). Updated per REVIEW-18 to an automatable test. However, production measurement of performance NFRs (NFR-1 through NFR-5) was only specified for test environments. Production measurement methodology and alerting thresholds should be defined in a post-v1 observability runbook.

**Rationale:** These are observability enhancements that do not block v1 functionality. They are required for production SLA enforcement but can be added after the system is live and baseline metrics are captured.

---

### 12.2 Cross-Session SSE Replay (REVIEW-20)

**Item:** Prevent a client from reconnecting to one SSE stream using `Last-Event-ID` from a different stream. The spec now requires `stream_id` validation (updated FR-14), but cross-session token reuse is not explicitly prevented if stream IDs are predictable.

**Rationale:** UUID v4 stream IDs are cryptographically random; guessing another user's stream ID is infeasible. The `stream_id` validation in FR-14 is sufficient for v1. Further hardening (e.g., per-request signing of stream IDs) can be added if stream ID guessing becomes a concern.

---

### 12.3 Empty LLM Responses (REVIEW-21)

**Item:** Handle the case where the LLM returns a syntactically valid but semantically empty response (empty string, whitespace-only, or empty content array). Current spec treats this as a success; should be treated as an error and trigger retry/fallback logic.

**Rationale:** This is an edge case with low probability (LLM returns valid but empty). It can be addressed in a follow-up if telemetry shows it occurs in production. For v1, a best-effort approach (graceful logging) is acceptable.

---

### 12.4 Tool Result Context Window Overflow (REVIEW-22)

**Item:** When accumulated tool results exceed the LLM's context window, define a strategy: summarise older messages, drop oldest messages, or return a specific error. Current spec has no context management strategy.

**Rationale:** Context window overflow is unlikely for v1 (single-user, short conversations). This can be addressed when multi-turn conversations with multiple tools become common. For now, the LLM will return an HTTP 400 `context_length_exceeded` error, which FR-28 treats as a non-retryable error with a user-facing message.

---

### 12.5 Fallback Model List Validation Edge Cases (REVIEW-16, REVIEW-23)

**Item:** Distinguish between "no fallback models configured" (FALLBACK_MODELS empty) vs. "all fallback models failed". The spec now requires startup validation (new FR-33), but distinct error codes for these cases are not required for v1.

**Rationale:** Both scenarios result in the user receiving "retryable: false". Distinct error codes can be added in a future version when observability and fallback strategy are tuned based on real usage patterns.

---

### 12.6 Empty Tavily Result Set After Filtering (REVIEW-24)

**Item:** When all web search results fall below the relevance threshold or Tavily returns zero results, explicitly handle the empty case and inform the LLM not to hallucinate.

**Rationale:** The current spec relies on graceful error handling in FR-27 (tool failure). Explicit handling can be added after telemetry shows how often this occurs. For v1, returning an empty result set and letting the LLM handle it is acceptable.

---

### 12.7 Redis Eviction Policy (REVIEW-25)

**Item:** Explicitly configure Render's managed Redis to use `maxmemory-policy: noeviction` or `volatile-ttl` to prevent loss of quota counters. This is now mentioned in A-4 and the startup check, but detailed configuration instructions are deferred to `DESIGN.md`.

**Rationale:** Render's Redis configuration is set during deployment setup, not in code. Deferred to deployment documentation.

---

### 12.8 LangGraph Concurrency Model (REVIEW-26)

**Item:** Choose and document the strategy for handling concurrent requests to the same LangGraph thread: distributed lock (serialize) vs. optimistic locking (conflict detection and retry).

**Rationale:** Both strategies are valid; the choice depends on performance vs. consistency trade-offs. Updated A-9 to require this decision to be documented in `DESIGN.md`. Implementation decision deferred to architecture phase.

---

### 12.9 LangSmith Outage Resilience (REVIEW-27)

**Item:** Ensure LangSmith tracing failures do not propagate to request handlers. Updated NFR-17 to require non-blocking, fire-and-forget tracing.

**Rationale:** This is a best-practice approach already reflected in the updated spec. No further action required.

---

### 12.10 Guest Quota Bypass (REVIEW-28, now FR-31)

**Item:** Guest quotas tracked only per `session_id` can be bypassed by obtaining new tokens. Added FR-31 requiring per-IP quota enforcement for guests.

**Rationale:** FR-31 addresses this. See FR-31 for full details.

---

### 12.11 POST /chat Idempotency (REVIEW-29)

**Item:** Add an `Idempotency-Key` header to `POST /chat` to prevent duplicate message submissions on retried requests.

**Rationale:** This is a useful feature but adds complexity to the initial release. Deferring to v1.1. For now, HTTP 202 response includes a `message_id` that allows clients to detect retries via subsequent `GET /chat/stream` calls with the same `message_id`.

---

### 12.12 SSE Keep-Alive During HITL Wait (REVIEW-30)

**Item:** Emit `: keep-alive` SSE comment lines every 15 seconds during idle streams to prevent proxy timeout. This is important for long HITL waits (up to 10 minutes).

**Rationale:** This is a must-have for production (proxies and browsers drop idle connections after 30–60 seconds). However, implementation is straightforward. Recommend adding this before v1 ships, but if time is short, document as a known limitation in the deployment guide.

---

### 12.13 Session Title Collision Prevention (REVIEW-31, now FR-19)

**Item:** Auto-generated session titles can collide (identical first message → identical title). Updated FR-19 to require collision disambiguation via counter suffix.

**Rationale:** Updated in spec. No further action required.

---

### 12.14 JWT Storage and CSRF Protection (REVIEW-32)

**Item:** Specify how JWTs are stored (localStorage vs. HttpOnly cookie) and whether CSRF protection is required.

**Rationale:** This is a UI/client-side concern, not a backend spec detail. Deferred to client implementation guide. Recommended approach: store access token in localStorage (for API calls via `Authorization: Bearer` header); do not use cookies for API auth to avoid CSRF complexity.

---

### 12.15 Secrets Scanner Configuration (REVIEW-33)

**Item:** Specify which secrets scanner (`detect-secrets` vs. `gitleaks`), baseline file location, and exception process.

**Rationale:** CI/CD detail deferred to `.github/workflows/` configuration and a separate CI setup document.

---

### 12.16 LLM Retry Count Ambiguity (REVIEW-34)

**Item:** Retry counts for LLM failures are specified in multiple FRs (FR-24, FR-28, FR-29) with potential inconsistency.

**Rationale:** Updated FRs to remove ambiguity. Primary model retries 2 times (per FR-28); then fallback models are attempted (per FR-24). No further action required.

---

### 12.17 Token Counting Approximation (REVIEW-35)

**Item:** Token counts from OpenAI API may be approximations. Quota enforcement can drift if tokens are under/over-counted.

**Rationale:** Deferred to monitoring and tuning in v1.1. For v1, document that a 5% tolerance is acceptable (quotas are soft limits, not hard). Future work: implement token counting reconciliation if drift becomes measurable.

---

### 12.18 Formal GET /models Specification (REVIEW-36, now FR-33)

**Item:** `GET /models` (or `/tools`) endpoint referenced in FR-5 but never formally specified. Updated FR-33 to formally specify `GET /tools`.

**Rationale:** Addressed in updated spec (FR-33). No further action required.

---

### 12.19 Load-Shedding and Backpressure (REVIEW-37)

**Item:** No load-shedding mechanism specified. Under sustained load, connection pools exhaust and the system cascades into failure.

**Rationale:** For a single-user prototype (A-1), load-shedding is not critical for v1. Recommend adding HTTP 503 + `Retry-After` when connection pools are exhausted, but can be deferred to v1.1 or monitoring-based tuning.

---

### 12.20 Health Check Depth (REVIEW-38)

**Item:** `/readiness` endpoint referenced but never formally specified. Shallow health checks cause Render to route traffic to unhealthy instances.

**Rationale:** This is important for production reliability. Recommend implementing before v1 ships:  
- `GET /liveness` — returns HTTP 200 if process is running (no external checks).  
- `GET /readiness` — verifies PostgreSQL connectivity, Redis connectivity, and Alembic head revision match. Returns HTTP 503 if any check fails.

---

### 12.21 Input Sanitisation Details (REVIEW-14, now NFR-15)

**Item:** Prevent log injection, prompt injection, and other input-based attacks. Updated NFR-15 to specify sanitisation requirements (null bytes, Unicode normalisation).

**Rationale:** Addressed in updated spec (NFR-15). Prompt injection is a residual risk; documented as such in the implementation.

---

---

## 13. Glossary

| Term | Definition |
|---|---|
| **Checkpoint** | A serialised snapshot of a LangGraph `StateGraph` state written to PostgreSQL, enabling the graph to pause and resume at the exact node and state where it stopped. |
| **Ephemeral session** | A guest session whose agent state exists only in Redis with a 24-hour TTL and is not persisted to PostgreSQL. |
| **Fallback model** | A free or open-source LLM (e.g., served via Groq or Ollama) used when the primary OpenAI model is unavailable or rate-limited. Not subject to quota enforcement. |
| **HITL** | Human-in-the-Loop — a design pattern in which the agent graph suspends execution and waits for an explicit end-user decision before proceeding with a sensitive action. |
| **Priority score** | A computed value combining `last_accessed` recency and `access_count` frequency, used by the retention background job to determine which conversations to purge. |
| **Relevance threshold** | The minimum Tavily result score (default 0.7) below which a web search result is discarded before being passed to the LLM. |
| **SSE** | Server-Sent Events — a unidirectional HTTP streaming protocol where the server pushes a sequence of named, typed events to the client over a single long-lived HTTP connection. |
| **StateGraph** | The LangGraph construct representing the agent as a directed graph of nodes (processing steps) connected by conditional edges. |
| **Tool** | A discrete, self-contained capability the agent can invoke (weather lookup, calculator, web search). Each tool declares whether it is sensitive and implements the `BaseTool` interface. |
| **Two-step SSE handshake** | The pattern in which `POST /chat` returns a `stream_url` and `GET <stream_url>` opens the SSE stream, decoupling message submission from stream consumption to enable robust reconnection. |
