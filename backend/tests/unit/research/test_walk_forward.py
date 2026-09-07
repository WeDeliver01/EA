"""SPEC-07 §6 Stage 3 / SPEC-10 Phase 3 acceptance tests for walk-forward
validation.

Mirrors `test_backtester.py`'s approach: a fake engine fires TRADE at exact,
known timestamps so fold boundaries and train/test trade attribution can be
hand-verified, independent of the real pattern-detection pipeline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import AssetClass, Direction, Regime, Timeframe
from app.domain.market.market_state import MarketState
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.risk.limits import RiskLimits
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.engines.config import StrategyConfig
from app.research.backtester import BacktestConfig
from app.research.cost_model import CostModel
from app.research.walk_forward import (
    WalkForwardConfig,
    WalkForwardResult,
    profitable_fold_fraction,
    run_walk_forward,
)

pytestmark = pytest.mark.unit

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
    stops_level_points=10,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)

_LIMITS = RiskLimits(
    risk_per_trade_pct=Decimal("0.01"),
    max_daily_loss_pct=Decimal("0.5"),
    max_weekly_loss_pct=Decimal("0.5"),
    max_open_risk_pct=Decimal("0.5"),
    max_daily_trades=100,
    max_open_positions=10,
    max_positions_per_symbol=10,
    max_correlated_positions=10,
    min_rr=Decimal("0.1"),
    max_spread_multiple_of_atr=Decimal("100"),
    pause_after_consecutive_losses=100,
    pause_duration_minutes=0,
    max_lot_size=Decimal("50"),
)


def _zero_cost_model() -> CostModel:
    return CostModel(
        spread_source="fixed",
        fixed_spread_points=0,
        commission_per_lot_per_side=Decimal("0"),
        swap_long_points=Decimal("0"),
        swap_short_points=Decimal("0"),
        triple_swap_weekday=2,
        slippage_model="none",
        slippage_points=None,
    )


def _bar(t: datetime, *, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        open_time=t,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=100,
        real_volume=None,
        spread_points=0,
    )


_FLAT = {"open_": "3400", "high": "3400.5", "low": "3399.5", "close": "3400"}
_FILL = {"open_": "3400", "high": "3401", "low": "3399", "close": "3400.5"}
_RESOLUTION = {"open_": "3400.5", "high": "3425", "low": "3395", "close": "3410"}


def _master_bars(
    *, start: datetime, total_days: int, entry_times: set[datetime]
) -> tuple[Bar, ...]:
    """A continuous M15 series, flat everywhere except a fill bar and a
    take-profit-reaching resolution bar placed so each `entry_time` lands
    exactly on the fill bar's close_time (SPEC-07 §2 fill convention)."""
    step = timedelta(minutes=15)
    total_bars = total_days * 96
    fill_open_times = {t - step for t in entry_times}
    resolution_open_times = set(entry_times)
    bars = []
    for i in range(total_bars):
        ot = start + step * i
        if ot in fill_open_times:
            shape = _FILL
        elif ot in resolution_open_times:
            shape = _RESOLUTION
        else:
            shape = _FLAT
        bars.append(_bar(ot, **shape))
    return tuple(bars)


def _wait_decision(*, symbol: str, as_of: datetime) -> Decision:
    return Decision(
        outcome=DecisionOutcome.WAIT,
        symbol=symbol,
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
        gates=(),
        narrative="wait",
        engine_duration_ms=0,
    )


