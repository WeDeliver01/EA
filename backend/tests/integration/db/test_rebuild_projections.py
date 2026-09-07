"""SPEC-10 Phase 1 acceptance: rebuild_projections() reproduces `positions`
and `trades` from `deals` exactly, on a seeded fixture of 500 deals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Deal, PositionRow, Trade
from app.repositories.deals import DealRepository
from app.repositories.positions import PositionRepository
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


def _make_deals(refs, *, num_closed: int, num_open: int) -> tuple[list[Deal], dict]:
    """250 closed round trips (1 entry + 1 exit each = 500 deals) plus a
    handful of still-open positions, with hand-computable expected values."""
    deals: list[Deal] = []
    expected: dict[str, dict] = {}
    base_time = datetime(2026, 1, 1, tzinfo=UTC)

    for i in range(num_closed):
        broker_position_id = f"CLOSED-{i}"
        entry_price = Decimal("3400.00") + Decimal(i)
        exit_price = entry_price + (Decimal("5.00") if i % 2 == 0 else Decimal("-3.00"))
        volume = Decimal("0.10")
        entry_time = base_time + timedelta(minutes=i)
        exit_time = entry_time + timedelta(minutes=15)
        entry_profit = Decimal("0")
        exit_profit = (exit_price - entry_price) * volume * 100  # contract_size=100

        deals.append(
            Deal(
                id=uuid4(),
                broker_deal_id=f"{broker_position_id}-ENTRY",
                account_id=refs.account_id,
                instrument_id=refs.instrument_id,
                broker_position_id=broker_position_id,
                deal_type="ENTRY",
                side="BUY",
                volume=volume,
                price=entry_price,
                commission=Decimal("-0.20"),
                swap=Decimal("0"),
                profit=entry_profit,
                executed_at=entry_time,
                raw={},
                ingested_at=entry_time,
            )
        )
        deals.append(
            Deal(
                id=uuid4(),
                broker_deal_id=f"{broker_position_id}-EXIT",
                account_id=refs.account_id,
                instrument_id=refs.instrument_id,
                broker_position_id=broker_position_id,
                deal_type="EXIT",
                side="SELL",
                volume=volume,
                price=exit_price,
                commission=Decimal("-0.20"),
                swap=Decimal("-0.05"),
                profit=exit_profit,
                executed_at=exit_time,
                raw={},
                ingested_at=exit_time,
            )
        )

        gross_pnl = entry_profit + exit_profit
        commission = Decimal("-0.20") + Decimal("-0.20")
        swap = Decimal("0") + Decimal("-0.05")
        expected[broker_position_id] = {
            "status": "CLOSED",
            "volume": Decimal("0"),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_pnl": gross_pnl,
            "commission": commission,
            "swap": swap,
            "net_pnl": gross_pnl + commission + swap,
        }

    for i in range(num_open):
        broker_position_id = f"OPEN-{i}"
        entry_price = Decimal("3500.00") + Decimal(i)
        volume = Decimal("0.05")
        entry_time = base_time + timedelta(hours=1, minutes=i)

        deals.append(
            Deal(
                id=uuid4(),
                broker_deal_id=f"{broker_position_id}-ENTRY",
                account_id=refs.account_id,
                instrument_id=refs.instrument_id,
                broker_position_id=broker_position_id,
                deal_type="ENTRY",
                side="BUY",
                volume=volume,
                price=entry_price,
                commission=Decimal("-0.10"),
                swap=Decimal("0"),
                profit=Decimal("0"),
                executed_at=entry_time,
                raw={},
                ingested_at=entry_time,
            )
        )
        expected[broker_position_id] = {
            "status": "OPEN",
            "volume": volume,
            "entry_price": entry_price,
        }

    return deals, expected


async def test_rebuild_projections_matches_hand_computed_expectations(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    deals, expected = _make_deals(refs, num_closed=250, num_open=10)
    assert len(deals) == 500 + 10  # 250 round trips (500 deals) + 10 open entries

    db_session.add_all(deals)
    await db_session.commit()

    deal_repo = DealRepository(db_session)
    position_repo = PositionRepository(db_session)

    positions, trades = await deal_repo.rebuild_projections(refs.account_id)
    assert len(positions) == 260
    assert len(trades) == 250

    await position_repo.apply_projections(positions, trades)
    await db_session.commit()

    stored_positions = (
        (
            await db_session.execute(
                select(PositionRow).where(PositionRow.account_id == refs.account_id)
            )
        )
        .scalars()
        .all()
    )
    stored_trades = (
        (await db_session.execute(select(Trade).where(Trade.account_id == refs.account_id)))
        .scalars()
        .all()
    )

    assert len(stored_positions) == 260
    assert len(stored_trades) == 250

    positions_by_broker_id = {p.broker_position_id: p for p in stored_positions}
    for broker_position_id, exp in expected.items():
        row = positions_by_broker_id[broker_position_id]
        assert row.status == exp["status"]
        assert row.volume == exp["volume"]
        assert row.entry_price == exp["entry_price"]

    trades_by_position = {
        positions_by_broker_id[bid].id: bid
        for bid in expected
        if expected[bid]["status"] == "CLOSED"
    }
    for trade in stored_trades:
        broker_position_id = trades_by_position[trade.position_id]
        exp = expected[broker_position_id]
        assert trade.entry_price == exp["entry_price"]
        assert trade.exit_price == exp["exit_price"]
        assert trade.gross_pnl == exp["gross_pnl"]
        assert trade.commission == exp["commission"]
        assert trade.swap == exp["swap"]
        assert trade.net_pnl == exp["net_pnl"]


async def test_rebuild_projections_is_idempotent(db_session: AsyncSession) -> None:
    """Running the rebuild twice must not create duplicate rows or drift."""
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    deals, _ = _make_deals(refs, num_closed=5, num_open=2)
    db_session.add_all(deals)
    await db_session.commit()

    deal_repo = DealRepository(db_session)
    position_repo = PositionRepository(db_session)

    for _ in range(2):
        positions, trades = await deal_repo.rebuild_projections(refs.account_id)
        await position_repo.apply_projections(positions, trades)
        await db_session.commit()

    stored_positions = (
        (
            await db_session.execute(
                select(PositionRow).where(PositionRow.account_id == refs.account_id)
            )
        )
        .scalars()
        .all()
    )
    stored_trades = (
        (await db_session.execute(select(Trade).where(Trade.account_id == refs.account_id)))
        .scalars()
        .all()
    )

    assert len(stored_positions) == 7
    assert len(stored_trades) == 5
