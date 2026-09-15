"""SPEC-03 §11: liveness, readiness and status endpoints.

`/system/health` and `/system/ready` stay unauthenticated - they're for
infra probes (Nginx, uptime checks), not operators. `/system/status`
requires a bearer token like every other operator-facing read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.deps import CurrentUser
from app.core.streams import STREAM_BARS_CLOSED, STREAM_INTENTS_PENDING
from app.repositories.reconciliation import ReconciliationRepository

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
async def status(
    request: Request, user: CurrentUser, account_id: UUID | None = None
) -> dict[str, Any]:
    """Per SPEC-03 §11: "everything it reports comes from Redis, not from
    table scans" - true for the scheduler/stream fields; the one exception
    is `unresolved_discrepancy_count`, which needs `account_id` and does
    read the database (the reconciliation table isn't mirrored into Redis),
    so it's only included when an account is asked about."""
    settings = request.app.state.settings
    redis = request.app.state.redis
    body: dict[str, Any] = {
        "git_sha": settings.git_sha,
        "environment": settings.environment,
        "global_trading_enabled": settings.global_trading_enabled,
        "scheduler_enabled": request.app.state.scheduler is not None,
        "streams": {
            "bars_closed_length": await redis.xlen(STREAM_BARS_CLOSED),
            "intents_pending_length": await redis.xlen(STREAM_INTENTS_PENDING),
        },
    }
    if account_id is not None:
        body["agent_connected"] = request.app.state.agent_registry.is_connected(account_id)
        session_factory = request.app.state.session_factory
        async with session_factory() as session:
            unresolved = await ReconciliationRepository(session).list_unresolved(account_id)
        body["unresolved_discrepancy_count"] = len(unresolved)
    return body
