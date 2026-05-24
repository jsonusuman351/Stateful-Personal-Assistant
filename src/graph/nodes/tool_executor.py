"""Tool executor node: concurrent dispatch with per-tool retry (T-026, DESIGN §2.3)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from langchain_core.runnables import RunnableConfig

from src.agents.state import AgentState, ToolCall, ToolResult
from src.tools.registry import get_registry

_TOOL_TIMEOUT = 3.0  # seconds per individual tool call
_RETRY_DELAYS = [1, 2, 4]  # sleep durations between retry attempts (3 retries total)


async def tool_executor_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Execute all tool_calls concurrently; retry each up to 3 times on failure.

    Uses asyncio.gather for parallelism (FR-7).
    Per-tool retries use asyncio.sleep delays of 1s, 2s, 4s (FR-27).
    Returns {"tool_results": [...]}, replacing the previous list entirely.
    """
    registry = get_registry()

    async def run_one(tc: ToolCall) -> ToolResult:
        """Execute a single tool call with timeout and inline retry."""
        tool = registry[tc["tool_name"]]
        last_error = ""
        start = time.monotonic()

        for attempt in range(4):  # attempt 0 (no delay) + 3 retries
            if attempt > 0:
                await asyncio.sleep(_RETRY_DELAYS[attempt - 1])
            try:
                output: dict[str, Any] = await asyncio.wait_for(
                    tool.execute(tc["tool_input"]), timeout=_TOOL_TIMEOUT
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                return ToolResult(
                    tool_name=tc["tool_name"],
                    output=output,
                    error=None,
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                last_error = str(exc)

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(
            tool_name=tc["tool_name"],
            output={},
            error=last_error,
            duration_ms=duration_ms,
        )

    results: list[ToolResult] = list(
        await asyncio.gather(*[run_one(tc) for tc in state["tool_calls"]])
    )
    return {"tool_results": results}
