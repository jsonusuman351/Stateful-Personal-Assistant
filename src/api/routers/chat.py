"""Chat endpoints: POST /chat and GET /chat/stream SSE (T-039, FR-12, FR-13)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from src.api.dependencies import TokenPayload, get_current_user
from src.config.settings import get_settings
from src.persistence.checkpointer import build_guest_thread_id, build_thread_id
from src.persistence.redis_client import get_redis
from src.quota.limiter import check_and_increment
from src.streaming.emitter import SSEEmitter
from src.streaming.replay import get_events_after, validate_stream_ownership

router = APIRouter(tags=["chat"])

_CONTEXT_TTL = 300  # seconds — pending chat context lives this long in Redis


# ── Request / Response models ─────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """POST /chat request body."""

    message: str
    session_id: str | None = None  # None → new conversation


class ChatResponse(BaseModel):
    """POST /chat 202 response body."""

    message_id: str
    session_id: str
    stream_url: str


# ── Internal: live SSE emitter ────────────────────────────────────────────────


class _LiveEmitter:
    """SSEEmitter wrapper that simultaneously queues events for HTTP streaming.

    Calling ``emit()`` stores events in Redis (for replay) and also enqueues
    the formatted SSE text for the ``stream()`` async generator.
    """

    def __init__(self, stream_id: str, redis_client: aioredis.Redis) -> None:
        self._emitter = SSEEmitter(stream_id, redis_client)
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Store in Redis and enqueue for live streaming."""
        await self._emitter.emit(event_type, payload)
        # Read back the text just appended (last element)
        raw = await self._emitter._redis.lindex(self._emitter._key, -1)  # type: ignore[attr-defined]
        text = raw if isinstance(raw, str) else (raw.decode() if raw else "")
        await self._queue.put(text)
        if event_type == "done":
            await self._queue.put(None)  # Sentinel

    async def stream(self) -> AsyncGenerator[bytes, None]:
        """Yield encoded SSE events as they arrive from the graph."""
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if item is None:
                return
            yield item.encode("utf-8")


# ── POST /chat ────────────────────────────────────────────────────────────────


@router.post("/chat", status_code=202, response_model=ChatResponse)
async def post_chat(
    body: ChatRequest,
    request: Request,
    current_user: TokenPayload = Depends(get_current_user),
) -> ChatResponse:
    """Accept a user message and return 202 with stream_url (FR-12, FR-26).

    Validates quota before creating the stream. Session_id=null creates a new
    conversation; an existing session_id continues it.
    """
    import hashlib

    settings = get_settings()
    redis = get_redis()

    owner_id = current_user.user_id or current_user.session_id or str(uuid.uuid4())

    # IP hash for guest quota (FR-31)
    forwarded = request.headers.get("X-Forwarded-For")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    ip_hash = hashlib.sha256(ip.encode()).hexdigest() if current_user.is_guest else None

    # Quota check (request count; token count is tracked by llm node)
    quota_status = await check_and_increment(
        owner_id, ip_hash, settings.OPENAI_DEFAULT_MODEL, 0, redis_client=redis
    )
    if not quota_status.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "QUOTA_EXCEEDED",
                "message": f"Quota exceeded ({quota_status.quota_type}).",
                "retryable": True,
                "retry_after_seconds": 3600,
            },
        )

    # Resolve session_id
    session_id = body.session_id
    if session_id is None:
        if current_user.is_guest:
            session_id = current_user.session_id or str(uuid.uuid4())
        else:
            session_id = str(uuid.uuid4())  # Auth user: new conversation

    # Build LangGraph thread_id
    if current_user.is_guest:
        ua = request.headers.get("User-Agent", "")
        thread_id = build_guest_thread_id(ip, ua)
    else:
        thread_id = build_thread_id(owner_id, session_id)

    message_id = str(uuid.uuid4())
    stream_id = str(uuid.uuid4())

    # Store pending context and stream ownership in Redis
    context: dict[str, Any] = {
        "message": body.message,
        "message_id": message_id,
        "session_id": session_id,
        "thread_id": thread_id,
        "user_id": current_user.user_id,
        "is_guest": current_user.is_guest,
        "active_model": settings.OPENAI_DEFAULT_MODEL,
    }
    async with redis.pipeline() as pipe:
        pipe.set(f"chat:{stream_id}:context", json.dumps(context), ex=_CONTEXT_TTL)
        pipe.set(f"stream:{stream_id}:owner", owner_id, ex=_CONTEXT_TTL)
        await pipe.execute()

    stream_url = f"/chat/stream?stream_id={stream_id}"
    return ChatResponse(message_id=message_id, session_id=session_id, stream_url=stream_url)


