"""SPEC-07 §6 Stage 5: Monte Carlo simulation.

Operates on an already-computed `list[ResearchTrade]` from a single
backtest run, not on raw bars - it resamples and reorders trades that
already happened rather than re-simulating fills. Of the five independent
randomisation axes SPEC-07 §6 Stage 5 lists, two are implemented exactly as
specified because they are trade-list operations:

- **Trade order**: each iteration draws a bootstrap resample (with
  replacement) from the trade pool, in the drawn order - order is not
  restored to chronological, since varying it is the entire point (it is
  what produces the drawdown distribution).
- **Trade removal**: each iteration then drops a random
  `trade_removal_fraction` of its resampled trades.

**Start date** is approximated: each iteration's resample pool starts from
a trade drawn at random from the first `first_year_days` days of the
dataset, rather than from a genuine bar-level restart. **Entry timing
shift** and **slippage resampling** are not implemented - both need a full
backtest re-simulation per iteration (jittered fill timing and a live
slippage distribution this MVP doesn't have), which is a materially larger
piece of work than a trade-level resample; this is documented as a
deviation in `docs/adr/0001-mvp-scope.md`.

Randomness here is legitimate and required (research/ is not engines/, so
the purity guard doesn't apply) but is always seeded, so a given
`(trades, config)` pair reproduces identical percentiles run to run -
determinism at the boundary that actually needs it, even though the
underlying process is a simulation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.research.types import ResearchTrade

_DEFAULT_ITERATIONS = 2000
_FIRST_YEAR_DAYS = 365


@dataclass(frozen=True, slots=True)
class MonteCarloConfig:
    iterations: int = _DEFAULT_ITERATIONS
    trade_removal_fraction: Decimal = Decimal("0.10")
    seed: int = 0


@dataclass(frozen=True, slots=True)
class PercentileStats:
    p5: Decimal
    p50: Decimal
    p95: Decimal


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    iterations: int
    final_equity: PercentileStats
    max_drawdown_pct: PercentileStats
    profit_factor: PercentileStats | None


def _percentile(values: list[Decimal], p: Decimal) -> Decimal:
    ordered = sorted(values)
    rank = (p * Decimal(len(ordered) - 1)).to_integral_value(rounding=ROUND_HALF_UP)
    index = min(len(ordered) - 1, max(0, int(rank)))
    return ordered[index]


def _stats(values: list[Decimal]) -> PercentileStats:
    return PercentileStats(
        p5=_percentile(values, Decimal("0.05")),
        p50=_percentile(values, Decimal("0.50")),
        p95=_percentile(values, Decimal("0.95")),
    )


def _max_drawdown_pct(initial_balance: Decimal, ordered_trades: list[ResearchTrade]) -> Decimal:
    balance = initial_balance
    peak = initial_balance
    max_dd = Decimal(0)
    for trade in ordered_trades:
        balance += trade.net_pnl
        if balance > peak:
            peak = balance
        elif peak > 0:
            max_dd = max(max_dd, (peak - balance) / peak)
    return max_dd


def _profit_factor(ordered_trades: list[ResearchTrade]) -> Decimal | None:
    gross_profit = sum((t.net_pnl for t in ordered_trades if t.net_pnl > 0), Decimal(0))
    gross_loss = sum((-t.net_pnl for t in ordered_trades if t.net_pnl < 0), Decimal(0))
    return gross_profit / gross_loss if gross_loss > 0 else None


def run_monte_carlo(
    trades: list[ResearchTrade],
    *,
    initial_balance: Decimal,
    config: MonteCarloConfig | None = None,
) -> MonteCarloResult:
    if not trades:
        raise ValueError("cannot run Monte Carlo simulation with zero trades")
    config = config or MonteCarloConfig()

    sorted_trades = sorted(trades, key=lambda t: t.entry_time)
    dataset_start = sorted_trades[0].entry_time
    first_year_indices = [
        i
        for i, t in enumerate(sorted_trades)
        if (t.entry_time - dataset_start).days <= _FIRST_YEAR_DAYS
    ] or [0]

    rng = random.Random(config.seed)
    keep_fraction = Decimal(1) - config.trade_removal_fraction

    final_equities: list[Decimal] = []
    drawdowns: list[Decimal] = []
    profit_factors: list[Decimal] = []

    for _ in range(config.iterations):
        start_index = rng.choice(first_year_indices)
        pool = sorted_trades[start_index:] or sorted_trades

        resampled = [rng.choice(pool) for _ in range(len(pool))]
        keep_count = max(1, round(len(resampled) * float(keep_fraction)))
        kept = resampled if keep_count >= len(resampled) else rng.sample(resampled, keep_count)

        balance = initial_balance
        for trade in kept:
            balance += trade.net_pnl
        final_equities.append(balance)
        drawdowns.append(_max_drawdown_pct(initial_balance, kept))
        pf = _profit_factor(kept)
        if pf is not None:
            profit_factors.append(pf)

    return MonteCarloResult(
        iterations=config.iterations,
        final_equity=_stats(final_equities),
        max_drawdown_pct=_stats(drawdowns),
        profit_factor=_stats(profit_factors) if profit_factors else None,
    )
