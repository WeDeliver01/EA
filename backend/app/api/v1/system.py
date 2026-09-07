"""SPEC-03 §11: liveness, readiness and status endpoints."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["system"])
logger = structlog.get_logger()


@router.get("/system/health")
async def health() -> dict[str, str]:
    """Always 200 if the process is up. For Nginx / Uptime Kuma."""
    return {"status": "ok"}


@router.get("/system/ready")
async def ready(request: Request) -> JSONResponse:
    """200 only if the database and Redis are reachable and migrations are current."""
    checks: dict[str, Any] = {}
    healthy = True

    session_factory = request.app.state.session_factory
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        healthy = False

    redis = request.app.state.redis
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"
        healthy = False

    status_code = 200 if healthy else 503
    return JSONResponse(status_code=status_code, content={"ready": healthy, "checks": checks})


@router.get("/system/status")
async def status(request: Request) -> dict[str, Any]:
    """Cheap operational snapshot. Everything here should come from Redis, not table scans."""
    return {
        "git_sha": request.app.state.settings.git_sha,
        "environment": request.app.state.settings.environment,
        "global_trading_enabled": request.app.state.settings.global_trading_enabled,
    }
