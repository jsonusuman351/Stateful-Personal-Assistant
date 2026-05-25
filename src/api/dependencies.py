"""FastAPI dependency providers: get_current_user, require_auth_user, get_db, get_redis."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.blacklist import is_blacklisted
from src.auth.jwt import JWTError, decode_token
from src.persistence.database import get_db_session
from src.persistence.redis_client import get_redis as _get_redis_singleton

_bearer = HTTPBearer(auto_error=False)


class TokenPayload:
    """Validated JWT claims with convenience accessors."""

    def __init__(self, claims: dict[str, Any]) -> None:
        """Wrap raw JWT claims dict."""
        self.claims = claims
        self.user_id: str | None = claims.get("user_id")
        self.session_id: str | None = claims.get("session_id")
        self.mode: str = claims.get("mode", "auth")
        self.jti: str = claims.get("jti", "")

    @property
    def is_guest(self) -> bool:
        """True when mode is ``"guest"``."""
        return self.mode == "guest"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> TokenPayload:
    """Decode and validate the Bearer JWT.

    Returns a guest ``TokenPayload`` (no session_id) when no ``Authorization``
    header is present, so endpoints accessible to guests work without a token.

    Raises:
        HTTPException(401): JWT validation failure or blacklisted JTI.
    """
    if credentials is None:
        return TokenPayload({"mode": "guest", "session_id": None, "jti": ""})

    try:
        claims = decode_token(credentials.credentials)
    except JWTError as err:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from err

    jti = claims.get("jti", "")
    if jti and await is_blacklisted(jti):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    return TokenPayload(claims)


async def require_auth_user(
    user: TokenPayload = Depends(get_current_user),
) -> TokenPayload:
    """Require a non-guest authenticated user.

    Raises:
        HTTPException(403): token is guest or no token was provided.
    """
    if user.is_guest:
        raise HTTPException(status_code=403, detail="Authentication required")
    return user


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a transactional ``AsyncSession`` for the current request."""
    async with get_db_session() as session:
        yield session


async def get_redis() -> aioredis.Redis:
    """Return the module-level Redis singleton."""
    return _get_redis_singleton()
