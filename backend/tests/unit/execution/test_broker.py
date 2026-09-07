"""SPEC-04 / SPEC-06 §10 unit tests for the fake agent.

`SimulatedBroker` is what makes the chaos scenarios in SPEC-06 §10
testable at all - each fault is exercised here in isolation before the
integration tests build the full pipeline on top of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.execution.enums import DealType, OrderSide, OrderType
from app.domain.execution.intent import OrderIntent
from app.domain.market.enums import AssetClass
from app.domain.market.symbol_spec import SymbolSpec
from app.execution.broker import BrokerFault, SimulatedBroker

pytestmark = pytest.mark.unit

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

_AT = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _intent(*, client_order_id: str = "CO-1", volume: str = "0.10") -> OrderIntent:
    import uuid

    return OrderIntent(
        client_order_id=client_order_id,
        signal_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        volume=Decimal(volume),
        limit_price=Decimal("3400"),
        stop_loss=Decimal("3390"),
        take_profit=Decimal("3420"),
        max_slippage_points=30,
        magic=12345,
        comment=client_order_id[:24],
        expires_at=None,
        idempotency_key=client_order_id,
    )


def test_place_order_fills_and_opens_a_position() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    result = broker.place_order(_intent(), at=_AT)

    assert result is not None
    assert result.retcode == 10009
    assert result.filled_volume == Decimal("0.10")
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].volume == Decimal("0.10")
    deals = broker.get_deals()
    assert len(deals) == 1
    assert deals[0].deal_type == DealType.ENTRY
    assert deals[0].profit == Decimal("0")  # no P&L realised on open


def test_place_order_is_idempotent_by_client_order_id() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    result1 = broker.place_order(_intent(), at=_AT)
    result2 = broker.place_order(_intent(), at=_AT)  # same client_order_id

    assert result1 == result2
    # No second position or deal was created by the replay.
    assert len(broker.get_positions()) == 1
    assert len(broker.get_deals()) == 1


def test_reject_fault_returns_no_position() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    broker.inject_fault("CO-1", BrokerFault.REJECT, retcode=10016, retcode_text="INVALID_STOPS")
    result = broker.place_order(_intent(), at=_AT)

    assert result is not None
    assert result.retcode == 10016
    assert result.retcode_text == "INVALID_STOPS"
    assert result.filled_volume == Decimal("0")
    assert broker.get_positions() == ()
    assert broker.get_deals() == ()


def test_partial_fill_fault_fills_less_than_requested() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    broker.inject_fault("CO-1", BrokerFault.PARTIAL_FILL, fraction="0.5")
    result = broker.place_order(_intent(volume="0.10"), at=_AT)

    assert result is not None
    assert result.filled_volume == Decimal("0.050")
    assert broker.get_positions()[0].volume == Decimal("0.050")


def test_silent_fault_fills_at_the_broker_but_returns_none() -> None:
    """The order genuinely happens - `get_positions`/`get_deals` show it -
    but the caller gets nothing back, simulating a dropped connection right
    after the broker accepted the order (SPEC-06 §10 row 1)."""
    broker = SimulatedBroker(spec=_SPEC)
    broker.inject_fault("CO-1", BrokerFault.SILENT)
    result = broker.place_order(_intent(), at=_AT)

    assert result is None
    assert len(broker.get_positions()) == 1
    assert len(broker.get_deals()) == 1


def test_close_position_fully_realises_pnl_and_removes_the_position() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    open_result = broker.place_order(_intent(), at=_AT)
    assert open_result is not None
    broker_position_id = open_result.broker_position_id
    assert broker_position_id is not None

    close_deal = broker.close_position(broker_position_id, price=Decimal("3420"), at=_AT)

    assert close_deal is not None
    # (3420 - 3400) / 0.01 ticks * $1.00/tick * 0.10 lots = $200.
    assert close_deal.profit == Decimal("200.00")
    assert close_deal.deal_type == DealType.EXIT
    assert broker.get_positions() == ()


def test_close_position_partial_leaves_the_remainder_open() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    open_result = broker.place_order(_intent(volume="0.10"), at=_AT)
    assert open_result is not None
    broker_position_id = open_result.broker_position_id
    assert broker_position_id is not None

    close_deal = broker.close_position(
        broker_position_id, price=Decimal("3420"), at=_AT, volume=Decimal("0.04")
    )

    assert close_deal is not None
    assert close_deal.deal_type == DealType.PARTIAL_EXIT
    remaining = broker.get_positions()
    assert len(remaining) == 1
    assert remaining[0].volume == Decimal("0.06")


def test_modify_position_updates_stop_and_take_profit() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    open_result = broker.place_order(_intent(), at=_AT)
    assert open_result is not None
    broker_position_id = open_result.broker_position_id
    assert broker_position_id is not None

    modify_result = broker.modify_position(broker_position_id, stop_loss=Decimal("3395"))

    assert modify_result.retcode == 10009
    assert broker.get_positions()[0].stop_loss == Decimal("3395")


def test_modify_position_on_unknown_position_returns_no_position_retcode() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    result = broker.modify_position("does-not-exist", stop_loss=Decimal("3395"))
    assert result.retcode != 10009


def test_simulate_manual_open_uses_magic_zero() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    broker.simulate_manual_open(
        side=OrderSide.BUY, volume=Decimal("0.05"), price=Decimal("3405"), at=_AT
    )
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].magic == 0


def test_simulate_manual_close_removes_the_position_and_records_a_deal() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    open_result = broker.place_order(_intent(), at=_AT)
    assert open_result is not None
    broker_position_id = open_result.broker_position_id
    assert broker_position_id is not None

    broker.simulate_manual_close(broker_position_id, price=Decimal("3410"), at=_AT)

    assert broker.get_positions() == ()
    deals = broker.get_deals()
    assert len(deals) == 2  # the original entry, plus the manual close
    assert deals[-1].deal_type == DealType.EXIT


def test_simulate_broker_moves_stop() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    open_result = broker.place_order(_intent(), at=_AT)
    assert open_result is not None
    broker_position_id = open_result.broker_position_id
    assert broker_position_id is not None

    broker.simulate_broker_moves_stop(broker_position_id, new_stop=Decimal("3396"))

    assert broker.get_positions()[0].stop_loss == Decimal("3396")


def test_get_deals_since_filters_by_executed_at() -> None:
    broker = SimulatedBroker(spec=_SPEC)
    broker.place_order(_intent(client_order_id="CO-1"), at=_AT)
    later = _AT.replace(hour=10)
    broker.place_order(_intent(client_order_id="CO-2"), at=later)

    assert len(broker.get_deals(since=_AT)) == 2
    assert len(broker.get_deals(since=later)) == 1
