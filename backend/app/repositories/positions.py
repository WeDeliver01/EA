"""Position (and Trade) projection persistence, and reads back into domain
objects (SPEC-02 §6, P5)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.intent import Position
from app.models.tables import Instrument, PositionRow, Trade
from app.repositories.mappers import position_row_to_domain
from app.repositories.projections import PositionProjection, TradeProjection


class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def apply_projections(
        self, positions: list[PositionProjection], trades: list[TradeProjection]
    ) -> None:
        """Upserts computed projections. Existing rows are matched on
        (account_id, broker_position_id) and updated in place so ids stay
        stable across a rebuild."""
        for projection in positions:
            stmt = select(PositionRow).where(
                PositionRow.account_id == projection.account_id,
                PositionRow.broker_position_id == projection.broker_position_id,
            )
            existing = (await self._session.execute(stmt)).scalar_one_or_none()
            if existing is None:
                existing = PositionRow(id=uuid4(), updated_at=projection.opened_at)
                self._session.add(existing)

            existing.broker_position_id = projection.broker_position_id
            existing.account_id = projection.account_id
            existing.instrument_id = projection.instrument_id
            existing.trade_intent_id = projection.trade_intent_id
            existing.direction = projection.direction.value
            existing.status = projection.status.value
            existing.volume = projection.volume
            existing.initial_volume = projection.initial_volume
            existing.entry_price = projection.entry_price
            existing.realised_pnl = projection.realised_pnl
            existing.unrealised_pnl = existing.unrealised_pnl or Decimal(0)
            existing.opened_at = projection.opened_at
            existing.closed_at = projection.closed_at
            existing.updated_at = projection.closed_at or projection.opened_at

        await self._session.flush()

        for trade_projection in trades:
            pos_stmt = select(PositionRow).where(
                PositionRow.account_id == trade_projection.account_id,
                PositionRow.broker_position_id == trade_projection.broker_position_id,
            )
            position_row = (await self._session.execute(pos_stmt)).scalar_one()

            existing_trade_stmt = select(Trade).where(Trade.position_id == position_row.id)
            existing_trade = (await self._session.execute(existing_trade_stmt)).scalar_one_or_none()
            if existing_trade is None:
                existing_trade = Trade(
                    id=uuid4(), position_id=position_row.id, created_at=trade_projection.exit_time
                )
                self._session.add(existing_trade)

            existing_trade.account_id = trade_projection.account_id
            existing_trade.instrument_id = trade_projection.instrument_id
            existing_trade.strategy_version_id = None
            existing_trade.direction = trade_projection.direction.value
            existing_trade.entry_time = trade_projection.entry_time
            existing_trade.exit_time = trade_projection.exit_time
            existing_trade.holding_seconds = trade_projection.holding_seconds
            existing_trade.entry_price = trade_projection.entry_price
            existing_trade.exit_price = trade_projection.exit_price
            existing_trade.volume = trade_projection.volume
            existing_trade.gross_pnl = trade_projection.gross_pnl
            existing_trade.commission = trade_projection.commission
            existing_trade.swap = trade_projection.swap
            existing_trade.net_pnl = trade_projection.net_pnl
            existing_trade.risk_amount = existing_trade.risk_amount or Decimal(0)
            existing_trade.r_multiple = existing_trade.r_multiple or Decimal(0)
            existing_trade.exit_reason = existing_trade.exit_reason or "RECONCILED"

        await self._session.flush()

    async def list_open(self, account_id: UUID) -> tuple[Position, ...]:
        stmt = (
            select(PositionRow, Instrument.canonical_symbol)
            .join(Instrument, PositionRow.instrument_id == Instrument.id)
            .where(PositionRow.account_id == account_id, PositionRow.status == "OPEN")
        )
        result = await self._session.execute(stmt)
        return tuple(position_row_to_domain(row, symbol=symbol) for row, symbol in result.all())

    async def get_id_by_broker_position_id(
        self, account_id: UUID, broker_position_id: str
    ) -> UUID | None:
        stmt = select(PositionRow.id).where(
            PositionRow.account_id == account_id,
            PositionRow.broker_position_id == broker_position_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_initial_risk(
        self,
        position_id: UUID,
        *,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        initial_risk: Decimal,
        updated_at: datetime,
    ) -> None:
        """Called once, right after a position is created from a deal -
        `deals` carries no stop/take-profit/risk data (P5's
        projections-from-deals-alone design), so this is the only place
        these fields are ever set from the original sizing decision."""
        row = await self._session.get(PositionRow, position_id)
        if row is None:
            raise LookupError(f"position {position_id} not found")
        row.stop_loss = stop_loss
        row.take_profit = take_profit
        row.initial_stop_loss = stop_loss
        row.initial_risk = initial_risk
        row.updated_at = updated_at
        await self._session.flush()

    async def apply_stop_update(
        self, position_id: UUID, *, new_stop: Decimal, breakeven_moved: bool, updated_at: datetime
    ) -> None:
        """SPEC-06 §8 invariant: the stop only ever moves to reduce risk -
        enforced by the caller (`position_manager.decide`'s `_improves`
        check) before this is called, not re-checked here."""
        row = await self._session.get(PositionRow, position_id)
        if row is None:
            raise LookupError(f"position {position_id} not found")
        row.stop_loss = new_stop
        if breakeven_moved:
            row.breakeven_moved = True
        row.updated_at = updated_at
        await self._session.flush()

    async def increment_partials_taken(self, position_id: UUID, *, updated_at: datetime) -> None:
        """The rung index is persisted here, not inferred from remaining
        volume (SPEC-06 §8: 'Step 4 must not fire twice for the same
        rung')."""
        row = await self._session.get(PositionRow, position_id)
        if row is None:
            raise LookupError(f"position {position_id} not found")
        row.partials_taken += 1
        row.updated_at = updated_at
        await self._session.flush()
