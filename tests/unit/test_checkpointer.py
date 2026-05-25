"""Unit tests for LangGraph PostgreSQL checkpointer wiring (T-012)."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.persistence.checkpointer as checkpointer_module
from src.persistence.checkpointer import (
    _to_psycopg_url,
    build_guest_thread_id,
    build_thread_id,
    get_checkpointer,
    setup_checkpointer,
)

# Minimal env vars required by Settings.
_REQUIRED_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "sk-test-key",  # pragma: allowlist secret
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/testdb",  # pragma: allowlist secret
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "a" * 32,
    "JWT_ISSUER": "stateful-assistant",
    "JWT_AUDIENCE": "stateful-assistant-users",
}


@pytest.fixture(autouse=True)
def reset_checkpointer_state() -> None:
    """Reset module-level singletons before each test.

    get_checkpointer() and setup_checkpointer() use module globals to track
    singleton state.  Resetting them prevents cross-test contamination.
    """
    checkpointer_module._checkpointer = None
    checkpointer_module._setup_done = False


# ── _to_psycopg_url ──────────────────────────────────────────────────────────


class TestToPsycopgUrl:
    def test_strips_asyncpg_prefix(self) -> None:
        """postgresql+asyncpg:// must be replaced with postgresql://."""
        url = "postgresql+asyncpg://user:pass@localhost:5432/mydb"  # pragma: allowlist secret
        assert _to_psycopg_url(url) == "postgresql://user:pass@localhost:5432/mydb"  # pragma: allowlist secret

    def test_plain_url_unchanged(self) -> None:
        """A URL without the asyncpg prefix must be returned unchanged."""
        url = "postgresql://user:pass@localhost:5432/mydb"  # pragma: allowlist secret
        assert _to_psycopg_url(url) == url

    def test_only_first_occurrence_replaced(self) -> None:
        """The replacement must target only the leading prefix, not all occurrences."""
        # Pathological URL with the prefix appearing twice
        url = "postgresql+asyncpg://host/postgresql+asyncpg://oops"
        result = _to_psycopg_url(url)
        assert result.startswith("postgresql://")
        # Second occurrence is untouched
        assert "postgresql+asyncpg://" in result


# ── build_thread_id ───────────────────────────────────────────────────────────


class TestBuildThreadId:
    def test_thread_id_auth_format(self) -> None:
        """build_thread_id must return 'auth|{user_id}|{conversation_id}'."""
        user_id = "550e8400-e29b-41d4-a716-446655440000"
        conversation_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
        result = build_thread_id(user_id, conversation_id)
        assert result == f"auth|{user_id}|{conversation_id}"

    def test_thread_id_starts_with_auth_prefix(self) -> None:
        """Auth thread IDs must be prefixed with 'auth|'."""
        assert build_thread_id("uid", "cid").startswith("auth|")

    def test_thread_id_contains_exactly_two_pipes(self) -> None:
        """Auth thread IDs must have exactly two pipe delimiters."""
        assert build_thread_id("uid", "cid").count("|") == 2

    def test_thread_id_components_preserved(self) -> None:
        """Both user_id and conversation_id must appear verbatim."""
        uid, cid = "abc-123", "def-456"
        parts = build_thread_id(uid, cid).split("|")
        assert parts[1] == uid
        assert parts[2] == cid


# ── build_guest_thread_id ─────────────────────────────────────────────────────


class TestBuildGuestThreadId:
    def test_thread_id_guest_format(self) -> None:
        """build_guest_thread_id must return 'guest|{64-char hex}'."""
        result = build_guest_thread_id("192.168.1.1", "Mozilla/5.0")
        assert result.startswith("guest|")
        hex_part = result[len("guest|"):]
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_guest_thread_id_uses_sha256(self) -> None:
        """The hash must be the SHA-256 of the ip+user_agent concatenation."""
        ip, ua = "10.0.0.1", "curl/7.68.0"
        expected = hashlib.sha256(f"{ip}{ua}".encode()).hexdigest()
        assert build_guest_thread_id(ip, ua) == f"guest|{expected}"

    def test_guest_thread_id_is_deterministic(self) -> None:
        """Same IP + UA must always produce the same thread ID."""
        assert build_guest_thread_id("1.1.1.1", "AgentA") == build_guest_thread_id(
            "1.1.1.1", "AgentA"
        )

    def test_guest_thread_id_differs_on_different_ip(self) -> None:
        """Different IP addresses must produce different thread IDs."""
        assert build_guest_thread_id("1.1.1.1", "AgentA") != build_guest_thread_id(
            "2.2.2.2", "AgentA"
        )

    def test_guest_thread_id_differs_on_different_ua(self) -> None:
        """Different User-Agent strings must produce different thread IDs."""
        assert build_guest_thread_id("1.1.1.1", "AgentA") != build_guest_thread_id(
            "1.1.1.1", "AgentB"
        )


# ── get_checkpointer ──────────────────────────────────────────────────────────


class TestGetCheckpointer:
    def test_returns_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_checkpointer() called twice must return the exact same object."""
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)

        mock_saver = MagicMock()

        with patch("src.persistence.checkpointer._create_saver", return_value=mock_saver):
            first = get_checkpointer()
            second = get_checkpointer()

        assert first is second
        assert first is mock_saver

    def test_url_is_converted_to_psycopg_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The asyncpg driver prefix must be stripped before passing to _create_saver."""
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)

        captured_urls: list[str] = []

        def _spy(conn_str: str) -> MagicMock:
            captured_urls.append(conn_str)
            return MagicMock()

        with patch("src.persistence.checkpointer._create_saver", side_effect=_spy):
            get_checkpointer()

        assert len(captured_urls) == 1
        assert captured_urls[0] == "postgresql://user:pass@localhost/testdb"  # pragma: allowlist secret


# ── setup_checkpointer ────────────────────────────────────────────────────────


class TestSetupCheckpointer:
    async def test_setup_called_on_first_invocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """setup() must be awaited on the checkpointer during first call."""
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)

        mock_saver = AsyncMock()

        with patch("src.persistence.checkpointer._create_saver", return_value=mock_saver):
            await setup_checkpointer()

        mock_saver.setup.assert_awaited_once()

    async def test_setup_idempotent_on_repeated_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """setup() must not be called more than once, even after multiple setup_checkpointer() calls."""
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("FALLBACK_MODELS", raising=False)

        mock_saver = AsyncMock()

        with patch("src.persistence.checkpointer._create_saver", return_value=mock_saver):
            await setup_checkpointer()
            await setup_checkpointer()
            await setup_checkpointer()

        mock_saver.setup.assert_awaited_once()
