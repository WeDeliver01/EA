"""The process that actually runs the trading loop continuously: candle-
close detection -> strategy evaluation -> execution -> dispatch ->
reconciliation, each on its own asyncio task, sharing the one
`AgentConnectionRegistry` instance the FastAPI app's `/agent/ws` route
already holds on `app.state`.

Runs inside the `api` process (started from `app.main`'s lifespan), not as
a separate service: `WSAgentBroker`/`AgentConnectionRegistry` are
in-process, single-instance state with no cross-process transport (no
Redis-backed command relay exists) - a live agent WebSocket connection is
only reachable from the process that accepted it. A future iteration
could split this into its own service once such a relay exists; today it
would just be unable to ever reach the agent.

Single account/instrument for this MVP pass (`Settings.scheduler_*`) -
matches this codebase's existing single-tenant assumptions elsewhere (one
`AgentConnectionRegistry` entry per account, `WSAgentBroker` constructed
per-account). `GLOBAL_TRADING_ENABLED` (default off) gates only the
dispatch step - decisions are still recorded either way (P3), and
existing intents still get created and queued, but nothing is actually
sent to the broker while it's off. Every loop iteration is wrapped so one
failure never kills the task - it logs and waits for the next tick,
same as a real supervisor would restart it, just without the restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.streams import (
    STREAM_BARS_CLOSED,
    STREAM_INTENTS_PENDING,
    ensure_consumer_group,
    xadd,
)
from app.domain.market.enums import Timeframe
from app.engines.config import StrategyConfig
from app.engines.strategy_engine import StrategyEngine
from app.execution.dispatcher import OutboxDispatcher
from app.repositories.accounts import AccountRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.reconciliation import ReconciliationRepository
from app.repositories.trade_intents import TradeIntentRepository
from app.services.execution_worker import CONSUMER_GROUP as EXECUTION_CONSUMER_GROUP
from app.services.execution_worker import ExecutionWorker
from app.services.reconciliation_worker import ReconciliationLoop
from app.transport.registry import AgentConnectionRegistry
from app.transport.ws_broker import WSAgentBroker
from app.workers.market_scanner import MarketScanner, ScanTarget
from app.workers.strategy_worker import CONSUMER_GROUP as STRATEGY_CONSUMER_GROUP
from app.workers.strategy_worker import StrategyWorker, StrategyWorkerConfig

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    account_id: UUID
    instrument_id: UUID
    strategy_version_id: UUID
    symbol: str
    environment: str
    primary_tf: Timeframe
    context_timeframes: tuple[Timeframe, ...]
    scan_interval_seconds: float
    outbox_dispatch_interval_seconds: float
    reconciliation_interval_seconds: float
    candle_close_grace_ms: int
    global_trading_enabled: bool
    quote_stale_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> SchedulerConfig | None:
        """`None` when the scheduler isn't configured to run at all -
        `SCHEDULER_ACCOUNT_ID` unset is the safe default (nothing trades
        on its own until an operator deliberately configures an account)."""
        if (
            settings.scheduler_account_id is None
            or settings.scheduler_instrument_id is None
            or settings.scheduler_strategy_version_id is None
            or not settings.scheduler_symbol
        ):
            return None
        context = tuple(
            Timeframe(tf.strip())
            for tf in settings.scheduler_context_timeframes.split(",")
            if tf.strip()
        )
        return cls(
            account_id=settings.scheduler_account_id,
            instrument_id=settings.scheduler_instrument_id,
            strategy_version_id=settings.scheduler_strategy_version_id,
            symbol=settings.scheduler_symbol,
            environment=settings.scheduler_environment,
            primary_tf=Timeframe(settings.scheduler_primary_timeframe),
            context_timeframes=context,
            scan_interval_seconds=settings.scheduler_scan_interval_seconds,
            outbox_dispatch_interval_seconds=settings.scheduler_dispatch_interval_seconds,
            reconciliation_interval_seconds=float(settings.reconciliation_interval_seconds),
            candle_close_grace_ms=settings.candle_close_grace_ms,
            global_trading_enabled=settings.global_trading_enabled,
            quote_stale_seconds=settings.quote_stale_seconds,
        )


class Scheduler:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        registry: AgentConnectionRegistry,
        config: SchedulerConfig,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._registry = registry
        self._config = config
        self._broker = WSAgentBroker(account_id=config.account_id, registry=registry)
        self._engine = StrategyEngine(
            StrategyConfig(), strategy_version_id=config.strategy_version_id
        )
        self._scanner = MarketScanner(
            bar_source=self._broker,
            targets=[
                ScanTarget(
                    account_id=config.account_id,
                    instrument_id=config.instrument_id,
                    symbol=config.symbol,
                    timeframes=(config.primary_tf, *config.context_timeframes),
                )
            ],
            candle_close_grace=timedelta(milliseconds=config.candle_close_grace_ms),
        )
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._scanner_loop(), name="scheduler.scanner"),
            asyncio.create_task(self._strategy_loop(), name="scheduler.strategy"),
            asyncio.create_task(self._execution_loop(), name="scheduler.execution"),
            asyncio.create_task(self._dispatch_loop(), name="scheduler.dispatch"),
            asyncio.create_task(self._reconciliation_loop(), name="scheduler.reconciliation"),
        ]
        logger.info("scheduler.started", account_id=str(self._config.account_id))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("scheduler.stopped")

    async def _scanner_loop(self) -> None:
        while True:
            try:
                async with self._session_factory() as session:
                    newly_closed = await self._scanner.scan_once(
                        market_data_repo=MarketDataRepository(session), now=datetime.now(UTC)
                    )
                    await session.commit()
                for event in newly_closed:
                    await xadd(
                        self._redis,
                        STREAM_BARS_CLOSED,
                        {
                            "account_id": str(event.account_id),
                            "instrument_id": str(event.instrument_id),
                            "symbol": event.symbol,
                            "timeframe": event.timeframe.value,
                            "as_of": event.bar.close_time.isoformat(),
                        },
                    )
            except Exception:
                logger.exception("scheduler.scanner_loop_error")
            await asyncio.sleep(self._config.scan_interval_seconds)

    async def _strategy_loop(self) -> None:
        worker = StrategyWorker(
            redis=self._redis,
            session_factory=self._session_factory,
            engine=self._engine,
            config=StrategyWorkerConfig(
                primary_tf=self._config.primary_tf,
                context_timeframes=self._config.context_timeframes,
                environment=self._config.environment,
            ),
            consumer_name="scheduler",
        )
        await ensure_consumer_group(self._redis, STREAM_BARS_CLOSED, STRATEGY_CONSUMER_GROUP)
        while True:
            try:
                await worker.run_once()
            except Exception:
                logger.exception("scheduler.strategy_loop_error")
                await asyncio.sleep(1.0)

    async def _execution_loop(self) -> None:
        worker = ExecutionWorker(
            redis=self._redis, session_factory=self._session_factory, consumer_name="scheduler"
        )
        await ensure_consumer_group(self._redis, STREAM_INTENTS_PENDING, EXECUTION_CONSUMER_GROUP)
        while True:
            try:
                await worker.run_once()
            except Exception:
                logger.exception("scheduler.execution_loop_error")
                await asyncio.sleep(1.0)

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                if self._config.global_trading_enabled:
                    async with self._session_factory() as session:
                        dispatcher = OutboxDispatcher(
                            broker=self._broker,
                            account_repo=AccountRepository(session),
                            outbox_repo=OutboxRepository(session),
                            intent_repo=TradeIntentRepository(session),
                            reconciliation_repo=ReconciliationRepository(session),
                            redis=self._redis,
                            quote_stale_seconds=self._config.quote_stale_seconds,
                        )
                        guard = await dispatcher.execution_guard(
                            self._config.account_id,
                            as_of=datetime.now(UTC),
                            symbol=self._config.symbol,
                        )
                        if guard.passed:
                            await dispatcher.dispatch_pending(
                                account_id=self._config.account_id, as_of=datetime.now(UTC)
                            )
                            await session.commit()
            except Exception:
                logger.exception("scheduler.dispatch_loop_error")
            await asyncio.sleep(self._config.outbox_dispatch_interval_seconds)

    async def _reconciliation_loop(self) -> None:
        loop = ReconciliationLoop(session_factory=self._session_factory)
        while True:
            try:
                await loop.run_for_account(
                    account_id=self._config.account_id,
                    instrument_id=self._config.instrument_id,
                    broker=self._broker,
                    as_of=datetime.now(UTC),
                )
            except Exception:
                logger.exception("scheduler.reconciliation_loop_error")
            await asyncio.sleep(self._config.reconciliation_interval_seconds)
