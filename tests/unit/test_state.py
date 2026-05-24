"""Unit tests for AgentState TypedDict (T-022)."""

from __future__ import annotations

from typing import get_args, get_type_hints

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.agents.state import (
    AgentState,
    ApprovalState,
    ErrorState,
    ToolCall,
    ToolResult,
)


# ── T-022: AgentState ────────────────────────────────────────────────────────


def test_messages_reducer_appends() -> None:
    """add_messages reducer must append, not replace, the message list."""
    from langgraph.graph.message import add_messages

    existing = [HumanMessage(content="hello")]
    new = [AIMessage(content="world")]
    result = add_messages(existing, new)
    assert len(result) == 2
    assert result[0].content == "hello"
    assert result[1].content == "world"


def test_tool_calls_reducer_replaces() -> None:
    """tool_calls reducer must replace the previous list entirely."""
    hints = get_type_hints(AgentState, include_extras=True)
    annotation = hints["tool_calls"]
    args = get_args(annotation)
    reducer = args[1]  # second Annotated arg is the reducer callable

    old: list[ToolCall] = [ToolCall(tool_name="a", tool_input={}, is_sensitive=False)]
    new: list[ToolCall] = [ToolCall(tool_name="b", tool_input={}, is_sensitive=True)]
    result = reducer(old, new)
    assert result == new
    assert result is new  # replace-on-write: same object, not a copy


def test_agent_state_fields_present() -> None:
    """All required fields must exist in AgentState.__annotations__."""
    required = {
        "session_id",
        "user_id",
        "thread_id",
        "stream_id",
        "messages",
        "turn_index",
        "tool_calls",
        "tool_results",
        "pending_approval",
        "hitl_decision",
        "active_model",
        "error",
    }
    assert required.issubset(set(AgentState.__annotations__.keys()))
