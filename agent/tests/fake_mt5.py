"""A fake `MetaTrader5` module surface for tests that don't touch a real
terminal. Only the names `mt5_client.py` actually uses are provided, kept
value-compatible with the real package (SPEC-04 depends on some of these
being exact, e.g. `TRADE_RETCODE_DONE = 10009`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeMT5:
    # -- constants, values match the real MetaTrader5 package -----------------
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_TYPE_BALANCE = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1

    def __init__(self) -> None:
        self.initialize_result = True
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True, build=6184)
        self.account = SimpleNamespace(
            login=1,
            server="Test-Demo",
            currency="USD",
            leverage=100,
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            margin_free=10000.0,
            margin_level=0.0,
            trade_allowed=True,
        )
        self.symbols: dict[str, SimpleNamespace] = {}
        self.ticks: dict[str, SimpleNamespace] = {}
        self.positions: dict[int, SimpleNamespace] = {}
        self.order_send_result: Any = None
        self.order_send_calls: list[dict] = []
        self.history_deals: list[SimpleNamespace] = []
        self.last_error_value = (0, "no error")

    # -- terminal / account -----------------------------------------------------

    def initialize(self, **kwargs: Any) -> bool:
        return self.initialize_result

    def shutdown(self) -> None:
        pass

    def terminal_info(self) -> Any:
        return self.terminal

    def account_info(self) -> Any:
        return self.account

    def last_error(self) -> tuple[int, str]:
        return self.last_error_value

    # -- symbols -----------------------------------------------------------------

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return symbol in self.symbols

    def symbol_info(self, symbol: str) -> Any:
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        return self.ticks.get(symbol)

    def symbols_get(self) -> tuple:
        return tuple(self.symbols.values())

    # -- trading -----------------------------------------------------------------

    def order_send(self, request: dict) -> Any:
        self.order_send_calls.append(request)
        return self.order_send_result

    def positions_get(self, ticket: int | None = None) -> tuple:
        if ticket is not None:
            p = self.positions.get(ticket)
            return (p,) if p is not None else ()
        return tuple(self.positions.values())

    def history_deals_get(self, *args: Any, **kwargs: Any) -> tuple:
        self.history_deals_get_args = (args, kwargs)
        position = kwargs.get("position")
        if position is not None:
            return tuple(d for d in self.history_deals if d.position_id == position)
        return tuple(self.history_deals)


def make_symbol(
    *,
    name: str = "XAUUSD",
    digits: int = 2,
    point: float = 0.01,
    visible: bool = True,
    volume_min: float = 0.01,
    volume_max: float = 100.0,
    volume_step: float = 0.01,
    trade_stops_level: int = 0,
    trade_freeze_level: int = 0,
    filling_mode: int = 3,
    trade_tick_size: float = 0.01,
    trade_tick_value: float = 1.0,
    trade_contract_size: float = 100.0,
    margin_initial: float = 0.0,
    currency_profit: str = "USD",
    currency_margin: str = "USD",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        digits=digits,
        point=point,
        visible=visible,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        trade_stops_level=trade_stops_level,
        trade_freeze_level=trade_freeze_level,
        filling_mode=filling_mode,
        trade_tick_size=trade_tick_size,
        trade_tick_value=trade_tick_value,
        trade_contract_size=trade_contract_size,
        margin_initial=margin_initial,
        currency_profit=currency_profit,
        currency_margin=currency_margin,
    )


def make_tick(
    *, bid: float = 100.0, ask: float = 100.1, time: int = 1_700_000_000
) -> SimpleNamespace:
    return SimpleNamespace(bid=bid, ask=ask, time=time)


def make_order_result(
    *,
    retcode: int = 10009,
    comment: str = "Request executed",
    order: int = 1000,
    deal: int = 2000,
    volume: float = 0.01,
    price: float = 100.0,
) -> SimpleNamespace:
    result = SimpleNamespace(
        retcode=retcode, comment=comment, order=order, deal=deal, volume=volume, price=price
    )
    result._asdict = lambda: vars(result)
    return result


def make_position(
    *,
    ticket: int = 1000,
    symbol: str = "XAUUSD",
    type: int = 0,
    volume: float = 0.01,
    price_open: float = 100.0,
    sl: float = 0.0,
    tp: float = 0.0,
    magic: int = 1,
    comment: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        type=type,
        volume=volume,
        price_open=price_open,
        sl=sl,
        tp=tp,
        magic=magic,
        comment=comment,
    )


def make_deal(
    *,
    ticket: int,
    order: int,
    position_id: int,
    type: int,
    entry: int,
    symbol: str = "XAUUSD",
    volume: float = 0.01,
    price: float = 100.0,
    commission: float = 0.0,
    swap: float = 0.0,
    profit: float = 0.0,
    time: int = 1_700_000_000,
) -> SimpleNamespace:
    return SimpleNamespace(
        ticket=ticket,
        order=order,
        position_id=position_id,
        type=type,
        entry=entry,
        symbol=symbol,
        volume=volume,
        price=price,
        commission=commission,
        swap=swap,
        profit=profit,
        time=time,
    )
