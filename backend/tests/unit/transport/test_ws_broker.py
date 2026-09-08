"""`WSAgentBroker` against a fake agent connection - a `send_text` callback
that decodes the command envelope and replies exactly the way
`agent/executor.py` + `agent/main.py` actually do (payload shapes, the
`event.<command>_result` naming, correlation by envelope id), so these
tests catch a wire-format mismatch with the real agent, not just internal
consistency with itself."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain.execution.enums import DealType, OrderSide, OrderType
from app.domain.execution.intent import OrderIntent
from app.transport.envelope import Envelope
from app.transport.registry import AgentConnectionRegistry
from app.transport.ws_broker import WSAgentBroker

pytestmark = pytest.mark.unit

Responder = Callable[[Envelope], dict[str, Any] | None]


class FakeAgent:
    """Decodes the command sent to it and immediately resolves the matching
    reply in-process - no real socket, no asyncio.wait_for race to manage."""

    def __init__(
        self, registry: AgentConnectionRegistry, account_id: uuid.UUID, *, responder: Responder
    ) -> None:
        self._registry = registry
        self._account_id = account_id
        self._responder = responder

    async def send_text(self, data: str) -> None:
        envelope = Envelope.decode(data)
        reply = self._responder(envelope)
        if reply is not None:
            self._registry.resolve(self._account_id, correlation_id=envelope.id, payload=reply)


def _broker(registry: AgentConnectionRegistry, account_id: uuid.UUID) -> WSAgentBroker:
    return WSAgentBroker(account_id=account_id, registry=registry, timeout_seconds=1.0)


def _intent(**overrides: Any) -> OrderIntent:
    base: dict[str, Any] = {
        "client_order_id": "CO-1",
        "signal_id": uuid.uuid4(),
        "account_id": uuid.uuid4(),
        "symbol": "XAUUSD",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "volume": Decimal("0.10"),
        "limit_price": None,
        "stop_loss": Decimal("3390"),
        "take_profit": Decimal("3420"),
        "max_slippage_points": 30,
        "magic": 12345,
        "comment": "CO-1",
        "expires_at": None,
        "idempotency_key": "CO-1",
    }
    base.update(overrides)
    return OrderIntent(**base)


async def test_place_order_parses_a_real_shaped_reply() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()

    def responder(envelope: Envelope) -> dict[str, Any] | None:
        assert envelope.type == "command.place_order"
        assert envelope.id == "CO-1"  # SPEC-04 §3: id == client_order_id for orders
        assert envelope.payload["side"] == "BUY"
        return {
            "client_order_id": "CO-1",
            "retcode": 10009,
            "retcode_text": "DONE",
            "broker_order_id": "555",
            "broker_deal_id": "777",
            "broker_position_id": "555",
            "filled_volume": "0.10",
            "fill_price": "3400.10",
            "requested_price": "3400.10",
            "slippage_points": 0,
            "latency_ms": 42,
            "raw": {},
        }

    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=responder),
    )
    result = await _broker(registry, account_id).place_order(_intent(), at=datetime.now(UTC))

    assert result is not None
    assert result.retcode == 10009
    assert result.broker_position_id == "555"
    assert result.filled_volume == Decimal("0.10")
    assert result.fill_price == Decimal("3400.10")


async def test_place_order_returns_none_when_the_agent_never_replies() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=lambda _e: None),
    )
    broker = WSAgentBroker(account_id=account_id, registry=registry, timeout_seconds=0.05)

    result = await broker.place_order(_intent(), at=datetime.now(UTC))
    assert result is None


async def test_place_order_returns_none_when_there_is_no_connection_at_all() -> None:
    registry = AgentConnectionRegistry()
    broker = _broker(registry, uuid.uuid4())
    result = await broker.place_order(_intent(), at=datetime.now(UTC))
    assert result is None


async def test_modify_position_parses_the_reply() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()

    def responder(envelope: Envelope) -> dict[str, Any] | None:
        assert envelope.type == "command.modify_position"
        assert envelope.payload["broker_position_id"] == "555"
        assert envelope.payload["stop_loss"] == "3395"
        return {"broker_position_id": "555", "retcode": 10009, "retcode_text": "DONE"}

    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=responder),
    )
    result = await _broker(registry, account_id).modify_position("555", stop_loss=Decimal("3395"))
    assert result.retcode == 10009


async def test_modify_position_returns_agent_unreachable_on_timeout() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=lambda _e: None),
    )
    broker = WSAgentBroker(account_id=account_id, registry=registry, timeout_seconds=0.05)

    result = await broker.modify_position("555", stop_loss=Decimal("3395"))
    assert result.retcode_text == "AGENT_UNREACHABLE"


async def test_close_position_parses_a_fill() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()

    def responder(envelope: Envelope) -> dict[str, Any] | None:
        assert envelope.type == "command.close_position"
        return {
            "broker_deal_id": "901",
            "client_order_id": "",
            "broker_order_id": "900",
            "broker_position_id": "555",
            "symbol": "XAUUSD",
            "side": "SELL",
            "volume": "0.10",
            "price": "3420.00",
            "commission": "0",
            "swap": "0",
            "profit": "200.00",
            "executed_at": "2026-09-07T09:14:22.318000+00:00",
            "deal_type": "EXIT",
        }

    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=responder),
    )
    fill = await _broker(registry, account_id).close_position(
        "555", price=Decimal("3420"), at=datetime.now(UTC)
    )

    assert fill is not None
    assert fill.side == OrderSide.SELL
    assert fill.deal_type == DealType.EXIT
    assert fill.profit == Decimal("200.00")


async def test_close_position_returns_none_on_no_position_error() -> None:
    """`agent/executor.py`'s `_handle_close_position` replies
    `{"error": "NO_POSITION"}`, not a Fill shape, when MT5 has no record of
    the position."""
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=lambda _e: {"error": "NO_POSITION"}),
    )
    fill = await _broker(registry, account_id).close_position(
        "does-not-exist", price=Decimal("3420"), at=datetime.now(UTC)
    )
    assert fill is None


async def test_get_positions_parses_the_list() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()

    def responder(envelope: Envelope) -> dict[str, Any] | None:
        assert envelope.type == "command.get_positions"
        return {
            "positions": [
                {
                    "broker_position_id": "555",
                    "symbol": "XAUUSD",
                    "side": "BUY",
                    "volume": "0.10",
                    "entry_price": "3400.00",
                    "stop_loss": "3390.00",
                    "take_profit": None,
                    "magic": 12345,
                    "comment": "CO-1",
                }
            ]
        }

    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=responder),
    )
    positions = await _broker(registry, account_id).get_positions()

    assert len(positions) == 1
    assert positions[0].broker_position_id == "555"
    assert positions[0].take_profit is None


async def test_get_positions_returns_empty_tuple_when_unreachable() -> None:
    registry = AgentConnectionRegistry()
    broker = _broker(registry, uuid.uuid4())
    assert await broker.get_positions() == ()


async def test_get_deals_parses_the_list_and_since_filter() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    since = datetime(2026, 9, 1, tzinfo=UTC)

    def responder(envelope: Envelope) -> dict[str, Any] | None:
        assert envelope.type == "command.get_deals"
        assert envelope.payload["from"] == since.isoformat()
        return {
            "deals": [
                {
                    "broker_deal_id": "777",
                    "client_order_id": "CO-1",
                    "broker_order_id": "555",
                    "broker_position_id": "555",
                    "symbol": "XAUUSD",
                    "side": "BUY",
                    "volume": "0.10",
                    "price": "3400.10",
                    "commission": "0",
                    "swap": "0",
                    "profit": "0",
                    "executed_at": "2026-09-07T09:14:22.318000+00:00",
                    "deal_type": "ENTRY",
                }
            ]
        }

    registry.register(
        account_id=account_id,
        agent_id=uuid.uuid4(),
        socket=FakeAgent(registry, account_id, responder=responder),
    )
    deals = await _broker(registry, account_id).get_deals(since=since)

    assert len(deals) == 1
    assert deals[0].deal_type == DealType.ENTRY
