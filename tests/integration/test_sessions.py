"""Integration test: session CRUD and data isolation (T-051).

Requires:
    TEST_DATABASE_URL — async-compatible PostgreSQL URL
    Redis at TEST_REDIS_URL (default: redis://localhost:6379/15)

Both are provisioned automatically in CI via Docker service containers.
Tests skip gracefully when prerequisites are unavailable.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession

# ── SSE parsing ───────────────────────────────────────────────────────────────


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Parse raw SSE text into a list of event dicts.

    Each event block (separated by double newline) becomes one dict with
    keys ``id``, ``event``, and ``data``.  Keep-alive comments are skipped.
    """
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


async def _create_user(db_engine: Any, email: str | None = None) -> tuple[uuid.UUID, str]:
    """Insert a committed test user; return (user_id, email)."""
    from src.persistence.models.user import User

    user_id = uuid.uuid4()
    email = email or f"sessions-test-{user_id}@test.local"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            session.add(
                User(
                    id=user_id,
                    email=email,
                    password_hash="$argon2id$v=19$m=65536,t=2,p=2$test$placeholder",
                )
            )

    return user_id, email


async def _create_conversation(
    db_engine: Any,
    user_id: uuid.UUID,
    title: str = "Test Conversation",
) -> uuid.UUID:
    """Insert a committed conversation; return conv_id."""
    from src.persistence.models.conversation import Conversation

    conv_id = uuid.uuid4()

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            session.add(
                Conversation(
                    id=conv_id,
                    user_id=user_id,
                    title=title,
                    active_model="gpt-4o-mini",
                )
            )

    return conv_id


async def _insert_messages(
    db_engine: Any,
    conv_id: uuid.UUID,
    user_id: uuid.UUID,
    rows: list[tuple[str, str, str | None]],
) -> None:
    """Insert messages with sequential created_at timestamps (1 second apart).

    Args:
        db_engine: Async SQLAlchemy engine.
        conv_id: Conversation the messages belong to.
        user_id: Owner of the conversation.
        rows: List of (role, content, tool_name) tuples in desired order.
    """
    from src.persistence.models.message import Message

    base_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            for i, (role, content, tool_name) in enumerate(rows):
                session.add(
                    Message(
                        conversation_id=conv_id,
                        user_id=user_id,
                        role=role,
                        content=content,
                        tool_name=tool_name,
                        turn_index=i,
                        created_at=base_time + timedelta(seconds=i),
                    )
                )


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """Mint an access JWT for user_id; return Authorization header dict."""
    from src.auth.jwt import create_access_token

    token = create_access_token(str(user_id), str(uuid.uuid4()))
    return {"Authorization": f"Bearer {token}"}


