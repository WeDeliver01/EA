"""`AgentConnectionRegistry` is the send/correlate-reply mechanism every
`WSAgentBroker` call goes through - these tests exercise it directly,
with a fake socket standing in for a real WebSocket."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.transport.envelope import Envelope
from app.transport.registry import AgentConnectionRegistry

pytestmark = pytest.mark.unit


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


def _envelope(command_id: str = "cmd-1") -> Envelope:
    return Envelope(
        v=1,
        type="command.ping",
        id=command_id,
        correlation_id=None,
        ts="2026-01-01T00:00:00Z",
        payload={},
    )


async def test_is_connected_reflects_register_and_unregister() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    assert registry.is_connected(account_id) is False

    registry.register(account_id=account_id, agent_id=uuid.uuid4(), socket=FakeSocket())
    assert registry.is_connected(account_id) is True

    registry.unregister(account_id)
    assert registry.is_connected(account_id) is False


async def test_send_command_returns_none_when_no_agent_is_connected() -> None:
    registry = AgentConnectionRegistry()
    result = await registry.send_command(uuid.uuid4(), _envelope(), timeout_seconds=0.1)
    assert result is None


async def test_send_command_resolves_when_the_matching_reply_arrives() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    socket = FakeSocket()
    registry.register(account_id=account_id, agent_id=uuid.uuid4(), socket=socket)

    async def resolve_shortly() -> None:
        await asyncio.sleep(0)
        resolved = registry.resolve(account_id, correlation_id="cmd-1", payload={"pong": True})
        assert resolved is True

    task = asyncio.create_task(resolve_shortly())
    result = await registry.send_command(account_id, _envelope("cmd-1"), timeout_seconds=1.0)
    await task

    assert result == {"pong": True}
    assert len(socket.sent) == 1


async def test_send_command_times_out_when_no_reply_arrives() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    registry.register(account_id=account_id, agent_id=uuid.uuid4(), socket=FakeSocket())

    result = await registry.send_command(account_id, _envelope(), timeout_seconds=0.05)
    assert result is None


async def test_resolve_returns_false_for_unknown_account() -> None:
    registry = AgentConnectionRegistry()
    resolved = registry.resolve(uuid.uuid4(), correlation_id="anything", payload={})
    assert resolved is False


async def test_resolve_returns_false_for_unknown_correlation_id() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    registry.register(account_id=account_id, agent_id=uuid.uuid4(), socket=FakeSocket())
    resolved = registry.resolve(account_id, correlation_id="no-such-id", payload={})
    assert resolved is False


async def test_unregister_cancels_pending_requests() -> None:
    registry = AgentConnectionRegistry()
    account_id = uuid.uuid4()
    registry.register(account_id=account_id, agent_id=uuid.uuid4(), socket=FakeSocket())

    async def drop_connection_shortly() -> None:
        await asyncio.sleep(0)
        registry.unregister(account_id)

    task = asyncio.create_task(drop_connection_shortly())
    result = await registry.send_command(account_id, _envelope(), timeout_seconds=1.0)
    await task

    assert result is None
