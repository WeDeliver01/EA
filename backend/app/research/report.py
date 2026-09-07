"""SPEC-07 §9: backtest report.

Assembles the already-computed outputs of the other `research/` modules
into one structure, numbered to match SPEC-07 §9's own list, except point
14 (promotion gate results) moves to the top per that point's own
instruction: "at the top of the report, not the bottom." This module only
assembles; it does not compute new statistics beyond trivial pass-throughs
(a histogram bucketing, a gate-rejection tally) - every number it reports
comes from `metrics.py`, `data_quality.py`, `walk_forward.py`,
`monte_carlo.py` or `promotion_gates.py`.

Rendering to HTML/PDF (SPEC-07 §9's "renderable as ...") is a presentation
concern for a later phase (the Terminal, SPEC-09); this module produces the
data such a renderer would consume, not pixels.

Two SPEC-07 §9 items are not computed anywhere in this MVP pass, documented
in `docs/adr/0001-mvp-scope.md`:
- **Parameter perturbation surfaces** (§6 Stage 4): needs a real sweep
  harness that re-runs the backtester across a parameter grid; a strategy
  version's `parameter_plateau_passed` is a caller-supplied boolean
  (`PromotionGateInputs`), not computed here.
- **Cost sensitivity table** (§6 Stage 7): needs re-runs at 1.5x/2x spread
  and 1.5x commission; likewise caller-supplied if available.

Both parameters accept an already-computed payload from a caller that has
one, so this module has a place to put it once it exists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.domain.strategy.decision import Decision
from app.research.data_quality import DataQualityReport
from app.research.metrics import (
    BacktestMetrics,
    net_profit_excluding_best_trade,
    net_profit_excluding_best_year,
)
from app.research.metrics import monthly_returns as compute_monthly_returns
from app.research.metrics import yearly_returns as compute_yearly_returns
from app.research.monte_carlo import MonteCarloResult
from app.research.promotion_gates import (
    GateCheckResult,
    PromotionGateInputs,
    all_gates_passed,
    evaluate_promotion_gates,
)
from app.research.types import EquityPoint, ResearchTrade
from app.research.walk_forward import WalkForwardResult

_R_MULTIPLE_BUCKET_WIDTH = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class RunConfigSummary:
    strategy_version_id: str
    engine_git_sha: str
    dataset_name: str
    period_start: str
    period_end: str
    cost_model_summary: dict[str, object]
    fill_model_name: str
    seed: int | None


@dataclass(frozen=True, slots=True)
class RMultipleBucket:
    lower: Decimal
    upper: Decimal
    count: int


@dataclass(frozen=True, slots=True)
class MaeMfePoint:
    mae: Decimal | None
    mfe: Decimal | None
    net_pnl: Decimal
    exit_reason: str


@dataclass(frozen=True, slots=True)
class GateRejectionCount:
    code: str
    count: int


@dataclass(frozen=True, slots=True)
class ConcentrationAnalysis:
    top1_pct: Decimal | None
    top5_pct: Decimal | None
    net_profit_excluding_best_trade: Decimal
    net_profit_excluding_best_year: Decimal
    longest_no_new_high_days: int


@dataclass(frozen=True, slots=True)
class BacktestReport:
    promotion: tuple[GateCheckResult, ...] | None
    promotion_passed: bool | None
    run_config: RunConfigSummary
    data_quality: DataQualityReport
    metrics: BacktestMetrics
    equity_curve: tuple[EquityPoint, ...]
    yearly_returns: dict[str, Decimal]
    monthly_returns: dict[str, Decimal]
    r_multiple_histogram: tuple[RMultipleBucket, ...]
    mae_mfe_points: tuple[MaeMfePoint, ...]
    gate_rejection_counts: tuple[GateRejectionCount, ...]
    concentration: ConcentrationAnalysis
    walk_forward: WalkForwardResult | None
    monte_carlo: MonteCarloResult | None
    parameter_perturbation: dict[str, object] | None = None
    cost_sensitivity: dict[str, object] | None = None


def _r_multiple_histogram(
    trades: list[ResearchTrade], *, bucket_width: Decimal = _R_MULTIPLE_BUCKET_WIDTH
) -> tuple[RMultipleBucket, ...]:
    if not trades:
        return ()
    counts: Counter[int] = Counter()
    for t in trades:
        bucket_index = int((t.r_multiple / bucket_width).to_integral_value(rounding=ROUND_FLOOR))
        counts[bucket_index] += 1
    buckets = []
    for idx in sorted(counts):
        lower = Decimal(idx) * bucket_width
        buckets.append(RMultipleBucket(lower=lower, upper=lower + bucket_width, count=counts[idx]))
    return tuple(buckets)


def _gate_rejection_counts(decisions: list[Decision]) -> tuple[GateRejectionCount, ...]:
    counts: Counter[str] = Counter()
    for d in decisions:
        for gate in d.gates:
            if not gate.passed:
                counts[gate.code.value] += 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return tuple(GateRejectionCount(code=code, count=n) for code, n in ordered)


def build_report(
    *,
    run_config: RunConfigSummary,
    trades: list[ResearchTrade],
    equity_curve: list[EquityPoint],
    decisions: list[Decision],
    data_quality: DataQualityReport,
    metrics: BacktestMetrics,
    walk_forward: WalkForwardResult | None = None,
    monte_carlo: MonteCarloResult | None = None,
    promotion_inputs: PromotionGateInputs | None = None,
    parameter_perturbation: dict[str, object] | None = None,
    cost_sensitivity: dict[str, object] | None = None,
) -> BacktestReport:
    promotion = evaluate_promotion_gates(promotion_inputs) if promotion_inputs is not None else None

    return BacktestReport(
        promotion=promotion,
        promotion_passed=all_gates_passed(promotion) if promotion is not None else None,
        run_config=run_config,
        data_quality=data_quality,
        metrics=metrics,
        equity_curve=tuple(equity_curve),
        yearly_returns=compute_yearly_returns(equity_curve),
        monthly_returns=compute_monthly_returns(equity_curve),
        r_multiple_histogram=_r_multiple_histogram(trades),
        mae_mfe_points=tuple(
            MaeMfePoint(mae=t.mae, mfe=t.mfe, net_pnl=t.net_pnl, exit_reason=t.exit_reason)
            for t in trades
        ),
        gate_rejection_counts=_gate_rejection_counts(decisions),
        concentration=ConcentrationAnalysis(
            top1_pct=metrics.profit_concentration_top1_pct,
            top5_pct=metrics.profit_concentration_top5_pct,
            net_profit_excluding_best_trade=net_profit_excluding_best_trade(trades),
            net_profit_excluding_best_year=net_profit_excluding_best_year(trades),
            longest_no_new_high_days=metrics.longest_no_new_high_days,
        ),
        walk_forward=walk_forward,
        monte_carlo=monte_carlo,
        parameter_perturbation=parameter_perturbation,
        cost_sensitivity=cost_sensitivity,
    )
