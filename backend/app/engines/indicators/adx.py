"""Average Directional Index, Wilder smoothing (SPEC-05 §5).

Standard Wilder construction: directional movement and true range are each
Wilder-smoothed over `period`, +DI/-DI are derived from those, DX from the
DI spread, and ADX is DX Wilder-smoothed again.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.bar import Bar
from app.engines.indicators.atr import true_range


def _wilder_smooth(values: list[Decimal], *, period: int) -> list[Decimal | None]:
    n = len(values)
    result: list[Decimal | None] = [None] * n
    if n < period:
        return result
    seed = sum(values[:period], Decimal(0))
    result[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = prev - (prev / period) + values[i]
        result[i] = prev
    return result


def adx(bars: tuple[Bar, ...], *, period: int) -> tuple[Decimal | None, ...]:
    """Wilder ADX. `None` for the warm-up period (needs 2*period bars)."""
    if period < 1:
        raise ValueError("period must be >= 1")

    n = len(bars)
    plus_dm = [Decimal(0)] * n
    minus_dm = [Decimal(0)] * n
    tr = [Decimal(0)] * n

    for i in range(1, n):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else Decimal(0)
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else Decimal(0)
        tr[i] = true_range(bars[i], bars[i - 1].close)

    smoothed_plus_dm = _wilder_smooth(plus_dm, period=period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period=period)
    smoothed_tr = _wilder_smooth(tr, period=period)

    dx: list[Decimal | None] = [None] * n
    for i in range(n):
        s_tr = smoothed_tr[i]
        s_plus = smoothed_plus_dm[i]
        s_minus = smoothed_minus_dm[i]
        if s_tr is None or s_plus is None or s_minus is None or s_tr == 0:
            continue
        plus_di = (s_plus / s_tr) * 100
        minus_di = (s_minus / s_tr) * 100
        di_sum = plus_di + minus_di
        dx[i] = abs(plus_di - minus_di) / di_sum * 100 if di_sum != 0 else Decimal(0)

    # ADX is the Wilder *average* of DX over `period` (not a re-smoothed sum),
    # seeded by the plain mean of the first `period` available DX values.
    result: list[Decimal | None] = [None] * n
    first_dx_index = next((i for i, v in enumerate(dx) if v is not None), None)
    if first_dx_index is None or n - first_dx_index < period:
        return tuple(result)

    seed_start = first_dx_index
    seed_end = first_dx_index + period
    seed_values = dx[seed_start:seed_end]
    if any(v is None for v in seed_values):
        return tuple(result)
    seed = sum((v for v in seed_values if v is not None), Decimal(0)) / period
    result[seed_end - 1] = seed
    prev = seed
    for i in range(seed_end, n):
        current_dx = dx[i]
        if current_dx is None:
            continue
        prev = (prev * (period - 1) + current_dx) / period
        result[i] = prev

    return tuple(result)
