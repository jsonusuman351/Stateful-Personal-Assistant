"""Configurable mock for WebSearchTool._search_sync (T-047)."""

from __future__ import annotations

from typing import Any


class MockTavily:
    """Drop-in replacement for ``WebSearchTool._search_sync``.

    Usage with pytest's monkeypatch::

        mock = MockTavily()
        monkeypatch.setattr(
            "src.tools.web_search.WebSearchTool._search_sync",
            mock.as_method(),
        )

    Attributes:
        results:     List of result dicts returned by ``as_method()``.
                     Each dict should have ``"content"``, ``"url"``, ``"score"``.
        call_count:  Number of search calls received.
    """

    _DEFAULT_RESULTS: list[dict[str, Any]] = [
        {
            "content": "Test result 1: detailed information about the topic.",
            "url": "https://example.com/result-1",
            "score": 0.95,
        },
        {
            "content": "Test result 2: additional context and background.",
            "url": "https://example.com/result-2",
            "score": 0.85,
        },
        {
            "content": "Test result 3: supplementary data.",
            "url": "https://example.com/result-3",
            "score": 0.75,
        },
    ]

    def __init__(self) -> None:
        """Initialise with three default high-relevance results."""
        self.results: list[dict[str, Any]] = list(self._DEFAULT_RESULTS)
        self.call_count: int = 0
        self.last_query: str = ""

    def set_results(self, results: list[dict[str, Any]]) -> None:
        """Replace the result list returned by subsequent calls.

        Args:
            results: List of dicts.  Each should have at minimum ``"score"``
                     so ``WebSearchTool._filter_and_truncate`` can apply the
                     relevance threshold.
        """
        self.results = results

    def reset(self) -> None:
        """Reset to default results, zero call count, and empty last_query."""
        self.results = list(self._DEFAULT_RESULTS)
        self.call_count = 0
        self.last_query = ""

    def as_method(self) -> object:
        """Return a callable that replaces the ``_search_sync`` instance method.

        The original signature is ``(self, query: str, api_key: str) -> dict``
        where ``self`` is the ``WebSearchTool`` instance.  This replacement
        ignores ``self`` and ``api_key`` and returns the configured results.
        """
        mock = self

        def _mock_search_sync(
            _tool_self: object,
            query: str,
            api_key: str,
        ) -> dict[str, Any]:
            mock.call_count += 1
            mock.last_query = query
            return {"results": mock.results, "query": query}

        return _mock_search_sync
