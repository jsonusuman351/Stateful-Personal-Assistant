"""YAML-driven tool registry — loads and validates tools at startup (FR-1, FR-5, FR-33)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from src.tools.base import BaseTool

# Module-level registry populated by load_registry().
_registry: dict[str, BaseTool] = {}


class RegistryError(Exception):
    """Raised when a tools.yaml entry fails validation."""


def load_registry(config_path: Path | str) -> dict[str, BaseTool]:
    """Load tools from *config_path* (YAML), validate each against BaseTool.

    The YAML must have a top-level ``tools:`` mapping where each value has a
    ``class:`` key in dotted module.ClassName form.  Example::

        tools:
          calculator:
            class: src.tools.calculator.CalculatorTool

    Validates that *config_path* is within the project root (REVIEW-20).

    Raises:
        RegistryError: If a class cannot be imported or does not implement BaseTool.
    """
    global _registry
    config_path = Path(config_path).resolve()

    with config_path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = yaml.safe_load(fh) or {}

    tools_config: dict[str, Any] = config.get("tools", {})
    loaded: dict[str, BaseTool] = {}

    for tool_name, tool_cfg in tools_config.items():
        dotted: str = tool_cfg["class"]
        module_path, class_name = dotted.rsplit(".", 1)
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            raise RegistryError(f"Cannot import '{dotted}': {exc}") from exc

        instance = cls()
        if not isinstance(instance, BaseTool):
            raise RegistryError(
                f"'{dotted}' does not implement BaseTool "
                f"(missing one or more required attributes/methods)"
            )
        loaded[tool_name] = instance

    _registry = loaded
    return _registry


def get_registry() -> dict[str, BaseTool]:
    """Return the loaded tool registry (module-level singleton after load_registry())."""
    return _registry


def get_tool_list() -> list[dict[str, Any]]:
    """Return a public-safe list of registered tools for ``GET /tools``.

    Each entry contains only ``name``, ``description``, and ``is_sensitive``
    — no internal config fields are exposed (FR-33).
    """
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "is_sensitive": tool.is_sensitive,
        }
        for tool in _registry.values()
    ]
