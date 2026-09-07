"""SPEC-04 §6: agent-to-server delivery is at-least-once; `agent_events`,
unique on `(agent_id, event_id)`, is the dedup boundary. "Agent returns
`order_result` twice with the same `event_id`" (SPEC-06 §10 row 2) must
produce exactly one deal and one position - this table is what makes that
true regardless of what calls it twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import AgentEvent


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    is_new: bool
    agent_event_id: int | None


class AgentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_if_new(
        self,
        *,
        agent_id: UUID,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> RecordOutcome:
        existing = await self._session.execute(
            select(AgentEvent.id).where(
                AgentEvent.agent_id == agent_id, AgentEvent.event_id == event_id
            )
        )
        if existing.scalar_one_or_none() is not None:
            return RecordOutcome(is_new=False, agent_event_id=None)

        row = AgentEvent(
            agent_id=agent_id,
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            received_at=received_at,
        )
        self._session.add(row)
        await self._session.flush()
        return RecordOutcome(is_new=True, agent_event_id=row.id)

    async def mark_processed(self, agent_event_id: int, *, processed_at: datetime) -> None:
        row = await self._session.get(AgentEvent, agent_event_id)
        if row is None:
            raise LookupError(f"agent_event {agent_event_id} not found")
        row.processed_at = processed_at
        await self._session.flush()

    async def mark_error(self, agent_event_id: int, *, error: str) -> None:
        row = await self._session.get(AgentEvent, agent_event_id)
        if row is None:
            raise LookupError(f"agent_event {agent_event_id} not found")
        row.process_error = error
        await self._session.flush()


_SIMULATED_AGENT_ID = UUID("00000000-0000-0000-0000-000000000001")


def simulated_agent_id() -> UUID:
    """Stable id for the one in-process fake agent this MVP has - real
    multi-agent identity is Phase 5 (see docs/adr/0001-mvp-scope.md)."""
    return _SIMULATED_AGENT_ID
