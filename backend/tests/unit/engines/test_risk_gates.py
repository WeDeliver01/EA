"""SPEC-10 Phase 2 acceptance: every GateCode this MVP implements has at
least one triggering test - the risk-side gates from SPEC-06 §3 that are
computable from RiskState/RiskLimits alone."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.risk.limits import RiskLimits
from app.domain.risk.state import RiskState
from app.domain.strategy.enums import GateCode
from app.engines.risk.gates import evaluate_risk_gates

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 7, 9, 15, tzinfo=UTC)


def _state(**overrides: object) -> RiskState:
    defaults: dict[str, object] = {
        "as_of": _NOW,
        "realised_pnl_today": Decimal("0"),
        "realised_pnl_week": Decimal("0"),
        "open_risk": Decimal("0"),
        "trades_today": 0,
        "open_position_count": 0,
        "consecutive_losses": 0,
        "peak_equity": Decimal("10000"),
        "current_drawdown_pct": Decimal("0"),
        "trading_enabled": True,
        "kill_switch_active": False,
    }
    defaults.update(overrides)
    return RiskState(**defaults)  # type: ignore[arg-type]


def _limits(**overrides: object) -> RiskLimits:
    defaults: dict[str, object] = {
        "risk_per_trade_pct": Decimal("0.005"),
        "max_daily_loss_pct": Decimal("0.03"),
        "max_weekly_loss_pct": Decimal("0.06"),
        "max_open_risk_pct": Decimal("0.02"),
        "max_daily_trades": 5,
        "max_open_positions": 3,
        "max_positions_per_symbol": 1,
        "max_correlated_positions": 1,
        "min_rr": Decimal("1.5"),
        "max_spread_multiple_of_atr": Decimal("0.12"),
        "pause_after_consecutive_losses": 3,
        "pause_duration_minutes": 60,
        "max_lot_size": Decimal("5"),
    }
    defaults.update(overrides)
    return RiskLimits(**defaults)  # type: ignore[arg-type]


def _gate(gates, code: GateCode):
    return next(g for g in gates if g.code == code)


def _evaluate(**overrides: object):
    defaults: dict[str, object] = {
        "state": _state(),
        "limits": _limits(),
        "account_equity": Decimal("10000"),
        "equity_at_day_start": Decimal("10000"),
        "equity_at_week_start": Decimal("10000"),
        "new_risk_amount": Decimal("0"),
        "sizing_block_reason": None,
    }
    defaults.update(overrides)
    return evaluate_risk_gates(**defaults)  # type: ignore[arg-type]


def test_all_gates_pass_on_a_clean_risk_state() -> None:
    gates = _evaluate()
    assert all(g.passed for g in gates)
    assert {g.code for g in gates} == {
        GateCode.TRADING_DISABLED,
        GateCode.KILL_SWITCH_ACTIVE,
        GateCode.DAILY_LOSS_LIMIT,
        GateCode.WEEKLY_LOSS_LIMIT,
        GateCode.MAX_DAILY_TRADES,
        GateCode.MAX_OPEN_POSITIONS,
        GateCode.MAX_OPEN_RISK,
        GateCode.INSUFFICIENT_MARGIN,
    }


def test_trading_disabled_fails_when_trading_is_off() -> None:
    gates = _evaluate(state=_state(trading_enabled=False))
    assert _gate(gates, GateCode.TRADING_DISABLED).passed is False


def test_kill_switch_active_fails_when_switch_is_on() -> None:
    gates = _evaluate(state=_state(kill_switch_active=True))
    assert _gate(gates, GateCode.KILL_SWITCH_ACTIVE).passed is False


def test_daily_loss_limit_fails_when_realised_loss_breaches_the_floor() -> None:
    # floor = -(10000 * 0.03) = -300
    gates = _evaluate(state=_state(realised_pnl_today=Decimal("-301")))
    assert _gate(gates, GateCode.DAILY_LOSS_LIMIT).passed is False


def test_daily_loss_limit_passes_at_the_boundary() -> None:
    gates = _evaluate(state=_state(realised_pnl_today=Decimal("-299")))
    assert _gate(gates, GateCode.DAILY_LOSS_LIMIT).passed is True


def test_weekly_loss_limit_fails_when_realised_loss_breaches_the_floor() -> None:
    # floor = -(10000 * 0.06) = -600
    gates = _evaluate(state=_state(realised_pnl_week=Decimal("-601")))
    assert _gate(gates, GateCode.WEEKLY_LOSS_LIMIT).passed is False


def test_max_daily_trades_fails_at_the_limit() -> None:
    gates = _evaluate(state=_state(trades_today=5), limits=_limits(max_daily_trades=5))
    assert _gate(gates, GateCode.MAX_DAILY_TRADES).passed is False


def test_max_daily_trades_passes_below_the_limit() -> None:
    gates = _evaluate(state=_state(trades_today=4), limits=_limits(max_daily_trades=5))
    assert _gate(gates, GateCode.MAX_DAILY_TRADES).passed is True


def test_max_open_positions_fails_at_the_limit() -> None:
    gates = _evaluate(state=_state(open_position_count=3), limits=_limits(max_open_positions=3))
    assert _gate(gates, GateCode.MAX_OPEN_POSITIONS).passed is False


def test_max_open_risk_fails_when_adding_the_new_trade_would_breach_it() -> None:
    # limit = 10000 * 0.02 = 200
    gates = _evaluate(state=_state(open_risk=Decimal("150")), new_risk_amount=Decimal("100"))
    assert _gate(gates, GateCode.MAX_OPEN_RISK).passed is False


def test_max_open_risk_passes_at_the_boundary() -> None:
    gates = _evaluate(state=_state(open_risk=Decimal("100")), new_risk_amount=Decimal("100"))
    assert _gate(gates, GateCode.MAX_OPEN_RISK).passed is True


def test_insufficient_margin_fails_when_sizing_was_blocked_for_margin() -> None:
    gates = _evaluate(sizing_block_reason="INSUFFICIENT_MARGIN")
    assert _gate(gates, GateCode.INSUFFICIENT_MARGIN).passed is False


def test_insufficient_margin_passes_for_other_block_reasons() -> None:
    gates = _evaluate(sizing_block_reason="BELOW_MIN_VOLUME")
    assert _gate(gates, GateCode.INSUFFICIENT_MARGIN).passed is True
