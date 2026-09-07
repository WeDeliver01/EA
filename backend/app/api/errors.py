"""The error envelope from SPEC-03 §1, plus the exception -> HTTP mapping."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


class ApiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


def _envelope(
    code: str, message: str, detail: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "detail": detail,
            "correlation_id": correlation_id,
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.detail, correlation_id),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
        logger.error(
            "unhandled_exception",
            error=str(exc),
            correlation_id=correlation_id,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=_envelope("INTERNAL_ERROR", "An unexpected error occurred", {}, correlation_id),
        )
