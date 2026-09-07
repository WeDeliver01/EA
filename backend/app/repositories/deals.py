"""Deal persistence and projection rebuilding (SPEC-02 §6-7, P5)."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import DealType, OrderSide
from app.domain.execution.intent import Fill
from app.models.tables import Deal, Instrument
from app.repositories.mappers import deal_row_to_fill
from app.repositories.projections import DealRecord, rebuild_projections_from_deals
from app.repositories.projections import (
    PositionProjection as _PositionProjection,  # re-export for callers
)
from app.repositories.projections import (
    TradeProjection as _TradeProjection,
)

__all__ = ["DealRepository", "_PositionProjection", "_TradeProjection"]


class DealRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        fill: Fill,
        *,
        account_id: UUID,
        instrument_id: UUID,
        order_id: UUID | None = None,
        trade_intent_id: UUID | None = None,
        magic: int | None = None,
        comment: str | None = None,
        raw: dict[str, object] | None = None,
    ) -> UUID:
        row = Deal(
            id=uuid4(),
            broker_deal_id=fill.broker_deal_id,
            account_id=account_id,
            instrument_id=instrument_id,
            order_id=order_id,
            trade_intent_id=trade_intent_id,
            broker_order_id=fill.broker_order_id or None,
            broker_position_id=fill.broker_position_id,
            deal_type=fill.deal_type.value,
            side=fill.side.value,
            volume=fill.volume,
            price=fill.price,
            commission=fill.commission,
            swap=fill.swap,
            profit=fill.profit,
            executed_at=fill.executed_at,
            magic=magic,
            comment=comment,
            raw=raw or {},
            ingested_at=fill.executed_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_for_account(self, account_id: UUID) -> tuple[Fill, ...]:
        stmt = (
            select(Deal, Instrument.canonical_symbol)
            .join(Instrument, Deal.instrument_id == Instrument.id)
            .where(Deal.account_id == account_id)
            .order_by(Deal.executed_at)
        )
        result = await self._session.execute(stmt)
        return tuple(deal_row_to_fill(row, symbol=symbol) for row, symbol in result.all())

    async def rebuild_projections(
        self, account_id: UUID
    ) -> tuple[list[_PositionProjection], list[_TradeProjection]]:
        """Recomputes positions and trades for an account from `deals` alone.

        Does not write anything - see `app.repositories.positions` for the
        upsert side. Kept separate so the same read can be used to *verify*
        the live projection without mutating it.
        """
        stmt = select(Deal).where(Deal.account_id == account_id).order_by(Deal.executed_at)
        result = await self._session.execute(stmt)
        records = [
            DealRecord(
                id=row.id,
                broker_deal_id=row.broker_deal_id,
                account_id=row.account_id,
                instrument_id=row.instrument_id,
                trade_intent_id=row.trade_intent_id,
                broker_position_id=row.broker_position_id,
                deal_type=DealType(row.deal_type),
                side=OrderSide(row.side),
                volume=row.volume,
                price=row.price,
                commission=row.commission,
                swap=row.swap,
                profit=row.profit,
                executed_at=row.executed_at,
            )
            for row in result.scalars().all()
        ]
        return rebuild_projections_from_deals(records)
