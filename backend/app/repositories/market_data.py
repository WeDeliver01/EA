"""SPEC-02 §5: `market_bars` persistence - live OHLC data the market
scanner fetches from the agent (`command.get_bars`) and the market data
engine reads back to build a `MarketState`.

`source` is always `'mt5'` for anything written from the live scanner -
`'import'`/`'synthetic'` are research/backtest concerns this repository
never touches (see `app/research/` for those).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.models.tables import MarketBar


class MarketDataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_bars(
        self,
        bars: Sequence[Bar],
        *,
        instrument_id: UUID,
        source: str,
        ingested_at: datetime,
    ) -> None:
        """A re-fetch of a bar the scanner already has (its close was
        reported early, or the scanner overlaps its own window on retry)
        is expected, not an error - `ON CONFLICT` on the natural
        `(instrument_id, timeframe, open_time)` primary key refreshes the
        row in place rather than failing or duplicating it."""
        if not bars:
            return
        values = [
            {
                "instrument_id": instrument_id,
                "timeframe": bar.timeframe.value,
                "open_time": bar.open_time,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "tick_volume": bar.tick_volume,
                "real_volume": bar.real_volume,
                "spread_points": bar.spread_points,
                "source": source,
                "ingested_at": ingested_at,
            }
            for bar in bars
        ]
        stmt = insert(MarketBar).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "timeframe", "open_time"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "tick_volume": stmt.excluded.tick_volume,
                "real_volume": stmt.excluded.real_volume,
                "spread_points": stmt.excluded.spread_points,
                "source": stmt.excluded.source,
                "ingested_at": stmt.excluded.ingested_at,
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def get_recent_closed_bars(
        self,
        instrument_id: UUID,
        timeframe: Timeframe,
        *,
        symbol: str,
        before: datetime,
        limit: int,
    ) -> tuple[Bar, ...]:
        """The most recent `limit` bars whose CLOSE time is `<= before`,
        oldest first - the exact ordering `MarketState.__post_init__`
        requires and the exact lookahead boundary it enforces (`before` is
        normally the primary timeframe's own `as_of`).

        Filtering on `open_time` alone would be wrong for any timeframe
        coarser than the primary one: an H1 bar that opened 10 minutes
        before an M15 `as_of` has `open_time < before` but doesn't close
        for another 50 minutes - `close_time = open_time + timeframe.seconds`
        is the actual boundary `Bar`'s own docstring defines "closed" by."""
        close_cutoff = before - timedelta(seconds=timeframe.seconds)
        result = await self._session.execute(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument_id,
                MarketBar.timeframe == timeframe.value,
                MarketBar.open_time <= close_cutoff,
            )
            .order_by(MarketBar.open_time.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        return tuple(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=row.open_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                tick_volume=row.tick_volume,
                real_volume=row.real_volume,
                spread_points=row.spread_points,
            )
            for row in rows
        )
