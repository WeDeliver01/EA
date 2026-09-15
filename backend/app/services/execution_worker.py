"""SPEC-06 §5 steps 10-17: consumes `stream:intents:pending` and turns a
persisted `Signal` into a real trade_intent + outbox row via
`app.execution.intent_service.submit_decision`.

Lives in `app.services`, not `app.workers`: this needs
`app.execution.intent_service`, and `app.execution`/`app.workers` are
mutually exclusive at the same import-linter layer (siblings may not
import each other - `app.workers.market_scanner`/`strategy_worker` hit
the same wall the other way and stayed in `app.workers` by defining their
own narrow Protocols instead). Orchestration across layers belongs one
layer up, exactly where SPEC-00 places `app.services`
("Orchestration, transactions").

`submit_decision` takes a `Decision`, not a persisted `Signal` - this
reconstructs a minimal one from the signal's own stored fields
(direction/entry/stop_loss/take_profits/confluence_score), since that is
everything `submit_decision` actually reads off `decision` today (its
`.outcome` must be `TRADE`; `symbol`/`regime`/`confluence_band`/
`evidence`/`gates`/`narrative` are all ignored - `submit_decision` derives
the traded symbol from the instrument's own spec, not from
`decision.symbol`). This is a real coupling to `submit_decision`'s current
field usage, not a stable contract - a future change there that starts
reading one of those placeholder fields needs this reconstruction updated
too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.streams import STREAM_INTENTS_PENDING, xack, xreadgroup
from app.domain.market.enums import Regime
from app.domain.strategy.decision import Decision
from app.domain.strategy.enums import DecisionOutcome
from app.execution.intent_service import submit_decision
from app.repositories.accounts import AccountRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.signals import SignalRepository
from app.repositories.trade_intents import TradeIntentRepository

logger = structlog.get_logger(__name__)

CONSUMER_GROUP = "execution-worker"


class ExecutionWorker:
    def __init__(
        self,
        *,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        consumer_name: str,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._consumer_name = consumer_name

    async def run_once(self, *, count: int = 10, block_ms: int = 5000) -> int:
        entries = await xreadgroup(
            self._redis,
            STREAM_INTENTS_PENDING,
            CONSUMER_GROUP,
            self._consumer_name,
            count=count,
            block_ms=block_ms,
        )
        for entry_id, fields in entries:
            try:
                await self._process(fields)
            except Exception:
                logger.exception(
                    "execution_worker.process_failed", entry_id=entry_id, fields=fields
                )
                continue
            await xack(self._redis, STREAM_INTENTS_PENDING, CONSUMER_GROUP, entry_id)
        return len(entries)

    async def _process(self, fields: dict[str, str]) -> None:
        signal_id = UUID(fields["signal_id"])
        account_id = UUID(fields["account_id"])
        instrument_id = UUID(fields["instrument_id"])
        strategy_version_id = UUID(fields["strategy_version_id"])
        environment = fields["environment"]

        async with self._session_factory() as session:
            signal_repo = SignalRepository(session)
            account_repo = AccountRepository(session)
            outbox_repo = OutboxRepository(session)
            intent_repo = TradeIntentRepository(session)

            signal = await signal_repo.get(signal_id)
            if signal is None:
                # Nothing to do - the signal it names doesn't exist. Not
                # expected in normal operation (strategy_worker always
                # commits the signal before publishing this message), but
                # not a reason to crash the worker either.
                logger.warning("execution_worker.signal_not_found", signal_id=str(signal_id))
                return

            now = datetime.now(UTC)
            account = await account_repo.load_account_state(account_id, as_of=now)

            decision = Decision(
                outcome=DecisionOutcome.TRADE,
                symbol="",
                as_of=now,
                strategy_version_id=strategy_version_id,
                regime=Regime.EXPANSION,
                setup=None,
                direction=signal.direction,
                entry=signal.entry,
                stop_loss=signal.stop_loss,
                take_profits=signal.take_profits,
                confluence_score=signal.confluence_score,
                confluence_band="",
                evidence=(),
                gates=(),
                narrative="",
                engine_duration_ms=0,
            )

            await submit_decision(
                decision,
                account_repo=account_repo,
                outbox_repo=outbox_repo,
                intent_repo=intent_repo,
                signal_id=signal_id,
                account_id=account_id,
                instrument_id=instrument_id,
                strategy_version_id=strategy_version_id,
                environment=environment,
                leverage=account.leverage,
                as_of=now,
            )
            await session.commit()
