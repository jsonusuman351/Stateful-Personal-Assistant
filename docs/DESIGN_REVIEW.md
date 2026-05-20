# Design Review: Stateful Personal Assistant
## Adversarial Audit of `docs/DESIGN.md` v1.0

**Review date:** 2026-05-20  
**Reviewer:** Senior Principal Engineer (Adversarial Audit)  
**Design version audited:** 1.0 (Draft)  
**Spec version cross-referenced:** SPEC.md v1.1

---

## Summary Table

| Finding ID | Severity | One-line Description | Affected Design Section / Requirement |
|---|---|---|---|
| REVIEW-1 | CRITICAL | `interrupt_before=["hitl_gate"]` contradicts the design's own description of `hitl_gate` doing work before suspension | §2.1, §2.3, FR-9 |
| REVIEW-2 | CRITICAL | HITL resume re-evaluates `route_after_hitl` on stale state, creating an infinite suspension loop | §2.4, FR-10 |
| REVIEW-3 | CRITICAL | `tool_calls`, `tool_results`, `pending_approval`, and `error` have no reducer — last-write-wins silently drops data under parallel writes | §2.2, FR-7 |
| REVIEW-4 | CRITICAL | SSE emission mechanism inside nodes is unspecified — no design for how nodes push events to the SSE stream | §2.3, FR-8, FR-13 |
| REVIEW-5 | CRITICAL | `error_handler` implements retry via blocking inline sleep, making async retry loops an event-loop blocker | §2.3, FR-27 |
| REVIEW-6 | HIGH | `thread_id` colon-delimiter format is ambiguous when `user_id` or `session_id` contains a colon | §2.2, §5.2 DDL, FR-18 |
| REVIEW-7 | HIGH | HITL distributed lock TTL of 30 seconds is too short for slow checkpoint resumption; expired lock mid-resume leaves the graph in an inconsistent state | §5.3 Redis, FR-10 |
| REVIEW-8 | HIGH | `hitl_audit_log` "append-only" is enforced at application layer only — no database-level constraint prevents UPDATE/DELETE | §5.2 DDL, FR-32 |
| REVIEW-9 | HIGH | `asyncio.gather()` for parallel tools has no per-tool timeout — one hanging tool blocks all tool results indefinitely | §2.3, FR-7, NFR-3 |
| REVIEW-10 | HIGH | `route_after_tools` routes to `error_handler` on any partial failure, discarding all successful tool results | §2.4, FR-7, FR-27 |
| REVIEW-11 | HIGH | `route_after_router` conditional edge has no default/fallback mapping — unexpected return value raises an uncaught `KeyError` at runtime | §2.4, FR-6 |
| REVIEW-12 | HIGH | `llm_response: Optional[str]` creates a second source of truth for the assistant turn alongside `messages` | §2.2, FR-13 |
| REVIEW-13 | HIGH | `astream()` vs. `astream_events()` is inconsistent across the design — the correct streaming API is never settled | §2.1, §10.1, FR-13 |
| REVIEW-14 | HIGH | DB connection pool exhaustion: concurrent SSE streams holding HITL-suspended connections for up to 10 minutes will exhaust the pool of 10 | §5.3, NFR-7, NFR-6 |
| REVIEW-15 | HIGH | Guest session state stored in `AgentState.thread_id` as `"guest:{hash}:{session_id}"` — the hash function is unspecified; MD5/SHA1 are not collision-resistant | §2.2, §5.2 DDL, FR-21 |
| REVIEW-16 | MEDIUM | `stream_id` in `AgentState` couples infrastructure state to agent state, causing it to be checkpointed and replayed — an SSE implementation detail embedded in graph state | §2.2, FR-14 |
| REVIEW-17 | MEDIUM | `thread_id` stored redundantly in `AgentState` — LangGraph already provides it via `RunnableConfig`; creates a divergence risk | §2.2, FR-18 |
| REVIEW-18 | MEDIUM | SSE replay buffer uses Redis List append + `LRANGE` — O(N) range scan on lists with thousands of token events per turn may be slow | §5.3, §10.3, NFR-5 |
| REVIEW-19 | MEDIUM | Node side-effects (SSE emission) are not idempotent — checkpoint replay after a crash re-emits duplicate SSE events to the client | §2.3, FR-9, FR-13 |
| REVIEW-20 | MEDIUM | `tools.yaml` config path is not described as environment-variable-overridable; a path traversal in a future endpoint could allow malicious tool registration | §2.5, FR-5 |
| REVIEW-21 | MEDIUM | Input sanitisation order is inverted: Pydantic runs before null-byte rejection, so null bytes pass Pydantic validators before being caught by middleware | §9.6, NFR-15 |
| REVIEW-22 | MEDIUM | Retention job performs an unbatched DELETE on large tables with `FOR UPDATE SKIP LOCKED` — a single statement locking thousands of rows can cause table-level lock contention | §5.2 DDL, FR-22 |
| REVIEW-23 | MEDIUM | `jti` blacklist check on every request is a synchronous Redis call on the hot path — not pipelined or batched | §9.2, FR-16 |
| REVIEW-24 | MEDIUM | `error_handler` has three responsibilities (retry, fallback, user-facing message) but `ErrorState` has no `source` field to distinguish tool vs. LLM vs. HITL failure | §2.3, §2.2, FR-27, FR-28 |
| REVIEW-25 | MEDIUM | LangGraph graph is compiled once at startup and shared across concurrent async requests — no documentation of whether this is concurrency-safe | §2.1, NFR-6 |
| REVIEW-26 | MEDIUM | `decision_reason` in `hitl_audit_log` is user-supplied but no sanitisation or length cap is described | §5.2 DDL, FR-32, NFR-15 |
| REVIEW-27 | MEDIUM | LangGraph concurrency strategy (distributed lock vs. optimistic locking) is deferred per SPEC.md §12.8 but DESIGN.md never fills in the decision | §2.1, NFR-6 |
| REVIEW-28 | LOW | `WebSearchTool` uses `TavilyClient` (sync import) — `async def execute` must use the async Tavily client or `asyncio.to_thread`, neither of which is specified | §2.5, FR-4, SC-4 |
| REVIEW-29 | LOW | No rate limit design for `GET /chat/stream` — opening thousands of idle SSE connections is a DoS vector | §6.2, §9, NFR-6 |
| REVIEW-30 | LOW | FR-19 requires session titles truncated to 60 characters with collision disambiguation — `conversations` table DDL has `title VARCHAR(100)`, inconsistent with the spec | §5.2 DDL, FR-19 |
| REVIEW-31 | LOW | FR-11 requires an `expired` status column on the checkpoint; the `hitl_approvals` DDL has a boolean `expired` flag but the retention job (FR-22) must not delete sessions with open non-expired HITL — the join condition for this check is never described | §5.2 DDL, FR-11, FR-22 |
| REVIEW-32 | LOW | `POST /sessions/{session_id}/model` stores `active_model` in `conversations.active_model` (PostgreSQL) but `AgentState.active_model` is set at graph construction time — there is no design for how a model switch propagates into a running graph checkpoint | §5.2 DDL, §6.3, FR-25 |

---

## Detailed Findings

---

### REVIEW-1

**ID:** REVIEW-1  
**Severity:** CRITICAL  
**Affected section:** §2.1 LangGraph StateGraph Design, §2.3 Node Responsibilities (`hitl_gate`), FR-9  
**Category:** LangGraph Design

**Finding:**  
The design specifies `interrupt_before=["hitl_gate"]` in the `builder.compile()` call. With `interrupt_before`, LangGraph suspends the graph **before the named node executes** — `hitl_gate` never runs. Yet §2.3 describes `hitl_gate` as the node that (1) generates the `approval_id`, (2) writes to PostgreSQL in an atomic transaction, and (3) emits the `approval_required` SSE event. These two descriptions are mutually exclusive: if the graph interrupts before `hitl_gate`, none of the work in §2.3 is ever performed.

The data flow diagram in §4.3 makes this even clearer — it shows `hitl_gate` writing to PostgreSQL and emitting SSE, which is only possible if `hitl_gate` runs. But `interrupt_before` prevents it from running.

**Risk:**  
This is a fundamental design error. If implemented as written, the `approval_id` is never generated, the checkpoint is never written, the SSE event is never emitted, and the HITL flow silently dies with no feedback to the user. The entire HITL feature (FR-9, FR-10) is broken.

