"""SPEC-04 §8: asyncio entrypoint. Supervises the MT5 connection, deal
watcher, heartbeat, health endpoint and (once a backend exists to talk to)
the WSS transport, with graceful shutdown on SIGINT/SIGTERM.

The transport is wired but not started by default - `AGENT_BACKEND_WS_URL`
points nowhere reachable yet (see `agent/README.md`). Run with
`--no-transport` (the current default) to exercise the MT5-facing half
standalone; drop the flag once a backend is deployed.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import uuid

import structlog

from agent.config import Settings
from agent.executor import CommandExecutor
from agent.health import HealthServer
from agent.heartbeat import Heartbeat
from agent.mt5_client import MT5Client, MT5Credentials
from agent.store import AgentStore
from agent.transport import AgentTransport, Envelope
from agent.watcher import DealWatcher

logger = structlog.get_logger(__name__)


async def run(*, settings: Settings, enable_transport: bool) -> None:
    mt5_client = MT5Client(
        MT5Credentials(
            login=settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
            path=settings.mt5_path,
        )
    )
    await mt5_client.connect()
    logger.info("agent.mt5.connected")

    store = AgentStore(settings.state_db_path)
    executor = CommandExecutor(mt5_client=mt5_client, store=store)

    transport: AgentTransport | None = None

    async def emit_event(
        event_type: str, payload: dict, *, correlation_id: str | None = None
    ) -> None:
        if transport is None or not transport.connected:
            return
        envelope = Envelope(
            v=1,
            type=event_type,
            id=str(uuid.uuid4()),
            correlation_id=correlation_id,
            ts=payload.get("agent_time", ""),
            payload=payload,
        )
        await transport.send(envelope)

    heartbeat = Heartbeat(
        mt5_client=mt5_client,
        symbols=settings.symbols,
        emit=lambda payload: emit_event("event.heartbeat", payload),
        interval_ms=settings.heartbeat_interval_ms,
    )
    watcher = DealWatcher(
        mt5_client=mt5_client,
        store=store,
        emit_deal=lambda payload: emit_event("event.deal", payload),
        poll_interval_seconds=settings.deal_poll_interval_seconds,
        window_minutes=settings.deal_poll_window_minutes,
    )
    health = HealthServer(
        host=settings.health_host,
        port=settings.health_port,
        status=lambda: {"status": "ok", "mt5_connected": True},
    )

    async def on_command(envelope: Envelope) -> None:
        result = await executor.handle(envelope)
        await emit_event(
            f"event.{envelope.type.removeprefix('command.')}_result",
            result,
            correlation_id=envelope.id,
        )

    if enable_transport:
        transport = AgentTransport(
            ws_url=settings.backend_ws_url,
            api_key=settings.api_key,
            hmac_secret=settings.hmac_secret,
            on_command=on_command,
        )

    await health.start()
    logger.info("agent.health.started", host=settings.health_host, port=settings.health_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows' default (Proactor) event loop doesn't support
            # add_signal_handler; signal.signal() is the portable fallback.
            signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(stop.set))

    async with asyncio.TaskGroup() as tg:
        tg.create_task(heartbeat.run())
        tg.create_task(watcher.run())
        if transport is not None:
            tg.create_task(transport.run())
        tg.create_task(_wait_and_shutdown(stop, heartbeat, watcher, transport, health, mt5_client))


async def _wait_and_shutdown(
    stop: asyncio.Event,
    heartbeat: Heartbeat,
    watcher: DealWatcher,
    transport: AgentTransport | None,
    health: HealthServer,
    mt5_client: MT5Client,
) -> None:
    await stop.wait()
    logger.info("agent.shutdown.start")
    await heartbeat.stop()
    await watcher.stop()
    if transport is not None:
        await transport.stop()
    await health.stop()
    await mt5_client.shutdown()
    logger.info("agent.shutdown.complete")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--enable-transport",
        action="store_true",
        help="Connect to the backend over WSS. Off by default: no backend is deployed yet.",
    )
    args = parser.parse_args()

    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    settings = Settings()
    asyncio.run(run(settings=settings, enable_transport=args.enable_transport))


if __name__ == "__main__":
    main()
