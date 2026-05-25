"""Unit tests for LangGraph nodes (T-024 to T-026)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.agents.state import AgentState, ApprovalState, ErrorState, ToolCall, ToolResult

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
    from src.config.settings import get_settings

    get_settings.cache_clear()


def _state(**kwargs: Any) -> AgentState:
    """Build a minimal AgentState for node tests."""
    base: dict[str, Any] = {
        "session_id": "sess-1",
        "user_id": None,
        "thread_id": "guest|abc",
        "stream_id": "stream-1",
        "messages": [HumanMessage(content="Hello")],
        "turn_index": 0,
        "tool_calls": [],
        "tool_results": [],
        "pending_approval": None,
        "hitl_decision": None,
        "active_model": "gpt-4o-mini",
        "error": None,
    }
    base.update(kwargs)
    return base  # type: ignore[return-value]


# ── T-024: router_node ────────────────────────────────────────────────────────


async def test_router_no_tool_needed() -> None:
    """LLM response with no tool calls must return empty tool_calls and tool_results."""
    ai_resp = AIMessage(content="Hello!")
    ai_resp.tool_calls = []

    with patch("src.graph.nodes.router.ChatOpenAI") as mock_cls, patch(
        "src.graph.nodes.router.get_registry", return_value={}
    ):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=ai_resp)
        mock_cls.return_value = mock_llm

        from src.graph.nodes.router import router_node

        result = await router_node(_state(), {})

    assert result["tool_calls"] == []
    assert result["tool_results"] == []


async def test_router_selects_weather_tool() -> None:
    """LLM response with weather tool call must populate tool_calls correctly."""
    weather_mock = MagicMock()
    weather_mock.description = "Returns weather"
    weather_mock.input_schema = {"location": {"type": "string"}}
    weather_mock.is_sensitive = False

    ai_resp = AIMessage(content="")
    ai_resp.tool_calls = [
        {"name": "weather", "args": {"location": "London"}, "id": "call_1"}
    ]

    registry = {"weather": weather_mock}

    with patch("src.graph.nodes.router.ChatOpenAI") as mock_cls, patch(
        "src.graph.nodes.router.get_registry", return_value=registry
    ):
        mock_llm = MagicMock()
        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(return_value=ai_resp)
        mock_llm.bind_tools.return_value = mock_bound
        mock_cls.return_value = mock_llm

        from src.graph.nodes.router import router_node

        result = await router_node(
            _state(messages=[HumanMessage(content="What's the weather in London?")]),
            {},
        )

    assert len(result["tool_calls"]) == 1
    tc = result["tool_calls"][0]
    assert tc["tool_name"] == "weather"
    assert tc["tool_input"] == {"location": "London"}
    assert tc["is_sensitive"] is False


async def test_router_resets_tool_state() -> None:
    """Router must always return fresh empty tool_results regardless of prior state."""
    existing = ToolCall(tool_name="old", tool_input={}, is_sensitive=False)
    ai_resp = AIMessage(content="Hi")
    ai_resp.tool_calls = []

    with patch("src.graph.nodes.router.ChatOpenAI") as mock_cls, patch(
        "src.graph.nodes.router.get_registry", return_value={}
    ):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=ai_resp)
        mock_cls.return_value = mock_llm

        from src.graph.nodes.router import router_node

        result = await router_node(_state(tool_calls=[existing]), {})

    assert result["tool_results"] == []
    assert result["tool_calls"] == []


# ── T-025: hitl_gate_node ────────────────────────────────────────────────────


_AUTH_THREAD = (
    "auth"
    "|00000000-0000-0000-0000-000000000001"
    "|00000000-0000-0000-0000-000000000002"
)
_APPROVAL_UUID = UUID("12345678-1234-5678-1234-567812345678")


def _hitl_state(**kwargs: Any) -> AgentState:
    return _state(
        tool_calls=[ToolCall(tool_name="web_search", tool_input={"query": "q"}, is_sensitive=True)],
        thread_id=_AUTH_THREAD,
        **kwargs,
    )


def _mock_db_ctx(side_effect: Exception | None = None) -> MagicMock:
    """Return a mock async context manager for get_db_session."""
    ctx = MagicMock()
    if side_effect:
        ctx.__aenter__ = AsyncMock(side_effect=side_effect)
    else:
        ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


async def test_hitl_gate_writes_approval_to_db() -> None:
    """hitl_gate must call HITLRepository.create_approval within a DB session."""
    mock_repo = MagicMock()
    mock_repo.create_approval = AsyncMock(return_value=_APPROVAL_UUID)

    with patch("src.graph.nodes.hitl_gate.get_db_session", return_value=_mock_db_ctx()), patch(
        "src.graph.nodes.hitl_gate.HITLRepository", return_value=mock_repo
    ):
        from src.graph.nodes.hitl_gate import hitl_gate_node

        await hitl_gate_node(_hitl_state(), {})

    mock_repo.create_approval.assert_called_once()


async def test_hitl_gate_sets_pending_approval_state() -> None:
    """Successful DB write must return pending_approval with correct fields."""
    mock_repo = MagicMock()
    mock_repo.create_approval = AsyncMock(return_value=_APPROVAL_UUID)

    with patch("src.graph.nodes.hitl_gate.get_db_session", return_value=_mock_db_ctx()), patch(
        "src.graph.nodes.hitl_gate.HITLRepository", return_value=mock_repo
    ):
        from src.graph.nodes.hitl_gate import hitl_gate_node

        result = await hitl_gate_node(_hitl_state(), {})

    assert "pending_approval" in result
    pa: ApprovalState = result["pending_approval"]
    assert pa["approval_id"] == str(_APPROVAL_UUID)
    assert pa["tool_name"] == "web_search"
    assert "expires_at" in pa
    assert "description" in pa
    assert "error" not in result or result.get("error") is None


async def test_hitl_gate_db_error_sets_error_state() -> None:
    """DB failure must set error state with HITL_DB_ERROR code, not raise."""
    with patch(
        "src.graph.nodes.hitl_gate.get_db_session",
        return_value=_mock_db_ctx(side_effect=RuntimeError("DB is down")),
    ):
        from src.graph.nodes.hitl_gate import hitl_gate_node

        result = await hitl_gate_node(_hitl_state(), {})

    assert "error" in result
    err: ErrorState = result["error"]
    assert err["code"] == "HITL_DB_ERROR"
    assert "pending_approval" not in result or result.get("pending_approval") is None


# ── T-026: tool_executor_node ────────────────────────────────────────────────


async def test_tool_executor_parallel_dispatch() -> None:
    """Two tools with 0.1s delay each must complete in under 0.3s (parallel)."""

    async def slow_execute(tool_input: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.1)
        return {"result": "done"}

    mock_a = MagicMock()
    mock_a.execute = slow_execute
    mock_b = MagicMock()
    mock_b.execute = slow_execute

    registry = {"tool_a": mock_a, "tool_b": mock_b}
    state = _state(
        tool_calls=[
            ToolCall(tool_name="tool_a", tool_input={}, is_sensitive=False),
            ToolCall(tool_name="tool_b", tool_input={}, is_sensitive=False),
        ]
    )

    with patch("src.graph.nodes.tool_executor.get_registry", return_value=registry):
        from src.graph.nodes.tool_executor import tool_executor_node

        start = time.monotonic()
        result = await tool_executor_node(state, {})
        elapsed = time.monotonic() - start

    assert elapsed < 0.3, f"Expected parallel execution < 0.3s, got {elapsed:.3f}s"
    assert len(result["tool_results"]) == 2
    assert all(r["error"] is None for r in result["tool_results"])


async def test_tool_executor_retry_on_failure() -> None:
    """Tool that fails once then succeeds must return a successful ToolResult."""
    call_count = [0]

    async def flaky(tool_input: dict[str, Any]) -> dict[str, Any]:
        call_count[0] += 1
        if call_count[0] < 2:
            raise RuntimeError("Temporary failure")
        return {"result": "ok"}

    mock_tool = MagicMock()
    mock_tool.execute = flaky

    state = _state(tool_calls=[ToolCall(tool_name="flaky", tool_input={}, is_sensitive=False)])

    with patch("src.graph.nodes.tool_executor.get_registry", return_value={"flaky": mock_tool}), patch(
        "src.graph.nodes.tool_executor.asyncio.sleep", new_callable=AsyncMock
    ):
        from src.graph.nodes.tool_executor import tool_executor_node

        result = await tool_executor_node(state, {})

    tr = result["tool_results"][0]
    assert tr["error"] is None
    assert tr["output"] == {"result": "ok"}


async def test_tool_executor_retry_delays() -> None:
    """asyncio.sleep must be called with delays 1, 2, 4 on three retries."""
    sleep_calls: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    async def always_fail(tool_input: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Always fails")

    mock_tool = MagicMock()
    mock_tool.execute = always_fail

    state = _state(tool_calls=[ToolCall(tool_name="t", tool_input={}, is_sensitive=False)])

    with patch("src.graph.nodes.tool_executor.get_registry", return_value={"t": mock_tool}), patch(
        "src.graph.nodes.tool_executor.asyncio.sleep", side_effect=record_sleep
    ):
        from src.graph.nodes.tool_executor import tool_executor_node

        await tool_executor_node(state, {})

    assert sleep_calls == [1, 2, 4]


async def test_tool_executor_exhausted_retries_sets_error() -> None:
    """All 4 attempts failing must set ToolResult.error, not raise."""

    async def always_fail(tool_input: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Permanent failure")

    mock_tool = MagicMock()
    mock_tool.execute = always_fail

    state = _state(tool_calls=[ToolCall(tool_name="bad", tool_input={}, is_sensitive=False)])

    with patch("src.graph.nodes.tool_executor.get_registry", return_value={"bad": mock_tool}), patch(
        "src.graph.nodes.tool_executor.asyncio.sleep", new_callable=AsyncMock
    ):
        from src.graph.nodes.tool_executor import tool_executor_node

        result = await tool_executor_node(state, {})

    tr = result["tool_results"][0]
    assert tr["error"] is not None
    assert "Permanent failure" in tr["error"]
    assert tr["output"] == {}


# ── T-027: llm_node ───────────────────────────────────────────────────────────


def _make_llm_factory(responses: list[Any]) -> Any:
    """Return a ChatOpenAI factory whose instances return responses in sequence."""
    call_idx = [0]

    def factory(*args: Any, **kwargs: Any) -> MagicMock:
        idx = call_idx[0]
        call_idx[0] += 1
        mock = MagicMock()
        resp = responses[idx] if idx < len(responses) else RuntimeError("unexpected call")
        if isinstance(resp, Exception):
            mock.ainvoke = AsyncMock(side_effect=resp)
        else:
            mock.ainvoke = AsyncMock(return_value=resp)
        return mock

    return factory


async def test_llm_node_success() -> None:
    """Primary model success returns AIMessage appended to messages."""
    from langchain_core.messages import AIMessage

    ai_msg = AIMessage(content="Hello from LLM!")
    factory = _make_llm_factory([ai_msg])

    with patch("src.graph.nodes.llm.ChatOpenAI", side_effect=factory), patch(
        "src.graph.nodes.llm.asyncio.sleep", new_callable=AsyncMock
    ):
        from src.graph.nodes.llm import llm_node

        result = await llm_node(_state(), {})

    assert "messages" in result
    assert len(result["messages"]) == 1
    assert result["messages"][0].content == "Hello from LLM!"
    assert result.get("error") is None


async def test_llm_node_retries_primary_twice() -> None:
    """Primary fails twice then succeeds; asyncio.sleep called with delays [2, 4]."""
    from langchain_core.messages import AIMessage

    ai_msg = AIMessage(content="Success after retries")
    factory = _make_llm_factory([RuntimeError("fail 1"), RuntimeError("fail 2"), ai_msg])
    sleep_calls: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("src.graph.nodes.llm.ChatOpenAI", side_effect=factory), patch(
        "src.graph.nodes.llm.asyncio.sleep", side_effect=record_sleep
    ):
        from src.graph.nodes.llm import llm_node

        result = await llm_node(_state(), {})

    assert sleep_calls == [2, 4]
    assert result["messages"][0].content == "Success after retries"


async def test_llm_node_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Primary exhausted; fallback model produces a valid response."""
    from langchain_core.messages import AIMessage

    monkeypatch.setenv("FALLBACK_MODELS", '["fallback-model"]')
    from src.config.settings import get_settings

    get_settings.cache_clear()

    ai_msg = AIMessage(content="Fallback response")
    factory = _make_llm_factory([
        RuntimeError("primary 1"),
        RuntimeError("primary 2"),
        RuntimeError("primary 3"),
        ai_msg,
    ])

    with patch("src.graph.nodes.llm.ChatOpenAI", side_effect=factory), patch(
        "src.graph.nodes.llm.asyncio.sleep", new_callable=AsyncMock
    ):
        from src.graph.nodes.llm import llm_node

        result = await llm_node(_state(), {})

    assert "messages" in result
    assert result["messages"][0].content == "Fallback response"
    assert result.get("error") is None


