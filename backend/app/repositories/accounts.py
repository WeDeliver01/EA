"""Live account/risk state, read fresh from the database on every use.

SPEC-06 §5 step 12: "reload live account state, risk state, open positions
FROM DB, not from the message" - the whole reason `execution_worker` re-runs
the risk engine instead of trusting the signal it was handed. Nothing here
is cached between calls.

Equity is tracked at trade close, not mark-to-market intrabar - the same
simplification `research/backtester.py` documents, extended to the live
path for the same reason: floating P&L needs a live quote stream, which
this MVP pass doesn't wire up (see docs/adr/0001-mvp-scope.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.symbol_spec import SymbolSpec
from app.domain.portfolio.account_state import AccountState
from app.domain.risk.limits import RiskLimits
from app.domain.risk.state import RiskState
from app.models.tables import Account, Instrument, PositionRow, RiskProfile, Trade
from app.repositories.mappers import (
    account_row_to_domain,
    instrument_row_to_spec,
    risk_profile_row_to_limits,
)

_ACCOUNT_STALE_SECONDS = 10


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_account_state(self, account_id: UUID, *, as_of: datetime) -> AccountState:
        row = await self._session.get(Account, account_id)
        if row is None:
            raise LookupError(f"account {account_id} not found")
        reported_at = row.state_reported_at or row.updated_at
        is_stale = (as_of - reported_at) > timedelta(seconds=_ACCOUNT_STALE_SECONDS)
        return account_row_to_domain(row, is_stale=is_stale)

    async def load_symbol_spec(self, instrument_id: UUID) -> SymbolSpec:
        row = await self._session.get(Instrument, instrument_id)
        if row is None:
            raise LookupError(f"instrument {instrument_id} not found")
        return instrument_row_to_spec(row)

    async def load_active_risk_limits(self, account_id: UUID) -> tuple[UUID, RiskLimits]:
        stmt = select(RiskProfile).where(
            RiskProfile.account_id == account_id, RiskProfile.is_active.is_(True)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise LookupError(f"no active risk profile for account {account_id}")
        return row.id, risk_profile_row_to_limits(row)

    async def compute_risk_state(self, account_id: UUID, *, as_of: datetime) -> RiskState:
        day_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=as_of.weekday())

        today_trades = await self._trades_since(account_id, since=day_start)
        week_trades = await self._trades_since(account_id, since=week_start)
        recent_trades = await self._recent_trades(account_id, limit=50)

        consecutive_losses = 0
        for trade in recent_trades:  # newest first
            if trade.net_pnl < 0:
                consecutive_losses += 1
            else:
                break

        open_positions_stmt = select(PositionRow).where(
            PositionRow.account_id == account_id, PositionRow.status == "OPEN"
        )
        open_positions = (await self._session.execute(open_positions_stmt)).scalars().all()
        open_risk = sum(
            (p.initial_risk for p in open_positions if p.initial_risk is not None), Decimal(0)
        )

        account = await self._session.get(Account, account_id)
        if account is None:
            raise LookupError(f"account {account_id} not found")
        peak_equity = account.peak_equity or account.equity
        current_drawdown_pct = (
            (peak_equity - account.equity) / peak_equity if peak_equity > 0 else Decimal(0)
        )

        return RiskState(
            as_of=as_of,
            realised_pnl_today=sum((t.net_pnl for t in today_trades), Decimal(0)),
            realised_pnl_week=sum((t.net_pnl for t in week_trades), Decimal(0)),
            open_risk=open_risk,
            trades_today=len(today_trades),
            open_position_count=len(open_positions),
            consecutive_losses=consecutive_losses,
            peak_equity=peak_equity,
            current_drawdown_pct=current_drawdown_pct,
            trading_enabled=account.trading_enabled,
            kill_switch_active=account.kill_switch_active,
        )

    async def apply_realised_pnl(self, account_id: UUID, *, net_pnl: Decimal, at: datetime) -> None:
        account = await self._session.get(Account, account_id)
        if account is None:
            raise LookupError(f"account {account_id} not found")
        account.balance += net_pnl
        account.equity = account.balance
        account.free_margin = account.equity - account.margin
        account.peak_equity = max(account.peak_equity or account.balance, account.equity)
        account.state_reported_at = at
        await self._session.flush()

    async def _trades_since(self, account_id: UUID, *, since: datetime) -> tuple[Trade, ...]:
        stmt = select(Trade).where(Trade.account_id == account_id, Trade.exit_time >= since)
        result = await self._session.execute(stmt)
        return tuple(result.scalars().all())

    async def _recent_trades(self, account_id: UUID, *, limit: int) -> tuple[Trade, ...]:
        stmt = (
            select(Trade)
            .where(Trade.account_id == account_id)
            .order_by(Trade.exit_time.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return tuple(result.scalars().all())
