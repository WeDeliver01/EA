"""SPEC-06 §6: reconciliation, wired to real, live-read local state.

`Reconciler.run()` (Phase 4, fully tested) already does the actual work -
this just builds its two DB-sourced inputs (`local_positions`,
`pending_intents`) from the repositories and hands it a real broker. Lives
in `app.services`, not `app.workers`, for the same reason
`execution_worker` does: `Reconciler` lives in `app.execution`, and
`app.execution`/`app.workers` are mutually exclusive siblings under the
import-linter layering contract.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.execution.enums import ExecutionState, OrderSide
from app.execution.broker import AsyncBroker
from app.execution.event_consumer import EventConsumer
from app.execution.reconciliation import Finding, LocalOpenPosition, PendingIntent, Reconciler
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository
from app.repositories.deals import DealRepository
from app.repositories.positions import PositionRepository
from app.repositories.reconciliation import ReconciliationRepository
from app.repositories.trade_intents import TradeIntentRepository


class ReconciliationLoop:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def run_for_account(
        self,
        *,
        account_id: UUID,
        instrument_id: UUID,
        broker: AsyncBroker,
        as_of: datetime,
    ) -> list[Finding]:
        async with self._session_factory() as session:
            position_repo = PositionRepository(session)
            intent_repo = TradeIntentRepository(session)

            positions = await position_repo.list_open(account_id)
            local_positions = [
                LocalOpenPosition(
                    id=p.id,
                    broker_position_id=p.broker_position_id,
                    volume=p.volume,
                    stop_loss=p.stop_loss,
                    take_profit=p.take_profit,
                    entry_price=p.entry_price,
                )
                for p in positions
            ]

            pending_records = await intent_repo.list_pending(account_id)
            pending_intents = [
                PendingIntent(
                    id=r.id,
                    client_order_id=r.client_order_id,
                    magic=r.magic,
                    symbol=r.symbol,
                    side=OrderSide(r.side),
                    volume=r.volume,
                    created_at=r.created_at,
                    state=ExecutionState(r.state),
                )
                for r in pending_records
            ]

            reconciler = Reconciler(
                session=session,
                broker=broker,
                reconciliation_repo=ReconciliationRepository(session),
                intent_repo=intent_repo,
                position_repo=position_repo,
                event_consumer=EventConsumer(
                    agent_event_repo=AgentEventRepository(session),
                    deal_repo=DealRepository(session),
                    position_repo=position_repo,
                    intent_repo=intent_repo,
                ),
                account_repo=AccountRepository(session),
            )
            findings = await reconciler.run(
                account_id=account_id,
                instrument_id=instrument_id,
                local_positions=local_positions,
                pending_intents=pending_intents,
                as_of=as_of,
            )
            await session.commit()
            return findings
