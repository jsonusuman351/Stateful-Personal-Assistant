"""Unit tests for Phase 4 tools (T-017 to T-021)."""

from __future__ import annotations

import ast
import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.base import BaseTool

# ── T-017: BaseTool Protocol ──────────────────────────────────────────────────


class _GoodTool:
    name = "good"
    description = "A conforming tool"
    input_schema: dict = {}
    is_sensitive = False

    async def execute(self, tool_input: dict) -> dict:
        return {}


class _MissingAttrTool:
    name = "bad"
    # description missing
    input_schema: dict = {}
    is_sensitive = False

    async def execute(self, tool_input: dict) -> dict:
        return {}


def test_conforming_class_is_base_tool() -> None:
    """A class with all attrs and async execute must pass isinstance check."""
    assert isinstance(_GoodTool(), BaseTool)


def test_missing_attribute_fails_isinstance() -> None:
    """A class missing a required attribute must fail isinstance check."""
    assert not isinstance(_MissingAttrTool(), BaseTool)


# ── T-018: CalculatorTool ─────────────────────────────────────────────────────


@pytest.fixture()
def calculator():
    from src.tools.calculator import CalculatorTool
    return CalculatorTool()


async def test_calculator_arithmetic(calculator) -> None:
    """Basic arithmetic must return the correct result."""
    result = await calculator.execute({"expression": "2 ** 10 + 5 * 3"})
    assert result == {"result": 1039}


async def test_calculator_injection_raises(calculator) -> None:
    """Attempt to execute shell commands must raise ValueError."""
    with pytest.raises(ValueError):
        await calculator.execute({"expression": "__import__('os').system('ls')"})


async def test_calculator_math_functions(calculator) -> None:
    """Math functions like sqrt must be available."""
    result = await calculator.execute({"expression": "sqrt(16)"})
    assert result == {"result": 4.0}


def test_calculator_no_eval_exec() -> None:
    """Static AST scan must confirm zero calls to eval, exec, or compile."""
    src_path = Path("src/tools/calculator.py")
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    banned = {"eval", "exec", "compile"}
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in banned
    ]
    assert calls == [], f"Banned calls found: {calls}"


# ── T-019: WeatherTool ────────────────────────────────────────────────────────


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


