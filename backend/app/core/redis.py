"""Redis client factory, lock helper, and TTL-enforcing state setter.

Every key written through here carries a TTL (SPEC-08 §4): a Redis key
without one is a memory leak and a stale-data hazard.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def make_redis_client(redis_url: str) -> Redis:
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    return client


async def set_with_ttl(redis: Redis, key: str, value: str, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        raise ValueError("every Redis key must carry a positive TTL")
    await redis.set(key, value, ex=ttl_seconds)


@asynccontextmanager
async def redis_lock(redis: Redis, key: str, *, ttl_seconds: int) -> AsyncIterator[bool]:
    """Best-effort distributed lock. Yields True if acquired, False otherwise.

    Released via a Lua compare-and-delete so a lock is never released by a
    holder that no longer owns it (e.g. after TTL expiry and reacquisition).
    """
    token = str(uuid.uuid4())
    acquired = bool(await redis.set(key, token, nx=True, ex=ttl_seconds))
    try:
        yield acquired
    finally:
        if acquired:
            await redis.eval(_RELEASE_LOCK_SCRIPT, 1, key, token)  # type: ignore[misc]
