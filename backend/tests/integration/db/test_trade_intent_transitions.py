"""SPEC-10 Phase 1 acceptance:
- changing trade_intents.state without a transition row raises (DB trigger)
- the repository enforces the same state machine in Python before ever
  reaching the database
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import IllegalStateTransition
from app.domain.execution.enums import ExecutionState, OrderSide, OrderType
from app.domain.execution.intent import OrderIntent
from app.repositories.trade_intents import TradeIntentRepository
from tests.integration.seed import seed_minimal_refs, seed_signal

pytestmark = pytest.mark.integration


def _intent(*, signal_id, account_id, client_order_id: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        signal_id=signal_id,
        account_id=account_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        volume=Decimal("0.10"),
        limit_price=None,
        stop_loss=Decimal("3412.55"),
        take_profit=Decimal("3428.10"),
        max_slippage_points=30,
        magic=730914221,
        comment=client_order_id[:24],
        expires_at=None,
        idempotency_key=client_order_id,
    )


async def test_raw_state_change_without_transition_row_raises(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    signal_id = await seed_signal(db_session, refs, reference="SIG-TEST-0001")
    await db_session.commit()

    repo = TradeIntentRepository(db_session)
    intent_id = await repo.create(
        _intent(signal_id=signal_id, account_id=refs.account_id, client_order_id="CO-RAW-1"),
        id_=None,
        instrument_id=refs.instrument_id,
        risk_profile_id=refs.risk_profile_id,
        risk_amount=Decimal("50.00"),
        risk_pct_actual=Decimal("0.005"),
        sizing_calculation={},
        risk_state_snapshot={},
        created_at=datetime.now(UTC),
    )
    await db_session.commit()

    # Bypass the repository entirely: flip the state column with no
    # execution_transitions row. The deferred constraint trigger must reject
    # this at commit.
    await db_session.execute(
        text("UPDATE trade_intents SET state = 'VALIDATING' WHERE id = :id"),
        {"id": intent_id},
    )
    with pytest.raises(DBAPIError, match="state change without transition log"):
        await db_session.commit()
    await db_session.rollback()


async def test_repository_transition_writes_state_and_log_together(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    signal_id = await seed_signal(db_session, refs, reference="SIG-TEST-0002")
    await db_session.commit()

    repo = TradeIntentRepository(db_session)
    intent_id = await repo.create(
        _intent(signal_id=signal_id, account_id=refs.account_id, client_order_id="CO-RAW-2"),
        id_=None,
        instrument_id=refs.instrument_id,
        risk_profile_id=refs.risk_profile_id,
        risk_amount=Decimal("50.00"),
        risk_pct_actual=Decimal("0.005"),
        sizing_calculation={},
        risk_state_snapshot={},
        created_at=datetime.now(UTC),
    )
    await db_session.commit()

    await repo.transition(
        intent_id,
        ExecutionState.VALIDATING,
        reason="risk evaluation started",
        actor="worker",
        occurred_at=datetime.now(UTC),
    )
    await db_session.commit()  # must not raise: the log row exists

    assert await repo.get_state(intent_id) == ExecutionState.VALIDATING
    transitions = await repo.list_transitions(intent_id)
    assert [t.to_state for t in transitions] == [ExecutionState.DETECTED, ExecutionState.VALIDATING]


async def test_repository_rejects_illegal_transition_before_touching_the_db(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    signal_id = await seed_signal(db_session, refs, reference="SIG-TEST-0003")
    await db_session.commit()

    repo = TradeIntentRepository(db_session)
    intent_id = await repo.create(
        _intent(signal_id=signal_id, account_id=refs.account_id, client_order_id="CO-RAW-3"),
        id_=None,
        instrument_id=refs.instrument_id,
        risk_profile_id=refs.risk_profile_id,
        risk_amount=Decimal("50.00"),
        risk_pct_actual=Decimal("0.005"),
        sizing_calculation={},
        risk_state_snapshot={},
        created_at=datetime.now(UTC),
    )
    await db_session.commit()

    # DETECTED cannot jump straight to FILLED.
    with pytest.raises(IllegalStateTransition):
        await repo.transition(
            intent_id,
            ExecutionState.FILLED,
            reason="bogus",
            actor="worker",
            occurred_at=datetime.now(UTC),
        )

    # Nothing was written: state is unchanged and commit succeeds cleanly.
    await db_session.commit()
    assert await repo.get_state(intent_id) == ExecutionState.DETECTED