**Recommendation:**  
Choose exactly one of the two valid LangGraph HITL patterns and apply it consistently throughout the design:

- **Pattern A (`interrupt_after`):** Run `hitl_gate` to completion (it generates the `approval_id`, writes to DB, emits SSE), then suspend. Use `interrupt_after=["hitl_gate"]`. The graph pauses after the node returns its state update. Resume with `graph.ainvoke(None, config=...)`.
- **Pattern B (`interrupt_before` + external handler):** Remove the DB write and SSE emission from `hitl_gate`. Instead, perform those operations in the API layer **before** calling `graph.invoke()` the first time, passing the `approval_id` into the initial state. Let `interrupt_before` pause the graph at the node entry. Resume as before.

Pattern A is simpler and more consistent with the data flow diagrams. Update §2.1, §2.3, and §4.3 to reflect the chosen pattern.

---

### REVIEW-2

**ID:** REVIEW-2  
**Severity:** CRITICAL  
**Affected section:** §2.4 Edge Logic (`route_after_hitl`), FR-10  
**Category:** LangGraph Design

**Finding:**  
`route_after_hitl` checks `state["pending_approval"]`. If `pending_approval is None`, it returns `"approved"`. If not None, it returns `"suspended"` (which maps to `END`). This logic has a fatal flaw when the graph resumes after HITL approval.

When `POST /sessions/{session_id}/approve` resumes the graph via `graph.ainvoke(None, config=...)`, LangGraph re-runs the conditional edge function `route_after_hitl` with the **checkpointed state as it was at the time of suspension**. At that point, `pending_approval` is still set (it was written by `hitl_gate` before the graph suspended). The approval handler (in the API layer) marks the approval as used in PostgreSQL, but it never updates the LangGraph graph state to clear `pending_approval`. As a result, `route_after_hitl` returns `"suspended"` again — the graph immediately re-suspends without executing `tool_executor`, and the HITL cycle loops forever.

**Risk:**  
Every approved HITL action results in an infinite loop: the graph resumes, sees `pending_approval` is still set, re-suspends, waits for approval again, and repeats. The tool is never executed. This makes the approval flow completely non-functional.

**Recommendation:**  
The approval handler must clear `pending_approval` from the graph state **before** resuming the graph. In LangGraph, this means passing a state update in the resume call:
```python
await graph.ainvoke(
    {"pending_approval": None},   # clear the pending approval
    config={"configurable": {"thread_id": thread_id}}
)
```
Alternatively, rewrite `route_after_hitl` to check whether the `approval_id` has been marked as used in the database (via a DB query in the edge function), which introduces a DB call in an edge function — an anti-pattern. The state-update approach is correct. Document this explicitly in §2.4 and §4.3.

---

### REVIEW-3

**ID:** REVIEW-3  
**Severity:** CRITICAL  
**Affected section:** §2.2 State Schema, FR-7  
**Category:** LangGraph Design

**Finding:**  
In `AgentState`, only `messages` has a reducer annotation (`Annotated[list[BaseMessage], add_messages]`). The fields `tool_calls`, `tool_results`, `pending_approval`, and `error` have **no reducer annotation** — they use LangGraph's default "last write wins" behaviour. This is a critical footgun when the `tool_executor` node dispatches tools concurrently via `asyncio.gather()` (as described in §2.3).

In a LangGraph parallel branch (or when the framework internally handles concurrent state updates), if two tool results are written to `tool_results` at nearly the same time, the second write silently overwrites the first. The result is that `tool_results` contains only one entry even though two tools ran.

Additionally, `error` being last-write-wins means if two error conditions are raised in sequence (e.g., tool failure followed by HITL denial), one error state is silently overwritten.

**Risk:**  
Silent data loss: tool results disappear before the `llm` node reads them, causing the LLM to synthesise a response without all tool data. This is not detectable at runtime — no exception is raised. Under parallel tool execution (FR-7), this is a near-certain failure path.

**Recommendation:**  
Define and apply reducers for every list field that can be written by concurrent operations:
```python
from langgraph.graph import add_messages

def append_tool_calls(left: list, right: list) -> list:
    return left + right

def append_tool_results(left: list, right: list) -> list:
    return left + right

tool_calls:    Annotated[list[ToolCall],   append_tool_calls]
tool_results:  Annotated[list[ToolResult], append_tool_results]
```
For `error`, decide whether it should be last-write-wins (acceptable for a single error field) or a list of errors. Document the decision in §2.2.

---

### REVIEW-4

**ID:** REVIEW-4  
**Severity:** CRITICAL  
**Affected section:** §2.3 Node Responsibilities (all nodes), §4.1–§4.3 Data Flow Diagrams, FR-8, FR-13  
**Category:** Missing Detail

**Finding:**  
Every node in the design (`router`, `tool_executor`, `hitl_gate`, `llm`, `error_handler`) is described as emitting SSE events directly. However, LangGraph nodes are async functions that **return state updates** — they do not have a mechanism to push to an external channel like a Redis-backed SSE emitter. The design never explains how SSE emission is wired.

There are three valid implementation patterns, each with different tradeoffs:
- **(a) `graph.astream_events()`:** Wrap the graph invocation in `astream_events()`, filter for node-entry events and LLM token events, and translate them to SSE in the API layer. Nodes remain pure.
- **(b) Injected emitter dependency:** Pass the `SSEEmitter` instance into node functions via LangGraph's `RunnableConfig["configurable"]`. Nodes call `await emitter.emit(...)` directly. Nodes have side effects but are still independently testable.
- **(c) LangGraph callbacks:** Use LangGraph/LangChain callback handlers that fire on node entry/exit and on LLM tokens, emitting SSE from the callback.

The design shows nodes calling `SSE → Redis → Client` in every sequence diagram but never specifies which pattern is used or how the emitter object is passed into node scope.

**Risk:**  
Without specifying the mechanism, every implementer will make a different choice. If pattern (a) is chosen but the graph is invoked with `graph.ainvoke()` instead of `graph.astream_events()`, zero SSE events are emitted. If pattern (b) is chosen without explicit documentation, nodes will try to access an emitter that was never injected, raising `KeyError` at runtime. This is the most implementation-critical missing detail in the entire design.

**Recommendation:**  
Choose pattern (a) — `astream_events()` — as it keeps nodes pure and aligns with LangGraph 0.2+ best practices. Add a concrete code sketch to §2.1 or §4.1 showing:
```python
# In runner.py
async def run_turn(graph, state, config, emitter):
    async for event in graph.astream_events(state, config, version="v2"):
        if event["event"] == "on_chain_start":
            await emitter.emit("thinking", {"node": event["name"]})
        elif event["event"] == "on_chat_model_stream":
            await emitter.emit("token", {"content": event["data"]["chunk"].content})
```
Remove all SSE emission from within node function descriptions in §2.3 and move SSE emission to the streaming wrapper in `runner.py`.

---

### REVIEW-5

**ID:** REVIEW-5  
**Severity:** CRITICAL  
**Affected section:** §2.3 Node Responsibilities (`error_handler`), §7.2 Retry Logic, FR-27  
**Category:** Scalability

**Finding:**  
§2.3 describes `error_handler` as retrying tools up to 3 times with delays of 1 s, 2 s, and 4 s, and retrying LLMs up to 2 times with delays of 2 s and 4 s. If this retry loop is implemented as an inline `while` loop inside the `error_handler` async node, the total blocking duration is:
- Tool retries: 1 + 2 + 4 = **7 seconds** of `asyncio.sleep()` per failed tool turn.
- LLM retries: 2 + 4 = **6 seconds** of `asyncio.sleep()` per failed LLM turn.

While `asyncio.sleep()` is non-blocking for the event loop, the design does not state this explicitly. If a developer uses `time.sleep()` instead (a common mistake in async codebases), the event loop is blocked for 7 seconds, starving all other concurrent requests.

More critically, the design describes `error_handler` as a single LangGraph node that **also** routes to `tool_executor` on retry (described in the state diagram: `tool_executor → error_handler` and the retry loop is back to `tool_executor`). But the graph has `builder.add_edge("error_handler", END)` — `error_handler` only has an edge to `END`. There is no loop edge from `error_handler` back to `tool_executor`. The retry loop cannot be a graph-level loop with the current topology.

