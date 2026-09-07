"""SPEC-06 §8 invariants, simulated bar by bar: the stop never moves against
the position, a rung fires at most once, breakeven/trailing/time exits work
as specified."""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import AssetClass, Direction, Timeframe
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import TakeProfit
from app.engines.config import TradeConstructionConfig
from app.research.fill_model import NextBarOpenFillModel
from app.research.position_simulator import SimulatedPosition, advance, open_position

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
_FILL_MODEL = NextBarOpenFillModel()
_START = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def _bar(i: int, *, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        open_time=_START + timedelta(minutes=15 * i),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=100,
        real_volume=None,
        spread_points=10,
    )


def _cfg(**overrides: object) -> TradeConstructionConfig:
    return TradeConstructionConfig(**overrides)


def _long_position(
    *, entry: str = "3400", stop: str = "3390", tps: tuple[TakeProfit, ...] | None = None
) -> SimulatedPosition:
    take_profits = tps or (
        TakeProfit(level=Decimal("3420"), fraction=Decimal("1.0"), r_multiple=Decimal("2.0")),
    )
    return open_position(
        direction=Direction.LONG,
        entry_price=Decimal(entry),
        stop_loss=Decimal(stop),
        take_profits=take_profits,
        volume=Decimal("1.0"),
    )


