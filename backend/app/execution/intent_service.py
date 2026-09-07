"""SPEC-06 §5 steps 10-17: signal to a `SENT` (or `RISK_BLOCKED`) intent.

Step 12's whole point is here: every value this module feeds the risk
engine is reloaded from the database at call time
(`AccountRepository.load_account_state` / `compute_risk_state`), never
taken from the `Decision` or any cached message. A decision computed
seconds ago cannot know whether the daily loss limit was hit a moment
later - trusting it here is exactly how a system executes a trade it
should not.

Magic number deviation from `SPEC-06` §7 (documented in
`docs/adr/0001-mvp-scope.md`): the literal encoding needs a
`strategy_version_seq`/`instrument_seq` assignment table this MVP doesn't
build. `_magic` hashes the two ids instead - still one stable magic per
(strategy version, instrument, environment) triple for the reconciler to
match on, just not a small human-readable integer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.domain.execution.enums import ExecutionState, OrderSide, OrderType
from app.domain.execution.intent import OrderIntent
from app.domain.market.enums import Direction
from app.domain.strategy.decision import Decision
from app.domain.strategy.enums import DecisionOutcome
from app.engines.risk.engine import evaluate as evaluate_risk
from app.repositories.accounts import AccountRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.trade_intents import TradeIntentRepository

_DEFAULT_MAX_SLIPPAGE_POINTS = 30
_APPROVAL_CHAIN = (
    ExecutionState.VALIDATING,
    ExecutionState.APPROVED,
    ExecutionState.QUEUED,
    ExecutionState.SENT,
)
_APPROVAL_CHAIN_REASONS = {
    ExecutionState.VALIDATING: "risk evaluation started",
    ExecutionState.APPROVED: "risk engine approved",
    ExecutionState.QUEUED: "queued for dispatch",
    ExecutionState.SENT: "outbox command enqueued",
}


@dataclass(frozen=True, slots=True)
class SubmitResult:
    trade_intent_id: UUID
    state: ExecutionState
    client_order_id: str | None
    outbox_id: int | None


def _magic(strategy_version_id: UUID, instrument_id: UUID, *, environment: str) -> int:
    environment_code = 1 if environment == "demo" else 2
    digest = hashlib.blake2b(
        f"{strategy_version_id}:{instrument_id}".encode(), digest_size=8
    ).digest()
    raw = int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFC  # clear the low 2 bits
    return raw | environment_code


def _side_for(direction: Direction) -> OrderSide:
    return OrderSide.BUY if direction == Direction.LONG else OrderSide.SELL


def _risk_state_snapshot(risk_state_as_of: datetime, **fields: object) -> dict[str, object]:
    return {"as_of": risk_state_as_of.isoformat(), **{k: str(v) for k, v in fields.items()}}


async def submit_decision(
    decision: Decision,
    *,
    account_repo: AccountRepository,
    outbox_repo: OutboxRepository,
    intent_repo: TradeIntentRepository,
    signal_id: UUID,
    account_id: UUID,
    instrument_id: UUID,
    strategy_version_id: UUID,
    environment: str,
    conversion_rate: Decimal = Decimal(1),
    leverage: int = 500,
    margin_safety_factor: Decimal = Decimal("0.30"),
    as_of: datetime,
) -> SubmitResult:
    if decision.outcome != DecisionOutcome.TRADE:
        raise ValueError("submit_decision requires a TRADE decision")
    assert decision.direction is not None
    assert decision.entry is not None
    assert decision.stop_loss is not None

    account_state = await account_repo.load_account_state(account_id, as_of=as_of)
    spec = await account_repo.load_symbol_spec(instrument_id)
    risk_profile_id, risk_limits = await account_repo.load_active_risk_limits(account_id)
    risk_state = await account_repo.compute_risk_state(account_id, as_of=as_of)

    risk_decision = evaluate_risk(
        risk_state=risk_state,
        risk_limits=risk_limits,
        account_equity=account_state.equity,
        account_free_margin=account_state.free_margin,
        # Documented simplification (see ADR): no day/week-start equity
        # snapshot table exists yet, so the daily/weekly loss gates compare
        # against current equity rather than the value at period start.
        equity_at_day_start=account_state.equity,
        equity_at_week_start=account_state.equity,
        entry=decision.entry,
        stop_loss=decision.stop_loss,
        spec=spec,
        conversion_rate=conversion_rate,
        leverage=leverage,
        margin_safety_factor=margin_safety_factor,
    )

    gates_detail: dict[str, object] = {
        "gates": [
            {"code": g.code.value, "passed": g.passed, "detail": g.detail}
            for g in risk_decision.gates
        ]
    }
    risk_state_snapshot = _risk_state_snapshot(
        risk_state.as_of,
        realised_pnl_today=risk_state.realised_pnl_today,
        realised_pnl_week=risk_state.realised_pnl_week,
        open_risk=risk_state.open_risk,
        trades_today=risk_state.trades_today,
        open_position_count=risk_state.open_position_count,
        consecutive_losses=risk_state.consecutive_losses,
        peak_equity=risk_state.peak_equity,
        current_drawdown_pct=risk_state.current_drawdown_pct,
    )

    placeholder_intent = OrderIntent(
        client_order_id="",
        signal_id=signal_id,
        account_id=account_id,
        symbol=spec.symbol,
        side=_side_for(decision.direction),
        order_type=OrderType.MARKET,
        # `trade_intents.volume` has a `volume_positive` CHECK constraint;
        # a blocked intent was never sized (RiskDecision.size is None
        # whenever any gate fails, even if sizing itself would have
        # succeeded), so this is a nominal placeholder, not a real size -
        # the order never reaches the broker regardless of its value.
        volume=spec.volume_min,
        limit_price=decision.entry,
        stop_loss=decision.stop_loss,
        take_profit=decision.take_profits[0].level if decision.take_profits else None,
        max_slippage_points=_DEFAULT_MAX_SLIPPAGE_POINTS,
        magic=_magic(strategy_version_id, instrument_id, environment=environment),
        comment="",
        expires_at=None,
        idempotency_key="",
    )

    if not risk_decision.approved or risk_decision.size is None:
        trade_intent_id = await intent_repo.create(
            placeholder_intent,
            id_=None,
            instrument_id=instrument_id,
            risk_profile_id=risk_profile_id,
            risk_amount=Decimal(0),
            risk_pct_actual=Decimal(0),
            sizing_calculation={},
            risk_state_snapshot=risk_state_snapshot,
            created_at=as_of,
        )
        await intent_repo.transition(
            trade_intent_id,
            ExecutionState.RISK_BLOCKED,
            reason="risk engine did not approve",
            actor="worker",
            occurred_at=as_of,
            detail=gates_detail,
        )
        return SubmitResult(
            trade_intent_id=trade_intent_id,
            state=ExecutionState.RISK_BLOCKED,
            client_order_id=None,
            outbox_id=None,
        )

    client_order_id = uuid4().hex.upper()
    intent = OrderIntent(
        client_order_id=client_order_id,
        signal_id=signal_id,
        account_id=account_id,
        symbol=spec.symbol,
        side=_side_for(decision.direction),
        order_type=OrderType.MARKET,
        volume=risk_decision.size.volume,
        limit_price=decision.entry,
        stop_loss=decision.stop_loss,
        take_profit=decision.take_profits[0].level if decision.take_profits else None,
        max_slippage_points=_DEFAULT_MAX_SLIPPAGE_POINTS,
        magic=placeholder_intent.magic,
        comment=client_order_id[:24],
        expires_at=None,
        idempotency_key=client_order_id,
    )

    trade_intent_id = await intent_repo.create(
        intent,
        id_=None,
        instrument_id=instrument_id,
        risk_profile_id=risk_profile_id,
        risk_amount=risk_decision.size.risk_amount,
        risk_pct_actual=risk_decision.size.risk_pct_actual,
        sizing_calculation=dict(risk_decision.size.calculation),
        risk_state_snapshot=risk_state_snapshot,
        created_at=as_of,
    )
    for target in _APPROVAL_CHAIN:
        await intent_repo.transition(
            trade_intent_id,
            target,
            reason=_APPROVAL_CHAIN_REASONS[target],
            actor="worker",
            occurred_at=as_of,
        )

    payload = {
        "client_order_id": intent.client_order_id,
        "signal_id": str(intent.signal_id),
        "account_id": str(intent.account_id),
        "symbol": intent.symbol,
        "side": intent.side.value,
        "order_type": intent.order_type.value,
        "volume": str(intent.volume),
        "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
        "stop_loss": str(intent.stop_loss),
        "take_profit": str(intent.take_profit) if intent.take_profit is not None else None,
        "max_slippage_points": intent.max_slippage_points,
        "magic": intent.magic,
        "comment": intent.comment,
        "trade_intent_id": str(trade_intent_id),
    }
    outbox_id = await outbox_repo.enqueue(
        aggregate_type="trade_intent",
        aggregate_id=trade_intent_id,
        command_type="place_order",
        idempotency_key=client_order_id,
        payload=payload,
        available_at=as_of,
        created_at=as_of,
    )

    return SubmitResult(
        trade_intent_id=trade_intent_id,
        state=ExecutionState.SENT,
        client_order_id=client_order_id,
        outbox_id=outbox_id,
    )
