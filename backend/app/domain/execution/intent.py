"""SPEC-01 §5: OrderIntent, Fill, Position."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.domain.execution.enums import DealType, OrderSide, OrderType, PositionStatus
from app.domain.market.enums import Direction


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str  # ULID. Generated ONCE, before any network call.
    signal_id: UUID
    account_id: UUID
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: Decimal
    limit_price: Decimal | None
    stop_loss: Decimal
    take_profit: Decimal | None  # first TP; ladder is managed post-fill
    max_slippage_points: int
    magic: int  # strategy version fingerprint, see SPEC-06 §7
    comment: str  # first 24 chars of client_order_id
    expires_at: datetime | None
    idempotency_key: str  # = client_order_id, restated for clarity


@dataclass(frozen=True, slots=True)
class Fill:
    broker_deal_id: str
    client_order_id: str
    broker_order_id: str
    broker_position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    price: Decimal
    commission: Decimal
    swap: Decimal
    profit: Decimal
    executed_at: datetime  # broker server time, converted to UTC
    deal_type: DealType


@dataclass(frozen=True, slots=True)
class Position:
    id: UUID
    broker_position_id: str
    account_id: UUID
    symbol: str
    direction: Direction
    volume: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    opened_at: datetime
    status: PositionStatus
    signal_id: UUID | None  # None means ORPHANED
    initial_risk: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    breakeven_moved: bool
    partials_taken: int
