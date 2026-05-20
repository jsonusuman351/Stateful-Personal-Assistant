# SPEC Review: Stateful Personal Assistant
## Production-Readiness Audit of `docs/SPEC.md` v1.0

**Review date:** 2026-05-20  
**Reviewer:** Senior Principal Engineer (Adversarial Audit)  
**Spec version audited:** 1.0 (Draft)

---

## Summary Table

| Finding ID | Severity | One-line Description | Affected Requirement |
|---|---|---|---|
| REVIEW-1 | CRITICAL | HITL approval replay not closed across server restarts | NFR-16, FR-10 |
| REVIEW-2 | CRITICAL | No brute-force lockout or rate limiting on `/auth/login` | FR-16 |
| REVIEW-3 | CRITICAL | Refresh tokens not rotated on use — session hijacking risk | FR-16 |
| REVIEW-4 | CRITICAL | No token blacklist for logout — revoked access tokens remain valid for up to 1 hour | FR-16 |
| REVIEW-5 | CRITICAL | FR-24 (30 s timeout triggers fallback) conflicts with FR-25 (queue 60 s before fallback) | FR-24, FR-25 |
| REVIEW-6 | CRITICAL | No password hashing algorithm specified — implementation may use insecure hashing | FR-16 |
| REVIEW-7 | HIGH | Account enumeration on login — timing/response distinguishes valid vs. invalid email | FR-16 |
| REVIEW-8 | HIGH | JWT claim validation (iss, aud, nbf) not specified — unsigned/tampered tokens may be accepted | FR-15, FR-16 |
| REVIEW-9 | HIGH | Guest user can access session history endpoint — IDOR via `session_id` guessing | FR-19, FR-20, FR-21 |
| REVIEW-10 | HIGH | SSE replay buffer 5-minute TTL is insufficient for HITL scenarios (10-minute timeout) | FR-14, FR-11, NFR-8 |
| REVIEW-11 | HIGH | HITL race condition: two parallel requests hitting the same HITL gate simultaneously | FR-9, FR-10 |
| REVIEW-12 | HIGH | PostgreSQL checkpointer write failure mid-HITL leaves graph in an inconsistent state | FR-9, FR-18 |
| REVIEW-13 | HIGH | No database schema migration rollback strategy — A-9 assumes forwards-compatibility without enforcement | A-9, NFR-10 |
| REVIEW-14 | HIGH | No input sanitisation beyond size limits — injection into LLM prompts, log injection, HTML injection | NFR-15 |
| REVIEW-15 | HIGH | Retention job running concurrently with active session may purge an in-progress conversation | FR-22 |
| REVIEW-16 | HIGH | `FALLBACK_MODELS` malformed JSON causes undefined failure mode — no parse error handling specified | FR-24 |
| REVIEW-17 | HIGH | No audit log for HITL decisions — who approved what and when is unrecoverable | FR-10, FR-11 |
| REVIEW-18 | MEDIUM | SM-9 is untestable — "user-reported context continuity" is not a measurable criterion | SM-9 |
| REVIEW-19 | MEDIUM | NFR-1 and NFR-2 performance targets lack measurement methodology for production (only test-env covered) | NFR-1, NFR-2 |
| REVIEW-20 | MEDIUM | `Last-Event-ID` from a different session is not rejected — cross-session SSE replay possible | FR-14 |
| REVIEW-21 | MEDIUM | LLM returning an empty response is not handled — no specified behaviour or fallback | FR-6, FR-29 |
| REVIEW-22 | MEDIUM | Tool result exceeding context window not handled — truncation strategy absent | FR-7, FR-4 |
| REVIEW-23 | MEDIUM | Fallback model list may be empty at runtime — no requirement to validate non-empty at startup | FR-24, FR-29 |
| REVIEW-24 | MEDIUM | Tavily result set empty after filtering — LLM receives no search results with no defined behaviour | FR-4 |
| REVIEW-25 | MEDIUM | Redis eviction under memory pressure can destroy guest sessions and quota counters despite explicit TTLs | A-4, NFR-8 |
| REVIEW-26 | MEDIUM | Single-writer assumption for LangGraph checkpoint thread not stated — multi-worker race condition risk | NFR-6, FR-18 |
| REVIEW-27 | MEDIUM | LangSmith connectivity failure mode not specified — silent data loss or application crash | NFR-17 |
| REVIEW-28 | MEDIUM | Guest quota counters keyed by `session_id` are ineffective — a guest can bypass quotas by getting a new session token | FR-27, FR-15 |
| REVIEW-29 | MEDIUM | No idempotency key on `POST /chat` — network retries cause duplicate message submissions | FR-12 |
| REVIEW-30 | MEDIUM | No SSE heartbeat/keep-alive specified — proxies and load balancers will drop idle SSE connections during HITL wait | FR-14, FR-11 |
| REVIEW-31 | MEDIUM | Session title auto-generation collision not handled — two sessions with identical first messages get identical titles | FR-19 |
| REVIEW-32 | MEDIUM | No CSRF protection for cookie-based auth paths — spec mentions JWT but not cookie delivery mechanism | FR-15, FR-16 |
| REVIEW-33 | MEDIUM | Secrets scanner in CI (NFR-12) lacks specificity — no scanner config, baseline, or exception process defined | NFR-12 |
| REVIEW-34 | LOW | FR-26 "retry twice" before fallback (FR-29 says retry primary 2 times) vs. FR-24's own retry count are ambiguous | FR-24, FR-26, FR-29 |
| REVIEW-35 | LOW | OpenAI token counting approximation can cause quota drift — quota enforced on approximate counts | FR-27 |
| REVIEW-36 | LOW | `GET /models` endpoint referenced in FR-5 acceptance criterion but never formally specified | FR-5 |
| REVIEW-37 | LOW | No load-shedding or backpressure mechanism specified for sustained overload | NFR-6, SC-1 |
| REVIEW-38 | LOW | Health check depth not specified — `/readiness` may pass while downstream services are unreachable | NFR-10 |

---

## Detailed Findings

---

### REVIEW-1

**ID:** REVIEW-1  
**Severity:** CRITICAL  
**Affected requirement(s):** NFR-16, FR-10, FR-11  
**Category:** Security Gap

**Finding:**  
NFR-16 requires that a replayed `approval_id` returns HTTP 410. However, the replay window is closed only "in the database" — it depends on the `used` flag being persisted. The spec does not require that the set of consumed `approval_id` values survive a server restart. If the application is restarted (crash, deploy, SIGTERM) between the approval submission and the database write that marks the ID as used, the same `approval_id` can be replayed against a fresh process and accepted. Additionally, the spec does not close the cross-session replay window atomically: it checks existence, session match, non-used, and non-expired in sequence (NFR-16 a/b/c) rather than as a single atomic compare-and-set operation, creating a TOCTOU (time-of-check time-of-use) vulnerability window.

