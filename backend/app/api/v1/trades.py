"""SPEC-03 §7: closed trade history."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.v1.deps import CurrentUser, DbSession
from app.repositories.trades import TradeRepository

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("")
async def list_trades(
    user: CurrentUser,
    session: DbSession,
    account_id: UUID | None = None,
    strategy_version_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    trades = await TradeRepository(session).list_trades(
        account_id=account_id,
        strategy_version_id=strategy_version_id,
        since=since,
        until=until,
        limit=limit,
    )
    return [
        {
            "id": str(t.id),
            "account_id": str(t.account_id),
            "instrument_id": str(t.instrument_id),
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat(),
            "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price),
            "volume": str(t.volume),
            "net_pnl": str(t.net_pnl),
            "r_multiple": str(t.r_multiple),
            "exit_reason": t.exit_reason,
        }
        for t in trades
    ]


@router.get("/{trade_id}")
async def get_trade(trade_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    trade = await TradeRepository(session).get_trade(trade_id)
    if trade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="trade not found")
    return {
        "id": str(trade.id),
        "account_id": str(trade.account_id),
        "instrument_id": str(trade.instrument_id),
        "symbol": trade.symbol,
        "position_id": str(trade.position_id),
        "signal_id": str(trade.signal_id) if trade.signal_id is not None else None,
        "strategy_version_id": (
            str(trade.strategy_version_id) if trade.strategy_version_id is not None else None
        ),
        "direction": trade.direction,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "holding_seconds": trade.holding_seconds,
        "entry_price": str(trade.entry_price),
        "exit_price": str(trade.exit_price),
        "volume": str(trade.volume),
        "gross_pnl": str(trade.gross_pnl),
        "commission": str(trade.commission),
        "swap": str(trade.swap),
        "net_pnl": str(trade.net_pnl),
        "risk_amount": str(trade.risk_amount),
        "r_multiple": str(trade.r_multiple),
        "mae": str(trade.mae) if trade.mae is not None else None,
        "mfe": str(trade.mfe) if trade.mfe is not None else None,
        "exit_reason": trade.exit_reason,
    }
