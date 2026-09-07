"""SPEC-07 §2: BacktestRunner.

`engine.evaluate()` is the exact same `StrategyEngine` used live (SPEC-07
§1) - the only thing that differs is how `MarketState` gets built (from a
preloaded historical array here, from Redis/the market-data engine live)
and what happens to the `Decision` afterwards. There is no `if backtest:`
anywhere in `engines/`.

Simplifications versus the full spec, documented rather than silent (see
docs/adr/0001-mvp-scope.md for the running list):
  - **Single position at a time.** A new decision is only evaluated while
    flat. Real portfolio-level concerns (MAX_OPEN_POSITIONS > 1, correlated
    exposure across symbols) don't arise, so `MarketState.open_positions`
    is always empty and those gates are untestable here by construction.
  - **No calendar data.** `MarketState.calendar_events` is always empty -
    there's no economic calendar in this environment. NEWS_BLACKOUT can
    never fire in a backtest run.
  - **Equity tracked only at trade close**, not mark-to-market intrabar.
    Floating P&L between bars isn't modelled; the equity curve steps at
    each realised (partial or full) exit.
  - Entry fills at the **next bar's open** (SPEC-07 §2 step 7) using
    whichever `FillModel` the run is configured with.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.domain.market.bar import Bar
from app.domain.market.enums import Direction, Timeframe
from app.domain.market.market_state import MarketState
from app.domain.market.quote import Quote
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.portfolio.account_state import AccountState
from app.domain.risk.limits import RiskLimits
from app.domain.risk.state import RiskState
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.domain.strategy.signal_ref import SignalRef
from app.engines.config import StrategyConfig
from app.engines.context.sessions import classify_session
from app.engines.indicators.atr import atr as compute_atr
from app.engines.risk.engine import evaluate as evaluate_risk
from app.engines.strategy_engine import StrategyEngine
from app.research.cost_model import CostModel, commission_cost, spread_cost_price, swap_cost
from app.research.fill_model import FILL_MODELS
from app.research.position_simulator import SimulatedPosition, StepResult, advance, open_position
from app.research.types import EquityPoint, ResearchTrade


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    strategy_config: StrategyConfig
    risk_limits: RiskLimits
    cost_model: CostModel
    fill_model_name: str
    initial_balance: Decimal
    conversion_rate: Decimal = Decimal(1)
    leverage: int = 500
    margin_safety_factor: Decimal = Decimal("0.30")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: list[ResearchTrade]
    equity_curve: list[EquityPoint]
    decisions: list[Decision]
    final_balance: Decimal


class _RunningRiskState:
    """Mutable bookkeeping for RiskState, day/week rollover included. Lives
    in `research/`, not `engines/` - it reads the wall-clock-equivalent
    `as_of` from the bar stream, not the real clock, so it stays
    deterministic, but it is not a pure function and has no purity
    obligation to meet."""

    def __init__(self, *, starting_balance: Decimal) -> None:
        self.balance = starting_balance
        self.peak_equity = starting_balance
        self.realised_pnl_today = Decimal(0)
        self.realised_pnl_week = Decimal(0)
        self.trades_today = 0
        self.consecutive_losses = 0
        self._current_day: date | None = None
        self._current_week: tuple[int, int] | None = None
        self.equity_at_day_start = starting_balance
        self.equity_at_week_start = starting_balance

    def roll_to(self, as_of: datetime) -> None:
        day = as_of.date()
        iso_year, iso_week, _ = as_of.isocalendar()
        week = (iso_year, iso_week)
        if self._current_day is not None and day != self._current_day:
            self.realised_pnl_today = Decimal(0)
            self.trades_today = 0
            self.equity_at_day_start = self.balance
        if self._current_week is not None and week != self._current_week:
            self.realised_pnl_week = Decimal(0)
            self.equity_at_week_start = self.balance
        self._current_day = day
        self._current_week = week

    def record_trade_close(self, net_pnl: Decimal) -> None:
        self.balance += net_pnl
        self.peak_equity = max(self.peak_equity, self.balance)
        self.realised_pnl_today += net_pnl
        self.realised_pnl_week += net_pnl
        self.trades_today += 1
        self.consecutive_losses = self.consecutive_losses + 1 if net_pnl < 0 else 0

    def as_risk_state(
        self, *, as_of: datetime, open_position_count: int, open_risk: Decimal
    ) -> RiskState:
        current_drawdown_pct = (
            (self.peak_equity - self.balance) / self.peak_equity
            if self.peak_equity > 0
            else Decimal(0)
        )
        return RiskState(
            as_of=as_of,
            realised_pnl_today=self.realised_pnl_today,
            realised_pnl_week=self.realised_pnl_week,
            open_risk=open_risk,
            trades_today=self.trades_today,
            open_position_count=open_position_count,
            consecutive_losses=self.consecutive_losses,
            peak_equity=self.peak_equity,
            current_drawdown_pct=current_drawdown_pct,
            trading_enabled=True,
            kill_switch_active=False,
        )


def _closed_prefix(bars: tuple[Bar, ...], as_of: datetime) -> tuple[Bar, ...]:
    """Bars whose close_time <= as_of, via binary search - the lookahead
    guard's data-supply side. `MarketState.__post_init__` re-checks this
    structurally on every construction regardless."""
    close_times = [b.close_time for b in bars]
    index = bisect.bisect_right(close_times, as_of)
    return bars[:index]


def _nights_held(entry: datetime, exit_: datetime) -> list[date]:
    span = (exit_.date() - entry.date()).days
    return [
        entry.date() for _ in range(span)
    ]  # one entry per rollover crossed; date value itself is unused


class BacktestRunner:
    def __init__(
        self,
        config: BacktestConfig,
        *,
        strategy_version_id: UUID | None = None,
        engine: StrategyEngine | None = None,
    ) -> None:
        """`engine` is an injection seam for tests: pass a stand-in with the
        same `evaluate(state) -> Decision` shape to exercise the backtester's
        own mechanics (fills, costs, position simulation) against a
        hand-picked decision sequence, independent of whether the real
        pattern-detection pipeline happens to fire on a given synthetic bar
        series. Production callers leave it unset and get the real engine."""
        self._config = config
        self._strategy_version_id = strategy_version_id or uuid4()
        self._account_id = uuid4()
        self._engine = engine or StrategyEngine(
            config.strategy_config, strategy_version_id=self._strategy_version_id
        )
        self._fill_model = FILL_MODELS[config.fill_model_name]

    def run(
        self,
        *,
        symbol: str,
        spec: SymbolSpec,
        primary_tf: Timeframe,
        bars_by_timeframe: dict[Timeframe, tuple[Bar, ...]],
    ) -> BacktestResult:
        cfg = self._config
        primary_bars = bars_by_timeframe[primary_tf]

        risk_state = _RunningRiskState(starting_balance=cfg.initial_balance)
        equity_curve: list[EquityPoint] = []
        trades: list[ResearchTrade] = []
        decisions: list[Decision] = []
        recent_signals: list[SignalRef] = []

        position: SimulatedPosition | None = None
        position_entry_time: datetime | None = None
        pending_direction: Direction | None = None
        pending_stop: Decimal | None = None
        pending_take_profits: tuple[TakeProfit, ...] = ()
        realized_gross = Decimal(0)
        realized_commission = Decimal(0)
        entry_price_for_open: Decimal | None = None

        for bar in primary_bars:
            as_of = bar.close_time
            risk_state.roll_to(as_of)

            if pending_direction is not None and position is None:
                entry_price = self._fill_model.resolve_entry(
                    direction=pending_direction, next_bar=bar, spec=spec, cost_model=cfg.cost_model
                )
                sizing_stop = pending_stop
                assert sizing_stop is not None
                risk_decision = evaluate_risk(
                    risk_state=risk_state.as_risk_state(
                        as_of=as_of, open_position_count=0, open_risk=Decimal(0)
                    ),
                    risk_limits=cfg.risk_limits,
                    account_equity=risk_state.balance,
                    account_free_margin=risk_state.balance,
                    equity_at_day_start=risk_state.equity_at_day_start,
                    equity_at_week_start=risk_state.equity_at_week_start,
                    entry=entry_price,
                    stop_loss=sizing_stop,
                    spec=spec,
                    conversion_rate=cfg.conversion_rate,
                    leverage=cfg.leverage,
                    margin_safety_factor=cfg.margin_safety_factor,
                )
                if risk_decision.approved and risk_decision.size is not None:
                    volume = risk_decision.size.volume
                    position = open_position(
                        direction=pending_direction,
                        entry_price=entry_price,
                        stop_loss=sizing_stop,
                        take_profits=pending_take_profits,
                        volume=volume,
                    )
                    position_entry_time = as_of
                    entry_price_for_open = entry_price
                    realized_gross = Decimal(0)
                    realized_commission = -commission_cost(volume, cfg.cost_model)
                pending_direction = None
                pending_stop = None
                pending_take_profits = ()

            if position is not None:
                direction_before = position.direction
                initial_stop_before = position.initial_stop
                initial_volume_before = position.initial_volume

                primary_atr_series = compute_atr(
                    _closed_prefix(primary_bars, as_of),
                    period=cfg.strategy_config.context.atr_period,
                )
                atr = next((v for v in reversed(primary_atr_series) if v is not None), Decimal(0))
                step: StepResult = advance(
                    position,
                    bar,
                    atr=atr,
                    cfg=cfg.strategy_config.trade_construction,
                    fill_model=self._fill_model,
                    spec=spec,
                )
                for event in step.events:
                    sign = Decimal(1) if direction_before == Direction.LONG else Decimal(-1)
                    assert entry_price_for_open is not None
                    event_gross = (
                        (event.price - entry_price_for_open)
                        * sign
                        / spec.tick_size
                        * spec.tick_value
                        * cfg.conversion_rate
                        * event.volume
                    )
                    realized_gross += event_gross
                    realized_commission -= commission_cost(event.volume, cfg.cost_model)

                position = step.position
                if position is None:
                    assert position_entry_time is not None and entry_price_for_open is not None
                    last_event = step.events[-1]
                    swap = swap_cost(
                        volume=initial_volume_before,
                        direction=direction_before,
                        nights_held=_nights_held(position_entry_time, as_of),
                        cfg=cfg.cost_model,
                        spec=spec,
                    )
                    net_pnl = realized_gross + realized_commission + swap
                    stop_distance_ticks = (
                        abs(entry_price_for_open - initial_stop_before) / spec.tick_size
                    )
                    risk_amount = (
                        stop_distance_ticks
                        * spec.tick_value
                        * cfg.conversion_rate
                        * initial_volume_before
                    )
                    r_multiple = net_pnl / risk_amount if risk_amount > 0 else Decimal(0)
                    trades.append(
                        ResearchTrade(
                            entry_time=position_entry_time,
                            exit_time=as_of,
                            direction=direction_before,
                            entry_price=entry_price_for_open,
                            exit_price=last_event.price,
                            volume=initial_volume_before,
                            gross_pnl=realized_gross,
                            commission=realized_commission,
                            swap=swap,
                            net_pnl=net_pnl,
                            risk_amount=risk_amount,
                            r_multiple=r_multiple,
                            mae=None,
                            mfe=None,
                            exit_reason=last_event.kind,
                        )
                    )
                    risk_state.record_trade_close(net_pnl)
                    position_entry_time = None
                    entry_price_for_open = None

            if position is None and pending_direction is None:
                state = self._build_state(
                    symbol=symbol,
                    spec=spec,
                    as_of=as_of,
                    primary_tf=primary_tf,
                    bars_by_timeframe=bars_by_timeframe,
                    risk_state=risk_state,
                    recent_signals=tuple(recent_signals),
                )
                decision = self._engine.evaluate(state)
                decisions.append(decision)
                if decision.outcome == DecisionOutcome.TRADE:
                    assert decision.direction is not None and decision.stop_loss is not None
                    pending_direction = decision.direction
                    pending_stop = decision.stop_loss
                    pending_take_profits = decision.take_profits
                    if decision.setup is not None:
                        recent_signals.append(
                            SignalRef(
                                id=uuid4(),
                                symbol=symbol,
                                setup_fingerprint=decision.setup.fingerprint,
                                direction=decision.direction,
                                created_at=as_of,
                            )
                        )

            equity_curve.append(
                EquityPoint(
                    ts=as_of,
                    balance=risk_state.balance,
                    equity=risk_state.balance,
                    open_positions=1 if position is not None else 0,
                )
            )

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            decisions=decisions,
            final_balance=risk_state.balance,
        )

    def _build_state(
        self,
        *,
        symbol: str,
        spec: SymbolSpec,
        as_of: datetime,
        primary_tf: Timeframe,
        bars_by_timeframe: dict[Timeframe, tuple[Bar, ...]],
        risk_state: _RunningRiskState,
        recent_signals: tuple[SignalRef, ...],
    ) -> MarketState:
        bars = {tf: _closed_prefix(series, as_of) for tf, series in bars_by_timeframe.items()}
        primary_bars = bars.get(primary_tf, ())
        last_close = primary_bars[-1].close if primary_bars else Decimal(0)
        last_spread_points = primary_bars[-1].spread_points if primary_bars else 0
        half_spread = spread_cost_price(
            spec, self._config.cost_model, recorded_spread_points=last_spread_points
        )

        quote = Quote(
            symbol=symbol,
            bid=last_close - half_spread,
            ask=last_close + half_spread,
            server_time=as_of,
            received_at=as_of,
        )
        account = AccountState(
            account_id=self._account_id,
            broker="backtest",
            login="backtest",
            currency="USD",
            balance=risk_state.balance,
            equity=risk_state.balance,
            margin=Decimal(0),
            free_margin=risk_state.balance,
            margin_level=None,
            leverage=self._config.leverage,
            server_time=as_of,
            reported_at=as_of,
            is_stale=False,
        )
        return MarketState(
            symbol=symbol,
            spec=spec,
            as_of=as_of,
            primary_tf=primary_tf,
            bars=bars,
            quote=quote,
            session=classify_session(as_of),
            account=account,
            open_positions=(),
            recent_signals=recent_signals,
            calendar_events=(),
            risk_state=risk_state.as_risk_state(
                as_of=as_of, open_position_count=0, open_risk=Decimal(0)
            ),
        )