def _trade_decision(*, symbol: str, as_of: datetime) -> Decision:
    return Decision(
        outcome=DecisionOutcome.TRADE,
        symbol=symbol,
        as_of=as_of,
        strategy_version_id=uuid.UUID(int=0),
        regime=Regime.TRENDING_UP,
        setup=None,
        direction=Direction.LONG,
        entry=Decimal("3400"),
        stop_loss=Decimal("3390"),
        take_profits=(
            TakeProfit(level=Decimal("3420"), fraction=Decimal("1.0"), r_multiple=Decimal("2.0")),
        ),
        confluence_score=Decimal("9"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="trade",
        engine_duration_ms=0,
    )


class _MultiTriggerFakeEngine:
    """Fires TRADE whenever `state.as_of` is one of `trigger_times`; WAIT
    otherwise. Reused across every fold's own `BacktestRunner`, since
    `run_walk_forward` passes the same `engine` instance to each fold."""

    def __init__(self, *, trigger_times: set[datetime], symbol: str) -> None:
        self._triggers = trigger_times
        self._symbol = symbol

    def evaluate(self, state: MarketState) -> Decision:
        if state.as_of in self._triggers:
            return _trade_decision(symbol=self._symbol, as_of=state.as_of)
        return _wait_decision(symbol=self._symbol, as_of=state.as_of)


_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # a Monday, midnight


def _run(*, entry_offset_days: list[float], config: WalkForwardConfig) -> WalkForwardResult:
    entry_times = {_START + timedelta(days=d) for d in entry_offset_days}
    bars = _master_bars(start=_START, total_days=6, entry_times=entry_times)
    trigger_times = {t - timedelta(minutes=15) for t in entry_times}
    engine = _MultiTriggerFakeEngine(trigger_times=trigger_times, symbol="XAUUSD")
    backtest_config = BacktestConfig(
        strategy_config=StrategyConfig(),
        risk_limits=_LIMITS,
        cost_model=_zero_cost_model(),
        fill_model_name="next_bar_open",
        initial_balance=Decimal("10000"),
        leverage=500,
        margin_safety_factor=Decimal("0.30"),
    )
    return run_walk_forward(
        symbol="XAUUSD",
        spec=_SPEC,
        primary_tf=Timeframe.M15,
        bars_by_timeframe={Timeframe.M15: bars},
        backtest_config=backtest_config,
        config=config,
        engine=engine,
    )


def test_fold_count_and_boundaries_rolling() -> None:
    config = WalkForwardConfig(train_days=2, test_days=1, step_days=1, anchored=False)
    result = _run(entry_offset_days=[], config=config)

    assert [(f.train_start, f.test_start, f.test_end) for f in result.folds] == [
        (_START, _START + timedelta(days=2), _START + timedelta(days=3)),
        (_START + timedelta(days=1), _START + timedelta(days=3), _START + timedelta(days=4)),
        (_START + timedelta(days=2), _START + timedelta(days=4), _START + timedelta(days=5)),
        (_START + timedelta(days=3), _START + timedelta(days=5), _START + timedelta(days=6)),
    ]


def test_anchored_train_start_never_moves() -> None:
    config = WalkForwardConfig(train_days=2, test_days=1, step_days=1, anchored=True)
    result = _run(entry_offset_days=[], config=config)

    assert len(result.folds) == 4
    assert all(f.train_start == _START for f in result.folds)


def test_no_folds_when_dataset_shorter_than_one_train_plus_test_window() -> None:
    config = WalkForwardConfig(train_days=10, test_days=5, step_days=1, anchored=False)
    result = _run(entry_offset_days=[], config=config)
    assert result.folds == ()
    assert result.concatenated_test_trades == ()
    assert result.walk_forward_efficiency is None


def test_empty_dataset_returns_empty_result() -> None:
    config = WalkForwardConfig(train_days=2, test_days=1, step_days=1)
    backtest_config = BacktestConfig(
        strategy_config=StrategyConfig(),
        risk_limits=_LIMITS,
        cost_model=_zero_cost_model(),
        fill_model_name="next_bar_open",
        initial_balance=Decimal("10000"),
    )
    result = run_walk_forward(
        symbol="XAUUSD",
        spec=_SPEC,
        primary_tf=Timeframe.M15,
        bars_by_timeframe={Timeframe.M15: ()},
        backtest_config=backtest_config,
        config=config,
    )
    assert result == run_walk_forward(
        symbol="XAUUSD",
        spec=_SPEC,
        primary_tf=Timeframe.M15,
        bars_by_timeframe={Timeframe.M15: ()},
        backtest_config=backtest_config,
        config=config,
    )
    assert result.folds == ()


def test_trades_attributed_to_train_or_test_by_entry_time() -> None:
    config = WalkForwardConfig(train_days=2, test_days=1, step_days=1, anchored=False)
    # One trade lands in each of the four test windows (2.5d, 3.5d, 4.5d,
    # 5.5d) - each also falls inside the *train* window of every later fold
    # whose train span covers it, since each fold re-slices the same master
    # bar series independently.
    result = _run(entry_offset_days=[2.5, 3.5, 4.5, 5.5], config=config)

    assert len(result.folds) == 4
    fold_a, fold_b, fold_c, fold_d = result.folds

    assert len(fold_a.train_trades) == 0
    assert len(fold_a.test_trades) == 1
    assert fold_a.test_trades[0].entry_time == _START + timedelta(days=2.5)

    assert len(fold_b.train_trades) == 1
    assert fold_b.train_trades[0].entry_time == _START + timedelta(days=2.5)
    assert len(fold_b.test_trades) == 1
    assert fold_b.test_trades[0].entry_time == _START + timedelta(days=3.5)

    assert len(fold_c.train_trades) == 2
    assert len(fold_c.test_trades) == 1
    assert fold_c.test_trades[0].entry_time == _START + timedelta(days=4.5)

    assert len(fold_d.train_trades) == 2
    assert len(fold_d.test_trades) == 1
    assert fold_d.test_trades[0].entry_time == _START + timedelta(days=5.5)

    assert len(result.concatenated_test_trades) == 4
    assert {t.entry_time for t in result.concatenated_test_trades} == {
        _START + timedelta(days=d) for d in (2.5, 3.5, 4.5, 5.5)
    }


def test_walk_forward_efficiency_is_ratio_of_oos_to_is_expectancy() -> None:
    config = WalkForwardConfig(train_days=2, test_days=1, step_days=1, anchored=False)
    result = _run(entry_offset_days=[2.5, 3.5, 4.5, 5.5], config=config)

    # Every trade here is the same hand-computed r=2 zero-cost scenario
    # (see test_backtester.py), so both the aggregate in-sample and
    # out-of-sample expectancy are exactly 2 - efficiency is exactly 1.
    for fold in result.folds:
        for trade in (*fold.train_trades, *fold.test_trades):
            assert trade.r_multiple == Decimal("2")
    assert result.walk_forward_efficiency == Decimal("1")


def test_profitable_fold_fraction_all_profitable() -> None:
    config = WalkForwardConfig(train_days=2, test_days=1, step_days=1, anchored=False)
    result = _run(entry_offset_days=[2.5, 3.5, 4.5, 5.5], config=config)
    assert profitable_fold_fraction(result) == Decimal("1")


def test_profitable_fold_fraction_none_when_no_folds() -> None:
    config = WalkForwardConfig(train_days=10, test_days=5, step_days=1, anchored=False)
    result = _run(entry_offset_days=[], config=config)
    assert profitable_fold_fraction(result) is None
