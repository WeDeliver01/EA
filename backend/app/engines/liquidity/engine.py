"""SPEC-05 §3.3: LiquidityEngine.

Identifies where stops are likely resting: equal highs/lows within a
tolerance band, the key levels handed down from StructureEngine (PDH/PDL/
PWH/PWL/session extremes), and round numbers. A pool is a "sweep candidate"
if the most recent bar's range reached it.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.bar import Bar
from app.domain.market.market_state import MarketState
from app.engines.config import LiquidityConfig
from app.engines.liquidity.types import Liquidity, LiquidityPool
from app.engines.structure.types import Structure

_ROUND_STEP = Decimal("1")  # whole-unit levels; not asset-class aware at MVP scope


def _equal_level_pools(
    bars: tuple[Bar, ...], *, tolerance: Decimal, min_touches: int, side: str
) -> list[LiquidityPool]:
    prices = [b.high for b in bars] if side == "HIGH" else [b.low for b in bars]
    if not prices:
        return []

    pools: list[LiquidityPool] = []
    used = [False] * len(prices)
    for i, price in enumerate(prices):
        if used[i]:
            continue
        group = [price]
        used[i] = True
        for j in range(i + 1, len(prices)):
            if used[j]:
                continue
            if abs(prices[j] - price) <= tolerance:
                group.append(prices[j])
                used[j] = True
        if len(group) >= min_touches:
            avg_price = sum(group, Decimal(0)) / len(group)
            kind = "EQUAL_HIGH" if side == "HIGH" else "EQUAL_LOW"
            significance = min(Decimal(len(group)) / Decimal(5), Decimal(1))
            pools.append(
                LiquidityPool(
                    price=avg_price,
                    kind=kind,
                    side=side,
                    touches=len(group),
                    significance=significance,
                )
            )
    return pools


def _round_number_pools(bars: tuple[Bar, ...]) -> list[LiquidityPool]:
    if not bars:
        return []
    lo = min(b.low for b in bars)
    hi = max(b.high for b in bars)
    pools: list[LiquidityPool] = []
    level = (lo // _ROUND_STEP) * _ROUND_STEP
    while level <= hi:
        if lo <= level <= hi:
            side = "HIGH" if level >= bars[-1].close else "LOW"
            pools.append(
                LiquidityPool(
                    price=level,
                    kind="ROUND_NUMBER",
                    side=side,
                    touches=1,
                    significance=Decimal("0.3"),
                )
            )
        level += _ROUND_STEP
    return pools


class LiquidityEngine:
    def __init__(self, config: LiquidityConfig) -> None:
        self._config = config

    def evaluate(self, state: MarketState, structure: Structure) -> Liquidity:
        cfg = self._config
        primary_bars = state.bars.get(state.primary_tf, ())
        window = primary_bars[-cfg.lookback_bars :]

        primary_atr = _rough_atr(window)
        tolerance = cfg.equal_level_tolerance_atr * primary_atr if primary_atr else Decimal(0)

        pools: list[LiquidityPool] = []
        pools.extend(
            _equal_level_pools(
                window, tolerance=tolerance, min_touches=cfg.min_touches, side="HIGH"
            )
        )
        pools.extend(
            _equal_level_pools(window, tolerance=tolerance, min_touches=cfg.min_touches, side="LOW")
        )

        for level in structure.key_levels:
            side = "HIGH" if level.kind in {"PDH", "PWH", "SESSION_HIGH"} else "LOW"
            pools.append(
                LiquidityPool(
                    price=level.price,
                    kind=level.kind,
                    side=side,
                    touches=1,
                    significance=Decimal("0.6"),
                )
            )

        pools.extend(_round_number_pools(window))

        sweep_candidates: list[LiquidityPool] = []
        if primary_bars:
            last_bar = primary_bars[-1]
            for pool in pools:
                if last_bar.low <= pool.price <= last_bar.high:
                    sweep_candidates.append(pool)

        return Liquidity(pools=tuple(pools), sweep_candidates=tuple(sweep_candidates))


def _rough_atr(bars: tuple[Bar, ...]) -> Decimal:
    """A cheap true-range average for tolerance sizing; ContextEngine owns the
    canonical Wilder ATR used everywhere else in the pipeline."""
    if len(bars) < 2:
        return Decimal(0)
    ranges = [b.range for b in bars[-14:]]
    return sum(ranges, Decimal(0)) / len(ranges) if ranges else Decimal(0)
