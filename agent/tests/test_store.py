from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from agent.store import AgentStore


@pytest.fixture
async def store() -> AsyncGenerator[AgentStore, None]:
    s = AgentStore(":memory:")
    yield s
    await s.close()


class TestOrderDedup:
    async def test_unknown_order_returns_none(self, store: AgentStore) -> None:
        assert await store.get_order_result("nope") is None

    async def test_recorded_order_round_trips(self, store: AgentStore) -> None:
        await store.record_order_result("abc", {"retcode": 10009})
        assert await store.get_order_result("abc") == {"retcode": 10009}

    async def test_first_write_wins(self, store: AgentStore) -> None:
        """SPEC-04 §6: the effect on the broker is exactly once. A second
        write for the same client_order_id must never clobber the first,
        even though normal flow never triggers this (the executor checks
        `get_order_result` before calling this at all)."""
        await store.record_order_result("abc", {"retcode": 10009, "filled_volume": "0.02"})
        await store.record_order_result("abc", {"retcode": 99999})
        result = await store.get_order_result("abc")
        assert result is not None
        assert result["retcode"] == 10009


class TestOutboundQueue:
    async def test_enqueued_event_is_unacked(self, store: AgentStore) -> None:
        await store.enqueue_event("evt-1", "event.heartbeat", {"x": 1})
        unacked = await store.unacked_events()
        assert len(unacked) == 1
        assert unacked[0].event_id == "evt-1"

    async def test_duplicate_event_id_is_ignored(self, store: AgentStore) -> None:
        await store.enqueue_event("evt-1", "event.heartbeat", {"x": 1})
        await store.enqueue_event("evt-1", "event.heartbeat", {"x": 2})
        assert len(await store.unacked_events()) == 1

    async def test_acked_event_is_excluded(self, store: AgentStore) -> None:
        await store.enqueue_event("evt-1", "event.heartbeat", {"x": 1})
        await store.ack_event("evt-1")
        assert await store.unacked_events() == ()


class TestDealDedup:
    async def test_unseen_deal_is_not_seen(self, store: AgentStore) -> None:
        assert await store.is_deal_seen("d1") is False

    async def test_marked_deal_is_seen(self, store: AgentStore) -> None:
        await store.mark_deal_seen("d1")
        assert await store.is_deal_seen("d1") is True


class TestMeta:
    async def test_round_trip(self, store: AgentStore) -> None:
        assert await store.get_meta("last_deal_ticket") is None
        await store.set_meta("last_deal_ticket", "12345")
        assert await store.get_meta("last_deal_ticket") == "12345"
