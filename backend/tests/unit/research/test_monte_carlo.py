"""SPEC-07 §6 Stage 5 / SPEC-10 Phase 3 acceptance tests for Monte Carlo
simulation.

A single-trade pool makes every iteration's resample degenerate (bootstrap
resampling from one element always yields that element, and trade removal
never drops the last one - `max(1, ...)`), which turns an otherwise
inherently random process into something exactly hand-computable: every one
of the 5th/50th/95th percentiles collapses to the same single number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.enums import Direction
from app.research.monte_carlo import (
    MonteCarloConfig,
    _percentile,
    run_monte_carlo,
)
from app.research.types import ResearchTrade

pytestmark = pytest.mark.unit

_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _trade(
    *, net_pnl: Decimal, entry_offset_hours: int = 0, exit_offset_hours: int = 1
) -> ResearchTrade:
    entry = _START + timedelta(hours=entry_offset_hours)
    return ResearchTrade(
        entry_time=entry,
        exit_time=entry + timedelta(hours=exit_offset_hours),
        direction=Direction.LONG,
        entry_price=Decimal("3400"),
        exit_price=Decimal("3400") + net_pnl,
        volume=Decimal("0.1"),
        gross_pnl=net_pnl,
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=net_pnl,
        risk_amount=Decimal("100"),
        r_multiple=net_pnl / Decimal("100"),
        mae=None,
        mfe=None,
        exit_reason="PARTIAL_TP" if net_pnl > 0 else "STOP",
    )


def test_raises_on_zero_trades() -> None:
    with pytest.raises(ValueError, match="zero trades"):
        run_monte_carlo([], initial_balance=Decimal("10000"))


def test_single_winning_trade_is_fully_degenerate() -> None:
    trades = [_trade(net_pnl=Decimal("500"))]
    result = run_monte_carlo(
        trades, initial_balance=Decimal("10000"), config=MonteCarloConfig(iterations=50, seed=1)
    )

    assert result.iterations == 50
    assert (
        result.final_equity.p5
        == result.final_equity.p50
        == result.final_equity.p95
        == Decimal("10500")
    )
    assert result.max_drawdown_pct.p5 == result.max_drawdown_pct.p50 == Decimal("0")
    assert result.max_drawdown_pct.p95 == Decimal("0")
    # No losing trades ever appear -> profit factor is undefined every
    # iteration -> the aggregate is None, not zero.
    assert result.profit_factor is None


def test_single_losing_trade_is_fully_degenerate() -> None:
    trades = [_trade(net_pnl=Decimal("-300"))]
    result = run_monte_carlo(
        trades, initial_balance=Decimal("10000"), config=MonteCarloConfig(iterations=50, seed=1)
    )

    assert (
        result.final_equity.p5
        == result.final_equity.p50
        == result.final_equity.p95
        == Decimal("9700")
    )
    assert result.max_drawdown_pct.p5 == result.max_drawdown_pct.p50 == result.max_drawdown_pct.p95
    assert result.max_drawdown_pct.p50 == Decimal("0.03")
    assert result.profit_factor is not None
    assert (
        result.profit_factor.p5
        == result.profit_factor.p50
        == result.profit_factor.p95
        == Decimal("0")
    )


def test_deterministic_same_seed_same_result() -> None:
    trades = [
        _trade(net_pnl=Decimal(v), entry_offset_hours=i)
        for i, v in enumerate([200, -100, 300, -50, 150])
    ]
    config = MonteCarloConfig(iterations=200, seed=42)
    result1 = run_monte_carlo(trades, initial_balance=Decimal("10000"), config=config)
    result2 = run_monte_carlo(trades, initial_balance=Decimal("10000"), config=config)
    assert result1 == result2


def test_different_seed_can_produce_a_different_result() -> None:
    trades = [
        _trade(net_pnl=Decimal(v), entry_offset_hours=i)
        for i, v in enumerate([200, -100, 300, -50, 150])
    ]
    result_a = run_monte_carlo(
        trades, initial_balance=Decimal("10000"), config=MonteCarloConfig(iterations=200, seed=1)
    )
    result_b = run_monte_carlo(
        trades, initial_balance=Decimal("10000"), config=MonteCarloConfig(iterations=200, seed=2)
    )
    assert result_a != result_b


def test_percentiles_are_non_decreasing() -> None:
    trades = [
        _trade(net_pnl=Decimal(v), entry_offset_hours=i)
        for i, v in enumerate([200, -100, 300, -50, 150, -400, 600])
    ]
    result = run_monte_carlo(
        trades, initial_balance=Decimal("10000"), config=MonteCarloConfig(iterations=500, seed=7)
    )
    assert result.final_equity.p5 <= result.final_equity.p50 <= result.final_equity.p95
    assert result.max_drawdown_pct.p5 <= result.max_drawdown_pct.p50 <= result.max_drawdown_pct.p95
    assert result.profit_factor is not None
    assert result.profit_factor.p5 <= result.profit_factor.p50 <= result.profit_factor.p95


def test_full_trade_removal_still_keeps_at_least_one_trade() -> None:
    trades = [
        _trade(net_pnl=Decimal(v), entry_offset_hours=i)
        for i, v in enumerate([100, 200, 300, 400, 500])
    ]
    config = MonteCarloConfig(iterations=100, trade_removal_fraction=Decimal("1.0"), seed=3)
    result = run_monte_carlo(trades, initial_balance=Decimal("10000"), config=config)

    possible_final_equities = {Decimal("10000") + t.net_pnl for t in trades}
    # Every iteration keeps exactly one (bootstrap-resampled) trade, so the
    # final equity of every percentile must be one of the five possible
    # single-trade outcomes.
    assert result.final_equity.p5 in possible_final_equities
    assert result.final_equity.p50 in possible_final_equities
    assert result.final_equity.p95 in possible_final_equities


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        (
            [Decimal(0), Decimal(10), Decimal(20), Decimal(30), Decimal(40)],
            Decimal("0.05"),
            Decimal(0),
        ),
        (
            [Decimal(0), Decimal(10), Decimal(20), Decimal(30), Decimal(40)],
            Decimal("0.50"),
            Decimal(20),
        ),
        (
            [Decimal(0), Decimal(10), Decimal(20), Decimal(30), Decimal(40)],
            Decimal("0.95"),
            Decimal(40),
        ),
        (
            [Decimal(0), Decimal(10), Decimal(20)],
            Decimal("0.25"),
            Decimal(10),
        ),  # rank 0.5 rounds up
        ([Decimal(5)], Decimal("0.50"), Decimal(5)),
    ],
)
def test_percentile_nearest_rank_with_half_up_rounding(
    values: list[Decimal], p: Decimal, expected: Decimal
) -> None:
    assert _percentile(values, p) == expected
