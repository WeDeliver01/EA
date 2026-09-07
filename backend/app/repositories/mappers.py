"""ORM row <-> domain object conversions.

Nothing outside `app.repositories` should import `app.models` directly
(SPEC-10 Phase 1: "Repositories return domain objects, never ORM
instances").
"""

from __future__ import annotations

from app.domain.execution.enums import DealType, OrderSide, PositionStatus
from app.domain.execution.intent import Fill, Position
from app.domain.market.enums import Direction
from app.models.tables import Deal, PositionRow


def position_row_to_domain(row: PositionRow, *, symbol: str) -> Position:
    return Position(
        id=row.id,
        broker_position_id=row.broker_position_id,
        account_id=row.account_id,
        symbol=symbol,
        direction=Direction(row.direction),
        volume=row.volume,
        entry_price=row.entry_price,
        stop_loss=row.stop_loss,
        take_profit=row.take_profit,
        opened_at=row.opened_at,
        status=PositionStatus(row.status),
        signal_id=row.signal_id,
        initial_risk=row.initial_risk if row.initial_risk is not None else row.entry_price * 0,
        realised_pnl=row.realised_pnl,
        unrealised_pnl=row.unrealised_pnl,
        breakeven_moved=row.breakeven_moved,
        partials_taken=row.partials_taken,
    )


def deal_row_to_fill(row: Deal, *, symbol: str) -> Fill:
    return Fill(
        broker_deal_id=row.broker_deal_id,
        client_order_id=row.trade_intent.client_order_id if row.trade_intent is not None else "",
        broker_order_id=row.broker_order_id or "",
        broker_position_id=row.broker_position_id,
        symbol=symbol,
        side=OrderSide(row.side),
        volume=row.volume,
        price=row.price,
        commission=row.commission,
        swap=row.swap,
        profit=row.profit,
        executed_at=row.executed_at,
        deal_type=DealType(row.deal_type),
    )
