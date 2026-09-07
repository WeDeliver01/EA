"""SPEC-07 §2 / SPEC-10 Phase 3 acceptance tests.

A `FakeEngine` stands in for the real `StrategyEngine` (already exhaustively
tested in `tests/golden` and `tests/unit/engines`) so these tests can pin
exact, hand-computable numbers for the backtester's own mechanics - fill
timing, cost application, position simulation - without depending on the
pattern-detection pipeline firing on a specific synthetic bar shape.
"""

from __future__ import annotations

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
from app.research.backtester import BacktestConfig, BacktestResult, BacktestRunner, _closed_prefix
from app.research.cost_model import CostModel

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


def _zero_cost_model(**overrides: object) -> CostModel:
    defaults: dict[str, object] = {
        "spread_source": "fixed",
        "fixed_spread_points": 0,
        "commission_per_lot_per_side": Decimal("0"),
        "swap_long_points": Decimal("0"),
        "swap_short_points": Decimal("0"),
        "triple_swap_weekday": 2,
        "slippage_model": "none",
        "slippage_points": None,
    }
    defaults.update(overrides)
    return CostModel(**defaults)  # type: ignore[arg-type]


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


def _make_bars(*, filler_count: int, start: datetime) -> tuple[tuple[Bar, ...], datetime]:
    """`filler_count` flat bars, then a decision bar, a fill bar (narrow
    range - no immediate stop/TP), then a resolution bar whose range reaches
    the 3420 take-profit but not the 3390 stop. Returns (bars,
    decision_bar_open_time)."""
    bars = []
    for i in range(filler_count):
        t = start + timedelta(minutes=15 * i)
        bars.append(_bar(t, open_="3400", high="3400.5", low="3399.5", close="3400"))

    decision_bar_time = start + timedelta(minutes=15 * filler_count)
    bars.append(_bar(decision_bar_time, open_="3400", high="3400.5", low="3399.5", close="3400"))

    fill_bar_time = decision_bar_time + timedelta(minutes=15)
    bars.append(_bar(fill_bar_time, open_="3400", high="3401", low="3399", close="3400.5"))

    resolution_bar_time = fill_bar_time + timedelta(minutes=15)
    bars.append(_bar(resolution_bar_time, open_="3400.5", high="3425", low="3395", close="3410"))

    return tuple(bars), decision_bar_time


def _wait_decision(*, symbol: str, as_of: datetime) -> Decision:
    import uuid

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
    import uuid

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


class FakeEngine:
    """Returns TRADE exactly once, at `trigger_as_of`; WAIT everywhere else."""

    def __init__(self, *, trigger_as_of: datetime, symbol: str) -> None:
        self._trigger_as_of = trigger_as_of
        self._symbol = symbol
        self.seen: list[MarketState] = []

    def evaluate(self, state: MarketState) -> Decision:
        self.seen.append(state)
        if state.as_of == self._trigger_as_of:
            return _trade_decision(symbol=self._symbol, as_of=state.as_of)
        return _wait_decision(symbol=self._symbol, as_of=state.as_of)


def _run(cost_model: CostModel, *, start: datetime) -> tuple[BacktestResult, FakeEngine]:
    bars, decision_bar_time = _make_bars(filler_count=10, start=start)
    engine = FakeEngine(trigger_as_of=decision_bar_time + timedelta(minutes=15), symbol="XAUUSD")
    # FakeEngine keys off close_time, which for a decision bar equals its
    # own open_time + 15 minutes - i.e. decision_bar_time + 15m.
    config = BacktestConfig(
        strategy_config=StrategyConfig(),
        risk_limits=_LIMITS,
        cost_model=cost_model,
        fill_model_name="next_bar_open",
        initial_balance=Decimal("10000"),
        leverage=500,
        margin_safety_factor=Decimal("0.30"),
    )
    runner = BacktestRunner(config, engine=engine)  # type: ignore[arg-type]
    result = runner.run(
        symbol="XAUUSD",
        spec=_SPEC,
        primary_tf=Timeframe.M15,
        bars_by_timeframe={Timeframe.M15: bars},
    )
    return result, engine


