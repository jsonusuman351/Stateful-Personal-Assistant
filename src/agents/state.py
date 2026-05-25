"""AgentState TypedDict and sub-TypedDicts (T-022, DESIGN §2.2)."""

# No 'from __future__ import annotations' here — LangGraph inspects these
# annotations at runtime to extract reducer callables from Annotated metadata.

from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class ToolCall(TypedDict):
    """A single tool invocation selected by the router node."""

    tool_name: str
    tool_input: dict[str, Any]
    is_sensitive: bool


class ToolResult(TypedDict):
    """Outcome of a single tool invocation from tool_executor."""

    tool_name: str
    output: dict[str, Any]
    error: str | None
    duration_ms: int


class ApprovalState(TypedDict):
    """State written by hitl_gate while awaiting human approval."""

    approval_id: str  # UUID v4
    tool_name: str
    description: str
    expires_at: str  # ISO8601


class ErrorState(TypedDict):
    """Error descriptor written by any node on a fatal, unrecoverable failure."""

    code: str  # SCREAMING_SNAKE_CASE error code
    message: str  # user-facing description
    retryable: bool
    retry_count: int


def _replace(_: Any, new: Any) -> Any:
    """Replace-on-write reducer: each turn the router overwrites tool state entirely."""
    return new


class AgentState(TypedDict):
    """Shared state threaded through every LangGraph node (DESIGN §2.2).

    Reducer rules:
    - ``messages``: ``add_messages`` appends rather than overwrites (multi-turn history).
    - ``tool_calls`` / ``tool_results``: ``_replace`` overwrites every turn; the router
      writes ``[]`` to reset, then ``tool_executor`` writes the full populated list.
    """

    # Identity
    session_id: str
    user_id: str | None  # None for guest sessions
    thread_id: str  # "auth|{user_id}|{conversation_id}" or "guest|{sha256_hex}"
    stream_id: str  # UUID for the current SSE stream

    # Conversation
    messages: Annotated[list[BaseMessage], add_messages]
    turn_index: int

    # Tool orchestration — replace-on-write reducers (DESIGN §2.2)
    tool_calls: Annotated[list[ToolCall], _replace]
    tool_results: Annotated[list[ToolResult], _replace]

    # HITL
    pending_approval: ApprovalState | None  # set by hitl_gate, cleared on resume
    hitl_decision: Literal["approve", "deny"] | None  # injected by API on resume

    # LLM
    active_model: str  # current model identifier

    # Error — last-write-wins; single writer per turn
    error: ErrorState | None