@pytest.fixture()
def weather(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WEATHER_API_KEY", "test-weather-key")
    from src.config.settings import get_settings
    get_settings.cache_clear()
    from src.tools.weather import WeatherTool
    return WeatherTool()


@pytest.fixture()
def weather_no_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WEATHER_API_KEY", "")
    from src.config.settings import get_settings
    get_settings.cache_clear()
    from src.tools.weather import WeatherTool
    return WeatherTool()


async def test_weather_success_mock(weather) -> None:
    """execute() with mocked API must return temperature, condition, humidity, location."""
    geo_response = MagicMock()
    geo_response.status_code = 200
    geo_response.json.return_value = {"results": [{"latitude": 51.5, "longitude": -0.12}]}

    forecast_response = MagicMock()
    forecast_response.status_code = 200
    forecast_response.json.return_value = {
        "current": {
            "temperature_2m": 18.5,
            "weathercode": 0,
            "relative_humidity_2m": 65,
        }
    }

    with patch("src.tools.weather.httpx.get", side_effect=[geo_response, forecast_response]):
        result = await weather.execute({"location": "London"})

    assert "temperature" in result
    assert "condition" in result
    assert "humidity" in result
    assert "location" in result


async def test_weather_api_error_raises_tool_error(weather) -> None:
    """Non-2xx forecast API response must raise ToolExecutionError."""
    from src.tools.weather import ToolExecutionError
    geo_response = MagicMock()
    geo_response.status_code = 200
    geo_response.json.return_value = {"results": [{"latitude": 51.5, "longitude": -0.12}]}

    bad_response = MagicMock()
    bad_response.status_code = 503
    bad_response.text = "Service unavailable"

    with patch("src.tools.weather.httpx.get", side_effect=[geo_response, bad_response]):
        with pytest.raises(ToolExecutionError):
            await weather.execute({"location": "London"})


async def test_weather_missing_key_returns_config_error(weather_no_key) -> None:
    """Missing WEATHER_API_KEY must return config error dict, not crash."""
    result = await weather_no_key.execute({"location": "London"})
    assert result == {"error": "Weather API not configured"}


# ── T-020: WebSearchTool ──────────────────────────────────────────────────────


@pytest.fixture()
def web_search(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    from src.config.settings import get_settings
    get_settings.cache_clear()
    from src.tools.web_search import WebSearchTool
    return WebSearchTool()


@pytest.fixture()
def web_search_no_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    from src.config.settings import get_settings
    get_settings.cache_clear()
    from src.tools.web_search import WebSearchTool
    return WebSearchTool()


def _make_results(count: int, score: float) -> list[dict]:
    return [
        {"content": "x" * 100, "url": f"http://ex.com/{i}", "score": score}
        for i in range(count)
    ]


async def test_web_search_relevance_filter(web_search) -> None:
    """10 results (5 above 0.7, 5 below) must yield exactly 5."""
    raw = _make_results(5, 0.9) + _make_results(5, 0.5)

    with patch("src.tools.web_search.asyncio.to_thread", new=AsyncMock(return_value={"results": raw})):
        result = await web_search.execute({"query": "test"})

    assert len(result["results"]) == 5


async def test_web_search_max_results_cap(web_search) -> None:
    """8 results all above 0.7 must be capped at 5."""
    raw = _make_results(8, 0.9)

    with patch("src.tools.web_search.asyncio.to_thread", new=AsyncMock(return_value={"results": raw})):
        result = await web_search.execute({"query": "test"})

    assert len(result["results"]) <= 5


async def test_web_search_token_budget_truncation(web_search) -> None:
    """Combined snippet chars must not exceed TOKEN_BUDGET * 4."""
    raw = [{"content": "y" * 5000, "url": f"http://ex.com/{i}", "score": 0.9} for i in range(5)]

    with patch("src.tools.web_search.asyncio.to_thread", new=AsyncMock(return_value={"results": raw})):
        result = await web_search.execute({"query": "test"})

    total_chars = sum(len(r["content"]) for r in result["results"])
    assert total_chars <= web_search.TOKEN_BUDGET * 4


async def test_web_search_is_sensitive(web_search) -> None:
    """WebSearchTool.is_sensitive must be True."""
    assert web_search.is_sensitive is True


async def test_web_search_missing_key_returns_config_error(web_search_no_key) -> None:
    """Missing TAVILY_API_KEY must return config error dict, not crash."""
    result = await web_search_no_key.execute({"query": "test"})
    assert result == {"error": "Web search not configured"}


# ── T-021: Tool Registry ──────────────────────────────────────────────────────


@pytest.fixture()
def registry_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a minimal tools.yaml to a temp directory and return its path."""
    monkeypatch.setenv("WEATHER_API_KEY", "k")
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    from src.config.settings import get_settings
    get_settings.cache_clear()
    yaml_content = (
        "tools:\n"
        "  calculator:\n"
        "    class: src.tools.calculator.CalculatorTool\n"
        "  weather:\n"
        "    class: src.tools.weather.WeatherTool\n"
        "  web_search:\n"
        "    class: src.tools.web_search.WebSearchTool\n"
    )
    yaml_file = tmp_path / "tools.yaml"
    yaml_file.write_text(yaml_content)
    return yaml_file


def test_registry_loads_all_tools(registry_path: Path) -> None:
    """load_registry must return entries for calculator, weather, web_search."""
    from src.tools.registry import load_registry
    registry = load_registry(registry_path)
    assert "calculator" in registry
    assert "weather" in registry
    assert "web_search" in registry


def test_registry_extensible_with_mock_tool(tmp_path: Path) -> None:
    """A new tools.yaml entry pointing to a valid class must appear in registry."""
    import sys
    tool_src = (
        "class MockTool:\n"
        "    name = 'mock'\n"
        "    description = 'A mock tool'\n"
        "    input_schema = {}\n"
        "    is_sensitive = False\n"
        "    async def execute(self, tool_input):\n"
        "        return {}\n"
    )
    (tmp_path / "mock_tool.py").write_text(tool_src)
    sys.path.insert(0, str(tmp_path))

    yaml_content = "tools:\n  mock:\n    class: mock_tool.MockTool\n"
    yaml_file = tmp_path / "tools.yaml"
    yaml_file.write_text(yaml_content)

    from src.tools.registry import load_registry
    registry = load_registry(yaml_file)
    assert "mock" in registry

    sys.path.pop(0)


def test_registry_invalid_tool_raises(tmp_path: Path) -> None:
    """A class not implementing BaseTool must raise RegistryError."""
    import sys
    bad_src = "class BadTool:\n    pass\n"
    (tmp_path / "bad_tool.py").write_text(bad_src)
    sys.path.insert(0, str(tmp_path))

    yaml_content = "tools:\n  bad:\n    class: bad_tool.BadTool\n"
    yaml_file = tmp_path / "tools.yaml"
    yaml_file.write_text(yaml_content)

    from src.tools.registry import RegistryError, load_registry
    with pytest.raises(RegistryError):
        load_registry(yaml_file)

    sys.path.pop(0)


def test_get_tool_list_excludes_internal_fields(registry_path: Path) -> None:
    """get_tool_list must return only name, description, is_sensitive per entry."""
    from src.tools.registry import get_tool_list, load_registry
    load_registry(registry_path)
    tool_list = get_tool_list()
    assert len(tool_list) > 0
    for item in tool_list:
        assert set(item.keys()) == {"name", "description", "is_sensitive"}
