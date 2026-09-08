"""Tracks live agent WebSocket connections - one per account, per SPEC-04
§10's "agent keys are scoped to a single account_id" - and the requests
each connection currently has in flight, correlated by envelope id.

`_SendableSocket` is a `Protocol` rather than `starlette.WebSocket`
directly, so this module (and `ws_broker.py`, which depends on it) has no
FastAPI/Starlette import - only the WS route in `app.api` touches those.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from app.transport.envelope import Envelope


class _SendableSocket(Protocol):
    async def send_text(self, data: str) -> None: ...


@dataclass(slots=True)
class _Connection:
    agent_id: UUID
    socket: _SendableSocket
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)


class AgentConnectionRegistry:
    """One process-local instance, held on `app.state` and shared by the WS
    route (registers/unregisters connections, resolves replies) and every
    `WSAgentBroker` (sends commands, awaits replies) for the process's
    lifetime. Not persisted - a restart drops all connections, which is
    fine: the agent reconnects (SPEC-04 §7) and outbox rows survive in
    Postgres regardless."""

    def __init__(self) -> None:
        self._by_account: dict[UUID, _Connection] = {}

    def register(self, *, account_id: UUID, agent_id: UUID, socket: _SendableSocket) -> None:
        self._by_account[account_id] = _Connection(agent_id=agent_id, socket=socket)

    def unregister(self, account_id: UUID) -> None:
        conn = self._by_account.pop(account_id, None)
        if conn is None:
            return
        for future in conn.pending.values():
            if not future.done():
                future.cancel()

    def is_connected(self, account_id: UUID) -> bool:
        return account_id in self._by_account

    async def send_command(
        self, account_id: UUID, envelope: Envelope, *, timeout_seconds: float
    ) -> dict[str, Any] | None:
        """Sends `envelope` and awaits the reply correlated to its `id`.
        Returns `None` if there's no connection, the agent never replies
        within `timeout_seconds`, or the connection drops mid-flight -
        exactly `SimulatedBroker`'s `BrokerFault.SILENT` contract, which
        `OutboxDispatcher`/`PositionManager` already know how to handle."""
        conn = self._by_account.get(account_id)
        if conn is None:
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        conn.pending[envelope.id] = future
        try:
            await conn.socket.send_text(envelope.encode())
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except (TimeoutError, asyncio.CancelledError, ConnectionError):
            return None
        finally:
            conn.pending.pop(envelope.id, None)

    def resolve(self, account_id: UUID, *, correlation_id: str, payload: dict[str, Any]) -> bool:
        """Called by the WS route for every inbound `event.*_result` frame.
        Returns whether anything was actually waiting on it - a `False` is
        not necessarily an error (a stale reply after a caller's own
        timeout already gave up), just informative for logging."""
        conn = self._by_account.get(account_id)
        if conn is None:
            return False
        future = conn.pending.get(correlation_id)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True
