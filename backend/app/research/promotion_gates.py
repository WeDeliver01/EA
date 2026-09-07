"""SPEC-07 §7: promotion gates.

A strategy version cannot be activated on a live account unless every one of
these passes. Implemented as a function returning per-gate pass/fail so a
report can show all of them, not just the first failure - matching the
report generator's "promotion gate results ... at the top of the report,
not the bottom" (SPEC-07 §9 point 14). A version that fails is not blocked
from research; it's blocked from money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PromotionGateInputs:
    oos_trade_count: int
    oos_expectancy_r: Decimal
    oos_profit_factor: Decimal | None
    walk_forward_efficiency: Decimal | None
    monte_carlo_p95_drawdown_pct: Decimal
    profit_concentration_top1_pct: Decimal | None
    profit_concentration_top5_pct: Decimal | None
    profitable_periods_pct: Decimal
    parameter_plateau_passed: bool
    cost_sensitivity_2x_spread_pf: Decimal | None
    pessimistic_fill_pf: Decimal | None
    data_quality_passed: bool
    demo_trading_days: int
    demo_trade_count: int
    demo_vs_backtest_expectancy_gap_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class GateCheckResult:
    name: str
    passed: bool
    detail: dict[str, object] = field(default_factory=dict)


def evaluate_promotion_gates(inputs: PromotionGateInputs) -> tuple[GateCheckResult, ...]:
    checks: list[GateCheckResult] = []

    checks.append(
        GateCheckResult(
            "oos_trade_count",
            inputs.oos_trade_count >= 100,
            {"actual": inputs.oos_trade_count, "minimum": 100},
        )
    )
    checks.append(
        GateCheckResult(
            "oos_expectancy_positive",
            inputs.oos_expectancy_r > 0,
            {"actual": str(inputs.oos_expectancy_r)},
        )
    )
    checks.append(
        GateCheckResult(
            "oos_profit_factor",
            inputs.oos_profit_factor is not None and inputs.oos_profit_factor >= Decimal("1.15"),
            {"actual": str(inputs.oos_profit_factor), "minimum": "1.15"},
        )
    )
    checks.append(
        GateCheckResult(
            "walk_forward_efficiency",
            inputs.walk_forward_efficiency is not None
            and inputs.walk_forward_efficiency >= Decimal("0.5"),
            {"actual": str(inputs.walk_forward_efficiency), "minimum": "0.5"},
        )
    )
    checks.append(
        GateCheckResult(
            "monte_carlo_drawdown",
            inputs.monte_carlo_p95_drawdown_pct <= Decimal("0.25"),
            {"actual": str(inputs.monte_carlo_p95_drawdown_pct), "maximum": "0.25"},
        )
    )
    checks.append(
        GateCheckResult(
            "concentration_top1",
            inputs.profit_concentration_top1_pct is not None
            and inputs.profit_concentration_top1_pct <= Decimal("0.25"),
            {"actual": str(inputs.profit_concentration_top1_pct), "maximum": "0.25"},
        )
    )
    checks.append(
        GateCheckResult(
            "concentration_top5",
            inputs.profit_concentration_top5_pct is not None
            and inputs.profit_concentration_top5_pct <= Decimal("0.60"),
            {"actual": str(inputs.profit_concentration_top5_pct), "maximum": "0.60"},
        )
    )
    checks.append(
        GateCheckResult(
            "profitable_periods",
            inputs.profitable_periods_pct >= Decimal("0.60"),
            {"actual": str(inputs.profitable_periods_pct), "minimum": "0.60"},
        )
    )
    checks.append(GateCheckResult("parameter_plateau", inputs.parameter_plateau_passed, {}))
    checks.append(
        GateCheckResult(
            "cost_sensitivity_2x_spread",
            inputs.cost_sensitivity_2x_spread_pf is not None
            and inputs.cost_sensitivity_2x_spread_pf > Decimal("1.0"),
            {"actual": str(inputs.cost_sensitivity_2x_spread_pf), "minimum": "1.0 (exclusive)"},
        )
    )
    checks.append(
        GateCheckResult(
            "fill_model_gap",
            inputs.pessimistic_fill_pf is not None and inputs.pessimistic_fill_pf > Decimal("1.0"),
            {"actual": str(inputs.pessimistic_fill_pf), "minimum": "1.0 (exclusive)"},
        )
    )
    checks.append(GateCheckResult("data_quality", inputs.data_quality_passed, {}))

    demo_gate_ok = (
        inputs.demo_trading_days >= 30
        and inputs.demo_trade_count >= 30
        and inputs.demo_vs_backtest_expectancy_gap_pct is not None
        and abs(inputs.demo_vs_backtest_expectancy_gap_pct) <= Decimal("0.30")
    )
    checks.append(
        GateCheckResult(
            "forward_demo",
            demo_gate_ok,
            {
                "demo_trading_days": inputs.demo_trading_days,
                "demo_trade_count": inputs.demo_trade_count,
                "expectancy_gap_pct": str(inputs.demo_vs_backtest_expectancy_gap_pct),
            },
        )
    )

    return tuple(checks)


def all_gates_passed(results: tuple[GateCheckResult, ...]) -> bool:
    return all(r.passed for r in results)