**Risk:**  
An attacker who observes an `approval_id` (e.g., via a logged SSE payload) can replay it after a server restart or race a concurrent request to approve a sensitive tool call (web search) without the user's knowledge.

**Recommendation:**  
Add a new requirement: the `approval_id` must be consumed atomically using a `UPDATE ... WHERE id=? AND used=false AND expired=false RETURNING id` pattern (one database roundtrip). If zero rows are affected, return HTTP 410 without further checks. Document that this operation must be durable before the graph is resumed, and that the check must be enforced even across process restarts (i.e., the state lives in PostgreSQL, never only in memory).

---

### REVIEW-2

**ID:** REVIEW-2  
**Severity:** CRITICAL  
**Affected requirement(s):** FR-16, Section 6.4  
**Category:** Missing Requirement

**Finding:**  
The spec specifies no rate limiting on `POST /auth/login` and `POST /auth/refresh` beyond the general LLM quota enforcement (FR-27). There is no brute-force lockout mechanism — no temporary account lock after N failed attempts, no CAPTCHA, no exponential delay, and no IP-based throttling. FR-27 applies only to OpenAI model calls, not to authentication endpoints.

**Risk:**  
An attacker can attempt unlimited password guesses against any known email address. Given the JWT access token expires in 1 hour and the refresh token in 30 days, a successful brute-force gives long-term account access. Even for a single-user system, this represents a complete authentication bypass path.

**Recommendation:**  
Add a new requirement (e.g., FR-31): `POST /auth/login` must enforce a per-IP and per-email rate limit (e.g., 10 attempts per 15 minutes). After 5 consecutive failures for the same email, the account must be soft-locked for 15 minutes. The lock duration must be configurable via environment variable. This is separate from FR-27. Implement via Redis counters with appropriate TTLs.

---

### REVIEW-3

**ID:** REVIEW-3  
**Severity:** CRITICAL  
**Affected requirement(s):** FR-16  
**Category:** Security Gap

**Finding:**  
FR-16 specifies that refresh tokens are "stored server-side and invalidated on logout." It does not require refresh token rotation on use. This means a stolen refresh token remains valid for 30 days regardless of how many times the legitimate user calls `POST /auth/refresh`. If an attacker exfiltrates the refresh token from storage (cookie theft, XSS, log exposure), they can silently generate new access tokens indefinitely without triggering any detection mechanism.

**Risk:**  
Refresh token theft provides persistent 30-day access with no mechanism to detect concurrent token usage. The legitimate user sees no anomaly because their own refresh succeeds too.

**Recommendation:**  
Update FR-16 to require refresh token rotation: each call to `POST /auth/refresh` must invalidate the presented refresh token and issue a new one. The server must maintain a one-to-one mapping of valid refresh tokens. Optionally implement refresh token families: if a previously invalidated refresh token is presented, immediately invalidate the entire family (detecting theft). Update the test in FR-16(d) to assert that the old refresh token is invalid after a successful refresh.

---

### REVIEW-4

**ID:** REVIEW-4  
**Severity:** CRITICAL  
**Affected requirement(s):** FR-16  
**Category:** Security Gap

**Finding:**  
FR-16 requires that refresh tokens are invalidated on logout (`POST /auth/refresh` with an invalidated token returns HTTP 401). However, the spec does not address access token revocation. Access tokens expire in 1 hour, meaning after a user logs out, their access token remains cryptographically valid for up to 59 minutes. Any system that receives the access token (including the application itself) will accept it until natural expiry. There is no token blacklist requirement.

**Risk:**  
If an access token is stolen (e.g., from a browser log, an XSS payload, or a compromised third-party system), the attacker retains valid API access for up to 1 hour after the victim logs out. For a personal assistant that can trigger web searches on the user's behalf, this is a meaningful risk.

**Recommendation:**  
Add a new requirement specifying a Redis-backed access token blacklist. On `POST /auth/logout`, the presented access token's `jti` (JWT ID) claim must be stored in Redis with a TTL equal to the token's remaining lifetime. All protected endpoints must check this blacklist before processing the request. Add `jti` to the required JWT claims in FR-16. Update NFR-8 to include blacklist entries in the TTL table.

---

### REVIEW-5

**ID:** REVIEW-5  
**Severity:** CRITICAL  
**Affected requirement(s):** FR-24, FR-25  
**Category:** Conflicting Requirements

**Finding:**  
FR-24 states: "Fallback is triggered automatically when the primary model call does not return a first token within **30 seconds**." FR-25 states: "When the primary model is unresponsive (timeout after 30 seconds), the request must be placed in a Redis-backed queue and held for **up to 60 seconds** before the next fallback model is attempted." These two requirements directly contradict each other. FR-24 says the fallback is triggered at 30 s; FR-25 says the fallback is attempted after an additional 60 s queue hold, i.e., 90 s from request start. The acceptance criterion for FR-24 says "the system retries twice then invokes the first fallback model" (citing FR-26), while FR-25 adds a 60 s queuing step between the two. The actual elapsed time before the user gets a response from the fallback could be anywhere from 30 s to 90 s depending on which requirement is implemented.

**Risk:**  
Developers implementing these two requirements independently will produce inconsistent behaviour. One interpretation silently discards the 60 s queue; the other makes the user wait 90 s for a fallback response. Neither path is explicitly the intended one. In production, a 90-second wait for a response is an unacceptable user experience and will trigger client-side timeouts on most HTTP clients.

**Recommendation:**  
Reconcile FR-24 and FR-25 into a single, unambiguous timeline. Suggested resolution: (a) primary model timeout = 30 s; (b) user is immediately notified via SSE and offered a model-switch option; (c) if the user does not act within N seconds, the fallback is attempted automatically; (d) the Redis queue is used only to persist the request across the notification window, not as an additional hold. Remove the separate "60 seconds before fallback" clause from FR-25 or reframe it as "the user has up to 60 s to choose before auto-fallback occurs."

---

### REVIEW-6

**ID:** REVIEW-6  
**Severity:** CRITICAL  
**Affected requirement(s):** FR-16, Section 6.4  
**Category:** Missing Requirement

**Finding:**  
FR-16 specifies email and password login and that invalid credentials return HTTP 401. It does not specify how passwords are hashed and stored. There is no requirement naming a hashing algorithm (bcrypt, Argon2, scrypt, PBKDF2), a minimum work factor/cost parameter, or a requirement to use a salt. Without this specification, an implementation could store passwords in plaintext, MD5, SHA-1, or any other unsuitable form, all of which would pass the acceptance criteria as written.

**Risk:**  
Plaintext or weakly hashed passwords in PostgreSQL expose all user credentials in the event of a database dump or SQL injection. Even for a single-developer system, this is a textbook OWASP A02 failure.

**Recommendation:**  
Add an explicit requirement (or expand FR-16): passwords must be hashed using Argon2id (preferred) or bcrypt with a minimum cost factor of 12. The raw password must never be stored, logged, or returned in any response. Add a test asserting that the stored value in the `users` table does not equal the plaintext password. Reference `passlib` or `argon2-cffi` as the approved implementation library.

