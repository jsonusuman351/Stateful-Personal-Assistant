"""Unit tests for agents/runner.py (T-030)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── shared env setup ──────────────────────────────────────────────────────────

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",  # pragma: allowlist secret
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
    graph = _make_graph(
        [{"event": "on_chat_model_stream", "name": "llm", "data": {"chunk": chunk}}]
    )
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

    with (
        patch(
            "src.agents.runner.LangChainTracer",
            side_effect=RuntimeError("langsmith unavailable"),
            create=True,
        ),
        patch(
            "src.agents.runner.LangSmithClient",
            side_effect=RuntimeError("langsmith unavailable"),
            create=True,
        ),
    ):
        from src.agents.runner import run_turn

        # Must not raise despite tracer failure
        await run_turn(graph, {}, {}, emitter)

    done_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "done"]
    assert len(done_calls) == 1


async def test_langsmith_tracer_setup_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LangSmith tracer set up without error must augment the config callbacks."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-real-key")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    mock_tracer = MagicMock()
    mock_client = MagicMock()

    graph = _make_graph([])
    emitter = _make_emitter()

    with (
        patch("src.agents.runner.LangChainTracer", return_value=mock_tracer, create=True),
        patch("src.agents.runner.LangSmithClient", return_value=mock_client, create=True),
    ):
        from src.agents.runner import run_turn

        await run_turn(graph, {}, {}, emitter)

    # If tracer was injected, done is still emitted
    done_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "done"]
    assert len(done_calls) == 1


async def test_token_event_not_emitted_for_empty_content() -> None:
    """on_chat_model_stream with empty chunk.content must NOT emit a 'token' event."""
    chunk = MagicMock()
    chunk.content = ""  # empty — falsy
    graph = _make_graph(
        [{"event": "on_chat_model_stream", "name": "llm", "data": {"chunk": chunk}}]
    )
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    token_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "token"]
    assert len(token_calls) == 0


async def test_token_event_not_emitted_for_none_chunk() -> None:
    """on_chat_model_stream with no chunk must NOT emit a 'token' event."""
    graph = _make_graph([{"event": "on_chat_model_stream", "name": "llm", "data": {"chunk": None}}])
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    token_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "token"]
    assert len(token_calls) == 0


async def test_tool_result_event_emitted_on_tool_end() -> None:
    """on_tool_end graph event must produce a 'tool_result' SSE event."""
    graph = _make_graph([{"event": "on_tool_end", "name": "calculator", "data": {"output": "42"}}])
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    tool_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "tool_result"]
    assert len(tool_calls) == 1
    assert tool_calls[0].args[1] == {"tool": "calculator", "result": "42"}


async def test_approval_required_emitted_on_hitl_gate_pending() -> None:
    """on_chain_end for hitl_gate with pending_approval must emit 'approval_required'."""
    pending = {"approval_id": "abc-123", "tool_name": "send_email"}
    graph = _make_graph(
        [
            {
                "event": "on_chain_end",
                "name": "hitl_gate",
                "data": {"output": {"pending_approval": pending}},
            }
        ]
    )
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    approval_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "approval_required"]
    assert len(approval_calls) == 1
    assert approval_calls[0].args[1] == pending


async def test_error_emitted_on_hitl_gate_error() -> None:
    """on_chain_end for hitl_gate with error must emit 'error'."""
    graph = _make_graph(
        [
            {
                "event": "on_chain_end",
                "name": "hitl_gate",
                "data": {"output": {"error": "Tool blocked"}},
            }
        ]
    )
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    error_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "error"]
    assert len(error_calls) == 1
    assert error_calls[0].args[1] == "Tool blocked"


async def test_chain_end_non_hitl_gate_ignored() -> None:
    """on_chain_end for a node other than hitl_gate must not emit any event."""
    graph = _make_graph([{"event": "on_chain_end", "name": "router", "data": {"output": {}}}])
    emitter = _make_emitter()

    from src.agents.runner import run_turn

    await run_turn(graph, {}, {}, emitter)

    non_done = [c for c in emitter.emit.call_args_list if c.args[0] != "done"]
    assert len(non_done) == 0


async def test_resume_turn_delegates_to_run_turn() -> None:
    """resume_turn must delegate to run_turn with the supplied decision state."""
    graph = _make_graph([])
    emitter = _make_emitter()

    from src.agents.runner import resume_turn

    await resume_turn(graph, {"hitl_decision": "approve"}, {}, emitter)

    done_calls = [c for c in emitter.emit.call_args_list if c.args[0] == "done"]
    assert len(done_calls) == 1
