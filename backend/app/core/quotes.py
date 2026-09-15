"""Live quote cache backing the `PRICE_STALE` execution gate and a real live
spread for `SPREAD_TOO_WIDE` (`app.engines.gates.strategy_gates`) - Redis-
backed, not persisted to Postgres, since nothing downstream needs quote
history, only "what's the freshest tick, and how old is it."

Populated from `event.heartbeat`'s own `symbols` field (`app.transport.
event_router`), which already carries `{symbol, bid, ask, time}` per watched
symbol on every heartbeat (`agent/heartbeat.py`'s `_beat_once`) - a real live
tick every `AGENT_HEARTBEAT_INTERVAL_MS` (2s by default), comfortably inside
`QUOTE_STALE_SECONDS` (5s by default). SPEC-04 §5's separate, throttled
`event.quote` stream is handled too if the agent ever emits one standalone,
but isn't required for this cache to stay fresh.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from redis.asyncio import Redis

from app.core.redis import set_with_ttl

# Well beyond QUOTE_STALE_SECONDS (default 5s) - a missing key and a stale
# key both fail the PRICE_STALE gate the same way, so this only needs to
# avoid leaking keys for symbols nobody watches anymore.
_KEY_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class CachedQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    server_time: datetime
    received_at: datetime

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


def _key(account_id: UUID, symbol: str) -> str:
    return f"quote:{account_id}:{symbol}"


async def store_quote(
    redis: Redis,
    *,
    account_id: UUID,
    symbol: str,
    bid: Decimal,
    ask: Decimal,
    server_time: datetime,
    received_at: datetime,
) -> None:
    payload = json.dumps(
        {
            "symbol": symbol,
            "bid": str(bid),
            "ask": str(ask),
            "server_time": server_time.isoformat(),
            "received_at": received_at.isoformat(),
        }
    )
    await set_with_ttl(redis, _key(account_id, symbol), payload, _KEY_TTL_SECONDS)


async def get_quote(redis: Redis, *, account_id: UUID, symbol: str) -> CachedQuote | None:
    raw = await redis.get(_key(account_id, symbol))
    if raw is None:
        return None
    data = json.loads(raw)
    return CachedQuote(
        symbol=data["symbol"],
        bid=Decimal(data["bid"]),
        ask=Decimal(data["ask"]),
        server_time=datetime.fromisoformat(data["server_time"]),
        received_at=datetime.fromisoformat(data["received_at"]),
    )
