"""SPEC-10 Phase 2 acceptance: for any as_of, every timeframe in
MarketState.bars has close_time <= as_of. This is the lookahead guard,
enforced structurally by MarketState.__post_init__."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from tests.factories import make_market_state

pytestmark = pytest.mark.unit


def test_market_state_accepts_only_closed_bars() -> None:
    state = make_market_state()
    for tf, bars in state.bars.items():
        for bar in bars:
            assert (
                bar.close_time <= state.as_of
            ), f"{tf} bar {bar.open_time} is not closed as of {state.as_of}"


def test_market_state_rejects_a_forming_bar() -> None:
    as_of = datetime(2026, 9, 7, 9, 15, tzinfo=UTC)
    forming_bar = Bar(
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        open_time=as_of - timedelta(minutes=5),  # closes at 09:20, after as_of
        open=Decimal("3418.00"),
        high=Decimal("3419.00"),
        low=Decimal("3417.50"),
        close=Decimal("3418.50"),
        tick_volume=10,
        real_volume=None,
        spread_points=20,
    )
    state = make_market_state(as_of=as_of)
    bars = dict(state.bars)
    bars[Timeframe.M15] = (*bars[Timeframe.M15], forming_bar)

    from app.domain.market.market_state import MarketState

    with pytest.raises(ValueError, match="lookahead violation"):
        MarketState(
            symbol=state.symbol,
            spec=state.spec,
            as_of=state.as_of,
            primary_tf=state.primary_tf,
            bars=bars,
            quote=state.quote,
            session=state.session,
            account=state.account,
            open_positions=state.open_positions,
            recent_signals=state.recent_signals,
            calendar_events=state.calendar_events,
            risk_state=state.risk_state,
        )


def test_market_state_rejects_naive_as_of() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_market_state(as_of=datetime(2026, 9, 7, 9, 15))
