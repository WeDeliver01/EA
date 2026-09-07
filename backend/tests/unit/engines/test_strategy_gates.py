"""SPEC-10 Phase 2 acceptance: every GateCode has at least one test that
triggers it. Covers every strategy gate this MVP implements
(`app.engines.gates.strategy_gates`); the six risk/execution gates that need
live agent/reconciliation state this MVP doesn't model
(AGENT_DISCONNECTED, BROKER_DISCONNECTED, RECONCILIATION_UNRESOLVED,
MARKET_CLOSED, STRATEGY_PAUSED, CORRELATED_EXPOSURE) are documented as
deferred in docs/adr/0001-mvp-scope.md and have no test here because there is
no code path to trigger."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.market.enums import Session, Timeframe
from app.domain.strategy.enums import GateCode
from app.domain.strategy.signal_ref import SignalRef
from app.engines.config import StrategyConfig
from app.engines.strategy_engine import StrategyEngine
from tests.factories import make_calendar_event, make_market_state, make_trade_ready_state

pytestmark = pytest.mark.unit


def _gate(decision, code: GateCode):
    return next(g for g in decision.gates if g.code == code)


def test_confluence_below_threshold_fails_on_the_default_oscillating_fixture() -> None:
    engine = StrategyEngine(StrategyConfig(), strategy_version_id=uuid4())
    decision = engine.evaluate(make_market_state())
    assert _gate(decision, GateCode.CONFLUENCE_BELOW_THRESHOLD).passed is False


def test_confluence_below_threshold_passes_on_the_trade_ready_fixture(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.CONFLUENCE_BELOW_THRESHOLD).passed is True


def test_regime_not_permitted_fails_when_permitted_regimes_is_empty(trade_ready_config) -> None:
    config = trade_ready_config.model_copy(
        update={"context": trade_ready_config.context.model_copy(update={"permitted_regimes": ()})}
    )
    engine = StrategyEngine(config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.REGIME_NOT_PERMITTED).passed is False


def test_regime_not_permitted_passes_on_the_trade_ready_fixture(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.REGIME_NOT_PERMITTED).passed is True


def test_session_not_permitted_fails_outside_configured_sessions(trade_ready_config) -> None:
    state = make_trade_ready_state(session=Session.DEAD)
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(state)
    assert _gate(decision, GateCode.SESSION_NOT_PERMITTED).passed is False


def test_session_not_permitted_passes_inside_configured_sessions(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state(session=Session.LONDON_NY_OVERLAP))
    assert _gate(decision, GateCode.SESSION_NOT_PERMITTED).passed is True


def test_duplicate_setup_fails_when_a_recent_signal_shares_the_fingerprint(
    trade_ready_config,
) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    state = make_trade_ready_state()
    setup = engine._setup_detector.evaluate(
        state, engine._structure_engine.evaluate(state), atr=Decimal("1")
    )
    assert setup is not None
    recent = SignalRef(
        id=uuid4(),
        symbol=state.symbol,
        setup_fingerprint=setup.fingerprint,
        direction=setup.direction,
        created_at=state.as_of - timedelta(minutes=15),
    )
    state_with_dup = replace(state, recent_signals=(recent,))
    decision = engine.evaluate(state_with_dup)
    assert _gate(decision, GateCode.DUPLICATE_SETUP).passed is False


def test_duplicate_setup_passes_with_no_recent_signals(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.DUPLICATE_SETUP).passed is True


def test_news_blackout_fails_with_a_high_impact_event_in_the_window(trade_ready_config) -> None:
    state = make_trade_ready_state()
    event = make_calendar_event(event_time=state.as_of)
    state_with_news = replace(state, calendar_events=(event,))
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(state_with_news)
    assert _gate(decision, GateCode.NEWS_BLACKOUT).passed is False


def test_news_blackout_passes_with_no_events(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.NEWS_BLACKOUT).passed is True


def test_volatility_out_of_bounds_fails_when_regime_is_unknown() -> None:
    # Too few bars for ContextEngine to classify anything -> Regime.UNKNOWN.
    state = make_market_state(bar_counts={Timeframe.M15: 5})
    engine = StrategyEngine(StrategyConfig(), strategy_version_id=uuid4())
    decision = engine.evaluate(state)
    assert _gate(decision, GateCode.VOLATILITY_OUT_OF_BOUNDS).passed is False


def test_volatility_out_of_bounds_passes_on_the_trade_ready_fixture(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.VOLATILITY_OUT_OF_BOUNDS).passed is True


def test_spread_too_wide_fails_when_spread_exceeds_the_atr_multiple(trade_ready_config) -> None:
    state = make_trade_ready_state()
    wide_quote = replace(state.quote, ask=state.quote.bid + Decimal("50"))
    state_with_wide_spread = replace(state, quote=wide_quote)
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(state_with_wide_spread)
    assert _gate(decision, GateCode.SPREAD_TOO_WIDE).passed is False


def test_spread_too_wide_passes_on_the_trade_ready_fixture(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.SPREAD_TOO_WIDE).passed is True


def test_rr_below_minimum_fails_when_min_rr_exceeds_the_first_tp_rung(trade_ready_config) -> None:
    config = trade_ready_config.model_copy(
        update={"filters": trade_ready_config.filters.model_copy(update={"min_rr": Decimal("10")})}
    )
    engine = StrategyEngine(config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.RR_BELOW_MINIMUM).passed is False


def test_rr_below_minimum_passes_on_the_trade_ready_fixture(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.RR_BELOW_MINIMUM).passed is True


def test_stop_distance_invalid_fails_when_atr_bounds_are_impossible(trade_ready_config) -> None:
    trade_construction = trade_ready_config.trade_construction.model_copy(
        update={"min_stop_atr_multiple": Decimal("5"), "max_stop_atr_multiple": Decimal("1")}
    )
    config = trade_ready_config.model_copy(update={"trade_construction": trade_construction})
    engine = StrategyEngine(config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.STOP_DISTANCE_INVALID).passed is False


def test_stop_distance_invalid_passes_on_the_trade_ready_fixture(trade_ready_config) -> None:
    engine = StrategyEngine(trade_ready_config, strategy_version_id=uuid4())
    decision = engine.evaluate(make_trade_ready_state())
    assert _gate(decision, GateCode.STOP_DISTANCE_INVALID).passed is True
