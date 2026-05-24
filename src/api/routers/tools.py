"""Tool registry endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["tools"])


@router.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    """Return registered tools with public fields only (FR-33, DESIGN §9.3).

    Returns ``name``, ``description``, and ``is_sensitive`` — no internal
    config fields.  Accessible to both guest and authenticated users.
    """
    from src.tools.registry import get_tool_list

    return get_tool_list()
