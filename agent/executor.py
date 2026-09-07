"""SPEC-04 §4: command handlers. Dispatches an inbound `Envelope` to
`MT5Client`, with the dedup and local-rejection behaviour §4 specifies.

No decisions live here - see SPEC-04 §8.5. This module executes what it is
told and reports facts; it does not decide whether a trade should happen.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from agent.models import OrderSide, OrderType, PlaceOrderCommand
from agent.mt5_client import MT5Client
from agent.store import AgentStore
from agent.transport import Envelope

logger = structlog.get_logger(__name__)


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):  # StrEnum
        return value.value
    return value


def _fill_result_dict(result: Any) -> dict[str, Any]:
    return {k: _to_jsonable(v) for k, v in asdict(result).items()}


class CommandExecutor:
    def __init__(self, *, mt5_client: MT5Client, store: AgentStore) -> None:
        self._mt5 = mt5_client
        self._store = store

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = getattr(
            self, f"_handle_{envelope.type.removeprefix('command.')}", None
        )
        if handler is None:
            logger.warning("agent.executor.unknown_command", type=envelope.type)
            return {"error": "UNKNOWN_COMMAND", "type": envelope.type}
        return await handler(envelope.payload)

    async def _handle_place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_order_id = payload["client_order_id"]

        # SPEC-04 §4 step 1: dedup check before touching the broker.
        cached = await self._store.get_order_result(client_order_id)
        if cached is not None:
            return cached

        cmd = PlaceOrderCommand(
            client_order_id=client_order_id,
            symbol=payload["symbol"],
            side=OrderSide(payload["side"]),
            order_type=OrderType(payload["order_type"]),
            volume=Decimal(str(payload["volume"])),
            limit_price=_decimal_or_none(payload.get("limit_price")),
            stop_loss=_decimal_or_none(payload.get("stop_loss")),
            take_profit=_decimal_or_none(payload.get("take_profit")),
            max_slippage_points=int(payload["max_slippage_points"]),
            magic=int(payload["magic"]),
            comment=payload["comment"],
        )
        result = await self._mt5.place_order(cmd)
        result_dict = _fill_result_dict(result)
        await self._store.record_order_result(client_order_id, result_dict)
        return result_dict

    async def _handle_modify_position(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self._mt5.modify_position(
            payload["broker_position_id"],
            stop_loss=_decimal_or_none(payload.get("stop_loss")),
            take_profit=_decimal_or_none(payload.get("take_profit")),
        )
        return _fill_result_dict(result)

    async def _handle_close_position(self, payload: dict[str, Any]) -> dict[str, Any]:
        fill = await self._mt5.close_position(
            payload["broker_position_id"],
            max_slippage_points=int(payload["max_slippage_points"]),
            volume=_decimal_or_none(payload.get("volume")),
        )
        return _fill_result_dict(fill) if fill is not None else {"error": "NO_POSITION"}

    async def _handle_get_positions(self, _payload: dict[str, Any]) -> dict[str, Any]:
        positions = await self._mt5.get_positions()
        return {"positions": [_fill_result_dict(p) for p in positions]}

    async def _handle_get_deals(self, payload: dict[str, Any]) -> dict[str, Any]:
        since = datetime.fromisoformat(payload["from"]) if payload.get("from") else None
        deals = await self._mt5.get_deals(since=since)
        return {"deals": [_fill_result_dict(d) for d in deals]}

    async def _handle_get_symbol_spec(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = await self._mt5.get_symbol_spec(payload["symbol"])
        return _fill_result_dict(spec)

    async def _handle_ping(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}
