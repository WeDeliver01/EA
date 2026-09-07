"""SPEC-06 §5: the outbox pattern.

`insert trade_intent(state=SENT) + outbox row, one transaction, XACK after
commit` is the mechanism that makes execution idempotent (P4). A row here
survives a crash between commit and dispatch; `claim_pending` (`FOR UPDATE
SKIP LOCKED`) is what lets a restarted dispatcher find it and send exactly
once, and the unique `(command_type, idempotency_key)` constraint (already
in the schema, SPEC-02) is what makes an accidental double-enqueue of the
same command a no-op rather than a double order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Outbox


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    aggregate_type: str
    aggregate_id: UUID
    command_type: str
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        command_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        available_at: datetime,
        created_at: datetime,
    ) -> int:
        """Idempotent by `(command_type, idempotency_key)`: enqueuing the
        same command twice returns the existing row's id rather than
        inserting a second one."""
        existing = await self._session.execute(
            select(Outbox).where(
                Outbox.command_type == command_type,
                Outbox.idempotency_key == idempotency_key,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row.id

        row = Outbox(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            payload=payload,
            available_at=available_at,
            attempts=0,
            created_at=created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def claim_pending(self, *, limit: int = 20) -> tuple[OutboxRow, ...]:
        """`FOR UPDATE SKIP LOCKED`: two dispatchers racing on the same
        table each get disjoint rows, never the same one twice."""
        stmt = (
            select(Outbox)
            .where(Outbox.dispatched_at.is_(None))
            .order_by(Outbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return tuple(
            OutboxRow(
                id=r.id,
                aggregate_type=r.aggregate_type,
                aggregate_id=r.aggregate_id,
                command_type=r.command_type,
                idempotency_key=r.idempotency_key,
                payload=r.payload,
                attempts=r.attempts,
            )
            for r in result.scalars().all()
        )

    async def mark_dispatched(self, outbox_id: int, *, dispatched_at: datetime) -> None:
        row = await self._session.get(Outbox, outbox_id)
        if row is None:
            raise LookupError(f"outbox row {outbox_id} not found")
        row.dispatched_at = dispatched_at
        row.attempts += 1
        await self._session.flush()

    async def record_failure(self, outbox_id: int, *, error: str) -> None:
        """The row stays undispatched (`dispatched_at` is still `NULL`) so
        the next dispatcher pass retries it; only the attempt count and
        last error advance."""
        row = await self._session.get(Outbox, outbox_id)
        if row is None:
            raise LookupError(f"outbox row {outbox_id} not found")
        row.attempts += 1
        row.last_error = error
        await self._session.flush()

    async def get_by_idempotency_key(
        self, *, command_type: str, idempotency_key: str
    ) -> OutboxRow | None:
        result = await self._session.execute(
            select(Outbox).where(
                Outbox.command_type == command_type,
                Outbox.idempotency_key == idempotency_key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return OutboxRow(
            id=row.id,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            command_type=row.command_type,
            idempotency_key=row.idempotency_key,
            payload=row.payload,
            attempts=row.attempts,
        )