async def _full_turn(
    client: Any,
    message: str,
    headers: dict[str, str],
    session_id: str | None = None,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """POST /chat → GET /chat/stream; return (post_body, parsed_events).

    Asserts HTTP 202 on POST and 200 on the stream endpoint.
    """
    body: dict[str, Any] = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id

    post_resp = await client.post("/chat", json=body, headers=headers)
    assert post_resp.status_code == 202, (
        f"POST /chat returned {post_resp.status_code}: {post_resp.text}"
    )
    post_body: dict[str, Any] = post_resp.json()

    stream_resp = await client.get(post_body["stream_url"], headers=headers, timeout=timeout)
    assert stream_resp.status_code == 200, (
        f"GET stream returned {stream_resp.status_code}: {stream_resp.text}"
    )

    return post_body, _parse_sse(stream_resp.text)


# ── T-051 tests ───────────────────────────────────────────────────────────────


async def test_session_list_scoped_to_user(
    async_client: Any,
    db_engine: Any,
) -> None:
    """GET /sessions returns only the requesting user's sessions (FR-19).

    Creates one conversation for User A and one for User B. Asserts that
    User A's session list contains A's conversation and excludes B's.
    """
    user_a_id, _ = await _create_user(db_engine)
    user_b_id, _ = await _create_user(db_engine)

    conv_a_id = await _create_conversation(db_engine, user_a_id, "User A session")
    conv_b_id = await _create_conversation(db_engine, user_b_id, "User B session")

    headers_a = _auth_headers(user_a_id)
    resp = await async_client.get("/sessions", headers=headers_a)
    assert resp.status_code == 200, f"GET /sessions: {resp.text}"

    sessions = resp.json()["sessions"]
    session_ids = {s["id"] for s in sessions}

    assert str(conv_a_id) in session_ids, "User A's own conversation not returned"
    assert str(conv_b_id) not in session_ids, (
        "User B's conversation leaked into User A's session list"
    )


async def test_cross_user_access_403(
    async_client: Any,
    db_engine: Any,
) -> None:
    """Cross-user access to sessions and messages returns HTTP 403 (FR-17, FR-20).

    User A must not read User B's messages or delete User B's session.
    An invalid UUID format also returns 403 — not 422 — to avoid leaking
    whether the session exists (FR-20).
    """
    user_a_id, _ = await _create_user(db_engine)
    user_b_id, _ = await _create_user(db_engine)

    conv_b_id = await _create_conversation(db_engine, user_b_id, "B's private session")
    await _insert_messages(db_engine, conv_b_id, user_b_id, [("user", "secret content", None)])

    headers_a = _auth_headers(user_a_id)

    # GET /sessions/{B's conv}/messages with A's token → 403
    resp_msgs = await async_client.get(f"/sessions/{conv_b_id}/messages", headers=headers_a)
    assert resp_msgs.status_code == 403, (
        f"Expected 403 for cross-user messages read, got {resp_msgs.status_code}"
    )

    # DELETE /sessions/{B's conv} with A's token → 403
    resp_del = await async_client.delete(f"/sessions/{conv_b_id}", headers=headers_a)
    assert resp_del.status_code == 403, (
        f"Expected 403 for cross-user delete, got {resp_del.status_code}"
    )

    # Invalid UUID format → 403, not 422 (FR-20 information leakage prevention)
    resp_bad = await async_client.get("/sessions/not-a-valid-uuid/messages", headers=headers_a)
    assert resp_bad.status_code == 403, (
        f"Expected 403 for invalid UUID format, got {resp_bad.status_code}"
    )


async def test_message_history_ordered(
    async_client: Any,
    db_engine: Any,
) -> None:
    """Messages are returned ordered by created_at ASC with correct roles (FR-20).

    Inserts a user → tool → assistant sequence with explicit timestamps.
    Verifies chronological order, role sequence, and that tool-invocation turns
    carry a non-null tool_name field.
    """
    user_id, _ = await _create_user(db_engine)
    conv_id = await _create_conversation(db_engine, user_id, "Weather query")

    await _insert_messages(
        db_engine,
        conv_id,
        user_id,
        [
            ("user", "What is the weather in Tokyo?", None),
            ("tool", '{"temperature": "18°C", "condition": "Cloudy"}', "weather"),
            ("assistant", "The weather in Tokyo is 18°C and cloudy.", None),
        ],
    )

    headers = _auth_headers(user_id)
    resp = await async_client.get(f"/sessions/{conv_id}/messages", headers=headers)
    assert resp.status_code == 200, f"GET messages: {resp.text}"

    messages = resp.json()["messages"]
    assert len(messages) == 3, f"Expected 3 messages, got {len(messages)}: {messages}"

    # Verify chronological role sequence (user → tool → assistant)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "tool", "assistant"], (
        f"Messages not in expected role sequence: {roles}"
    )

    # Tool message must carry a non-null tool_name (FR-20)
    assert messages[1]["tool_name"] == "weather", (
        f"Expected tool_name='weather', got '{messages[1]['tool_name']}'"
    )

    # Non-tool messages have null tool_name
    assert messages[0]["tool_name"] is None, "User message should have null tool_name"
    assert messages[2]["tool_name"] is None, "Assistant message should have null tool_name"


