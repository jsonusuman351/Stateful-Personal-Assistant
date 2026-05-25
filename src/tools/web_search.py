"""WebSearchTool backed by Tavily with relevance filtering (FR-4, DESIGN §2.5)."""

from __future__ import annotations

import asyncio
from typing import Any

from src.config.settings import get_settings


class WebSearchTool:
    """Searches the web via Tavily; filters, caps, and truncates results.

    ``is_sensitive = True`` means every invocation requires HITL approval
    before the result is shown to the user (DESIGN §2.5).

    Class attributes are intentionally public so the registry and tests can
    inspect or override them:
    - ``RELEVANCE_THRESHOLD``: minimum Tavily score to include a result.
    - ``MAX_RESULTS``: hard cap on number of results returned.
    - ``TOKEN_BUDGET``: approximate token budget; characters truncated at
      ``TOKEN_BUDGET * 4`` (4 chars ≈ 1 token).
    """

    name = "web_search"
    description = "Searches the web and returns the top relevant results."
    input_schema: dict[str, Any] = {"query": {"type": "string"}}
    is_sensitive = True

    RELEVANCE_THRESHOLD: float = 0.7
    MAX_RESULTS: int = 5
    TOKEN_BUDGET: int = 2000

    async def execute(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Search Tavily for *tool_input['query']* and return filtered results.

        Returns:
            ``{"results": [...]}`` where each entry has ``content``, ``url``,
            ``score``.  Returns ``{"error": "..."}`` for config failures.
        """
        settings = get_settings()
        if not settings.TAVILY_API_KEY:
            return {"error": "Web search not configured"}

        query: str = tool_input.get("query", "")
        raw: dict[str, Any] = await asyncio.to_thread(
            self._search_sync, query, settings.TAVILY_API_KEY
        )
        return {"results": self._filter_and_truncate(raw.get("results", []))}

    def _search_sync(self, query: str, api_key: str) -> dict[str, Any]:
        """Blocking Tavily client call — wrapped in asyncio.to_thread."""
        from tavily import TavilyClient  # lazy import; not installed in all envs
        client = TavilyClient(api_key=api_key)
        return client.search(query)  # type: ignore[no-any-return]

    def _filter_and_truncate(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply relevance filter, cap, and token-budget truncation."""
        filtered = [r for r in results if r.get("score", 0) >= self.RELEVANCE_THRESHOLD]
        capped = filtered[: self.MAX_RESULTS]
        char_budget = self.TOKEN_BUDGET * 4
        output: list[dict[str, Any]] = []
        used = 0
        for item in capped:
            content: str = item.get("content", "")
            remaining = char_budget - used
            if remaining <= 0:
                break
            if len(content) > remaining:
                content = content[:remaining]
            output.append({"content": content, "url": item.get("url", ""), "score": item.get("score", 0)})
            used += len(content)
        return output
