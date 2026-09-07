"""SPEC-06 §3: the hard gates that can be evaluated from RiskState and
RiskLimits alone.

Not implemented here (need live inputs this MVP doesn't yet model - agent
heartbeats, reconciliation runs, correlation groups, broker trading-hours
calendars): AGENT_DISCONNECTED, BROKER_DISCONNECTED,
RECONCILIATION_UNRESOLVED, MARKET_CLOSED, STRATEGY_PAUSED,
CORRELATED_EXPOSURE. NEWS_BLACKOUT and DUPLICATE_SETUP are evaluated as
strategy gates instead (`app.engines.gates.strategy_gates`), since
`MarketState` already carries what they need. See
docs/adr/0001-mvp-scope.md.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.risk.limits import RiskLimits
from app.domain.risk.state import RiskState
from app.domain.strategy.enums import GateCode
from app.domain.strategy.gate_result import GateResult


def evaluate_risk_gates(
    state: RiskState,
    limits: RiskLimits,
    *,
    account_equity: Decimal,
    equity_at_day_start: Decimal,
    equity_at_week_start: Decimal,
    new_risk_amount: Decimal,
    sizing_block_reason: str | None,
) -> tuple[GateResult, ...]:
    gates: list[GateResult] = []

    gates.append(
        GateResult(code=GateCode.TRADING_DISABLED, passed=state.trading_enabled, detail={})
    )
    gates.append(
        GateResult(code=GateCode.KILL_SWITCH_ACTIVE, passed=not state.kill_switch_active, detail={})
    )

    daily_floor = -(equity_at_day_start * limits.max_daily_loss_pct)
    daily_ok = state.realised_pnl_today > daily_floor
    gates.append(
        GateResult(
            code=GateCode.DAILY_LOSS_LIMIT,
            passed=daily_ok,
            detail={"realised_pnl_today": str(state.realised_pnl_today), "floor": str(daily_floor)},
        )
    )

    weekly_floor = -(equity_at_week_start * limits.max_weekly_loss_pct)
    weekly_ok = state.realised_pnl_week > weekly_floor
    gates.append(
        GateResult(
            code=GateCode.WEEKLY_LOSS_LIMIT,
            passed=weekly_ok,
            detail={"realised_pnl_week": str(state.realised_pnl_week), "floor": str(weekly_floor)},
        )
    )

    gates.append(
        GateResult(
            code=GateCode.MAX_DAILY_TRADES,
            passed=state.trades_today < limits.max_daily_trades,
            detail={"trades_today": state.trades_today, "limit": limits.max_daily_trades},
        )
    )
    gates.append(
        GateResult(
            code=GateCode.MAX_OPEN_POSITIONS,
            passed=state.open_position_count < limits.max_open_positions,
            detail={
                "open_position_count": state.open_position_count,
                "limit": limits.max_open_positions,
            },
        )
    )

    max_open_risk = account_equity * limits.max_open_risk_pct
    open_risk_ok = (state.open_risk + new_risk_amount) <= max_open_risk
    gates.append(
        GateResult(
            code=GateCode.MAX_OPEN_RISK,
            passed=open_risk_ok,
            detail={
                "open_risk": str(state.open_risk),
                "new_risk": str(new_risk_amount),
                "limit": str(max_open_risk),
            },
        )
    )

    gates.append(
        GateResult(
            code=GateCode.INSUFFICIENT_MARGIN,
            passed=sizing_block_reason != "INSUFFICIENT_MARGIN",
            detail={"block_reason": sizing_block_reason},
        )
    )

    return tuple(gates)
