"""SPEC-06 §5 steps 18-22: the outbox dispatcher.

Deliberately stops at "send the command and record what came back." Turning
a successful `OrderResult` into a deal, a position and a `FILLED` transition
is `app.execution.event_consumer`'s job (SPEC-06 §5 steps 23-25) - the spec
runs these as two separate workers reacting to two different things (an
outbox row versus an agent event), and keeping them as two separate,
independently testable functions here preserves that even though nothing
routes between them but a direct call in this MVP (no real message bus - see
docs/adr/0001-mvp-scope.md).

Execution guard scope: SPEC-06 §5 step 19 lists six fast re-checks. Four are
implemented here because they're derivable from persisted state alone -
`KILL_SWITCH_ACTIVE`, `TRADING_DISABLED`, `DAILY_LOSS_LIMIT`,
`RECONCILIATION_UNRESOLVED`. `AGENT_DISCONNECTED`, `BROKER_DISCONNECTED` and
price/spread staleness need a live agent heartbeat and quote stream that
don't exist without Phase 5 - a documented gap, not a silent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.execution.enums import ExecutionState, OrderSide, OrderType
from app.domain.execution.intent import OrderIntent
from app.execution.broker import AsyncBroker, OrderResult
from app.repositories.accounts import AccountRepository
from app.repositories.outbox import OutboxRepository, OutboxRow
from app.repositories.reconciliation import ReconciliationRepository
from app.repositories.trade_intents import TradeIntentRepository


@dataclass(frozen=True, slots=True)
class GuardResult:
    passed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    outbox_id: int
    trade_intent_id: UUID
    sent: bool  # False only when the execution guard cancelled it
    cancelled_reason: str | None
    order_result: OrderResult | None  # None on cancellation, or on a silent/dropped fill


def _intent_from_payload(payload: dict[str, Any]) -> OrderIntent:
    client_order_id = str(payload["client_order_id"])
    limit_price = payload.get("limit_price")
    take_profit = payload.get("take_profit")
    return OrderIntent(
        client_order_id=client_order_id,
        signal_id=UUID(str(payload["signal_id"])),
        account_id=UUID(str(payload["account_id"])),
        symbol=str(payload["symbol"]),
        side=OrderSide(payload["side"]),
        order_type=OrderType(payload["order_type"]),
        volume=Decimal(str(payload["volume"])),
        limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
        stop_loss=Decimal(str(payload["stop_loss"])),
        take_profit=Decimal(str(take_profit)) if take_profit is not None else None,
        max_slippage_points=int(payload["max_slippage_points"]),
        magic=int(payload["magic"]),
        comment=str(payload["comment"]),
        expires_at=None,
        idempotency_key=client_order_id,
    )


class OutboxDispatcher:
    def __init__(
        self,
        *,
        broker: AsyncBroker,
        account_repo: AccountRepository,
        outbox_repo: OutboxRepository,
        intent_repo: TradeIntentRepository,
        reconciliation_repo: ReconciliationRepository,
    ) -> None:
        self._broker = broker
        self._account_repo = account_repo
        self._outbox_repo = outbox_repo
        self._intent_repo = intent_repo
        self._reconciliation_repo = reconciliation_repo

    async def execution_guard(self, account_id: UUID, *, as_of: datetime) -> GuardResult:
        risk_state = await self._account_repo.compute_risk_state(account_id, as_of=as_of)
        if risk_state.kill_switch_active:
            return GuardResult(False, "KILL_SWITCH_ACTIVE")
        if not risk_state.trading_enabled:
            return GuardResult(False, "TRADING_DISABLED")

        account_state = await self._account_repo.load_account_state(account_id, as_of=as_of)
        _, risk_limits = await self._account_repo.load_active_risk_limits(account_id)
        # Documented simplification (see ADR, same as intent_service): no
        # day-start equity snapshot table, so this compares against current
        # equity rather than equity at the daily rollover.
        daily_loss_floor = -(account_state.equity * risk_limits.max_daily_loss_pct)
        if risk_state.realised_pnl_today <= daily_loss_floor:
            return GuardResult(False, "DAILY_LOSS_LIMIT")

        if await self._reconciliation_repo.has_unresolved_critical(account_id):
            return GuardResult(False, "RECONCILIATION_UNRESOLVED")

        return GuardResult(True, None)

    async def dispatch_pending(
        self, *, account_id: UUID, as_of: datetime, limit: int = 20
    ) -> list[DispatchOutcome]:
        rows = await self._outbox_repo.claim_pending(limit=limit)
        outcomes: list[DispatchOutcome] = []
        for row in rows:
            if row.command_type != "place_order":
                continue  # modify/close dispatch belongs to position_manager
            outcomes.append(
                await self._dispatch_place_order(row, account_id=account_id, as_of=as_of)
            )
        return outcomes

    async def _dispatch_place_order(
        self, row: OutboxRow, *, account_id: UUID, as_of: datetime
    ) -> DispatchOutcome:
        trade_intent_id = UUID(str(row.payload["trade_intent_id"]))

        guard = await self.execution_guard(account_id, as_of=as_of)
        if not guard.passed:
            await self._outbox_repo.mark_dispatched(row.id, dispatched_at=as_of)
            await self._intent_repo.transition(
                trade_intent_id,
                ExecutionState.CANCELLED,
                reason=f"execution guard: {guard.reason}",
                actor="worker",
                occurred_at=as_of,
            )
            return DispatchOutcome(
                outbox_id=row.id,
                trade_intent_id=trade_intent_id,
                sent=False,
                cancelled_reason=guard.reason,
                order_result=None,
            )

        intent = _intent_from_payload(row.payload)
        result = await self._broker.place_order(intent, at=as_of)
        await self._outbox_repo.mark_dispatched(row.id, dispatched_at=as_of)

        if result is None:
            # SPEC-06 §10 row 1: the agent drops the connection right after
            # accepting the order. The trade happened; we just don't know
            # it yet. Only reconciliation can resolve an UNKNOWN intent.
            await self._intent_repo.transition(
                trade_intent_id,
                ExecutionState.UNKNOWN,
                reason="no order result received",
                actor="worker",
                occurred_at=as_of,
            )
            return DispatchOutcome(
                outbox_id=row.id,
                trade_intent_id=trade_intent_id,
                sent=True,
                cancelled_reason=None,
                order_result=None,
            )

        return DispatchOutcome(
            outbox_id=row.id,
            trade_intent_id=trade_intent_id,
            sent=True,
            cancelled_reason=None,
            order_result=result,
        )
