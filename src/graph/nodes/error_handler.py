"""Error handler node: emit user-facing messages for final failures (T-028, DESIGN §2.3)."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from src.agents.state import AgentState


def _failed_tool_names(state: AgentState) -> list[str]:
    """Return names of all tool calls that produced an error result."""
    results = state.get("tool_results") or []
    return [r["tool_name"] for r in results if r["error"]]


async def error_handler_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Emit a user-facing AIMessage based on the error code; clear the error field.

    Handles:
      HITL_DENIED          → polite cancellation message
      TOOL_ERROR / TOOL_TIMEOUT → lists which tools failed
      ALL_MODELS_FAILED / NO_FALLBACK_CONFIGURED → generic LLM failure
      HITL_TIMEOUT         → timeout notice with re-submit hint
      (default)            → generic failure message
    """
    error = state.get("error")
    code = error["code"] if error else "UNKNOWN"

    if code == "HITL_DENIED":
        content = "Your request has been cancelled. The tool action was not approved."
    elif code in ("TOOL_ERROR", "TOOL_TIMEOUT"):
        failed = _failed_tool_names(state)
        names = ", ".join(failed) if failed else "one or more tools"
        content = f"I was unable to complete your request because {names} failed. Please try again."
    elif code in ("ALL_MODELS_FAILED", "NO_FALLBACK_CONFIGURED"):
        content = (
            "I'm sorry, I couldn't generate a response right now. "
            "All available models are temporarily unavailable. Please try again later."
        )
    elif code == "HITL_TIMEOUT":
        content = (
            "Your approval request timed out. "
            "Please re-submit your message if you'd like to try again."
        )
    else:
        content = "Something went wrong while processing your request. Please try again."

    return {
        "messages": [AIMessage(content=content)],
        "error": None,
    }
