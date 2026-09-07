"""SPEC-05 §3.5: SetupDetector. Feeds a hand-built Structure (rather than a
fully engineered bar sequence) so BREAKOUT_RETEST and PULLBACK_CONTINUATION
can be exercised directly without re-deriving fractal swings by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import Direction, Timeframe
from app.engines.config import SetupsConfig
from app.engines.setup.detector import SetupDetector
from app.engines.structure.types import Structure, StructureBreak, SwingPoint
from tests.factories import make_market_state

pytestmark = pytest.mark.unit


def _bars(count: int, *, start: datetime, prices: list[Decimal]) -> tuple[Bar, ...]:
    bars = []
    for i in range(count):
        c = prices[i]
        bars.append(
            Bar(
                symbol="XAUUSD",
                timeframe=Timeframe.M15,
                open_time=start + timedelta(minutes=15 * i),
                open=c,
                high=c + Decimal("0.5"),
                low=c - Decimal("0.5"),
                close=c,
                tick_volume=100,
                real_volume=None,
                spread_points=10,
            )
        )
    return tuple(bars)


def _bar_start(count: int) -> datetime:
    """A start time that places `count` M15 bars ending exactly at the
    default MarketState fixture's `as_of`, so replacing the primary-tf bars
    never trips the lookahead guard against the other timeframes' bars."""
    default_as_of = make_market_state().as_of
    return default_as_of - timedelta(minutes=15 * count)


def _state_with_bars(bars: tuple[Bar, ...]):
    from dataclasses import replace

    state = make_market_state()
    new_bars = dict(state.bars)
    new_bars[state.primary_tf] = bars
    return replace(state, bars=new_bars)


def test_breakout_retest_fires_within_the_configured_window() -> None:
    start = _bar_start(2)
    # Break happens at bar 0 (price jumps above the 3405 level), then the
    # *last* bar pulls back close to the level and closes just above it -
    # only the last bar's proximity to the level is checked (SPEC-05 §3.5:
    # "price has pulled back within retest_tolerance_atr of the level").
    prices = [Decimal("3411"), Decimal("3405.3")]
    bars = _bars(2, start=start, prices=prices)
    state = _state_with_bars(bars)

    structure = Structure(
        swing_highs=(SwingPoint(price=Decimal("3405"), bar_time=start - timedelta(minutes=15)),),
        swing_lows=(SwingPoint(price=Decimal("3395"), bar_time=start - timedelta(minutes=30)),),
        trend_state="MIXED",
        range_high=None,
        range_low=None,
        range_bars=0,
        last_break=StructureBreak(
            direction=Direction.LONG, level=Decimal("3405"), bar_time=start, by_wick=False
        ),
        key_levels=(),
    )
    config = SetupsConfig(
        enabled=("BREAKOUT_RETEST",), retest_max_bars=6, retest_tolerance_atr=Decimal("1")
    )
    detector = SetupDetector(config)

    setup = detector.evaluate(state, structure, atr=Decimal("2"))
    assert setup is not None
    assert setup.kind == "BREAKOUT_RETEST"
    assert setup.direction == Direction.LONG


def test_breakout_retest_does_not_fire_outside_the_window() -> None:
    start = _bar_start(9)
    prices = [Decimal("3411")] + [
        Decimal("3411.5")
    ] * 8  # break, then 8 bars with no retest back to level
    bars = _bars(9, start=start, prices=prices)
    state = _state_with_bars(bars)

    structure = Structure(
        swing_highs=(),
        swing_lows=(),
        trend_state="MIXED",
        range_high=None,
        range_low=None,
        range_bars=0,
        last_break=StructureBreak(
            direction=Direction.LONG, level=Decimal("3405"), bar_time=start, by_wick=False
        ),
        key_levels=(),
    )
    config = SetupsConfig(
        enabled=("BREAKOUT_RETEST",), retest_max_bars=2, retest_tolerance_atr=Decimal("0.1")
    )
    detector = SetupDetector(config)

    setup = detector.evaluate(state, structure, atr=Decimal("2"))
    assert setup is None


def test_breakout_retest_requires_a_positive_atr() -> None:
    start = _bar_start(3)
    bars = _bars(3, start=start, prices=[Decimal("3411"), Decimal("3405.5"), Decimal("3410.5")])
    state = _state_with_bars(bars)
    structure = Structure(
        swing_highs=(),
        swing_lows=(),
        trend_state="MIXED",
        range_high=None,
        range_low=None,
        range_bars=0,
        last_break=StructureBreak(
            direction=Direction.LONG, level=Decimal("3405"), bar_time=start, by_wick=False
        ),
        key_levels=(),
    )
    detector = SetupDetector(SetupsConfig(enabled=("BREAKOUT_RETEST",)))
    assert detector.evaluate(state, structure, atr=Decimal("0")) is None


