"""SSE event Pydantic models — one model per event type (T-031, FR-13)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ThinkingEvent(BaseModel):
    """Emitted when a graph node starts executing."""

    event_type: Literal["thinking"] = "thinking"
    node: str
    elapsed_ms: int


class TokenEvent(BaseModel):
    """Emitted for each streamed LLM token."""

    event_type: Literal["token"] = "token"
    content: str


class ToolResultEvent(BaseModel):
    """Emitted when a tool finishes executing."""

    event_type: Literal["tool_result"] = "tool_result"
    tool: str
    result: Any = None


class ApprovalRequiredEvent(BaseModel):
    """Emitted when the HITL gate suspends the graph pending user approval."""

    event_type: Literal["approval_required"] = "approval_required"
    approval_id: str
    tool_name: str
    description: str
    expires_at: str


class ErrorEvent(BaseModel):
    """Emitted when the error_handler node handles a final failure."""

    event_type: Literal["error"] = "error"
    code: str
    message: str
    retryable: bool


class DoneEvent(BaseModel):
    """Emitted when the graph terminates or is suspended by interrupt_after."""

    event_type: Literal["done"] = "done"
    message_id: str
