"""Payload (de)serialisation shared between `ws_broker.py` (backend-
initiated commands) and `event_router.py` (unsolicited agent events) -
both parse the same `Fill`/`BrokerPositionSnapshot`/`OrderResult` shapes
`agent/executor.py`, `agent/watcher.py` and `agent/models.py` actually
produce.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.execution.enums import DealType, OrderSide
from app.domain.execution.intent import Fill
from app.execution.broker import BrokerPositionSnapshot, OrderResult


def decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def parse_order_result(payload: dict[str, Any]) -> OrderResult:
    return OrderResult(
        client_order_id=str(payload["client_order_id"]),
        retcode=int(payload["retcode"]),
        retcode_text=str(payload["retcode_text"]),
        broker_order_id=payload.get("broker_order_id"),
        broker_deal_id=payload.get("broker_deal_id"),
        broker_position_id=payload.get("broker_position_id"),
        filled_volume=Decimal(str(payload["filled_volume"])),
        fill_price=decimal_or_none(payload.get("fill_price")),
        requested_price=Decimal(str(payload["requested_price"])),
        slippage_points=int(payload["slippage_points"]),
        latency_ms=int(payload["latency_ms"]),
        raw=payload.get("raw") or {},
    )


def parse_fill(payload: dict[str, Any]) -> Fill:
    return Fill(
        broker_deal_id=str(payload["broker_deal_id"]),
        client_order_id=str(payload["client_order_id"]),
        broker_order_id=str(payload["broker_order_id"]),
        broker_position_id=str(payload["broker_position_id"]),
        symbol=str(payload["symbol"]),
        side=OrderSide(payload["side"]),
        volume=Decimal(str(payload["volume"])),
        price=Decimal(str(payload["price"])),
        commission=Decimal(str(payload["commission"])),
        swap=Decimal(str(payload["swap"])),
        profit=Decimal(str(payload["profit"])),
        executed_at=datetime.fromisoformat(str(payload["executed_at"])),
        deal_type=DealType(payload["deal_type"]),
    )


def parse_position(payload: dict[str, Any]) -> BrokerPositionSnapshot:
    return BrokerPositionSnapshot(
        broker_position_id=str(payload["broker_position_id"]),
        symbol=str(payload["symbol"]),
        side=OrderSide(payload["side"]),
        volume=Decimal(str(payload["volume"])),
        entry_price=Decimal(str(payload["entry_price"])),
        stop_loss=decimal_or_none(payload.get("stop_loss")),
        take_profit=decimal_or_none(payload.get("take_profit")),
        magic=int(payload["magic"]),
        comment=str(payload["comment"]),
    )
