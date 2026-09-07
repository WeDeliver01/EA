"""SPEC-07 §5: the cost model.

Not pure in the `engines/` sense (it isn't subject to the purity grep or the
import-linter's "engines import only domain" contract - `research/` may
import `domain` and `engines` per SPEC-00 §5's layering rule), but every
function here is still deterministic: no I/O, no clock reads. Monte Carlo
(`app.research.monte_carlo`) is the one place under `research/` that uses
randomness, and it takes an explicit seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.domain.exceptions import DomainError
from app.domain.market.enums import Direction
from app.domain.market.symbol_spec import SymbolSpec


class UnsupportedSlippageModel(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class CostModel:
    spread_source: str  # 'recorded' | 'fixed'
    fixed_spread_points: int | None
    commission_per_lot_per_side: Decimal
    swap_long_points: Decimal
    swap_short_points: Decimal
    triple_swap_weekday: int  # 0=Monday .. 6=Sunday; 2=Wednesday for most brokers
    slippage_model: str  # 'none' | 'fixed'
    slippage_points: int | None


def spread_cost_price(
    spec: SymbolSpec, cfg: CostModel, *, recorded_spread_points: int | None
) -> Decimal:
    """Half-spread, in price terms, paid on entry and again on exit."""
    if cfg.spread_source == "fixed":
        points = cfg.fixed_spread_points or 0
    else:
        points = recorded_spread_points or 0
    return Decimal(points) * spec.point / 2


def slippage_price(spec: SymbolSpec, cfg: CostModel) -> Decimal:
    if cfg.slippage_model == "none":
        return Decimal(0)
    if cfg.slippage_model == "fixed":
        return Decimal(cfg.slippage_points or 0) * spec.point
    # 'measured' and 'volatility_scaled' (SPEC-07 §5) need a real distribution
    # of live slippage, which doesn't exist without live trades - see
    # docs/adr/0001-mvp-scope.md.
    raise UnsupportedSlippageModel(
        f"slippage_model {cfg.slippage_model!r} needs live-measured data, not available yet"
    )


def commission_cost(volume: Decimal, cfg: CostModel) -> Decimal:
    """One side (entry or exit) of commission. Callers charge it twice for a
    full round trip."""
    return volume * cfg.commission_per_lot_per_side


def swap_cost(
    *,
    volume: Decimal,
    direction: Direction,
    nights_held: list[date],
    cfg: CostModel,
    spec: SymbolSpec,
) -> Decimal:
    """Swap charged for each night the position was held open, tripled on
    `triple_swap_weekday` (most brokers charge 3x on Wednesday to cover the
    weekend). `nights_held` is the list of calendar dates the position was
    open at each broker daily rollover.
    """
    points_per_night = (
        cfg.swap_long_points if direction == Direction.LONG else cfg.swap_short_points
    )
    total_points = Decimal(0)
    for night in nights_held:
        multiplier = 3 if night.weekday() == cfg.triple_swap_weekday else 1
        total_points += points_per_night * multiplier
    return total_points * spec.point * volume
