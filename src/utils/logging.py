"""Structured JSON logging with context injection and sensitive key redaction (T-005)."""

from __future__ import annotations

import logging
from typing import Any

import structlog

from src.config.settings import get_settings


def _redact_sensitive_keys(logger: Any, name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact passwords, tokens, and API keys from log output.

    Strips values for any key containing 'password', 'token', or 'api_key' (case-insensitive).
    Returns the modified event dict.
    """
    sensitive_patterns = ("password", "token", "api_key")
    for key in list(event_dict.keys()):
        if any(pattern in key.lower() for pattern in sensitive_patterns):
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging() -> None:
    """Configure structlog with JSON output, context injection, and log level from settings.

    Must be called once at application startup before any logging occurs.
    Sets log level from settings.LOG_LEVEL; defaults to INFO.
    """
    settings = get_settings()
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Configure Python's standard logging to use structlog
    logging.basicConfig(
        format="%(message)s",
        stream=None,  # Use structlog's factory instead
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact_sensitive_keys,  # type: ignore[list-item]
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def bind_request_context(
    request_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Bind request context variables to the async task context.

    These values will be auto-injected into all log lines emitted within the same
    async task via structlog's contextvars mechanism.
    """
    context = {}
    if request_id is not None:
        context["request_id"] = request_id
    if user_id is not None:
        context["user_id"] = user_id
    if session_id is not None:
        context["session_id"] = session_id

    if context:
        structlog.contextvars.bind_contextvars(**context)
