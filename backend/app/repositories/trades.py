"""SPEC-03 §7: closed trade history reads. `trades` is a projection,
rebuildable from `deals` alone (see `app.repositories.projections`) - this
module only reads it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Instrument, Trade


@dataclass(frozen=True, slots=True)
class TradeSummary:
    id: UUID
    account_id: UUID
    instrument_id: UUID
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    volume: Decimal
    net_pnl: Decimal
    r_multiple: Decimal
    exit_reason: str


@dataclass(frozen=True, slots=True)
class TradeDetail:
    id: UUID
    account_id: UUID
    instrument_id: UUID
    symbol: str
    position_id: UUID
    signal_id: UUID | None
    strategy_version_id: UUID | None
    direction: str
    entry_time: datetime
    exit_time: datetime
    holding_seconds: int
    entry_price: Decimal
    exit_price: Decimal
    volume: Decimal
    gross_pnl: Decimal
    commission: Decimal
    swap: Decimal
    net_pnl: Decimal
    risk_amount: Decimal
    r_multiple: Decimal
    mae: Decimal | None
    mfe: Decimal | None
    exit_reason: str


class TradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_trades(
        self,
        *,
        account_id: UUID | None = None,
        strategy_version_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> tuple[TradeSummary, ...]:
        stmt = select(Trade, Instrument.canonical_symbol).join(
            Instrument, Trade.instrument_id == Instrument.id
        )
        if account_id is not None:
            stmt = stmt.where(Trade.account_id == account_id)
        if strategy_version_id is not None:
            stmt = stmt.where(Trade.strategy_version_id == strategy_version_id)
        if since is not None:
            stmt = stmt.where(Trade.exit_time >= since)
        if until is not None:
            stmt = stmt.where(Trade.exit_time <= until)
        stmt = stmt.order_by(Trade.exit_time.desc()).limit(limit)

        result = await self._session.execute(stmt)
        return tuple(
            TradeSummary(
                id=row.id,
                account_id=row.account_id,
                instrument_id=row.instrument_id,
                symbol=symbol,
                direction=row.direction,
                entry_time=row.entry_time,
                exit_time=row.exit_time,
                entry_price=row.entry_price,
                exit_price=row.exit_price,
                volume=row.volume,
                net_pnl=row.net_pnl,
                r_multiple=row.r_multiple,
                exit_reason=row.exit_reason,
            )
            for row, symbol in result.all()
        )

    async def get_trade(self, trade_id: UUID) -> TradeDetail | None:
        stmt = (
            select(Trade, Instrument.canonical_symbol)
            .join(Instrument, Trade.instrument_id == Instrument.id)
            .where(Trade.id == trade_id)
        )
        result = await self._session.execute(stmt)
        row_pair = result.one_or_none()
        if row_pair is None:
            return None
        row, symbol = row_pair
        return TradeDetail(
            id=row.id,
            account_id=row.account_id,
            instrument_id=row.instrument_id,
            symbol=symbol,
            position_id=row.position_id,
            signal_id=row.signal_id,
            strategy_version_id=row.strategy_version_id,
            direction=row.direction,
            entry_time=row.entry_time,
            exit_time=row.exit_time,
            holding_seconds=row.holding_seconds,
            entry_price=row.entry_price,
            exit_price=row.exit_price,
            volume=row.volume,
            gross_pnl=row.gross_pnl,
            commission=row.commission,
            swap=row.swap,
            net_pnl=row.net_pnl,
            risk_amount=row.risk_amount,
            r_multiple=row.r_multiple,
            mae=row.mae,
            mfe=row.mfe,
            exit_reason=row.exit_reason,
        )
