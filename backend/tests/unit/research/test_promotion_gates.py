"""SPEC-07 §7 / SPEC-10 Phase 3 acceptance tests for promotion gates.

Acceptance criterion: "Promotion gate function returns per-gate pass or fail
and is unit tested at each boundary." `_ALL_PASSING` sits exactly on the
passing side of every threshold; each parametrized case flips exactly one
field to the failing side of its own boundary and asserts that only that
gate's result changes.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.research.promotion_gates import (
    GateCheckResult,
    PromotionGateInputs,
    all_gates_passed,
    evaluate_promotion_gates,
)

pytestmark = pytest.mark.unit

# Every field sits exactly on the passing edge of its own threshold.
_ALL_PASSING = PromotionGateInputs(
    oos_trade_count=100,
    oos_expectancy_r=Decimal("0.0001"),
    oos_profit_factor=Decimal("1.15"),
    walk_forward_efficiency=Decimal("0.5"),
    monte_carlo_p95_drawdown_pct=Decimal("0.25"),
    profit_concentration_top1_pct=Decimal("0.25"),
    profit_concentration_top5_pct=Decimal("0.60"),
    profitable_periods_pct=Decimal("0.60"),
    parameter_plateau_passed=True,
    cost_sensitivity_2x_spread_pf=Decimal("1.0001"),
    pessimistic_fill_pf=Decimal("1.0001"),
    data_quality_passed=True,
    demo_trading_days=30,
    demo_trade_count=30,
    demo_vs_backtest_expectancy_gap_pct=Decimal("0.30"),
)


def _result(results: tuple[GateCheckResult, ...], name: str) -> GateCheckResult:
    matches = [r for r in results if r.name == name]
    assert matches, f"no gate named {name!r}"
    return matches[0]


def test_all_passing_inputs_pass_every_gate() -> None:
    results = evaluate_promotion_gates(_ALL_PASSING)
    assert all_gates_passed(results) is True
    assert all(r.passed for r in results)
    assert len(results) == 13


@pytest.mark.parametrize(
    ("gate_name", "failing_overrides"),
    [
        ("oos_trade_count", {"oos_trade_count": 99}),
        ("oos_expectancy_positive", {"oos_expectancy_r": Decimal("0")}),
        ("oos_profit_factor", {"oos_profit_factor": Decimal("1.14")}),
        ("oos_profit_factor", {"oos_profit_factor": None}),
        ("walk_forward_efficiency", {"walk_forward_efficiency": Decimal("0.49")}),
        ("walk_forward_efficiency", {"walk_forward_efficiency": None}),
        ("monte_carlo_drawdown", {"monte_carlo_p95_drawdown_pct": Decimal("0.2501")}),
        ("concentration_top1", {"profit_concentration_top1_pct": Decimal("0.2501")}),
        ("concentration_top1", {"profit_concentration_top1_pct": None}),
        ("concentration_top5", {"profit_concentration_top5_pct": Decimal("0.6001")}),
        ("concentration_top5", {"profit_concentration_top5_pct": None}),
        ("profitable_periods", {"profitable_periods_pct": Decimal("0.5999")}),
        ("parameter_plateau", {"parameter_plateau_passed": False}),
        ("cost_sensitivity_2x_spread", {"cost_sensitivity_2x_spread_pf": Decimal("1.0")}),
        ("cost_sensitivity_2x_spread", {"cost_sensitivity_2x_spread_pf": None}),
        ("fill_model_gap", {"pessimistic_fill_pf": Decimal("1.0")}),
        ("fill_model_gap", {"pessimistic_fill_pf": None}),
        ("data_quality", {"data_quality_passed": False}),
        ("forward_demo", {"demo_trading_days": 29}),
        ("forward_demo", {"demo_trade_count": 29}),
        ("forward_demo", {"demo_vs_backtest_expectancy_gap_pct": Decimal("0.3001")}),
        ("forward_demo", {"demo_vs_backtest_expectancy_gap_pct": Decimal("-0.3001")}),
        ("forward_demo", {"demo_vs_backtest_expectancy_gap_pct": None}),
    ],
)
def test_gate_fails_just_past_its_boundary(
    gate_name: str, failing_overrides: dict[str, object]
) -> None:
    inputs = replace(_ALL_PASSING, **failing_overrides)  # type: ignore[arg-type]
    results = evaluate_promotion_gates(inputs)
    assert _result(results, gate_name).passed is False
    assert all_gates_passed(results) is False

    other_names = {r.name for r in results if r.name != gate_name}
    still_passing = {r.name for r in results if r.passed}
    assert other_names <= still_passing | {gate_name}


@pytest.mark.parametrize(
    ("gate_name", "passing_overrides"),
    [
        ("monte_carlo_drawdown", {"monte_carlo_p95_drawdown_pct": Decimal("0.2499")}),
        ("forward_demo", {"demo_vs_backtest_expectancy_gap_pct": Decimal("-0.30")}),
    ],
)
def test_gate_passes_at_and_inside_its_boundary(
    gate_name: str, passing_overrides: dict[str, object]
) -> None:
    inputs = replace(_ALL_PASSING, **passing_overrides)  # type: ignore[arg-type]
    results = evaluate_promotion_gates(inputs)
    assert _result(results, gate_name).passed is True
    assert all_gates_passed(results) is True
