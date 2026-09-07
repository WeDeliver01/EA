"""SPEC-06 §5 step 12 acceptance: live account/risk state is read fresh
from the database, never trusted from a cached message."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import PositionRow, Trade
from app.repositories.accounts import AccountRepository
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


async def test_load_account_state_maps_all_fields(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = AccountRepository(db_session)
    state = await repo.load_account_state(refs.account_id, as_of=datetime.now(UTC))

    assert state.account_id == refs.account_id
    assert state.currency == "USD"
    assert state.balance == Decimal("10000.00")
    # `state_reported_at` is unset in the seed, so staleness falls back to
    # `updated_at`, which the seed just set to "now" - not stale yet.
    assert state.is_stale is False


async def test_load_account_state_is_stale_past_the_threshold(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = AccountRepository(db_session)
    far_future = datetime.now(UTC) + timedelta(seconds=30)
    state = await repo.load_account_state(refs.account_id, as_of=far_future)
    assert state.is_stale is True


async def test_load_symbol_spec_and_risk_limits(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = AccountRepository(db_session)
    spec = await repo.load_symbol_spec(refs.instrument_id)
    assert spec.symbol == "XAUUSD"
    assert spec.tick_value == Decimal("1.00")

    risk_profile_id, limits = await repo.load_active_risk_limits(refs.account_id)
    assert risk_profile_id == refs.risk_profile_id
    assert limits.risk_per_trade_pct == Decimal("0.005")


async def test_compute_risk_state_aggregates_todays_trades(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    now = datetime.now(UTC)

    position = PositionRow(
        id=uuid4(),
        broker_position_id="1001",
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        direction="LONG",
        status="CLOSED",
        volume=Decimal("0"),
        initial_volume=Decimal("0.10"),
        entry_price=Decimal("3400"),
        realised_pnl=Decimal("-50"),
        opened_at=now - timedelta(hours=2),
        closed_at=now - timedelta(hours=1),
        updated_at=now,
    )
    db_session.add(position)
    await db_session.flush()

    trade = Trade(
        id=uuid4(),
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        position_id=position.id,
        direction="LONG",
        entry_time=now - timedelta(hours=2),
        exit_time=now - timedelta(hours=1),
        holding_seconds=3600,
        entry_price=Decimal("3400"),
        exit_price=Decimal("3395"),
        volume=Decimal("0.10"),
        gross_pnl=Decimal("-50"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("-50"),
        risk_amount=Decimal("100"),
        r_multiple=Decimal("-0.5"),
        exit_reason="STOP",
        created_at=now,
    )
    db_session.add(trade)
    await db_session.commit()

    repo = AccountRepository(db_session)
    risk_state = await repo.compute_risk_state(refs.account_id, as_of=now)

    assert risk_state.realised_pnl_today == Decimal("-50")
    assert risk_state.trades_today == 1
    assert risk_state.consecutive_losses == 1


async def test_apply_realised_pnl_updates_balance_equity_and_peak(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = AccountRepository(db_session)
    await repo.apply_realised_pnl(refs.account_id, net_pnl=Decimal("200"), at=datetime.now(UTC))
    await db_session.commit()

    state = await repo.load_account_state(refs.account_id, as_of=datetime.now(UTC))
    assert state.balance == Decimal("10200.00")
    assert state.equity == Decimal("10200.00")
