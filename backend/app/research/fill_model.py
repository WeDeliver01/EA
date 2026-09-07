"""SPEC-07 §4: fill models.

Three implementations behind one interface, selected per run.

Deviation from spec: the `tick` fill model ("replays ticks within each bar
... required for any run that supports a promotion decision") needs real
tick data, which this environment has none of - there is no broker
connection anywhere in this repository (see docs/adr/0001-mvp-scope.md).
`TickApproximationFillModel` here is a documented stand-in that behaves like
`PessimisticFillModel` (stop-first on any bar where both stop and TP are
reachable, since without a tick sequence there is no way to know which was
actually touched first) - it must not be used for an actual promotion
decision until it's replaced with a real tick replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from app.domain.market.bar import Bar
from app.domain.market.enums import Direction
from app.domain.market.symbol_spec import SymbolSpec
from app.research.cost_model import CostModel, slippage_price, spread_cost_price

ExitReason = Literal["STOP", "TAKE_PROFIT"]


def _sign(direction: Direction) -> Decimal:
    return Decimal(1) if direction == Direction.LONG else Decimal(-1)


class FillModel(Protocol):
    @property
    def name(self) -> str: ...

    def resolve_entry(
        self, *, direction: Direction, next_bar: Bar, spec: SymbolSpec, cost_model: CostModel
    ) -> Decimal: ...

    def resolve_exit(
        self,
        *,
        direction: Direction,
        bar: Bar,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> tuple[Decimal, ExitReason] | None: ...


def _reachable(bar: Bar, level: Decimal) -> bool:
    return bar.low <= level <= bar.high


def _stop_first_resolution(
    *, direction: Direction, bar: Bar, stop_loss: Decimal, take_profit: Decimal | None
) -> tuple[Decimal, ExitReason] | None:
    """If the bar's range contains both the stop and the TP, assume the stop
    hit first (SPEC-07 §4) - the pessimistic assumption for both
    `next_bar_open` and `pessimistic`, since bar data alone can't say which
    was actually touched first."""
    stop_reachable = _reachable(bar, stop_loss)
    tp_reachable = take_profit is not None and _reachable(bar, take_profit)

    if stop_reachable:
        return stop_loss, "STOP"
    if tp_reachable:
        assert take_profit is not None
        return take_profit, "TAKE_PROFIT"
    return None


@dataclass(frozen=True, slots=True)
class NextBarOpenFillModel:
    """Fast, for parameter sweeps. Never for a promotion decision."""

    name: str = "next_bar_open"

    def resolve_entry(
        self, *, direction: Direction, next_bar: Bar, spec: SymbolSpec, cost_model: CostModel
    ) -> Decimal:
        sign = _sign(direction)
        spread = spread_cost_price(spec, cost_model, recorded_spread_points=next_bar.spread_points)
        slippage = slippage_price(spec, cost_model)
        return next_bar.open + sign * (spread + slippage)

    def resolve_exit(
        self,
        *,
        direction: Direction,
        bar: Bar,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> tuple[Decimal, ExitReason] | None:
        return _stop_first_resolution(
            direction=direction, bar=bar, stop_loss=stop_loss, take_profit=take_profit
        )


@dataclass(frozen=True, slots=True)
class PessimisticFillModel:
    """Stress test. Entry at the bar's adverse extreme, full spread, and
    slippage. If the strategy only works under `next_bar_open`, it doesn't
    have an edge, it has a fill assumption (SPEC-07 §4)."""

    name: str = "pessimistic"

    def resolve_entry(
        self, *, direction: Direction, next_bar: Bar, spec: SymbolSpec, cost_model: CostModel
    ) -> Decimal:
        sign = _sign(direction)
        adverse_extreme = next_bar.high if direction == Direction.LONG else next_bar.low
        spread = (
            spread_cost_price(spec, cost_model, recorded_spread_points=next_bar.spread_points) * 2
        )
        slippage = slippage_price(spec, cost_model)
        return adverse_extreme + sign * (spread + slippage)

    def resolve_exit(
        self,
        *,
        direction: Direction,
        bar: Bar,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> tuple[Decimal, ExitReason] | None:
        return _stop_first_resolution(
            direction=direction, bar=bar, stop_loss=stop_loss, take_profit=take_profit
        )


@dataclass(frozen=True, slots=True)
class TickApproximationFillModel:
    """Stand-in for the spec's tick-replay fill model - see module
    docstring. Behaves like `PessimisticFillModel` on entry and exit."""

    name: str = "tick"
    _delegate: PessimisticFillModel = field(default_factory=PessimisticFillModel)

    def resolve_entry(
        self, *, direction: Direction, next_bar: Bar, spec: SymbolSpec, cost_model: CostModel
    ) -> Decimal:
        return self._delegate.resolve_entry(
            direction=direction, next_bar=next_bar, spec=spec, cost_model=cost_model
        )

    def resolve_exit(
        self,
        *,
        direction: Direction,
        bar: Bar,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> tuple[Decimal, ExitReason] | None:
        return self._delegate.resolve_exit(
            direction=direction, bar=bar, stop_loss=stop_loss, take_profit=take_profit
        )


FILL_MODELS: dict[str, FillModel] = {
    "next_bar_open": NextBarOpenFillModel(),
    "pessimistic": PessimisticFillModel(),
    "tick": TickApproximationFillModel(),
}
