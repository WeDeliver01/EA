"""SPEC-01 §4: RiskDecision."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.risk.limits import RiskLimits
from app.domain.risk.sizing import PositionSize
from app.domain.risk.state import RiskState
from app.domain.strategy.gate_result import GateResult


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    size: PositionSize | None
    gates: tuple[GateResult, ...]
    limits_snapshot: RiskLimits
    state_snapshot: RiskState
