"""SPEC-06 §5 steps 10-17 acceptance: a TRADE decision becomes either a
`SENT` intent with exactly one outbox row, or a `RISK_BLOCKED` intent with
none - and the risk evaluation always uses live, DB-reloaded state."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import ExecutionState
from app.domain.market.enums import Direction, Regime
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.execution.intent_service import submit_decision
from app.models.tables import Account
from app.repositories.accounts import AccountRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.trade_intents import TradeIntentRepository
from tests.integration.seed import seed_minimal_refs, seed_signal

pytestmark = pytest.mark.integration


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


async def test_approved_decision_produces_a_sent_intent_and_one_outbox_row(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    signal_id = await seed_signal(db_session, refs, reference="SIG-INTENT-1")
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

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

    assert result.state == ExecutionState.SENT
    assert result.client_order_id is not None
    assert result.outbox_id is not None
    assert await intent_repo.get_state(result.trade_intent_id) == ExecutionState.SENT

    transitions = await intent_repo.list_transitions(result.trade_intent_id)
    assert [t.to_state for t in transitions] == [
        ExecutionState.DETECTED,
        ExecutionState.VALIDATING,
        ExecutionState.APPROVED,
        ExecutionState.QUEUED,
        ExecutionState.SENT,
    ]

    pending = await outbox_repo.claim_pending()
    assert len(pending) == 1
    assert pending[0].command_type == "place_order"
    assert pending[0].idempotency_key == result.client_order_id


async def test_trading_disabled_produces_a_risk_blocked_intent_and_no_outbox_row(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    signal_id = await seed_signal(db_session, refs, reference="SIG-INTENT-2")
    # trading_enabled defaults to False - deliberately left unset.
    await db_session.commit()

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

    assert result.state == ExecutionState.RISK_BLOCKED
    assert result.client_order_id is None
    assert result.outbox_id is None
    assert await intent_repo.get_state(result.trade_intent_id) == ExecutionState.RISK_BLOCKED
    assert await outbox_repo.claim_pending() == ()


async def test_wait_decision_is_rejected(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    signal_id = await seed_signal(db_session, refs, reference="SIG-INTENT-WAIT")
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    now = datetime.now(UTC)
    wait_decision = dataclasses.replace(
        _trade_decision(symbol="XAUUSD", as_of=now), outcome=DecisionOutcome.WAIT
    )

    with pytest.raises(ValueError, match="requires a TRADE decision"):
        await submit_decision(
            wait_decision,
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


async def test_magic_is_stable_across_calls_for_the_same_triple(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    signal_id_1 = await seed_signal(db_session, refs, reference="SIG-INTENT-3A")
    signal_id_2 = await seed_signal(db_session, refs, reference="SIG-INTENT-3B")
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    account_repo = AccountRepository(db_session)
    outbox_repo = OutboxRepository(db_session)
    intent_repo = TradeIntentRepository(db_session)
    now = datetime.now(UTC)

    result1 = await submit_decision(
        _trade_decision(symbol="XAUUSD", as_of=now),
        account_repo=account_repo,
        outbox_repo=outbox_repo,
        intent_repo=intent_repo,
        signal_id=signal_id_1,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        environment="demo",
        as_of=now,
    )
    await db_session.commit()

    # A second, independent submission for the SAME position closes it
    # first isn't modelled here - this just checks magic is a pure function
    # of (strategy_version_id, instrument_id, environment), not a counter.
    result2 = await submit_decision(
        _trade_decision(symbol="XAUUSD", as_of=now),
        account_repo=account_repo,
        outbox_repo=outbox_repo,
        intent_repo=intent_repo,
        signal_id=signal_id_2,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        environment="demo",
        as_of=now,
    )
    await db_session.commit()

    pending = await outbox_repo.claim_pending(limit=10)
    magics = {row.payload["magic"] for row in pending}
    assert len(magics) == 1  # both intents share the same magic
    assert result1.trade_intent_id != result2.trade_intent_id
