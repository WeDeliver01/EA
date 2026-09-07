"""SPEC-10 Phase 1 acceptance: repositories return domain objects, never ORM
instances."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import PositionStatus
from app.domain.execution.intent import Fill, Position
from app.domain.market.enums import Direction
from app.models.base import Base
from app.repositories.deals import DealRepository
from app.repositories.positions import PositionRepository
from app.repositories.projections import PositionProjection, TradeProjection
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


async def test_deal_repository_returns_domain_fills_not_orm_rows(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    from app.domain.execution.enums import DealType, OrderSide

    fill = Fill(
        broker_deal_id="D-1",
        client_order_id="",
        broker_order_id="",
        broker_position_id="P-1",
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.10"),
        price=Decimal("3418.20"),
        commission=Decimal("-0.20"),
        swap=Decimal("0"),
        profit=Decimal("0"),
        executed_at=datetime.now(UTC),
        deal_type=DealType.ENTRY,
    )
    repo = DealRepository(db_session)
    await repo.insert(fill, account_id=refs.account_id, instrument_id=refs.instrument_id)
    await db_session.commit()

    results = await repo.list_for_account(refs.account_id)
    assert len(results) == 1
    for result in results:
        assert isinstance(result, Fill)
        assert not isinstance(result, Base)
        assert not hasattr(result, "__table__")


async def test_position_repository_returns_domain_positions_not_orm_rows(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    projection = PositionProjection(
        broker_position_id="P-OPEN-1",
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        trade_intent_id=None,
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        volume=Decimal("0.10"),
        initial_volume=Decimal("0.10"),
        entry_price=Decimal("3418.20"),
        realised_pnl=Decimal("0"),
        opened_at=datetime.now(UTC),
        closed_at=None,
    )
    repo = PositionRepository(db_session)
    await repo.apply_projections([projection], [])
    await db_session.commit()

    results = await repo.list_open(refs.account_id)
    assert len(results) == 1
    for result in results:
        assert isinstance(result, Position)
        assert not isinstance(result, Base)
        assert not hasattr(result, "__table__")


def test_projection_dataclasses_are_not_orm_types() -> None:
    assert not hasattr(PositionProjection, "__table__")
    assert not hasattr(TradeProjection, "__table__")
