"""SPEC-06 §5 steps 10-17 acceptance for `ExecutionWorker`: a queued
`stream:intents:pending` entry turns into a real trade_intent + outbox
row via the same `submit_decision` the manual `/agent/test-trade`
endpoint uses - risk-approved here reaches `SENT` with a real outbox
entry; risk-blocked reaches `RISK_BLOCKED` with none."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.streams import STREAM_INTENTS_PENDING, ensure_consumer_group, xadd
from app.domain.market.enums import Direction, Regime
from app.domain.strategy.decision import Decision, DecisionOutcome
from app.models.tables import Account
from app.repositories.signals import SignalRepository
from app.services.execution_worker import CONSUMER_GROUP, ExecutionWorker
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


def _decision(*, strategy_version_id: UUID) -> Decision:
    return Decision(
        outcome=DecisionOutcome.TRADE,
        symbol="XAUUSD",
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        strategy_version_id=strategy_version_id,
        regime=Regime.EXPANSION,
        setup=None,
        direction=Direction.LONG,
        entry=Decimal("100.00"),
        stop_loss=Decimal("99.50"),
        take_profits=(),
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="test",
        engine_duration_ms=0,
    )


async def _seed_signal(db_session: AsyncSession, *, trading_enabled: bool):
    refs = await seed_minimal_refs(db_session)
    account = await db_session.get(Account, refs.account_id)
    assert account is not None
    account.trading_enabled = trading_enabled
    account.kill_switch_active = not trading_enabled
    await db_session.commit()

    decision = _decision(strategy_version_id=refs.strategy_version_id)
    signal_id = await SignalRepository(db_session).create(
        decision,
        reference=f"ref-{refs.account_id}",
        analysis_run_id=refs.analysis_run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        created_at=datetime.now(UTC),
    )
    await db_session.commit()
    return refs, signal_id


def _worker(*, redis: Redis, db_engine: AsyncEngine, consumer: str = "test-consumer"):
    return ExecutionWorker(
        redis=redis,
        session_factory=async_sessionmaker(db_engine, expire_on_commit=False),
        consumer_name=consumer,
    )


async def _xpending_count(redis: Redis) -> int:
    info = await redis.xpending(STREAM_INTENTS_PENDING, CONSUMER_GROUP)
    return int(info["pending"])


async def test_risk_approved_signal_reaches_sent_with_an_outbox_row(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    refs, signal_id = await _seed_signal(db_session, trading_enabled=True)

    await ensure_consumer_group(redis_client, STREAM_INTENTS_PENDING, CONSUMER_GROUP)
    await xadd(
        redis_client,
        STREAM_INTENTS_PENDING,
        {
            "signal_id": str(signal_id),
            "account_id": str(refs.account_id),
            "instrument_id": str(refs.instrument_id),
            "strategy_version_id": str(refs.strategy_version_id),
            "environment": "demo",
        },
    )

    worker = _worker(redis=redis_client, db_engine=db_engine)
    processed = await worker.run_once(block_ms=100)

    assert processed == 1
    assert await _xpending_count(redis_client) == 0

    rows = await db_session.execute(
        text("SELECT state FROM trade_intents WHERE signal_id = :s"), {"s": str(signal_id)}
    )
    states = [r[0] for r in rows]
    assert states == ["SENT"]

    outbox_rows = await db_session.execute(text("SELECT count(*) FROM outbox"))
    assert outbox_rows.scalar_one() == 1


async def test_risk_blocked_signal_reaches_risk_blocked_with_no_outbox_row(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    refs, signal_id = await _seed_signal(db_session, trading_enabled=False)

    await ensure_consumer_group(redis_client, STREAM_INTENTS_PENDING, CONSUMER_GROUP)
    await xadd(
        redis_client,
        STREAM_INTENTS_PENDING,
        {
            "signal_id": str(signal_id),
            "account_id": str(refs.account_id),
            "instrument_id": str(refs.instrument_id),
            "strategy_version_id": str(refs.strategy_version_id),
            "environment": "demo",
        },
    )

    worker = _worker(redis=redis_client, db_engine=db_engine)
    processed = await worker.run_once(block_ms=100)

    assert processed == 1
    assert await _xpending_count(redis_client) == 0

    rows = await db_session.execute(
        text("SELECT state FROM trade_intents WHERE signal_id = :s"), {"s": str(signal_id)}
    )
    states = [r[0] for r in rows]
    assert states == ["RISK_BLOCKED"]

    outbox_rows = await db_session.execute(text("SELECT count(*) FROM outbox"))
    assert outbox_rows.scalar_one() == 0


async def test_unknown_signal_is_a_noop_but_still_acked(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    await ensure_consumer_group(redis_client, STREAM_INTENTS_PENDING, CONSUMER_GROUP)
    await xadd(
        redis_client,
        STREAM_INTENTS_PENDING,
        {
            "signal_id": "00000000-0000-0000-0000-000000000001",
            "account_id": str(refs.account_id),
            "instrument_id": str(refs.instrument_id),
            "strategy_version_id": str(refs.strategy_version_id),
            "environment": "demo",
        },
    )

    worker = _worker(redis=redis_client, db_engine=db_engine)
    processed = await worker.run_once(block_ms=100)

    assert processed == 1
    assert await _xpending_count(redis_client) == 0
