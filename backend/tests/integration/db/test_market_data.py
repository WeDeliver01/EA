"""SPEC-02 §5 acceptance for `MarketDataRepository`: upsert is idempotent
on the natural (instrument_id, timeframe, open_time) key, and reads come
back oldest-first with the lookahead boundary the market data engine will
rely on to build a `MarketState`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.repositories.market_data import MarketDataRepository
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


def _bar(
    open_time: datetime,
    *,
    close: Decimal = Decimal("100.00"),
    timeframe: Timeframe = Timeframe.M15,
) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe=timeframe,
        open_time=open_time,
        open=Decimal("99.50"),
        high=Decimal("100.50"),
        low=Decimal("99.00"),
        close=close,
        tick_volume=100,
        real_volume=None,
        spread_points=2,
    )


async def test_upsert_then_read_round_trips(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = MarketDataRepository(db_session)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [_bar(base + timedelta(minutes=15 * i)) for i in range(3)]

    await repo.upsert_bars(bars, instrument_id=refs.instrument_id, source="mt5", ingested_at=base)
    await db_session.commit()

    read_back = await repo.get_recent_closed_bars(
        refs.instrument_id,
        Timeframe.M15,
        symbol="XAUUSD",
        before=base + timedelta(minutes=45),
        limit=10,
    )

    assert [b.open_time for b in read_back] == [b.open_time for b in bars]


async def test_upsert_is_idempotent_on_natural_key(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = MarketDataRepository(db_session)
    open_time = datetime(2026, 1, 1, tzinfo=UTC)

    await repo.upsert_bars(
        [_bar(open_time, close=Decimal("100.00"))],
        instrument_id=refs.instrument_id,
        source="mt5",
        ingested_at=open_time,
    )
    await db_session.commit()

    # A re-fetch of the same bar (e.g. the scanner's window overlapped a
    # retry) refreshes the row rather than raising a PK violation.
    await repo.upsert_bars(
        [_bar(open_time, close=Decimal("100.25"))],
        instrument_id=refs.instrument_id,
        source="mt5",
        ingested_at=open_time + timedelta(seconds=1),
    )
    await db_session.commit()

    read_back = await repo.get_recent_closed_bars(
        refs.instrument_id,
        Timeframe.M15,
        symbol="XAUUSD",
        before=open_time + timedelta(minutes=15),
        limit=10,
    )

    assert len(read_back) == 1
    assert read_back[0].close == Decimal("100.25")


async def test_before_boundary_excludes_the_still_forming_bar(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = MarketDataRepository(db_session)
    closed_bar_open = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    forming_bar_open = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)

    await repo.upsert_bars(
        [_bar(closed_bar_open), _bar(forming_bar_open)],
        instrument_id=refs.instrument_id,
        source="mt5",
        ingested_at=closed_bar_open,
    )
    await db_session.commit()

    read_back = await repo.get_recent_closed_bars(
        refs.instrument_id,
        Timeframe.M15,
        symbol="XAUUSD",
        before=forming_bar_open,  # the forming bar's own as_of - never itself
        limit=10,
    )

    assert len(read_back) == 1
    assert read_back[0].open_time == closed_bar_open


async def test_boundary_uses_close_time_not_open_time_for_coarser_timeframes(
    db_session: AsyncSession,
) -> None:
    """An H1 bar that opened only 10 minutes before an M15 `as_of` has
    `open_time < as_of` but is nowhere near closed - filtering on open_time
    alone would leak a forming bar into MarketState for any context
    timeframe coarser than the primary one."""
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = MarketDataRepository(db_session)
    as_of = datetime(2026, 1, 1, 1, 10, tzinfo=UTC)  # 01:10
    still_forming_h1_open = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # opened 01:00, closes 02:00
    already_closed_h1_open = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)  # opened 00:00, closed 01:00

    await repo.upsert_bars(
        [
            _bar(still_forming_h1_open, timeframe=Timeframe.H1),
            _bar(already_closed_h1_open, timeframe=Timeframe.H1),
        ],
        instrument_id=refs.instrument_id,
        source="mt5",
        ingested_at=as_of,
    )
    await db_session.commit()

    read_back = await repo.get_recent_closed_bars(
        refs.instrument_id, Timeframe.H1, symbol="XAUUSD", before=as_of, limit=10
    )

    assert len(read_back) == 1
    assert read_back[0].open_time == already_closed_h1_open


async def test_empty_bars_is_a_noop(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = MarketDataRepository(db_session)
    await repo.upsert_bars(
        [], instrument_id=refs.instrument_id, source="mt5", ingested_at=datetime.now(UTC)
    )
    await db_session.commit()

    read_back = await repo.get_recent_closed_bars(
        refs.instrument_id,
        Timeframe.M15,
        symbol="XAUUSD",
        before=datetime.now(UTC),
        limit=10,
    )
    assert read_back == ()
