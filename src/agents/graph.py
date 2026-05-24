"""StateGraph construction, compile, and checkpointer wiring (T-029, DESIGN §2.1)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.state import AgentState
from src.graph.edges import (
    route_after_hitl,
    route_after_llm,
    route_after_router,
    route_after_tools,
)
from src.graph.nodes.error_handler import error_handler_node
from src.graph.nodes.hitl_gate import hitl_gate_node
from src.graph.nodes.llm import llm_node
from src.graph.nodes.router import router_node
from src.graph.nodes.tool_executor import tool_executor_node


def build_graph(checkpointer: Any = None) -> CompiledStateGraph:
    """Construct and compile the LangGraph StateGraph.

    Args:
        checkpointer: An AsyncPostgresSaver (or compatible) instance.  Pass
            ``None`` in tests to compile without persistence.

    Returns:
        A compiled graph with ``interrupt_after=["hitl_gate"]``.
    """
    builder: StateGraph = StateGraph(AgentState)

    builder.add_node("router", router_node)
    builder.add_node("hitl_gate", hitl_gate_node)
    builder.add_node("tool_executor", tool_executor_node)
    builder.add_node("llm", llm_node)
    builder.add_node("error_handler", error_handler_node)

    builder.set_entry_point("router")

    builder.add_conditional_edges(
        "router",
        route_after_router,
        {
            "hitl": "hitl_gate",
            "tool": "tool_executor",
            "llm_direct": "llm",
            "error": "error_handler",
        },
    )
    builder.add_conditional_edges(
        "hitl_gate",
        route_after_hitl,
        {
            "approved": "tool_executor",
            "denied": "error_handler",
            "error": "error_handler",
        },
    )
    builder.add_conditional_edges(
        "tool_executor",
        route_after_tools,
        {
            "success": "llm",
            "error": "error_handler",
        },
    )
    builder.add_conditional_edges(
        "llm",
        route_after_llm,
        {
            "success": END,
            "error": "error_handler",
        },
    )
    builder.add_edge("error_handler", END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_after=["hitl_gate"],
    )


@lru_cache(maxsize=1)
def get_graph(checkpointer: Any = None) -> CompiledStateGraph:
    """Return the compiled graph singleton.

    In production, call once during app lifespan with a live checkpointer.
    In tests, call with ``checkpointer=None``.
    """
    return build_graph(checkpointer=checkpointer)
