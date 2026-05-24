"""JWT creation and claim validation (iss, aud, exp, nbf, jti)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt
from jwt.exceptions import InvalidTokenError

from src.config.settings import get_settings


class JWTError(Exception):
    """Raised by decode_token when any JWT validation check fails."""


def create_access_token(user_id: str, jti: str) -> str:
    """Return a signed HS256 JWT for an authenticated user.

    Claims: sub, user_id, jti, iss, aud, exp (+1 h), nbf (now).
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "user_id": user_id,
        "jti": jti,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "exp": now + timedelta(hours=1),
        "nbf": now,
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def create_guest_token(session_id: str, jti: str) -> str:
    """Return a signed HS256 JWT for a guest session.

    Claims: mode="guest", session_id, jti, iss, aud, exp (+24 h), nbf (now).
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "mode": "guest",
        "session_id": session_id,
        "jti": jti,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "exp": now + timedelta(hours=24),
        "nbf": now,
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def create_refresh_token(user_id: str, jti: str) -> str:
    """Return a signed HS256 refresh JWT for an authenticated user.

    Claims: sub, user_id, type="refresh", jti, iss, aud, exp (+30 d), nbf (now).
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "user_id": user_id,
        "type": "refresh",
        "jti": jti,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "exp": now + timedelta(days=30),
        "nbf": now,
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    """Decode and fully validate a JWT; return its claims dict.

    Validates (in order): parseable, alg=HS256 only, signature, iss, aud,
    exp, nbf.  Raises :class:`JWTError` for any failure — callers must not
    inspect which check failed (DESIGN §9.2).
    """
    settings = get_settings()
    try:
        claims: dict[str, Any] = pyjwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=["HS256"],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
        return claims
    except InvalidTokenError as exc:
        raise JWTError(str(exc)) from exc
