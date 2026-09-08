"""SPEC-04 §5: routes an unsolicited agent event (`correlation_id is None`
- not a reply to anything the backend asked for) to wherever it needs to
land. A reply to a backend-initiated command never reaches this - the WS
route resolves those directly via the registry, keyed by `correlation_id`,
before this is ever called.

Only `event.heartbeat` and `event.deal` are handled - the two SPEC-04 §5
events this MVP's execution path actually depends on (liveness, and the
"how did a position really close" source of truth). Everything else
(`event.position_snapshot` unsolicited, `event.quote`, `event.terminal_error`,
`event.agent_error`, etc.) is logged and dropped - a documented gap, not a
silent one (see docs/adr/0001-mvp-scope.md).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

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
) -> None:
    if envelope.type == "event.heartbeat":
        await _handle_heartbeat(
            envelope.payload, agent_id=agent_id, account_id=account_id, agent_repo=agent_repo
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
    payload: dict[str, Any], *, agent_id: UUID, account_id: UUID, agent_repo: AgentRepository
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
