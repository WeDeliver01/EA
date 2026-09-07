"""SPEC-06 §8: position management.

Evaluated per current price rather than per historical bar - there is no
live quote stream in this MVP (see docs/adr/0001-mvp-scope.md), so callers
supply the latest known price and ATR the same way
`research/backtester.py` supplies them per bar. `decide()` is a pure
function (given a position snapshot and a price, what should happen) so it
is unit-testable without a database or a broker; `PositionManager` is the
thin, DB/broker-touching orchestration around it.

One important difference from backtesting: a live broker-side stop order
executes itself. This module never decides "the stop was hit" - that fact
arrives as a deal via reconciliation (SPEC-06 §6), the same path a manual
close or a broker-side stop-out does. This module only ever *moves* the
stop, takes profit rungs, and time-exits - all closes it initiates go
through the same `close_position` command a human-initiated close would.

The full take-profit ladder isn't in `positions.take_profit` (singular -
"ladder is managed post-fill", `OrderIntent`'s own comment) - it lives on
the originating `Signal.take_profits`, so `PositionManager` reads it from
there, not from the position row.

Not implemented (documented deviation): structural invalidation (needs the
StructureEngine re-run against live context, SPEC-06 §8 step 7) and the
emergency-spread-widen check (needs a live spread reading). Both need
infrastructure this MVP doesn't have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.domain.execution.enums import OrderSide
from app.domain.execution.intent import Fill
from app.domain.market.enums import Direction
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import TakeProfit
from app.engines.config import TradeConstructionConfig
from app.engines.risk.sizing import floor_to_step
from app.execution.broker import RETCODE_DONE, ModifyResult, SimulatedBroker
from app.execution.event_consumer import EventConsumer
from app.repositories.accounts import AccountRepository
from app.repositories.positions import PositionRepository


def _sign(direction: Direction) -> Decimal:
    return Decimal(1) if direction == Direction.LONG else Decimal(-1)


def _improves(direction: Direction, current: Decimal, candidate: Decimal) -> bool:
    return candidate > current if direction == Direction.LONG else candidate < current


@dataclass(frozen=True, slots=True)
class LivePosition:
    id: UUID
    broker_position_id: str
    account_id: UUID
    instrument_id: UUID
    direction: Direction
    initial_volume: Decimal
    remaining_volume: Decimal
    entry_price: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    take_profits: tuple[TakeProfit, ...]
    partials_taken: int
    breakeven_moved: bool
    opened_at: datetime

    @property
    def initial_risk_price(self) -> Decimal:
        return abs(self.entry_price - self.initial_stop)


@dataclass(frozen=True, slots=True)
class ManagementAction:
    kind: str  # "none" | "modify_stop" | "close_partial" | "close_full"
    new_stop: Decimal | None = None
    sets_breakeven: bool = False  # True only for the breakeven move itself, not a later trail
    close_volume: Decimal | None = None
    close_price: Decimal | None = None
    close_reason: str | None = None
    rung_index: int | None = None


def decide(
    position: LivePosition,
    *,
    current_price: Decimal,
    atr: Decimal,
    cfg: TradeConstructionConfig,
    spec: SymbolSpec,
    as_of: datetime,
    max_holding_duration: timedelta,
) -> ManagementAction:
    sign = _sign(position.direction)

    for index, tp in enumerate(position.take_profits):
        if index < position.partials_taken:
            continue
        reachable = current_price >= tp.level if sign > 0 else current_price <= tp.level
        if not reachable:
            break  # rungs fire in order; a later rung can't be reached before an earlier one
        close_volume = floor_to_step(position.initial_volume * tp.fraction, spec.volume_step)
        close_volume = min(close_volume, position.remaining_volume)
        if close_volume < spec.volume_min:
            continue  # too small to execute alone; folds into whatever closes it next
        return ManagementAction(
            kind="close_partial",
            close_volume=close_volume,
            close_price=tp.level,
            close_reason="PARTIAL_TP",
            rung_index=index,
        )

    if not position.breakeven_moved:
        favorable_move = (current_price - position.entry_price) * sign
        if favorable_move >= cfg.breakeven_at_r * position.initial_risk_price:
            candidate = position.entry_price + sign * cfg.breakeven_buffer_atr * atr
            if _improves(position.direction, position.current_stop, candidate):
                return ManagementAction(kind="modify_stop", new_stop=candidate, sets_breakeven=True)

    trailing_active = position.breakeven_moved or position.partials_taken > 0
    if trailing_active and cfg.trail_mode not in ("", "none"):
        candidate = current_price - sign * cfg.trail_atr_multiple * atr
        if _improves(position.direction, position.current_stop, candidate):
            return ManagementAction(kind="modify_stop", new_stop=candidate)

    if as_of - position.opened_at >= max_holding_duration:
        return ManagementAction(
            kind="close_full",
            close_volume=position.remaining_volume,
            close_price=current_price,
            close_reason="TIME_EXIT",
        )

    return ManagementAction(kind="none")


class PositionManager:
    def __init__(
        self,
        *,
        broker: SimulatedBroker,
        event_consumer: EventConsumer,
        position_repo: PositionRepository,
        account_repo: AccountRepository,
    ) -> None:
        self._broker = broker
        self._event_consumer = event_consumer
        self._position_repo = position_repo
        self._account_repo = account_repo

    async def apply(
        self, position: LivePosition, action: ManagementAction, *, spec: SymbolSpec, at: datetime
    ) -> None:
        if action.kind == "none":
            return

        if action.kind == "modify_stop":
            assert action.new_stop is not None
            result: ModifyResult = self._broker.modify_position(
                position.broker_position_id, stop_loss=action.new_stop
            )
            if result.retcode != RETCODE_DONE:
                return  # broker refused; nothing local changes until reconciliation sees why
            await self._position_repo.apply_stop_update(
                position.id,
                new_stop=action.new_stop,
                breakeven_moved=action.sets_breakeven,
                updated_at=at,
            )
            return

        assert action.close_volume is not None
        assert action.close_price is not None
        close_fill = self._broker.close_position(
            position.broker_position_id,
            price=action.close_price,
            at=at,
            volume=action.close_volume,
        )
        if close_fill is None:
            return  # broker has no record of this position; reconciliation's job to notice

        exit_side = OrderSide.SELL if position.direction == Direction.LONG else OrderSide.BUY
        fill = Fill(
            broker_deal_id=close_fill.broker_deal_id,
            client_order_id="",
            broker_order_id=position.broker_position_id,
            broker_position_id=position.broker_position_id,
            symbol=spec.symbol,
            side=exit_side,
            volume=close_fill.volume,
            price=close_fill.price,
            commission=Decimal(0),
            swap=Decimal(0),
            profit=close_fill.profit,
            executed_at=at,
            deal_type=close_fill.deal_type,
        )
        await self._event_consumer.record_deal(
            fill,
            account_id=position.account_id,
            instrument_id=position.instrument_id,
            trade_intent_id=None,
        )
        if close_fill.profit != 0:
            await self._account_repo.apply_realised_pnl(
                position.account_id, net_pnl=close_fill.profit, at=at
            )
        if action.rung_index is not None:
            await self._position_repo.increment_partials_taken(position.id, updated_at=at)
