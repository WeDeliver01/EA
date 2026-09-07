"""SPEC-05 §3.2: Structure and its constituent value types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.market.enums import Direction


@dataclass(frozen=True, slots=True)
class SwingPoint:
    price: Decimal
    bar_time: datetime


@dataclass(frozen=True, slots=True)
class StructureBreak:
    direction: Direction
    level: Decimal
    bar_time: datetime
    by_wick: bool


@dataclass(frozen=True, slots=True)
class Level:
    kind: str  # PDH, PDL, PWH, PWL, SESSION_HIGH, SESSION_LOW
    price: Decimal


@dataclass(frozen=True, slots=True)
class Structure:
    swing_highs: tuple[SwingPoint, ...]
    swing_lows: tuple[SwingPoint, ...]
    trend_state: str  # HH_HL, LH_LL, MIXED
    range_high: Decimal | None
    range_low: Decimal | None
    range_bars: int
    last_break: StructureBreak | None
    key_levels: tuple[Level, ...]
