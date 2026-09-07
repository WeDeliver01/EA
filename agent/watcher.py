"""SPEC-04 §5 "Deal polling": independently of any command, poll
`history_deals_get` on a rolling window and emit `event.deal` for anything
not seen before. This is how manual trades, broker-side stop-outs and
margin closures reach the backend."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from agent.models import Fill
from agent.mt5_client import MT5Client
from agent.store import AgentStore

logger = structlog.get_logger(__name__)

EmitDeal = Callable[[dict[str, Any]], Awaitable[None]]


def _jsonable_fill(fill: Fill) -> dict[str, Any]:
    out = asdict(fill)
    for key, value in out.items():
        if isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        elif hasattr(value, "value"):
            out[key] = value.value
    return out


class DealWatcher:
    def __init__(
        self,
        *,
        mt5_client: MT5Client,
        store: AgentStore,
        emit_deal: EmitDeal,
        poll_interval_seconds: float = 2.0,
        window_minutes: int = 10,
    ) -> None:
        self._mt5 = mt5_client
        self._store = store
        self._emit_deal = emit_deal
        self._poll_interval = poll_interval_seconds
        self._window = timedelta(minutes=window_minutes)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception:
                logger.exception("agent.watcher.poll_failed")
            await asyncio.wait(
                [asyncio.create_task(self._stop.wait())], timeout=self._poll_interval
            )

    async def stop(self) -> None:
        self._stop.set()

    async def _poll_once(self) -> None:
        since = datetime.now(tz=UTC) - self._window
        deals = await self._mt5.get_deals(since=since)
        for deal in deals:
            if await self._store.is_deal_seen(deal.broker_deal_id):
                continue
            await self._emit_deal(_jsonable_fill(deal))
            await self._store.mark_deal_seen(deal.broker_deal_id)
