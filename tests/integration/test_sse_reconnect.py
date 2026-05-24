"""Integration test: SSE reconnection via Last-Event-ID (T-050, FR-14, NFR-5).

Requires:
    TEST_DATABASE_URL — async-compatible PostgreSQL URL
    Redis at TEST_REDIS_URL (default: redis://localhost:6379/15)

Both are provisioned automatically in CI via Docker service containers.
Tests skip gracefully when prerequisites are unavailable.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest


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


# ── Shared helpers ────────────────────────────────────────────────────────────


async def _guest_auth_headers(client: Any) -> dict[str, str]:
    """Issue a guest JWT and return Authorization header dict."""
    resp = await client.post("/auth/guest")
    assert resp.status_code == 200, f"Guest auth failed: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _stream_id_from_url(stream_url: str) -> str:
    """Extract the stream_id UUID from /chat/stream?stream_id=<UUID>."""
    return stream_url.split("stream_id=")[1]


async def _run_full_turn(
    client: Any,
    message: str,
    headers: dict[str, str],
    timeout: float = 30.0,
) -> tuple[str, str, list[dict[str, Any]]]:
    """POST /chat → GET /chat/stream. Returns (stream_id, stream_url, events).

    Asserts HTTP 202 on POST and HTTP 200 text/event-stream on GET.
    """
    post_resp = await client.post(
        "/chat", json={"message": message}, headers=headers
    )
    assert post_resp.status_code == 202, (
        f"POST /chat returned {post_resp.status_code}: {post_resp.text}"
    )
    post_body: dict[str, Any] = post_resp.json()
    stream_url: str = post_body["stream_url"]
    stream_id = _stream_id_from_url(stream_url)

    stream_resp = await client.get(stream_url, headers=headers, timeout=timeout)
    assert stream_resp.status_code == 200, (
        f"GET {stream_url} returned {stream_resp.status_code}: {stream_resp.text}"
    )
    return stream_id, stream_url, _parse_sse(stream_resp.text)


# ── T-050 tests ───────────────────────────────────────────────────────────────


async def test_reconnect_replays_missed_events(
    async_client: Any,
    mock_openai: Any,
) -> None:
    """Reconnect with Last-Event-ID: 2 replays events 3+ in correct order (FR-14).

    Simulates a client that received events 1-2 before disconnecting, then
    reconnects.  Verifies the replay has only events with id > 2 and that IDs
    are strictly increasing.
    """
    mock_openai.set_response("Reconnect replay test response.")
    headers = await _guest_auth_headers(async_client)

    _stream_id, stream_url, all_events = await _run_full_turn(
        async_client, "Reconnect replay test", headers
    )
    assert len(all_events) >= 3, (
        f"Need ≥3 events to split 'seen' from 'missed'. Got: {all_events}"
    )

    # Simulate: client received the first 2 events, then disconnected.
    split_at = 2
    expected_remaining = [e for e in all_events if e["id"] > split_at]

    reconnect_resp = await async_client.get(
        stream_url,
        headers={**headers, "Last-Event-ID": str(split_at)},
        timeout=10.0,
    )
    assert reconnect_resp.status_code == 200, (
        f"Reconnect returned {reconnect_resp.status_code}: {reconnect_resp.text}"
    )
    assert "text/event-stream" in reconnect_resp.headers.get("content-type", ""), (
        "Reconnect response content-type is not text/event-stream"
    )

    replayed = _parse_sse(reconnect_resp.text)
    assert len(replayed) == len(expected_remaining), (
        f"Expected {len(expected_remaining)} replayed events, got {len(replayed)}. "
        f"Replayed: {replayed}"
    )

    # All replayed events must have id > split_at
    for ev in replayed:
        assert ev["id"] > split_at, (
            f"Replayed event id {ev['id']} ≤ Last-Event-ID {split_at}"
        )

    # IDs must be strictly increasing
    ids = [e["id"] for e in replayed]
    for i, (a, b) in enumerate(zip(ids, ids[1:])):
        assert b > a, f"Replayed IDs not strictly increasing at index {i}: {ids}"


async def test_reconnect_within_2s(
    async_client: Any,
    mock_openai: Any,
) -> None:
    """First replayed event arrives within 2 seconds of reconnecting (NFR-5).

    Events are already buffered in Redis after the turn completes, so replay
    is bounded only by Redis and HTTP latency — well under 2 seconds.
    """
    mock_openai.set_response("Timing test response.")
    headers = await _guest_auth_headers(async_client)

    _stream_id, stream_url, all_events = await _run_full_turn(
        async_client, "Timing reconnect test", headers
    )
    assert len(all_events) >= 2, (
        f"Need ≥2 events for reconnect timing test. Got: {all_events}"
    )

    # Reconnect with Last-Event-ID: 1 → replay everything from event 2 onward
    t0 = time.monotonic()
    reconnect_resp = await async_client.get(
        stream_url,
        headers={**headers, "Last-Event-ID": "1"},
        timeout=10.0,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert reconnect_resp.status_code == 200, (
        f"Reconnect returned {reconnect_resp.status_code}: {reconnect_resp.text}"
    )
    replayed = _parse_sse(reconnect_resp.text)
    assert replayed, "No events were replayed on reconnect"
    assert elapsed_ms < 2000, (
        f"Reconnect took {elapsed_ms:.0f}ms — exceeds 2000ms NFR-5 budget"
    )


async def test_cross_session_reconnect_403(
    async_client: Any,
    mock_openai: Any,
) -> None:
    """A different guest session is rejected with HTTP 403 when replaying another session's stream (FR-14).

    Guest A completes a turn. Guest B (different session_id from a second
    /auth/guest call) attempts to reconnect to Guest A's stream_url with a
    Last-Event-ID header and must receive HTTP 403.
    """
    mock_openai.set_response("Session A turn.")
    headers_a = await _guest_auth_headers(async_client)

    # Guest A: complete a full turn
    _stream_id, stream_url_a, _events = await _run_full_turn(
        async_client, "Session A message", headers_a
    )

    # Guest B: a fresh guest call issues a new session_id (different from A)
    headers_b = await _guest_auth_headers(async_client)

    # Guest B attempts to reconnect to Guest A's stream
    reconnect_resp = await async_client.get(
        stream_url_a,
        headers={**headers_b, "Last-Event-ID": "1"},
        timeout=10.0,
    )
    assert reconnect_resp.status_code == 403, (
        f"Expected HTTP 403 for cross-session reconnect, "
        f"got {reconnect_resp.status_code}: {reconnect_resp.text}"
    )


async def test_expired_stream_reconnect_410(
    async_client: Any,
    redis_client: Any,
    mock_openai: Any,
) -> None:
    """After stream TTL expires, any reconnect with Last-Event-ID returns HTTP 410.

    Simulates stream expiry by deleting the Redis ownership and events keys
    (equivalent to the 5-minute TTL elapsed for a non-HITL stream).
    """
    mock_openai.set_response("Expiry simulation response.")
    headers = await _guest_auth_headers(async_client)

    stream_id, stream_url, _events = await _run_full_turn(
        async_client, "Expiry test message", headers
    )

    # Simulate TTL expiry: delete both the ownership and events keys
    await redis_client.delete(f"stream:{stream_id}:owner")
    await redis_client.delete(f"stream:{stream_id}:events")

    # Reconnect must return 410 (stream expired)
    reconnect_resp = await async_client.get(
        stream_url,
        headers={**headers, "Last-Event-ID": "1"},
        timeout=10.0,
    )
    assert reconnect_resp.status_code == 410, (
        f"Expected HTTP 410 for expired stream, "
        f"got {reconnect_resp.status_code}: {reconnect_resp.text}"
    )


async def test_graph_not_reexecuted_on_reconnect(
    async_client: Any,
    mock_openai: Any,
) -> None:
    """Graph nodes are NOT re-executed on reconnect; only buffered events are replayed (FR-14).

    Checks that mock_openai.call_count (total LLM invocations across router
    and llm nodes) does not increase after a reconnect with Last-Event-ID.
    """
    mock_openai.set_response("Idempotency test response.")
    headers = await _guest_auth_headers(async_client)

    _stream_id, stream_url, all_events = await _run_full_turn(
        async_client, "No re-execution test", headers
    )
    assert len(all_events) >= 2, (
        f"Need ≥2 events for reconnect test. Got: {all_events}"
    )

    # Record LLM call count after the first (and only) graph execution
    call_count_after_turn = mock_openai.call_count
    assert call_count_after_turn > 0, "LLM was never called during the graph turn"

    # Reconnect: must replay buffered events without invoking the graph
    reconnect_resp = await async_client.get(
        stream_url,
        headers={**headers, "Last-Event-ID": "1"},
        timeout=10.0,
    )
    assert reconnect_resp.status_code == 200, (
        f"Reconnect returned {reconnect_resp.status_code}: {reconnect_resp.text}"
    )
    replayed = _parse_sse(reconnect_resp.text)
    assert replayed, "No events replayed — reconnect response was empty"

    # LLM call count must be unchanged after reconnect
    assert mock_openai.call_count == call_count_after_turn, (
        f"LLM was re-invoked on reconnect: call_count changed from "
        f"{call_count_after_turn} to {mock_openai.call_count}"
    )
