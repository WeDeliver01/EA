from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from agent import mt5_client as mt5_client_module
from agent.models import OrderSide, OrderType, PlaceOrderCommand
from agent.mt5_client import (
    LOCAL_INVALID_VOLUME,
    LOCAL_NOT_CONNECTED,
    LOCAL_UNSUPPORTED_ORDER_TYPE,
    MT5Client,
)
from agent.tests.fake_mt5 import (
    FakeMT5,
    make_deal,
    make_order_result,
    make_position,
    make_symbol,
    make_tick,
)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMT5:
    f = FakeMT5()
    monkeypatch.setattr(mt5_client_module, "mt5", f)
    return f


@pytest.fixture
async def client(fake: FakeMT5) -> MT5Client:
    c = MT5Client()
    await c.connect()
    return c


def place_cmd(**overrides: Any) -> PlaceOrderCommand:
    base: dict[str, Any] = {
        "client_order_id": "01JCXG",
        "symbol": "XAUUSD",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "volume": Decimal("0.01"),
        "limit_price": None,
        "stop_loss": Decimal("99.00"),
        "take_profit": Decimal("101.00"),
        "max_slippage_points": 30,
        "magic": 1,
        "comment": "test",
    }
    base.update(overrides)
    return PlaceOrderCommand(**base)


class TestResolveFillingMode:
    def test_prefers_fok(self, fake: FakeMT5) -> None:
        c = MT5Client()
        assert c._resolve_filling_mode(0b11) == fake.ORDER_FILLING_FOK

    def test_falls_back_to_ioc(self, fake: FakeMT5) -> None:
        c = MT5Client()
        assert c._resolve_filling_mode(0b10) == fake.ORDER_FILLING_IOC

    def test_falls_back_to_return(self, fake: FakeMT5) -> None:
        c = MT5Client()
        assert c._resolve_filling_mode(0b00) == fake.ORDER_FILLING_RETURN


