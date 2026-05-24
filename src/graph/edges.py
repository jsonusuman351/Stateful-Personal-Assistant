"""Conditional edge functions for the LangGraph StateGraph (T-023, DESIGN §2.4)."""

from __future__ import annotations

from src.agents.state import AgentState


def route_after_router(state: AgentState) -> str:
    """Select next node after router based on tool sensitivity.

    Returns:
        "llm_direct" — no tools selected; go straight to the LLM node.
        "hitl"       — at least one sensitive tool; require human approval.
        "tool"       — only non-sensitive tools; execute immediately.
        "error"      — unexpected error state from router (default guard).
    """
    if not state["tool_calls"]:
        return "llm_direct"
    if any(tc["is_sensitive"] for tc in state["tool_calls"]):
        return "hitl"
    if state.get("error"):
        return "error"
    return "tool"


def route_after_hitl(state: AgentState) -> str:
    """Select next node after hitl_gate resolves on graph resume.

    Called only when the graph resumes via graph.ainvoke() after the
    interrupt_after suspension. The API handler injects hitl_decision into
    state before resuming.

    Returns:
        "approved" — user approved; proceed to tool_executor.
        "denied"   — user denied; proceed to error_handler.
        "error"    — missing or unknown decision; proceed to error_handler.
    """
    decision = state.get("hitl_decision")
    if decision == "approve":
        return "approved"
    if decision == "deny":
        return "denied"
    return "error"


def route_after_tools(state: AgentState) -> str:
    """Select next node after tool execution.

    Routes to error_handler only when ALL tools failed after retries.
    Partial success (at least one tool returned a result without error) still
    routes to the LLM so it can synthesise with available data.

    Returns:
        "success" — at least one tool succeeded; proceed to llm.
        "error"   — every tool failed after retries; proceed to error_handler.
    """
    if state.get("error"):
        return "error"
    if all(r["error"] for r in state["tool_results"]):
        return "error"
    return "success"


def route_after_llm(state: AgentState) -> str:
    """Select next node after LLM synthesis.

    Returns:
        "success" — non-empty AIMessage appended; graph terminates at END.
        "error"   — LLM produced empty/missing response; proceed to error_handler.
    """
    if state.get("error"):
        return "error"
    last = state["messages"][-1] if state["messages"] else None
    if not last or not getattr(last, "content", ""):
        return "error"
    return "success"
