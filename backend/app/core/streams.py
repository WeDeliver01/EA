"""SPEC-08 §4: Redis Streams primitives the trading-path workers share -
`stream:bars:closed`, `stream:intents:pending`, `stream:agent:events`.

At-least-once, explicit-ack delivery (SPEC-00's "Trading path workers"
decision): a worker XREADGROUPs, does its work, and only XACKs once the
effect is durably persisted (usually a DB commit) - a crash between those
two steps means the same entry is redelivered to the next XREADGROUP call
for that consumer group, not lost. Every stream name follows the
`stream:<domain>:<event>` convention this module's constants use.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import ResponseError

STREAM_BARS_CLOSED = "stream:bars:closed"
STREAM_INTENTS_PENDING = "stream:intents:pending"
STREAM_AGENT_EVENTS = "stream:agent:events"


async def ensure_consumer_group(redis: Redis, stream: str, group: str) -> None:
    """Creates the stream (if absent, via `mkstream`) and the consumer
    group (starting from the beginning, `id="0"`) if it doesn't already
    exist. Idempotent - safe to call on every worker startup, which is the
    only place it needs calling from."""
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def xadd(redis: Redis, stream: str, fields: dict[str, str]) -> str:
    entry_id = await redis.xadd(stream, fields)  # type: ignore[arg-type]
    return str(entry_id)


async def xreadgroup(
    redis: Redis,
    stream: str,
    group: str,
    consumer: str,
    *,
    count: int = 10,
    block_ms: int = 5000,
) -> list[tuple[str, dict[str, str]]]:
    """Reads only entries this consumer group has never delivered before
    (`">"`), blocking up to `block_ms` if none are pending yet. Returns a
    flat `(entry_id, fields)` list for the one stream asked for - callers
    `xack` each id only once its effect is durably persisted."""
    response = await redis.xreadgroup(
        group, consumer, streams={stream: ">"}, count=count, block=block_ms
    )
    if not response:
        return []
    _stream_name, entries = response[0]
    return [(str(entry_id), dict(fields)) for entry_id, fields in entries]


async def xack(redis: Redis, stream: str, group: str, *entry_ids: str) -> None:
    if entry_ids:
        await redis.xack(stream, group, *entry_ids)
