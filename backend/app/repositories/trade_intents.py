"""TradeIntent persistence, with the execution state machine enforced in
Python *and* backed by the DB's deferred constraint trigger (SPEC-01 §6,
SPEC-02 §9): every state change is written in the same transaction as its
`execution_transitions` row, or the trigger raises at commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import ExecutionState
from app.domain.execution.intent import OrderIntent
from app.domain.execution.state_machine import assert_transition
from app.models.tables import ExecutionTransition, TradeIntent


@dataclass(frozen=True, slots=True)
class ExecutionTransitionRecord:
    """A domain-shaped (non-ORM) view of one execution_transitions row."""

    from_state: ExecutionState | None
    to_state: ExecutionState
    reason: str
    actor: str
    occurred_at: datetime


class TradeIntentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        intent: OrderIntent,
        *,
        id_: UUID | None,
        instrument_id: UUID,
        risk_profile_id: UUID,
        risk_amount: Any,
        risk_pct_actual: Any,
        sizing_calculation: dict[str, Any],
        risk_state_snapshot: dict[str, Any],
        created_at: datetime,
        correlation_id: UUID | None = None,
    ) -> UUID:
        row_id = id_ or uuid4()
        row = TradeIntent(
            id=row_id,
            client_order_id=intent.client_order_id,
            signal_id=intent.signal_id,
            account_id=intent.account_id,
            instrument_id=instrument_id,
            risk_profile_id=risk_profile_id,
            state=ExecutionState.DETECTED.value,
            side=intent.side.value,
            order_type=intent.order_type.value,
            volume=intent.volume,
            limit_price=intent.limit_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            max_slippage_points=intent.max_slippage_points,
            magic=intent.magic,
            risk_amount=risk_amount,
            risk_pct_actual=risk_pct_actual,
            sizing_calculation=sizing_calculation,
            risk_state_snapshot=risk_state_snapshot,
            expires_at=intent.expires_at,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.add(
            ExecutionTransition(
                trade_intent_id=row_id,
                from_state=None,
                to_state=ExecutionState.DETECTED.value,
                reason="created",
                actor="worker",
                correlation_id=correlation_id,
                occurred_at=created_at,
            )
        )
        await self._session.flush()
        return row_id

    async def transition(
        self,
        trade_intent_id: UUID,
        target: ExecutionState,
        *,
        reason: str,
        actor: str,
        occurred_at: datetime,
        correlation_id: UUID | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        row = await self._session.get(TradeIntent, trade_intent_id)
        if row is None:
            raise LookupError(f"trade_intent {trade_intent_id} not found")

        current = ExecutionState(row.state)
        assert_transition(current, target)  # raises IllegalStateTransition, Python-side

        row.state = target.value
        if target in {
            ExecutionState.CLOSED,
            ExecutionState.RISK_BLOCKED,
            ExecutionState.REJECTED,
            ExecutionState.CANCELLED,
            ExecutionState.EXPIRED,
            ExecutionState.BROKER_ERROR,
        }:
            row.settled_at = occurred_at

        self._session.add(
            ExecutionTransition(
                trade_intent_id=trade_intent_id,
                from_state=current.value,
                to_state=target.value,
                reason=reason,
                actor=actor,
                correlation_id=correlation_id,
                detail=detail or {},
                occurred_at=occurred_at,
            )
        )
        await self._session.flush()

    async def get_state(self, trade_intent_id: UUID) -> ExecutionState:
        row = await self._session.get(TradeIntent, trade_intent_id)
        if row is None:
            raise LookupError(f"trade_intent {trade_intent_id} not found")
        return ExecutionState(row.state)

    async def list_transitions(
        self, trade_intent_id: UUID
    ) -> tuple[ExecutionTransitionRecord, ...]:
        stmt = (
            select(ExecutionTransition)
            .where(ExecutionTransition.trade_intent_id == trade_intent_id)
            .order_by(ExecutionTransition.occurred_at)
        )
        result = await self._session.execute(stmt)
        return tuple(
            ExecutionTransitionRecord(
                from_state=ExecutionState(r.from_state) if r.from_state else None,
                to_state=ExecutionState(r.to_state),
                reason=r.reason,
                actor=r.actor,
                occurred_at=r.occurred_at,
            )
            for r in result.scalars().all()
        )
