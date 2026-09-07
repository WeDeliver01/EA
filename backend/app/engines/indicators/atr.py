"""Average True Range, Wilder smoothing (SPEC-05 §5).

Wilder's smoothing is not a simple or exponential moving average: the first
value is a plain average of the first `period` true ranges, and every value
after that is `prev - prev/period + current`. Using a plain EMA here is the
single most common way an engine's ATR silently diverges from MT5's.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.bar import Bar


def true_range(bar: Bar, prev_close: Decimal | None) -> Decimal:
    if prev_close is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def atr(bars: tuple[Bar, ...], *, period: int) -> tuple[Decimal | None, ...]:
    """Wilder ATR. `None` for the warm-up period (indices 0..period-1)."""
    if period < 1:
        raise ValueError("period must be >= 1")

    n = len(bars)
    trs: list[Decimal] = [Decimal(0)] * n
    prev_close: Decimal | None = None
    for i, bar in enumerate(bars):
        trs[i] = true_range(bar, prev_close)
        prev_close = bar.close

    result: list[Decimal | None] = [None] * n
    if n < period:
        return tuple(result)

    seed = sum(trs[:period], Decimal(0)) / period
    result[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        result[i] = prev

    return tuple(result)
