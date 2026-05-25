"""Unit tests for LangGraph conditional edge functions (T-023) and graph construction (T-029)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from src.agents.state import AgentState, ToolCall, ToolResult
from src.graph.edges import (
    route_after_hitl,
    route_after_llm,
    route_after_router,
    route_after_tools,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _state(**kwargs: object) -> AgentState:
    """Build a minimal AgentState for edge function tests."""
    base: dict[object, object] = {
        "session_id": "sess-1",
        "user_id": None,
        "thread_id": "guest|abc",
        "stream_id": "stream-1",
        "messages": [],
        "turn_index": 0,
        "tool_calls": [],
        "tool_results": [],
        "pending_approval": None,
        "hitl_decision": None,
        "active_model": "gpt-4o-mini",
        "error": None,
    }
    base.update(kwargs)
    return base  # type: ignore[return-value]


# ── T-023: route_after_router ────────────────────────────────────────────────


def test_router_edge_no_tools() -> None:
    """Empty tool_calls must route to llm_direct."""
    assert route_after_router(_state(tool_calls=[])) == "llm_direct"


def test_router_edge_sensitive_tool() -> None:
    """Any sensitive tool must route to hitl."""
    tc = ToolCall(tool_name="web_search", tool_input={}, is_sensitive=True)
    assert route_after_router(_state(tool_calls=[tc])) == "hitl"


def test_router_edge_non_sensitive() -> None:
    """Only non-sensitive tools must route to tool."""
    tc = ToolCall(tool_name="weather", tool_input={}, is_sensitive=False)
    assert route_after_router(_state(tool_calls=[tc])) == "tool"


# ── T-023: route_after_hitl ──────────────────────────────────────────────────


def test_hitl_edge_approve() -> None:
    """hitl_decision='approve' must route to approved."""
    assert route_after_hitl(_state(hitl_decision="approve")) == "approved"


def test_hitl_edge_deny() -> None:
    """hitl_decision='deny' must route to denied."""
    assert route_after_hitl(_state(hitl_decision="deny")) == "denied"


def test_hitl_edge_missing_decision() -> None:
    """Missing hitl_decision must route to error."""
    assert route_after_hitl(_state(hitl_decision=None)) == "error"


# ── T-023: route_after_tools ─────────────────────────────────────────────────


def test_tools_edge_all_failed() -> None:
    """All tool results with errors must route to error."""
    results = [
        ToolResult(tool_name="a", output={}, error="fail1", duration_ms=0),
        ToolResult(tool_name="b", output={}, error="fail2", duration_ms=0),
    ]
    assert route_after_tools(_state(tool_results=results)) == "error"


def test_tools_edge_partial_success() -> None:
    """At least one successful result must route to success."""
    results = [
        ToolResult(tool_name="a", output={"data": 1}, error=None, duration_ms=100),
        ToolResult(tool_name="b", output={}, error="fail", duration_ms=50),
    ]
    assert route_after_tools(_state(tool_results=results)) == "success"


# ── T-023: route_after_llm ───────────────────────────────────────────────────


def test_llm_edge_empty_response() -> None:
    """Empty AIMessage content must route to error."""
    assert route_after_llm(_state(messages=[AIMessage(content="")])) == "error"


# ── T-029: graph construction ─────────────────────────────────────────────────


def test_graph_has_five_nodes() -> None:
    """build_graph must register exactly 5 nodes on the StateGraph."""
    with patch("src.agents.graph.StateGraph") as mock_sg:
        mock_builder = MagicMock()
        mock_sg.return_value = mock_builder

        from src.agents.graph import build_graph

        build_graph(checkpointer=None)

    assert mock_builder.add_node.call_count == 5
    node_names = {c.args[0] for c in mock_builder.add_node.call_args_list}
    assert node_names == {"router", "hitl_gate", "tool_executor", "llm", "error_handler"}


def test_graph_compiled_with_interrupt_after() -> None:
    """build_graph must compile with interrupt_after=['hitl_gate']."""
    with patch("src.agents.graph.StateGraph") as mock_sg:
        mock_builder = MagicMock()
        mock_sg.return_value = mock_builder
        sentinel = MagicMock()
        mock_builder.compile.return_value = sentinel

        from src.agents.graph import build_graph

        result = build_graph(checkpointer=None)

    mock_builder.compile.assert_called_once()
    _, kwargs = mock_builder.compile.call_args
    assert kwargs.get("interrupt_after") == ["hitl_gate"]
    assert result is sentinel


def test_all_conditional_edges_registered() -> None:
    """build_graph must register conditional edges for router, hitl_gate, tool_executor, llm."""
    with patch("src.agents.graph.StateGraph") as mock_sg:
        mock_builder = MagicMock()
        mock_sg.return_value = mock_builder

        from src.agents.graph import build_graph

        build_graph(checkpointer=None)

    sources = {c.args[0] for c in mock_builder.add_conditional_edges.call_args_list}
    assert sources == {"router", "hitl_gate", "tool_executor", "llm"}
