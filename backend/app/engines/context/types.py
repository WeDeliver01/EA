"""SPEC-05 §3.1: Context, the ContextEngine's output."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.market.enums import Direction, Regime, Session, Timeframe


@dataclass(frozen=True, slots=True)
class Context:
    regime: Regime
    htf_bias: Direction | None
    itf_bias: Direction | None
    atr: Mapping[Timeframe, Decimal]
    atr_percentile: Decimal
    atr_expanding: bool
    adx: Decimal
    range_pct_of_atr: Decimal
    session: Session
    bars_since_session_open: int
    inputs: Mapping[str, Any] = field(default_factory=dict)
