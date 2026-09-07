"""SPEC-04: the execution agent protocol, stood in by an in-process fake.

Phase 5 (not built - see docs/adr/0001-mvp-scope.md) is the real agent: a
Windows process in `agent/` talking WSS to an actual MT5 terminal.
`SimulatedBroker` implements the same command/event surface (SPEC-04 §4-5)
so everything in `app/execution/` is the exact code that would run against
the real agent - only the transport differs when Phase 5 eventually swaps
this object for a WSS client. It is also the "fake agent that can be
instructed to misbehave" SPEC-06 §10 requires for chaos testing: every
fault it can inject is an explicit method, not a hidden branch, so a test
states its scenario in one line.

Single instrument per broker instance, matching "for v1, single instrument"
(SPEC-06 §4) - a second symbol needs a `SymbolSpec` per instrument, not per
broker, which is a straightforward extension nobody needs yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.domain.execution.enums import DealType, OrderSide
from app.domain.execution.intent import Fill, OrderIntent
from app.domain.market.symbol_spec import SymbolSpec

RETCODE_DONE = 10009
RETCODE_REJECT = 10006
RETCODE_INVALID_STOPS = 10016
RETCODE_NO_POSITION = 10017


class BrokerFault(Enum):
    """Instructable misbehaviour, keyed per `client_order_id`."""

    NONE = "none"
    SILENT = "silent"  # the order fills for real; the result never reaches us
    REJECT = "reject"
    PARTIAL_FILL = "partial_fill"


@dataclass(frozen=True, slots=True)
class OrderResult:
    client_order_id: str
    retcode: int
    retcode_text: str
    broker_order_id: str | None
    broker_deal_id: str | None
    broker_position_id: str | None
    filled_volume: Decimal
    fill_price: Decimal | None
    requested_price: Decimal
    slippage_points: int
    latency_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModifyResult:
    broker_position_id: str
    retcode: int
    retcode_text: str


@dataclass(frozen=True, slots=True)
class BrokerPositionSnapshot:
    broker_position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    magic: int
    comment: str


@dataclass(frozen=True, slots=True)
class _BrokerPosition:
    broker_position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    magic: int
    comment: str
    client_order_id: str


class SimulatedBroker:
    def __init__(self, *, spec: SymbolSpec, starting_ticket: int = 100_000) -> None:
        self._spec = spec
        self._next_ticket = starting_ticket
        self._results: dict[str, OrderResult | None] = {}
        self._positions: dict[str, _BrokerPosition] = {}
        self._deals: list[Fill] = []
        self._faults: dict[str, tuple[BrokerFault, dict[str, Any]]] = {}

    # -- test / operator control -------------------------------------------------

    def inject_fault(self, client_order_id: str, fault: BrokerFault, **params: Any) -> None:
        self._faults[client_order_id] = (fault, params)

    def simulate_manual_close(
        self, broker_position_id: str, *, price: Decimal, at: datetime
    ) -> Fill:
        """A human closes a position directly in the terminal: the deal
        exists at the broker with no corresponding local command."""
        position = self._positions.pop(broker_position_id)
        return self._record_deal(
            position, deal_type=DealType.EXIT, volume=position.volume, price=price, at=at
        )

    def simulate_manual_open(
        self, *, side: OrderSide, volume: Decimal, price: Decimal, at: datetime
    ) -> Fill:
        """A human places a trade the system never asked for. `magic=0`
        (SPEC-06 §7) is how the reconciler recognises it as manual, not a
        bug in attribution."""
        position = _BrokerPosition(
            broker_position_id=str(self._new_ticket()),
            symbol=self._spec.symbol,
            side=side,
            volume=volume,
            entry_price=price,
            stop_loss=None,
            take_profit=None,
            magic=0,
            comment="manual",
            client_order_id="",
        )
        self._positions[position.broker_position_id] = position
        return self._record_deal(
            position, deal_type=DealType.ENTRY, volume=volume, price=price, at=at
        )

    def simulate_broker_moves_stop(self, broker_position_id: str, *, new_stop: Decimal) -> None:
        position = self._positions[broker_position_id]
        self._positions[broker_position_id] = replace(position, stop_loss=new_stop)

    # -- SPEC-04 §4 commands -------------------------------------------------

    def place_order(self, intent: OrderIntent, *, at: datetime) -> OrderResult | None:
        """Returns `None` only for `BrokerFault.SILENT`: the order still
        fills at the broker (it shows up in `get_positions`/`get_deals`),
        the caller just never learns the outcome directly - exactly what
        "the agent drops the connection right after `place_order`" means."""
        if intent.client_order_id in self._results:
            return self._results[intent.client_order_id]  # idempotent replay, per client_order_id

        fault, params = self._faults.get(intent.client_order_id, (BrokerFault.NONE, {}))

        if fault == BrokerFault.REJECT:
            result = OrderResult(
                client_order_id=intent.client_order_id,
                retcode=int(params.get("retcode", RETCODE_REJECT)),
                retcode_text=str(params.get("retcode_text", "REJECTED")),
                broker_order_id=None,
                broker_deal_id=None,
                broker_position_id=None,
                filled_volume=Decimal(0),
                fill_price=None,
                requested_price=intent.limit_price or Decimal(0),
                slippage_points=0,
                latency_ms=15,
            )
            self._results[intent.client_order_id] = result
            return result

        fill_volume = intent.volume
        if fault == BrokerFault.PARTIAL_FILL:
            fraction = Decimal(str(params.get("fraction", "0.5")))
            fill_volume = intent.volume * fraction

        fill_price = intent.limit_price or Decimal("0")
        position = _BrokerPosition(
            broker_position_id=str(self._new_ticket()),
            symbol=intent.symbol,
            side=intent.side,
            volume=fill_volume,
            entry_price=fill_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            magic=intent.magic,
            comment=intent.comment,
            client_order_id=intent.client_order_id,
        )
        self._positions[position.broker_position_id] = position
        deal = self._record_deal(
            position, deal_type=DealType.ENTRY, volume=fill_volume, price=fill_price, at=at
        )

        result = OrderResult(
            client_order_id=intent.client_order_id,
            retcode=RETCODE_DONE,
            retcode_text="DONE",
            broker_order_id=position.broker_position_id,
            broker_deal_id=deal.broker_deal_id,
            broker_position_id=position.broker_position_id,
            filled_volume=fill_volume,
            fill_price=fill_price,
            requested_price=fill_price,
            slippage_points=0,
            latency_ms=15,
        )
        self._results[intent.client_order_id] = result
        if fault == BrokerFault.SILENT:
            return None
        return result

    def modify_position(
        self,
        broker_position_id: str,
        *,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> ModifyResult:
        position = self._positions.get(broker_position_id)
        if position is None:
            return ModifyResult(
                broker_position_id, retcode=RETCODE_NO_POSITION, retcode_text="NO_POSITION"
            )
        self._positions[broker_position_id] = replace(
            position,
            stop_loss=stop_loss if stop_loss is not None else position.stop_loss,
            take_profit=take_profit if take_profit is not None else position.take_profit,
        )
        return ModifyResult(broker_position_id, retcode=RETCODE_DONE, retcode_text="DONE")

    def close_position(
        self,
        broker_position_id: str,
        *,
        price: Decimal,
        at: datetime,
        volume: Decimal | None = None,
    ) -> Fill | None:
        position = self._positions.get(broker_position_id)
        if position is None:
            return None
        close_volume = position.volume if volume is None else min(volume, position.volume)
        deal_type = DealType.EXIT if close_volume >= position.volume else DealType.PARTIAL_EXIT
        deal = self._record_deal(
            position, deal_type=deal_type, volume=close_volume, price=price, at=at
        )

        remaining = position.volume - close_volume
        if remaining <= 0:
            del self._positions[broker_position_id]
        else:
            self._positions[broker_position_id] = replace(position, volume=remaining)
        return deal

    def get_positions(self) -> tuple[BrokerPositionSnapshot, ...]:
        return tuple(
            BrokerPositionSnapshot(
                broker_position_id=p.broker_position_id,
                symbol=p.symbol,
                side=p.side,
                volume=p.volume,
                entry_price=p.entry_price,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                magic=p.magic,
                comment=p.comment,
            )
            for p in self._positions.values()
        )

    def get_deals(self, *, since: datetime | None = None) -> tuple[Fill, ...]:
        if since is None:
            return tuple(self._deals)
        return tuple(d for d in self._deals if d.executed_at >= since)

    # -- internal -------------------------------------------------------------

    def _new_ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def _pnl(self, position: _BrokerPosition, *, exit_price: Decimal, volume: Decimal) -> Decimal:
        sign = Decimal(1) if position.side == OrderSide.BUY else Decimal(-1)
        ticks = (exit_price - position.entry_price) * sign / self._spec.tick_size
        return ticks * self._spec.tick_value * volume

    def _record_deal(
        self,
        position: _BrokerPosition,
        *,
        deal_type: DealType,
        volume: Decimal,
        price: Decimal,
        at: datetime,
    ) -> Fill:
        exit_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        deal = Fill(
            broker_deal_id=str(self._new_ticket()),
            client_order_id=position.client_order_id,
            broker_order_id=position.broker_position_id,
            broker_position_id=position.broker_position_id,
            symbol=position.symbol,
            side=position.side if deal_type == DealType.ENTRY else exit_side,
            volume=volume,
            price=price,
            commission=Decimal(0),
            swap=Decimal(0),
            profit=self._pnl(position, exit_price=price, volume=volume)
            if deal_type != DealType.ENTRY
            else Decimal(0),
            executed_at=at,
            deal_type=deal_type,
        )
        self._deals.append(deal)
        return deal