def test_pullback_continuation_fires_near_the_pivot_in_an_uptrend() -> None:
    start = _bar_start(2)
    bars = _bars(2, start=start, prices=[Decimal("3400"), Decimal("3400.5")])
    state = _state_with_bars(bars)

    structure = Structure(
        swing_highs=(
            SwingPoint(price=Decimal("3395"), bar_time=start - timedelta(minutes=60)),
            SwingPoint(price=Decimal("3410"), bar_time=start - timedelta(minutes=30)),
        ),
        swing_lows=(
            SwingPoint(price=Decimal("3390"), bar_time=start - timedelta(minutes=45)),
            SwingPoint(price=Decimal("3400"), bar_time=start - timedelta(minutes=15)),
        ),
        trend_state="HH_HL",
        range_high=None,
        range_low=None,
        range_bars=0,
        last_break=None,
        key_levels=(),
    )
    detector = SetupDetector(SetupsConfig(enabled=("PULLBACK_CONTINUATION",)))

    setup = detector.evaluate(state, structure, atr=Decimal("2"))
    assert setup is not None
    assert setup.kind == "PULLBACK_CONTINUATION"
    assert setup.direction == Direction.LONG


def test_pullback_continuation_does_not_fire_when_trend_state_is_mixed() -> None:
    start = _bar_start(2)
    bars = _bars(2, start=start, prices=[Decimal("3400"), Decimal("3400.5")])
    state = _state_with_bars(bars)
    structure = Structure(
        swing_highs=(SwingPoint(price=Decimal("3410"), bar_time=start),),
        swing_lows=(SwingPoint(price=Decimal("3400"), bar_time=start),),
        trend_state="MIXED",
        range_high=None,
        range_low=None,
        range_bars=0,
        last_break=None,
        key_levels=(),
    )
    detector = SetupDetector(SetupsConfig(enabled=("PULLBACK_CONTINUATION",)))
    assert detector.evaluate(state, structure, atr=Decimal("2")) is None


def test_setup_detector_respects_priority_order_in_config() -> None:
    """BREAKOUT and BREAKOUT_RETEST can both be satisfiable; whichever is
    listed first in `config.setups.enabled` wins."""
    start = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
    state = make_market_state(as_of=start + timedelta(minutes=15))
    primary_bars = state.bars[state.primary_tf]
    from dataclasses import replace

    breakout_bar = Bar(
        symbol="XAUUSD",
        timeframe=state.primary_tf,
        open_time=state.as_of - timedelta(minutes=15),
        open=Decimal("3400"),
        high=Decimal("3411"),
        low=Decimal("3399"),
        close=Decimal("3410.5"),
        tick_volume=100,
        real_volume=None,
        spread_points=10,
    )
    new_bars = dict(state.bars)
    new_bars[state.primary_tf] = (*primary_bars[:-1], breakout_bar)
    state = replace(state, bars=new_bars)

    structure = Structure(
        swing_highs=(
            SwingPoint(price=Decimal("3405"), bar_time=state.as_of - timedelta(minutes=45)),
        ),
        swing_lows=(),
        trend_state="MIXED",
        range_high=None,
        range_low=None,
        range_bars=0,
        last_break=StructureBreak(
            direction=Direction.LONG,
            level=Decimal("3405"),
            bar_time=breakout_bar.open_time,
            by_wick=False,
        ),
        key_levels=(),
    )
    detector = SetupDetector(SetupsConfig(enabled=("BREAKOUT", "BREAKOUT_RETEST")))
    setup = detector.evaluate(state, structure, atr=Decimal("2"))
    assert setup is not None
    assert setup.kind == "BREAKOUT"


def test_setup_detector_skips_unknown_configured_kinds() -> None:
    detector = SetupDetector(SetupsConfig(enabled=("NOT_A_REAL_KIND",)))
    state = make_market_state()
    structure = Structure(
        swing_highs=(),
        swing_lows=(),
        trend_state="MIXED",
        range_high=None,
        range_low=None,
        range_bars=0,
        last_break=None,
        key_levels=(),
    )
    assert detector.evaluate(state, structure, atr=Decimal("1")) is None
