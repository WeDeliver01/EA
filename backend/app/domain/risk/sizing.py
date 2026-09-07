"""SPEC-01 §4: PositionSize."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class PositionSize:
    volume: Decimal  # rounded to volume_step
    risk_amount: Decimal  # actual account-currency risk after rounding
    risk_pct_actual: Decimal
    stop_distance_price: Decimal
    stop_distance_points: int
    value_per_point_per_lot: Decimal
    margin_required: Decimal
    # every intermediate value from the sizing calculation, for audit
    calculation: Mapping[str, Any] = field(default_factory=dict)
