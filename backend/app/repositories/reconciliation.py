"""Persistence for reconciliation runs and the discrepancies they find
(SPEC-06 §6, SPEC-02 schema). The comparison/classification logic itself is
pure and lives in `app.execution.reconciliation` - this module only reads
and writes rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Discrepancy, ReconciliationRun


@dataclass(frozen=True, slots=True)
class DiscrepancyRow:
    id: UUID
    kind: str
    severity: str
    local_state: dict[str, Any] | None
    broker_state: dict[str, Any] | None
    resolution: str | None
    resolved_at: datetime | None


class ReconciliationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start_run(self, account_id: UUID, *, started_at: datetime) -> UUID:
        run_id = uuid4()
        self._session.add(
            ReconciliationRun(
                id=run_id,
                account_id=account_id,
                started_at=started_at,
                status="running",
            )
        )
        await self._session.flush()
        return run_id

    async def finish_run(
        self,
        run_id: UUID,
        *,
        finished_at: datetime,
        status: str,
        local_position_count: int,
        broker_position_count: int,
        discrepancy_count: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        row = await self._session.get(ReconciliationRun, run_id)
        if row is None:
            raise LookupError(f"reconciliation_run {run_id} not found")
        row.finished_at = finished_at
        row.status = status
        row.local_position_count = local_position_count
        row.broker_position_count = broker_position_count
        row.discrepancy_count = discrepancy_count
        row.detail = detail or {}
        await self._session.flush()

    async def record_discrepancy(
        self,
        *,
        reconciliation_run_id: UUID,
        account_id: UUID,
        kind: str,
        severity: str,
        local_state: dict[str, Any] | None,
        broker_state: dict[str, Any] | None,
        created_at: datetime,
        resolution: str | None = None,
        resolved_at: datetime | None = None,
        resolved_by: str | None = None,
    ) -> UUID:
        discrepancy_id = uuid4()
        self._session.add(
            Discrepancy(
                id=discrepancy_id,
                reconciliation_run_id=reconciliation_run_id,
                account_id=account_id,
                kind=kind,
                severity=severity,
                local_state=local_state,
                broker_state=broker_state,
                resolution=resolution,
                resolved_at=resolved_at,
                resolved_by=resolved_by,
                created_at=created_at,
            )
        )
        await self._session.flush()
        return discrepancy_id

    async def resolve(
        self, discrepancy_id: UUID, *, resolution: str, resolved_at: datetime, resolved_by: str
    ) -> None:
        row = await self._session.get(Discrepancy, discrepancy_id)
        if row is None:
            raise LookupError(f"discrepancy {discrepancy_id} not found")
        row.resolution = resolution
        row.resolved_at = resolved_at
        row.resolved_by = resolved_by
        await self._session.flush()

    async def has_unresolved_critical(self, account_id: UUID) -> bool:
        stmt = select(Discrepancy.id).where(
            Discrepancy.account_id == account_id,
            Discrepancy.severity == "critical",
            Discrepancy.resolved_at.is_(None),
        )
        result = await self._session.execute(stmt.limit(1))
        return result.scalar_one_or_none() is not None

    async def list_unresolved(self, account_id: UUID) -> tuple[DiscrepancyRow, ...]:
        stmt = select(Discrepancy).where(
            Discrepancy.account_id == account_id, Discrepancy.resolved_at.is_(None)
        )
        result = await self._session.execute(stmt)
        return tuple(
            DiscrepancyRow(
                id=r.id,
                kind=r.kind,
                severity=r.severity,
                local_state=r.local_state,
                broker_state=r.broker_state,
                resolution=r.resolution,
                resolved_at=r.resolved_at,
            )
            for r in result.scalars().all()
        )
