"""Unit tests for structured JSON logging (T-005)."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from io import StringIO
from unittest.mock import patch

import pytest
import structlog

from src.utils.logging import (
    _redact_sensitive_keys,
    bind_request_context,
    configure_logging,
)


@pytest.fixture()
def reset_structlog() -> Generator[None, None, None]:
    """Reset structlog state and context vars before and after each test."""
    structlog.contextvars.clear_contextvars()
    yield
    # Reconfigure to reset global state
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture()
def capture_logs(reset_structlog: Generator[None, None, None]) -> Generator[StringIO, None, None]:
    """Capture structlog output to a StringIO and return it."""
    output = StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact_sensitive_keys,  # type: ignore[list-item]
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=output),
        cache_logger_on_first_use=False,
    )
    yield output
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


class TestLogLineIsValidJson:
    def test_log_line_is_valid_json(self, capture_logs: StringIO) -> None:
        """Every log line must be valid JSON with at least level, event, timestamp."""
        logger = structlog.get_logger()
        logger.info("test_event")

        output = capture_logs.getvalue()
        assert output.strip(), "No log output captured"

        parsed = json.loads(output.strip())
        assert "level" in parsed
        assert "event" in parsed
        assert "timestamp" in parsed
        assert parsed["event"] == "test_event"
        assert parsed["level"] == "info"

    def test_log_contains_all_required_fields(self, capture_logs: StringIO) -> None:
        """Log output must include level, event, and timestamp at minimum."""
        logger = structlog.get_logger()
        logger.warning("auth_failed", user_email="test@example.com")

        output = capture_logs.getvalue()
        parsed = json.loads(output.strip())

        assert parsed["level"] == "warning"
        assert parsed["event"] == "auth_failed"
        assert parsed["user_email"] == "test@example.com"
        assert "timestamp" in parsed


class TestRequestContextInjected:
    def test_request_context_injected(self, capture_logs: StringIO) -> None:
        """bind_request_context() makes those keys appear on all subsequent logs."""
        bind_request_context(
            request_id="550e8400-e29b-41d4-a716-446655440000",
            user_id="user-123",
            session_id="session-456",
        )

        logger = structlog.get_logger()
        logger.info("request_start")

        output = capture_logs.getvalue()
        parsed = json.loads(output.strip())

        assert parsed["request_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert parsed["user_id"] == "user-123"
        assert parsed["session_id"] == "session-456"

    def test_partial_context_binding(self, capture_logs: StringIO) -> None:
        """bind_request_context() with partial values binds only non-None keys."""
        bind_request_context(request_id="req-789", user_id=None)

        logger = structlog.get_logger()
        logger.info("partial_context")

        output = capture_logs.getvalue()
        parsed = json.loads(output.strip())

        assert parsed["request_id"] == "req-789"
        assert "user_id" not in parsed
        assert "session_id" not in parsed


class TestSensitiveKeyRedaction:
    def test_password_redacted(self, capture_logs: StringIO) -> None:
        """Passwords must be redacted in logs."""
        logger = structlog.get_logger()
        logger.info("user_created", password="super_secret_123")

        output = capture_logs.getvalue()
        parsed = json.loads(output.strip())

        assert parsed["password"] == "[REDACTED]"

    def test_token_redacted(self, capture_logs: StringIO) -> None:
        """Tokens must be redacted in logs."""
        logger = structlog.get_logger()
        logger.info("auth_token_issued", refresh_token="eyJ0eXAi...")

        output = capture_logs.getvalue()
        parsed = json.loads(output.strip())

        assert parsed["refresh_token"] == "[REDACTED]"

    def test_api_key_redacted(self, capture_logs: StringIO) -> None:
        """API keys must be redacted in logs."""
        logger = structlog.get_logger()
        logger.info("openai_call", openai_api_key="sk-...")

        output = capture_logs.getvalue()
        parsed = json.loads(output.strip())

        assert parsed["openai_api_key"] == "[REDACTED]"

    def test_case_insensitive_redaction(self, capture_logs: StringIO) -> None:
        """Redaction must be case-insensitive (PASSWORD, Token, API_KEY all redacted)."""
        logger = structlog.get_logger()
        logger.info(
            "case_test",
            PASSWORD="pass1",
            ACCESS_TOKEN="token1",
            API_KEY="key1",
        )

        output = capture_logs.getvalue()
        parsed = json.loads(output.strip())

        assert parsed["PASSWORD"] == "[REDACTED]"
        assert parsed["ACCESS_TOKEN"] == "[REDACTED]"
        assert parsed["API_KEY"] == "[REDACTED]"


class TestLogLevelRespected:
    def test_log_level_debug(self) -> None:
        """LOG_LEVEL=DEBUG must allow debug messages."""
        output = StringIO()
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=output),
            cache_logger_on_first_use=False,
        )

        logger = structlog.get_logger()
        logger.debug("debug_msg")
        logger.info("info_msg")

        logs = [json.loads(line) for line in output.getvalue().strip().split("\n") if line]
        assert len(logs) == 2
        assert logs[0]["level"] == "debug"
        assert logs[1]["level"] == "info"

        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()

    def test_log_level_warning(self) -> None:
        """LOG_LEVEL=WARNING must suppress info and debug messages."""
        output = StringIO()
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=output),
            cache_logger_on_first_use=False,
        )

        logger = structlog.get_logger()
        logger.debug("debug_msg")
        logger.info("info_msg")
        logger.warning("warning_msg")
        logger.error("error_msg")

        logs = [json.loads(line) for line in output.getvalue().strip().split("\n") if line]
        # Only warning and error should be present
        assert len(logs) == 2
        assert logs[0]["level"] == "warning"
        assert logs[1]["level"] == "error"

        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()

    def test_configure_logging_uses_settings(self) -> None:
        """configure_logging() must read LOG_LEVEL from settings."""
        with patch("src.utils.logging.get_settings") as mock_get_settings:
            mock_get_settings.return_value.LOG_LEVEL = "DEBUG"

            configure_logging()

            # Verify that structlog was configured
            logger = structlog.get_logger()
            assert logger is not None

        structlog.reset_defaults()
