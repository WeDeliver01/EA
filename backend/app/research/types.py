"""Shared research-package types: what a backtest run produces and what
`metrics.py` consumes. Distinct from `app.models.tables.Trade` (the
persisted DB row) - this is the research engine's own working record,
independent of how or whether it ever gets written to the database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.market.enums import Direction, Regime, Session


@dataclass(frozen=True, slots=True)
class ResearchTrade:
    entry_time: datetime
    exit_time: datetime
    direction: Direction
    entry_price: Decimal
    exit_price: Decimal
    volume: Decimal
    gross_pnl: Decimal
    commission: Decimal
    swap: Decimal
    net_pnl: Decimal
    risk_amount: Decimal
    r_multiple: Decimal
    mae: Decimal | None
    mfe: Decimal | None
    exit_reason: str
    session: Session | None = None
    regime: Regime | None = None
    confluence_score: Decimal | None = None
    is_out_of_sample: bool = False


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts: datetime
    balance: Decimal
    equity: Decimal
    open_positions: int = 0
