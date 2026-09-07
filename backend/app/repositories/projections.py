"""Rebuilds `positions` and `trades` from `deals` alone (SPEC-02 §6, P5).

`deals` is append-only and is the financial source of truth. `positions` and
`trades` are projections: this module computes them from a `deals` history so
that a corrupted or lost projection can always be regenerated exactly.

Scope for this MVP: one round trip per `broker_position_id` (one or more
ENTRY deals opening it, then EXIT/PARTIAL_EXIT deals closing it). Re-entry
pyramiding after a full close under the same `broker_position_id` is not
modelled - brokers don't reuse position ids across separate trades, so this
is not a real-world limitation, only a documented simplification.

Kept pure (plain dataclasses in, plain dataclasses out) so the reconstruction
logic can be tested without a database; the repository-facing wrapper in
`app.repositories.deals` does the I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.domain.execution.enums import DealType, OrderSide, PositionStatus
from app.domain.market.enums import Direction

_ENTRY_TYPES = frozenset({DealType.ENTRY})
_EXIT_TYPES = frozenset({DealType.EXIT, DealType.PARTIAL_EXIT})


@dataclass(frozen=True, slots=True)
class DealRecord:
    id: UUID
    broker_deal_id: str
    account_id: UUID
    instrument_id: UUID
    trade_intent_id: UUID | None
    broker_position_id: str
    deal_type: DealType
    side: OrderSide
    volume: Decimal
    price: Decimal
    commission: Decimal
    swap: Decimal
    profit: Decimal
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class PositionProjection:
    broker_position_id: str
    account_id: UUID
    instrument_id: UUID
    trade_intent_id: UUID | None
    direction: Direction
    status: PositionStatus
    volume: Decimal
    initial_volume: Decimal
    entry_price: Decimal
    realised_pnl: Decimal
    opened_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TradeProjection:
    broker_position_id: str
    account_id: UUID
    instrument_id: UUID
    trade_intent_id: UUID | None
    direction: Direction
    entry_time: datetime
    exit_time: datetime
    holding_seconds: int
    entry_price: Decimal
    exit_price: Decimal
    volume: Decimal
    gross_pnl: Decimal
    commission: Decimal
    swap: Decimal
    net_pnl: Decimal


def _direction_for(side: OrderSide) -> Direction:
    return Direction.LONG if side == OrderSide.BUY else Direction.SHORT


def rebuild_projections_from_deals(
    deals: list[DealRecord],
) -> tuple[list[PositionProjection], list[TradeProjection]]:
    by_position: dict[str, list[DealRecord]] = {}
    for deal in deals:
        by_position.setdefault(deal.broker_position_id, []).append(deal)

    positions: list[PositionProjection] = []
    trades: list[TradeProjection] = []

    for broker_position_id, position_deals in by_position.items():
        ordered = sorted(position_deals, key=lambda d: (d.executed_at, d.id))
        entries = [d for d in ordered if d.deal_type in _ENTRY_TYPES]
        exits = [d for d in ordered if d.deal_type in _EXIT_TYPES]
        if not entries:
            continue  # SWAP/COMMISSION/CORRECTION-only rows carry no position

        first_entry = entries[0]
        account_id = first_entry.account_id
        instrument_id = first_entry.instrument_id
        trade_intent_id = first_entry.trade_intent_id
        direction = _direction_for(first_entry.side)

        entry_volume = sum((d.volume for d in entries), Decimal(0))
        entry_notional = sum((d.volume * d.price for d in entries), Decimal(0))
        entry_price = entry_notional / entry_volume if entry_volume else Decimal(0)

        exit_volume = sum((d.volume for d in exits), Decimal(0))
        remaining_volume = entry_volume - exit_volume

        realised_pnl = sum((d.profit for d in position_deals), Decimal(0))
        opened_at = first_entry.executed_at
        closed_at = exits[-1].executed_at if remaining_volume <= 0 and exits else None
        status = PositionStatus.CLOSED if remaining_volume <= 0 else PositionStatus.OPEN

        positions.append(
            PositionProjection(
                broker_position_id=broker_position_id,
                account_id=account_id,
                instrument_id=instrument_id,
                trade_intent_id=trade_intent_id,
                direction=direction,
                status=status,
                volume=max(remaining_volume, Decimal(0)),
                initial_volume=entry_volume,
                entry_price=entry_price,
                realised_pnl=realised_pnl,
                opened_at=opened_at,
                closed_at=closed_at,
            )
        )

        if status is PositionStatus.CLOSED:
            exit_notional = sum((d.volume * d.price for d in exits), Decimal(0))
            exit_price = exit_notional / exit_volume if exit_volume else Decimal(0)
            gross_pnl = sum((d.profit for d in position_deals), Decimal(0))
            commission = sum((d.commission for d in position_deals), Decimal(0))
            swap = sum((d.swap for d in position_deals), Decimal(0))
            net_pnl = gross_pnl + commission + swap
            exit_time = exits[-1].executed_at

            trades.append(
                TradeProjection(
                    broker_position_id=broker_position_id,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    trade_intent_id=trade_intent_id,
                    direction=direction,
                    entry_time=opened_at,
                    exit_time=exit_time,
                    holding_seconds=int((exit_time - opened_at).total_seconds()),
                    entry_price=entry_price,
                    exit_price=exit_price,
                    volume=entry_volume,
                    gross_pnl=gross_pnl,
                    commission=commission,
                    swap=swap,
                    net_pnl=net_pnl,
                )
            )

    return positions, trades
