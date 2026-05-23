"""Refresh token ORM model re-export (T-007).

RefreshToken is defined in user.py alongside the User model.
This module re-exports it for convenience.
"""

from __future__ import annotations

from src.persistence.models.user import RefreshToken

__all__ = ["RefreshToken"]
