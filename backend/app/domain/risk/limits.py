"""SPEC-01 §4: RiskLimits."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RiskLimits:
    risk_per_trade_pct: Decimal  # e.g. 0.005 = 0.5%
    max_daily_loss_pct: Decimal
    max_weekly_loss_pct: Decimal
    max_open_risk_pct: Decimal
    max_daily_trades: int
    max_open_positions: int
    max_positions_per_symbol: int
    max_correlated_positions: int
    min_rr: Decimal
    max_spread_multiple_of_atr: Decimal
    pause_after_consecutive_losses: int
    pause_duration_minutes: int
    max_lot_size: Decimal
