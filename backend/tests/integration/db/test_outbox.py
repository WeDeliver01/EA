"""SPEC-06 §5 / §10 acceptance: the outbox pattern's crash-recovery and
concurrency guarantees need a real Postgres transaction to mean anything -
`FOR UPDATE SKIP LOCKED` has no meaningful equivalent against a mock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.repositories.outbox import OutboxRepository
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


async def test_enqueue_is_idempotent_by_command_type_and_key(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = OutboxRepository(db_session)
    now = datetime.now(UTC)
    first_id = await repo.enqueue(
        aggregate_type="trade_intent",
        aggregate_id=refs.account_id,
        command_type="place_order",
        idempotency_key="CO-DUP-1",
        payload={"foo": "bar"},
        available_at=now,
        created_at=now,
    )
    second_id = await repo.enqueue(
        aggregate_type="trade_intent",
        aggregate_id=refs.account_id,
        command_type="place_order",
        idempotency_key="CO-DUP-1",
        payload={"foo": "bar-different-payload-ignored"},
        available_at=now,
        created_at=now,
    )
    await db_session.commit()

    assert first_id == second_id
    pending = await repo.claim_pending()
    assert len(pending) == 1


async def test_claim_pending_excludes_already_dispatched_rows(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = OutboxRepository(db_session)
    now = datetime.now(UTC)
    outbox_id = await repo.enqueue(
        aggregate_type="trade_intent",
        aggregate_id=refs.account_id,
        command_type="place_order",
        idempotency_key="CO-DISPATCH-1",
        payload={},
        available_at=now,
        created_at=now,
    )
    await db_session.commit()

    pending_before = await repo.claim_pending()
    assert len(pending_before) == 1

    await repo.mark_dispatched(outbox_id, dispatched_at=datetime.now(UTC))
    await db_session.commit()

    pending_after = await repo.claim_pending()
    assert pending_after == ()


async def test_record_failure_leaves_the_row_pending_for_retry(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = OutboxRepository(db_session)
    now = datetime.now(UTC)
    outbox_id = await repo.enqueue(
        aggregate_type="trade_intent",
        aggregate_id=refs.account_id,
        command_type="place_order",
        idempotency_key="CO-FAIL-1",
        payload={},
        available_at=now,
        created_at=now,
    )
    await db_session.commit()

    await repo.record_failure(outbox_id, error="connection reset")
    await db_session.commit()

    pending = await repo.claim_pending()
    assert len(pending) == 1
    assert pending[0].attempts == 1


async def test_skip_locked_gives_two_concurrent_dispatchers_disjoint_rows(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    """Simulates 'two workers consume the same intent' (SPEC-06 §10):
    two separate sessions, each holding a row lock, must never see the
    other's claimed row."""
    refs = await seed_minimal_refs(db_session)
    now = datetime.now(UTC)
    repo = OutboxRepository(db_session)
    for i in range(2):
        await repo.enqueue(
            aggregate_type="trade_intent",
            aggregate_id=refs.account_id,
            command_type="place_order",
            idempotency_key=f"CO-RACE-{i}",
            payload={},
            available_at=now,
            created_at=now,
        )
    await db_session.commit()

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session_a, session_factory() as session_b:
        repo_a = OutboxRepository(session_a)
        repo_b = OutboxRepository(session_b)

        claimed_a = await repo_a.claim_pending(limit=1)
        claimed_b = await repo_b.claim_pending(limit=1)

        assert len(claimed_a) == 1
        assert len(claimed_b) == 1
        assert claimed_a[0].id != claimed_b[0].id

        await session_a.commit()
        await session_b.commit()
