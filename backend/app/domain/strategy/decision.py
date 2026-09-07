"""SPEC-01 §3: Decision and TakeProfit.

A Decision is always produced, even when the outcome is WAIT. Every
evaluation writes an analysis_run row and a decision row (P3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.domain.market.enums import Direction, Regime
from app.domain.strategy.enums import DecisionOutcome
from app.domain.strategy.evidence import Evidence
from app.domain.strategy.gate_result import GateResult
from app.domain.strategy.setup import Setup


@dataclass(frozen=True, slots=True)
class TakeProfit:
    level: Decimal
    fraction: Decimal  # portion of the position to close, sums to <= 1
    r_multiple: Decimal


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: DecisionOutcome
    symbol: str
    as_of: datetime
    strategy_version_id: UUID
    regime: Regime
    setup: Setup | None
    direction: Direction | None
    entry: Decimal | None
    stop_loss: Decimal | None
    take_profits: tuple[TakeProfit, ...]
    confluence_score: Decimal
    confluence_band: str  # WAIT | WEAK | VALID | HIGH
    evidence: tuple[Evidence, ...]
    gates: tuple[GateResult, ...]
    narrative: str
    engine_duration_ms: int

    @property
    def blocking_gates(self) -> tuple[GateResult, ...]:
        return tuple(g for g in self.gates if not g.passed)