---

### REVIEW-7

**ID:** REVIEW-7  
**Severity:** HIGH  
**Affected requirement(s):** FR-16  
**Category:** Security Gap

**Finding:**  
The acceptance criterion for FR-16 states: "valid credentials return two tokens" and "invalid credentials return HTTP 401." It does not distinguish between "email not found" and "email found but password incorrect." Without an explicit requirement to return the same response body, the same HTTP status, and the same response time for both cases, implementations commonly produce different error messages ("user not found" vs. "incorrect password") or exhibit measurable timing differences (a missing user skips password hashing; a found user performs an expensive hash comparison). Either difference enables account enumeration.

**Risk:**  
An attacker can enumerate valid email addresses registered in the system, then focus brute-force attempts exclusively on confirmed accounts.

**Recommendation:**  
Add to FR-16: the response body and HTTP status code for "email not found" must be identical to "email found, wrong password." The response time must be equalised using a constant-time dummy hash operation when the user does not exist (e.g., `passlib.context.verify_and_update` against a static dummy hash). Add a test asserting response body equality for both failure paths.

---

### REVIEW-8

**ID:** REVIEW-8  
**Severity:** HIGH  
**Affected requirement(s):** FR-15, FR-16, NFR-16  
**Category:** Security Gap

**Finding:**  
The spec does not require validation of JWT standard claims beyond `exp` (expiry) and `mode`/`session_id` (custom claims). There is no requirement to validate `iss` (issuer), `aud` (audience), or `nbf` (not-before). Without `iss` validation, a JWT signed by any service using the same algorithm and a guessable secret (or none) would be accepted. Without `aud` validation, a token issued for a different service in the same infrastructure would be accepted. Without `nbf` validation, a pre-issued token can be used before its intended activation time.

**Risk:**  
Algorithm confusion attacks (e.g., changing `alg` to `none` or `HS256` if the server supports `RS256`) and cross-service token reuse are well-documented JWT attack vectors. The spec's current validation is insufficient to prevent these.

**Recommendation:**  
Add a requirement specifying the exact JWT validation steps: (1) verify signature using the server's secret; (2) reject tokens with `alg: none`; (3) validate `exp` (reject expired); (4) validate `nbf` (reject not-yet-valid); (5) validate `iss` matches the configured issuer string; (6) validate `aud` matches the configured audience string. Specify the signing algorithm (e.g., HS256 with a minimum 256-bit secret or RS256). Add these as testable acceptance criteria.

---

### REVIEW-9

**ID:** REVIEW-9  
**Severity:** HIGH  
**Affected requirement(s):** FR-19, FR-20, FR-21  
**Category:** Security Gap / Missing Edge Case

**Finding:**  
FR-19 and FR-20 correctly require HTTP 403 for cross-user access and for guest users. However, FR-21 specifies that guest session state is stored under `guest:<session_id>:state` in Redis. A guest `session_id` is issued in a JWT (FR-15). The spec does not prevent a guest user from calling `GET /sessions/{session_id}/messages` with a `session_id` that belongs to an authenticated user — the only guard is the HTTP 403 for guests, but that check in FR-20 applies to the endpoint overall. If the implementation checks "is this a guest?" before checking "does this session belong to this user?", a guest with a crafted request could probe the existence of session IDs (IDOR via timing differences or error message differences) even if they cannot read the content.

**Risk:**  
Information leakage about the existence and structure of other users' sessions. This becomes more serious if `session_id` values are predictable or sequential.

**Recommendation:**  
Strengthen FR-19 and FR-20: the check order must be (1) is the token valid? (2) is the user authenticated (not guest)? (3) does the session_id belong to the authenticated user? All three checks must return indistinguishable HTTP 403 responses for checks (2) and (3) — do not reveal which check failed. Add `session_id` as a UUID v4 requirement (already implied by NFR-16 for approval IDs, but not stated for session IDs).

---

### REVIEW-10

**ID:** REVIEW-10  
**Severity:** HIGH  
**Affected requirement(s):** FR-14, FR-11, NFR-8  
**Category:** Conflicting Requirements

**Finding:**  
FR-14 specifies that SSE events are stored in Redis with a TTL of **5 minutes after the `done` event**. FR-11 specifies that HITL approval may take up to **10 minutes**. During the HITL wait, no `done` event is emitted (the graph is suspended). If a client disconnects at minute 4 of a HITL wait and attempts to reconnect at minute 6, the SSE replay buffer TTL has not yet started (no `done` event), so the buffer should still be alive. However, the spec sets the TTL at write time (NFR-8), not at `done` time. NFR-8 says "SSE replay buffer — 5 minutes after `done` event" but also says "TTL set at write time." These two are contradictory: a write-time TTL cannot dynamically extend to 5 minutes after a future `done` event.

**Risk:**  
During a 10-minute HITL approval window, the initial SSE events (including the `approval_required` event itself) will expire from Redis before the `done` event is emitted if the TTL is applied at write time. A reconnecting client will lose all context about the pending approval. This is confirmed as a known risk in R-4, but the spec's proposed mitigation ("HITL pause emits events only on approval, keeping the buffer fresh") is vague and contradicts NFR-8's write-time TTL mandate.

**Recommendation:**  
Resolve the contradiction in NFR-8 by specifying that the SSE replay buffer TTL is **extended** (using Redis `EXPIRE` or `EXPIREAT`) each time a new event is written. The buffer should remain alive for at least `HITL_TIMEOUT + reconnection_grace_period` (i.e., at least 12 minutes) for streams that have an `approval_required` event. Alternatively, store HITL-state SSE events in PostgreSQL alongside the checkpoint for durability, and use Redis only for non-HITL streams.

---

### REVIEW-11

**ID:** REVIEW-11  
**Severity:** HIGH  
**Affected requirement(s):** FR-9, FR-10  
**Category:** Missing Edge Case

**Finding:**  
The spec does not address what happens when two parallel requests (e.g., from two browser tabs or two API clients) submit `POST /sessions/{session_id}/approve` with the same `approval_id` simultaneously. Both requests will pass the `approval_id` validity check (REVIEW-1 addresses the atomicity problem in part), and both may attempt to resume the LangGraph graph from the same checkpoint concurrently. LangGraph's PostgreSQL checkpointer does not inherently prevent concurrent graph resumption from the same checkpoint thread.

**Risk:**  
Concurrent graph resumption from the same checkpoint causes the tool to execute twice (web search runs twice, billing is doubled), produces two conflicting SSE streams for the same `stream_id`, and leaves the checkpoint in an undefined state.

**Recommendation:**  
Add a requirement specifying that the approval endpoint must use a distributed lock (Redis `SET NX PX` or PostgreSQL advisory lock) keyed by `approval_id` for the duration of the graph resumption operation. Only one approval request per `approval_id` can hold the lock at any time. The second concurrent request must receive HTTP 409 (Conflict) if the lock is already held. This is separate from the TOCTOU fix in REVIEW-1.

