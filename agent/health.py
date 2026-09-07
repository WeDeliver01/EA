"""SPEC-04 §8: local HTTP /health on 127.0.0.1:8799 for Uptime Kuma.

A hand-rolled asyncio TCP server rather than a web framework dependency -
this agent needs exactly one static-ish JSON response on one path, and
SPEC-10's working agreement requires an ADR for any new dependency.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import structlog

logger = structlog.get_logger(__name__)

HealthStatus = Callable[[], dict[str, object]]


class HealthServer:
    def __init__(self, *, host: str, port: int, status: HealthStatus) -> None:
        self._host = host
        self._port = port
        self._status = status
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readline(), timeout=5)  # discard the request line
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if not line or line in (b"\r\n", b"\n"):
                    break

            body = json.dumps(self._status()).encode()
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
