"""SPEC-05 §3.5 acceptance for `SignalRepository`: a TRADE decision
persists as a signal row, and `list_recent` returns it back as the
`SignalRef` shape `MarketState.recent_signals`/the `DUPLICATE_SETUP` gate
expect - scoped by account+instrument and a `since` cutoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.enums import Direction, Regime
from app.domain.strategy.decision import Decision
from app.domain.strategy.enums import DecisionOutcome
from app.domain.strategy.setup import Setup
from app.repositories.signals import SignalRepository
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


def _trade_decision(*, setup_fingerprint: str = "fp-1") -> Decision:
    return Decision(
        outcome=DecisionOutcome.TRADE,
        symbol="XAUUSD",
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        strategy_version_id=uuid4(),
        regime=Regime.EXPANSION,
        setup=Setup(
            kind="BREAKOUT",
            direction=Direction.LONG,
            trigger_price=Decimal("3418.20"),
            invalidation_price=Decimal("3412.55"),
            reference_level=Decimal("3417.00"),
            fingerprint=setup_fingerprint,
        ),
        direction=Direction.LONG,
        entry=Decimal("3418.20"),
        stop_loss=Decimal("3412.55"),
        take_profits=(),
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="test",
        engine_duration_ms=0,
    )


async def test_create_then_list_recent_round_trips(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = SignalRepository(db_session)
    now = datetime.now(UTC)
    signal_id = await repo.create(
        _trade_decision(),
        reference="ref-1",
        analysis_run_id=refs.analysis_run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        created_at=now,
    )
    await db_session.commit()

    recent = await repo.list_recent(
        refs.account_id, refs.instrument_id, symbol="XAUUSD", since=now - timedelta(minutes=1)
    )

    assert len(recent) == 1
    assert recent[0].id == signal_id
    assert recent[0].symbol == "XAUUSD"
    assert recent[0].setup_fingerprint == "fp-1"
    assert recent[0].direction == Direction.LONG
    assert recent[0].created_at == now


async def test_list_recent_excludes_signals_before_since(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = SignalRepository(db_session)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    await repo.create(
        _trade_decision(),
        reference="ref-old",
        analysis_run_id=refs.analysis_run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        created_at=old,
    )
    await db_session.commit()

    recent = await repo.list_recent(
        refs.account_id,
        refs.instrument_id,
        symbol="XAUUSD",
        since=old + timedelta(minutes=1),
    )

    assert recent == ()


async def test_no_setup_falls_back_to_reference_as_fingerprint(
    db_session: AsyncSession,
) -> None:
    """A `TRADE` decision without a `Setup` (not all strategies need one) -
    setup_kind/setup_fingerprint must still be non-null columns, so
    `create()` falls back rather than raising."""
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    decision = Decision(
        outcome=DecisionOutcome.TRADE,
        symbol="XAUUSD",
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        strategy_version_id=uuid4(),
        regime=Regime.EXPANSION,
        setup=None,
        direction=Direction.LONG,
        entry=Decimal("3418.20"),
        stop_loss=Decimal("3412.55"),
        take_profits=(),
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="test",
        engine_duration_ms=0,
    )
    repo = SignalRepository(db_session)
    now = datetime.now(UTC)
    signal_id = await repo.create(
        decision,
        reference="ref-no-setup",
        analysis_run_id=refs.analysis_run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        created_at=now,
    )
    await db_session.commit()

    recent = await repo.list_recent(
        refs.account_id, refs.instrument_id, symbol="XAUUSD", since=now - timedelta(minutes=1)
    )
    assert recent[0].id == signal_id
    assert recent[0].setup_fingerprint == "ref-no-setup"


async def test_get_returns_the_signal_record(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    decision = _trade_decision(setup_fingerprint="fp-get")
    repo = SignalRepository(db_session)
    now = datetime.now(UTC)
    signal_id = await repo.create(
        decision,
        reference="ref-get",
        analysis_run_id=refs.analysis_run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        created_at=now,
    )
    await db_session.commit()

    record = await repo.get(signal_id)

    assert record is not None
    assert record.id == signal_id
    assert record.account_id == refs.account_id
    assert record.instrument_id == refs.instrument_id
    assert record.strategy_version_id == refs.strategy_version_id
    assert record.direction == Direction.LONG
    assert record.entry == Decimal("3418.20")
    assert record.stop_loss == Decimal("3412.55")
    assert record.take_profits == ()
    assert record.confluence_score == Decimal("8.0")


async def test_get_returns_none_for_an_unknown_signal(db_session: AsyncSession) -> None:
    repo = SignalRepository(db_session)
    assert await repo.get(uuid4()) is None
