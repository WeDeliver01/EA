"""SPEC-01 §3: Setup."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.market.enums import Direction


@dataclass(frozen=True, slots=True)
class Setup:
    kind: str  # "BREAKOUT", "BREAKOUT_RETEST", "PULLBACK_CONTINUATION"
    direction: Direction
    trigger_price: Decimal
    invalidation_price: Decimal  # structural stop level, before ATR adjustment
    reference_level: Decimal  # the level that was broken or retested
    fingerprint: str  # deterministic hash for duplicate detection
