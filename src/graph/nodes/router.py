"""Router node: classifies user intent and selects tools (T-024, DESIGN §2.3)."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.agents.state import AgentState, ToolCall
from src.config.settings import get_settings
from src.tools.registry import get_registry


async def router_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Classify user intent and populate tool_calls (or [] for direct LLM path).

    Resets tool_calls and tool_results at the start of every turn (replace-on-write).
    Calls the active model with tool schemas; parses the response's tool_calls to build
    a list of ToolCall objects enriched with is_sensitive from the registry.
    """
    settings = get_settings()
    registry = get_registry()

    # Build OpenAI function schemas from the loaded registry
    tool_schemas: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": tool.input_schema,
                    "required": list(tool.input_schema.keys()),
                },
            },
        }
        for name, tool in registry.items()
    ]

    model_id: str = state["active_model"]
    llm = ChatOpenAI(model=model_id, api_key=SecretStr(settings.OPENAI_API_KEY))

    messages: list[BaseMessage] = list(state["messages"])
    if tool_schemas:
        response = await llm.bind_tools(tool_schemas).ainvoke(messages)
    else:
        response = await llm.ainvoke(messages)

    selected: list[ToolCall] = []
    for tc in getattr(response, "tool_calls", None) or []:
        name: str = tc["name"]
        tool_instance = registry.get(name)
        if tool_instance is None:
            continue
        selected.append(
            ToolCall(
                tool_name=name,
                tool_input=tc.get("args", {}),
                is_sensitive=tool_instance.is_sensitive,
            )
        )

    return {"tool_calls": selected, "tool_results": []}
