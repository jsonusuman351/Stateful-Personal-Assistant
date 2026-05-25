"""HITL gate node: atomic write and interrupt (T-025, DESIGN §2.3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig

from src.agents.state import AgentState, ApprovalState, ErrorState
from src.persistence.database import get_db_session
from src.persistence.repositories.hitl_repo import HITLRepository

_EXPIRY_MINUTES = 10


def _parse_thread_id(thread_id: str) -> tuple[UUID | None, UUID | None]:
    """Extract (user_id, conversation_id) from 'auth|{uid}|{cid}' thread format.

    Returns (None, None) for guest threads or malformed IDs.
    """
    parts = thread_id.split("|")
    if len(parts) == 3 and parts[0] == "auth":
        try:
            return UUID(parts[1]), UUID(parts[2])
        except ValueError:
            pass
    return None, None


async def hitl_gate_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Record pending approval atomically in the DB; return state update.

    On success  → writes ``pending_approval`` to state.
    On DB error → writes ``error`` to state; does not raise.

    LangGraph suspends the graph after this node via
    ``interrupt_after=["hitl_gate"]`` configured in agents/graph.py.
    """
    tool_calls = state.get("tool_calls") or []
    sensitive_call = next((tc for tc in tool_calls if tc["is_sensitive"]), None)
    tool_name = sensitive_call["tool_name"] if sensitive_call else "unknown"

    user_id, conversation_id = _parse_thread_id(state["thread_id"])
    configurable: dict[str, Any] = (config.get("configurable") or {})
    checkpoint_id: str = configurable.get("checkpoint_id", "")

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=_EXPIRY_MINUTES)

    try:
        async with get_db_session() as session:
            repo = HITLRepository(session)
            approval_uuid: UUID = await repo.create_approval(
                conversation_id=conversation_id or UUID(int=0),
                user_id=user_id,
                tool_name=tool_name,
                thread_id=state["thread_id"],
                checkpoint_id=checkpoint_id,
            )
    except Exception as exc:
        return {
            "error": ErrorState(
                code="HITL_DB_ERROR",
                message=str(exc),
                retryable=False,
                retry_count=0,
            )
        }

    return {
        "pending_approval": ApprovalState(
            approval_id=str(approval_uuid),
            tool_name=tool_name,
            description=f"Approve {tool_name} tool execution",
            expires_at=expires_at.isoformat(),
        )
    }
