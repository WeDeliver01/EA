"""SPEC-06 §5 steps 1-9: reacts to `stream:bars:closed`, builds a live
`MarketState`, runs the pure `StrategyEngine`, records the decision either
way (P3), and - on `TRADE` - persists a `Signal` and queues it for
execution via `stream:intents:pending`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.redis import redis_lock
from app.core.streams import STREAM_BARS_CLOSED, STREAM_INTENTS_PENDING, xack, xadd, xreadgroup
from app.domain.market.enums import Timeframe
from app.domain.strategy.enums import DecisionOutcome
from app.engines.strategy_engine import StrategyEngine
from app.repositories.accounts import AccountRepository
from app.repositories.analysis_runs import AnalysisRunRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.positions import PositionRepository
from app.repositories.signals import SignalRepository
from app.workers.market_state import build_market_state

logger = structlog.get_logger(__name__)

CONSUMER_GROUP = "strategy-worker"
_ANALYSE_LOCK_TTL_SECONDS = 30


@dataclass(frozen=True, slots=True)
class StrategyWorkerConfig:
    primary_tf: Timeframe
    context_timeframes: tuple[Timeframe, ...]
    environment: str  # "demo" | "live" - carried onto the intents:pending message


class StrategyWorker:
    def __init__(
        self,
        *,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        engine: StrategyEngine,
        config: StrategyWorkerConfig,
        consumer_name: str,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._engine = engine
        self._config = config
        self._consumer_name = consumer_name

    async def run_once(self, *, count: int = 10, block_ms: int = 5000) -> int:
        """Reads and processes up to `count` new `stream:bars:closed`
        entries, ack'ing each only once its effect is durably persisted (or
        once it's determined there is nothing to do - a context-timeframe
        close, or a lock already held). Returns how many entries were read,
        for the caller to log or pace on."""
        entries = await xreadgroup(
            self._redis,
            STREAM_BARS_CLOSED,
            CONSUMER_GROUP,
            self._consumer_name,
            count=count,
            block_ms=block_ms,
        )
        for entry_id, fields in entries:
            try:
                await self._process(fields)
            except Exception:
                logger.exception("strategy_worker.process_failed", entry_id=entry_id, fields=fields)
                # Left un-acked: a stuck entry needs an XCLAIM-based retry
                # sweep to ever be redelivered (not implemented yet - see
                # the ADR) rather than being silently lost, so this at
                # least doesn't compound the gap by acking a failure.
                continue
            await xack(self._redis, STREAM_BARS_CLOSED, CONSUMER_GROUP, entry_id)
        return len(entries)

    async def _process(self, fields: dict[str, str]) -> None:
        timeframe = Timeframe(fields["timeframe"])
        if timeframe != self._config.primary_tf:
            return  # only the primary timeframe's own close triggers an evaluation

        account_id = UUID(fields["account_id"])
        instrument_id = UUID(fields["instrument_id"])
        symbol = fields["symbol"]
        as_of = datetime.fromisoformat(fields["as_of"])

        lock_key = f"lock:analyse:{account_id}:{symbol}:{timeframe.value}"
        async with redis_lock(
            self._redis, lock_key, ttl_seconds=_ANALYSE_LOCK_TTL_SECONDS
        ) as acquired:
            if not acquired:
                return  # another process already has this - not a failure

            signal_id: UUID | None = None
            async with self._session_factory() as session:
                account_repo = AccountRepository(session)
                market_data_repo = MarketDataRepository(session)
                position_repo = PositionRepository(session)
                signal_repo = SignalRepository(session)
                analysis_run_repo = AnalysisRunRepository(session)

                state = await build_market_state(
                    account_repo=account_repo,
                    market_data_repo=market_data_repo,
                    position_repo=position_repo,
                    signal_repo=signal_repo,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    symbol=symbol,
                    primary_tf=self._config.primary_tf,
                    context_timeframes=self._config.context_timeframes,
                    as_of=as_of,
                )

                started = time.monotonic()
                decision = self._engine.evaluate(state)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                decision = replace(decision, engine_duration_ms=elapsed_ms)

                analysis_run_id = await analysis_run_repo.create_from_decision(
                    decision,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    mode="live",
                    created_at=as_of,
                )

                if decision.outcome is DecisionOutcome.TRADE:
                    signal_id = await signal_repo.create(
                        decision,
                        reference=f"{account_id}:{symbol}:{timeframe.value}:{as_of.isoformat()}",
                        analysis_run_id=analysis_run_id,
                        account_id=account_id,
                        instrument_id=instrument_id,
                        strategy_version_id=decision.strategy_version_id,
                        created_at=as_of,
                    )

                await session.commit()

        if signal_id is None:
            return

        # Published outside the lock and the DB transaction: the signal is
        # already durably committed by this point, so a crash here loses
        # at most this one XADD, not the record of the decision itself -
        # the safer failure mode (SPEC-06's own default-to-not-trading
        # principle) over risking a duplicate.
        await xadd(
            self._redis,
            STREAM_INTENTS_PENDING,
            {
                "signal_id": str(signal_id),
                "account_id": str(account_id),
                "instrument_id": str(instrument_id),
                "strategy_version_id": str(decision.strategy_version_id),
                "environment": self._config.environment,
            },
        )
