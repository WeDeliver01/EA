"""SPEC-06 §5 step 1 acceptance for `MarketScanner`: a bar the agent
reports as closed (per `candle_close_grace`) gets upserted and reported
exactly once as newly closed - a re-scan of the same bar (the agent's
`count`-based fetch always includes recently-closed bars, not just brand
new ones) must not re-announce it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.repositories.market_data import MarketDataRepository
from app.workers.market_scanner import MarketScanner, ScanTarget
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


class _FakeBarSource:
    def __init__(self, bars: dict[Timeframe, tuple[Bar, ...]]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, Timeframe, int]] = []

    async def get_bars(self, symbol: str, timeframe: Timeframe, *, count: int) -> tuple[Bar, ...]:
        self.calls.append((symbol, timeframe, count))
        return self._bars.get(timeframe, ())


def _scanner(source: _FakeBarSource, refs) -> MarketScanner:
    return MarketScanner(
        bar_source=source,
        targets=[
            ScanTarget(
                account_id=refs.account_id,
                instrument_id=refs.instrument_id,
                symbol="XAUUSD",
                timeframes=(Timeframe.M15,),
            )
        ],
    )


async def test_a_closed_bar_is_upserted_and_reported_once(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    closed_bar = _bar(now - timedelta(minutes=16))  # closes well before `now`, clears the grace

    source = _FakeBarSource({Timeframe.M15: (closed_bar,)})
    market_data_repo = MarketDataRepository(db_session)
    scanner = _scanner(source, refs)

    newly_closed = await scanner.scan_once(market_data_repo=market_data_repo, now=now)
    await db_session.commit()

    assert len(newly_closed) == 1
    assert newly_closed[0].bar.open_time == closed_bar.open_time
    assert newly_closed[0].account_id == refs.account_id
    assert newly_closed[0].instrument_id == refs.instrument_id

    persisted = await market_data_repo.get_recent_closed_bars(
        refs.instrument_id, Timeframe.M15, symbol="XAUUSD", before=now, limit=10
    )
    assert len(persisted) == 1


async def test_the_still_forming_bar_is_never_reported(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    forming_bar = _bar(now)  # opened exactly at `now` - not closed yet

    source = _FakeBarSource({Timeframe.M15: (forming_bar,)})
    scanner = _scanner(source, refs)

    result = await scanner.scan_once(market_data_repo=MarketDataRepository(db_session), now=now)
    assert result == []


async def test_a_re_scan_of_the_same_close_is_not_reported_twice(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    closed_bar = _bar(now - timedelta(minutes=16))

    source = _FakeBarSource({Timeframe.M15: (closed_bar,)})
    scanner = _scanner(source, refs)
    market_data_repo = MarketDataRepository(db_session)

    first = await scanner.scan_once(market_data_repo=market_data_repo, now=now)
    await db_session.commit()
    # A later tick before the next candle closes re-fetches the same
    # window - `count`-based get_bars always includes recently-closed bars.
    second = await scanner.scan_once(
        market_data_repo=market_data_repo, now=now + timedelta(seconds=30)
    )
    await db_session.commit()

    assert len(first) == 1
    assert second == []


async def test_a_genuinely_new_close_is_reported(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    first_now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    first_bar = _bar(first_now - timedelta(minutes=16))

    source = _FakeBarSource({Timeframe.M15: (first_bar,)})
    market_data_repo = MarketDataRepository(db_session)
    scanner = _scanner(source, refs)

    await scanner.scan_once(market_data_repo=market_data_repo, now=first_now)
    await db_session.commit()

    second_now = first_now + timedelta(minutes=15)
    second_bar = _bar(second_now - timedelta(minutes=16))
    source._bars = {Timeframe.M15: (first_bar, second_bar)}

    second_result = await scanner.scan_once(market_data_repo=market_data_repo, now=second_now)
    await db_session.commit()

    assert len(second_result) == 1
    assert second_result[0].bar.open_time == second_bar.open_time
