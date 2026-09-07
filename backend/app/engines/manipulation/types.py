"""SPEC-05 §3.4: Manipulation, the ManipulationEngine's output."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.market.enums import Direction
from app.engines.liquidity.types import LiquidityPool


@dataclass(frozen=True, slots=True)
class Manipulation:
    quality: Decimal  # 0..1, 0 means no sweep-and-reject sequence found
    swept_pool: LiquidityPool | None
    direction: Direction | None  # direction of the anticipated move after the sweep
    sweep_bar_time: datetime | None
    rejection_bar_time: datetime | None
    detail: Mapping[str, Any] = field(default_factory=dict)
