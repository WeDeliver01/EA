"""SPEC-04 §5 acceptance for `route_unsolicited_event` - `event.heartbeat`
and `event.deal` landing correctly against a real database."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.event_consumer import EventConsumer
from app.models.tables import Agent, AgentHeartbeat, PositionRow
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agents import AgentRepository
from app.repositories.deals import DealRepository
from app.repositories.positions import PositionRepository
from app.repositories.trade_intents import TradeIntentRepository
from app.transport.envelope import Envelope
from app.transport.event_router import route_unsolicited_event
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration

_KEY = "test-encryption-key-not-for-production"


def _event_consumer(db_session: AsyncSession) -> EventConsumer:
    return EventConsumer(
        agent_event_repo=AgentEventRepository(db_session),
        deal_repo=DealRepository(db_session),
        position_repo=PositionRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
    )


async def test_heartbeat_event_is_recorded_and_touches_last_seen(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    agent_repo = AgentRepository(db_session, encryption_key=_KEY)
    creds = await agent_repo.create(
        account_id=refs.account_id, name="win-vps-1", created_at=datetime.now(UTC)
    )
    await db_session.commit()

    envelope = Envelope(
        v=1,
        type="event.heartbeat",
        id="evt-1",
        correlation_id=None,
        ts="2026-09-07T09:14:22.318Z",
        payload={
            "agent_time": "2026-09-07T09:14:22.318000+00:00",
            "broker_time": "2026-09-07T12:14:22+00:00",
            "terminal_connected": True,
            "trade_allowed": True,
            "algo_trading_enabled": True,
            "build": 4620,
            "account": {
                "login": "51234567",
                "currency": "USD",
                "leverage": 500,
                "balance": "10000.00",
                "equity": "10142.30",
                "margin": "212.40",
                "free_margin": "9929.90",
                "margin_level": "4775.10",
            },
            "open_position_count": 1,
            "open_position_hash": "abc123",
            "symbols": [],
            "agent_version": "1.4.2",
        },
    )

    await route_unsolicited_event(
        envelope,
        agent_id=creds.agent_id,
        account_id=refs.account_id,
        agent_repo=agent_repo,
        account_repo=AccountRepository(db_session),
        event_consumer=_event_consumer(db_session),
    )
    await db_session.commit()

    heartbeat = (
        await db_session.execute(
            select(AgentHeartbeat).where(AgentHeartbeat.agent_id == creds.agent_id)
        )
    ).scalar_one()
    assert heartbeat.terminal_connected is True
    assert heartbeat.balance == Decimal("10000.00")
    assert heartbeat.equity == Decimal("10142.30")
    assert heartbeat.open_position_count == 1

    agent_row = await db_session.get(Agent, creds.agent_id)
    assert agent_row is not None
    assert agent_row.last_seen_at is not None


async def test_heartbeat_with_no_broker_time_is_recorded_as_null(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    agent_repo = AgentRepository(db_session, encryption_key=_KEY)
    creds = await agent_repo.create(
        account_id=refs.account_id, name="win-vps-2", created_at=datetime.now(UTC)
    )
    await db_session.commit()

    envelope = Envelope(
        v=1,
        type="event.heartbeat",
        id="evt-2",
        correlation_id=None,
        ts="2026-09-07T09:14:22.318Z",
        payload={
            "agent_time": "2026-09-07T09:14:22.318000+00:00",
            "broker_time": None,
            "terminal_connected": False,
            "trade_allowed": False,
            "algo_trading_enabled": False,
            "build": 4620,
            "account": {},
            "open_position_count": 0,
            "open_position_hash": "",
            "symbols": [],
            "agent_version": "1.4.2",
        },
    )

    await route_unsolicited_event(
        envelope,
        agent_id=creds.agent_id,
        account_id=refs.account_id,
        agent_repo=agent_repo,
        account_repo=AccountRepository(db_session),
        event_consumer=_event_consumer(db_session),
    )
    await db_session.commit()

    heartbeat = (
        await db_session.execute(
            select(AgentHeartbeat).where(AgentHeartbeat.agent_id == creds.agent_id)
        )
    ).scalar_one()
    assert heartbeat.broker_time is None
    assert heartbeat.balance is None


async def test_deal_event_creates_an_orphaned_position(db_session: AsyncSession) -> None:
    """A manual trade a human placed in the terminal, with no matching
    intent - discovered via the deal-watcher's independent poll, not a
    command reply. `record_deal` rebuilds positions from deals regardless
    of origin, so this lands the same way `EventConsumer._apply` would."""
    refs = await seed_minimal_refs(db_session)
    agent_repo = AgentRepository(db_session, encryption_key=_KEY)
    creds = await agent_repo.create(
        account_id=refs.account_id, name="win-vps-3", created_at=datetime.now(UTC)
    )
    await db_session.commit()

    envelope = Envelope(
        v=1,
        type="event.deal",
        id="evt-3",
        correlation_id=None,
        ts="2026-09-07T09:14:22.318Z",
        payload={
            "broker_deal_id": "999",
            "client_order_id": "",
            "broker_order_id": "998",
            "broker_position_id": "998",
            "symbol": "XAUUSD",
            "side": "BUY",
            "volume": "0.05",
            "price": "3405.00",
            "commission": "0",
            "swap": "0",
            "profit": "0",
            "executed_at": "2026-09-07T09:14:22.318000+00:00",
            "deal_type": "ENTRY",
        },
    )

    await route_unsolicited_event(
        envelope,
        agent_id=creds.agent_id,
        account_id=refs.account_id,
        agent_repo=agent_repo,
        account_repo=AccountRepository(db_session),
        event_consumer=_event_consumer(db_session),
    )
    await db_session.commit()

    row = (
        await db_session.execute(select(PositionRow).where(PositionRow.broker_position_id == "998"))
    ).scalar_one()
    assert row.status == "OPEN"
    assert row.volume == Decimal("0.05")


async def test_unhandled_event_type_is_a_no_op(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    agent_repo = AgentRepository(db_session, encryption_key=_KEY)
    creds = await agent_repo.create(
        account_id=refs.account_id, name="win-vps-4", created_at=datetime.now(UTC)
    )
    await db_session.commit()

    envelope = Envelope(
        v=1,
        type="event.quote",
        id="evt-4",
        correlation_id=None,
        ts="2026-09-07T09:14:22.318Z",
        payload={"symbol": "XAUUSD", "bid": "3400", "ask": "3400.5"},
    )

    await route_unsolicited_event(
        envelope,
        agent_id=creds.agent_id,
        account_id=refs.account_id,
        agent_repo=agent_repo,
        account_repo=AccountRepository(db_session),
        event_consumer=_event_consumer(db_session),
    )  # must not raise


async def test_deal_for_a_symbol_with_no_matching_instrument_is_dropped(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    agent_repo = AgentRepository(db_session, encryption_key=_KEY)
    creds = await agent_repo.create(
        account_id=refs.account_id, name="win-vps-5", created_at=datetime.now(UTC)
    )
    await db_session.commit()

    envelope = Envelope(
        v=1,
        type="event.deal",
        id="evt-5",
        correlation_id=None,
        ts="2026-09-07T09:14:22.318Z",
        payload={
            "broker_deal_id": "1",
            "client_order_id": "",
            "broker_order_id": "1",
            "broker_position_id": "1",
            "symbol": "EURUSD",  # not seeded for this broker
            "side": "BUY",
            "volume": "0.01",
            "price": "1.10",
            "commission": "0",
            "swap": "0",
            "profit": "0",
            "executed_at": "2026-09-07T09:14:22.318000+00:00",
            "deal_type": "ENTRY",
        },
    )

    await route_unsolicited_event(
        envelope,
        agent_id=creds.agent_id,
        account_id=refs.account_id,
        agent_repo=agent_repo,
        account_repo=AccountRepository(db_session),
        event_consumer=_event_consumer(db_session),
    )  # must not raise

    assert (await db_session.execute(select(PositionRow))).scalars().all() == []
