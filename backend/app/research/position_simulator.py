"""SPEC-06 §8: position management, simulated bar by bar for the backtester.

Bar-level simplifications, documented rather than silent:
  - Breakeven and trailing triggers are evaluated against the bar's *close*,
    not its intrabar extremes - only the final stop/take-profit resolution
    (`fill_model.resolve_exit`) uses the bar's high/low. Using extremes for
    every management trigger without real tick data would be a second,
    uncontrolled source of optimism on top of the fill model's own
    assumptions.
  - If the stop is reachable within a bar, it is assumed hit before any
    partial take-profit in the same bar (consistent with SPEC-07 §4's
    "assume stop hit first" rule for both non-tick fill models) - partial
    rungs are only evaluated on a bar where the stop was not reached.
  - A scheduled max-holding-bars exit fills at that bar's close. A real
    system would place the order at the next bar's open; for backtesting
    purposes the one-bar difference is immaterial next to the fill model's
    other approximations.
  - Structural invalidation (SPEC-06 §8 step 7) is not simulated - it needs
    StructureEngine re-run against the position's evolving context every
    bar, which is a real feature, not a simplification, and is deferred
    (see docs/adr/0001-mvp-scope.md).

Two invariants enforced exactly as specified: the stop never moves against
the position, and a rung fires at most once.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from app.domain.market.bar import Bar
from app.domain.market.enums import Direction
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import TakeProfit
from app.engines.config import TradeConstructionConfig
from app.engines.risk.sizing import floor_to_step
from app.research.fill_model import FillModel


def _sign(direction: Direction) -> Decimal:
    return Decimal(1) if direction == Direction.LONG else Decimal(-1)


def _improves(direction: Direction, current: Decimal, candidate: Decimal) -> bool:
    """True if `candidate` reduces risk relative to `current` - stricter for
    a long (must be higher), for a short (must be lower). Never widens."""
    return candidate > current if direction == Direction.LONG else candidate < current


@dataclass(frozen=True, slots=True)
class SimulatedPosition:
    direction: Direction
    entry_price: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    take_profits: tuple[TakeProfit, ...]
    rungs_taken: tuple[bool, ...]
    initial_volume: Decimal
    remaining_volume: Decimal
    breakeven_moved: bool
    bars_held: int

    @property
    def initial_risk_price(self) -> Decimal:
        return abs(self.entry_price - self.initial_stop)


@dataclass(frozen=True, slots=True)
class ExitEvent:
    kind: str  # "PARTIAL_TP" | "STOP" | "TAKE_PROFIT" | "TIME_EXIT"
    price: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class StepResult:
    position: SimulatedPosition | None  # None once fully closed
    events: tuple[ExitEvent, ...]


def open_position(
    *,
    direction: Direction,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[TakeProfit, ...],
    volume: Decimal,
) -> SimulatedPosition:
    return SimulatedPosition(
        direction=direction,
        entry_price=entry_price,
        initial_stop=stop_loss,
        current_stop=stop_loss,
        take_profits=take_profits,
        rungs_taken=tuple(False for _ in take_profits),
        initial_volume=volume,
        remaining_volume=volume,
        breakeven_moved=False,
        bars_held=0,
    )


def advance(
    position: SimulatedPosition,
    bar: Bar,
    *,
    atr: Decimal,
    cfg: TradeConstructionConfig,
    fill_model: FillModel,
    spec: SymbolSpec,
) -> StepResult:
    sign = _sign(position.direction)
    position = replace(position, bars_held=position.bars_held + 1)
    events: list[ExitEvent] = []

    stop_or_tp = fill_model.resolve_exit(
        direction=position.direction, bar=bar, stop_loss=position.current_stop, take_profit=None
    )
    if stop_or_tp is not None and stop_or_tp[1] == "STOP":
        price, _ = stop_or_tp
        events.append(ExitEvent(kind="STOP", price=price, volume=position.remaining_volume))
        return StepResult(position=None, events=tuple(events))

    for index, (tp, taken) in enumerate(
        zip(position.take_profits, position.rungs_taken, strict=True)
    ):
        if taken:
            continue
        reachable = bar.low <= tp.level <= bar.high
        if not reachable:
            continue
        close_volume = floor_to_step(position.initial_volume * tp.fraction, spec.volume_step)
        close_volume = min(close_volume, position.remaining_volume)
        if close_volume < spec.volume_min:
            continue  # too small to execute; folds into whatever closes the position next
        remaining = position.remaining_volume - close_volume
        rungs_taken = tuple(True if i == index else t for i, t in enumerate(position.rungs_taken))
        events.append(ExitEvent(kind="PARTIAL_TP", price=tp.level, volume=close_volume))
        position = replace(position, remaining_volume=remaining, rungs_taken=rungs_taken)
        if remaining <= 0:
            return StepResult(position=None, events=tuple(events))

    if not position.breakeven_moved:
        favorable_move = (bar.close - position.entry_price) * sign
        if favorable_move >= cfg.breakeven_at_r * position.initial_risk_price:
            candidate = position.entry_price + sign * cfg.breakeven_buffer_atr * atr
            if _improves(position.direction, position.current_stop, candidate):
                position = replace(position, current_stop=candidate, breakeven_moved=True)

    trailing_active = position.breakeven_moved or any(position.rungs_taken)
    if trailing_active and cfg.trail_mode not in ("", "none"):
        candidate = bar.close - sign * cfg.trail_atr_multiple * atr
        if _improves(position.direction, position.current_stop, candidate):
            position = replace(position, current_stop=candidate)

    if position.bars_held >= cfg.max_holding_bars:
        events.append(
            ExitEvent(kind="TIME_EXIT", price=bar.close, volume=position.remaining_volume)
        )
        return StepResult(position=None, events=tuple(events))

    return StepResult(position=position, events=tuple(events))
