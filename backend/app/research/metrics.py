"""SPEC-02 §8 / SPEC-07 §9: backtest metrics.

Money, R-multiples and drawdown percentages stay `Decimal` throughout (P8).
Sharpe/Sortino/Calmar are derived statistics, not balances or risk amounts,
so they're computed in `float` from a list of period returns - consistent
with "floats are permitted only inside indicator maths ... never on account
balances, risk amounts, or volumes" (SPEC-00 §2, P8).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from typing import Any

from app.research.types import EquityPoint, ResearchTrade

_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True, slots=True)
class DrawdownResult:
    max_drawdown_pct: Decimal
    max_drawdown_duration_days: int
    longest_no_new_high_days: int


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    trade_count: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    expectancy_r: Decimal | None
    avg_win_r: Decimal | None
    avg_loss_r: Decimal | None
    net_profit: Decimal
    max_drawdown_pct: Decimal
    max_drawdown_duration_days: int
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    longest_loss_streak: int
    longest_win_streak: int
    trades_per_year: Decimal | None
    profit_concentration_top1_pct: Decimal | None
    profit_concentration_top5_pct: Decimal | None
    best_year_pct: Decimal | None
    worst_year_pct: Decimal | None
    profitable_years: int
    total_years: int
    longest_no_new_high_days: int
    yearly_returns: dict[str, Decimal] = field(default_factory=dict)
    regime_breakdown: dict[str, Any] = field(default_factory=dict)
    session_breakdown: dict[str, Any] = field(default_factory=dict)


def win_rate(trades: list[ResearchTrade]) -> Decimal | None:
    if not trades:
        return None
    wins = sum(1 for t in trades if t.net_pnl > 0)
    return Decimal(wins) / Decimal(len(trades))


def profit_factor(trades: list[ResearchTrade]) -> Decimal | None:
    gross_profit = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal(0))
    gross_loss = sum((-t.net_pnl for t in trades if t.net_pnl < 0), Decimal(0))
    if gross_loss == 0:
        return None  # undefined: no losses to divide by
    return gross_profit / gross_loss


def expectancy_r(trades: list[ResearchTrade]) -> Decimal | None:
    if not trades:
        return None
    return sum((t.r_multiple for t in trades), Decimal(0)) / len(trades)


def avg_win_r(trades: list[ResearchTrade]) -> Decimal | None:
    wins = [t.r_multiple for t in trades if t.net_pnl > 0]
    return sum(wins, Decimal(0)) / len(wins) if wins else None


def avg_loss_r(trades: list[ResearchTrade]) -> Decimal | None:
    losses = [t.r_multiple for t in trades if t.net_pnl < 0]
    return sum(losses, Decimal(0)) / len(losses) if losses else None


def net_profit(trades: list[ResearchTrade]) -> Decimal:
    return sum((t.net_pnl for t in trades), Decimal(0))


def streaks(trades: list[ResearchTrade]) -> tuple[int, int]:
    """(longest_win_streak, longest_loss_streak), trades taken in order."""
    longest_win = longest_loss = 0
    current_win = current_loss = 0
    for t in trades:
        if t.net_pnl > 0:
            current_win += 1
            current_loss = 0
        elif t.net_pnl < 0:
            current_loss += 1
            current_win = 0
        else:
            current_win = current_loss = 0
        longest_win = max(longest_win, current_win)
        longest_loss = max(longest_loss, current_loss)
    return longest_win, longest_loss


def drawdown(equity_curve: list[EquityPoint]) -> DrawdownResult:
    if not equity_curve:
        return DrawdownResult(Decimal(0), 0, 0)

    peak = equity_curve[0].equity
    peak_ts = equity_curve[0].ts
    max_dd = Decimal(0)
    max_dd_duration_days = 0
    max_no_new_high_days = 0

    for point in equity_curve:
        if point.equity >= peak:
            peak = point.equity
            peak_ts = point.ts
        else:
            dd_pct = (peak - point.equity) / peak if peak > 0 else Decimal(0)
            max_dd = max(max_dd, dd_pct)
        duration_days = (point.ts - peak_ts).days
        max_dd_duration_days = max(max_dd_duration_days, duration_days)
        max_no_new_high_days = max(max_no_new_high_days, duration_days)

    return DrawdownResult(max_dd, max_dd_duration_days, max_no_new_high_days)


def _period_returns(equity_curve: list[EquityPoint]) -> list[float]:
    returns: list[float] = []
    for prev, curr in pairwise(equity_curve):
        if prev.equity == 0:
            continue
        returns.append(float((curr.equity - prev.equity) / prev.equity))
    return returns


def sharpe_ratio(equity_curve: list[EquityPoint], *, risk_free_rate: float = 0.0) -> float | None:
    returns = _period_returns(equity_curve)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return (mean - risk_free_rate) / std * math.sqrt(_TRADING_DAYS_PER_YEAR)


def sortino_ratio(equity_curve: list[EquityPoint], *, risk_free_rate: float = 0.0) -> float | None:
    returns = _period_returns(equity_curve)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    downside = [min(r, 0.0) ** 2 for r in returns]
    downside_variance = sum(downside) / len(returns)
    downside_std = math.sqrt(downside_variance)
    if downside_std == 0:
        return None
    return (mean - risk_free_rate) / downside_std * math.sqrt(_TRADING_DAYS_PER_YEAR)


def calmar_ratio(equity_curve: list[EquityPoint], *, years: float) -> float | None:
    if not equity_curve or years <= 0:
        return None
    start_equity = equity_curve[0].equity
    end_equity = equity_curve[-1].equity
    if start_equity <= 0:
        return None
    cagr = float((end_equity / start_equity) ** (Decimal(1) / Decimal(str(years))) - 1)
    dd = drawdown(equity_curve)
    if dd.max_drawdown_pct == 0:
        return None
    return cagr / float(dd.max_drawdown_pct)


def concentration(trades: list[ResearchTrade]) -> tuple[Decimal | None, Decimal | None]:
    """(top1_pct, top5_pct): the share of total net profit contributed by
    the single best trade and the best 5 percent of trades. A promotion
    gate, not a vanity metric (SPEC-02 §8)."""
    total = net_profit(trades)
    if total <= 0 or not trades:
        return None, None
    sorted_pnl = sorted((t.net_pnl for t in trades), reverse=True)
    top1 = sorted_pnl[0] / total
    top5_count = max(1, round(len(sorted_pnl) * 0.05))
    top5 = sum(sorted_pnl[:top5_count], Decimal(0)) / total
    return top1, top5


def net_profit_excluding_best_trade(trades: list[ResearchTrade]) -> Decimal:
    """SPEC-07 §6 Stage 6: net profit with the single best trade removed."""
    if not trades:
        return Decimal(0)
    best = max(trades, key=lambda t: t.net_pnl)
    return net_profit([t for t in trades if t is not best])


def net_profit_excluding_best_year(trades: list[ResearchTrade]) -> Decimal:
    """SPEC-07 §6 Stage 6: net profit with the best calendar year removed,
    keyed by each trade's exit year."""
    if not trades:
        return Decimal(0)
    by_year: dict[int, Decimal] = defaultdict(Decimal)
    for t in trades:
        by_year[t.exit_time.year] += t.net_pnl
    best_year = max(by_year, key=lambda year: by_year[year])
    return net_profit([t for t in trades if t.exit_time.year != best_year])


