"""SPEC-03 §7: positions - open and closed, plus a position's linked deals
on the detail view."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.v1.deps import CurrentUser, DbSession
from app.domain.execution.intent import Position
from app.repositories.deals import DealRepository
from app.repositories.positions import PositionRepository

router = APIRouter(prefix="/positions", tags=["positions"])


def _position_json(p: Position) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "broker_position_id": p.broker_position_id,
        "account_id": str(p.account_id),
        "symbol": p.symbol,
        "direction": p.direction.value,
        "volume": str(p.volume),
        "entry_price": str(p.entry_price),
        "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
        "take_profit": str(p.take_profit) if p.take_profit is not None else None,
        "opened_at": p.opened_at.isoformat(),
        "status": p.status.value,
        "signal_id": str(p.signal_id) if p.signal_id is not None else None,
        "initial_risk": str(p.initial_risk),
        "realised_pnl": str(p.realised_pnl),
        "unrealised_pnl": str(p.unrealised_pnl),
        "breakeven_moved": p.breakeven_moved,
        "partials_taken": p.partials_taken,
    }


@router.get("")
async def list_positions(
    user: CurrentUser,
    session: DbSession,
    account_id: UUID,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[dict[str, Any]]:
    positions = await PositionRepository(session).list_positions(account_id, status=status_filter)
    return [_position_json(p) for p in positions]


@router.get("/{position_id}")
async def get_position(position_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    position = await PositionRepository(session).get_by_id(position_id)
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="position not found")
    deals = await DealRepository(session).list_for_position(
        position.account_id, position.broker_position_id
    )
    body = _position_json(position)
    body["deals"] = [
        {
            "broker_deal_id": d.broker_deal_id,
            "side": d.side.value,
            "volume": str(d.volume),
            "price": str(d.price),
            "commission": str(d.commission),
            "swap": str(d.swap),
            "profit": str(d.profit),
            "executed_at": d.executed_at.isoformat(),
            "deal_type": d.deal_type.value,
        }
        for d in deals
    ]
    return body
