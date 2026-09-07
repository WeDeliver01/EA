"""SPEC-04 §8: thin wrapper over the `MetaTrader5` package.

The package is not thread-safe and not async. Every `mt5.*` call in this
module runs on one dedicated worker thread (a single-worker executor
already serialises calls; the lock is kept anyway per spec, and it also
protects the connect/reconnect sequence against concurrent callers).

Local, pre-broker rejections (steps 3-5 of SPEC-04 §4) use negative
`retcode` values so they are never confused with a real MT5 trade
retcode, which is always >= 10000.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - MetaTrader5 is Windows-only
    # Not testable on this platform beyond import: `MT5Client` methods that
    # touch a real terminal will simply fail with AttributeError if called
    # without either a real Windows MT5 package or a monkeypatched `mt5`
    # (see tests/fake_mt5.py, which is what every test uses instead).
    mt5 = None  # type: ignore[assignment]

from agent.models import (
    AccountSnapshot,
    BrokerPositionSnapshot,
    DealType,
    Fill,
    ModifyResult,
    OrderResult,
    OrderSide,
    OrderType,
    PlaceOrderCommand,
    SymbolSpec,
    TerminalHealth,
)

LOCAL_NOT_CONNECTED = -1
LOCAL_SYMBOL_UNAVAILABLE = -2
LOCAL_INVALID_VOLUME = -3
LOCAL_STOPS_LEVEL_VIOLATION = -4
LOCAL_UNSUPPORTED_ORDER_TYPE = -5

_SYMBOL_FILLING_FOK = 1  # SYMBOL_FILLING_MODE bitmask, not exposed as a constant
_SYMBOL_FILLING_IOC = 2  # by the MetaTrader5 package - values per MQL5 docs.

# MT5 order/deal/position type constants - stable per the MQL5 API and
# mirrored exactly in tests/fake_mt5.py. Hardcoded (rather than read off
# `mt5.*`) so this module is importable without the Windows-only
# MetaTrader5 package installed.
_MT5_ORDER_TYPE_BUY = 0
_MT5_ORDER_TYPE_SELL = 1
_MT5_DEAL_TYPE_BUY = 0
_MT5_DEAL_TYPE_SELL = 1
_MT5_POSITION_TYPE_BUY = 0
_MT5_POSITION_TYPE_SELL = 1

_SIDE_TO_MT5 = {OrderSide.BUY: _MT5_ORDER_TYPE_BUY, OrderSide.SELL: _MT5_ORDER_TYPE_SELL}
_MT5_DEAL_SIDE = {_MT5_DEAL_TYPE_BUY: OrderSide.BUY, _MT5_DEAL_TYPE_SELL: OrderSide.SELL}
_MT5_POSITION_SIDE = {
    _MT5_POSITION_TYPE_BUY: OrderSide.BUY,
    _MT5_POSITION_TYPE_SELL: OrderSide.SELL,
}


_T = TypeVar("_T")


class MT5Error(RuntimeError):
    """`mt5.initialize()` or a required read call failed."""


@dataclass(frozen=True, slots=True)
class MT5Credentials:
    login: int | None = None
    password: str | None = None
    server: str | None = None
    path: str | None = None


class MT5Client:
    def __init__(self, credentials: MT5Credentials | None = None) -> None:
        self._credentials = credentials or MT5Credentials()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._lock = asyncio.Lock()
        self._connected = False

    async def connect(self) -> None:
        async with self._lock:
            await self._run(self._sync_connect)
            self._connected = True

    async def shutdown(self) -> None:
        async with self._lock:
            if self._connected:
                await self._run(mt5.shutdown)
                self._connected = False
        self._executor.shutdown(wait=True)

    async def terminal_health(self) -> TerminalHealth:
        async with self._lock:
            return await self._run(self._sync_terminal_health)

    async def account_snapshot(self) -> AccountSnapshot:
        async with self._lock:
            return await self._run(self._sync_account_snapshot)

    async def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        async with self._lock:
            return await self._run(self._sync_symbol_spec, symbol)

    async def get_tick(self, symbol: str) -> tuple[Decimal, Decimal, datetime]:
        async with self._lock:
            return await self._run(self._sync_tick, symbol)

    async def place_order(self, cmd: PlaceOrderCommand) -> OrderResult:
        async with self._lock:
            return await self._run(self._sync_place_order, cmd)

    async def modify_position(
        self,
        broker_position_id: str,
        *,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> ModifyResult:
        async with self._lock:
            return await self._run(
                self._sync_modify_position, broker_position_id, stop_loss, take_profit
            )

    async def close_position(
        self,
        broker_position_id: str,
        *,
        max_slippage_points: int,
        volume: Decimal | None = None,
    ) -> Fill | None:
        async with self._lock:
            return await self._run(
                self._sync_close_position, broker_position_id, max_slippage_points, volume
            )

    async def get_positions(self) -> tuple[BrokerPositionSnapshot, ...]:
        async with self._lock:
            return await self._run(self._sync_get_positions)

    async def get_deals(self, *, since: datetime | None = None) -> tuple[Fill, ...]:
        async with self._lock:
            return await self._run(self._sync_get_deals, since)

    # -- executor plumbing ----------------------------------------------------

    async def _run(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    # -- sync implementations, run on the mt5 worker thread --------------------

    def _sync_connect(self) -> None:
        c = self._credentials
        kwargs: dict[str, Any] = {}
        if c.path:
            kwargs["path"] = c.path
        if c.login:
            kwargs["login"] = c.login
            kwargs["password"] = c.password
            kwargs["server"] = c.server
        if not mt5.initialize(**kwargs):
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.initialize() failed: [{code}] {desc}")

    def _sync_terminal_health(self) -> TerminalHealth:
        terminal = mt5.terminal_info()
        if terminal is None:
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.terminal_info() failed: [{code}] {desc}")
        return TerminalHealth(
            connected=bool(terminal.connected),
            trade_allowed=bool(terminal.trade_allowed),
            algo_trading_enabled=bool(terminal.trade_allowed),
            build=int(terminal.build),
            broker_time=self._server_time(),
        )

    def _server_time(self) -> datetime | None:
        offset = self._broker_offset()
        if offset == timedelta(0):
            return None
        return datetime.now(tz=UTC) + offset

    def _sync_account_snapshot(self) -> AccountSnapshot:
        account = mt5.account_info()
        if account is None:
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.account_info() failed: [{code}] {desc}")
        return AccountSnapshot(
            login=int(account.login),
            server=str(account.server),
            currency=str(account.currency),
            leverage=int(account.leverage),
            balance=Decimal(str(account.balance)),
            equity=Decimal(str(account.equity)),
            margin=Decimal(str(account.margin)),
            free_margin=Decimal(str(account.margin_free)),
            margin_level=Decimal(str(account.margin_level)),
            trade_allowed=bool(account.trade_allowed),
        )

    def _select_symbol(self, symbol: str) -> Any:
        info = mt5.symbol_info(symbol)
        if info is None or not info.visible:
            if not mt5.symbol_select(symbol, True):
                code, desc = mt5.last_error()
                raise MT5Error(f"mt5.symbol_select({symbol!r}) failed: [{code}] {desc}")
            info = mt5.symbol_info(symbol)
        if info is None:
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.symbol_info({symbol!r}) failed: [{code}] {desc}")
        return info

    def _sync_symbol_spec(self, symbol: str) -> SymbolSpec:
        info = self._select_symbol(symbol)
        return SymbolSpec(
            symbol=info.name,
            digits=info.digits,
            point=Decimal(str(info.point)),
            tick_size=Decimal(str(info.trade_tick_size)),
            tick_value=Decimal(str(info.trade_tick_value)),
            contract_size=Decimal(str(info.trade_contract_size)),
            volume_min=Decimal(str(info.volume_min)),
            volume_max=Decimal(str(info.volume_max)),
            volume_step=Decimal(str(info.volume_step)),
            stops_level_points=int(info.trade_stops_level),
            freeze_level_points=int(info.trade_freeze_level),
            margin_initial=Decimal(str(info.margin_initial)),
            currency_profit=info.currency_profit,
            currency_margin=info.currency_margin,
        )

    def _sync_tick(self, symbol: str) -> tuple[Decimal, Decimal, datetime]:
        self._select_symbol(symbol)
        # First tick after symbol_select can read back zeroed before the
        # terminal's feed catches up; give it a few short retries.
        tick = None
        for _ in range(10):
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and tick.time > 0 and (tick.bid > 0 or tick.ask > 0):
                break
            time.sleep(0.1)
        if tick is None or tick.time == 0:
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.symbol_info_tick({symbol!r}) returned no data: [{code}] {desc}")
        return (
            Decimal(str(tick.bid)),
            Decimal(str(tick.ask)),
            datetime.fromtimestamp(tick.time, tz=UTC),
        )

    def _resolve_filling_mode(self, filling_mode_bitmask: int) -> int:
        if filling_mode_bitmask & _SYMBOL_FILLING_FOK:
            return int(mt5.ORDER_FILLING_FOK)
        if filling_mode_bitmask & _SYMBOL_FILLING_IOC:
            return int(mt5.ORDER_FILLING_IOC)
        return int(mt5.ORDER_FILLING_RETURN)

    def _local_reject(
        self, cmd: PlaceOrderCommand, *, retcode: int, retcode_text: str
    ) -> OrderResult:
        return OrderResult(
            client_order_id=cmd.client_order_id,
            retcode=retcode,
            retcode_text=retcode_text,
            broker_order_id=None,
            broker_deal_id=None,
            broker_position_id=None,
            filled_volume=Decimal(0),
            fill_price=None,
            requested_price=cmd.limit_price or Decimal(0),
            slippage_points=0,
            latency_ms=0,
        )

    def _sync_place_order(self, cmd: PlaceOrderCommand) -> OrderResult:
        # SPEC-04 §4 steps 3-5: local pre-flight checks before the broker call.
        terminal = mt5.terminal_info()
        if terminal is None or not terminal.connected or not terminal.trade_allowed:
            return self._local_reject(
                cmd, retcode=LOCAL_NOT_CONNECTED, retcode_text="NOT_CONNECTED"
            )

        if cmd.order_type is not OrderType.MARKET:
            return self._local_reject(
                cmd,
                retcode=LOCAL_UNSUPPORTED_ORDER_TYPE,
                retcode_text=f"UNSUPPORTED_ORDER_TYPE:{cmd.order_type}",
            )

        try:
            info = self._select_symbol(cmd.symbol)
        except MT5Error:
            return self._local_reject(
                cmd, retcode=LOCAL_SYMBOL_UNAVAILABLE, retcode_text="SYMBOL_UNAVAILABLE"
            )

        volume_step = Decimal(str(info.volume_step))
        volume_min = Decimal(str(info.volume_min))
        volume_max = Decimal(str(info.volume_max))
        remainder = (cmd.volume / volume_step) % 1
        if cmd.volume < volume_min or cmd.volume > volume_max or remainder != 0:
            return self._local_reject(
                cmd, retcode=LOCAL_INVALID_VOLUME, retcode_text="INVALID_VOLUME"
            )

        tick = mt5.symbol_info_tick(cmd.symbol)
        if tick is None:
            return self._local_reject(cmd, retcode=LOCAL_SYMBOL_UNAVAILABLE, retcode_text="NO_TICK")
        price = tick.ask if cmd.side is OrderSide.BUY else tick.bid

        stops_level = int(info.trade_stops_level) * Decimal(str(info.point))
        if cmd.stop_loss is not None and stops_level > 0:
            distance = abs(price - float(cmd.stop_loss))
            if Decimal(str(distance)) < stops_level:
                return self._local_reject(
                    cmd,
                    retcode=LOCAL_STOPS_LEVEL_VIOLATION,
                    retcode_text=f"STOPS_LEVEL_VIOLATION:{info.trade_stops_level}",
                )

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": cmd.symbol,
            "volume": float(cmd.volume),
            "type": _SIDE_TO_MT5[cmd.side],
            "price": price,
            "deviation": cmd.max_slippage_points,
            "magic": cmd.magic,
            "comment": cmd.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._resolve_filling_mode(int(info.filling_mode)),
        }
        if cmd.stop_loss is not None:
            request["sl"] = float(cmd.stop_loss)
        if cmd.take_profit is not None:
            request["tp"] = float(cmd.take_profit)

        started = time.monotonic()
        result = mt5.order_send(request)
        latency_ms = int((time.monotonic() - started) * 1000)

        if result is None:
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.order_send() returned None: [{code}] {desc}")

        raw = result._asdict() if hasattr(result, "_asdict") else {}
        done = result.retcode == mt5.TRADE_RETCODE_DONE
        deal_id, fill_price = (result.deal, result.price) if done else (0, 0.0)
        if done and (deal_id == 0 or fill_price == 0.0):
            # Some execution modes (e.g. "market execution") return the
            # synchronous result before the deal record is queryable and
            # with price/deal left at zero. The deal itself is real; look
            # it up. Never invent a price - the deal record is the truth.
            deal_id, fill_price = self._find_deal_by_order(result.order, result.order)

        return OrderResult(
            client_order_id=cmd.client_order_id,
            retcode=int(result.retcode),
            retcode_text=str(result.comment),
            broker_order_id=str(result.order) if result.order else None,
            broker_deal_id=str(deal_id) if deal_id else None,
            broker_position_id=str(result.order) if done else None,
            filled_volume=Decimal(str(result.volume)) if done else Decimal(0),
            fill_price=Decimal(str(fill_price)) if done and fill_price else None,
            requested_price=Decimal(str(price)),
            slippage_points=0,
            latency_ms=latency_ms,
            raw=raw,
        )

    def _find_deal_by_order(
        self, order_ticket: int, position_id: int, attempts: int = 10
    ) -> tuple[int, float]:
        """`history_deals_get` has no `order=` filter; filter by `position=`
        (always populated - it equals the order ticket for a fresh entry)
        and match the specific order client-side."""
        for _ in range(attempts):
            deals = mt5.history_deals_get(position=position_id)
            if deals:
                for d in deals:
                    if d.order == order_ticket:
                        return d.ticket, d.price
            time.sleep(0.1)
        return 0, 0.0

    def _broker_offset(self) -> timedelta:
        """`broker_time - agent_time`, per SPEC-04 §8.4. `history_deals_get`'s
        date bounds are interpreted as naive broker-server time, not UTC and
        not agent-local time - queried without this offset, a broker running
        ahead of the agent silently excludes the most recent deals."""
        symbols = [s for s in mt5.symbols_get() or () if s.visible] or mt5.symbols_get() or ()
        for s in symbols:
            tick = mt5.symbol_info_tick(s.name)
            if tick is not None and tick.time > 0:
                broker_now = datetime.fromtimestamp(tick.time, tz=UTC)
                return broker_now - datetime.now(tz=UTC)
        return timedelta(0)

    def _get_position(self, ticket: int, attempts: int = 10) -> Any:
        """`positions_get` briefly returns empty right after another order on
        the same ticket settles (the terminal's local table is mid-update).
        Retry a handful of times before concluding the position is gone."""
        for i in range(attempts):
            positions = mt5.positions_get(ticket=ticket)
            if positions:
                return positions[0]
            if i < attempts - 1:
                time.sleep(0.1)
        return None

    def _sync_modify_position(
        self,
        broker_position_id: str,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
    ) -> ModifyResult:
        position = self._get_position(int(broker_position_id))
        if position is None:
            return ModifyResult(broker_position_id, retcode=10036, retcode_text="NO_POSITION")
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": position.ticket,
            "sl": float(stop_loss) if stop_loss is not None else position.sl,
            "tp": float(take_profit) if take_profit is not None else position.tp,
        }
        result = mt5.order_send(request)
        if result is None:
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.order_send() (modify) returned None: [{code}] {desc}")
        return ModifyResult(
            broker_position_id, retcode=int(result.retcode), retcode_text=str(result.comment)
        )

    def _sync_close_position(
        self,
        broker_position_id: str,
        max_slippage_points: int,
        volume: Decimal | None,
    ) -> Fill | None:
        position = self._get_position(int(broker_position_id))
        if position is None:
            return None
        close_volume = float(volume) if volume is not None else position.volume
        close_volume = min(close_volume, position.volume)

        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            code, desc = mt5.last_error()
            raise MT5Error(f"mt5.symbol_info_tick({position.symbol!r}) failed: [{code}] {desc}")

        is_buy = position.type == mt5.POSITION_TYPE_BUY
        close_side_mt5 = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        price = tick.bid if is_buy else tick.ask

        info = mt5.symbol_info(position.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": close_volume,
            "type": close_side_mt5,
            "position": position.ticket,
            "price": price,
            "deviation": max_slippage_points,
            "magic": position.magic,
            "comment": f"close:{position.ticket}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": (
                self._resolve_filling_mode(int(info.filling_mode))
                if info
                else mt5.ORDER_FILLING_IOC
            ),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return None

        deal_id, fill_price = result.deal, result.price
        if deal_id == 0 or fill_price == 0.0:
            deal_id, fill_price = self._find_deal_by_order(result.order, position.ticket)

        deal_type = DealType.EXIT if close_volume >= position.volume else DealType.PARTIAL_EXIT
        return Fill(
            broker_deal_id=str(deal_id),
            client_order_id="",
            broker_order_id=str(result.order),
            broker_position_id=broker_position_id,
            symbol=position.symbol,
            side=OrderSide.SELL if is_buy else OrderSide.BUY,
            volume=Decimal(str(result.volume)),
            price=Decimal(str(fill_price)),
            commission=Decimal(0),
            swap=Decimal(0),
            profit=Decimal(0),
            executed_at=datetime.now(tz=UTC),
            deal_type=deal_type,
        )

    def _sync_get_positions(self) -> tuple[BrokerPositionSnapshot, ...]:
        positions = mt5.positions_get()
        if positions is None:
            return ()
        return tuple(
            BrokerPositionSnapshot(
                broker_position_id=str(p.ticket),
                symbol=p.symbol,
                side=_MT5_POSITION_SIDE[p.type],
                volume=Decimal(str(p.volume)),
                entry_price=Decimal(str(p.price_open)),
                stop_loss=Decimal(str(p.sl)) if p.sl else None,
                take_profit=Decimal(str(p.tp)) if p.tp else None,
                magic=int(p.magic),
                comment=str(p.comment),
            )
            for p in positions
            if p.type in _MT5_POSITION_SIDE
        )

    def _sync_get_deals(self, since: datetime | None) -> tuple[Fill, ...]:
        # `history_deals_get`'s bounds are naive broker-server time, not UTC
        # (see `_broker_offset`). `since` and the returned `executed_at` are
        # both true UTC - the only place that conversion happens.
        offset = self._broker_offset()
        # mt5.history_deals_get raises OSError on dates near the 1970 epoch
        # (the underlying CRT call rejects them on Windows); there is no
        # legitimate reason for the agent to ask further back than this.
        since_utc = since or (datetime.now(tz=UTC) - timedelta(days=90))
        date_from = (since_utc + offset).replace(tzinfo=None)
        date_to = (datetime.now(tz=UTC) + offset).replace(tzinfo=None)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return ()
        out: list[Fill] = []
        for d in deals:
            if d.type not in _MT5_DEAL_SIDE:
                continue  # skip balance/credit/correction rows: not a trade deal
            deal_type = DealType.ENTRY if d.entry == mt5.DEAL_ENTRY_IN else DealType.EXIT
            out.append(
                Fill(
                    broker_deal_id=str(d.ticket),
                    client_order_id="",
                    broker_order_id=str(d.order),
                    broker_position_id=str(d.position_id),
                    symbol=d.symbol,
                    side=_MT5_DEAL_SIDE[d.type],
                    volume=Decimal(str(d.volume)),
                    price=Decimal(str(d.price)),
                    commission=Decimal(str(d.commission)),
                    swap=Decimal(str(d.swap)),
                    profit=Decimal(str(d.profit)),
                    executed_at=datetime.fromtimestamp(d.time, tz=UTC) - offset,
                    deal_type=deal_type,
                )
            )
        return tuple(out)
