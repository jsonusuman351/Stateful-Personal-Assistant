"""Sessions CRUD and HITL approval endpoints (T-040, FR-10, FR-17, FR-19, FR-20, FR-25)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import TokenPayload, get_db, require_auth_user
from src.persistence.repositories.conversation_repo import ConversationRepository
from src.persistence.repositories.hitl_repo import HITLRepository
from src.persistence.repositories.message_repo import MessageRepository

router = APIRouter(prefix="/sessions", tags=["sessions"])


# ── Request / Response models ─────────────────────────────────────────────────


class ApproveRequest(BaseModel):
    """POST /sessions/{id}/approve request body."""

    approval_id: str
    decision: str  # "approve" | "deny"


class ModelSwitchRequest(BaseModel):
    """POST /sessions/{id}/model request body."""

    model: str


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_uuid(val: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(val)
    except (ValueError, AttributeError):
        return None


def _valid_models() -> set[str]:
    """Return the union of OpenAI models and configured fallback models."""
    from src.config.settings import get_settings
    from src.quota.limiter import OPENAI_MODELS

    settings = get_settings()
    return set(OPENAI_MODELS) | set(settings.fallback_model_list)


# ── GET /sessions ─────────────────────────────────────────────────────────────


@router.get("")
async def list_sessions(
    limit: int = 20,
    cursor: str | None = None,
    current_user: TokenPayload = Depends(require_auth_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List conversations ordered by last_accessed DESC with cursor pagination (FR-19).

    Guest tokens are rejected with 403 via ``require_auth_user``.
    """
    user_uuid = uuid.UUID(current_user.user_id)
    repo = ConversationRepository(db)
    conversations, next_cursor = await repo.list_conversations(user_uuid, cursor, limit)

    return {
        "sessions": [
            {
                "id": str(c.id),
                "title": c.title,
                "last_accessed": c.last_accessed.isoformat(),
                "message_count": c.message_count,
                "access_count": c.access_count,
            }
            for c in conversations
        ],
        "next_cursor": next_cursor,
    }


# ── GET /sessions/{id}/messages ───────────────────────────────────────────────


