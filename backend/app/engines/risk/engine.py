"""SPEC-06 §1: the risk engine, composed from sizing + gates.

P1 made structural: nothing below this in the safety hierarchy (confluence,
pattern detection) can influence `approved`. A `Decision` with `outcome ==
TRADE` and confluence 10.0 that reaches here with a limit breached still
produces `approved=False`. There is no `force=True` parameter.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.symbol_spec import SymbolSpec
from app.domain.risk.decision import RiskDecision
from app.domain.risk.limits import RiskLimits
from app.domain.risk.state import RiskState
from app.engines.risk.gates import evaluate_risk_gates
from app.engines.risk.sizing import calculate_size


def evaluate(
    *,
    risk_state: RiskState,
    risk_limits: RiskLimits,
    account_equity: Decimal,
    account_free_margin: Decimal,
    equity_at_day_start: Decimal,
    equity_at_week_start: Decimal,
    entry: Decimal,
    stop_loss: Decimal,
    spec: SymbolSpec,
    conversion_rate: Decimal,
    leverage: int,
    margin_safety_factor: Decimal,
) -> RiskDecision:
    sizing_outcome = calculate_size(
        account_equity=account_equity,
        account_free_margin=account_free_margin,
        risk_pct=risk_limits.risk_per_trade_pct,
        entry=entry,
        stop_loss=stop_loss,
        spec=spec,
        conversion_rate=conversion_rate,
        leverage=leverage,
        max_lot_size=risk_limits.max_lot_size,
        margin_safety_factor=margin_safety_factor,
    )
    new_risk_amount = (
        sizing_outcome.size.risk_amount if sizing_outcome.size is not None else Decimal(0)
    )

    gates = evaluate_risk_gates(
        risk_state,
        risk_limits,
        account_equity=account_equity,
        equity_at_day_start=equity_at_day_start,
        equity_at_week_start=equity_at_week_start,
        new_risk_amount=new_risk_amount,
        sizing_block_reason=sizing_outcome.block_reason,
    )

    approved = sizing_outcome.approved and all(g.passed for g in gates)

    return RiskDecision(
        approved=approved,
        size=sizing_outcome.size if approved else None,
        gates=gates,
        limits_snapshot=risk_limits,
        state_snapshot=risk_state,
    )
