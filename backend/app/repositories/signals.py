"""SPEC-05 §3.5: recent signals, read back for `MarketState.recent_signals`
so `SetupDetector`'s `DUPLICATE_SETUP` gate can suppress a re-trigger of a
setup it already signalled recently, without the engine touching the
database directly (the engine only ever sees the `SignalRef`s it's handed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.enums import Direction
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.signal_ref import SignalRef
from app.models.tables import Signal


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """Just enough of a persisted `Signal` for `execution_worker` to act on
    it - a plain dataclass, never the ORM row (`app/repositories/`'s own
    rule: translate, don't leak)."""

    id: UUID
    account_id: UUID
    instrument_id: UUID
    strategy_version_id: UUID
    direction: Direction
    entry: Decimal
    stop_loss: Decimal
    take_profits: tuple[TakeProfit, ...]
    confluence_score: Decimal


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        decision: Decision,
        *,
        reference: str,
        analysis_run_id: UUID,
        account_id: UUID,
        instrument_id: UUID,
        strategy_version_id: UUID,
        created_at: datetime,
    ) -> UUID:
        """A `TRADE` decision's own record of itself - `strategy_worker`
        writes this (SPEC-06 §5 step 7) before the intent side of the
        pipeline (`submit_decision`) ever runs, since `trade_intents.signal_id`
        is a real FK to this table."""
        assert decision.direction is not None
        assert decision.entry is not None
        assert decision.stop_loss is not None
        row = Signal(
            id=uuid4(),
            reference=reference,
            analysis_run_id=analysis_run_id,
            account_id=account_id,
            instrument_id=instrument_id,
            strategy_version_id=strategy_version_id,
            direction=decision.direction.value,
            setup_kind=decision.setup.kind if decision.setup is not None else "NONE",
            setup_fingerprint=(
                decision.setup.fingerprint if decision.setup is not None else reference
            ),
            entry=decision.entry,
            stop_loss=decision.stop_loss,
            take_profits=[
                {
                    "level": str(tp.level),
                    "fraction": str(tp.fraction),
                    "r_multiple": str(tp.r_multiple),
                }
                for tp in decision.take_profits
            ],
            confluence_score=decision.confluence_score,
            expires_at=created_at,
            created_at=created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def get(self, signal_id: UUID) -> SignalRecord | None:
        row = await self._session.get(Signal, signal_id)
        if row is None:
            return None
        return SignalRecord(
            id=row.id,
            account_id=row.account_id,
            instrument_id=row.instrument_id,
            strategy_version_id=row.strategy_version_id,
            direction=Direction(row.direction),
            entry=row.entry,
            stop_loss=row.stop_loss,
            take_profits=tuple(
                TakeProfit(
                    level=Decimal(tp["level"]),
                    fraction=Decimal(tp["fraction"]),
                    r_multiple=Decimal(tp["r_multiple"]),
                )
                for tp in row.take_profits
            ),
            confluence_score=row.confluence_score,
        )

    async def list_recent(
        self, account_id: UUID, instrument_id: UUID, *, symbol: str, since: datetime
    ) -> tuple[SignalRef, ...]:
        result = await self._session.execute(
            select(Signal)
            .where(
                Signal.account_id == account_id,
                Signal.instrument_id == instrument_id,
                Signal.created_at >= since,
            )
            .order_by(Signal.created_at)
        )
        return tuple(
            SignalRef(
                id=row.id,
                symbol=symbol,
                setup_fingerprint=row.setup_fingerprint,
                direction=Direction(row.direction),
                created_at=row.created_at,
            )
            for row in result.scalars().all()
        )
