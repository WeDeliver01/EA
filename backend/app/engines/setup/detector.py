"""SPEC-05 §3.5: SetupDetector.

Emits at most one Setup per evaluation. If multiple kinds qualify, the
highest-priority kind by `config.setups.enabled` order wins.

`fingerprint` is `sha256(f"{symbol}|{kind}|{direction}|{round(reference_level,
digits)}|{structure_break_bar_time}")[:16]`, used downstream to suppress
firing on the same setup repeatedly as price oscillates around a level.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from app.domain.market.enums import Direction
from app.domain.market.market_state import MarketState
from app.domain.strategy.setup import Setup
from app.engines.config import SetupsConfig
from app.engines.structure.types import Structure


def _fingerprint(
    *,
    symbol: str,
    kind: str,
    direction: Direction,
    reference_level: Decimal,
    digits: int,
    bar_time: str,
) -> str:
    payload = f"{symbol}|{kind}|{direction.value}|{round(reference_level, digits)}|{bar_time}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _invalidation_for(direction: Direction, structure: Structure, fallback: Decimal) -> Decimal:
    if direction == Direction.LONG:
        return structure.swing_lows[-1].price if structure.swing_lows else fallback
    return structure.swing_highs[-1].price if structure.swing_highs else fallback


def _detect_breakout(state: MarketState, structure: Structure) -> Setup | None:
    last_break = structure.last_break
    if last_break is None:
        return None
    primary_bars = state.bars.get(state.primary_tf, ())
    if not primary_bars or last_break.bar_time != primary_bars[-1].open_time:
        return None  # only fresh, this-bar breaks count as a BREAKOUT setup

    invalidation = _invalidation_for(last_break.direction, structure, fallback=last_break.level)
    fingerprint = _fingerprint(
        symbol=state.symbol,
        kind="BREAKOUT",
        direction=last_break.direction,
        reference_level=last_break.level,
        digits=state.spec.digits,
        bar_time=last_break.bar_time.isoformat(),
    )
    return Setup(
        kind="BREAKOUT",
        direction=last_break.direction,
        trigger_price=primary_bars[-1].close,
        invalidation_price=invalidation,
        reference_level=last_break.level,
        fingerprint=fingerprint,
    )


def _detect_breakout_retest(
    state: MarketState, structure: Structure, cfg: SetupsConfig, *, atr: Decimal
) -> Setup | None:
    last_break = structure.last_break
    if last_break is None or atr <= 0:
        return None
    primary_bars = state.bars.get(state.primary_tf, ())
    if not primary_bars:
        return None

    break_index = next(
        (i for i, b in enumerate(primary_bars) if b.open_time == last_break.bar_time), None
    )
    if break_index is None:
        return None
    bars_since_break = len(primary_bars) - 1 - break_index
    if not (0 < bars_since_break <= cfg.retest_max_bars):
        return None

    last_bar = primary_bars[-1]
    tolerance = cfg.retest_tolerance_atr * atr
    retested = abs(last_bar.close - last_break.level) <= tolerance or (
        last_bar.low <= last_break.level <= last_bar.high
    )
    if not retested:
        return None

    # Rejection: the retest bar closes back in the breakout's direction.
    confirmed = (
        last_bar.close > last_break.level
        if last_break.direction == Direction.LONG
        else last_bar.close < last_break.level
    )
    if not confirmed:
        return None

    invalidation = _invalidation_for(last_break.direction, structure, fallback=last_break.level)
    fingerprint = _fingerprint(
        symbol=state.symbol,
        kind="BREAKOUT_RETEST",
        direction=last_break.direction,
        reference_level=last_break.level,
        digits=state.spec.digits,
        bar_time=last_break.bar_time.isoformat(),
    )
    return Setup(
        kind="BREAKOUT_RETEST",
        direction=last_break.direction,
        trigger_price=last_bar.close,
        invalidation_price=invalidation,
        reference_level=last_break.level,
        fingerprint=fingerprint,
    )


def _detect_pullback_continuation(
    state: MarketState, structure: Structure, *, atr: Decimal
) -> Setup | None:
    if structure.trend_state not in {"HH_HL", "LH_LL"} or atr <= 0:
        return None
    primary_bars = state.bars.get(state.primary_tf, ())
    if not primary_bars or not structure.swing_highs or not structure.swing_lows:
        return None

    direction = Direction.LONG if structure.trend_state == "HH_HL" else Direction.SHORT
    pivot = structure.swing_lows[-1] if direction == Direction.LONG else structure.swing_highs[-1]
    last_bar = primary_bars[-1]

    near_pivot = abs(last_bar.close - pivot.price) <= atr
    continuing = (
        last_bar.close > pivot.price
        if direction == Direction.LONG
        else last_bar.close < pivot.price
    )
    if not (near_pivot and continuing):
        return None

    invalidation = _invalidation_for(direction, structure, fallback=pivot.price)
    fingerprint = _fingerprint(
        symbol=state.symbol,
        kind="PULLBACK_CONTINUATION",
        direction=direction,
        reference_level=pivot.price,
        digits=state.spec.digits,
        bar_time=pivot.bar_time.isoformat(),
    )
    return Setup(
        kind="PULLBACK_CONTINUATION",
        direction=direction,
        trigger_price=last_bar.close,
        invalidation_price=invalidation,
        reference_level=pivot.price,
        fingerprint=fingerprint,
    )


_DETECTORS = {
    "BREAKOUT": lambda state, structure, cfg, atr: _detect_breakout(state, structure),
    "BREAKOUT_RETEST": lambda state, structure, cfg, atr: _detect_breakout_retest(
        state, structure, cfg, atr=atr
    ),
    "PULLBACK_CONTINUATION": lambda state, structure, cfg, atr: _detect_pullback_continuation(
        state, structure, atr=atr
    ),
}


class SetupDetector:
    def __init__(self, config: SetupsConfig) -> None:
        self._config = config

    def evaluate(self, state: MarketState, structure: Structure, *, atr: Decimal) -> Setup | None:
        for kind in self._config.enabled:
            detector = _DETECTORS.get(kind)
            if detector is None:
                continue
            setup = detector(state, structure, self._config, atr)
            if setup is not None:
                return setup
        return None
