"""SPEC-01 §4: RiskState."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RiskState:
    as_of: datetime
    realised_pnl_today: Decimal
    realised_pnl_week: Decimal
    open_risk: Decimal  # sum of (entry - stop) * size across open positions
    trades_today: int
    open_position_count: int
    consecutive_losses: int
    peak_equity: Decimal
    current_drawdown_pct: Decimal
    trading_enabled: bool
    kill_switch_active: bool
