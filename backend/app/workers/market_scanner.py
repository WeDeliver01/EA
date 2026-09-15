"""SPEC-08 §1/§4, SPEC-06 §5 step 1: candle-close detection - the thing
that turns "the agent has MT5 bar data" into `stream:bars:closed`, the
event `strategy_worker` reacts to.

`AgentBarSource` is deliberately its own, narrow Protocol rather than a
dependency on `app.execution.broker.AsyncBroker` (which already has a
`get_bars`-shaped concept via `app.transport.ws_broker.WSAgentBroker`):
`app.workers` may import neither `app.transport` (an explicit
import-linter contract) nor `app.execution` (both sit in the same layer,
which import-linter's "layers" contract makes mutually exclusive). The
concrete implementation (`WSAgentBroker.get_bars`) is wired in from the
app entrypoint, the same seam `AsyncBroker`/`WSAgentBroker` already use
for execution.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.repositories.market_data import MarketDataRepository

DEFAULT_FETCH_COUNT = 3


class AgentBarSource(Protocol):
    async def get_bars(
        self, symbol: str, timeframe: Timeframe, *, count: int
    ) -> tuple[Bar, ...]: ...


@dataclass(frozen=True, slots=True)
class ScanTarget:
    account_id: UUID
    instrument_id: UUID
    symbol: str
    timeframes: tuple[Timeframe, ...]


@dataclass(frozen=True, slots=True)
class NewlyClosedBar:
    account_id: UUID
    instrument_id: UUID
    symbol: str
    timeframe: Timeframe
    bar: Bar


class MarketScanner:
    """Not itself a loop - `scan_once` is one pass over every configured
    target/timeframe, meant to be driven by an outer scheduler tick (see
    `app.workers.scheduler`). Kept this way so a test can call `scan_once`
    directly with a controlled `now` rather than needing to wait on a real
    clock."""

    def __init__(
        self,
        *,
        bar_source: AgentBarSource,
        targets: Sequence[ScanTarget],
        candle_close_grace: timedelta = timedelta(milliseconds=1500),
        fetch_count: int = DEFAULT_FETCH_COUNT,
    ) -> None:
        self._bar_source = bar_source
        self._targets = list(targets)
        self._candle_close_grace = candle_close_grace
        self._fetch_count = fetch_count
        # In-memory, per-process - a restart re-announces the most recent
        # close once. Deliberately not treated as a correctness hazard: the
        # engine is pure (P7) and evaluating the same historical bar twice
        # produces the same setup_fingerprint, which DUPLICATE_SETUP
        # rejects as long as the earlier signal is still within its window
        # - the existing safety net this relies on rather than duplicates.
        self._last_published: dict[tuple[UUID, Timeframe], datetime] = {}

    async def scan_once(
        self, *, market_data_repo: MarketDataRepository, now: datetime
    ) -> list[NewlyClosedBar]:
        """`market_data_repo` is a per-call parameter, not a constructor
        dependency: this scanner is meant to be long-lived (it holds
        `_last_published` across ticks), but each tick needs its own
        short-lived session/repo, not one held open for the scanner's
        whole lifetime."""
        newly_closed: list[NewlyClosedBar] = []
        for target in self._targets:
            for tf in target.timeframes:
                bars = await self._bar_source.get_bars(target.symbol, tf, count=self._fetch_count)
                closed = [b for b in bars if now >= b.close_time + self._candle_close_grace]
                if not closed:
                    continue

                await market_data_repo.upsert_bars(
                    closed,
                    instrument_id=target.instrument_id,
                    source="mt5",
                    ingested_at=now,
                )

                latest = max(closed, key=lambda b: b.open_time)
                key = (target.instrument_id, tf)
                last_published = self._last_published.get(key)
                if last_published is not None and latest.open_time <= last_published:
                    continue
                self._last_published[key] = latest.open_time
                newly_closed.append(
                    NewlyClosedBar(
                        account_id=target.account_id,
                        instrument_id=target.instrument_id,
                        symbol=target.symbol,
                        timeframe=tf,
                        bar=latest,
                    )
                )
        return newly_closed
