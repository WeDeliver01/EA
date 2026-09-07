"""SPEC-07 §6 Stage 3: walk-forward validation.

Each fold gets its own independent `BacktestRunner` over `[train_start,
test_end)` so a later fold's balance and indicator context never leak in
from an earlier one. This deviates from a textbook rolling-optimiser
walk-forward in one respect, documented in `docs/adr/0001-mvp-scope.md`:
there is no parameter optimiser in this MVP, so "optimise on train" (SPEC-07
§6 Stage 3, itself phrased as "if optimising at all") is a no-op - every
fold evaluates the same fixed `StrategyConfig` on both its train and test
windows. The efficiency ratio this module reports still measures in-sample
vs out-of-sample drift; it just isn't measuring drift introduced by fitting
parameters to the train window, because nothing here fits parameters.

Trades are attributed to a fold's train or test half by `entry_time`
relative to the fold's `test_start`, per the SPEC-07 §2 convention that a
trade's `entry_time` is its fill time (the bar after the triggering
decision), not the decision time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.domain.market.symbol_spec import SymbolSpec
from app.research.backtester import BacktestConfig, BacktestRunner
from app.research.metrics import expectancy_r, net_profit
from app.research.types import ResearchTrade


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_days: int = 730
    test_days: int = 182
    step_days: int = 182
    anchored: bool = False


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    train_start: datetime
    test_start: datetime
    test_end: datetime
    train_trades: tuple[ResearchTrade, ...]
    test_trades: tuple[ResearchTrade, ...]
    train_expectancy_r: Decimal | None
    test_expectancy_r: Decimal | None


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    folds: tuple[WalkForwardFold, ...]
    concatenated_test_trades: tuple[ResearchTrade, ...]
    walk_forward_efficiency: Decimal | None


def _slice_bars(bars: tuple[Bar, ...], start: datetime, end: datetime) -> tuple[Bar, ...]:
    return tuple(b for b in bars if start <= b.open_time < end)


def profitable_fold_fraction(result: WalkForwardResult) -> Decimal | None:
    """Fraction of test windows with positive net profit - the input to the
    'profitable periods >= 60 percent of test windows' promotion gate."""
    if not result.folds:
        return None
    profitable = sum(1 for f in result.folds if net_profit(list(f.test_trades)) > 0)
    return Decimal(profitable) / Decimal(len(result.folds))


def run_walk_forward(
    *,
    symbol: str,
    spec: SymbolSpec,
    primary_tf: Timeframe,
    bars_by_timeframe: dict[Timeframe, tuple[Bar, ...]],
    backtest_config: BacktestConfig,
    config: WalkForwardConfig | None = None,
    engine: object | None = None,
) -> WalkForwardResult:
    """`engine` is the same test-only injection seam `BacktestRunner`
    accepts - a stand-in engine reused across every fold's own runner."""
    config = config or WalkForwardConfig()
    primary_bars = bars_by_timeframe.get(primary_tf, ())
    if not primary_bars:
        return WalkForwardResult(
            folds=(), concatenated_test_trades=(), walk_forward_efficiency=None
        )

    dataset_start = primary_bars[0].open_time
    dataset_end = primary_bars[-1].close_time

    train_span = timedelta(days=config.train_days)
    test_span = timedelta(days=config.test_days)
    step = timedelta(days=config.step_days)

    folds: list[WalkForwardFold] = []
    test_start = dataset_start + train_span
    while test_start + test_span <= dataset_end:
        train_start = dataset_start if config.anchored else test_start - train_span
        test_end = test_start + test_span

        fold_bars = {
            tf: _slice_bars(series, train_start, test_end)
            for tf, series in bars_by_timeframe.items()
        }
        runner = BacktestRunner(backtest_config, engine=engine)  # type: ignore[arg-type]
        result = runner.run(
            symbol=symbol, spec=spec, primary_tf=primary_tf, bars_by_timeframe=fold_bars
        )
        train_trades = tuple(t for t in result.trades if t.entry_time < test_start)
        test_trades = tuple(t for t in result.trades if t.entry_time >= test_start)
        folds.append(
            WalkForwardFold(
                train_start=train_start,
                test_start=test_start,
                test_end=test_end,
                train_trades=train_trades,
                test_trades=test_trades,
                train_expectancy_r=expectancy_r(list(train_trades)),
                test_expectancy_r=expectancy_r(list(test_trades)),
            )
        )
        test_start = test_start + step

    concatenated_test_trades = tuple(t for fold in folds for t in fold.test_trades)
    all_train_trades = [t for fold in folds for t in fold.train_trades]
    is_expectancy = expectancy_r(all_train_trades)
    oos_expectancy = expectancy_r(list(concatenated_test_trades))
    efficiency = (
        oos_expectancy / is_expectancy
        if is_expectancy is not None and is_expectancy != 0 and oos_expectancy is not None
        else None
    )

    return WalkForwardResult(
        folds=tuple(folds),
        concatenated_test_trades=concatenated_test_trades,
        walk_forward_efficiency=efficiency,
    )
