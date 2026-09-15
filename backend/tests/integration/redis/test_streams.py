"""SPEC-08 §4 acceptance for the Redis Streams primitives: consumer-group
creation is idempotent, XREADGROUP only returns never-before-delivered
entries per group, and XACK removes an entry from the group's pending
list - the at-least-once, explicit-ack contract the trading-path workers
depend on."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis

from app.core.streams import ensure_consumer_group, xack, xadd, xreadgroup

pytestmark = pytest.mark.integration

_STREAM = "stream:test:widgets"
_GROUP = "test-group"


async def test_ensure_consumer_group_is_idempotent(redis_client: Redis) -> None:
    await ensure_consumer_group(redis_client, _STREAM, _GROUP)
    # A second call against the same stream/group must not raise BUSYGROUP.
    await ensure_consumer_group(redis_client, _STREAM, _GROUP)


async def test_xadd_then_xreadgroup_delivers_the_entry(redis_client: Redis) -> None:
    await ensure_consumer_group(redis_client, _STREAM, _GROUP)
    await xadd(redis_client, _STREAM, {"symbol": "XAUUSD", "timeframe": "M15"})

    entries = await xreadgroup(redis_client, _STREAM, _GROUP, "consumer-1", block_ms=100)

    assert len(entries) == 1
    entry_id, fields = entries[0]
    assert fields == {"symbol": "XAUUSD", "timeframe": "M15"}
    assert isinstance(entry_id, str)


async def test_same_entry_is_not_redelivered_to_a_different_consumer(
    redis_client: Redis,
) -> None:
    """Once delivered to any consumer in the group, an un-acked entry sits
    in that consumer's pending list - `">"` never hands it to a second
    consumer in the same group. This is the property that makes exactly
    one worker process each bar-close event even with several running."""
    await ensure_consumer_group(redis_client, _STREAM, _GROUP)
    await xadd(redis_client, _STREAM, {"n": "1"})

    first = await xreadgroup(redis_client, _STREAM, _GROUP, "consumer-1", block_ms=100)
    second = await xreadgroup(redis_client, _STREAM, _GROUP, "consumer-2", block_ms=100)

    assert len(first) == 1
    assert second == []


async def test_xack_clears_pending(redis_client: Redis) -> None:
    await ensure_consumer_group(redis_client, _STREAM, _GROUP)
    await xadd(redis_client, _STREAM, {"n": "1"})
    entries = await xreadgroup(redis_client, _STREAM, _GROUP, "consumer-1", block_ms=100)
    entry_id, _fields = entries[0]

    pending_before = await redis_client.xpending(_STREAM, _GROUP)
    assert pending_before["pending"] == 1

    await xack(redis_client, _STREAM, _GROUP, entry_id)

    pending_after = await redis_client.xpending(_STREAM, _GROUP)
    assert pending_after["pending"] == 0


async def test_xack_with_no_ids_is_a_noop(redis_client: Redis) -> None:
    await ensure_consumer_group(redis_client, _STREAM, _GROUP)
    await xack(redis_client, _STREAM, _GROUP)  # must not raise


async def test_xreadgroup_returns_empty_when_nothing_pending(redis_client: Redis) -> None:
    await ensure_consumer_group(redis_client, _STREAM, _GROUP)
    entries = await xreadgroup(redis_client, _STREAM, _GROUP, "consumer-1", block_ms=50)
    assert entries == []
