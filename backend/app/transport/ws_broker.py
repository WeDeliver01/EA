"""SPEC-04 §4: `WSAgentBroker` implements `app.execution.broker.AsyncBroker`
over a live WSS connection, via `AgentConnectionRegistry`. This is the real
broker Phase 5 promised - `dispatcher.py`/`position_manager.py`/
`reconciliation.py` need no changes to use it instead of
`AsyncSimulatedBrokerAdapter`; only which object gets constructed differs.

Wire-format note: this matches `agent/executor.py` and `agent/main.py` as
actually built and live-verified against a real MT5 terminal, not SPEC-04's
literal prose. Two real deviations: replies arrive as
`event.<command_name>_result` (e.g. `event.place_order_result`, not
`event.order_result`), and correlation is by `correlation_id` == the
original command envelope's `id` (SPEC-04 §3's own design, which
`agent/main.py`'s `on_command` now honours - see that commit). A command
that gets no reply within the timeout (agent gone, or a genuinely dropped
connection right after it accepted the order) returns `None` - the exact
`BrokerFault.SILENT` contract `SimulatedBroker` already models, which
`OutboxDispatcher` already knows how to turn into an `UNKNOWN` intent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.execution.intent import Fill, OrderIntent
from app.execution.broker import BrokerPositionSnapshot, ModifyResult, OrderResult
from app.transport.envelope import Envelope
from app.transport.registry import AgentConnectionRegistry
from app.transport.wire import parse_fill, parse_order_result, parse_position

DEFAULT_COMMAND_TIMEOUT_SECONDS = 10.0


class WSAgentBroker:
    def __init__(
        self,
        *,
        account_id: UUID,
        registry: AgentConnectionRegistry,
        timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._account_id = account_id
        self._registry = registry
        self._timeout_seconds = timeout_seconds

    async def _command(
        self, command_type: str, payload: dict[str, Any], *, envelope_id: str | None = None
    ) -> dict[str, Any] | None:
        envelope = Envelope(
            v=1,
            type=f"command.{command_type}",
            id=envelope_id or str(uuid.uuid4()),
            correlation_id=None,
            ts=datetime.now(tz=UTC).isoformat(),
            payload=payload,
        )
        return await self._registry.send_command(
            self._account_id, envelope, timeout_seconds=self._timeout_seconds
        )

    async def place_order(self, intent: OrderIntent, *, at: datetime) -> OrderResult | None:
        result = await self._command(
            "place_order",
            {
                "client_order_id": intent.client_order_id,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "order_type": intent.order_type.value,
                "volume": str(intent.volume),
                "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
                "stop_loss": str(intent.stop_loss),
                "take_profit": str(intent.take_profit) if intent.take_profit is not None else None,
                "max_slippage_points": intent.max_slippage_points,
                "magic": intent.magic,
                "comment": intent.comment,
            },
            # SPEC-04 §3: the envelope id is the idempotency key for a
            # place_order command, and equals client_order_id.
            envelope_id=intent.client_order_id,
        )
        if result is None or "error" in result:
            return None
        return parse_order_result(result)

    async def modify_position(
        self,
        broker_position_id: str,
        *,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> ModifyResult:
        result = await self._command(
            "modify_position",
            {
                "broker_position_id": broker_position_id,
                "stop_loss": str(stop_loss) if stop_loss is not None else None,
                "take_profit": str(take_profit) if take_profit is not None else None,
            },
        )
        if result is None:
            return ModifyResult(broker_position_id, retcode=-1, retcode_text="AGENT_UNREACHABLE")
        return ModifyResult(
            broker_position_id,
            retcode=int(result["retcode"]),
            retcode_text=str(result["retcode_text"]),
        )

    async def close_position(
        self,
        broker_position_id: str,
        *,
        price: Decimal,
        at: datetime,
        volume: Decimal | None = None,
    ) -> Fill | None:
        result = await self._command(
            "close_position",
            {
                "broker_position_id": broker_position_id,
                "max_slippage_points": 30,
                "volume": str(volume) if volume is not None else None,
            },
        )
        if result is None or "error" in result:
            return None
        return parse_fill(result)

    async def get_positions(self) -> tuple[BrokerPositionSnapshot, ...]:
        result = await self._command("get_positions", {})
        if result is None:
            return ()
        return tuple(parse_position(p) for p in result["positions"])

    async def get_deals(self, *, since: datetime | None = None) -> tuple[Fill, ...]:
        result = await self._command(
            "get_deals", {"from": since.isoformat() if since is not None else None}
        )
        if result is None:
            return ()
        return tuple(parse_fill(d) for d in result["deals"])