@router.get("/{session_id}/messages")
async def list_messages(
    session_id: str,
    current_user: TokenPayload = Depends(require_auth_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return messages for a conversation ordered by created_at ASC (FR-20).

    Returns HTTP 403 — not 404 — for invalid UUID format and cross-user access
    to prevent information leakage about session existence.
    """
    conv_uuid = _parse_uuid(session_id)
    if conv_uuid is None:
        raise HTTPException(status_code=403, detail="Forbidden")

    user_uuid = uuid.UUID(current_user.user_id)
    msg_repo = MessageRepository(db)
    messages = await msg_repo.list_messages(user_uuid, conv_uuid)

    # If no messages, check whether the conversation exists for this user
    if not messages:
        conv_repo = ConversationRepository(db)
        conv = await conv_repo.get_conversation(user_uuid, conv_uuid)
        if conv is None:
            raise HTTPException(status_code=403, detail="Forbidden")

    return {
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "tool_name": m.tool_name,
                "turn_index": m.turn_index,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ]
    }


# ── DELETE /sessions/{id} ─────────────────────────────────────────────────────


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    current_user: TokenPayload = Depends(require_auth_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a conversation owned by the current user (FR-17).

    Sets ``is_deleted = True`` so the conversation is hidden from all reads but
    its data is retained for audit; the retention job performs the eventual hard
    purge. Returns HTTP 403 for cross-user access with no distinguishing detail.
    """
    conv_uuid = _parse_uuid(session_id)
    if conv_uuid is None:
        raise HTTPException(status_code=403, detail="Forbidden")

    user_uuid = uuid.UUID(current_user.user_id)
    repo = ConversationRepository(db)

    conv = await repo.get_conversation(user_uuid, conv_uuid)
    if conv is None:
        raise HTTPException(status_code=403, detail="Forbidden")

    await repo.soft_delete_conversation(user_uuid, conv_uuid)


# ── POST /sessions/{id}/approve ───────────────────────────────────────────────


@router.post("/{session_id}/approve")
async def approve_hitl(
    session_id: str,
    body: ApproveRequest,
    request: Request,
    current_user: TokenPayload = Depends(require_auth_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Consume a HITL approval and resume the suspended graph (FR-10).

    - 200: approval consumed successfully.
    - 409: concurrent race — another request consumed this approval_id between
           our pre-check and the atomic UPDATE.
    - 410: approval_id does not exist, is already used, or is expired.
    """
    approval_uuid = _parse_uuid(body.approval_id)
    if approval_uuid is None:
        raise HTTPException(status_code=422, detail="Invalid approval_id format")

    user_uuid = uuid.UUID(current_user.user_id)
    hitl_repo = HITLRepository(db)

    # Pre-check: distinguish 409 (concurrent race) from 410 (already done/expired)
    existing = await hitl_repo.get_approval(approval_uuid)
    if existing is None:
        raise HTTPException(status_code=410, detail="Approval not found")
    if existing.used or existing.expired:
        raise HTTPException(status_code=410, detail="Approval already used or expired")

    # Atomic consume — returns None only if a concurrent request won the race
    consumed = await hitl_repo.consume_approval(approval_uuid, str(current_user.session_id or ""))
    if consumed is None:
        raise HTTPException(status_code=409, detail="Approval was consumed by a concurrent request")

    # Append immutable audit log entry
    ip = request.client.host if request.client else None
    await hitl_repo.insert_audit_log(
        approval_id=approval_uuid,
        user_id=user_uuid,
        conversation_id=consumed.conversation_id,
        tool_name=consumed.tool_name,
        decision=body.decision,
        request_ip=ip,
        decision_reason=None,
    )

    # Resume graph asynchronously (HITL gate was suspended by interrupt_after)
    asyncio.create_task(
        _resume_graph(
            thread_id=consumed.thread_id,
            decision=body.decision,
            message_id=body.approval_id,
        )
    )

    return {"status": "ok", "decision": body.decision}


async def _resume_graph(thread_id: str, decision: str, message_id: str) -> None:
    """Resume the LangGraph from the checkpoint stored at *thread_id*."""
    try:
        from langchain_core.runnables import RunnableConfig

        from src.agents.graph import get_graph
        from src.agents.runner import resume_turn
        from src.persistence.checkpointer import get_checkpointer
        from src.persistence.redis_client import get_redis
        from src.streaming.emitter import SSEEmitter

        checkpointer = get_checkpointer()
        graph = get_graph(checkpointer)
        redis = get_redis()

        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        state = await graph.aget_state(config)
        stream_id = state.values.get("stream_id", "") if state else ""
        emitter = SSEEmitter(stream_id, redis)

        resume_state = {"hitl_decision": decision, "pending_approval": None}
        await resume_turn(graph, resume_state, config, emitter)
    except Exception:
        pass  # Best-effort; errors logged by graph nodes


# ── POST /sessions/{id}/model ─────────────────────────────────────────────────


@router.post("/{session_id}/model")
async def switch_model(
    session_id: str,
    body: ModelSwitchRequest,
    current_user: TokenPayload = Depends(require_auth_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Switch the active LLM model for a session (FR-25).

    Returns HTTP 422 when the model name is not in the registered set.
    """
    if body.model not in _valid_models():
        raise HTTPException(
            status_code=422,
            detail=f"Model '{body.model}' is not registered.",
        )

    conv_uuid = _parse_uuid(session_id)
    if conv_uuid is None:
        raise HTTPException(status_code=403, detail="Forbidden")

    user_uuid = uuid.UUID(current_user.user_id)
    repo = ConversationRepository(db)
    conv = await repo.get_conversation(user_uuid, conv_uuid)
    if conv is None:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Store active model override in Redis for next chat turn
    from src.persistence.redis_client import get_redis

    redis = get_redis()
    await redis.set(f"session:{session_id}:model", body.model, ex=86400)

    return {"session_id": session_id, "active_model": body.model}
