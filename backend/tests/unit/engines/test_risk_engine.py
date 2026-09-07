"""SPEC-06 §1: nothing below the risk engine can override `approved` (P1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.market.enums import AssetClass
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.risk.limits import RiskLimits
from app.domain.risk.state import RiskState
from app.engines.risk.engine import evaluate

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 7, 9, 15, tzinfo=UTC)

_SPEC = SymbolSpec(
    symbol="XAUUSD",
    asset_class=AssetClass.METAL,
    digits=2,
    point=Decimal("0.01"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("1.00"),
    contract_size=Decimal("100"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("50"),
    volume_step=Decimal("0.01"),
    stops_level_points=50,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)

_LIMITS = RiskLimits(
    risk_per_trade_pct=Decimal("0.005"),
    max_daily_loss_pct=Decimal("0.03"),
    max_weekly_loss_pct=Decimal("0.06"),
    max_open_risk_pct=Decimal("0.02"),
    max_daily_trades=5,
    max_open_positions=3,
    max_positions_per_symbol=1,
    max_correlated_positions=1,
    min_rr=Decimal("1.5"),
    max_spread_multiple_of_atr=Decimal("0.12"),
    pause_after_consecutive_losses=3,
    pause_duration_minutes=60,
    max_lot_size=Decimal("5"),
)


def _clean_state(**overrides: object) -> RiskState:
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


def _evaluate(**overrides: object):
    defaults: dict[str, object] = {
        "risk_state": _clean_state(),
        "risk_limits": _LIMITS,
        "account_equity": Decimal("10000"),
        "account_free_margin": Decimal("10000"),
        "equity_at_day_start": Decimal("10000"),
        "equity_at_week_start": Decimal("10000"),
        "entry": Decimal("3418.20"),
        "stop_loss": Decimal("3412.55"),
        "spec": _SPEC,
        "conversion_rate": Decimal("1"),
        "leverage": 500,
        "margin_safety_factor": Decimal("0.30"),
    }
    defaults.update(overrides)
    return evaluate(**defaults)  # type: ignore[arg-type]


def test_approved_when_sizing_succeeds_and_all_gates_pass() -> None:
    decision = _evaluate()
    assert decision.approved is True
    assert decision.size is not None
    assert decision.size.volume > 0


def test_a_maxed_out_confluence_style_signal_is_still_blocked_by_a_risk_limit() -> None:
    """P1: risk outranks conviction. No `force=True` exists - a breached
    daily loss limit blocks the trade regardless of anything upstream."""
    decision = _evaluate(risk_state=_clean_state(realised_pnl_today=Decimal("-500")))
    assert decision.approved is False
    assert decision.size is None
    blocking = [g.code.value for g in decision.gates if not g.passed]
    assert "DAILY_LOSS_LIMIT" in blocking


def test_kill_switch_blocks_even_a_perfectly_sizeable_trade() -> None:
    decision = _evaluate(risk_state=_clean_state(kill_switch_active=True))
    assert decision.approved is False
    assert decision.size is None


def test_sizing_block_alone_is_sufficient_to_deny_even_with_clean_gates() -> None:
    decision = _evaluate(
        entry=Decimal("100"), stop_loss=Decimal("100.01"), account_equity=Decimal("1")
    )
    assert decision.approved is False
    assert decision.size is None