def monthly_returns(equity_curve: list[EquityPoint]) -> dict[str, Decimal]:
    by_month_first: dict[str, Decimal] = {}
    by_month_last: dict[str, Decimal] = {}
    for point in equity_curve:
        key = f"{point.ts.year:04d}-{point.ts.month:02d}"
        if key not in by_month_first:
            by_month_first[key] = point.equity
        by_month_last[key] = point.equity

    result: dict[str, Decimal] = {}
    for key in sorted(by_month_first):
        start = by_month_first[key]
        end = by_month_last[key]
        result[key] = (end - start) / start if start > 0 else Decimal(0)
    return result


def yearly_returns(equity_curve: list[EquityPoint]) -> dict[str, Decimal]:
    by_year_first: dict[int, Decimal] = {}
    by_year_last: dict[int, Decimal] = {}
    for point in equity_curve:
        year = point.ts.year
        if year not in by_year_first:
            by_year_first[year] = point.equity
        by_year_last[year] = point.equity

    result: dict[str, Decimal] = {}
    for year in sorted(by_year_first):
        start = by_year_first[year]
        end = by_year_last[year]
        result[str(year)] = (end - start) / start if start > 0 else Decimal(0)
    return result


def _breakdown_by(trades: list[ResearchTrade], key: str) -> dict[str, Any]:
    groups: dict[str, list[ResearchTrade]] = defaultdict(list)
    for t in trades:
        value = getattr(t, key)
        label = value.value if value is not None else "UNKNOWN"
        groups[label].append(t)
    return {
        label: {
            "trade_count": len(group_trades),
            "win_rate": str(win_rate(group_trades)) if win_rate(group_trades) is not None else None,
            "expectancy_r": str(expectancy_r(group_trades))
            if expectancy_r(group_trades) is not None
            else None,
            "net_profit": str(net_profit(group_trades)),
        }
        for label, group_trades in groups.items()
    }


def compute_metrics(
    trades: list[ResearchTrade], equity_curve: list[EquityPoint], *, period_years: float
) -> BacktestMetrics:
    dd = drawdown(equity_curve)
    win_streak, loss_streak = streaks(trades)
    top1, top5 = concentration(trades)
    years = yearly_returns(equity_curve)
    year_values = list(years.values())

    return BacktestMetrics(
        trade_count=len(trades),
        win_rate=win_rate(trades),
        profit_factor=profit_factor(trades),
        expectancy_r=expectancy_r(trades),
        avg_win_r=avg_win_r(trades),
        avg_loss_r=avg_loss_r(trades),
        net_profit=net_profit(trades),
        max_drawdown_pct=dd.max_drawdown_pct,
        max_drawdown_duration_days=dd.max_drawdown_duration_days,
        sharpe=sharpe_ratio(equity_curve),
        sortino=sortino_ratio(equity_curve),
        calmar=calmar_ratio(equity_curve, years=period_years),
        longest_loss_streak=loss_streak,
        longest_win_streak=win_streak,
        trades_per_year=(Decimal(len(trades)) / Decimal(str(period_years)))
        if period_years > 0
        else None,
        profit_concentration_top1_pct=top1,
        profit_concentration_top5_pct=top5,
        best_year_pct=max(year_values) if year_values else None,
        worst_year_pct=min(year_values) if year_values else None,
        profitable_years=sum(1 for v in year_values if v > 0),
        total_years=len(year_values),
        longest_no_new_high_days=dd.longest_no_new_high_days,
        yearly_returns=years,
        regime_breakdown=_breakdown_by(trades, "regime"),
        session_breakdown=_breakdown_by(trades, "session"),
    )
