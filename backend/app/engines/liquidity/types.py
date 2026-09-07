"""SPEC-05 §3.3: LiquidityPool and the LiquidityEngine's output."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class LiquidityPool:
    price: Decimal
    kind: str  # EQUAL_HIGH, EQUAL_LOW, PDH, PDL, SESSION_HIGH, SESSION_LOW, ROUND_NUMBER
    side: str  # "HIGH" (stops likely resting above) or "LOW" (below)
    touches: int
    significance: Decimal  # 0..1


@dataclass(frozen=True, slots=True)
class Liquidity:
    pools: tuple[LiquidityPool, ...]
    sweep_candidates: tuple[LiquidityPool, ...]