---

### REVIEW-12

**ID:** REVIEW-12  
**Severity:** HIGH  
**Affected requirement(s):** FR-9, FR-18, NFR-9  
**Category:** Missing Edge Case

**Finding:**  
FR-9 requires that the graph state is written to the PostgreSQL checkpointer before the `approval_required` SSE event is emitted. The spec does not specify what happens if this write fails (e.g., PostgreSQL is temporarily unavailable, the connection pool is exhausted, or a transaction deadlock occurs). The SSE event may be emitted, the client submits an approval, but there is no checkpoint to resume from.

**Risk:**  
The user approves the action, the system attempts to resume the graph from a checkpoint that was never written, and the graph crashes. The user's approval is consumed (NFR-16 marks it used), but the tool never executes. The session is left in a permanently broken state with no recovery path specified.

**Recommendation:**  
Add a requirement that the checkpoint write must be confirmed (commit acknowledged) before the `approval_required` SSE event is emitted. The write must be wrapped in a database transaction that is rolled back on failure, and the SSE event must not be emitted if the transaction fails. On failure, an `error` SSE event must be emitted instead. Specify that the `approval_id` must only be written to the database within the same transaction as the checkpoint, ensuring both are atomic.

---

### REVIEW-13

**ID:** REVIEW-13  
**Severity:** HIGH  
**Affected requirement(s):** A-9, NFR-10  
**Category:** Hidden Assumption

**Finding:**  
A-9 explicitly states: "Alembic migrations are always forwards-compatible; no rollback migrations are required for v1." NFR-10 requires that migrations run to the latest revision on startup. Together, these mean a failed migration or a bad deployment leaves the database at a partially applied revision with no recovery path. The spec does not require: (a) migration rollback scripts even as a break-glass procedure; (b) testing of the migration path in CI (only tests that the app starts against revision N-1); (c) a pre-migration database backup step; or (d) a migration dry-run (`alembic check`) before applying.

**Risk:**  
A migration that corrupts a production table (e.g., a column type change that fails mid-table on a large dataset) leaves the database in an inconsistent state. The application exits with a non-zero code (correct, per NFR-10), but there is no specified procedure to recover. On Render's managed PostgreSQL, point-in-time recovery requires manual intervention.

**Recommendation:**  
Remove or weaken A-9. Add a requirement that every Alembic migration file must include a working `downgrade()` function, even if the function is a no-op stub that raises `NotImplementedError` with a comment. CI must run `alembic downgrade -1` after `alembic upgrade head` in the test database and verify the database returns to revision N-1 without error. Document the rollback procedure in `DESIGN.md`.

---

### REVIEW-14

**ID:** REVIEW-14  
**Severity:** HIGH  
**Affected requirement(s):** NFR-15, FR-3, FR-16  
**Category:** Missing Requirement

**Finding:**  
NFR-15 specifies maximum input sizes (4,000 characters for chat, 60 characters for session title, 512 bytes for approval body) but does not specify any content sanitisation requirements beyond size. There is no requirement to: (a) strip or escape control characters from user input before injecting into LLM prompts (prompt injection); (b) sanitise log fields (log injection via newline characters in user input that fabricate fake log lines); (c) strip null bytes from input strings; (d) normalise Unicode to prevent homoglyph attacks on email addresses. The calculator tool specifies safe evaluation (FR-3), but the general input pipeline has no sanitisation layer.

**Risk:**  
A user can inject a newline character into a message and fabricate false structured log lines (NFR-18), manipulate LLM behaviour via prompt injection (e.g., "ignore previous instructions"), or cause downstream APIs to misbehave with malformed Unicode or null bytes.

**Recommendation:**  
Add a new requirement specifying an input sanitisation middleware layer applied to all endpoints: (a) reject inputs containing null bytes (`\x00`); (b) strip leading/trailing whitespace; (c) normalise Unicode to NFC form; (d) add a note that prompt injection is a known risk and mitigation (e.g., system-prompt hardening) must be documented in `DESIGN.md`. Do not attempt to prevent all prompt injection at the input layer — document this as a residual risk.

---

### REVIEW-15

**ID:** REVIEW-15  
**Severity:** HIGH  
**Affected requirement(s):** FR-22  
**Category:** Missing Edge Case

**Finding:**  
FR-22 specifies a background job that permanently deletes conversations where both purge conditions are met. The spec does not address the race condition where the retention job begins evaluating a conversation at minute 0, the user accesses the session at minute 0 (incrementing `access_count` and updating `last_accessed`), and the job completes the deletion at minute 1 using the stale values it read at minute 0. The job reads the conditions in one query and deletes in another, without requiring these to be a single atomic transaction scoped to the evaluated rows.

**Risk:**  
An active, recently accessed conversation is permanently deleted, destroying the user's history. This is especially likely if the daily job runs at midnight when the user might be in an active session.

**Recommendation:**  
Update FR-22: the retention job must perform the condition check and the deletion as a single atomic statement: `DELETE FROM conversations WHERE id IN (SELECT id FROM conversations WHERE last_accessed < NOW() - INTERVAL '90 days' AND access_count < 5 FOR UPDATE SKIP LOCKED)`. The `FOR UPDATE SKIP LOCKED` clause ensures in-progress sessions are not evaluated. Additionally, the job must not delete a session that has an open, non-expired HITL checkpoint (checked via a join on the checkpoints table).

---

### REVIEW-16

**ID:** REVIEW-16  
**Severity:** HIGH  
**Affected requirement(s):** FR-24, FR-29  
**Category:** Missing Edge Case

**Finding:**  
FR-24 specifies `FALLBACK_MODELS` as a JSON list in an environment variable. The spec does not specify what happens if: (a) `FALLBACK_MODELS` is not set at all; (b) `FALLBACK_MODELS` is set to a malformed JSON string (e.g., `["groq/llama-3-70b"` missing closing bracket); (c) `FALLBACK_MODELS` is set to an empty list `[]`; (d) `FALLBACK_MODELS` contains a model name that is not reachable. FR-29 says "if all fallback models also fail, emit `error` with `retryable: false`" — but this requires at least one fallback to exist. An empty fallback list means the primary failure immediately terminates with no retry attempted, which is not the same as "all fallbacks failed."

**Risk:**  
A misconfigured `FALLBACK_MODELS` environment variable causes a silent failure at the time of primary model failure, not at startup. The application starts successfully, appears healthy, but silently drops requests when the primary model is unavailable. Malformed JSON causes an unhandled exception that may crash the worker process.

**Recommendation:**  
Add a startup validation requirement: at application startup, `FALLBACK_MODELS` must be parsed and validated. If the value is missing, a warning must be logged. If it is malformed JSON, the application must exit with a non-zero code (treated the same as a missing required environment variable). If the list is empty, the application must log a startup warning and set `fallback_enabled: false`. Define the behaviour when no fallbacks are available separately from the "all fallbacks failed" case.