**Risk:**  
The retry logic is either: (a) an inline blocking loop inside `error_handler` that contradicts the "nodes return state updates" model, or (b) a graph-level retry loop that contradicts the edge definition (`error_handler → END` only). The implementation will be ambiguous, inconsistently applied, and potentially event-loop-blocking.

**Recommendation:**  
Clarify the retry architecture with one of two approaches:

- **Approach 1 (inline async retry in nodes, pre-graph):** Move retry logic into `tool_executor` and `llm` nodes themselves, using `asyncio.sleep()` (not `time.sleep()`). On exhaustion of all retries, write the error to `AgentState.error` and return, letting the edge route to `error_handler` for user-facing error emission only. This keeps `error_handler` simple and the graph topology clean.
- **Approach 2 (graph loop edge):** Add a `retry` edge from `error_handler` back to `tool_executor` (for tool errors) and to `llm` (for LLM errors), with `AgentState.error.retry_count` as the guard in the conditional edge. Add a `max_retries` field to `ErrorState` and make `route_after_error_handler` return `"retry"` or `"give_up"`. This requires adding loop edges to the graph topology.

Document the chosen approach in §2.3 and §2.4, and explicitly state that all sleep calls must use `asyncio.sleep()`.

---

### REVIEW-6

**ID:** REVIEW-6  
**Severity:** HIGH  
**Affected section:** §2.2 State Schema, §5.2 Table DDL (LangGraph checkpoint comment), FR-18  
**Category:** Security

**Finding:**  
`AgentState.thread_id` is documented as `"{user_id}:{conversation_id}"` for authenticated users and `"guest:{session_id_hash}:{session_id}"` for guests. This format uses a colon (`:`) as a delimiter. If `user_id` or `conversation_id` contain colons (which UUIDs do not, but which is not enforced), the parsing of this composite key is ambiguous.

More practically, the format `"guest:{session_id_hash}:{session_id}"` uses two colons as separators. A parser splitting on `:` must assume exactly one hash segment before the session_id. If `session_id_hash` itself contains a colon (which is possible for some hash encodings like Base64url with padding variants), the parse is ambiguous. The hashing function used for `session_id_hash` is completely unspecified — MD5, SHA1, SHA256, and HMAC all produce different outputs, and some encodings (hex, Base64) have different collision and ambiguity properties.

**Risk:**  
Two different guest sessions could produce the same `thread_id` if a hash collision occurs in a weak hash function (MD5, SHA1). This would cause one guest's session state to be returned to the other guest — a cross-session state leak. Even with a strong hash, using MD5 or SHA1 is considered a security anti-pattern and will fail security audits.

**Recommendation:**  
(1) Use UUID v4 for `session_id` (already in spec) and `user_id` (already UUID) — colons cannot appear in UUID strings, so the colon delimiter is safe for authenticated users. (2) For guests, use HMAC-SHA256 with a secret key (or simply use the raw UUID v4 `session_id` directly as the thread key — it is already random and collision-resistant). (3) Document the exact hash function and encoding (e.g., `hashlib.sha256(session_id.encode()).hexdigest()`) in §2.2. (4) Add a note that `thread_id` construction must be tested with UUID inputs to confirm uniqueness.

---

### REVIEW-7

**ID:** REVIEW-7  
**Severity:** HIGH  
**Affected section:** §5.3 Redis Key Namespace (`hitl:lock:{approval_id}`), FR-10  
**Category:** Security

**Finding:**  
The Redis distributed lock for HITL approval uses `SET NX PX 30000` (30-second TTL). The lock is acquired before the approval DB update and released after the graph is resumed. The resume operation requires: (1) acquiring the lock, (2) DB atomic UPDATE, (3) DB INSERT to audit log, (4) loading the LangGraph checkpoint (potentially a large serialised state from PostgreSQL), (5) re-entering the graph and running `tool_executor`. Step (4) and (5) can take significantly more than 30 seconds if the checkpoint is large, the DB is under load, or the tool itself begins executing.

