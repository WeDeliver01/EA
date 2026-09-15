"""SPEC-03 §8: gate telemetry - answers "why has the bot not traded in N
days" in one request, surfaced prominently in the terminal per spec.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter

from app.api.v1.deps import CurrentUser, DbSession
from app.repositories.analysis_runs import AnalysisRunRepository

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

_DEFAULT_WINDOW = timedelta(days=7)


@router.get("/gate-rejections")
async def gate_rejections(
    user: CurrentUser,
    session: DbSession,
    account_id: UUID | None = None,
    instrument_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    window_end = until or datetime.now(UTC)
    window_start = since or (window_end - _DEFAULT_WINDOW)

    summary = await AnalysisRunRepository(session).compute_gate_rejections(
        account_id=account_id, instrument_id=instrument_id, since=window_start, until=window_end
    )
    total = summary.total_evaluations
    return {
        "period": {"from": summary.since.isoformat(), "to": summary.until.isoformat()},
        "total_evaluations": total,
        "trades": summary.trades,
        "rejections": [
            {
                "code": r.code,
                "count": r.count,
                "pct": f"{(r.count / total):.3f}" if total > 0 else "0.000",
            }
            for r in summary.rejections
        ],
    }