async def test_conversation_resumption_after_restart(
    async_client: Any,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LangGraph checkpointer re-hydrates context across a simulated process restart (FR-18, SM-9).

    Verifies that clearing the compiled graph cache (simulating a restart) does not
    lose conversation history. The MemorySaver checkpointer preserves in-memory state,
    so the new graph instance sees all prior messages when processing turn 3.
    """
    # Capture every message list passed to ainvoke() to verify context propagation
    received_messages: list[list[Any]] = []

    class _CapturingMock:
        """Test double for ChatOpenAI that records all ainvoke inputs."""

        def __init__(self, **kwargs: Any) -> None:
            pass

        def bind_tools(self, tools: Any) -> _CapturingMock:
            """Passthrough so router's llm.bind_tools(schemas).ainvoke() works."""
            return self

        async def ainvoke(self, messages: Any) -> AIMessage:
            """Record incoming messages and return a fixed plain-text response."""
            received_messages.append(list(messages))
            return AIMessage(content="I remember our conversation.")

    # Override both nodes — router uses bind_tools().ainvoke(); llm uses ainvoke() directly
    monkeypatch.setattr("src.graph.nodes.llm.ChatOpenAI", _CapturingMock)
    monkeypatch.setattr("src.graph.nodes.router.ChatOpenAI", _CapturingMock)

    user_id, _ = await _create_user(db_engine)
    headers = _auth_headers(user_id)

    # Turn 1: establish named entities in the checkpointed state
    post_1, events_1 = await _full_turn(
        async_client, "My name is Alice and I live in Berlin.", headers
    )
    assert "done" in [e["event"] for e in events_1], "Turn 1 did not complete"
    session_id: str = post_1["session_id"]

    # Turn 2: continue on the same session_id → same LangGraph thread_id
    _post_2, events_2 = await _full_turn(
        async_client, "Tell me about the city.", headers, session_id=session_id
    )
    assert "done" in [e["event"] for e in events_2], "Turn 2 did not complete"

    calls_before_restart = len(received_messages)

    # Simulate process restart: clear the compiled graph singleton.
    # cp_module._checkpointer (MemorySaver) is unchanged — its in-memory checkpoint
    # for this thread_id is preserved through the cache clear.
    from src.agents.graph import get_graph

    get_graph.cache_clear()

    # Turn 3: the new graph instance must reload checkpoint and include prior context
    _post_3, events_3 = await _full_turn(
        async_client,
        "What is my name and where do I live?",
        headers,
        session_id=session_id,
    )
    assert "done" in [e["event"] for e in events_3], (
        "Turn 3 did not complete after simulated restart"
    )

    # LLM must have been invoked at least once during turn 3
    assert len(received_messages) > calls_before_restart, (
        "LLM was not called in turn 3 — graph failed to resume after cache clear"
    )

    # The last ainvoke call (llm node, turn 3) must include the full history.
    # With add_messages reducer, turns 1 and 2 are accumulated in the checkpoint.
    last_call = received_messages[-1]
    all_content = " ".join(
        getattr(m, "content", str(m)) for m in last_call if hasattr(m, "content")
    )
    assert "Berlin" in all_content or "Alice" in all_content, (
        f"Turn 3 LLM context does not include prior history. Content seen: {all_content!r}"
    )


async def test_guest_sessions_forbidden(
    async_client: Any,
) -> None:
    """Guest token on GET /sessions returns HTTP 403 (FR-19).

    Guests have no persistent session history; the endpoint is auth-only.
    """
    guest_resp = await async_client.post("/auth/guest")
    assert guest_resp.status_code == 200, f"Guest token request failed: {guest_resp.text}"
    guest_token = guest_resp.json()["access_token"]

    resp = await async_client.get(
        "/sessions",
        headers={"Authorization": f"Bearer {guest_token}"},
    )
    assert resp.status_code == 403, (
        f"Expected 403 for guest on GET /sessions, got {resp.status_code}: {resp.text}"
    )
