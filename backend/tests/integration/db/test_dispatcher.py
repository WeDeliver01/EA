"""SPEC-06 §5 steps 18-22 / §10 acceptance for the outbox dispatcher."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import ExecutionState
from app.domain.market.enums import AssetClass, Direction, Regime
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.execution.broker import BrokerFault, SimulatedBroker
from app.execution.dispatcher import OutboxDispatcher
from app.execution.intent_service import SubmitResult, submit_decision
from app.models.tables import Account, PositionRow, Trade
from app.repositories.accounts import AccountRepository
from app.repositories.outbox import OutboxRepository
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
        entry=Decimal("3418.20"),
        stop_loss=Decimal("3412.55"),
        take_profits=(
            TakeProfit(
                level=Decimal("3428.10"), fraction=Decimal("1.0"), r_multiple=Decimal("1.75")
            ),
        ),
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="trade",
        engine_duration_ms=0,
    )


async def _submit(db_session: AsyncSession, refs: SeededRefs, *, reference: str) -> SubmitResult:
    signal_id = await seed_signal(db_session, refs, reference=reference)
    account_repo = AccountRepository(db_session)
    outbox_repo = OutboxRepository(db_session)
    intent_repo = TradeIntentRepository(db_session)
    now = datetime.now(UTC)
    result = await submit_decision(
        _trade_decision(symbol="XAUUSD", as_of=now),
        account_repo=account_repo,
        outbox_repo=outbox_repo,
        intent_repo=intent_repo,
        signal_id=signal_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        environment="demo",
        as_of=now,
    )
    await db_session.commit()
    return result


async def test_dispatch_fills_and_marks_the_outbox_row_dispatched(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()
    result = await _submit(db_session, refs, reference="SIG-DISP-1")

    broker = SimulatedBroker(spec=_SPEC)
    dispatcher = OutboxDispatcher(
        broker=broker,
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    outcomes = await dispatcher.dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.sent is True
    assert outcome.order_result is not None
    assert outcome.order_result.retcode == 10009
    assert len(broker.get_positions()) == 1

    # Re-claiming finds nothing: the row is marked dispatched.
    pending = await OutboxRepository(db_session).claim_pending()
    assert pending == ()
    # Dispatch itself does not advance the intent past SENT - that is the
    # event consumer's job.
    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.SENT
    )


async def test_kill_switch_cancels_at_the_guard_without_calling_the_broker(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()
    result = await _submit(db_session, refs, reference="SIG-DISP-2")

    # Flip the kill switch after submission but before dispatch - exactly
    # the race step 12/19 exist to prevent.
    account = await db_session.get(Account, refs.account_id)
    assert account is not None
    account.kill_switch_active = True
    await db_session.commit()

    broker = SimulatedBroker(spec=_SPEC)
    dispatcher = OutboxDispatcher(
        broker=broker,
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    outcomes = await dispatcher.dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    assert len(outcomes) == 1
    assert outcomes[0].sent is False
    assert outcomes[0].cancelled_reason == "KILL_SWITCH_ACTIVE"
    assert broker.get_positions() == ()
    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.CANCELLED
    )


async def test_daily_loss_limit_hit_mid_dispatch_cancels_with_no_order_sent(
    db_session: AsyncSession,
) -> None:
    """SPEC-06 §10 row 12."""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()
    result = await _submit(db_session, refs, reference="SIG-DISP-3")

    # A large loss lands between submission and dispatch - equity is
    # 10000, max_daily_loss_pct is 0.03 (3%), so -350 breaches -300.
    # `realised_pnl_today` (the risk gate's input) comes from `trades`, a
    # projection of closed positions - not from the account balance alone -
    # so a real `Trade` row is needed, not just a balance mutation.
    now = datetime.now(UTC)
    position = PositionRow(
        id=uuid.uuid4(),
        broker_position_id="9001",
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        direction="LONG",
        status="CLOSED",
        volume=Decimal("0"),
        initial_volume=Decimal("1.0"),
        entry_price=Decimal("3400"),
        realised_pnl=Decimal("-350"),
        opened_at=now,
        closed_at=now,
        updated_at=now,
    )
    db_session.add(position)
    await db_session.flush()
    db_session.add(
        Trade(
            id=uuid.uuid4(),
            account_id=refs.account_id,
            instrument_id=refs.instrument_id,
            position_id=position.id,
            direction="LONG",
            entry_time=now,
            exit_time=now,
            holding_seconds=60,
            entry_price=Decimal("3400"),
            exit_price=Decimal("3350"),
            volume=Decimal("1.0"),
            gross_pnl=Decimal("-350"),
            commission=Decimal("0"),
            swap=Decimal("0"),
            net_pnl=Decimal("-350"),
            risk_amount=Decimal("100"),
            r_multiple=Decimal("-3.5"),
            exit_reason="STOP",
            created_at=now,
        )
    )
    await AccountRepository(db_session).apply_realised_pnl(
        refs.account_id, net_pnl=Decimal("-350"), at=now
    )
    await db_session.commit()

    broker = SimulatedBroker(spec=_SPEC)
    dispatcher = OutboxDispatcher(
        broker=broker,
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    outcomes = await dispatcher.dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    assert outcomes[0].sent is False
    assert outcomes[0].cancelled_reason == "DAILY_LOSS_LIMIT"
    assert broker.get_positions() == ()
    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.CANCELLED
    )


async def test_silent_fault_transitions_the_intent_to_unknown(db_session: AsyncSession) -> None:
    """SPEC-06 §10 row 1: the agent drops the connection immediately after
    receiving `place_order`."""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()
    result = await _submit(db_session, refs, reference="SIG-DISP-4")
    assert result.client_order_id is not None

    broker = SimulatedBroker(spec=_SPEC)
    broker.inject_fault(result.client_order_id, BrokerFault.SILENT)
    dispatcher = OutboxDispatcher(
        broker=broker,
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    outcomes = await dispatcher.dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    assert outcomes[0].sent is True
    assert outcomes[0].order_result is None
    # The trade genuinely happened at the broker...
    assert len(broker.get_positions()) == 1
    # ...but the intent has no way to know that yet.
    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.UNKNOWN
    )
