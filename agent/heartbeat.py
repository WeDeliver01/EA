"""SPEC-04 §5 `event.heartbeat`, every `heartbeat_interval_ms`. Carries
`open_position_hash`, the cheap reconciliation primitive that lets the
backend detect drift within two seconds instead of waiting sixty."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from agent.mt5_client import MT5Client

logger = structlog.get_logger(__name__)

EmitHeartbeat = Callable[[dict[str, Any]], Awaitable[None]]

AGENT_VERSION = "0.1.0"


def open_position_hash(positions: tuple) -> str:
    """sha256 of sorted (position_id, volume, sl, tp), SPEC-04 §5."""
    rows = sorted(
        f"{p.broker_position_id}:{p.volume}:{p.stop_loss}:{p.take_profit}" for p in positions
    )
    return hashlib.sha256("|".join(rows).encode()).hexdigest()


class Heartbeat:
    def __init__(
        self,
        *,
        mt5_client: MT5Client,
        symbols: list[str],
        emit: EmitHeartbeat,
        interval_ms: int = 2000,
    ) -> None:
        self._mt5 = mt5_client
        self._symbols = symbols
        self._emit = emit
        self._interval = interval_ms / 1000
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._beat_once()
            except Exception:
                logger.exception("agent.heartbeat.failed")
            await asyncio.wait([asyncio.create_task(self._stop.wait())], timeout=self._interval)

    async def stop(self) -> None:
        self._stop.set()

    async def _beat_once(self) -> None:
        health = await self._mt5.terminal_health()
        account = await self._mt5.account_snapshot()
        positions = await self._mt5.get_positions()

        symbol_quotes = []
        for symbol in self._symbols:
            try:
                bid, ask, tick_time = await self._mt5.get_tick(symbol)
            except Exception:
                continue
            symbol_quotes.append(
                {"symbol": symbol, "bid": str(bid), "ask": str(ask), "time": tick_time.isoformat()}
            )

        payload = {
            "agent_time": datetime.now(tz=UTC).isoformat(),
            "broker_time": health.broker_time.isoformat() if health.broker_time else None,
            "terminal_connected": health.connected,
            "trade_allowed": health.trade_allowed,
            "algo_trading_enabled": health.algo_trading_enabled,
            "build": health.build,
            "account": {
                "login": str(account.login),
                "currency": account.currency,
                "leverage": account.leverage,
                "balance": str(account.balance),
                "equity": str(account.equity),
                "margin": str(account.margin),
                "free_margin": str(account.free_margin),
                "margin_level": str(account.margin_level),
            },
            "open_position_count": len(positions),
            "open_position_hash": open_position_hash(positions),
            "symbols": symbol_quotes,
            "agent_version": AGENT_VERSION,
        }
        await self._emit(payload)
