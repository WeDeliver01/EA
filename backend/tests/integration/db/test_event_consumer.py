"""SPEC-06 §5 steps 23-25 / §10 acceptance for the agent event consumer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import ExecutionState, OrderSide
from app.domain.market.enums import AssetClass, Direction, Regime
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.execution.broker import (
    RETCODE_DONE,
    AsyncSimulatedBrokerAdapter,
    BrokerFault,
    OrderResult,
    SimulatedBroker,
)
from app.execution.dispatcher import DispatchOutcome, OutboxDispatcher
from app.execution.event_consumer import EventConsumer
from app.execution.intent_service import SubmitResult, submit_decision
from app.models.tables import Account, AgentEvent, PositionRow
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository, simulated_agent_id
from app.repositories.deals import DealRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.positions import PositionRepository
from app.repositories.reconciliation import ReconciliationRepository
from app.repositories.trade_intents import TradeIntentRepository
from tests.integration.seed import SeededRefs, seed_minimal_refs, seed_signal

pytestmark = pytest.mark.integration

_SPEC = SymbolSpec(
    symbol="XAUUSD",
    asset_class=AssetClass.METAL,
    digits=2,
    point=Decimal("0.01"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("1.00"),
    contract_size=Decimal("100"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("50"),
    volume_step=Decimal("0.01"),
    stops_level_points=10,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)


async def _enable_trading(session: AsyncSession, account_id: uuid.UUID) -> None:
    account = await session.get(Account, account_id)
    assert account is not None
    account.trading_enabled = True
    account.kill_switch_active = False
    await session.flush()


def _trade_decision(*, symbol: str, as_of: datetime) -> Decision:
    return Decision(
        outcome=DecisionOutcome.TRADE,
        symbol=symbol,
        as_of=as_of,
        strategy_version_id=uuid.uuid4(),
        regime=Regime.TRENDING_UP,
        setup=None,
        direction=Direction.LONG,
        entry=Decimal("3400.00"),
        stop_loss=Decimal("3390.00"),
        take_profits=(
            TakeProfit(
                level=Decimal("3420.00"), fraction=Decimal("1.0"), r_multiple=Decimal("2.0")
            ),
        ),
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="trade",
        engine_duration_ms=0,
    )


async def _submit_and_dispatch(
    db_session: AsyncSession,
    refs: SeededRefs,
    *,
    reference: str,
    broker: SimulatedBroker,
) -> tuple[SubmitResult, DispatchOutcome]:
    signal_id = await seed_signal(db_session, refs, reference=reference)
    now = datetime.now(UTC)
    result = await submit_decision(
        _trade_decision(symbol="XAUUSD", as_of=now),
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        signal_id=signal_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        environment="demo",
        as_of=now,
    )
    await db_session.commit()

    dispatcher = OutboxDispatcher(
        broker=AsyncSimulatedBrokerAdapter(broker),
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    outcomes = await dispatcher.dispatch_pending(account_id=refs.account_id, as_of=now)
    await db_session.commit()
    assert len(outcomes) == 1
    return result, outcomes[0]


def _consumer(db_session: AsyncSession) -> EventConsumer:
    return EventConsumer(
        agent_event_repo=AgentEventRepository(db_session),
        deal_repo=DealRepository(db_session),
        position_repo=PositionRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
    )


async def test_order_result_opens_a_position_and_settles_the_intent(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    broker = SimulatedBroker(spec=_SPEC)
    result, outcome = await _submit_and_dispatch(
        db_session, refs, reference="SIG-EVT-1", broker=broker
    )
    assert outcome.order_result is not None

    consumer = _consumer(db_session)
    consume_result = await consumer.process_order_result(
        outcome.order_result,
        trade_intent_id=result.trade_intent_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        agent_id=simulated_agent_id(),
        event_id=f"order_result:{outcome.order_result.client_order_id}",
        received_at=datetime.now(UTC),
    )
    await db_session.commit()

    assert consume_result.is_new_event is True
    assert consume_result.new_state == ExecutionState.POSITION_OPEN
    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.POSITION_OPEN
    )

    positions = (
        (
            await db_session.execute(
                select(PositionRow).where(PositionRow.account_id == refs.account_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(positions) == 1
    assert positions[0].status == "OPEN"
    assert positions[0].volume > Decimal("0")


async def test_duplicate_event_id_produces_exactly_one_deal_and_position(
    db_session: AsyncSession,
) -> None:
    """SPEC-06 §10 row 2."""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    broker = SimulatedBroker(spec=_SPEC)
    result, outcome = await _submit_and_dispatch(
        db_session, refs, reference="SIG-EVT-2", broker=broker
    )
    assert outcome.order_result is not None
    event_id = f"order_result:{outcome.order_result.client_order_id}"

    consumer = _consumer(db_session)
    for _ in range(2):
        await consumer.process_order_result(
            outcome.order_result,
            trade_intent_id=result.trade_intent_id,
            account_id=refs.account_id,
            instrument_id=refs.instrument_id,
            symbol="XAUUSD",
            side=OrderSide.BUY,
            agent_id=simulated_agent_id(),
            event_id=event_id,
            received_at=datetime.now(UTC),
        )
        await db_session.commit()

    deals = await DealRepository(db_session).list_for_account(refs.account_id)
    assert len(deals) == 1
    positions = (
        (
            await db_session.execute(
                select(PositionRow).where(PositionRow.account_id == refs.account_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(positions) == 1


async def test_reject_transitions_the_intent_to_rejected(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    signal_id = await seed_signal(db_session, refs, reference="SIG-EVT-3")
    now = datetime.now(UTC)
    result = await submit_decision(
        _trade_decision(symbol="XAUUSD", as_of=now),
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        signal_id=signal_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        environment="demo",
        as_of=now,
    )
    await db_session.commit()
    assert result.client_order_id is not None

    broker = SimulatedBroker(spec=_SPEC)
    broker.inject_fault(
        result.client_order_id, BrokerFault.REJECT, retcode=10016, retcode_text="INVALID_STOPS"
    )
    dispatcher = OutboxDispatcher(
        broker=AsyncSimulatedBrokerAdapter(broker),
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    outcomes = await dispatcher.dispatch_pending(account_id=refs.account_id, as_of=now)
    await db_session.commit()
    assert outcomes[0].order_result is not None

    consumer = _consumer(db_session)
    await consumer.process_order_result(
        outcomes[0].order_result,
        trade_intent_id=result.trade_intent_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        agent_id=simulated_agent_id(),
        event_id=f"order_result:{result.client_order_id}",
        received_at=now,
    )
    await db_session.commit()

    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.REJECTED
    )


async def test_malformed_done_result_records_the_error_and_reraises(
    db_session: AsyncSession,
) -> None:
    """A `DONE` result with no `broker_deal_id` violates `_apply`'s own
    invariant (SPEC-04 §4 step 7: a done order always carries a deal
    ticket) - this is the agent sending something the protocol says can't
    happen. The failure must still be durably recorded, not swallowed."""
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    malformed = OrderResult(
        client_order_id="CO-MALFORMED",
        retcode=RETCODE_DONE,
        retcode_text="DONE",
        broker_order_id="1",
        broker_deal_id=None,
        broker_position_id="1",
        filled_volume=Decimal("0.01"),
        fill_price=Decimal("100"),
        requested_price=Decimal("100"),
        slippage_points=0,
        latency_ms=1,
    )

    consumer = _consumer(db_session)
    with pytest.raises(AssertionError):
        await consumer.process_order_result(
            malformed,
            trade_intent_id=uuid.uuid4(),
            account_id=refs.account_id,
            instrument_id=refs.instrument_id,
            symbol="XAUUSD",
            side=OrderSide.BUY,
            agent_id=simulated_agent_id(),
            event_id="order_result:CO-MALFORMED",
            received_at=datetime.now(UTC),
        )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AgentEvent).where(
                AgentEvent.agent_id == simulated_agent_id(),
                AgentEvent.event_id == "order_result:CO-MALFORMED",
            )
        )
    ).scalar_one()
    assert row.process_error is not None
    assert row.processed_at is None


async def test_set_initial_risk_is_a_no_op_when_the_position_cannot_be_found(
    db_session: AsyncSession,
) -> None:
    """Defensive path: `_apply` always calls this immediately after
    rebuilding the very position it looks up, so in practice it's always
    found - this pins the fallback behaviour directly in case that
    invariant ever breaks."""
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    consumer = _consumer(db_session)
    await consumer._set_initial_risk_from_intent(
        uuid.uuid4(),
        account_id=refs.account_id,
        broker_position_id="does-not-exist",
        at=datetime.now(UTC),
    )  # must not raise
