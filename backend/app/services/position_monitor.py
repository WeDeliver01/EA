"""SPEC-06 §8's position-management loop, driven by the live quote cache
rather than candle closes.

Runs on its own short interval (`POSITION_MONITOR_INTERVAL_SECONDS`, 2s by
default), independent of the primary timeframe's candle closes: breakeven,
trailing and time-exit all need to react to price movement between closes,
not just at them - the distinction SPEC-06 §8 itself draws between
per-candle strategy evaluation and per-tick position management.

Lives in `app.services`, not `app.workers`, for the same reason
`execution_worker`/`reconciliation_worker` do: `PositionManager` lives in
`app.execution`, and `app.execution`/`app.workers` are mutually exclusive
siblings under the import-linter layering contract.

Only manages positions with both a `signal_id` and a recorded
`initial_stop` (`PositionRepository.list_open_for_management`'s own
filter) - an orphaned or manually-opened position has no take-profit
ladder or original stop to manage against; reconciliation is what notices
and reports those, not this loop. Skips the tick entirely (for the whole
account) if the live quote cache has no fresh entry for the symbol - the
same `QUOTE_STALE_SECONDS` bound `PRICE_STALE` uses, since there is no
safe management decision (a stop move, a partial close, a time exit)
without a live price to make it against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.quotes import get_quote
from app.domain.market.enums import Direction, Timeframe
from app.engines.config import TradeConstructionConfig
from app.engines.indicators.atr import atr as compute_atr
from app.execution.broker import AsyncBroker
from app.execution.event_consumer import EventConsumer
from app.execution.position_manager import LivePosition, PositionManager, decide
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository
from app.repositories.deals import DealRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.positions import PositionRepository
from app.repositories.signals import SignalRepository
from app.repositories.trade_intents import TradeIntentRepository

logger = structlog.get_logger(__name__)

# Comfortably past any configured atr_period's warm-up (14 by default).
_ATR_LOOKBACK_BARS = 100


@dataclass(frozen=True, slots=True)
class PositionMonitorConfig:
    trade_construction: TradeConstructionConfig
    atr_period: int
    primary_tf: Timeframe
    quote_stale_seconds: int


class PositionMonitorLoop:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        config: PositionMonitorConfig,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._config = config

    async def run_for_account(
        self,
        *,
        account_id: UUID,
        instrument_id: UUID,
        symbol: str,
        broker: AsyncBroker,
        as_of: datetime,
    ) -> int:
        """Manages every open, signal-backed position for this account and
        symbol. Returns how many positions actually got an action applied,
        for the caller to log or pace on."""
        cached = await get_quote(self._redis, account_id=account_id, symbol=symbol)
        if cached is None or (as_of - cached.received_at) >= timedelta(
            seconds=self._config.quote_stale_seconds
        ):
            return 0

        async with self._session_factory() as session:
            position_repo = PositionRepository(session)
            managed_positions = [
                p
                for p in await position_repo.list_open_for_management(account_id)
                if p.symbol == symbol
            ]
            if not managed_positions:
                return 0

            market_data_repo = MarketDataRepository(session)
            bars = await market_data_repo.get_recent_closed_bars(
                instrument_id,
                self._config.primary_tf,
                symbol=symbol,
                before=as_of,
                limit=_ATR_LOOKBACK_BARS,
            )
            atr_series = compute_atr(bars, period=self._config.atr_period)
            atr = next((v for v in reversed(atr_series) if v is not None), None)
            if atr is None:
                return 0  # not enough bars yet to warm up ATR

            account_repo = AccountRepository(session)
            spec = await account_repo.load_symbol_spec(instrument_id)
            signal_repo = SignalRepository(session)
            manager = PositionManager(
                broker=broker,
                event_consumer=EventConsumer(
                    agent_event_repo=AgentEventRepository(session),
                    deal_repo=DealRepository(session),
                    position_repo=position_repo,
                    intent_repo=TradeIntentRepository(session),
                ),
                position_repo=position_repo,
                account_repo=account_repo,
            )
            max_holding = timedelta(
                seconds=(
                    self._config.trade_construction.max_holding_bars
                    * self._config.primary_tf.seconds
                )
            )

            actioned = 0
            for position in managed_positions:
                signal = await signal_repo.get(position.signal_id)
                if signal is None:
                    continue  # signal row unreachable - nothing to manage against

                current_price = cached.bid if position.direction == Direction.LONG else cached.ask
                live_position = LivePosition(
                    id=position.id,
                    broker_position_id=position.broker_position_id,
                    account_id=position.account_id,
                    instrument_id=position.instrument_id,
                    direction=position.direction,
                    initial_volume=position.initial_volume,
                    remaining_volume=position.remaining_volume,
                    entry_price=position.entry_price,
                    initial_stop=position.initial_stop,
                    current_stop=position.current_stop,
                    take_profits=signal.take_profits,
                    partials_taken=position.partials_taken,
                    breakeven_moved=position.breakeven_moved,
                    opened_at=position.opened_at,
                )
                action = decide(
                    live_position,
                    current_price=current_price,
                    atr=atr,
                    cfg=self._config.trade_construction,
                    spec=spec,
                    as_of=as_of,
                    max_holding_duration=max_holding,
                )
                if action.kind != "none":
                    await manager.apply(live_position, action, spec=spec, at=as_of)
                    actioned += 1

            await session.commit()
            return actioned
