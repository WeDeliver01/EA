"""SPEC-10 Phase 1 acceptance: `UPDATE` on `deals` is silently a no-op."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Deal
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


async def _insert_deal(session: AsyncSession, refs, *, broker_deal_id: str) -> Deal:
    now = datetime.now(UTC)
    deal = Deal(
        id=uuid4(),
        broker_deal_id=broker_deal_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        broker_position_id="POS-1",
        deal_type="ENTRY",
        side="BUY",
        volume=Decimal("0.10"),
        price=Decimal("3418.20"),
        commission=Decimal("-0.50"),
        swap=Decimal("0"),
        profit=Decimal("0"),
        executed_at=now,
        raw={},
        ingested_at=now,
    )
    session.add(deal)
    await session.flush()
    await session.commit()
    return deal


async def test_update_on_deals_is_a_no_op(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()
    deal = await _insert_deal(db_session, refs, broker_deal_id="DEAL-1")

    await db_session.execute(
        text("UPDATE deals SET price = :new_price WHERE id = :id"),
        {"new_price": Decimal("9999.99"), "id": deal.id},
    )
    await db_session.commit()

    reloaded = (await db_session.execute(select(Deal).where(Deal.id == deal.id))).scalar_one()
    assert reloaded.price == Decimal("3418.2000000000")


async def test_delete_on_deals_is_a_no_op(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()
    deal = await _insert_deal(db_session, refs, broker_deal_id="DEAL-2")

    await db_session.execute(text("DELETE FROM deals WHERE id = :id"), {"id": deal.id})
    await db_session.commit()

    reloaded = (
        await db_session.execute(select(Deal).where(Deal.id == deal.id))
    ).scalar_one_or_none()
    assert reloaded is not None