If the lock TTL expires before the approval processing completes, a second concurrent request for the same `approval_id` can acquire the lock and proceed. Both requests now see `used=false` (since the first request's DB UPDATE has not committed yet or committed but was not committed within the lock window), and both will attempt to resume the graph. The `UPDATE ... WHERE used=false RETURNING id` pattern protects against double-consumption, but only if the first UPDATE commits before the second request reads. Under high DB latency, this race window is real.

**Risk:**  
A race condition between lock expiry and DB write can result in two concurrent graph resumptions for the same HITL approval, causing the tool to execute twice. For web search, this means two Tavily API calls for the same query — a minor cost issue. For future sensitive tools (e.g., email sending, order placement), this is a double-execution bug.

**Recommendation:**  
Increase the lock TTL to at least 60 seconds (covering the full resume-and-execute cycle). Implement a lock heartbeat (`EXPIRE key 60` called every 10 seconds from the approval handler) to extend the lock while the operation is in progress. Alternatively, make the graph resumption idempotent: before resuming, re-check `used=true` inside the graph at the start of `tool_executor`, so a second execution is a no-op.

---

### REVIEW-8

**ID:** REVIEW-8  
**Severity:** HIGH  
**Affected section:** §5.2 Table DDL (`hitl_audit_log`), FR-32  
**Category:** Security

**Finding:**  
The `hitl_audit_log` DDL comment states "No UPDATE or DELETE granted on this table; enforced at application layer." This is not a database-level control — it is a code comment. Application-layer enforcement means: (a) the application's SQLAlchemy models simply never call `.update()` or `.delete()` on this table, and (b) there are no tests that verify this constraint at the DB level. Any future developer, migration, or direct `psql` session can UPDATE or DELETE rows freely.

A genuine append-only audit log requires database-level enforcement via one or more of:
- Revoking `UPDATE` and `DELETE` privileges on the table for the application's database role.
- A PostgreSQL trigger that raises an exception on any UPDATE or DELETE.
- Row-level security (RLS) policy blocking modifications.

**Risk:**  
An attacker with application-level DB credentials (e.g., via SQL injection, a compromised service account, or a misconfigured migration) can alter or erase the audit trail for HITL decisions. The audit log loses its forensic value. FR-32 specifically states the table must be append-only — the current design does not satisfy this requirement.

**Recommendation:**  
Add a migration step that revokes UPDATE and DELETE on `hitl_audit_log` from the application role:
```sql
REVOKE UPDATE, DELETE ON hitl_audit_log FROM <app_role>;
```
Alternatively, add a BEFORE UPDATE/DELETE trigger that raises an exception:
```sql
CREATE RULE no_update_hitl_audit AS ON UPDATE TO hitl_audit_log DO INSTEAD NOTHING;
```
Document this in §5.2 and add a CI test that attempts a direct UPDATE on the table using the application credentials and asserts it fails with a permission error.

---

### REVIEW-9

**ID:** REVIEW-9  
**Severity:** HIGH  
**Affected section:** §2.3 Node Responsibilities (`tool_executor`), §7.2 Retry Logic, FR-7, NFR-3  
**Category:** Scalability

**Finding:**  
`tool_executor` dispatches all tool calls via `asyncio.gather()` without any per-tool timeout. §2.3 specifies that tools must complete within 3 seconds (NFR-3), but no timeout is enforced at the gather level. If a tool (e.g., `WeatherTool` calling an external HTTP endpoint) hangs indefinitely (no response, TCP stall), `asyncio.gather()` will wait indefinitely. This blocks the `tool_executor` node, holds the LangGraph state transition, keeps the SSE stream open, and occupies a DB connection for the duration.

The design also mentions that `error_handler` applies retry logic for tool failures — but if `asyncio.gather()` never raises (because the tool never times out), `error_handler` is never invoked. The tool just blocks forever.

**Risk:**  
A single stuck external API call (e.g., a weather service that stops responding) blocks a user's entire session indefinitely. Under concurrent load (A-1 assumption of 5 users), 5 such stuck sessions exhaust the DB connection pool (NFR-7 specifies max 10 connections) and cause all other sessions to fail.

**Recommendation:**  
Wrap each tool execute call with `asyncio.wait_for(tool.execute(input), timeout=5.0)` (configurable via `TOOL_TIMEOUT_SECONDS` env var). In `asyncio.gather()`, use `return_exceptions=True` so individual tool timeouts are captured as exceptions rather than propagating and cancelling all tools:
```python
results = await asyncio.gather(
    *[asyncio.wait_for(tool.execute(inp), timeout=TOOL_TIMEOUT_SECONDS)
      for tool, inp in zip(tools, inputs)],
    return_exceptions=True
)
```
Document the timeout value in §2.3 and add it to the `ErrorState` codes as `TOOL_TIMEOUT`.

---

### REVIEW-10

**ID:** REVIEW-10  
**Severity:** HIGH  
**Affected section:** §2.4 Edge Logic (`route_after_tools`), FR-7, FR-27  
**Category:** LangGraph Design

**Finding:**  
`route_after_tools` routes to `"error"` if `any(r["error"] for r in state["tool_results"])`. This means if 3 out of 4 parallel tools succeed and 1 fails, the edge routes to `error_handler` — discarding all 3 successful tool results. The `llm` node is never invoked with the partial results. The user receives a generic tool-failure error message even though the agent has useful partial data to work with.

Additionally, the design also checks `state.get("error")` as the first condition in `route_after_tools`. If `error_handler` previously set `AgentState.error` and the graph somehow reaches `tool_executor` again (e.g., via a retry loop), the stale `AgentState.error` from the previous iteration will immediately route back to `error_handler`, bypassing tool execution. The design does not show where `AgentState.error` is cleared between graph runs.

**Risk:**  
Partial tool success results in a complete failure UX, even when enough data is available for a useful response. Additionally, stale `error` state from a previous turn (if not cleared by `router`) can cause the graph to immediately route to `error_handler` at the start of every subsequent turn.

**Recommendation:**  
(1) Change `route_after_tools` to distinguish between total failure (all tools failed) and partial failure (some tools failed). Route to `llm` on partial success, passing available results with per-tool error indicators. The `llm` prompt template should be updated to handle missing tool results gracefully.  
(2) Document that `router` must clear `AgentState.error` (set it to `None`) at the start of each turn, in addition to clearing `tool_calls` and `tool_results`. Add this to the `router` node description in §2.3.

---

### REVIEW-11

**ID:** REVIEW-11  
**Severity:** HIGH  
**Affected section:** §2.4 Edge Logic (`route_after_router`), §2.1 Graph Construction, FR-6  
**Category:** LangGraph Design

**Finding:**  
`route_after_router` returns one of three strings: `"hitl"`, `"tool"`, or `"llm_direct"`. The `add_conditional_edges` mapping covers exactly these three values. If `route_after_router` returns any other value (e.g., due to a bug in the routing logic, a new enum value added during development, or a silent LLM parsing error that produces unexpected output), LangGraph raises a `KeyError` at the point of edge resolution. This exception is uncaught, propagates up through `graph.astream()`, and terminates the entire graph execution with no user-facing error — the SSE stream simply closes.

The same issue exists for `route_after_hitl` (three possible values) and `route_after_llm` (two possible values).

**Risk:**  
Any routing bug that produces an unexpected return value causes an unhandled `KeyError` that crashes the graph silently from the user's perspective. The `error_handler` node is never invoked because the crash happens at the graph infrastructure level, not within a node. No `error` SSE event is emitted.

**Recommendation:**  
Add a default/fallback case to every conditional edge mapping. LangGraph supports a `"__default__"` key in the mapping dict that catches any unmapped return value:
```python
builder.add_conditional_edges("router", route_after_router, {
    "hitl":       "hitl_gate",
    "tool":       "tool_executor",
    "llm_direct": "llm",
    "__default__": "error_handler",   # catch unexpected values
})
```
Also add defensive assertions in each edge function:
```python
assert result in {"hitl", "tool", "llm_direct"}, f"Unexpected route: {result}"
```
Document this pattern in §2.4.

---

### REVIEW-12

**ID:** REVIEW-12  
**Severity:** HIGH  
**Affected section:** §2.2 State Schema, FR-13  
**Category:** Conflicting Design

**Finding:**  
`AgentState` defines `llm_response: Optional[str]` as a separate field for the synthesised LLM response. At the same time, §2.3 states the `llm` node also "appends the completed assistant message to `messages`" (with the `add_messages` reducer). This means the final assistant response exists in two places: once in `messages` as an `AIMessage` object, and again in `llm_response` as a raw string.

These two representations will diverge. If the LLM returns structured content (tool calls, multi-part messages), the `AIMessage` in `messages` captures the full structure while `llm_response` contains only the text content. Any downstream component that reads `llm_response` instead of parsing `messages` will get an incomplete or wrong view of the response.

`route_after_llm` uses `state.get("llm_response")` to detect success/failure — this check is fragile if `llm_response` is an empty string (falsy in Python) but a valid response.

**Risk:**  
Two sources of truth for the assistant response create maintainability bugs. Developers will be uncertain which field to read. The `route_after_llm` check using `not state.get("llm_response")` returns `True` (error path) for an empty string response, which is correct per the design, but also for `None` (uninitialised), which is the initial state — meaning if `llm_response` is never set (e.g., due to a bug), the route silently goes to `error_handler` with no clear signal.

**Recommendation:**  
Remove `llm_response` from `AgentState`. The `llm` node should write only to `messages`. For success/failure detection, `route_after_llm` should check whether the last message in `state["messages"]` is an `AIMessage` with non-empty content:
```python
def route_after_llm(state: AgentState) -> str:
    last = state["messages"][-1] if state["messages"] else None
    if isinstance(last, AIMessage) and last.content:
        return "success"
    return "error"
```

---

### REVIEW-13

**ID:** REVIEW-13  
**Severity:** HIGH  
**Affected section:** §2.1 Graph Construction, §10.1 LangGraph Justification, FR-13  
**Category:** Missing Detail

**Finding:**  
The design uses `graph.astream()` in the data flow diagram (§4.1: `API->>G: graph.astream({messages}, config={thread_id})`) but §10.1 justifies LangGraph by citing "Streaming is built-in (`astream_events`)". These are two distinct LangGraph APIs with different output formats:

- `graph.astream()` yields state snapshots after each node completes — it does not yield intermediate LLM tokens.
- `graph.astream_events()` yields granular events including `on_chat_model_stream` for individual LLM tokens, `on_chain_start`/`on_chain_end` for node transitions, and `on_tool_start`/`on_tool_end` for tool calls.

To stream LLM tokens to the SSE client (as required by FR-13 — `token` events for each incremental chunk), `astream_events()` is the only correct choice. Using `astream()` means LLM tokens are only available after the entire LLM response is complete — defeating the purpose of streaming.

**Risk:**  
If `graph.astream()` is used, no `token` SSE events are emitted during LLM generation, violating FR-13 and NFR-1 (time to first token ≤ 1.5 s is impossible if tokens arrive in batch). The user sees a long blank wait followed by the full response appearing at once.

**Recommendation:**  
Replace all references to `graph.astream()` in data flow diagrams with `graph.astream_events(version="v2")`. Add a concrete mapping table to §2.1 or §4.1 showing which LangGraph event types map to which SSE event types:

| LangGraph event | SSE event type |
|---|---|
| `on_chain_start` (node entry) | `thinking` |
| `on_chat_model_stream` | `token` |
| `on_tool_end` | `tool_result` |
| `on_chain_end` (graph complete) | `done` |

---

### REVIEW-14

**ID:** REVIEW-14  
**Severity:** HIGH  
**Affected section:** §5.3 Redis Key Namespace, §5.2 Table DDL, NFR-7, NFR-6  
**Category:** Scalability

**Finding:**  
NFR-7 specifies a maximum PostgreSQL connection pool of 10 connections. The design supports HITL flows where the graph is suspended for up to 10 minutes (FR-11). During HITL suspension, the SSE stream at `GET /chat/stream` remains open, waiting for events. If the SSE handler holds a DB connection open for the duration of the HITL wait (e.g., because it uses a SQLAlchemy session in the request handler scope), 5 concurrent HITL-suspended users (A-1 assumption) hold 5 connections for up to 10 minutes each, consuming 50% of the pool.

Additionally, the `POST /sessions/{session_id}/approve` endpoint loads the LangGraph checkpoint (requiring a DB connection) and resumes the graph (requiring another DB connection for the checkpointer write). Under concurrent load, these approval operations consume additional pool slots simultaneously with the suspended SSE streams.

The design does not describe how the SSE handler manages its DB connection during the long HITL wait, nor does it define a maximum number of concurrent open SSE streams.

**Risk:**  
Under moderate concurrent usage (5 users, all with active HITL flows), the DB connection pool exhausts. All subsequent DB operations (including approval submissions and new chat requests) receive a "pool timeout" error and fail. The `GET /readiness` check catches this only after the fact.

**Recommendation:**  
(1) The SSE handler at `GET /chat/stream` must **not** hold a DB connection open during the stream. It should read from the Redis replay buffer only (no DB access during streaming). DB connections should be acquired only for initial validation and released immediately. (2) Document this pattern explicitly in §4.4 and §6.2. (3) Add a configurable limit on the number of concurrent open SSE connections (e.g., `MAX_CONCURRENT_SSE_STREAMS=50`, enforced via a Redis counter). (4) Distinguish between "SSE stream is open" and "LangGraph graph is running" — a suspended HITL graph must not hold any DB connection.

---

### REVIEW-15

**ID:** REVIEW-15  
**Severity:** HIGH  
**Affected section:** §2.2 State Schema, §5.3 Redis Key Namespace, FR-21  
**Category:** Security

**Finding:**  
`AgentState.thread_id` for guests is specified as `"guest:{session_id_hash}:{session_id}"` where `{session_id_hash}` is a hash of the guest session ID. The Redis key in §5.3 is `guest:{session_id_hash}:state`. The specific hashing function is never named in the design.

If MD5 is used: MD5 is broken for collision resistance and produces a 32-character hex string. Two different session IDs could produce the same hash, mapping two guests to the same Redis key and the same LangGraph thread — a cross-session state leak.

If SHA1 is used: SHA1 has known collision attacks (SHAttered, 2017) and is not considered collision-resistant for security purposes.

If the raw session_id UUID is used without hashing: there is no security benefit to hashing it, but also no risk. The hash is unnecessary since UUIDs are already opaque.

**Risk:**  
Use of a weak hash function could allow a crafted guest session to collide with another guest's session, reading or corrupting that session's state. This is a cross-user data exposure vulnerability.

**Recommendation:**  
Use SHA-256 minimum, or preferably HMAC-SHA256 with the `JWT_SECRET` as the key, to prevent brute-force preimage attacks against the hash. Or simply use the raw UUID v4 `session_id` as the Redis key without hashing, since UUIDs are already 122 bits of entropy. Document the exact implementation in §5.3 with the specific Python call, e.g.:
```python
import hashlib
session_hash = hashlib.sha256(session_id.encode()).hexdigest()
```

---

### REVIEW-16

**ID:** REVIEW-16  
**Severity:** MEDIUM  
**Affected section:** §2.2 State Schema, FR-14  
**Category:** LangGraph Design

**Finding:**  
`AgentState` includes `stream_id: str` — the UUID of the current SSE stream. This is an infrastructure concern (which Redis key to write events to), not an agent concern (not relevant to routing, tool selection, or response generation). Storing it in `AgentState` means it is serialised into every LangGraph checkpoint and replayed on graph resumption.

If a user reconnects with a new `stream_id` after a HITL suspension (the old stream expired), the graph resumes with the old `stream_id` from the checkpoint — SSE events are emitted to the expired buffer, not the new connection. The user receives no events.

**Risk:**  
After a reconnect, the resumed graph emits all events to the stale Redis key (`stream:{old_stream_id}:events`), which the client is no longer listening to. The client's new stream (`stream:{new_stream_id}:events`) receives nothing. The HITL resumption is effectively invisible to the user.

**Recommendation:**  
Remove `stream_id` from `AgentState`. Instead, pass it as part of `RunnableConfig["configurable"]` so it is available to nodes at runtime but is not checkpointed:
```python
config = {
    "configurable": {
        "thread_id": thread_id,
        "stream_id": stream_id,   # injected at invocation time, not in state
    }
}
```
Nodes that need to emit SSE events access `config["configurable"]["stream_id"]`. On resume, the API layer passes the new `stream_id` in the resumption `config`. Update §2.2, §2.3, and §4.3 accordingly.

---

### REVIEW-17

**ID:** REVIEW-17  
**Severity:** MEDIUM  
**Affected section:** §2.2 State Schema, §8.1 LangSmith Tracing, FR-18  
**Category:** LangGraph Design

**Finding:**  
`AgentState` includes `thread_id: str`. LangGraph already provides `thread_id` via the `RunnableConfig` passed to every node as the second argument:
```python
async def my_node(state: AgentState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
```
Storing `thread_id` in `AgentState` is redundant. It also creates a divergence risk: if the `configurable["thread_id"]` ever differs from `state["thread_id"]` (e.g., due to a misconfigured resume call), nodes that read from state will see a stale value while LangGraph uses the `config` value for checkpointing. The §8.1 LangSmith config block reads `state.thread_id` — if this diverges from the actual LangGraph thread, traces will be associated with the wrong thread in LangSmith.

**Risk:**  
Divergence between `state.thread_id` and `config["configurable"]["thread_id"]` causes LangSmith traces to be misfiled and makes debugging session-specific issues extremely difficult. For authenticated users, this could cross-associate traces from different sessions.

**Recommendation:**  
Remove `thread_id` from `AgentState`. In any node or edge function that needs the thread ID, access it from `RunnableConfig`:
```python
async def router_node(state: AgentState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
```
Update §2.2 and §8.1 accordingly.

---

### REVIEW-18

**ID:** REVIEW-18  
**Severity:** MEDIUM  
**Affected section:** §5.3 Redis Key Namespace (`stream:{stream_id}:events`), §10.3, NFR-5  
**Category:** Scalability

**Finding:**  
The SSE replay buffer is implemented as a Redis List (`stream:{stream_id}:events`). The SSE reconnection flow reads events using `LRANGE stream:{stream_id}:events 0 -1` (or a filtered equivalent). `LRANGE` is O(N) in the number of elements. For a streaming turn with 500 LLM tokens, each stored as a separate list element, a full replay requires returning 500 elements. At the design's 15-minute TTL for HITL streams, a heavily used stream could accumulate thousands of elements.

For a single Redis instance (as deployed on Render), the `LRANGE` operation holds the Redis server thread for the duration of the scan. Redis is single-threaded for command execution; a 1,000-element range scan blocks all other Redis commands for the duration.

The design's own §10.3 acknowledges "list append + range scan is O(N) in Redis" but justifies it without quantifying the N value or the performance impact.

**Risk:**  
Under high token output (long LLM responses) or HITL flows with frequent `thinking` events, the replay buffer grows large. A reconnecting client triggers a large `LRANGE` that momentarily blocks all other Redis operations — including quota counter lookups, jti blacklist checks, and approval lock operations. This introduces latency spikes across all concurrent users.

**Recommendation:**  
Use a Redis Sorted Set instead of a List, with the event ID as the score:
```python
# Write: O(log N)
await redis.zadd(f"stream:{stream_id}:events", {serialised_event: event_id})

# Replay from Last-Event-ID: O(log N + M) where M is replayed events
await redis.zrangebyscore(f"stream:{stream_id}:events", last_event_id + 1, "+inf")
```
Sorted Sets support O(log N + M) range queries by score (event ID), which is more efficient than O(N) list traversal and natively supports the `Last-Event-ID` filtering without client-side filtering. Update §5.3 and §10.3.

---

### REVIEW-19

**ID:** REVIEW-19  
**Severity:** MEDIUM  
**Affected section:** §2.3 Node Responsibilities (all nodes), FR-9, FR-13  
**Category:** LangGraph Design

**Finding:**  
LangGraph may re-enter a node after a checkpoint restore in certain failure scenarios (e.g., process crash mid-node, or when `interrupt_before`/`interrupt_after` causes re-entry). Nodes in the design emit SSE events as side effects (e.g., `router` emits `thinking`, `hitl_gate` emits `approval_required`). If a node is re-entered (after a crash recovery or a replay), it will re-emit SSE events that the client has already received.

For example, if the `router` node crashes after emitting its `thinking` event but before writing its state update, LangGraph will re-run `router` on next invocation — and `router` will emit a second `thinking {node: "router"}` event. The client sees a duplicate event. For `hitl_gate`, a second `approval_required` emission for the same `approval_id` would be confusing and potentially trigger a duplicate approval dialog.

**Risk:**  
Duplicate SSE events cause incorrect UI behaviour. Duplicate `thinking` events are cosmetic. Duplicate `approval_required` events for the same `approval_id` cause UX confusion (two approval dialogs). Duplicate `error` events could cause the UI to display the error twice. The design has no deduplication mechanism at the client or server.

**Recommendation:**  
Include the event ID (monotonically incrementing integer) in the SSE `id` field (already required by FR-13). The client must deduplicate events with already-seen IDs. On the server side, before emitting any SSE event, check if an event with this ID already exists in the Redis replay buffer (`ZSCORE` or `LPOS`) and skip re-emission if it does. Document this idempotency check in §2.3. Alternatively, use `astream_events()` (REVIEW-4 recommendation) to move SSE emission outside nodes entirely, making replay a non-issue.

---

### REVIEW-20

**ID:** REVIEW-20  
**Severity:** MEDIUM  
**Affected section:** §2.5 Tool Registry Loader, FR-5  
**Category:** Security

**Finding:**  
`tools.yaml` is loaded at application startup from `config/tools.yaml`. The `load_registry` function takes a `config_path: str` parameter. The design does not state whether this path is hardcoded, read from an environment variable, or passed as a command-line argument. If the path is configurable from an environment variable, an attacker who can control environment variables (e.g., via a deployment misconfiguration, a `.env` file inclusion vulnerability, or a compromised CI secret) can point the tool registry to a malicious YAML file that registers a tool with a `module` path pointing to attacker-controlled code.

Additionally, the `load_registry` function uses dynamic import (`module: src.tools.weather`, `class: WeatherTool`) — it dynamically loads Python modules by name. This is equivalent to a restricted `eval()` on the module system: if the module name is user-controlled, it becomes an arbitrary code execution vector.

**Risk:**  
If `config_path` is injectable, an attacker can register a malicious tool that executes arbitrary Python code on `BaseTool.execute()` invocation. If the module path in `tools.yaml` can be modified (e.g., via a path traversal), the same applies.

**Recommendation:**  
(1) Hardcode the `config_path` in the application code rather than reading it from an environment variable. (2) If configurability is needed, validate that the path is within the application directory (refuse paths containing `..` or absolute paths outside the expected directory). (3) Validate that each `module` value in `tools.yaml` matches an allowlist of known module prefixes (e.g., must start with `src.tools.`). (4) Document these constraints in §2.5 and add a startup validation step.

---

### REVIEW-21

**ID:** REVIEW-21  
**Severity:** MEDIUM  
**Affected section:** §9.6 Input Validation and Sanitisation Pipeline, NFR-15  
**Category:** Security

**Finding:**  
§9.6 lists the sanitisation pipeline in this order:
1. Pydantic validation
2. Null byte rejection
3. Unicode normalisation
4. Whitespace stripping

Pydantic validation runs **first** — before null bytes are rejected. Pydantic's string validators process the raw input including any null bytes (`\x00`). If a Pydantic field uses a regex validator (e.g., for email format), the regex may match incorrectly on strings containing null bytes. More critically, null bytes can truncate strings in C-extension layers used by some Pydantic validators, potentially causing the validator to see a shorter string than intended.

The sanitisation pipeline is described as "implemented as a FastAPI dependency applied globally via middleware." If it is middleware (runs before route handlers), it runs before Pydantic validation (which happens inside route handler parameter parsing). This contradicts the listed order — middleware runs first, then Pydantic. The listed order is inverted from the actual FastAPI request lifecycle.

**Risk:**  
Null bytes in user input pass through Pydantic validation (potentially corrupting it), then reach middleware sanitisation, then are injected into application logic before being caught. The intended defence-in-depth pipeline is actually operating in the wrong order, reducing its effectiveness.

**Recommendation:**  
Correct the pipeline to match FastAPI's actual request lifecycle:
1. **Middleware** — null byte rejection and Unicode normalisation (runs first, before route handler)
2. **Pydantic** — type checking, length limits, regex patterns (runs inside route handler)
3. **Application logic** — whitespace stripping (applied to sanitised, validated data)

Update §9.6 to reflect the correct order and add a note explaining that FastAPI middleware runs before Pydantic model instantiation.

---

### REVIEW-22

**ID:** REVIEW-22  
**Severity:** MEDIUM  
**Affected section:** §5.2 Table DDL (conversations partial index), FR-22  
**Category:** Scalability

**Finding:**  
The retention job (FR-22) must delete conversations meeting both purge conditions atomically. The design mentions `FOR UPDATE SKIP LOCKED` (in §10.2 PostgreSQL justification) but provides no implementation sketch for the retention job itself. The partial index on `conversations` is:
```sql
CREATE INDEX idx_conversations_retention
    ON conversations(last_accessed, access_count)
    WHERE last_accessed < NOW() - INTERVAL '60 days';
```
The spec requires a 90-day threshold (FR-22: "more than 90 days"), but the partial index predicate uses 60 days — a mismatch. The partial index will include rows that are not eligible for deletion, increasing the index size and making the job's filter less selective.

More critically, the design does not describe batching for the retention job. A single `DELETE FROM conversations WHERE ...` that matches 10,000 rows acquires row locks on all 10,000 rows simultaneously, holds them for the duration of the DELETE, and can block concurrent `SELECT` queries that join to `conversations`. On a large installation, this can cause visible latency spikes.

**Risk:**  
(1) The 60-day index predicate is inconsistent with the 90-day spec requirement, causing either index misses (slower retention query) or premature index inclusion of non-purgeable rows. (2) Unbatched deletion of thousands of rows holds table locks for extended periods, blocking active user sessions.

**Recommendation:**  
(1) Fix the partial index predicate to use 90 days, matching FR-22: `WHERE last_accessed < NOW() - INTERVAL '90 days'`. (2) Batch the deletion in the retention job:
```python
while True:
    deleted = await db.execute(
        """DELETE FROM conversations WHERE id IN (
            SELECT id FROM conversations
            WHERE last_accessed < NOW() - INTERVAL '90 days'
            AND access_count < 5
            AND id NOT IN (SELECT conversation_id FROM hitl_approvals WHERE used=false AND expired=false)
            LIMIT 100 FOR UPDATE SKIP LOCKED
        ) RETURNING id"""
    )
    if deleted.rowcount == 0:
        break
    await asyncio.sleep(0.1)  # yield between batches
```
Document the batch size and inter-batch delay as configurable parameters.

---

### REVIEW-23

**ID:** REVIEW-23  
**Severity:** MEDIUM  
**Affected section:** §9.2 JWT Claim Validation, FR-16  
**Category:** Scalability

**Finding:**  
JWT claim validation step 8 (§9.2) is a Redis lookup: `jti` is checked against `blacklist:jti:{jti}`. This check occurs on every protected endpoint, including `GET /chat/stream` (which is a long-lived SSE connection) and the HITL approval endpoint. The Redis lookup is performed as a standalone command, not as part of a pipeline.

For a high-frequency endpoint path (e.g., every SSE heartbeat check or every tool result event triggering a quota check), each Redis call is a separate round-trip. The design uses `redis-py` async, which is non-blocking, but separate round-trips still introduce per-request latency (typically 0.5–2 ms per Redis call on Render's network). For the hot path of SSE streaming, this adds up.

The design does not specify whether the blacklist check and the quota check are pipelined into a single Redis transaction.

**Risk:**  
Under high request frequency (20 requests per 4 hours per FR-26), each request makes at least 2 Redis calls (blacklist check + quota increment) as separate round-trips. Pipelining these into a single Redis `MULTI/EXEC` or pipeline call would halve the Redis round-trips. While not critical at the design's target scale (A-1: 5 concurrent users), this is a scalability issue for any production hardening.

**Recommendation:**  
Pipeline the blacklist check and quota increment into a single Redis pipeline:
```python
async with redis.pipeline(transaction=False) as pipe:
    pipe.get(f"blacklist:jti:{jti}")
    pipe.incr(f"quota:{user_id}:{window}:requests")
    results = await pipe.execute()
is_blacklisted, quota_count = results
```
Document this pattern in §9.2 and the quota enforcement section. For the SSE streaming path specifically, perform the blacklist check once at connection time and not on every event.

---

### REVIEW-24

**ID:** REVIEW-24  
**Severity:** MEDIUM  
**Affected section:** §2.3 Node Responsibilities (`error_handler`), §2.2 State Schema (`ErrorState`), FR-27, FR-28  
**Category:** LangGraph Design

**Finding:**  
`error_handler` is responsible for: (1) inspecting the error source (tool, LLM, HITL denial), (2) applying tool retries with exponential backoff, (3) applying LLM retries and triggering the fallback chain, (4) emitting user-facing `error` SSE events, and (5) generating cancellation messages for HITL denials. This is five distinct behaviours in one node.

`ErrorState` has fields: `code`, `message`, `retryable`, `retry_count`. There is no `source` field indicating whether the error originated from a tool, an LLM call, or a HITL denial. §2.3 says `error_handler` "inspects the error source" — but `ErrorState` provides no mechanism to do this. `code` could encode the source (e.g., `TOOL_TIMEOUT` vs. `LLM_ERROR` vs. `HITL_DENIED`), but this is not documented, and the `error_handler` implementation would need to parse the error code string to determine routing — a fragile pattern.

**Risk:**  
Without a `source` field, `error_handler` must infer the error source from the error code. If the code is set to a generic value (e.g., `TOOL_ERROR`) that does not specify which tool, `error_handler` cannot know which tool to retry (it needs `tool_calls` to find this, but those might be partially cleared). The retry logic in `error_handler` will be brittle and fragile.

**Recommendation:**  
Add a `source` field to `ErrorState`:
```python
class ErrorState(TypedDict):
    code: str
    message: str
    retryable: bool
    retry_count: int
    source: Literal["tool", "llm", "hitl"]   # add this
    failed_tool_name: Optional[str]            # which tool failed, for targeted retry
```
Split `error_handler` into two nodes: `retry_handler` (handles retries and fallbacks for tool/LLM failures) and `error_emitter` (generates user-facing SSE errors). This separates retry orchestration from error presentation. Update §2.2 and §2.3.

---

### REVIEW-25

**ID:** REVIEW-25  
**Severity:** MEDIUM  
**Affected section:** §2.1 Graph Construction, NFR-6  
**Category:** LangGraph Design

**Finding:**  
The design compiles the LangGraph `StateGraph` once at application startup:
```python
graph = builder.compile(checkpointer=checkpointer, interrupt_before=["hitl_gate"])
```
This `graph` object is stored as a module-level singleton and invoked concurrently by multiple request handlers via `graph.astream_events(...)`. The design does not state whether the compiled LangGraph graph object is safe for concurrent async access.

In LangGraph, the compiled `CompiledStateGraph` object is stateless between invocations — all per-invocation state is stored in the checkpointer and in the `RunnableConfig`. However, internal LangGraph components (e.g., the `AsyncPostgresSaver` checkpointer) use connection pools that have per-operation state. If the checkpointer is not designed for concurrent async use, concurrent invocations on the same `graph` object could corrupt each other's checkpoint reads/writes.

**Risk:**  
If `AsyncPostgresSaver` is not safe for concurrent use (i.e., it maintains per-invocation mutable state on the instance), concurrent requests to the same `graph` instance will race on the checkpointer's internal state, causing checkpoint corruption or incorrect state restoration. This could result in one user's state being returned to another user.

**Recommendation:**  
Add an explicit note in §2.1 confirming that `CompiledStateGraph` and `AsyncPostgresSaver` are safe for concurrent async invocation (verify against LangGraph 0.2+ source). If they are not, instantiate a new `AsyncPostgresSaver` per request (using a connection pool internally). Add a test that invokes the same compiled graph with 10 concurrent async requests targeting different `thread_id` values and asserts no cross-thread state contamination.

---

### REVIEW-26

**ID:** REVIEW-26  
**Severity:** MEDIUM  
**Affected section:** §5.2 Table DDL (`hitl_audit_log`), FR-32, NFR-15  
**Category:** Security

**Finding:**  
The `hitl_audit_log` table includes a `decision_reason TEXT` column, described as "optional user-provided text." The design does not specify any length limit, sanitisation requirement, or encoding validation for this field. A user can supply an arbitrarily long string (potentially megabytes) as a `decision_reason`, causing unbounded row growth in the audit log table. Additionally, if `decision_reason` is logged (as required by NFR-18: "a corresponding log line at `INFO` level must be written per NFR-18"), a malicious user can inject log control characters (newlines, ANSI escape codes) to corrupt structured log output.

**Risk:**  
(1) An unbounded `decision_reason` can bloat the `hitl_audit_log` table to gigabytes with a single malicious approval request. (2) Log injection via newline-embedded `decision_reason` values can corrupt JSON log lines, breaking log aggregation pipelines. (3) If `decision_reason` is reflected in any user-facing output, XSS is possible.

**Recommendation:**  
(1) Add a `CHECK (length(decision_reason) <= 500)` constraint to the `hitl_audit_log` DDL, and enforce the same limit in the Pydantic request model for `POST /sessions/{session_id}/approve`. (2) In the structured logging middleware, sanitise `decision_reason` before logging — strip newlines, ANSI codes, and other control characters. (3) Document the field length limit in §5.2 DDL and §6.3 API contract.

---

### REVIEW-27

**ID:** REVIEW-27  
**Severity:** MEDIUM  
**Affected section:** §2.1 Graph Construction, §5.3 Redis Key Namespace, NFR-6  
**Category:** Missing Detail

**Finding:**  
SPEC.md §12.8 (Deferred Decisions) acknowledges that the concurrency strategy for LangGraph threads — distributed lock vs. optimistic locking — is "deferred to architecture phase" and "documented in DESIGN.md." DESIGN.md does not document this decision anywhere. The design section for the graph (§2.1) and the NFR-6 scalability requirement both treat concurrent access to the same `(user_id, session_id)` as a resolved problem, but no mechanism is described.

Without a concurrency strategy, two simultaneous requests to the same thread (e.g., a HITL approval resume while the previous turn is still running) will result in both requests loading the same checkpoint version from PostgreSQL, processing independently, and then each writing a new checkpoint — whichever writes last wins, and the other's state update is silently lost.

**Risk:**  
Concurrent requests to the same LangGraph thread cause one request's state update to be silently overwritten by the other. Depending on timing, this could cause: (a) a HITL approval to be overwritten by a concurrent new-turn invocation, resulting in the approval being lost; (b) two turns producing responses that are both written to `messages`, with the second turn's write overwriting the first.

**Recommendation:**  
Make an explicit decision and document it in §2.1. The two options:
- **Distributed lock per thread:** Acquire a Redis lock on `"graph:lock:{thread_id}"` before any `graph.invoke()` or `graph.ainvoke()`. Reject concurrent requests with HTTP 409. Simple but serialising.
- **Optimistic locking:** Use LangGraph's checkpoint version field. On concurrent write conflict, retry the losing operation. More complex but more concurrent.

For v1 with single-user scope (A-1), the distributed lock approach is simpler. Add the `graph:lock:{thread_id}` key to §5.3 Redis Key Namespace.

---

### REVIEW-28

**ID:** REVIEW-28  
**Severity:** LOW  
**Affected section:** §2.5 Tool Definitions (`WebSearchTool`), FR-4, SC-4  
**Category:** Missing Detail

**Finding:**  
`WebSearchTool` imports `from tavily import TavilyClient`. The standard Tavily Python SDK provides a synchronous client. The tool's `execute` method is declared `async def execute(...)`, but if it calls the synchronous `TavilyClient.search()` internally, that call blocks the asyncio event loop for the duration of the HTTP request (typically 1–3 seconds). This violates SC-4 (all handlers must be async) and will cause visible latency degradation under concurrent load.

The design does not specify whether to use `asyncio.to_thread()` (to run the sync client in a thread pool) or the Tavily async client (`AsyncTavilyClient`, if available).

**Risk:**  
Synchronous blocking in an `async def` method blocks the event loop during web search, preventing any other coroutine (SSE event emission, quota check, heartbeat) from running for the duration of the Tavily API call. Under 5 concurrent users (A-1), all 5 running web searches simultaneously would block the event loop for up to 3 seconds each, serially — effectively serialising all concurrent requests.

**Recommendation:**  
Use `asyncio.to_thread()` if Tavily has no async client, or use `AsyncTavilyClient` if available:
```python
# Option A: wrap sync client
result = await asyncio.to_thread(self._client.search, query=tool_input["query"])

# Option B: use async client if available
result = await self._async_client.search(query=tool_input["query"])
```
Add an explicit note in §2.5 stating that all tool `execute()` methods must be truly non-blocking, and that sync SDK clients must be wrapped with `asyncio.to_thread()`.

---

### REVIEW-29

**ID:** REVIEW-29  
**Severity:** LOW  
**Affected section:** §6.2 Chat Endpoints, §9 Security Model  
**Category:** Security

**Finding:**  
`GET /chat/stream` opens a long-lived SSE connection with no connection limit per user or per IP. An attacker can open thousands of SSE connections to `GET /chat/stream?stream_id=<uuid>` — since `stream_id` is a UUID, they can generate valid-looking (but non-existent) UUIDs. Each connection is held open until the 5-minute (or 15-minute HITL) TTL expires. With no backpressure, this is a straightforward resource exhaustion attack (file descriptors, event loop tasks, Redis buffer reads).

The design's auth model requires a valid JWT for the SSE endpoint (§9.3), which limits unauthenticated DoS, but an authenticated user or a guest with a valid JWT (obtained freely from `POST /auth/guest`) can still open many connections.

**Risk:**  
A single authenticated attacker can exhaust the server's file descriptor limit or event loop concurrency by opening thousands of idle SSE connections. Render's free/starter tier has strict resource limits; this attack is practical with a basic script.

**Recommendation:**  
Add a per-user or per-IP limit on concurrent open SSE connections (e.g., `MAX_SSE_CONNECTIONS_PER_USER=5`), enforced via a Redis counter:
```python
conn_count = await redis.incr(f"sse:connections:{user_id}")
if conn_count > MAX_SSE_CONNECTIONS:
    raise HTTPException(status_code=429, detail="Too many concurrent streams")
```
Decrement the counter when the SSE connection closes (using a `finally` block in the streaming generator). Document this limit in §6.2 and §9.

---

### REVIEW-30

**ID:** REVIEW-30  
**Severity:** LOW  
**Affected section:** §5.2 Table DDL (`conversations`), §6.3 Sessions Endpoints, FR-19  
**Category:** Conflicting Design

**Finding:**  
FR-19 (SPEC.md) requires session titles to be "auto-generated from the first user message, truncated to **60 characters**." The `conversations` table DDL in §5.2 defines `title VARCHAR(100) NOT NULL` — a 100-character limit. This inconsistency means the DB schema allows 40 characters more than the spec requires.

The excess capacity is not harmful in itself, but it indicates the design was not cross-validated against the spec. If the application enforces the 60-character truncation in code (as it should per FR-19), the VARCHAR(100) is simply over-provisioned. However, if a developer reads the DDL rather than the spec, they may implement 100-character titles, violating FR-19's acceptance criterion.

**Risk:**  
Low risk in isolation. But inconsistencies between the spec and DDL erode trust in the design as a source of truth. If a future change extends the title length, the DDL is changed but the application code (enforcing 60 chars) is not updated, and titles silently get truncated at 60 even though the DB accepts 100.

**Recommendation:**  
Change the DDL to `title VARCHAR(60) NOT NULL` to match FR-19. Enforce the 60-character limit at the database layer with a CHECK constraint: `CHECK (char_length(title) <= 60)`. Remove the need for application-layer enforcement as the sole guard.

---

### REVIEW-31

**ID:** REVIEW-31  
**Severity:** LOW  
**Affected section:** §5.2 Table DDL (`hitl_approvals`), §5.2 Table DDL (`conversations`), FR-11, FR-22  
**Category:** Missing Detail

**Finding:**  
FR-22 requires the retention job to "not delete a session that has an open, non-expired HITL checkpoint." The `hitl_approvals` table has `used BOOLEAN` and `expired BOOLEAN` fields, but no description of how the retention job queries these to exclude protected conversations. The join condition is non-trivial:

```sql
-- Conversations to protect from deletion:
SELECT DISTINCT conversation_id FROM hitl_approvals
WHERE used = false AND expired = false
```

This subquery must be executed and its results excluded from the DELETE. However, the design provides no sketch of the retention job's full SQL, and the partial index on `conversations` does not account for this HITL exclusion — it indexes only `last_accessed` and `access_count`.

Additionally, FR-11 specifies that after a 10-minute timeout, the `approval_required` event is emitted and the pending checkpoint is marked `status: expired`. But `hitl_approvals` has no `status` column — only separate `used` and `expired` boolean columns. A timeout should set `expired = true`, but the design never shows the background job or process that sets this flag. If no process sets `expired = true`, all timed-out approvals remain `used=false, expired=false` forever, protecting their conversations from the retention job indefinitely.

**Risk:**  
Conversations with timed-out HITL approvals (where no user action was taken) will never be cleaned up by the retention job if `expired` is never set to `true`. Over time, this accumulates orphaned conversations in the database, growing unboundedly.

**Recommendation:**  
(1) Add a background task (or extend the retention job) that sets `expired = true` on `hitl_approvals` where `expires_at < NOW() AND used = false AND expired = false`. This should run every minute or be triggered by the HITL timeout check. (2) Add the retention job's full SQL sketch to §5.2 or §7, including the HITL exclusion join. (3) Add an index on `hitl_approvals(used, expired, expires_at)` to make the timeout scan efficient.

---

### REVIEW-32

**ID:** REVIEW-32  
**Severity:** LOW  
**Affected section:** §5.2 Table DDL (`conversations.active_model`), §6.3 `POST /sessions/{session_id}/model`, FR-25  
**Category:** Missing Detail

**Finding:**  
`POST /sessions/{session_id}/model` allows the user to switch the active LLM mid-session. The design describes updating `conversations.active_model` in PostgreSQL. However, `AgentState.active_model` is also part of the checkpointed graph state. When the graph resumes for the next turn, it rehydrates `AgentState` from the latest checkpoint — which has the old `active_model` value. The model switch in PostgreSQL is not reflected in the checkpointed `AgentState`.

The design does not describe how the new `active_model` value from PostgreSQL propagates into the restored `AgentState` for the next turn. Possible approaches: (a) the runner reads `conversations.active_model` from the DB before each turn and overrides the checkpointed value; (b) the model switch also updates the LangGraph checkpoint directly; (c) the model switch is queued and applied by `router` at the start of the next turn. None of these is documented.

**Risk:**  
After `POST /sessions/{session_id}/model`, the next turn still uses the old model (from the checkpoint), not the newly selected model. The user receives no confirmation that the switch took effect, and the response is generated by the wrong model. This breaks FR-25.

**Recommendation:**  
Specify the model propagation mechanism explicitly. The recommended approach is (a): before invoking the graph for a new turn, the runner reads `conversations.active_model` from PostgreSQL and injects it as a state override in the invocation:
```python
current_model = await conversation_repo.get_active_model(session_id)
state_override = {"active_model": current_model}
await graph.ainvoke(state_override, config=config)
```
Document this in §6.3 (`POST /sessions/{session_id}/model`) and in `agents/runner.py` responsibilities (§3 Folder Structure).

---

## Statistics

| Category | Count |
|---|---|
| Spec Gap (FR/NFR not addressed in design) | 0 |
| Scalability | 5 |
| Security | 6 |
| LangGraph Design | 12 |
| Missing Detail | 5 |
| Conflicting Design | 4 |
| **Total findings** | **32** |

| Severity | Count |
|---|---|
| CRITICAL | 5 |
| HIGH | 9 |
| MEDIUM | 12 |
| LOW | 6 |
| **Total** | **32** |

---

## Spec Coverage Summary

All 33 functional requirements (FR-1 through FR-33) and all 18 non-functional requirements (NFR-1 through NFR-18) have a corresponding design element in DESIGN.md. No FR or NFR is completely absent from the design. However, the following requirements are addressed **only vaguely** and require additional design detail before implementation can begin:

| Requirement | Gap |
|---|---|
| FR-9 | `interrupt_before` contradicts the described `hitl_gate` behaviour — design element exists but is internally inconsistent (REVIEW-1) |
| FR-10 | HITL resume flow will loop indefinitely due to stale `pending_approval` in checkpoint (REVIEW-2) |
| FR-7 | `tool_results` reducer absent — concurrent writes silently drop data (REVIEW-3) |
| FR-8, FR-13 | SSE emission mechanism unspecified — the "how" of pushing events from nodes is completely missing (REVIEW-4) |
| FR-25 | Model switch does not propagate into the checkpointed state (REVIEW-32) |
| FR-22 | Retention job SQL not sketched; 60-day vs. 90-day index mismatch; expired HITL flag never set (REVIEW-22, REVIEW-31) |
| NFR-6 | LangGraph concurrency strategy deferred in spec but never filled in by design (REVIEW-27) |
