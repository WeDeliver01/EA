"""SPEC-07 §9 / SPEC-10 Phase 3 acceptance tests for the report assembler.

`build_report` only assembles pieces already computed elsewhere, so these
tests check the assembly wiring itself: promotion gates move to the top and
match a direct `evaluate_promotion_gates` call, the R-multiple histogram
buckets correctly, gate rejections tally and sort correctly, and the
concentration/yearly/monthly figures match their source functions in
`metrics.py` exactly rather than being recomputed differently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.enums import AssetClass, Direction, Regime, Timeframe
from app.domain.strategy.decision import Decision
from app.domain.strategy.enums import DecisionOutcome, GateCode
from app.domain.strategy.gate_result import GateResult
from app.research.data_quality import DataQualityReport, check_data_quality
from app.research.metrics import (
    compute_metrics,
    monthly_returns,
    net_profit_excluding_best_trade,
    net_profit_excluding_best_year,
    yearly_returns,
)
from app.research.promotion_gates import PromotionGateInputs, evaluate_promotion_gates
from app.research.report import RMultipleBucket, RunConfigSummary, build_report
from app.research.types import EquityPoint, ResearchTrade
from tests.factories import make_bars

pytestmark = pytest.mark.unit

_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _data_quality_report() -> DataQualityReport:
    bars = make_bars(count=10, start=_START, timeframe=Timeframe.M15)
    return check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)


def _run_config() -> RunConfigSummary:
    return RunConfigSummary(
        strategy_version_id="v1",
        engine_git_sha="deadbeef",
        dataset_name="synthetic",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cost_model_summary={"spread_source": "fixed"},
        fill_model_name="next_bar_open",
        seed=1,
    )


def _trade(
    *,
    r_multiple: Decimal,
    net_pnl: Decimal,
    entry_offset_days: int,
    mae: Decimal | None = None,
    mfe: Decimal | None = None,
    exit_reason: str = "PARTIAL_TP",
) -> ResearchTrade:
    entry = _START + timedelta(days=entry_offset_days)
    return ResearchTrade(
        entry_time=entry,
        exit_time=entry + timedelta(hours=1),
        direction=Direction.LONG,
        entry_price=Decimal("3400"),
        exit_price=Decimal("3400") + net_pnl,
        volume=Decimal("0.1"),
        gross_pnl=net_pnl,
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=net_pnl,
        risk_amount=Decimal("100"),
        r_multiple=r_multiple,
        mae=mae,
        mfe=mfe,
        exit_reason=exit_reason,
    )


def _equity_curve(trades: list[ResearchTrade], *, initial_balance: Decimal) -> list[EquityPoint]:
    balance = initial_balance
    points = []
    for t in sorted(trades, key=lambda tr: tr.exit_time):
        balance += t.net_pnl
        points.append(EquityPoint(ts=t.exit_time, balance=balance, equity=balance))
    return points


def _decision(
    *, outcome: DecisionOutcome, gates: tuple[GateResult, ...], as_of: datetime
) -> Decision:
    return Decision(
        outcome=outcome,
        symbol="XAUUSD",
        as_of=as_of,
        strategy_version_id=uuid.UUID(int=0),
        regime=Regime.UNKNOWN,
        setup=None,
        direction=None,
        entry=None,
        stop_loss=None,
        take_profits=(),
        confluence_score=Decimal("0"),
        confluence_band="WAIT",
        evidence=(),
        gates=gates,
        narrative="",
        engine_duration_ms=0,
    )


def _minimal_report_kwargs() -> tuple[list[ResearchTrade], list[EquityPoint]]:
    trades = [
        _trade(r_multiple=Decimal("2"), net_pnl=Decimal("200"), entry_offset_days=1),
        _trade(r_multiple=Decimal("-1"), net_pnl=Decimal("-100"), entry_offset_days=2),
    ]
    equity_curve = _equity_curve(trades, initial_balance=Decimal("10000"))
    return trades, equity_curve


def test_promotion_is_none_when_no_inputs_given() -> None:
    trades, equity_curve = _minimal_report_kwargs()
    metrics = compute_metrics(trades, equity_curve, period_years=1.0)
    dq = _data_quality_report()

    report = build_report(
        run_config=_run_config(),
        trades=trades,
        equity_curve=equity_curve,
        decisions=[],
        data_quality=dq,
        metrics=metrics,
    )
    assert report.promotion is None
    assert report.promotion_passed is None


def test_promotion_matches_direct_evaluate_call_and_is_included() -> None:
    trades, equity_curve = _minimal_report_kwargs()
    metrics = compute_metrics(trades, equity_curve, period_years=1.0)
    dq = _data_quality_report()

    inputs = PromotionGateInputs(
        oos_trade_count=50,
        oos_expectancy_r=Decimal("0.5"),
        oos_profit_factor=Decimal("1.0"),
        walk_forward_efficiency=Decimal("0.4"),
        monte_carlo_p95_drawdown_pct=Decimal("0.3"),
        profit_concentration_top1_pct=Decimal("0.4"),
        profit_concentration_top5_pct=Decimal("0.7"),
        profitable_periods_pct=Decimal("0.5"),
        parameter_plateau_passed=False,
        cost_sensitivity_2x_spread_pf=Decimal("0.9"),
        pessimistic_fill_pf=Decimal("0.9"),
        data_quality_passed=True,
        demo_trading_days=10,
        demo_trade_count=5,
        demo_vs_backtest_expectancy_gap_pct=Decimal("0.5"),
    )
    expected = evaluate_promotion_gates(inputs)

    report = build_report(
        run_config=_run_config(),
        trades=trades,
        equity_curve=equity_curve,
        decisions=[],
        data_quality=dq,
        metrics=metrics,
        promotion_inputs=inputs,
    )
    assert report.promotion == expected
    assert report.promotion_passed is False  # every gate above sits on the failing side


def test_r_multiple_histogram_buckets_at_half_r_width() -> None:
    trades = [
        _trade(r_multiple=Decimal("0.3"), net_pnl=Decimal("30"), entry_offset_days=1),
        _trade(r_multiple=Decimal("0.4"), net_pnl=Decimal("40"), entry_offset_days=2),
        _trade(r_multiple=Decimal("1.2"), net_pnl=Decimal("120"), entry_offset_days=3),
        _trade(r_multiple=Decimal("-0.5"), net_pnl=Decimal("-50"), entry_offset_days=4),
    ]
    equity_curve = _equity_curve(trades, initial_balance=Decimal("10000"))
    metrics = compute_metrics(trades, equity_curve, period_years=1.0)
    dq = _data_quality_report()

    report = build_report(
        run_config=_run_config(),
        trades=trades,
        equity_curve=equity_curve,
        decisions=[],
        data_quality=dq,
        metrics=metrics,
    )
    # 0.3 and 0.4 both floor into [0.0, 0.5); 1.2 floors into [1.0, 1.5);
    # -0.5 floors into [-0.5, 0.0) exactly (floor division on a boundary
    # value stays in the bucket it starts, not the one below it).
    assert report.r_multiple_histogram == (
        RMultipleBucket(lower=Decimal("-0.5"), upper=Decimal("0.0"), count=1),
        RMultipleBucket(lower=Decimal("0.0"), upper=Decimal("0.5"), count=2),
        RMultipleBucket(lower=Decimal("1.0"), upper=Decimal("1.5"), count=1),
    )


def test_mae_mfe_points_pass_through_unchanged() -> None:
    trades = [
        _trade(
            r_multiple=Decimal("2"),
            net_pnl=Decimal("200"),
            entry_offset_days=1,
            mae=Decimal("-30"),
            mfe=Decimal("210"),
            exit_reason="PARTIAL_TP",
        ),
    ]
    equity_curve = _equity_curve(trades, initial_balance=Decimal("10000"))
    metrics = compute_metrics(trades, equity_curve, period_years=1.0)
    dq = _data_quality_report()

    report = build_report(
        run_config=_run_config(),
        trades=trades,
        equity_curve=equity_curve,
        decisions=[],
        data_quality=dq,
        metrics=metrics,
    )
    assert len(report.mae_mfe_points) == 1
    point = report.mae_mfe_points[0]
    assert point.mae == Decimal("-30")
    assert point.mfe == Decimal("210")
    assert point.net_pnl == Decimal("200")
    assert point.exit_reason == "PARTIAL_TP"


def test_gate_rejection_counts_tally_and_sort_by_frequency() -> None:
    decisions = [
        _decision(
            outcome=DecisionOutcome.WAIT,
            as_of=_START + timedelta(hours=i),
            gates=(
                GateResult(code=GateCode.SPREAD_TOO_WIDE, passed=False),
                GateResult(code=GateCode.CONFLUENCE_BELOW_THRESHOLD, passed=True),
            ),
        )
        for i in range(3)
    ] + [
        _decision(
            outcome=DecisionOutcome.WAIT,
            as_of=_START + timedelta(hours=10),
            gates=(GateResult(code=GateCode.CONFLUENCE_BELOW_THRESHOLD, passed=False),),
        )
    ]
    trades, equity_curve = _minimal_report_kwargs()
    metrics = compute_metrics(trades, equity_curve, period_years=1.0)
    dq = _data_quality_report()

    report = build_report(
        run_config=_run_config(),
        trades=trades,
        equity_curve=equity_curve,
        decisions=decisions,
        data_quality=dq,
        metrics=metrics,
    )
    assert report.gate_rejection_counts[0].code == "SPREAD_TOO_WIDE"
    assert report.gate_rejection_counts[0].count == 3
    assert report.gate_rejection_counts[1].code == "CONFLUENCE_BELOW_THRESHOLD"
    assert report.gate_rejection_counts[1].count == 1


def test_concentration_and_returns_match_metrics_functions_directly() -> None:
    trades = [
        _trade(r_multiple=Decimal("2"), net_pnl=Decimal("500"), entry_offset_days=1),
        _trade(r_multiple=Decimal("1"), net_pnl=Decimal("100"), entry_offset_days=40),
        _trade(r_multiple=Decimal("-1"), net_pnl=Decimal("-50"), entry_offset_days=400),
    ]
    equity_curve = _equity_curve(trades, initial_balance=Decimal("10000"))
    metrics = compute_metrics(trades, equity_curve, period_years=2.0)
    dq = _data_quality_report()

    report = build_report(
        run_config=_run_config(),
        trades=trades,
        equity_curve=equity_curve,
        decisions=[],
        data_quality=dq,
        metrics=metrics,
    )

    assert report.concentration.top1_pct == metrics.profit_concentration_top1_pct
    assert report.concentration.top5_pct == metrics.profit_concentration_top5_pct
    assert report.concentration.net_profit_excluding_best_trade == net_profit_excluding_best_trade(
        trades
    )
    assert report.concentration.net_profit_excluding_best_year == net_profit_excluding_best_year(
        trades
    )
    assert report.concentration.longest_no_new_high_days == metrics.longest_no_new_high_days
    assert report.yearly_returns == yearly_returns(equity_curve)
    assert report.monthly_returns == monthly_returns(equity_curve)


def test_walk_forward_and_monte_carlo_pass_through_when_provided() -> None:
    trades, equity_curve = _minimal_report_kwargs()
    metrics = compute_metrics(trades, equity_curve, period_years=1.0)
    dq = _data_quality_report()

    sentinel_walk_forward = object()
    sentinel_monte_carlo = object()
    report = build_report(
        run_config=_run_config(),
        trades=trades,
        equity_curve=equity_curve,
        decisions=[],
        data_quality=dq,
        metrics=metrics,
        walk_forward=sentinel_walk_forward,  # type: ignore[arg-type]
        monte_carlo=sentinel_monte_carlo,  # type: ignore[arg-type]
        parameter_perturbation={"atr_stop_multiple": [1.0, 1.5]},
        cost_sensitivity={"2x_spread_pf": Decimal("1.2")},
    )
    assert report.walk_forward is sentinel_walk_forward
    assert report.monte_carlo is sentinel_monte_carlo
    assert report.parameter_perturbation == {"atr_stop_multiple": [1.0, 1.5]}
    assert report.cost_sensitivity == {"2x_spread_pf": Decimal("1.2")}
