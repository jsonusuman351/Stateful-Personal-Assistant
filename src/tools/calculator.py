"""CalculatorTool implementation using simpleeval (FR-3, NFR-11)."""

from __future__ import annotations

import math

from simpleeval import EvalWithCompoundTypes, NameNotDefined


class CalculatorTool:
    """Safe arithmetic evaluator — never calls eval, exec, or compile.

    Uses simpleeval with an explicit math function allowlist so that
    expressions like ``sqrt(16)`` work while arbitrary code execution
    is impossible (NFR-11).
    """

    name = "calculator"
    description = "Evaluates arithmetic expressions safely, including math functions."
    input_schema: dict = {"expression": {"type": "string"}}
    is_sensitive = False

    _MATH_FUNCTIONS: dict = {
        name: getattr(math, name)
        for name in dir(math)
        if callable(getattr(math, name)) and not name.startswith("_")
    }

    async def execute(self, tool_input: dict) -> dict:
        """Evaluate *tool_input['expression']* and return ``{"result": value}``.

        Raises:
            ValueError: If the expression is invalid or attempts code injection.
        """
        expression: str = tool_input.get("expression", "")
        evaluator = EvalWithCompoundTypes(functions=self._MATH_FUNCTIONS)
        try:
            value = evaluator.eval(expression)
        except Exception as exc:
            raise ValueError(f"Invalid expression: {exc}") from exc
        return {"result": value}
