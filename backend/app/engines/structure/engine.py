"""SPEC-05 §3.2: StructureEngine.

Swing detection uses a fractal with a configurable lookback: a swing high at
index i needs `swing_lookback` bars on both sides with a strictly lower high
(mirrored for lows). Confirmed swings only - a swing is not reported until
`swing_lookback` bars after it forms, because using an unconfirmed swing is
lookahead bias (SPEC-05 §3.2).
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.bar import Bar
from app.domain.market.enums import Direction, Timeframe
from app.domain.market.market_state import MarketState
from app.engines.config import StructureConfig
from app.engines.context.sessions import classify_session
from app.engines.structure.types import Level, Structure, StructureBreak, SwingPoint


def _find_swing_highs(bars: tuple[Bar, ...], *, lookback: int) -> tuple[SwingPoint, ...]:
    swings: list[SwingPoint] = []
    n = len(bars)
    for i in range(lookback, n - lookback):
        candidate = bars[i].high
        left = all(bars[j].high < candidate for j in range(i - lookback, i))
        right = all(bars[j].high < candidate for j in range(i + 1, i + lookback + 1))
        if left and right:
            swings.append(SwingPoint(price=candidate, bar_time=bars[i].open_time))
    return tuple(swings)


def _find_swing_lows(bars: tuple[Bar, ...], *, lookback: int) -> tuple[SwingPoint, ...]:
    swings: list[SwingPoint] = []
    n = len(bars)
    for i in range(lookback, n - lookback):
        candidate = bars[i].low
        left = all(bars[j].low > candidate for j in range(i - lookback, i))
        right = all(bars[j].low > candidate for j in range(i + 1, i + lookback + 1))
        if left and right:
            swings.append(SwingPoint(price=candidate, bar_time=bars[i].open_time))
    return tuple(swings)


def _trend_state(swing_highs: tuple[SwingPoint, ...], swing_lows: tuple[SwingPoint, ...]) -> str:
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "MIXED"
    higher_high = swing_highs[-1].price > swing_highs[-2].price
    higher_low = swing_lows[-1].price > swing_lows[-2].price
    lower_high = swing_highs[-1].price < swing_highs[-2].price
    lower_low = swing_lows[-1].price < swing_lows[-2].price
    if higher_high and higher_low:
        return "HH_HL"
    if lower_high and lower_low:
        return "LH_LL"
    return "MIXED"


def _last_break(
    bars: tuple[Bar, ...],
    swing_highs: tuple[SwingPoint, ...],
    swing_lows: tuple[SwingPoint, ...],
    *,
    break_on_wick: bool,
) -> StructureBreak | None:
    if not bars:
        return None
    last_bar = bars[-1]
    candidates: list[StructureBreak] = []

    if swing_highs:
        level = swing_highs[-1].price
        broke = last_bar.high > level if break_on_wick else last_bar.close > level
        if broke:
            candidates.append(
                StructureBreak(
                    direction=Direction.LONG,
                    level=level,
                    bar_time=last_bar.open_time,
                    by_wick=break_on_wick,
                )
            )
    if swing_lows:
        level = swing_lows[-1].price
        broke = last_bar.low < level if break_on_wick else last_bar.close < level
        if broke:
            candidates.append(
                StructureBreak(
                    direction=Direction.SHORT,
                    level=level,
                    bar_time=last_bar.open_time,
                    by_wick=break_on_wick,
                )
            )
    if not candidates:
        return None
    # Both directions rarely break on the same bar; if they do, the larger
    # displacement from the level is the more meaningful break.
    return max(candidates, key=lambda c: abs(last_bar.close - c.level))


class StructureEngine:
    def __init__(self, config: StructureConfig) -> None:
        self._config = config

    def evaluate(self, state: MarketState) -> Structure:
        cfg = self._config
        primary_bars = state.bars.get(state.primary_tf, ())

        swing_highs = _find_swing_highs(primary_bars, lookback=cfg.swing_lookback)
        swing_lows = _find_swing_lows(primary_bars, lookback=cfg.swing_lookback)
        trend_state = _trend_state(swing_highs, swing_lows)

        range_high: Decimal | None = None
        range_low: Decimal | None = None
        range_bars = 0
        if trend_state == "MIXED" and len(primary_bars) >= cfg.range_min_bars:
            window = primary_bars[-cfg.range_min_bars :]
            range_high = max(b.high for b in window)
            range_low = min(b.low for b in window)
            range_bars = cfg.range_min_bars

        last_break = _last_break(
            primary_bars, swing_highs, swing_lows, break_on_wick=cfg.break_on_wick
        )

        key_levels = self._compute_key_levels(state)

        return Structure(
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            trend_state=trend_state,
            range_high=range_high,
            range_low=range_low,
            range_bars=range_bars,
            last_break=last_break,
            key_levels=key_levels,
        )

    def _compute_key_levels(self, state: MarketState) -> tuple[Level, ...]:
        cfg = self._config
        levels: list[Level] = []
        d1_bars = state.bars.get(Timeframe.D1, ())
        w1_bars = state.bars.get(Timeframe.W1, ())
        primary_bars = state.bars.get(state.primary_tf, ())

        if "PDH" in cfg.key_levels and d1_bars:
            levels.append(Level(kind="PDH", price=d1_bars[-1].high))
        if "PDL" in cfg.key_levels and d1_bars:
            levels.append(Level(kind="PDL", price=d1_bars[-1].low))
        if "PWH" in cfg.key_levels and w1_bars:
            levels.append(Level(kind="PWH", price=w1_bars[-1].high))
        if "PWL" in cfg.key_levels and w1_bars:
            levels.append(Level(kind="PWL", price=w1_bars[-1].low))

        if ("SESSION_HIGH" in cfg.key_levels or "SESSION_LOW" in cfg.key_levels) and primary_bars:
            current_session = state.session.value
            session_bars: list[Bar] = []
            for bar in reversed(primary_bars):
                if classify_session(bar.open_time).value != current_session:
                    break
                session_bars.append(bar)
            if session_bars:
                if "SESSION_HIGH" in cfg.key_levels:
                    levels.append(
                        Level(kind="SESSION_HIGH", price=max(b.high for b in session_bars))
                    )
                if "SESSION_LOW" in cfg.key_levels:
                    levels.append(Level(kind="SESSION_LOW", price=min(b.low for b in session_bars)))

        return tuple(levels)