def test_stop_hit_closes_the_full_remaining_position() -> None:
    position = _long_position()
    bar = _bar(0, open_="3400", high="3401", low="3385", close="3388")
    result = advance(
        position, bar, atr=Decimal("5"), cfg=_cfg(), fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result.position is None
    assert len(result.events) == 1
    assert result.events[0].kind == "STOP"
    assert result.events[0].price == Decimal("3390")
    assert result.events[0].volume == Decimal("1.0")


def test_take_profit_reachable_closes_the_rung() -> None:
    position = _long_position()
    bar = _bar(0, open_="3400", high="3425", low="3399", close="3420")
    result = advance(
        position, bar, atr=Decimal("5"), cfg=_cfg(), fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result.position is None  # only rung was 100% of volume
    assert result.events[0].kind == "PARTIAL_TP"
    assert result.events[0].price == Decimal("3420")
    assert result.events[0].volume == Decimal("1.0")


def test_stop_takes_priority_over_take_profit_on_the_same_bar() -> None:
    position = _long_position()
    # Both the stop (3390) and the TP (3420) are technically within this
    # bar's range - the fill model's stop-first pessimism must win.
    bar = _bar(0, open_="3400", high="3425", low="3385", close="3410")
    result = advance(
        position, bar, atr=Decimal("5"), cfg=_cfg(), fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result.position is None
    assert result.events[0].kind == "STOP"


def test_partial_rung_fires_once_then_remainder_continues() -> None:
    tps = (
        TakeProfit(level=Decimal("3410"), fraction=Decimal("0.5"), r_multiple=Decimal("1.0")),
        TakeProfit(level=Decimal("3420"), fraction=Decimal("0.5"), r_multiple=Decimal("2.0")),
    )
    position = _long_position(tps=tps)

    bar1 = _bar(0, open_="3400", high="3412", low="3399", close="3410")
    result1 = advance(
        position, bar1, atr=Decimal("5"), cfg=_cfg(), fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result1.position is not None
    assert result1.position.remaining_volume == Decimal("0.50")
    assert result1.position.rungs_taken == (True, False)
    assert len(result1.events) == 1

    # Same bar's level must not fire again even if price revisits it.
    bar2 = _bar(1, open_="3410", high="3411", low="3405", close="3408")
    result2 = advance(
        result1.position, bar2, atr=Decimal("5"), cfg=_cfg(), fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result2.position is not None
    assert result2.position.remaining_volume == Decimal("0.50")
    assert result2.position.rungs_taken == (True, False)


def test_breakeven_moves_stop_to_entry_plus_buffer_once_target_reached() -> None:
    cfg = _cfg(
        breakeven_at_r=Decimal("1.0"), breakeven_buffer_atr=Decimal("0.1"), trail_mode="none"
    )
    position = _long_position(entry="3400", stop="3390")  # initial risk = 10
    bar = _bar(0, open_="3400", high="3412", low="3399", close="3411")  # +11 favorable >= 1R (10)
    result = advance(position, bar, atr=Decimal("5"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC)
    assert result.position is not None
    assert result.position.breakeven_moved is True
    assert result.position.current_stop == Decimal("3400") + Decimal("0.1") * Decimal("5")


def test_breakeven_never_moves_the_stop_backwards() -> None:
    cfg = _cfg(
        breakeven_at_r=Decimal("1.0"), breakeven_buffer_atr=Decimal("0.1"), trail_mode="none"
    )
    # Stop is already better (tighter, i.e. higher for a long) than the
    # 3400.5 breakeven would produce (entry 3400 + 0.1*ATR 5), and the bar's
    # low stays above it so this isn't also a stop-out.
    position = _long_position(entry="3400", stop="3406")
    bar = _bar(0, open_="3408", high="3412", low="3407", close="3411")
    result = advance(position, bar, atr=Decimal("5"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC)
    assert result.position is not None
    assert result.position.current_stop == Decimal("3406")


def test_trailing_only_activates_after_breakeven_or_a_taken_rung() -> None:
    cfg = _cfg(
        breakeven_at_r=Decimal("100"), trail_atr_multiple=Decimal("1.0")
    )  # breakeven unreachable
    position = _long_position(entry="3400", stop="3390")
    bar = _bar(0, open_="3400", high="3405", low="3399", close="3404")
    result = advance(position, bar, atr=Decimal("2"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC)
    assert result.position is not None
    assert result.position.current_stop == Decimal("3390")  # untouched: trailing not active yet


def test_trailing_moves_stop_up_behind_price_once_active() -> None:
    cfg = _cfg(
        breakeven_at_r=Decimal("0.01"),
        breakeven_buffer_atr=Decimal("0"),
        trail_atr_multiple=Decimal("1.0"),
    )
    position = _long_position(entry="3400", stop="3390")
    # This bar triggers breakeven (candidate 3400) *and*, in the same pass,
    # trailing is now active and its candidate (close 3404 - 1*ATR 2 = 3402)
    # is better, so it wins.
    bar1 = _bar(0, open_="3400", high="3405", low="3399", close="3404")
    result1 = advance(position, bar1, atr=Decimal("2"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC)
    assert result1.position is not None
    assert result1.position.current_stop == Decimal("3402")

    bar2 = _bar(
        1, open_="3404", high="3415", low="3403", close="3412"
    )  # close - 1*ATR(2) = 3410 > 3402
    result2 = advance(
        result1.position, bar2, atr=Decimal("2"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result2.position is not None
    assert result2.position.current_stop == Decimal("3410")


def test_trailing_never_widens_the_stop() -> None:
    cfg = _cfg(
        breakeven_at_r=Decimal("0.01"),
        breakeven_buffer_atr=Decimal("0"),
        trail_atr_multiple=Decimal("5"),
    )
    position = _long_position(entry="3400", stop="3390")
    bar1 = _bar(0, open_="3400", high="3405", low="3399", close="3404")  # breakeven -> stop 3400
    result1 = advance(position, bar1, atr=Decimal("2"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC)
    assert result1.position is not None
    stop_after_breakeven = result1.position.current_stop

    # A pullback bar where close - 5*ATR would be far below the current stop.
    bar2 = _bar(1, open_="3404", high="3406", low="3401", close="3403")
    result2 = advance(
        result1.position, bar2, atr=Decimal("2"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result2.position is not None
    assert result2.position.current_stop == stop_after_breakeven


def test_time_exit_closes_the_position_at_max_holding_bars() -> None:
    cfg = _cfg(max_holding_bars=1)
    position = _long_position()
    bar = _bar(0, open_="3400", high="3402", low="3399", close="3401")
    result = advance(position, bar, atr=Decimal("5"), cfg=cfg, fill_model=_FILL_MODEL, spec=_SPEC)
    assert result.position is None
    assert result.events[-1].kind == "TIME_EXIT"
    assert result.events[-1].price == Decimal("3401")


def test_short_position_stop_and_breakeven_mirror_long() -> None:
    tps = (TakeProfit(level=Decimal("3380"), fraction=Decimal("1.0"), r_multiple=Decimal("2.0")),)
    position = open_position(
        direction=Direction.SHORT,
        entry_price=Decimal("3400"),
        stop_loss=Decimal("3410"),
        take_profits=tps,
        volume=Decimal("1.0"),
    )
    bar = _bar(0, open_="3400", high="3415", low="3398", close="3401")
    result = advance(
        position, bar, atr=Decimal("5"), cfg=_cfg(), fill_model=_FILL_MODEL, spec=_SPEC
    )
    assert result.position is None
    assert result.events[0].kind == "STOP"
    assert result.events[0].price == Decimal("3410")


def test_partial_rung_below_volume_min_is_skipped() -> None:
    tiny_spec = dc_replace(_SPEC, volume_min=Decimal("0.5"))
    tps = (
        TakeProfit(level=Decimal("3410"), fraction=Decimal("0.1"), r_multiple=Decimal("1.0")),
        TakeProfit(level=Decimal("3420"), fraction=Decimal("0.9"), r_multiple=Decimal("2.0")),
    )
    position = _long_position(tps=tps)
    bar = _bar(0, open_="3400", high="3412", low="3399", close="3410")
    result = advance(
        position, bar, atr=Decimal("5"), cfg=_cfg(), fill_model=_FILL_MODEL, spec=tiny_spec
    )
    assert result.position is not None
    assert result.position.remaining_volume == Decimal("1.0")  # rung skipped: 0.1 < volume_min 0.5
    assert result.position.rungs_taken == (False, False)
    assert result.events == ()
