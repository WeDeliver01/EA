from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import AssetClass, Direction, Timeframe
from app.domain.market.symbol_spec import SymbolSpec
from app.research.cost_model import CostModel
from app.research.fill_model import (
    NextBarOpenFillModel,
    PessimisticFillModel,
    TickApproximationFillModel,
)

pytestmark = pytest.mark.unit

_SPEC = SymbolSpec(
    symbol="XAUUSD",
    asset_class=AssetClass.METAL,
    digits=2,
    point=Decimal("0.01"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("1.00"),
    contract_size=Decimal("100"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("50"),
    volume_step=Decimal("0.01"),
    stops_level_points=50,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)

_COST_MODEL = CostModel(
    spread_source="fixed",
    fixed_spread_points=20,
    commission_per_lot_per_side=Decimal("3.5"),
    swap_long_points=Decimal("-5"),
    swap_short_points=Decimal("-2"),
    triple_swap_weekday=2,
    slippage_model="fixed",
    slippage_points=5,
)


def _bar(*, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        open_time=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=100,
        real_volume=None,
        spread_points=20,
    )


class TestNextBarOpenFillModel:
    def test_long_entry_pays_half_spread_and_slippage_above_open(self) -> None:
        model = NextBarOpenFillModel()
        bar = _bar(open_="3400", high="3401", low="3399", close="3400.5")
        price = model.resolve_entry(
            direction=Direction.LONG, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        # half spread = 20*0.01/2 = 0.10, slippage = 5*0.01 = 0.05
        assert price == Decimal("3400") + Decimal("0.10") + Decimal("0.05")

    def test_short_entry_subtracts_half_spread_and_slippage_below_open(self) -> None:
        model = NextBarOpenFillModel()
        bar = _bar(open_="3400", high="3401", low="3399", close="3400.5")
        price = model.resolve_entry(
            direction=Direction.SHORT, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        assert price == Decimal("3400") - Decimal("0.10") - Decimal("0.05")

    def test_stop_hit_first_when_both_stop_and_tp_are_in_range(self) -> None:
        model = NextBarOpenFillModel()
        bar = _bar(open_="3400", high="3410", low="3390", close="3405")
        result = model.resolve_exit(
            direction=Direction.LONG,
            bar=bar,
            stop_loss=Decimal("3395"),
            take_profit=Decimal("3408"),
        )
        assert result == (Decimal("3395"), "STOP")

    def test_take_profit_hit_when_stop_not_reachable(self) -> None:
        model = NextBarOpenFillModel()
        bar = _bar(open_="3400", high="3410", low="3398", close="3405")
        result = model.resolve_exit(
            direction=Direction.LONG,
            bar=bar,
            stop_loss=Decimal("3390"),
            take_profit=Decimal("3408"),
        )
        assert result == (Decimal("3408"), "TAKE_PROFIT")

    def test_no_exit_when_neither_level_reachable(self) -> None:
        model = NextBarOpenFillModel()
        bar = _bar(open_="3400", high="3402", low="3399", close="3401")
        result = model.resolve_exit(
            direction=Direction.LONG,
            bar=bar,
            stop_loss=Decimal("3390"),
            take_profit=Decimal("3420"),
        )
        assert result is None

    def test_no_exit_when_take_profit_is_none_and_stop_not_reachable(self) -> None:
        model = NextBarOpenFillModel()
        bar = _bar(open_="3400", high="3402", low="3399", close="3401")
        result = model.resolve_exit(
            direction=Direction.LONG, bar=bar, stop_loss=Decimal("3390"), take_profit=None
        )
        assert result is None


class TestPessimisticFillModel:
    def test_long_entry_fills_at_the_bars_high_plus_full_spread_and_slippage(self) -> None:
        model = PessimisticFillModel()
        bar = _bar(open_="3400", high="3403", low="3399", close="3401")
        price = model.resolve_entry(
            direction=Direction.LONG, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        # full spread = 20*0.01 = 0.20, slippage = 0.05
        assert price == Decimal("3403") + Decimal("0.20") + Decimal("0.05")

    def test_short_entry_fills_at_the_bars_low_minus_full_spread_and_slippage(self) -> None:
        model = PessimisticFillModel()
        bar = _bar(open_="3400", high="3403", low="3399", close="3401")
        price = model.resolve_entry(
            direction=Direction.SHORT, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        assert price == Decimal("3399") - Decimal("0.20") - Decimal("0.05")

    def test_more_expensive_than_next_bar_open_on_the_same_bar(self) -> None:
        bar = _bar(open_="3400", high="3403", low="3399", close="3401")
        pessimistic_price = PessimisticFillModel().resolve_entry(
            direction=Direction.LONG, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        next_open_price = NextBarOpenFillModel().resolve_entry(
            direction=Direction.LONG, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        assert pessimistic_price > next_open_price


class TestTickApproximationFillModel:
    def test_delegates_to_pessimistic_behaviour(self) -> None:
        bar = _bar(open_="3400", high="3403", low="3399", close="3401")
        tick_price = TickApproximationFillModel().resolve_entry(
            direction=Direction.LONG, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        pessimistic_price = PessimisticFillModel().resolve_entry(
            direction=Direction.LONG, next_bar=bar, spec=_SPEC, cost_model=_COST_MODEL
        )
        assert tick_price == pessimistic_price
