"""SPEC-03 §7: signals - the `TRADE` outcomes among the decision journal,
plus whatever execution has happened to them so far (the linked intent, if
one exists)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.v1.deps import CurrentUser, DbSession
from app.domain.market.enums import Direction
from app.repositories.signals import SignalRepository

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("")
async def list_signals(
    user: CurrentUser,
    session: DbSession,
    account_id: UUID | None = None,
    direction: Direction | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    signals = await SignalRepository(session).list_signals(
        account_id=account_id, direction=direction, since=since, until=until, limit=limit
    )
    return [
        {
            "id": str(s.id),
            "account_id": str(s.account_id),
            "instrument_id": str(s.instrument_id),
            "symbol": s.symbol,
            "direction": s.direction.value,
            "entry": str(s.entry),
            "stop_loss": str(s.stop_loss),
            "confluence_score": str(s.confluence_score),
            "created_at": s.created_at.isoformat(),
        }
        for s in signals
    ]


@router.get("/{signal_id}")
async def get_signal(signal_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    detail = await SignalRepository(session).get_detail(signal_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="signal not found")
    return {
        "id": str(detail.id),
        "account_id": str(detail.account_id),
        "instrument_id": str(detail.instrument_id),
        "symbol": detail.symbol,
        "strategy_version_id": str(detail.strategy_version_id),
        "direction": detail.direction.value,
        "setup_kind": detail.setup_kind,
        "entry": str(detail.entry),
        "stop_loss": str(detail.stop_loss),
        "take_profits": [
            {"level": str(tp.level), "fraction": str(tp.fraction), "r_multiple": str(tp.r_multiple)}
            for tp in detail.take_profits
        ],
        "confluence_score": str(detail.confluence_score),
        "created_at": detail.created_at.isoformat(),
        "trade_intent_id": str(detail.trade_intent_id) if detail.trade_intent_id else None,
        "trade_intent_state": detail.trade_intent_state,
    }
