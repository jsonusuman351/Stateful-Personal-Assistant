"""Integration test: HITL approval flow (T-049).

Requires:
    TEST_DATABASE_URL — async-compatible PostgreSQL URL
    Redis at TEST_REDIS_URL (default: redis://localhost:6379/15)

Both are provisioned automatically in CI via Docker service containers.
Tests skip gracefully when the prerequisites are unavailable.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# ── SSE parsing ───────────────────────────────────────────────────────────────


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Parse raw SSE text into a list of event dicts."""
    events: list[dict[str, Any]] = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                event["id"] = int(line[4:])
            elif line.startswith("event: "):
                event["event"] = line[7:]
            elif line.startswith("data: "):
                try:
                    event["data"] = json.loads(line[6:])
                except json.JSONDecodeError:
                    event["data"] = line[6:]
        if event:
            events.append(event)
    return events


# ── DB / auth helpers ─────────────────────────────────────────────────────────


async def _create_user_and_conversation(db_engine: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a committed test user + conversation; return (user_id, conv_id)."""
    from src.persistence.models.conversation import Conversation
    from src.persistence.models.user import User

    user_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            session.add(
                User(
                    id=user_id,
                    email=f"hitl-test-{user_id}@test.local",
                    password_hash="$argon2id$v=19$m=65536,t=2,p=2$test$placeholder",
                )
            )
            session.add(
                Conversation(
                    id=conv_id,
                    user_id=user_id,
                    title="HITL Test",
                    active_model="gpt-4o-mini",
                )
            )

    return user_id, conv_id


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """Return a Bearer Authorization header for the given test user."""
    from src.auth.jwt import create_access_token

    token = create_access_token(str(user_id), str(uuid.uuid4()))
    return {"Authorization": f"Bearer {token}"}


async def _seed_approval(
    db_engine: Any,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    thread_id: str,
) -> uuid.UUID:
    """Insert a committed HITL approval row; return its approval_id UUID."""
    from src.persistence.models.hitl import HITLApproval

    approval_id = uuid.uuid4()
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            session.add(
                HITLApproval(
                    id=approval_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tool_name="web_search",
                    thread_id=thread_id,
                    checkpoint_id="test-checkpoint-id",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            )
    return approval_id


# ── Mock factory ──────────────────────────────────────────────────────────────


def _make_hitl_mock() -> type:
    """Return a fresh ChatOpenAI test double: call 1 → web_search; calls 2+ → text."""
    from langchain_core.messages import AIMessage

    call_count: list[int] = [0]

    class _HITLMock:
        """Router selects web_search on first ainvoke(); LLM synthesises thereafter."""

        def __init__(self, **kwargs: Any) -> None:
            pass

        def bind_tools(self, tools: Any) -> "_HITLMock":
            return self

        async def ainvoke(self, messages: Any) -> AIMessage:
            call_count[0] += 1
            if call_count[0] == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "climate change latest news"},
                            "id": "call_hitl_0",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="Here are the latest climate change findings.")

    return _HITLMock


# ── Resume-wait helper ────────────────────────────────────────────────────────


async def _wait_for_resume(
    stream_id: str,
    initial_count: int,
    redis_client: Any,
    timeout: float = 10.0,
) -> None:
    """Poll Redis until the stream list has more events than initial_count with a done."""
    key = f"stream:{stream_id}:events"
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        total: int = await redis_client.llen(key)
        if total > initial_count:
            latest_raw = await redis_client.lindex(key, -1)
            latest = (
                latest_raw
                if isinstance(latest_raw, str)
                else (latest_raw.decode() if latest_raw else "")
            )
            if "event: done" in latest:
                return
        await asyncio.sleep(0.1)


# ── Autouse fixture ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def patch_db_session_factory(
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect get_db_session() to the test PostgreSQL engine.

    hitl_gate_node calls get_db_session() directly (not via FastAPI DI),
    so patching the module-level _session_factory singleton is the only
    reliable way to make those writes land in the test database.
    """
    import src.persistence.database as _db_module

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(_db_module, "_session_factory", factory)
    monkeypatch.setattr(_db_module, "_engine", db_engine)


# ── Shared flow helper ────────────────────────────────────────────────────────


async def _start_hitl_flow(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    db_engine: Any,
) -> tuple[dict[str, str], str, str, str, list[dict[str, Any]]]:
    """Trigger the first half of a HITL turn (router → hitl_gate → suspend).

    Returns (headers, conversation_id_str, stream_id, message_id, events).
    """
    user_id, conv_id = await _create_user_and_conversation(db_engine)
    headers = _auth_headers(user_id)

    hitl_mock = _make_hitl_mock()
    monkeypatch.setattr("src.graph.nodes.router.ChatOpenAI", hitl_mock)
    monkeypatch.setattr("src.graph.nodes.llm.ChatOpenAI", hitl_mock)

    post_resp = await client.post(
        "/chat",
        json={"message": "Search for climate change news", "session_id": str(conv_id)},
        headers=headers,
    )
    assert post_resp.status_code == 202, (
        f"POST /chat: {post_resp.status_code} — {post_resp.text}"
    )
    post_body: dict[str, Any] = post_resp.json()
    stream_url: str = post_body["stream_url"]
    message_id: str = post_body["message_id"]
    stream_id: str = stream_url.split("stream_id=")[1]

    stream_resp = await client.get(stream_url, headers=headers, timeout=30.0)
    assert stream_resp.status_code == 200, (
        f"GET /chat/stream: {stream_resp.status_code}"
    )
    assert "text/event-stream" in stream_resp.headers.get("content-type", "")

    events = _parse_sse(stream_resp.text)
    return headers, str(conv_id), stream_id, message_id, events


# ── T-049 tests ───────────────────────────────────────────────────────────────


async def test_checkpoint_committed_before_sse(
    async_client: Any, db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approval row is committed to DB before approval_required SSE is emitted (FR-9)."""
    headers, conv_id, stream_id, message_id, events = await _start_hitl_flow(
        async_client, monkeypatch, db_engine
    )

    approval_events = [e for e in events if e["event"] == "approval_required"]
    assert approval_events, (
        f"No approval_required event. Events: {[e['event'] for e in events]}"
    )

    payload: dict[str, Any] = approval_events[0]["data"]
    assert "approval_id" in payload, "approval_required payload missing approval_id"
    assert payload.get("tool_name") or payload.get("tool"), "Missing tool field"
    assert payload.get("description"), "Missing description field"

    approval_uuid = uuid.UUID(payload["approval_id"])
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        from src.persistence.repositories.hitl_repo import HITLRepository

        repo = HITLRepository(session)
        approval = await repo.get_approval(approval_uuid)

    assert approval is not None, "Approval row not found in DB after SSE stream closed"
    assert not approval.used, "Approval unexpectedly marked as used"
    assert not approval.expired, "Approval unexpectedly marked as expired"


async def test_approve_resumes_graph(
    async_client: Any,
    db_engine: Any,
    redis_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /approve with 'approve' resumes the graph; reconnect yields done (NFR-4)."""
    headers, conv_id, stream_id, message_id, events = await _start_hitl_flow(
        async_client, monkeypatch, db_engine
    )

    approval_events = [e for e in events if e["event"] == "approval_required"]
    assert approval_events, "No approval_required event — HITL flow did not trigger"
    approval_id: str = approval_events[0]["data"]["approval_id"]
    initial_count = len(events)

    resp = await async_client.post(
        f"/sessions/{conv_id}/approve",
        json={"approval_id": approval_id, "decision": "approve"},
        headers=headers,
    )
    assert resp.status_code == 200, f"Approve returned {resp.status_code}: {resp.text}"
    assert resp.json()["status"] == "ok"

    await _wait_for_resume(stream_id, initial_count, redis_client, timeout=10.0)

    reconnect_resp = await async_client.get(
        f"/chat/stream?stream_id={stream_id}",
        headers={**headers, "Last-Event-ID": str(initial_count)},
        timeout=10.0,
    )
    assert reconnect_resp.status_code == 200
    resumed_events = _parse_sse(reconnect_resp.text)
    event_types = [e["event"] for e in resumed_events]
    assert "done" in event_types, (
        f"No done event in resumed stream. Events: {event_types}"
    )


async def test_deny_routes_to_error_handler(
    async_client: Any,
    db_engine: Any,
    redis_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /approve with 'deny' routes through error_handler; graph completes cleanly."""
    headers, conv_id, stream_id, message_id, events = await _start_hitl_flow(
        async_client, monkeypatch, db_engine
    )

    approval_events = [e for e in events if e["event"] == "approval_required"]
    assert approval_events, "No approval_required event — HITL flow did not trigger"
    approval_id: str = approval_events[0]["data"]["approval_id"]
    initial_count = len(events)

    resp = await async_client.post(
        f"/sessions/{conv_id}/approve",
        json={"approval_id": approval_id, "decision": "deny"},
        headers=headers,
    )
    assert resp.status_code == 200, f"Deny returned {resp.status_code}: {resp.text}"

    await _wait_for_resume(stream_id, initial_count, redis_client, timeout=10.0)

    reconnect_resp = await async_client.get(
        f"/chat/stream?stream_id={stream_id}",
        headers={**headers, "Last-Event-ID": str(initial_count)},
        timeout=10.0,
    )
    assert reconnect_resp.status_code == 200
    resumed_events = _parse_sse(reconnect_resp.text)
    event_types = [e["event"] for e in resumed_events]
    assert "done" in event_types, (
        f"Graph did not complete after deny. Events: {event_types}"
    )


async def test_concurrent_approve_409(
    async_client: Any,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent POST /approve with same approval_id → one 200, one 409 (FR-10)."""
    user_id, conv_id = await _create_user_and_conversation(db_engine)
    headers = _auth_headers(user_id)
    thread_id = f"auth|{user_id}|{conv_id}"
    approval_id = await _seed_approval(db_engine, conv_id, user_id, thread_id)

    resp1, resp2 = await asyncio.gather(
        async_client.post(
            f"/sessions/{conv_id}/approve",
            json={"approval_id": str(approval_id), "decision": "approve"},
            headers=headers,
        ),
        async_client.post(
            f"/sessions/{conv_id}/approve",
            json={"approval_id": str(approval_id), "decision": "approve"},
            headers=headers,
        ),
    )

    statuses = sorted([resp1.status_code, resp2.status_code])
    assert statuses == [200, 409], (
        f"Expected [200, 409], got {statuses}. "
        f"Bodies: {resp1.text!r}, {resp2.text!r}"
    )


async def test_replayed_approval_id_410(
    async_client: Any,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-using a consumed approval_id returns HTTP 410 (FR-10)."""
    user_id, conv_id = await _create_user_and_conversation(db_engine)
    headers = _auth_headers(user_id)
    thread_id = f"auth|{user_id}|{conv_id}"
    approval_id = await _seed_approval(db_engine, conv_id, user_id, thread_id)

    resp1 = await async_client.post(
        f"/sessions/{conv_id}/approve",
        json={"approval_id": str(approval_id), "decision": "approve"},
        headers=headers,
    )
    assert resp1.status_code == 200, f"First approve failed: {resp1.text}"

    resp2 = await async_client.post(
        f"/sessions/{conv_id}/approve",
        json={"approval_id": str(approval_id), "decision": "approve"},
        headers=headers,
    )
    assert resp2.status_code == 410, (
        f"Expected 410, got {resp2.status_code}: {resp2.text}"
    )


async def test_audit_log_inserted(
    async_client: Any,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An audit log row is present after approval with correct fields (FR-32)."""
    from sqlalchemy import select

    from src.persistence.models.hitl import HITLAuditLog

    user_id, conv_id = await _create_user_and_conversation(db_engine)
    headers = _auth_headers(user_id)
    thread_id = f"auth|{user_id}|{conv_id}"
    approval_id = await _seed_approval(db_engine, conv_id, user_id, thread_id)

    resp = await async_client.post(
        f"/sessions/{conv_id}/approve",
        json={"approval_id": str(approval_id), "decision": "approve"},
        headers=headers,
    )
    assert resp.status_code == 200

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await session.execute(
            select(HITLAuditLog).where(HITLAuditLog.approval_id == approval_id)
        )
        log_entries = list(result.scalars())

    assert len(log_entries) == 1, (
        f"Expected 1 audit log entry, found {len(log_entries)}"
    )
    entry = log_entries[0]
    assert entry.decision == "approve"
    assert entry.conversation_id == conv_id
    assert entry.tool_name == "web_search"


async def test_replay_buffer_alive_at_8min(
    async_client: Any,
    db_engine: Any,
    redis_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HITL stream TTL is 900s; buffer survives past minute 8 (480s threshold) (FR-14)."""
    _headers, _conv_id, stream_id, _message_id, events = await _start_hitl_flow(
        async_client, monkeypatch, db_engine
    )

    approval_events = [e for e in events if e["event"] == "approval_required"]
    assert approval_events, "No approval_required event — HITL TTL was not applied"

    key = f"stream:{stream_id}:events"
    ttl: int = await redis_client.ttl(key)

    assert ttl > 0, f"Stream key {key!r} expired or missing (TTL={ttl})"
    assert ttl > 480, (
        f"HITL stream TTL {ttl}s too low — buffer expires before minute 8 (need > 480s)"
    )
    assert ttl <= 900, f"Stream TTL {ttl}s exceeds 900s maximum"