---

### REVIEW-17

**ID:** REVIEW-17  
**Severity:** HIGH  
**Affected requirement(s):** FR-10, FR-11, NFR-18  
**Category:** Missing Requirement

**Finding:**  
The spec includes structured request logging (NFR-18) covering HTTP-level fields, but there is no requirement for an audit log recording HITL decisions. There is no specified requirement to record: which `user_id` approved or denied which `approval_id`, at what timestamp, from which IP address, and for which tool invocation. NFR-18's log covers `method` and `path` but not the semantic content of approval decisions.

**Risk:**  
In any system where a human approves machine actions (especially a web search that could retrieve sensitive content), the absence of an approval audit trail means there is no way to investigate incidents, detect a compromised account approving malicious searches, or verify that the system behaved correctly during a HITL flow. This is a compliance gap even for personal use.

**Recommendation:**  
Add a new requirement (FR-32): every HITL decision (approve or deny) must be written to a dedicated `hitl_audit_log` table with columns: `id`, `approval_id`, `user_id`, `session_id`, `tool_name`, `decision` (`approve`/`deny`/`timeout`), `decided_at` (timestamp), `request_ip`. This table must be append-only (no UPDATE or DELETE). Add a corresponding log line at `INFO` level in NFR-18 for every HITL decision.

---

### REVIEW-18

**ID:** REVIEW-18  
**Severity:** MEDIUM  
**Affected requirement(s):** SM-9  
**Category:** Vague/Untestable

**Finding:**  
SM-9 defines "Conversation resumption accuracy" with the measurement method "Manual verification: resume session after 24 h and confirm prior context is present." This is the only success metric in the table with a subjective, non-automatable measurement method. "Confirm prior context is present" is undefined — it does not specify what constitutes presence (exact message recall, topical coherence, or name/entity recall), who confirms it, or what the pass/fail threshold is.

**Risk:**  
A metric with no automatable measurement cannot be reliably tracked over time. Regressions in context continuity (e.g., caused by a LangGraph checkpoint serialisation bug) will not be caught by CI. "User-reported" metrics disappear entirely in any future handoff of the codebase.

**Recommendation:**  
Replace SM-9 with an automatable criterion: "A test creates a two-turn conversation referencing a specific named entity (e.g., city name), terminates the server, restarts it, resumes the session, and sends a follow-up query. The LLM's response must reference the named entity from the prior turn. The test parses the response and asserts the entity string appears." This is testable and deterministic with a mocked LLM.

---

### REVIEW-19

**ID:** REVIEW-19  
**Severity:** MEDIUM  
**Affected requirement(s):** NFR-1, NFR-2, NFR-3, NFR-4, NFR-5  
**Category:** Vague/Untestable

**Finding:**  
NFR-1 through NFR-5 specify performance targets at "p95" percentile but define measurement methodology only for the test environment ("an integration test with a mocked LLM"). There is no requirement specifying how these targets are measured in production. Without production measurement: (a) the targets are untestable against real behaviour; (b) there is no alerting when a target is breached; (c) the spec does not say whether mocked tests count as acceptance of the NFR or only as a proxy. The OpenAI API, Tavily API, and PostgreSQL are all external — mocking them removes the dominant latency contributors, making test-environment measurements meaningless as production predictors.

**Risk:**  
NFRs pass in CI but are never met in production. The system may routinely exceed 1,500 ms TTFT with a real OpenAI API call, and no one will know because there is no production measurement requirement.

