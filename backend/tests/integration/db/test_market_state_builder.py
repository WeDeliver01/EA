"""Acceptance for `app.workers.market_state.build_market_state`: the live
equivalent of `app/research/backtester.py`'s `_build_state()`, reading
bars/account/risk/positions/signals from the database instead of an
in-memory dict, and constructing the exact `MarketState` shape
`StrategyEngine.evaluate()` requires."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.domain.market.quote import Quote
from app.repositories.accounts import AccountRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.positions import PositionRepository
from app.repositories.signals import SignalRepository
from app.workers.market_state import build_market_state
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


def _bar(open_time: datetime, *, timeframe: Timeframe = Timeframe.M15) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe=timeframe,
        open_time=open_time,
        open=Decimal("99.50"),
        high=Decimal("100.50"),
        low=Decimal("99.00"),
        close=Decimal("100.00"),
        tick_volume=100,
        real_volume=None,
        spread_points=2,
    )


async def test_builds_a_valid_market_state_from_live_data(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    market_data_repo = MarketDataRepository(db_session)
    as_of = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    await market_data_repo.upsert_bars(
        [_bar(as_of - timedelta(minutes=15 * i)) for i in range(1, 4)],
        instrument_id=refs.instrument_id,
        source="mt5",
        ingested_at=as_of,
    )
    await db_session.commit()

    quote = Quote(
        symbol="XAUUSD",
        bid=Decimal("100.00"),
        ask=Decimal("100.10"),
        server_time=as_of,
        received_at=as_of,
    )

    state = await build_market_state(
        account_repo=AccountRepository(db_session),
        market_data_repo=market_data_repo,
        position_repo=PositionRepository(db_session),
        signal_repo=SignalRepository(db_session),
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        primary_tf=Timeframe.M15,
        context_timeframes=[Timeframe.H1],
        quote=quote,
        as_of=as_of,
    )

    assert state.symbol == "XAUUSD"
    assert state.as_of == as_of
    assert state.primary_tf == Timeframe.M15
    assert len(state.bars[Timeframe.M15]) == 3
    # oldest first - MarketState's own required ordering.
    assert state.bars[Timeframe.M15][0].open_time < state.bars[Timeframe.M15][-1].open_time
    assert state.bars[Timeframe.H1] == ()  # none upserted for this timeframe
    assert state.quote is quote
    assert state.account.account_id == refs.account_id
    assert state.open_positions == ()
    assert state.recent_signals == ()
    assert state.calendar_events == ()
    # Constructing MarketState itself validates the lookahead invariant -
    # reaching this point at all is part of what this test proves.
