"""SPEC-04 §2, §3, §7: the WSS transport - HMAC handshake, message envelope,
reconnection with backoff. This module is exercised in isolation (handshake
signing, envelope encode/decode, backoff schedule are all pure and unit
tested); the connect loop itself needs a real backend to run against, which
does not exist yet - see `agent/README.md`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import structlog
import websockets
from websockets.client import WebSocketClientProtocol

logger = structlog.get_logger(__name__)

PROTOCOL_VERSION = 1
BACKOFF_SCHEDULE_SECONDS = (1, 2, 4, 8, 16, 30)  # then holds at 30s, SPEC-04 §7.1


def handshake_headers(
    *, api_key: str, hmac_secret: str, now_ms: int | None = None
) -> dict[str, str]:
    """SPEC-04 §2. `hmac_secret` is the raw shared secret, not hex-encoded."""
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    nonce = secrets.token_hex(16)
    signature = hmac.new(
        hmac_secret.encode(), f"{api_key}.{ts}.{nonce}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Agent-Key": api_key,
        "X-Agent-Ts": str(ts),
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": signature,
    }


@dataclass(frozen=True, slots=True)
class Envelope:
    """SPEC-04 §3 message envelope."""

    v: int
    type: str
    id: str
    correlation_id: str | None
    ts: str
    payload: dict[str, Any]

    @classmethod
    def decode(cls, raw: str | bytes) -> Envelope:
        data = json.loads(raw)
        return cls(
            v=data["v"],
            type=data["type"],
            id=data["id"],
            correlation_id=data.get("correlation_id"),
            ts=data["ts"],
            payload=data.get("payload", {}),
        )

    def encode(self) -> str:
        return json.dumps(
            {
                "v": self.v,
                "type": self.type,
                "id": self.id,
                "correlation_id": self.correlation_id,
                "ts": self.ts,
                "payload": self.payload,
            }
        )


def backoff_delay(attempt: int) -> float:
    """`attempt` is 0-indexed. SPEC-04 §7.1: 1s, 2s, 4s, 8s, 16s, 30s, capped."""
    index = min(attempt, len(BACKOFF_SCHEDULE_SECONDS) - 1)
    return float(BACKOFF_SCHEDULE_SECONDS[index])


CommandHandler = Callable[[Envelope], Coroutine[Any, Any, None]]


class AgentTransport:
    """Owns the WSS connection lifecycle. Reconnects with backoff and jitter;
    the caller supplies `on_command` to react to inbound frames and reads
    `connected` to gate whether sending is meaningful right now."""

    def __init__(
        self, *, ws_url: str, api_key: str, hmac_secret: str, on_command: CommandHandler
    ) -> None:
        self._ws_url = ws_url
        self._api_key = api_key
        self._hmac_secret = hmac_secret
        self._on_command = on_command
        self._connection: WebSocketClientProtocol | None = None
        self.connected = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Runs until `stop()` is called, reconnecting on every failure."""
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._ws_url,
                    extra_headers=handshake_headers(
                        api_key=self._api_key, hmac_secret=self._hmac_secret
                    ),
                ) as ws:
                    self._connection = ws
                    self.connected = True
                    attempt = 0
                    logger.info("agent.transport.connected")
                    async for raw in ws:
                        await self._on_command(Envelope.decode(raw))
            except (websockets.ConnectionClosed, OSError) as exc:
                logger.warning("agent.transport.disconnected", error=str(exc), attempt=attempt)
            finally:
                self.connected = False
                self._connection = None

            if self._stop.is_set():
                break
            delay = backoff_delay(attempt) + secrets.randbelow(1000) / 1000
            attempt += 1
            await asyncio.wait([asyncio.create_task(self._stop.wait())], timeout=delay)

    async def send(self, envelope: Envelope) -> None:
        if self._connection is None:
            raise ConnectionError("not connected")
        await self._connection.send(envelope.encode())

    async def stop(self) -> None:
        self._stop.set()
        if self._connection is not None:
            await self._connection.close()
