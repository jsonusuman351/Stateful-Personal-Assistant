"""LLM node: inference with retry and fallback chain (T-027, DESIGN §2.3)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI

from src.agents.state import AgentState, ErrorState
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_LLM_TIMEOUT = 30.0
_PRIMARY_RETRY_DELAYS = [2, 4]  # sleep durations between primary retry attempts


async def llm_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Synthesise a final response from conversation history and tool results.

    Primary model: 3 attempts (1 initial + 2 retries, delays 2 s and 4 s).
    Fallback chain: each model in FALLBACK_MODELS attempted once in order.
    Empty or whitespace responses are treated as failures; triggers next fallback.
    Sets ErrorState(code="ALL_MODELS_FAILED") if every model fails.
    """
    settings = get_settings()
    messages = list(state["messages"])

    tool_results = state.get("tool_results") or []
    if tool_results:
        parts: list[str] = []
        for tr in tool_results:
            if tr["error"]:
                parts.append(f"[Tool: {tr['tool_name']}] Error: {tr['error']}")
            else:
                parts.append(f"[Tool: {tr['tool_name']}] Result: {tr['output']}")
        messages = messages + [HumanMessage(content="\n".join(parts))]

    async def _try_model(model_id: str) -> AIMessage | None:
        llm = ChatOpenAI(model=model_id, api_key=settings.OPENAI_API_KEY)  # type: ignore[call-arg]
        try:
            response: AIMessage = await asyncio.wait_for(
                llm.ainvoke(messages), timeout=_LLM_TIMEOUT
            )
            content = getattr(response, "content", "") or ""
            if not content.strip():
                logger.warning("Empty response from model %s", model_id)
                return None
            usage = getattr(response, "usage_metadata", None)
            if usage:
                logger.info(
                    "Token usage — model=%s input=%s output=%s",
                    model_id,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )
            return response
        except Exception as exc:
            logger.warning("Model %s failed: %s", model_id, exc)
            return None

    # Primary model: 1 initial attempt + up to 2 retries
    response: AIMessage | None = None
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(_PRIMARY_RETRY_DELAYS[attempt - 1])
        response = await _try_model(state["active_model"])
        if response is not None:
            break

    # Fallback chain: each fallback model attempted once
    if response is None and settings.fallback_enabled:
        for fallback in settings.fallback_model_list:
            response = await _try_model(fallback)
            if response is not None:
                break

    if response is None:
        return {
            "error": ErrorState(
                code="ALL_MODELS_FAILED",
                message="All models failed to generate a response. Please try again later.",
                retryable=True,
                retry_count=0,
            )
        }

    return {"messages": [response]}
