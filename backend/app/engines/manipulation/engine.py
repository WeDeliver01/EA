"""SPEC-05 §3.4: ManipulationEngine, the market-maker-method core.

Detects the sweep-and-reject sequence: price trades beyond a liquidity pool
(the sweep), closes back inside within N bars (the rejection), then displaces
in the opposite direction (the confirmation). Scores a quality in [0, 1] from
penetration depth, bars spent beyond the level, rejection body ratio,
displacement magnitude and session relevance.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.bar import Bar
from app.domain.market.enums import Direction
from app.domain.market.market_state import MarketState
from app.engines.config import ManipulationConfig
from app.engines.liquidity.types import Liquidity, LiquidityPool
from app.engines.manipulation.types import Manipulation

_NO_MANIPULATION = Manipulation(
    quality=Decimal(0),
    swept_pool=None,
    direction=None,
    sweep_bar_time=None,
    rejection_bar_time=None,
)


def _evaluate_pool(
    bars: tuple[Bar, ...], pool: LiquidityPool, *, atr: Decimal, cfg: ManipulationConfig
) -> Manipulation | None:
    n = len(bars)
    search_start = max(0, n - cfg.max_bars_beyond_level - 3)

    sweep_index: int | None = None
    for i in range(n - 1, search_start - 1, -1):
        bar = bars[i]
        beyond = bar.high > pool.price if pool.side == "HIGH" else bar.low < pool.price
        if beyond:
            sweep_index = i
            break
    if sweep_index is None:
        return None

    rejection_index: int | None = None
    for j in range(sweep_index, min(sweep_index + cfg.max_bars_beyond_level + 1, n)):
        bar = bars[j]
        closed_back_inside = (
            bar.close < pool.price if pool.side == "HIGH" else bar.close > pool.price
        )
        if closed_back_inside and bar.body_ratio >= cfg.min_rejection_body_ratio:
            rejection_index = j
            break
    if rejection_index is None:
        return None

    rejection_bar = bars[rejection_index]
    last_bar = bars[-1]
    direction = Direction.SHORT if pool.side == "HIGH" else Direction.LONG
    displacement = (
        (rejection_bar.close - last_bar.close)
        if direction == Direction.SHORT
        else (last_bar.close - rejection_bar.close)
    )
    displacement_atr = (displacement / atr) if atr != 0 else Decimal(0)

    penetration = (
        (bars[sweep_index].high - pool.price)
        if pool.side == "HIGH"
        else (pool.price - bars[sweep_index].low)
    )
    penetration_atr = (penetration / atr) if atr != 0 else Decimal(0)
    bars_beyond = rejection_index - sweep_index

    quality = _score(
        penetration_atr=penetration_atr,
        bars_beyond=bars_beyond,
        max_bars=cfg.max_bars_beyond_level,
        rejection_body_ratio=rejection_bar.body_ratio,
        displacement_atr=displacement_atr,
        min_displacement_atr=cfg.min_displacement_atr,
    )

    return Manipulation(
        quality=quality,
        swept_pool=pool,
        direction=direction if displacement_atr >= cfg.min_displacement_atr else None,
        sweep_bar_time=bars[sweep_index].open_time,
        rejection_bar_time=rejection_bar.open_time,
        detail={
            "penetration_atr": str(penetration_atr),
            "bars_beyond": bars_beyond,
            "rejection_body_ratio": str(rejection_bar.body_ratio),
            "displacement_atr": str(displacement_atr),
        },
    )


def _score(
    *,
    penetration_atr: Decimal,
    bars_beyond: int,
    max_bars: int,
    rejection_body_ratio: Decimal,
    displacement_atr: Decimal,
    min_displacement_atr: Decimal,
) -> Decimal:
    penetration_score = min(penetration_atr / Decimal("0.5"), Decimal(1))
    speed_score = Decimal(1) - Decimal(bars_beyond) / Decimal(max(max_bars, 1))
    body_score = min(rejection_body_ratio, Decimal(1))
    displacement_score = (
        min(displacement_atr / (min_displacement_atr * 2), Decimal(1))
        if min_displacement_atr > 0
        else Decimal(0)
    )
    weights = (Decimal("0.2"), Decimal("0.2"), Decimal("0.3"), Decimal("0.3"))
    scores = (penetration_score, speed_score, body_score, displacement_score)
    total = sum((w * s for w, s in zip(weights, scores, strict=True)), Decimal(0))
    return max(min(total, Decimal(1)), Decimal(0))


class ManipulationEngine:
    def __init__(self, config: ManipulationConfig) -> None:
        self._config = config

    def evaluate(self, state: MarketState, liquidity: Liquidity, *, atr: Decimal) -> Manipulation:
        cfg = self._config
        if state.session not in cfg.require_session:
            return _NO_MANIPULATION

        primary_bars = state.bars.get(state.primary_tf, ())
        if not primary_bars:
            return _NO_MANIPULATION

        candidates = liquidity.sweep_candidates or liquidity.pools
        best: Manipulation | None = None
        for pool in candidates:
            result = _evaluate_pool(primary_bars, pool, atr=atr, cfg=cfg)
            if result is not None and (best is None or result.quality > best.quality):
                best = result

        return best if best is not None else _NO_MANIPULATION
