"""SPEC-04 §5: routes an unsolicited agent event (`correlation_id is None`
- not a reply to anything the backend asked for) to wherever it needs to
land. A reply to a backend-initiated command never reaches this - the WS
route resolves those directly via the registry, keyed by `correlation_id`,
before this is ever called.

`event.heartbeat`, `event.deal` and `event.quote` are handled.
`event.heartbeat` also caches a live quote (`app.core.quotes`) for every
symbol in its own `symbols` field - `agent/heartbeat.py` already reports
`{symbol, bid, ask, time}` per watched symbol on every beat, so this is a
real live tick every `AGENT_HEARTBEAT_INTERVAL_MS` without needing the
agent to also emit a separate, throttled `event.quote` stream (SPEC-04 §5
still describes one; a standalone `event.quote` is handled the same way if
the agent ever sends one, but nothing currently does).

`event.position_snapshot` sent unsolicited (SPEC-04 §7.3's post-reconnect
push) is logged distinctly rather than acted on: reconciliation already
polls `broker.get_positions()` itself on its own interval
(`app.services.reconciliation_worker`), so an unsolicited snapshot would
be redundant with a poll that already happens - reconnection replay itself
(replaying the missed-event window) remains unbuilt on both sides, a
documented gap (see docs/adr/0001-mvp-scope.md).

Everything else (`event.terminal_error`, `event.agent_error`, etc.) is
logged and dropped - a documented gap, not a silent one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis

from app.core.quotes import store_quote
from app.execution.event_consumer import EventConsumer
from app.repositories.accounts import AccountRepository
from app.repositories.agents import AgentRepository
from app.transport.envelope import Envelope
from app.transport.wire import parse_fill

logger = structlog.get_logger(__name__)


async def route_unsolicited_event(
    envelope: Envelope,
    *,
    agent_id: UUID,
    account_id: UUID,
    agent_repo: AgentRepository,
    account_repo: AccountRepository,
    event_consumer: EventConsumer,
    redis: Redis,
) -> None:
    if envelope.type == "event.heartbeat":
        await _handle_heartbeat(
            envelope.payload,
            agent_id=agent_id,
            account_id=account_id,
            agent_repo=agent_repo,
            redis=redis,
        )
        return

    if envelope.type == "event.quote":
        await _cache_quote(envelope.payload, account_id=account_id, redis=redis)
        return

    if envelope.type == "event.position_snapshot":
        logger.info(
            "agent_ws.unsolicited_position_snapshot",
            agent_id=str(agent_id),
            account_id=str(account_id),
            position_count=len(envelope.payload.get("positions", [])),
        )
        return

    if envelope.type == "event.deal":
        # The account's schema carries no direct instrument reference
        # (SPEC-06 §4 "for v1, single instrument" is an operational
        # convention, not a stored one) - resolved from the deal's own
        # symbol instead, so this works even before any TradeIntent exists.
        instrument_id = await account_repo.get_instrument_id_by_symbol(
            account_id, str(envelope.payload["symbol"])
        )
        if instrument_id is None:
            logger.warning(
                "agent_ws.deal_for_unknown_instrument",
                symbol=envelope.payload.get("symbol"),
                account_id=str(account_id),
            )
            return
        fill = parse_fill(envelope.payload)
        await event_consumer.record_deal(
            fill, account_id=account_id, instrument_id=instrument_id, trade_intent_id=None
        )
        return

    logger.info(
        "agent_ws.unhandled_event",
        type=envelope.type,
        agent_id=str(agent_id),
        account_id=str(account_id),
    )


async def _handle_heartbeat(
    payload: dict[str, Any],
    *,
    agent_id: UUID,
    account_id: UUID,
    agent_repo: AgentRepository,
    redis: Redis,
) -> None:
    account = payload.get("account") or {}
    now = datetime.now(tz=UTC)
    await agent_repo.record_heartbeat(
        agent_id=agent_id,
        account_id=account_id,
        received_at=now,
        agent_time=datetime.fromisoformat(payload["agent_time"]),
        broker_time=(
            datetime.fromisoformat(payload["broker_time"]) if payload.get("broker_time") else None
        ),
        terminal_connected=bool(payload["terminal_connected"]),
        trade_allowed=bool(payload["trade_allowed"]),
        balance=Decimal(str(account["balance"])) if account.get("balance") is not None else None,
        equity=Decimal(str(account["equity"])) if account.get("equity") is not None else None,
        open_position_count=payload.get("open_position_count"),
    )
    await agent_repo.touch_last_seen(agent_id, at=now)

    for symbol_quote in payload.get("symbols") or ():
        await _cache_quote(symbol_quote, account_id=account_id, redis=redis, received_at=now)


async def _cache_quote(
    payload: dict[str, Any],
    *,
    account_id: UUID,
    redis: Redis,
    received_at: datetime | None = None,
) -> None:
    await store_quote(
        redis,
        account_id=account_id,
        symbol=str(payload["symbol"]),
        bid=Decimal(str(payload["bid"])),
        ask=Decimal(str(payload["ask"])),
        server_time=datetime.fromisoformat(str(payload["time"])),
        received_at=received_at or datetime.now(tz=UTC),
    )