class TestPlaceOrderPreflight:
    """SPEC-04 §4 steps 3-5: local rejections never reach the broker."""

    async def test_not_connected_rejects_locally(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.terminal.trade_allowed = False
        result = await client.place_order(place_cmd())
        assert result.retcode == LOCAL_NOT_CONNECTED
        assert fake.order_send_calls == []

    async def test_non_market_order_rejects_locally(self, fake: FakeMT5, client: MT5Client) -> None:
        cmd = place_cmd(order_type=OrderType.LIMIT, limit_price=Decimal("100"))
        result = await client.place_order(cmd)
        assert result.retcode == LOCAL_UNSUPPORTED_ORDER_TYPE
        assert fake.order_send_calls == []

    async def test_invalid_volume_rejects_locally(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.symbols["XAUUSD"] = make_symbol(volume_min=0.01, volume_step=0.01)
        fake.ticks["XAUUSD"] = make_tick()
        result = await client.place_order(place_cmd(volume=Decimal("0.015")))
        assert result.retcode == LOCAL_INVALID_VOLUME
        assert fake.order_send_calls == []

    async def test_volume_below_min_rejects_locally(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.symbols["XAUUSD"] = make_symbol(volume_min=0.05, volume_step=0.01)
        fake.ticks["XAUUSD"] = make_tick()
        result = await client.place_order(place_cmd(volume=Decimal("0.01")))
        assert result.retcode == LOCAL_INVALID_VOLUME

    async def test_never_rounds_silently(self, fake: FakeMT5, client: MT5Client) -> None:
        """Step 4: a volume mismatch is rejected, never rounded - a mismatch
        means the backend's cached spec is stale."""
        fake.symbols["XAUUSD"] = make_symbol(volume_min=0.01, volume_step=0.01)
        fake.ticks["XAUUSD"] = make_tick()
        result = await client.place_order(place_cmd(volume=Decimal("0.017")))
        assert result.retcode == LOCAL_INVALID_VOLUME
        assert result.filled_volume == Decimal(0)


class TestPlaceOrderFill:
    async def test_done_result_maps_fields(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.symbols["XAUUSD"] = make_symbol()
        fake.ticks["XAUUSD"] = make_tick(bid=99.9, ask=100.1)
        fake.order_send_result = make_order_result(
            retcode=fake.TRADE_RETCODE_DONE, order=555, deal=777, volume=0.01, price=100.1
        )
        result = await client.place_order(place_cmd())
        assert result.retcode == fake.TRADE_RETCODE_DONE
        assert result.broker_position_id == "555"
        assert result.broker_deal_id == "777"
        assert result.filled_volume == Decimal("0.01")
        assert result.fill_price == Decimal("100.1")

    async def test_zero_price_falls_back_to_deal_lookup(
        self, fake: FakeMT5, client: MT5Client
    ) -> None:
        """The known 'market execution' quirk: order_send returns DONE with
        deal=0, price=0.0; the real fill has to be read from history."""
        fake.symbols["XAUUSD"] = make_symbol()
        fake.ticks["XAUUSD"] = make_tick(bid=99.9, ask=100.1)
        fake.order_send_result = make_order_result(
            retcode=fake.TRADE_RETCODE_DONE, order=555, deal=0, volume=0.01, price=0.0
        )
        fake.history_deals = [
            make_deal(
                ticket=888,
                order=555,
                position_id=555,
                type=fake.DEAL_TYPE_BUY,
                entry=fake.DEAL_ENTRY_IN,
                price=100.12,
            )
        ]
        result = await client.place_order(place_cmd())
        assert result.broker_deal_id == "888"
        assert result.fill_price == Decimal("100.12")

    async def test_rejected_result_has_no_position(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.symbols["XAUUSD"] = make_symbol()
        fake.ticks["XAUUSD"] = make_tick()
        fake.order_send_result = make_order_result(retcode=10006, comment="REJECTED")
        result = await client.place_order(place_cmd())
        assert result.broker_position_id is None
        assert result.filled_volume == Decimal(0)
        assert result.fill_price is None


class TestModifyPosition:
    async def test_no_position_returns_no_position_retcode(
        self, fake: FakeMT5, client: MT5Client
    ) -> None:
        result = await client.modify_position("999", stop_loss=Decimal("1"))
        assert result.retcode == 10036

    async def test_modifies_existing_position(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.positions[555] = make_position(ticket=555, sl=98.0, tp=102.0)
        fake.order_send_result = make_order_result(retcode=fake.TRADE_RETCODE_DONE)
        result = await client.modify_position("555", stop_loss=Decimal("99.5"))
        assert result.retcode == fake.TRADE_RETCODE_DONE
        sent = fake.order_send_calls[-1]
        assert sent["sl"] == 99.5
        assert sent["tp"] == 102.0  # untouched field carries the current value


class TestClosePosition:
    async def test_no_position_returns_none(self, fake: FakeMT5, client: MT5Client) -> None:
        assert await client.close_position("999", max_slippage_points=30) is None

    async def test_full_close(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.positions[555] = make_position(ticket=555, volume=0.02, type=fake.POSITION_TYPE_BUY)
        fake.ticks["XAUUSD"] = make_tick(bid=99.5, ask=99.6)
        fake.symbols["XAUUSD"] = make_symbol()
        fake.order_send_result = make_order_result(
            retcode=fake.TRADE_RETCODE_DONE, order=900, deal=901, volume=0.02, price=99.5
        )
        fill = await client.close_position("555", max_slippage_points=30)
        assert fill is not None
        assert fill.side == OrderSide.SELL  # closing a BUY sells
        assert fill.volume == Decimal("0.02")
        sent = fake.order_send_calls[-1]
        assert sent["type"] == fake.ORDER_TYPE_SELL
        assert sent["price"] == 99.5  # closing a long hits the bid

    async def test_partial_close_uses_requested_volume(
        self, fake: FakeMT5, client: MT5Client
    ) -> None:
        fake.positions[555] = make_position(ticket=555, volume=0.02, type=fake.POSITION_TYPE_BUY)
        fake.ticks["XAUUSD"] = make_tick(bid=99.5, ask=99.6)
        fake.symbols["XAUUSD"] = make_symbol()
        fake.order_send_result = make_order_result(retcode=fake.TRADE_RETCODE_DONE, volume=0.01)
        fill = await client.close_position("555", max_slippage_points=30, volume=Decimal("0.01"))
        assert fill is not None
        assert fill.deal_type.value == "PARTIAL_EXIT"
        assert fake.order_send_calls[-1]["volume"] == 0.01

    async def test_close_volume_never_exceeds_position(
        self, fake: FakeMT5, client: MT5Client
    ) -> None:
        fake.positions[555] = make_position(ticket=555, volume=0.01, type=fake.POSITION_TYPE_BUY)
        fake.ticks["XAUUSD"] = make_tick()
        fake.symbols["XAUUSD"] = make_symbol()
        fake.order_send_result = make_order_result(retcode=fake.TRADE_RETCODE_DONE, volume=0.01)
        await client.close_position("555", max_slippage_points=30, volume=Decimal("100"))
        assert fake.order_send_calls[-1]["volume"] == 0.01


class TestGetPositions:
    async def test_maps_snapshot_fields(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.positions[1] = make_position(ticket=1, symbol="XAUUSD", sl=99.0, tp=101.0, magic=42)
        snapshots = await client.get_positions()
        assert len(snapshots) == 1
        assert snapshots[0].broker_position_id == "1"
        assert snapshots[0].magic == 42
        assert snapshots[0].stop_loss == Decimal("99.0")

    async def test_zero_sl_tp_are_none(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.positions[1] = make_position(ticket=1, sl=0.0, tp=0.0)
        snapshots = await client.get_positions()
        assert snapshots[0].stop_loss is None
        assert snapshots[0].take_profit is None


class _FrozenDateTime(datetime):
    """`datetime` subclass with `now()` pinned, so broker-offset arithmetic
    (which calls `datetime.now(tz=UTC)`) is deterministic in tests. `fromtimestamp`
    is inherited unchanged."""

    _frozen_now: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._frozen_now.astimezone(tz) if tz else cls._frozen_now


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    agent_now = datetime(2023, 11, 14, 11, 0, 0, tzinfo=UTC)
    frozen = type("FrozenDateTime", (_FrozenDateTime,), {"_frozen_now": agent_now})
    monkeypatch.setattr(mt5_client_module, "datetime", frozen)
    return agent_now


class TestGetDeals:
    async def test_filters_non_trade_deal_types(self, fake: FakeMT5, client: MT5Client) -> None:
        fake.history_deals = [
            make_deal(
                ticket=1,
                order=1,
                position_id=1,
                type=fake.DEAL_TYPE_BUY,
                entry=fake.DEAL_ENTRY_IN,
            ),
            make_deal(ticket=2, order=2, position_id=1, type=fake.DEAL_TYPE_BALANCE, entry=0),
        ]
        deals = await client.get_deals()
        assert len(deals) == 1
        assert deals[0].broker_deal_id == "1"

    async def test_query_bounds_are_naive_broker_time(
        self, fake: FakeMT5, client: MT5Client, frozen_now: datetime
    ) -> None:
        """`history_deals_get` rejects tz-aware/int bounds on this broker and
        interprets naive bounds as broker-server time, not UTC."""
        fake.symbols["XAUUSD"] = make_symbol()
        broker_now = frozen_now + timedelta(hours=1)
        fake.ticks["XAUUSD"] = make_tick(time=int(broker_now.timestamp()))

        since = datetime(2023, 11, 14, 12, 0, 0, tzinfo=UTC)
        await client.get_deals(since=since)

        args, _kwargs = fake.history_deals_get_args
        date_from, date_to = args
        assert date_from.tzinfo is None
        assert date_to.tzinfo is None
        assert date_from.hour == 13  # since (12:00) shifted by the +1h broker offset

    async def test_executed_at_is_converted_back_to_utc(
        self, fake: FakeMT5, client: MT5Client, frozen_now: datetime
    ) -> None:
        fake.symbols["XAUUSD"] = make_symbol()
        broker_now = frozen_now + timedelta(hours=1)
        fake.ticks["XAUUSD"] = make_tick(time=int(broker_now.timestamp()))

        deal_broker_time = frozen_now + timedelta(hours=1, minutes=5)
        fake.history_deals = [
            make_deal(
                ticket=1,
                order=1,
                position_id=1,
                type=fake.DEAL_TYPE_BUY,
                entry=fake.DEAL_ENTRY_IN,
                time=int(deal_broker_time.timestamp()),
            )
        ]
        deals = await client.get_deals()
        # A deal recorded 5 minutes after the (frozen) broker "now", once the
        # +1h broker offset is subtracted back out, is 5 minutes after the
        # frozen agent "now" in true UTC.
        assert deals[0].executed_at == frozen_now + timedelta(minutes=5)
