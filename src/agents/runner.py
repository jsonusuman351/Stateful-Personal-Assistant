"""Graph invocation helpers: run_turn() and resume_turn() (T-030, DESIGN §2.6)."""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


def _with_langsmith(config: RunnableConfig) -> RunnableConfig:
    """Return config augmented with a LangSmith tracer if LANGSMITH_API_KEY is set.

    Failures are caught and logged at WARNING so tracing never breaks inference.
    """
    settings = get_settings()
    if not settings.LANGSMITH_API_KEY:
        return config
    try:
        from langchain_core.tracers import LangChainTracer
        from langsmith import Client as LangSmithClient

        tracer = LangChainTracer(client=LangSmithClient(api_key=settings.LANGSMITH_API_KEY))
        callbacks = list(config.get("callbacks") or []) + [tracer]  # type: ignore[arg-type]
        return {**config, "callbacks": callbacks}
    except Exception as exc:
        logger.warning("LangSmith tracer setup failed: %s", exc)
        return config


async def run_turn(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    input_state: dict[str, Any],
    config: RunnableConfig,
    sse_emitter: Any,
) -> None:
    """Invoke one graph turn and translate graph events to SSE.

    Iterates ``graph.astream_events(version="v2")`` and emits:

    - ``on_chain_start``       → ``thinking``
    - ``on_chat_model_stream`` → ``token``
    - ``on_tool_end``          → ``tool_result``
    - ``on_chain_end`` (hitl_gate) → ``approval_required`` or ``error``

    Always emits ``done`` after the loop, whether the graph completed
    naturally or was suspended by ``interrupt_after``.
    """
    enriched = _with_langsmith(config)
    start = time.monotonic()

    async for event in graph.astream_events(input_state, config=enriched, version="v2"):
        kind: str = event["event"]

        if kind == "on_chain_start":
            node_name: str = event.get("name", "unknown")
            elapsed = int((time.monotonic() - start) * 1000)
            await sse_emitter.emit("thinking", {"node": node_name, "elapsed_ms": elapsed})

        elif kind == "on_chat_model_stream":
            chunk = event["data"].get("chunk")
            if chunk and getattr(chunk, "content", ""):
                await sse_emitter.emit("token", {"content": chunk.content})

        elif kind == "on_tool_end":
            await sse_emitter.emit(
                "tool_result",
                {
                    "tool": event["name"],
                    "result": event["data"].get("output"),
                },
            )

        elif kind == "on_chain_end" and event.get("name") == "hitl_gate":
            output: dict[str, Any] = event["data"].get("output") or {}
            if output.get("pending_approval"):
                await sse_emitter.emit("approval_required", output["pending_approval"])
            elif output.get("error"):
                await sse_emitter.emit("error", output["error"])

    await sse_emitter.emit("done", {"message_id": str(config.get("run_id", ""))})


async def resume_turn(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    decision: dict[str, Any],
    config: RunnableConfig,
    sse_emitter: Any,
) -> None:
    """Resume a graph suspended by ``interrupt_after=["hitl_gate"]``.

    Args:
        graph:       The compiled graph.
        decision:    State update merged into the checkpoint before routing
                     resumes, e.g. ``{"hitl_decision": "approve",
                     "pending_approval": None}``.
        config:      Must carry the same ``thread_id`` / checkpoint config
                     used in the original ``run_turn`` call.
        sse_emitter: Async emitter for SSE events.
    """
    await run_turn(graph, decision, config, sse_emitter)
