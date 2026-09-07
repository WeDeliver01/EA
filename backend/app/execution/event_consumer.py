"""SPEC-06 §5 steps 23-25: turning an `order_result` into a deal, a
position, and a settled intent.

Deals are the append-only financial source of truth (P5); positions and
trades are projections rebuilt from them (`app.repositories.projections`,
already built in Phase 1) - this module inserts the deal and then calls
that same rebuild, rather than upserting a position by hand, so there is
only ever one way a position's numbers get computed.

Simplification versus the full state machine (documented in
`docs/adr/0001-mvp-scope.md`): a fill - full or partial - goes straight
`SENT -> ACKNOWLEDGED -> FILLED -> POSITION_OPEN`. `PARTIALLY_FILLED` isn't
used: `SimulatedBroker.place_order` resolves an order in exactly one
response (there's no real broker sending a second partial fill later for
the same order), so "partially filled" here just means the position opened
smaller than requested, not that more fills are still coming.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.domain.execution.enums import DealType, ExecutionState, OrderSide
from app.domain.execution.intent import Fill
from app.execution.broker import RETCODE_DONE, OrderResult
from app.repositories.agent_events import AgentEventRepository
from app.repositories.deals import DealRepository
from app.repositories.positions import PositionRepository
from app.repositories.trade_intents import TradeIntentRepository

_APPROVAL_TO_FILL = (
    ExecutionState.ACKNOWLEDGED,
    ExecutionState.FILLED,
    ExecutionState.POSITION_OPEN,
)


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    is_new_event: bool
    trade_intent_id: UUID
    new_state: ExecutionState | None  # None when this event was a dedup no-op


class EventConsumer:
    def __init__(
        self,
        *,
        agent_event_repo: AgentEventRepository,
        deal_repo: DealRepository,
        position_repo: PositionRepository,
        intent_repo: TradeIntentRepository,
    ) -> None:
        self._agent_event_repo = agent_event_repo
        self._deal_repo = deal_repo
        self._position_repo = position_repo
        self._intent_repo = intent_repo

    async def process_order_result(
        self,
        result: OrderResult,
        *,
        trade_intent_id: UUID,
        account_id: UUID,
        instrument_id: UUID,
        symbol: str,
        side: OrderSide,
        agent_id: UUID,
        event_id: str,
        received_at: datetime,
    ) -> ConsumeResult:
        recorded = await self._agent_event_repo.record_if_new(
            agent_id=agent_id,
            event_id=event_id,
            event_type="order_result",
            payload={"client_order_id": result.client_order_id, "retcode": result.retcode},
            received_at=received_at,
        )
        if not recorded.is_new:
            return ConsumeResult(
                is_new_event=False, trade_intent_id=trade_intent_id, new_state=None
            )
        assert recorded.agent_event_id is not None

        try:
            new_state = await self._apply(
                result,
                trade_intent_id=trade_intent_id,
                account_id=account_id,
                instrument_id=instrument_id,
                symbol=symbol,
                side=side,
                at=received_at,
            )
        except Exception as exc:  # recorded, then re-raised for the caller
            await self._agent_event_repo.mark_error(recorded.agent_event_id, error=str(exc))
            raise
        await self._agent_event_repo.mark_processed(
            recorded.agent_event_id, processed_at=received_at
        )
        return ConsumeResult(
            is_new_event=True, trade_intent_id=trade_intent_id, new_state=new_state
        )

    async def _apply(
        self,
        result: OrderResult,
        *,
        trade_intent_id: UUID,
        account_id: UUID,
        instrument_id: UUID,
        symbol: str,
        side: OrderSide,
        at: datetime,
    ) -> ExecutionState:
        if result.retcode != RETCODE_DONE:
            await self._intent_repo.transition(
                trade_intent_id,
                ExecutionState.REJECTED,
                reason=result.retcode_text,
                actor="worker",
                occurred_at=at,
                detail={"retcode": result.retcode},
            )
            return ExecutionState.REJECTED

        assert result.broker_deal_id is not None
        assert result.broker_position_id is not None
        assert result.fill_price is not None

        fill = Fill(
            broker_deal_id=result.broker_deal_id,
            client_order_id=result.client_order_id,
            broker_order_id=result.broker_order_id or "",
            broker_position_id=result.broker_position_id,
            symbol=symbol,
            side=side,
            volume=result.filled_volume,
            price=result.fill_price,
            commission=Decimal(0),
            swap=Decimal(0),
            profit=Decimal(0),  # no realised P&L on open
            executed_at=at,
            deal_type=DealType.ENTRY,
        )
        await self._deal_repo.insert(
            fill,
            account_id=account_id,
            instrument_id=instrument_id,
            trade_intent_id=trade_intent_id,
        )

        for target in _APPROVAL_TO_FILL:
            await self._intent_repo.transition(
                trade_intent_id,
                target,
                reason="order filled"
                if target == ExecutionState.ACKNOWLEDGED
                else "position opened",
                actor="worker",
                occurred_at=at,
            )

        await self._rebuild_and_apply_positions(account_id)
        await self._set_initial_risk_from_intent(
            trade_intent_id,
            account_id=account_id,
            broker_position_id=result.broker_position_id,
            at=at,
        )
        return ExecutionState.POSITION_OPEN

    async def _set_initial_risk_from_intent(
        self, trade_intent_id: UUID, *, account_id: UUID, broker_position_id: str, at: datetime
    ) -> None:
        """`deals` carries no stop/take-profit/risk data, so the freshly
        (re)built position reads its risk parameters from the originating
        intent. Simplification: on a partial fill, `initial_risk` is the
        amount the intent was sized for, not rescaled to the actual filled
        volume - documented in docs/adr/0001-mvp-scope.md."""
        position_id = await self._position_repo.get_id_by_broker_position_id(
            account_id, broker_position_id
        )
        if position_id is None:
            return
        details = await self._intent_repo.get_order_details(trade_intent_id)
        await self._position_repo.set_initial_risk(
            position_id,
            stop_loss=details.stop_loss,
            take_profit=details.take_profit,
            initial_risk=details.risk_amount,
            updated_at=at,
        )

    async def record_deal(
        self,
        fill: Fill,
        *,
        account_id: UUID,
        instrument_id: UUID,
        trade_intent_id: UUID | None,
    ) -> None:
        """Inserts any deal - entry or exit, from `_apply` above, from the
        position manager's own close command, or an entry/exit discovered
        by reconciliation - and rebuilds the position/trade projections
        from it. One path for every deal, per P5: deals are the only
        source of truth."""
        await self._deal_repo.insert(
            fill,
            account_id=account_id,
            instrument_id=instrument_id,
            trade_intent_id=trade_intent_id,
        )
        await self._rebuild_and_apply_positions(account_id)

    async def _rebuild_and_apply_positions(self, account_id: UUID) -> None:
        positions, trades = await self._deal_repo.rebuild_projections(account_id)
        await self._position_repo.apply_projections(positions, trades)