**Recommendation:**  
Add a measurement methodology for each performance NFR that covers production conditions. At minimum: (a) add structured latency logging (already partially covered by NFR-18's `latency_ms`) that records TTFT per request; (b) add a requirement to review latency percentiles from logs weekly; (c) note that the test-environment targets are proxies only and real OpenAI latency is outside system control. Alternatively, add SM-11 tracking p95 TTFT from production logs.

---

### REVIEW-20

**ID:** REVIEW-20  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-14  
**Category:** Security Gap / Missing Edge Case

**Finding:**  
FR-14 specifies that if a client reconnects with `Last-Event-ID`, the server replays events with IDs greater than the provided value. The spec does not require validating that the `Last-Event-ID` belongs to the same `stream_id` as the reconnecting request. A client could open a stream for `stream_id=A`, disconnect, then reconnect to `stream_id=B` with `Last-Event-ID: 3` from stream A. The server would replay events from stream B starting after ID 3 — this is the expected behaviour. However, the spec also does not prevent a client from supplying `Last-Event-ID` from a completely different session's stream, potentially receiving events that contain `tool_result` payloads from another user's session if stream IDs are predictable.

**Risk:**  
Cross-session SSE event replay if stream IDs are predictable UUIDs and the server does not validate that the `stream_id` in the query parameter belongs to the requesting user.

**Recommendation:**  
Update FR-14: the server must validate that the `stream_id` in `GET /chat/stream?stream_id=<uuid>` belongs to the authenticated user (or guest session token) making the request. If the stream_id does not match, return HTTP 403. Add this as an acceptance criterion.

---

### REVIEW-21

**ID:** REVIEW-21  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-6, FR-29, FR-28  
**Category:** Missing Edge Case

**Finding:**  
The spec defines detailed error handling for tool failures (FR-28) and LLM call failures (FR-29) but does not specify the behaviour when the LLM returns a syntactically valid but semantically empty response — for example, an empty string, a response containing only whitespace, or a response with an empty `content` array in the OpenAI response object. This can occur under rate limiting, content filtering, or model bugs.

**Risk:**  
An empty LLM response passes the success check in `tool_executor → llm → done` routing, emits zero `token` events, and emits a `done` event. The user receives a blank response with no indication of failure, no retry, and no fallback. The session appears complete but contains a corrupted turn.

**Recommendation:**  
Add a requirement to the `llm` node: if the LLM response content is empty or whitespace-only, this must be treated as a recoverable error (equivalent to HTTP 5xx), triggering the retry-then-fallback logic in FR-29. Add an acceptance criterion to FR-29 testing this path.

---

### REVIEW-22

**ID:** REVIEW-22  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-7, FR-4, FR-6  
**Category:** Missing Edge Case

**Finding:**  
FR-4 specifies that the web search tool truncates results to 2,000 tokens. However, the spec does not address what happens when a tool result — even after tool-level truncation — causes the total conversation history passed to the LLM to exceed the model's context window. For a multi-turn conversation with many tool results, the cumulative token count can exceed `gpt-4o-mini`'s 128K context limit. There is no requirement for a conversation summarisation strategy, a sliding window, or a hard error.

**Risk:**  
The LLM call fails with a context-length error from the OpenAI API (HTTP 400, not 429 or 503). FR-29's retry logic does not cover HTTP 400 errors, so the request is not retried and falls through to the error path without a useful user message.

**Recommendation:**  
Add a requirement specifying a context management strategy: before each LLM invocation, the total token count of the conversation history must be estimated. If it exceeds a configurable threshold (e.g., 80% of the model's context window), the oldest messages must be summarised or dropped. Alternatively, specify that HTTP 400 with `code: context_length_exceeded` from the OpenAI API is treated as a non-retryable error with a specific user-facing message advising them to start a new session.

---

### REVIEW-23

**ID:** REVIEW-23  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-24, FR-29  
**Category:** Missing Edge Case

**Finding:**  
FR-29 specifies: "If all fallback models also fail, emit `error` with `retryable: false`." This covers the case where fallback models are present but fail. The spec does not specify what happens when `FALLBACK_MODELS` is an empty list or contains only one model that fails. The acceptance criterion for FR-29 tests that "each fallback in `FALLBACK_MODELS` is attempted once" — if the list is empty, this test passes vacuously (zero attempts, zero successes, final error emitted). The system would immediately emit `retryable: false` on any primary model failure, with no fallback attempt and no distinct error code to distinguish "no fallbacks configured" from "all fallbacks exhausted."

**Risk:**  
Operational confusion: an operator sees `retryable: false` errors and does not know whether the fallbacks are failing or were never configured. Monitoring cannot distinguish these cases.

**Recommendation:**  
Define two distinct error codes: `ALL_MODELS_FAILED` (fallbacks existed but failed) and `NO_FALLBACK_CONFIGURED` (fallback list is empty or not set). Add startup validation for `FALLBACK_MODELS` (see REVIEW-16). Add an acceptance criterion distinguishing the two error paths.

---

### REVIEW-24

**ID:** REVIEW-24  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-4  
**Category:** Missing Edge Case

**Finding:**  
FR-4 requires that results below the 0.7 relevance threshold are discarded. The spec does not specify what happens when all results from Tavily fall below 0.7 and the filtered result set is empty. The acceptance criterion tests that results below 0.7 are excluded, but does not test the all-empty case. Similarly, Tavily may return zero results for an obscure query. An empty result set passed to the LLM may cause it to hallucinate or to respond with "I found no results" — but neither behaviour is specified.

**Risk:**  
If the LLM receives an empty web search result, it may fabricate search results (hallucination), silently produce a lower-quality response, or the code building the LLM prompt may throw an exception on an empty list.

**Recommendation:**  
Add to FR-4: if the filtered result set is empty (either all results below threshold or Tavily returned zero results), the tool must return a structured response `{"results": [], "warning": "no_results_above_threshold"}`. The `llm` node must detect this condition and explicitly inform the LLM in the prompt that no web search results were found, instructing it not to invent results. Add an acceptance criterion for this edge case.

---

### REVIEW-25

**ID:** REVIEW-25  
**Severity:** MEDIUM  
**Affected requirement(s):** A-4, NFR-8, FR-27  
**Category:** Hidden Assumption

**Finding:**  
The spec assumes Render's managed Redis will honour explicit TTLs. NFR-8 requires every Redis key to have a TTL. However, Redis eviction policies (e.g., `allkeys-lru` or `volatile-lru`) can evict keys with TTLs before they expire when memory pressure is high. Render's managed Redis on free/starter tiers enforces memory limits. If quota counter keys are evicted under memory pressure, quota enforcement silently fails — users get unlimited OpenAI requests. If guest session keys are evicted, HTTP 410 is returned correctly per FR-21, which is acceptable. If SSE replay buffer keys are evicted, reconnection fails silently.

**Risk:**  
Quota counter eviction allows a user to exceed their OpenAI token quota, potentially incurring unexpected OpenAI billing costs. This is the highest-impact failure: the system is designed to enforce cost controls, but Redis eviction can silently bypass them.

**Recommendation:**  
Add an assumption or requirement: the Redis instance must be configured with `maxmemory-policy noeviction` or `volatile-ttl` (evict keys closest to expiry first, not quota counters which have long TTLs). Document this as a configuration requirement in `DESIGN.md`. Add a startup check that verifies the Redis `maxmemory-policy` is an acceptable value and logs a warning if it is `allkeys-lru` or `allkeys-random`.

---

### REVIEW-26

**ID:** REVIEW-26  
**Severity:** MEDIUM  
**Affected requirement(s):** NFR-6, FR-18  
**Category:** Hidden Assumption

**Finding:**  
NFR-6 requires stateless application processes and states that "adding a second application instance must not require sticky sessions." FR-18 keys checkpointer threads by `(user_id, session_id)`. The spec does not address the scenario where two application instances concurrently handle requests for the same `(user_id, session_id)` thread — for example, if a client rapidly fires two `POST /chat` requests before the first completes. LangGraph's PostgreSQL checkpointer uses optimistic locking (version numbers in checkpoints), but the spec does not acknowledge this, specify the conflict resolution behaviour, or require testing concurrent access.

**Risk:**  
Two concurrent requests to the same session can cause a checkpoint version conflict in PostgreSQL, resulting in one request silently winning and the other silently dropping its state update. The user sees inconsistent conversation history.

**Recommendation:**  
Add a requirement specifying the concurrent-request policy for a single session: either (a) concurrent requests to the same `session_id` must be serialised using a Redis distributed lock, with the second request receiving HTTP 409 until the first completes; or (b) the application must rely on LangGraph's optimistic locking and return HTTP 409 to the client on a version conflict, advising retry. Document the chosen strategy and add a test simulating concurrent requests to the same session.

---

### REVIEW-27

**ID:** REVIEW-27  
**Severity:** MEDIUM  
**Affected requirement(s):** NFR-17  
**Category:** Hidden Assumption

**Finding:**  
NFR-17 requires LangSmith tracing to be enabled in production. The spec does not specify what happens if LangSmith is unreachable (network partition, LangSmith outage, invalid API key). LangChain's default tracing behaviour is to raise an exception or block on failed trace submissions, which would propagate into the agent execution path and cause request failures unrelated to the agent's actual task.

**Risk:**  
A LangSmith outage causes the entire assistant to stop working. Users cannot send messages not because the LLM is down but because an observability side-effect is blocking the main execution path.

**Recommendation:**  
Add to NFR-17: LangSmith tracing must be implemented in a fire-and-forget, non-blocking manner. Trace submission failures must be caught, logged at `WARNING` level, and must not propagate to the request handler. The system must remain fully functional regardless of LangSmith availability. Add a test that mocks LangSmith to throw a connection error and asserts the LLM response is still returned successfully.

---

### REVIEW-28

**ID:** REVIEW-28  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-27, FR-15  
**Category:** Conflicting Requirements

**Finding:**  
FR-27 specifies that guest quotas are tracked per `session_id`. FR-15 specifies that guest JWTs expire in 24 hours and that calling `POST /auth/guest` issues a new token with a new `session_id`. A guest user who exhausts their 4-hour quota (20 requests) can bypass enforcement entirely by calling `POST /auth/guest` again to receive a new `session_id` with a fresh quota counter. There is no IP-based or fingerprint-based quota enforcement for guests.

**Risk:**  
Guest quota enforcement provides no meaningful cost control. A determined user can make unlimited OpenAI requests by repeatedly obtaining new guest tokens. Given the system is designed to control OpenAI costs (G-5), this is a direct threat to that goal.

**Recommendation:**  
Update FR-27: guest quotas must additionally be tracked per source IP address, with a separate Redis counter keyed by `guest_quota:<ip_hash>:<window>`. The IP-based counter must enforce the same limits as the session-based counter. When either limit is exceeded, HTTP 429 must be returned. Note the inherent limitation of IP-based enforcement (shared NAT) and document it as a residual risk.

---

### REVIEW-29

**ID:** REVIEW-29  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-12  
**Category:** Missing Requirement

**Finding:**  
FR-12 specifies the two-step SSE handshake: `POST /chat` returns a `stream_url`, then `GET <stream_url>` opens the stream. There is no idempotency key requirement on `POST /chat`. If a client retries `POST /chat` due to a network error (the POST succeeds at the server but the 202 response is lost in transit), the server will process the message a second time, creating a duplicate entry in the conversation history, consuming quota twice, and potentially triggering a second HITL approval flow.

**Risk:**  
Duplicate messages in conversation history break context continuity and consume double the token quota. For HITL flows, the user is presented with two simultaneous approval prompts for the same action.

**Recommendation:**  
Add a requirement: `POST /chat` must accept an optional `Idempotency-Key` header (UUID). If the same key is presented within a configurable deduplication window (e.g., 60 seconds), the server must return the original 202 response (including the original `stream_url`) without processing the message again. The deduplication state must be stored in Redis with a TTL equal to the deduplication window. Reference RFC 7231 and industry practice for idempotency key semantics.

---

### REVIEW-30

**ID:** REVIEW-30  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-14, FR-11  
**Category:** Missing Requirement

**Finding:**  
The spec does not specify an SSE heartbeat or keep-alive mechanism. During a HITL approval wait (up to 10 minutes, per FR-11), the SSE connection is open but silent — no events are emitted between the `approval_required` event and the eventual approval/denial/timeout event. Most HTTP proxies, load balancers (including Render's infrastructure), and browser implementations close SSE connections that have been idle for 30–60 seconds. The `EventSource` API in browsers will auto-reconnect, but FR-14's reconnection requires `Last-Event-ID`, which depends on the SSE replay buffer being alive (see REVIEW-10).

**Risk:**  
During a HITL wait, the SSE connection is dropped by an intermediary proxy after 30–60 seconds of silence. The client reconnects, but if the replay buffer has issues (REVIEW-10), the `approval_required` event is not replayed and the user does not see the approval prompt.

**Recommendation:**  
Add a requirement: the SSE server must emit a `: keep-alive` comment line (SSE comment, no `event:` field) every 15 seconds while a stream is open and idle. This prevents proxy-level connection timeouts without adding events to the replay buffer (comments are not replayed on reconnect per SSE spec). The keep-alive interval must be configurable via `SSE_KEEPALIVE_INTERVAL` environment variable.

---

### REVIEW-31

**ID:** REVIEW-31  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-19  
**Category:** Missing Edge Case

**Finding:**  
FR-19 specifies that session titles are "auto-generated from the first user message, truncated to 60 characters." If two sessions start with identical first messages (e.g., "What's the weather in London?"), both sessions will have identical titles. There is no uniqueness constraint, disambiguation strategy, or collision handling. The `GET /sessions` endpoint returns both with the same title, `created_at`, and potentially the same `message_count`, making them indistinguishable to the user.

**Risk:**  
Users cannot distinguish between sessions with identical titles, leading to accidental resumption of the wrong session or deletion of the wrong conversation.

**Recommendation:**  
Update FR-19: session titles do not need to be globally unique, but the system must append a counter suffix when a user already has a session with the same title (e.g., "What's the weather in London? (2)"). Alternatively, require that session titles are unique per user by appending the session creation date. Add a uniqueness constraint or deduplication logic to the title generation code.

---

### REVIEW-32

**ID:** REVIEW-32  
**Severity:** MEDIUM  
**Affected requirement(s):** FR-15, FR-16, NFR-14  
**Category:** Missing Requirement

**Finding:**  
The spec mentions JWTs as the authentication mechanism and requires CORS restriction (NFR-14), but does not specify how JWTs are delivered to the client or stored. If JWTs are stored in `localStorage` (common default), they are vulnerable to XSS. If JWTs are stored in `HttpOnly` cookies, CSRF protection is required. The spec is silent on storage mechanism, cookie attributes (`HttpOnly`, `Secure`, `SameSite`), and CSRF defence. NFR-14 addresses CORS but CORS is not a CSRF defence.

**Risk:**  
If tokens are stored in `localStorage`, any XSS vulnerability exfiltrates the JWT and gives the attacker full API access. If tokens are stored in cookies without `SameSite=Strict` or a CSRF token, cross-site requests can abuse the cookie.

**Recommendation:**  
Add a requirement specifying JWT delivery and storage: (a) access tokens must be returned in the response body (for `Authorization: Bearer` header use in API calls); (b) if a session cookie is issued for browser convenience, it must use `HttpOnly; Secure; SameSite=Strict` attributes; (c) if `SameSite=Lax` is used, CSRF token validation must be added to all state-mutating endpoints. Document the chosen strategy in `DESIGN.md`.

---

### REVIEW-33

**ID:** REVIEW-33  
**Severity:** MEDIUM  
**Affected requirement(s):** NFR-12  
**Category:** Vague/Untestable

**Finding:**  
NFR-12 requires CI to "scan for hardcoded secrets using a tool such as `detect-secrets` or `gitleaks`" but does not specify: (a) which tool is mandatory; (b) whether a baseline file is maintained (so existing false positives don't block CI permanently); (c) what constitutes a "secret pattern" (the tool's default rules, custom rules, or a named ruleset); (d) the process for handling false positives or legitimate base64-encoded non-secret strings; (e) whether the scan covers all branches or only `main`.

**Risk:**  
Without a specific tool, baseline, and ruleset, different developers implement the scan differently. A scan without a baseline file will either block on false positives from the first run or will be disabled ("just add `--no-verify`") immediately. A scan that only runs on `main` misses secrets committed on feature branches before merge.

**Recommendation:**  
Specify `detect-secrets` (or `gitleaks`) as the mandatory tool, with a committed baseline file (`.secrets.baseline` or `.gitleaks.toml`). The CI step must run on every pull request, not only on `main`. The baseline must be reviewed and updated as part of code review. Document the false-positive exception process.

---

### REVIEW-34

**ID:** REVIEW-34  
**Severity:** LOW  
**Affected requirement(s):** FR-24, FR-26, FR-29  
**Category:** Conflicting Requirements

**Finding:**  
FR-29 specifies: "retry the same model up to 2 times with exponential backoff (2 s, 4 s) before triggering the fallback model chain (FR-24)." FR-24's acceptance criterion states: "The system retries twice (FR-26), then invokes the first fallback model." FR-26 is the model-switching endpoint, not a retry mechanism — the acceptance criterion appears to incorrectly cite FR-26 as the source of the retry count. This creates ambiguity about whether the retry count (2 retries) is defined in FR-24, FR-26, or FR-29.

**Risk:**  
Developers may implement different retry counts depending on which FR they read first. One implementation retries once (FR-24 citation of FR-26 is misread), another retries twice (FR-29).

**Recommendation:**  
Consolidate the retry count into a single canonical requirement (FR-29) and update FR-24's acceptance criterion to reference FR-29 explicitly. Remove the "(FR-26)" citation from FR-24's acceptance criterion.

---

### REVIEW-35

**ID:** REVIEW-35  
**Severity:** LOW  
**Affected requirement(s):** FR-27  
**Category:** Hidden Assumption

**Finding:**  
FR-27 enforces per-window token quotas using "sliding-window counters stored in Redis." The token count is presumably obtained from the OpenAI API response (`usage.total_tokens`). The spec does not acknowledge that token counts from the OpenAI API are exact only after the call completes. For streaming responses (which this system uses, per FR-12/FR-13), the token count is often estimated or unavailable until the stream closes. There is no specification of when and how tokens are counted (before the call, after the call, or from the stream's final chunk), nor what tolerance is acceptable.

**Risk:**  
If tokens are counted before the call (using a local tokeniser estimate), over-counting or under-counting of 5–15% is common, allowing quota drift. If counting is deferred until after the stream closes, a request that starts under quota could complete over quota. Neither case is handled by the spec.

**Recommendation:**  
Add a requirement specifying the token accounting method: tokens must be recorded from the `usage` field in the OpenAI streaming completion's final chunk (or from the non-streaming response). If the `usage` field is absent (some models), a local tokeniser (`tiktoken`) estimate must be used as a fallback, and this must be logged at `DEBUG` level as an approximation. Define an acceptable tolerance (e.g., 5% over-count is acceptable; quota enforcement must never allow more than `limit + 5%` in a window).

---

### REVIEW-36

**ID:** REVIEW-36  
**Severity:** LOW  
**Affected requirement(s):** FR-5  
**Category:** Missing Requirement

**Finding:**  
FR-5's acceptance criterion states: "a mock tool appears in the registered tool list returned by `GET /models` or an equivalent registry endpoint." This endpoint is referenced as the tool registry query surface but is never formally specified anywhere in the spec. There is no FR defining `GET /models`: its path, response schema, authentication requirements, or expected behaviour for empty registries.

**Risk:**  
The endpoint will be implemented ad-hoc with inconsistent response schemas. It may accidentally expose sensitive tool configuration (e.g., API key environment variable names). It may not be authenticated, allowing unauthenticated users to enumerate the system's capabilities.

**Recommendation:**  
Add a formal requirement (FR-33) specifying `GET /models` (or rename to `GET /tools` for clarity): path, authentication requirement (accessible to authenticated and guest users), response schema (array of `{"name": str, "description": str, "is_sensitive": bool}`), and explicit exclusion of internal configuration fields (API keys, endpoint URLs).

---

### REVIEW-37

**ID:** REVIEW-37  
**Severity:** LOW  
**Affected requirement(s):** NFR-6, SC-1  
**Category:** Missing Requirement

**Finding:**  
The spec does not specify any load-shedding or backpressure mechanism. Under sustained load (even at the 5-concurrent-session scale assumed by A-1), if all 5 sessions simultaneously trigger web searches with HITL waits, 5 SSE connections are held open, 5 PostgreSQL checkpointer threads are active, and the Redis connection pool (max 10, per NFR-7) is partially exhausted. There is no requirement for: (a) a maximum concurrent SSE connection limit; (b) a request queue depth limit; (c) HTTP 503 responses when the system is at capacity.

**Risk:**  
Under load (or a simple scripted loop from a single user), the connection pool exhausts, database connections time out, and the application enters a cascading failure state with no controlled degradation path.

**Recommendation:**  
Add a requirement specifying maximum concurrent SSE connections (`MAX_CONCURRENT_STREAMS`, default 20, configurable via environment variable). When this limit is reached, new `GET /chat/stream` requests must return HTTP 503 with a `Retry-After` header. This is minimal load-shedding appropriate for the system's scale.

---

### REVIEW-38

**ID:** REVIEW-38  
**Severity:** LOW  
**Affected requirement(s):** NFR-10  
**Category:** Missing Requirement

**Finding:**  
NFR-10 mentions a `/readiness` endpoint (implied by the acceptance criterion: "before `/readiness` returns HTTP 200"). The endpoint is never formally specified. The spec does not define: (a) what checks `/readiness` must perform (database connectivity, Redis connectivity, Alembic revision check, external API key presence); (b) whether there is a separate `/liveness` endpoint; (c) the response schema; (d) whether these endpoints require authentication.

**Risk:**  
A shallow health check that only verifies the HTTP server is running will return HTTP 200 even when PostgreSQL or Redis is unreachable. Render's health checks will consider the instance healthy and route traffic to it, but every request will fail with database connection errors.

**Recommendation:**  
Add a formal requirement specifying `/readiness` and `/liveness` endpoints. `/liveness` must return HTTP 200 if the process is running (no external checks). `/readiness` must verify: (a) PostgreSQL connection acquirable from the pool; (b) Redis connection acquirable from the pool; (c) Alembic revision matches the expected head. If any check fails, `/readiness` must return HTTP 503 with a JSON body identifying the failing component. Both endpoints must be unauthenticated (Render's health checker does not send auth headers).

---

## Statistics

### Findings by Severity

| Severity | Count |
|---|---|
| CRITICAL | 6 |
| HIGH | 11 |
| MEDIUM | 15 |
| LOW | 6 |
| **Total** | **38** |

### Findings by Category

| Category | Count |
|---|---|
| Security Gap | 9 |
| Missing Requirement | 11 |
| Missing Edge Case | 8 |
| Conflicting Requirements | 4 |
| Hidden Assumption | 4 |
| Vague/Untestable | 3 |
| **Total** | **38** (some findings span multiple categories; primary category assigned) |

### Priority Action Summary

The six CRITICAL findings must be resolved before implementation begins. REVIEW-5 (FR-24/FR-25 timeline conflict) and REVIEW-3 (refresh token rotation) are architectural decisions that will affect multiple implementation files if not resolved in the spec first. REVIEW-1 (HITL approval replay window) requires an atomic database operation pattern that must be designed into the schema from day one. REVIEW-2 and REVIEW-6 (auth brute-force and password hashing) are table-stakes security requirements for any system with user authentication.

The eleven HIGH findings should be resolved before the first testable milestone. REVIEW-10 (SSE replay buffer vs. HITL timeout), REVIEW-13 (migration rollback), and REVIEW-17 (HITL audit log) require schema design decisions that cannot be deferred without increasing migration cost.
