"""Unit tests for agents/runner.py (T-030)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── shared env setup ──────────────────────────────────────────────────────────

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "test-issuer",
    "JWT_AUDIENCE": "test-audience",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    from src.config.settings import get_settings

    get_settings.cache_clear()


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_graph(events: list[dict[str, Any]]) -> MagicMock:
    """Return a mock graph whose astream_events yields the given events."""

    def astream_events(*args: Any, **kwargs: Any) -> Any:
        async def gen() -> Any:
            for e in events:
                yield e

        return gen()

    mock = MagicMock()
    mock.astream_events = astream_events
    return mock


def _make_emitter() -> MagicMock:
    emitter = MagicMock()
    emitter.emit = AsyncMock()
    return emitter


# ── T-030: run_turn ───────────────────────────────────────────────────────────


async def test_thinking_event_emitted_on_chain_start() -> None:
    """on_chain_start graph event must produce a 'thinking' SSE event."""
    graph = _make_graph([{"event": "on_chain_start", "name": "router", "data": {}}])
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    thinking_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "thinking"]
    assert len(thinking_calls) == 1
    payload = thinking_calls[0].args[1]
    assert payload["node"] == "router"
    assert "elapsed_ms" in payload


async def test_token_event_emitted_on_llm_stream() -> None:
    """on_chat_model_stream with non-empty chunk must produce a 'token' SSE event."""
    chunk = MagicMock()
    chunk.content = "Hello"
    graph = _make_graph([
        {"event": "on_chat_model_stream", "name": "llm", "data": {"chunk": chunk}}
    ])
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    token_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "token"]
    assert len(token_calls) == 1
    assert token_calls[0].args[1] == {"content": "Hello"}


async def test_done_event_emitted_at_graph_end() -> None:
    """'done' must be emitted after the graph event loop completes."""
    graph = _make_graph([])  # no events — graph terminates immediately
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    done_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "done"]
    assert len(done_calls) == 1


async def test_langsmith_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LangSmith tracer setup failure must be swallowed; run_turn must still complete."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-fake-key")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    graph = _make_graph([])
    emitter = _make_emitter()

    with patch(
        "src.agents.runner.LangChainTracer",
        side_effect=RuntimeError("langsmith unavailable"),
        create=True,
    ), patch(
        "src.agents.runner.LangSmithClient",
        side_effect=RuntimeError("langsmith unavailable"),
        create=True,
    ):
        from src.agents.runner import run_turn

        # Must not raise despite tracer failure
        await run_turn(graph, {}, {}, emitter)

    done_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "done"]
    assert len(done_calls) == 1
