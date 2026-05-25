"""Request logging, sanitisation, timing middleware, and exception handlers (T-036, T-042)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.utils.logging import bind_request_context

logger = structlog.get_logger(__name__)

# HTTP status code → (SCREAMING_SNAKE_CASE code, retryable)
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    401: ("UNAUTHORIZED", False),
    403: ("FORBIDDEN", False),
    404: ("NOT_FOUND", False),
    409: ("CONFLICT", False),
    410: ("GONE", False),
    422: ("VALIDATION_ERROR", False),
    429: ("QUOTA_EXCEEDED", True),
    500: ("INTERNAL_SERVER_ERROR", False),
    503: ("SERVICE_UNAVAILABLE", True),
}


def _error_body(
    code: str,
    message: str,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    """Build the standard four-field error response body (FR-29)."""
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "retry_after_seconds": retry_after_seconds,
        }
    }


class RequestMiddleware(BaseHTTPMiddleware):
    """Starlette middleware: request_id injection, null-byte guard, latency logging.

    Responsibilities (NFR-15, NFR-18, DESIGN §9.6):
    - Generates a UUID v4 ``request_id`` and binds it to the structlog context.
    - Rejects POST/PUT/PATCH bodies containing ``\\x00`` with HTTP 422 before
      the body reaches any route handler.
    - Measures and logs ``latency_ms`` from request start to first response byte.
    - Logs one ``INFO`` line per request with the seven required fields.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the ASGI application."""
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        """Process one HTTP request: sanitise, time, log."""
        start = time.monotonic()
        request_id = str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        bind_request_context(request_id=request_id)

        # Null-byte guard (NFR-15): reject before route handlers
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.body()
                if b"\x00" in body:
                    return JSONResponse(
                        status_code=422,
                        content=_error_body(
                            "INVALID_INPUT",
                            "Request body contains null bytes.",
                        ),
                    )
            except Exception:
                pass  # Body read failure is handled downstream

        response = await call_next(request)

        latency_ms = max(1, int((time.monotonic() - start) * 1000))

        # Auth context may have been stamped on request.state by a dependency
        user_id: str | None = getattr(request.state, "user_id", None)
        session_id: str | None = getattr(request.state, "session_id", None)

        logger.info(
            "request",
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            method=request.method,
            path=str(request.url.path),
            status_code=response.status_code,
            latency_ms=latency_ms,
        )

        return response


# ── T-042: Standardised exception handlers ────────────────────────────────────


def register_exception_handlers(app: FastAPI) -> None:
    """Register standardised JSON error handlers for all HTTP status codes (T-042, FR-29).

    After registration, every non-SSE error returns a body that deserialises to
    ``{"error": {code, message, retryable, retry_after_seconds}}``.
    FastAPI's default 422 validation error is replaced with this schema.
    """
    from fastapi import HTTPException
    from pydantic import ValidationError

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        # Routers may set ``detail`` to a pre-built dict that already has ``code``
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            detail = dict(exc.detail)
            retry_after = detail.pop("retry_after_seconds", None)
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": detail.get("code", "HTTP_ERROR"),
                        "message": detail.get("message", ""),
                        "retryable": detail.get("retryable", False),
                        "retry_after_seconds": retry_after,
                    }
                },
            )

        code, retryable = _STATUS_TO_CODE.get(exc.status_code, ("HTTP_ERROR", False))
        retry_after_seconds: int | None = None
        if exc.status_code in (429, 503):
            try:
                ra = (exc.headers or {}).get("Retry-After", "0")
                retry_after_seconds = int(ra) or None
            except (TypeError, ValueError):
                pass

        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                code,
                str(exc.detail) if exc.detail else code,
                retryable,
                retry_after_seconds,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body("VALIDATION_ERROR", "Request validation failed.", False),
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body("VALIDATION_ERROR", "Request body validation failed.", False),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(
                "INTERNAL_SERVER_ERROR", "An unexpected error occurred.", False
            ),
        )
