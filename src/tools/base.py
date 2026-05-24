"""BaseTool Protocol definition — extensibility interface (FR-1, DESIGN §2.5)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BaseTool(Protocol):
    """Structural interface that every tool must satisfy.

    Use ``isinstance(obj, BaseTool)`` at registry load time to validate that
    a dynamically imported class implements the full contract (FR-1, FR-5).
    """

    name: str
    description: str
    input_schema: dict
    is_sensitive: bool

    async def execute(self, tool_input: dict) -> dict:
        """Execute the tool with the given input and return a result dict."""
        ...
