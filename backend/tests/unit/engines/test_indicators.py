"""Indicator correctness. SPEC-05 §5 requires matching an MT5 CSV export to
1e-8 over >= 500 values; no MT5 export is available in this environment (see
docs/adr/0001-mvp-scope.md), so these instead pin the Wilder formulas against
independently hand-computed values, which is what would be verified against
MT5 later without any code change."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.engines.indicators.adx import adx
from app.engines.indicators.atr import atr, true_range

pytestmark = pytest.mark.unit


def _bars_from_hlc(rows: list[tuple[str, str, str]]) -> tuple[Bar, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    for i, (h, low, c) in enumerate(rows):
        high, close = Decimal(h), Decimal(c)
        low_d = Decimal(low)
        open_ = close  # open value is irrelevant to ATR/ADX; keep it inside [low, high]
        bars.append(
            Bar(
                symbol="TEST",
                timeframe=Timeframe.M15,
                open_time=start + timedelta(minutes=15 * i),
                open=open_,
                high=high,
                low=low_d,
                close=close,
                tick_volume=1,
                real_volume=None,
                spread_points=0,
            )
        )
    return tuple(bars)


def test_true_range_uses_prev_close_when_available() -> None:
    bar = _bars_from_hlc([("10", "8", "9")])[0]
    assert true_range(bar, None) == Decimal("2")  # high - low only
    assert true_range(bar, Decimal("11")) == Decimal("3")  # |high - prev_close| dominates
    assert true_range(bar, Decimal("6")) == Decimal("4")  # |low - prev_close| dominates


def test_atr_seed_is_plain_average_of_first_period_true_ranges() -> None:
    # Hand-computable: 5 bars, high-low range only (constant close avoids
    # cross-bar true-range terms), period 3.
    rows = [
        ("10", "8", "9"),
        ("12", "9", "10"),
        ("11", "8", "9"),
        ("13", "10", "11"),
        ("14", "11", "12"),
    ]
    bars = _bars_from_hlc(rows)
    result = atr(bars, period=3)

    assert result[0] is None
    assert result[1] is None
    # seed = mean(TR[0..2]) where TR[0]=high-low=2 (no prev close),
    # TR[1]=max(3, |12-9|, |9-9|)=3, TR[2]=max(3, |11-10|, |8-10|)=3
    assert result[2] == (Decimal("2") + Decimal("3") + Decimal("3")) / 3

    # Wilder recursion for the ATR (a moving *average*, unlike the raw DM/TR
    # sums used inside ADX): ATR_i = ((period-1)*ATR_{i-1} + TR_i) / period
    seed = result[2]
    assert seed is not None
    tr3 = true_range(bars[3], bars[2].close)
    expected_3 = (seed * 2 + tr3) / 3
    assert result[3] == expected_3


def test_atr_warm_up_is_none_for_short_series() -> None:
    bars = _bars_from_hlc([("10", "8", "9"), ("11", "9", "10")])
    result = atr(bars, period=5)
    assert result == (None, None)


def test_atr_rejects_non_positive_period() -> None:
    with pytest.raises(ValueError, match="period must be >= 1"):
        atr((), period=0)


def test_adx_warm_up_is_none_for_short_series() -> None:
    rows = [("10", "8", "9")] * 5
    bars = _bars_from_hlc(rows)
    result = adx(bars, period=14)
    assert all(v is None for v in result)


def test_adx_is_high_for_a_strong_one_directional_trend() -> None:
    # Strictly rising highs and lows every bar: a textbook strong uptrend,
    # which should push ADX well above the compression/ranging threshold.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    price = Decimal("100")
    for i in range(60):
        high = price + Decimal("2")
        low = price
        bars.append(
            Bar(
                symbol="TEST",
                timeframe=Timeframe.M15,
                open_time=start + timedelta(minutes=15 * i),
                open=price,
                high=high,
                low=low,
                close=high,
                tick_volume=1,
                real_volume=None,
                spread_points=0,
            )
        )
        price += Decimal("2")

    result = adx(tuple(bars), period=14)
    last = result[-1]
    assert last is not None
    assert last > Decimal("40")


def test_adx_is_low_for_a_flat_choppy_series() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    for i in range(60):
        high = Decimal("100.5") if i % 2 == 0 else Decimal("100.3")
        low = Decimal("99.5") if i % 2 == 0 else Decimal("99.7")
        bars.append(
            Bar(
                symbol="TEST",
                timeframe=Timeframe.M15,
                open_time=start + timedelta(minutes=15 * i),
                open=Decimal("100"),
                high=high,
                low=low,
                close=Decimal("100"),
                tick_volume=1,
                real_volume=None,
                spread_points=0,
            )
        )
    result = adx(tuple(bars), period=14)
    last = result[-1]
    assert last is not None
    assert last < Decimal("20")