# ── GET /chat/stream ──────────────────────────────────────────────────────────


@router.get("/chat/stream")
async def stream_chat(
    stream_id: str,
    request: Request,
    current_user: TokenPayload = Depends(get_current_user),
) -> StreamingResponse:
    """Open an SSE connection and execute one graph turn (FR-13).

    Reconnect handling: ``Last-Event-ID: N`` header replays buffered events
    with id > N without re-running graph nodes (FR-14).
    """
    redis = get_redis()
    owner_id = current_user.user_id or current_user.session_id or ""

    # Ownership / expiry check
    owner = await redis.get(f"stream:{stream_id}:owner")
    if owner is None:
        raise HTTPException(status_code=410, detail="Stream has expired")
    if owner != owner_id:
        raise HTTPException(status_code=403, detail="Stream not owned by this user")

    # Reconnect path: replay buffered events
    last_event_id_str = request.headers.get("Last-Event-ID")
    if last_event_id_str:
        try:
            last_event_id = int(last_event_id_str)
        except ValueError:
            last_event_id = 0
        if last_event_id > 0:
            events = await get_events_after(stream_id, last_event_id, redis)
            if events is None:
                raise HTTPException(status_code=410, detail="Stream has expired")

            async def replay_gen() -> AsyncGenerator[bytes, None]:
                for ev in events:
                    yield ev.encode("utf-8") if isinstance(ev, str) else ev

            return StreamingResponse(
                replay_gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # Load pending chat context
    ctx_raw = await redis.get(f"chat:{stream_id}:context")
    if ctx_raw is None:
        raise HTTPException(status_code=410, detail="Stream context has expired")

    ctx: dict[str, Any] = json.loads(ctx_raw)
    emitter = _LiveEmitter(stream_id, redis)

    async def _run_graph() -> None:
        from langchain_core.runnables import RunnableConfig

        from src.agents.graph import get_graph
        from src.agents.runner import run_turn
        from src.persistence.checkpointer import get_checkpointer

        input_state: dict[str, Any] = {
            "messages": [HumanMessage(content=ctx["message"])],
            "session_id": ctx["session_id"],
            "user_id": ctx.get("user_id"),
            "thread_id": ctx["thread_id"],
            "stream_id": stream_id,
            "turn_index": 0,
            "tool_calls": [],
            "tool_results": [],
            "pending_approval": None,
            "hitl_decision": None,
            "active_model": ctx.get("active_model", get_settings().OPENAI_DEFAULT_MODEL),
            "error": None,
        }
        config: RunnableConfig = {
            "configurable": {"thread_id": ctx["thread_id"]},
            "run_id": ctx["message_id"],
        }
        try:
            await run_turn(
                get_graph(get_checkpointer()), input_state, config, emitter
            )
        except Exception as exc:
            await emitter.emit("error", {
                "code": "GRAPH_ERROR",
                "message": str(exc),
                "retryable": False,
            })
            await emitter.emit("done", {"message_id": ctx["message_id"]})

    async def _sse_generator() -> AsyncGenerator[bytes, None]:
        task = asyncio.create_task(_run_graph())
        try:
            async for chunk in emitter.stream():
                yield chunk
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
