"""SPEC-10 Phase 3 acceptance: metrics computed on a small, fully
hand-computable synthetic dataset must match the hand-derived values
exactly, not approximately."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.enums import Direction
from app.research.metrics import (
    avg_loss_r,
    avg_win_r,
    compute_metrics,
    concentration,
    drawdown,
    expectancy_r,
    net_profit,
    profit_factor,
    streaks,
    win_rate,
    yearly_returns,
)
from app.research.types import EquityPoint, ResearchTrade

pytestmark = pytest.mark.unit

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _trade(*, net_pnl: str, r_multiple: str, day_offset: int) -> ResearchTrade:
    entry = _START + timedelta(days=day_offset)
    return ResearchTrade(
        entry_time=entry,
        exit_time=entry + timedelta(hours=4),
        direction=Direction.LONG,
        entry_price=Decimal("100"),
        exit_price=Decimal("100") + Decimal(r_multiple),
        volume=Decimal("1"),
        gross_pnl=Decimal(net_pnl),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal(net_pnl),
        risk_amount=Decimal("100"),
        r_multiple=Decimal(r_multiple),
        mae=None,
        mfe=None,
        exit_reason="TAKE_PROFIT" if Decimal(net_pnl) > 0 else "STOP",
    )


# +2R, -1R, +1R, -1R, +3R - a small, fully hand-computable set.
_TRADES = [
    _trade(net_pnl="200", r_multiple="2", day_offset=1),
    _trade(net_pnl="-100", r_multiple="-1", day_offset=2),
    _trade(net_pnl="100", r_multiple="1", day_offset=3),
    _trade(net_pnl="-100", r_multiple="-1", day_offset=4),
    _trade(net_pnl="300", r_multiple="3", day_offset=5),
]

_EQUITY_CURVE = [
    EquityPoint(ts=_START, balance=Decimal("10000"), equity=Decimal("10000")),
    EquityPoint(ts=_START + timedelta(days=1), balance=Decimal("10200"), equity=Decimal("10200")),
    EquityPoint(ts=_START + timedelta(days=2), balance=Decimal("10100"), equity=Decimal("10100")),
    EquityPoint(ts=_START + timedelta(days=3), balance=Decimal("10200"), equity=Decimal("10200")),
    EquityPoint(ts=_START + timedelta(days=4), balance=Decimal("10100"), equity=Decimal("10100")),
    EquityPoint(ts=_START + timedelta(days=5), balance=Decimal("10400"), equity=Decimal("10400")),
]


def test_win_rate() -> None:
    assert win_rate(_TRADES) == Decimal("3") / Decimal("5")


def test_win_rate_is_none_for_no_trades() -> None:
    assert win_rate([]) is None


def test_profit_factor() -> None:
    # gross profit 600, gross loss 200
    assert profit_factor(_TRADES) == Decimal("3.0")


def test_profit_factor_is_none_with_no_losses() -> None:
    wins_only = [t for t in _TRADES if t.net_pnl > 0]
    assert profit_factor(wins_only) is None


def test_expectancy_r() -> None:
    assert expectancy_r(_TRADES) == Decimal("4") / Decimal("5")


def test_avg_win_and_loss_r() -> None:
    assert avg_win_r(_TRADES) == Decimal("2.0")
    assert avg_loss_r(_TRADES) == Decimal("-1.0")


def test_net_profit() -> None:
    assert net_profit(_TRADES) == Decimal("400")


def test_streaks() -> None:
    # win, loss, win, loss, win -> no streak longer than 1
    assert streaks(_TRADES) == (1, 1)


def test_streaks_with_a_real_run() -> None:
    trades = [
        _trade(net_pnl="100", r_multiple="1", day_offset=1),
        _trade(net_pnl="100", r_multiple="1", day_offset=2),
        _trade(net_pnl="100", r_multiple="1", day_offset=3),
        _trade(net_pnl="-100", r_multiple="-1", day_offset=4),
        _trade(net_pnl="-100", r_multiple="-1", day_offset=5),
    ]
    assert streaks(trades) == (3, 2)


def test_drawdown_from_the_equity_curve() -> None:
    result = drawdown(_EQUITY_CURVE)
    assert result.max_drawdown_pct == Decimal("100") / Decimal("10200")


def test_drawdown_with_no_losses_is_zero() -> None:
    rising = [
        EquityPoint(ts=_START, balance=Decimal("10000"), equity=Decimal("10000")),
        EquityPoint(
            ts=_START + timedelta(days=1), balance=Decimal("10100"), equity=Decimal("10100")
        ),
    ]
    result = drawdown(rising)
    assert result.max_drawdown_pct == Decimal("0")


def test_drawdown_with_empty_curve() -> None:
    result = drawdown([])
    assert result.max_drawdown_pct == Decimal("0")
    assert result.max_drawdown_duration_days == 0


def test_concentration() -> None:
    top1, top5 = concentration(_TRADES)
    assert top1 == Decimal("300") / Decimal("400")
    assert top5 == Decimal("300") / Decimal("400")  # 5% of 5 trades rounds to 1


def test_concentration_is_none_when_net_profit_is_not_positive() -> None:
    losing = [_trade(net_pnl="-100", r_multiple="-1", day_offset=1)]
    assert concentration(losing) == (None, None)


def test_yearly_returns_single_year() -> None:
    returns = yearly_returns(_EQUITY_CURVE)
    assert list(returns.keys()) == ["2026"]
    assert returns["2026"] == (Decimal("10400") - Decimal("10000")) / Decimal("10000")


def test_yearly_returns_across_two_years() -> None:
    curve = [
        EquityPoint(
            ts=datetime(2025, 6, 1, tzinfo=UTC), balance=Decimal("10000"), equity=Decimal("10000")
        ),
        EquityPoint(
            ts=datetime(2025, 12, 31, tzinfo=UTC), balance=Decimal("11000"), equity=Decimal("11000")
        ),
        EquityPoint(
            ts=datetime(2026, 1, 1, tzinfo=UTC), balance=Decimal("11000"), equity=Decimal("11000")
        ),
        EquityPoint(
            ts=datetime(2026, 6, 1, tzinfo=UTC), balance=Decimal("9900"), equity=Decimal("9900")
        ),
    ]
    returns = yearly_returns(curve)
    assert returns["2025"] == Decimal("1000") / Decimal("10000")
    assert returns["2026"] == (Decimal("9900") - Decimal("11000")) / Decimal("11000")


def test_compute_metrics_end_to_end() -> None:
    metrics = compute_metrics(_TRADES, _EQUITY_CURVE, period_years=1.0)
    assert metrics.trade_count == 5
    assert metrics.win_rate == Decimal("3") / Decimal("5")
    assert metrics.profit_factor == Decimal("3.0")
    assert metrics.net_profit == Decimal("400")
    assert metrics.longest_win_streak == 1
    assert metrics.longest_loss_streak == 1
    assert metrics.profitable_years == 1
    assert metrics.total_years == 1
    assert metrics.trades_per_year == Decimal("5")


def test_compute_metrics_with_no_trades_does_not_crash() -> None:
    metrics = compute_metrics([], [], period_years=1.0)
    assert metrics.trade_count == 0
    assert metrics.win_rate is None
    assert metrics.net_profit == Decimal("0")