_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # a Monday


def test_hand_computed_trade_outcome_with_zero_costs() -> None:
    result, _ = _run(_zero_cost_model(), start=_START)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.direction == Direction.LONG
    assert trade.entry_price == Decimal("3400")  # bar.open + 0 spread + 0 slippage
    assert trade.exit_price == Decimal("3420")  # the take-profit level
    assert trade.volume == Decimal("0.10")
    assert trade.gross_pnl == Decimal("200")  # (3420-3400)/0.01 ticks * $1.00/tick * 0.1 lots
    assert trade.commission == Decimal("0")
    assert trade.swap == Decimal("0")
    assert trade.net_pnl == Decimal("200")
    assert trade.risk_amount == Decimal("100")  # 1000 ticks * $1.00 * 0.1 lots
    assert trade.r_multiple == Decimal("2")
    assert trade.exit_reason == "PARTIAL_TP"
    assert result.final_balance == Decimal("10200")


def test_costs_change_net_pnl_by_exactly_the_expected_amount() -> None:
    zero_cost_result, _ = _run(_zero_cost_model(), start=_START)
    priced_result, _ = _run(
        _zero_cost_model(commission_per_lot_per_side=Decimal("5")), start=_START
    )

    zero_trade = zero_cost_result.trades[0]
    priced_trade = priced_result.trades[0]

    # Same fill prices (spread and slippage unchanged) -> identical gross P&L.
    assert zero_trade.gross_pnl == priced_trade.gross_pnl
    # Commission charged once on entry (0.1 lot) and once on exit (0.1 lot).
    expected_commission = Decimal("0.1") * Decimal("5") * 2
    assert priced_trade.commission == -expected_commission
    assert zero_trade.net_pnl - priced_trade.net_pnl == expected_commission


def test_entry_fills_on_the_bar_after_the_decision_not_the_decision_bar() -> None:
    result, engine = _run(_zero_cost_model(), start=_START)
    trade = result.trades[0]
    # Recorded entry_time is the *fill* bar's close_time (one bar after the
    # decision bar, per SPEC-07 §2 step 7), not the decision bar itself.
    assert trade.entry_time == engine._trigger_as_of + timedelta(minutes=15)


def test_determinism_same_inputs_produce_identical_trades_and_equity_curve() -> None:
    result1, _ = _run(_zero_cost_model(), start=_START)
    result2, _ = _run(_zero_cost_model(), start=_START)

    assert result1.trades == result2.trades
    assert result1.equity_curve == result2.equity_curve
    assert result1.final_balance == result2.final_balance


def test_closed_prefix_never_includes_a_bar_that_has_not_closed() -> None:
    bars, _ = _make_bars(filler_count=5, start=_START)
    cutoff = bars[3].close_time
    prefix = _closed_prefix(bars, cutoff)
    assert all(b.close_time <= cutoff for b in prefix)
    assert prefix == bars[:4]


def test_closed_prefix_excludes_a_forming_bar_at_the_boundary() -> None:
    bars, _ = _make_bars(filler_count=5, start=_START)
    # One instant before the 4th bar closes: only the first 3 are included.
    cutoff = bars[3].close_time - timedelta(seconds=1)
    prefix = _closed_prefix(bars, cutoff)
    assert prefix == bars[:3]


def test_build_state_never_hands_the_engine_a_forming_bar() -> None:
    """The structural lookahead guard (MarketState.__post_init__) fires on
    every construction - if `_build_state` ever leaked a forming bar, this
    would raise before the fake engine even saw the state."""
    result, engine = _run(_zero_cost_model(), start=_START)
    assert len(engine.seen) > 0
    for state in engine.seen:
        for bars in state.bars.values():
            for bar in bars:
                assert bar.close_time <= state.as_of
