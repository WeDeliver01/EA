"""SPEC-03 §7: the decision journal. Every evaluation the strategy engine
has ever made, WAIT included (P3) - this is the "why has the bot not
traded" view, not just a trade log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.v1.deps import CurrentUser, DbSession
from app.repositories.analysis_runs import AnalysisRunRepository

router = APIRouter(prefix="/analysis-runs", tags=["analysis"])


@router.get("")
async def list_analysis_runs(
    user: CurrentUser,
    session: DbSession,
    account_id: UUID | None = None,
    instrument_id: UUID | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    runs = await AnalysisRunRepository(session).list_runs(
        account_id=account_id,
        instrument_id=instrument_id,
        outcome=outcome,
        since=since,
        until=until,
        limit=limit,
    )
    return [
        {
            "id": str(r.id),
            "account_id": str(r.account_id),
            "instrument_id": str(r.instrument_id),
            "symbol": r.symbol,
            "timeframe": r.timeframe,
            "as_of": r.as_of.isoformat(),
            "mode": r.mode,
            "regime": r.regime,
            "outcome": r.outcome,
            "confluence_score": str(r.confluence_score),
            "confluence_band": r.confluence_band,
            "created_at": r.created_at.isoformat(),
        }
        for r in runs
    ]


@router.get("/{analysis_run_id}")
async def get_analysis_run(
    analysis_run_id: UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    detail = await AnalysisRunRepository(session).get_detail(analysis_run_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="analysis run not found")
    return {
        "id": str(detail.id),
        "account_id": str(detail.account_id),
        "instrument_id": str(detail.instrument_id),
        "symbol": detail.symbol,
        "strategy_version_id": str(detail.strategy_version_id),
        "timeframe": detail.timeframe,
        "as_of": detail.as_of.isoformat(),
        "mode": detail.mode,
        "regime": detail.regime,
        "outcome": detail.outcome,
        "confluence_score": str(detail.confluence_score),
        "confluence_band": detail.confluence_band,
        "direction": detail.direction,
        "entry": str(detail.entry) if detail.entry is not None else None,
        "stop_loss": str(detail.stop_loss) if detail.stop_loss is not None else None,
        "narrative": detail.narrative,
        "engine_duration_ms": detail.engine_duration_ms,
        "market_snapshot": detail.market_snapshot,
        "created_at": detail.created_at.isoformat(),
        "evidence": [
            {
                "type": e.type,
                "direction": e.direction,
                "present": e.present,
                "weight": str(e.weight),
                "score": str(e.score),
                "detail": e.detail,
            }
            for e in detail.evidence
        ],
        "gates": [{"code": g.code, "passed": g.passed, "detail": g.detail} for g in detail.gates],
    }
