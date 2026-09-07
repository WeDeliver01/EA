"""SPEC-06 §6 unit tests for `classify()` - the pure comparison logic, one
test per row of the discrepancy table this MVP implements."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.execution.enums import ExecutionState, OrderSide
from app.execution.broker import BrokerPositionSnapshot
from app.execution.reconciliation import Finding, LocalOpenPosition, PendingIntent, classify

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _local(
    *,
    broker_position_id: str = "1001",
    volume: str = "0.10",
    stop_loss: str | None = "3390",
    take_profit: str | None = "3420",
    entry_price: str = "3400",
) -> LocalOpenPosition:
    return LocalOpenPosition(
        id=uuid.uuid4(),
        broker_position_id=broker_position_id,
        volume=Decimal(volume),
        stop_loss=Decimal(stop_loss) if stop_loss is not None else None,
        take_profit=Decimal(take_profit) if take_profit is not None else None,
        entry_price=Decimal(entry_price),
    )


def _broker(
    *,
    broker_position_id: str = "1001",
    volume: str = "0.10",
    stop_loss: str | None = "3390",
    take_profit: str | None = "3420",
    entry_price: str = "3400",
    magic: int = 42,
    side: OrderSide = OrderSide.BUY,
) -> BrokerPositionSnapshot:
    return BrokerPositionSnapshot(
        broker_position_id=broker_position_id,
        symbol="XAUUSD",
        side=side,
        volume=Decimal(volume),
        entry_price=Decimal(entry_price),
        stop_loss=Decimal(stop_loss) if stop_loss is not None else None,
        take_profit=Decimal(take_profit) if take_profit is not None else None,
        magic=magic,
        comment="",
    )


def _intent(
    *, magic: int = 42, created_at: datetime = _NOW, state: ExecutionState = ExecutionState.SENT
) -> PendingIntent:
    return PendingIntent(
        id=uuid.uuid4(),
        client_order_id="CO-1",
        magic=magic,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.10"),
        created_at=created_at,
        state=state,
    )


def _kinds(findings: list[Finding]) -> set[str]:
    return {f.kind for f in findings}


def test_matching_position_produces_no_findings() -> None:
    findings = classify(
        local_positions=[_local()],
        broker_positions=(_broker(),),
        pending_intents=[],
        as_of=_NOW,
    )
    assert findings == []


def test_missing_at_broker() -> None:
    findings = classify(
        local_positions=[_local()], broker_positions=(), pending_intents=[], as_of=_NOW
    )
    assert _kinds(findings) == {"MISSING_AT_BROKER"}
    assert findings[0].severity == "critical"


def test_unknown_at_broker_with_no_matching_intent() -> None:
    findings = classify(
        local_positions=[],
        broker_positions=(_broker(magic=999),),
        pending_intents=[],
        as_of=_NOW,
    )
    assert _kinds(findings) == {"UNKNOWN_AT_BROKER"}
    assert findings[0].severity == "critical"
    assert findings[0].matched_intent_id is None


def test_unknown_at_broker_with_matching_intent() -> None:
    intent = _intent(magic=42)
    findings = classify(
        local_positions=[],
        broker_positions=(_broker(magic=42),),
        pending_intents=[intent],
        as_of=_NOW,
    )
    unknown = [f for f in findings if f.kind == "UNKNOWN_AT_BROKER"]
    assert len(unknown) == 1
    assert unknown[0].matched_intent_id == intent.id


def test_volume_mismatch() -> None:
    findings = classify(
        local_positions=[_local(volume="0.10")],
        broker_positions=(_broker(volume="0.05"),),
        pending_intents=[],
        as_of=_NOW,
    )
    assert "VOLUME_MISMATCH" in _kinds(findings)
    match = next(f for f in findings if f.kind == "VOLUME_MISMATCH")
    assert match.severity == "critical"


def test_sl_mismatch_beyond_tolerance() -> None:
    findings = classify(
        local_positions=[_local(stop_loss="3390")],
        broker_positions=(_broker(stop_loss="3395"),),
        pending_intents=[],
        as_of=_NOW,
        sl_tp_tolerance=Decimal("0.01"),
    )
    assert "SL_MISMATCH" in _kinds(findings)
    match = next(f for f in findings if f.kind == "SL_MISMATCH")
    assert match.severity == "warning"


def test_sl_mismatch_within_tolerance_is_not_flagged() -> None:
    findings = classify(
        local_positions=[_local(stop_loss="3390.00")],
        broker_positions=(_broker(stop_loss="3390.005"),),
        pending_intents=[],
        as_of=_NOW,
        sl_tp_tolerance=Decimal("0.01"),
    )
    assert "SL_MISMATCH" not in _kinds(findings)


def test_sl_mismatch_when_broker_has_no_stop_but_we_expect_one() -> None:
    findings = classify(
        local_positions=[_local(stop_loss="3390")],
        broker_positions=(_broker(stop_loss=None),),
        pending_intents=[],
        as_of=_NOW,
    )
    assert "SL_MISMATCH" in _kinds(findings)


def test_tp_mismatch_beyond_tolerance() -> None:
    findings = classify(
        local_positions=[_local(take_profit="3420")],
        broker_positions=(_broker(take_profit="3425"),),
        pending_intents=[],
        as_of=_NOW,
    )
    assert "TP_MISMATCH" in _kinds(findings)
    match = next(f for f in findings if f.kind == "TP_MISMATCH")
    assert match.severity == "warning"


def test_price_mismatch_beyond_tolerance() -> None:
    findings = classify(
        local_positions=[_local(entry_price="3400.00")],
        broker_positions=(_broker(entry_price="3400.50"),),
        pending_intents=[],
        as_of=_NOW,
    )
    assert "PRICE_MISMATCH" in _kinds(findings)
    match = next(f for f in findings if f.kind == "PRICE_MISMATCH")
    assert match.severity == "warning"


def test_orphan_intent_after_grace_period() -> None:
    old_intent = _intent(magic=42, created_at=_NOW - timedelta(seconds=121))
    findings = classify(
        local_positions=[], broker_positions=(), pending_intents=[old_intent], as_of=_NOW
    )
    assert _kinds(findings) == {"ORPHAN_INTENT"}
    assert findings[0].severity == "critical"


def test_orphan_intent_not_flagged_within_grace_period() -> None:
    """A normal SENT intent, seconds old, with no broker position yet - the
    dispatcher just hasn't run. This must never be flagged."""
    fresh_intent = _intent(magic=42, created_at=_NOW - timedelta(seconds=5))
    findings = classify(
        local_positions=[], broker_positions=(), pending_intents=[fresh_intent], as_of=_NOW
    )
    assert findings == []


def test_orphan_intent_not_flagged_when_a_matching_broker_position_exists() -> None:
    old_intent = _intent(magic=42, created_at=_NOW - timedelta(seconds=200))
    findings = classify(
        local_positions=[],
        broker_positions=(_broker(magic=42),),
        pending_intents=[old_intent],
        as_of=_NOW,
    )
    assert "ORPHAN_INTENT" not in _kinds(findings)
