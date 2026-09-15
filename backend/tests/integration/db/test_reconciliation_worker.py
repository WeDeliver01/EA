"""SPEC-06 §6 acceptance for `ReconciliationLoop`: proves the wiring
(local_positions/pending_intents built from real repositories, not the
classify() logic itself, which tests/integration/db/test_reconciliation.py
already covers exhaustively) - a clean broker/local state finds nothing,
and a real broker-side position with no local record is detected as
UNKNOWN_AT_BROKER using data this loop itself read from the database."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.execution.enums import OrderSide
from app.domain.market.enums import AssetClass
from app.domain.market.symbol_spec import SymbolSpec
from app.execution.broker import AsyncSimulatedBrokerAdapter, SimulatedBroker
from app.models.tables import Account, PositionRow
from app.services.reconciliation_worker import ReconciliationLoop
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration

_SPEC = SymbolSpec(
    symbol="XAUUSD",
    asset_class=AssetClass.METAL,
    digits=2,
    point=Decimal("0.01"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("1.00"),
    contract_size=Decimal("100"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("50"),
    volume_step=Decimal("0.01"),
    stops_level_points=10,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)


def _loop(db_engine: AsyncEngine) -> ReconciliationLoop:
    return ReconciliationLoop(session_factory=async_sessionmaker(db_engine, expire_on_commit=False))


async def test_clean_state_finds_nothing(db_session: AsyncSession, db_engine: AsyncEngine) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    broker = AsyncSimulatedBrokerAdapter(SimulatedBroker(spec=_SPEC))
    findings = await _loop(db_engine).run_for_account(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        broker=broker,
        as_of=datetime.now(UTC),
    )

    assert findings == []


async def test_an_untracked_broker_position_is_detected_as_orphaned(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    """SPEC-06 §10 row 8: a human opens an unrelated position - nothing
    local (this loop's own DB reads correctly return empty lists), so the
    broker-reported position is UNKNOWN_AT_BROKER with no matching intent."""
    refs = await seed_minimal_refs(db_session)
    account = await db_session.get(Account, refs.account_id)
    assert account is not None
    account.trading_enabled = True
    account.kill_switch_active = False
    await db_session.commit()

    sim = SimulatedBroker(spec=_SPEC)
    sim.simulate_manual_open(
        side=OrderSide.BUY, volume=Decimal("0.20"), price=Decimal("3405"), at=datetime.now(UTC)
    )
    broker = AsyncSimulatedBrokerAdapter(sim)

    findings = await _loop(db_engine).run_for_account(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        broker=broker,
        as_of=datetime.now(UTC),
    )

    assert any(f.kind == "UNKNOWN_AT_BROKER" and f.matched_intent_id is None for f in findings)

    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.account_id == refs.account_id)
        )
    ).scalar_one()
    assert row.status == "ORPHANED"
    # Never auto-closed - the non-negotiable safety property.
    assert sim.get_positions() != ()
