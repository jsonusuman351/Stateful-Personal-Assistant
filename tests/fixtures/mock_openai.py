"""Configurable mock for langchain_openai.ChatOpenAI (T-047)."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage


class MockOpenAI:
    """Drop-in replacement for ChatOpenAI that returns pre-configured responses.

    Usage with pytest's monkeypatch::

        mock = MockOpenAI()
        mock_class = mock.as_class()
        monkeypatch.setattr("src.graph.nodes.llm.ChatOpenAI", mock_class)
        monkeypatch.setattr("src.graph.nodes.router.ChatOpenAI", mock_class)

    Attributes:
        default_content: Text returned by ``ainvoke()`` when no tool_calls are set.
        tool_calls:       When non-empty, ``ainvoke()`` returns an AIMessage whose
                          ``.tool_calls`` field contains these entries so the router
                          node selects those tools.
        call_count:       Total number of ``ainvoke()`` calls received.
    """

    def __init__(self) -> None:
        """Initialise with a default text response and no tool calls."""
        self.default_content: str = "I'm a helpful AI assistant."
        self.tool_calls: list[dict[str, Any]] = []
        self.call_count: int = 0

    def set_response(self, content: str) -> None:
        """Set the text content returned by all subsequent ainvoke() calls.

        Clears any previously configured tool_calls.
        """
        self.default_content = content
        self.tool_calls = []

    def set_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        """Configure tool selection responses for the router node.

        Args:
            tool_calls: List of dicts with ``"name"`` and optional ``"args"`` keys,
                        e.g. ``[{"name": "weather", "args": {"location": "London"}}]``.
        """
        self.tool_calls = tool_calls
        self.default_content = ""

    def reset(self) -> None:
        """Reset to default state (plain text response, zero call count)."""
        self.default_content = "I'm a helpful AI assistant."
        self.tool_calls = []
        self.call_count = 0

    def as_class(self) -> type:
        """Return a class that can replace ``langchain_openai.ChatOpenAI``.

        The returned class is backed by this ``MockOpenAI`` instance, so
        ``call_count``, ``set_response()``, and ``set_tool_calls()`` remain
        usable after patching.
        """
        mock = self

        class _MockChatOpenAI:
            """Test double for langchain_openai.ChatOpenAI."""

            def __init__(self, **kwargs: object) -> None:
                pass

            def bind_tools(self, tools: object) -> _MockChatOpenAI:
                """Return self to allow ``llm.bind_tools(schemas).ainvoke(msgs)``."""
                return self

            async def ainvoke(self, messages: object) -> AIMessage:
                """Return a pre-configured AIMessage."""
                mock.call_count += 1
                if mock.tool_calls:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": tc["name"],
                                "args": tc.get("args", {}),
                                "id": f"call_{i}",
                                "type": "tool_call",
                            }
                            for i, tc in enumerate(mock.tool_calls)
                        ],
                    )
                return AIMessage(content=mock.default_content)

        return _MockChatOpenAI