async def test_llm_node_all_failed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """All models exhausted; returns ALL_MODELS_FAILED error state."""
    monkeypatch.setenv("FALLBACK_MODELS", '["fallback-1"]')
    from src.config.settings import get_settings

    get_settings.cache_clear()

    factory = _make_llm_factory([
        RuntimeError("primary 1"),
        RuntimeError("primary 2"),
        RuntimeError("primary 3"),
        RuntimeError("fallback 1"),
    ])

    with patch("src.graph.nodes.llm.ChatOpenAI", side_effect=factory), patch(
        "src.graph.nodes.llm.asyncio.sleep", new_callable=AsyncMock
    ):
        from src.graph.nodes.llm import llm_node

        result = await llm_node(_state(), {})

    assert "error" in result
    assert result["error"]["code"] == "ALL_MODELS_FAILED"
    assert result.get("messages") is None


async def test_llm_node_empty_response_triggers_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty content from primary treated as failure; fallback model used instead."""
    from langchain_core.messages import AIMessage

    monkeypatch.setenv("FALLBACK_MODELS", '["fallback-model"]')
    from src.config.settings import get_settings

    get_settings.cache_clear()

    empty = AIMessage(content="")
    real = AIMessage(content="Fallback answer")
    factory = _make_llm_factory([empty, empty, empty, real])

    with patch("src.graph.nodes.llm.ChatOpenAI", side_effect=factory), patch(
        "src.graph.nodes.llm.asyncio.sleep", new_callable=AsyncMock
    ):
        from src.graph.nodes.llm import llm_node

        result = await llm_node(_state(), {})

    assert "messages" in result
    assert result["messages"][0].content == "Fallback answer"


# ── T-028: error_handler_node ─────────────────────────────────────────────────


async def test_error_handler_hitl_denied() -> None:
    """HITL_DENIED error must produce a cancellation AIMessage and clear error."""
    state = _state(
        error={"code": "HITL_DENIED", "message": "denied", "retryable": False, "retry_count": 0}
    )

    from src.graph.nodes.error_handler import error_handler_node

    result = await error_handler_node(state, {})

    assert "messages" in result
    assert len(result["messages"]) == 1
    content: str = result["messages"][0].content
    assert "cancel" in content.lower() or "not approved" in content.lower()
    assert result.get("error") is None


async def test_error_handler_tool_failure() -> None:
    """TOOL_ERROR must list failing tool names in the AIMessage."""
    state = _state(
        error={"code": "TOOL_ERROR", "message": "tool failed", "retryable": False, "retry_count": 0},
        tool_results=[
            ToolResult(tool_name="weather", output={}, error="timeout", duration_ms=100),
            ToolResult(tool_name="calculator", output={"result": 42}, error=None, duration_ms=10),
        ],
    )

    from src.graph.nodes.error_handler import error_handler_node

    result = await error_handler_node(state, {})

    content: str = result["messages"][0].content
    assert "weather" in content
    assert result.get("error") is None


async def test_error_handler_all_models_failed() -> None:
    """ALL_MODELS_FAILED must produce a generic unavailability AIMessage."""
    state = _state(
        error={
            "code": "ALL_MODELS_FAILED",
            "message": "all failed",
            "retryable": True,
            "retry_count": 0,
        }
    )

    from src.graph.nodes.error_handler import error_handler_node

    result = await error_handler_node(state, {})

    content: str = result["messages"][0].content
    assert "unavailable" in content.lower() or "try again" in content.lower()
    assert result.get("error") is None
