"""Unit tests for JWT creation and validation (T-014)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt
import pytest

from src.auth.jwt import (
    JWTError,
    create_access_token,
    create_guest_token,
    create_refresh_token,
    decode_token,
)

_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test",  # pragma: allowlist secret
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "test-issuer",
    "JWT_AUDIENCE": "test-audience",
}


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject required env vars and bypass the lru_cache singleton."""
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)
    # Clear lru_cache so settings pick up the monkeypatched env vars.
    from src.config.settings import get_settings
    get_settings.cache_clear()


class TestCreateAccessToken:
    def test_access_token_claims(self) -> None:
        """Access token must carry iss, aud, exp, jti, user_id claims."""
        user_id = "user-123"
        jti = "jti-abc"
        token = create_access_token(user_id, jti)
        claims = decode_token(token)

        assert claims["user_id"] == user_id
        assert claims["jti"] == jti
        assert claims["iss"] == "test-issuer"
        assert claims["aud"] == "test-audience"
        assert "exp" in claims

    def test_access_token_expires_in_one_hour(self) -> None:
        """Access token exp must be approximately now + 1 hour."""
        before = datetime.now(timezone.utc)
        token = create_access_token("uid", "jti")

        claims = decode_token(token)
        exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)

        delta = (exp - before).total_seconds()
        assert 3599 <= delta <= 3602


class TestCreateGuestToken:
    def test_guest_token_claims(self) -> None:
        """Guest token must carry mode='guest', session_id, exp (+24 h)."""
        session_id = "sess-xyz"
        jti = "jti-guest"
        token = create_guest_token(session_id, jti)
        claims = decode_token(token)

        assert claims["mode"] == "guest"
        assert claims["session_id"] == session_id
        assert claims["jti"] == jti

        exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        assert exp > now + timedelta(hours=23)  # roughly 24 h


class TestCreateRefreshToken:
    def test_refresh_token_expires_in_30_days(self) -> None:
        """Refresh token exp must be approximately now + 30 days."""
        before = datetime.now(timezone.utc)
        token = create_refresh_token("uid", "jti")

        claims = decode_token(token)
        exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)

        delta = (exp - before).total_seconds()
        assert timedelta(days=30).total_seconds() - 1 <= delta <= timedelta(days=30).total_seconds() + 2


class TestDecodeToken:
    def _make_token(self, overrides: dict[str, Any]) -> str:
        """Create a raw PyJWT token with custom payload for rejection tests."""
        base = {
            "sub": "uid",
            "iss": "test-issuer",
            "aud": "test-audience",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "nbf": datetime.now(timezone.utc),
            "jti": "jti-1",
        }
        base.update(overrides)
        return pyjwt.encode(base, "a" * 32, algorithm="HS256")

    def test_wrong_iss_rejected(self) -> None:
        """decode_token must raise JWTError for a token with wrong iss."""
        token = self._make_token({"iss": "bad-issuer"})
        with pytest.raises(JWTError):
            decode_token(token)

    def test_wrong_aud_rejected(self) -> None:
        """decode_token must raise JWTError for a token with wrong aud."""
        token = self._make_token({"aud": "bad-audience"})
        with pytest.raises(JWTError):
            decode_token(token)

    def test_expired_token_rejected(self) -> None:
        """decode_token must raise JWTError for an expired token."""
        token = self._make_token({"exp": datetime.now(timezone.utc) - timedelta(seconds=1)})
        with pytest.raises(JWTError):
            decode_token(token)

    def test_wrong_signature_rejected(self) -> None:
        """decode_token must raise JWTError when signature does not match."""
        bad_token = pyjwt.encode(
            {"sub": "uid", "iss": "test-issuer", "aud": "test-audience",
             "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            "b" * 32,  # different secret — 32 bytes to pass key-length check
            algorithm="HS256",
        )
        with pytest.raises(JWTError):
            decode_token(bad_token)
