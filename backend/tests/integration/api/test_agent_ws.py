"""SPEC-04 §2 acceptance for the real WebSocket endpoint: the handshake
(accepted with a valid signature, rejected without one - bad signature,
stale timestamp, reused nonce, missing headers) and a live event round
trip once connected. Uses Starlette's `TestClient`, which runs the app's
real ASGI lifespan (real Postgres, real Redis) rather than mocking either.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.tables import Agent, AgentHeartbeat
from app.repositories.agents import AgentRepository, NewAgentCredentials
from tests.integration.seed import SeededRefs, seed_minimal_refs

pytestmark = pytest.mark.integration

_ENCRYPTION_KEY = "test-encryption-key-not-for-production"


def _settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://dt:dt_dev_password@localhost:5432/delicate_trader"
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        environment="development",
        agent_secret_encryption_key=_ENCRYPTION_KEY,
    )


async def _reset_database(engine) -> None:  # type: ignore[no-untyped-def]
    """These tests commit through a real WS connection (a separate thread's
    event loop), so there's no per-test transaction to roll back - mirrors
    `tests/integration/conftest.py`'s `db_session` fixture's own cleanup,
    run as setup instead since these tests don't use that fixture."""
    async with engine.begin() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename NOT IN "
                        "('alembic_version') AND tablename NOT LIKE 'market_bars_%' "
                        "AND tablename NOT LIKE 'market_ticks_%'"
                    )
                )
            )
            .scalars()
            .all()
        )
        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def _clean_database_after_each_test():  # type: ignore[no-untyped-def]
    """These tests don't use the `db_session` fixture (they need a real
    socket-shaped connection, via `TestClient`), so nothing else truncates
    the tables `seed_minimal_refs` writes fixed-identity rows into (notably
    `strategy_versions`) - without this, the last test here leaves rows
    behind that collide with the very next integration test file's own
    `seed_minimal_refs` call, anywhere in the suite."""
    yield

    async def _cleanup() -> None:
        engine = create_async_engine(_settings().database_url)
        try:
            await _reset_database(engine)
        finally:
            await engine.dispose()

    asyncio.run(_cleanup())


async def _provision_agent(settings: Settings) -> tuple[SeededRefs, NewAgentCredentials]:
    engine = create_async_engine(settings.database_url)
    try:
        await _reset_database(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            refs = await seed_minimal_refs(session)
            creds = await AgentRepository(session, encryption_key=_ENCRYPTION_KEY).create(
                account_id=refs.account_id,
                name=f"test-agent-{uuid4().hex[:8]}",
                created_at=datetime.now(UTC),
            )
            await session.commit()
        return refs, creds
    finally:
        await engine.dispose()


def _headers(
    *,
    api_key: str,
    hmac_secret: str,
    ts_millis: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Mirrors `agent/transport.py`'s `handshake_headers` exactly - this
    test is the other half of that wire contract."""
    ts = ts_millis if ts_millis is not None else int(time.time() * 1000)
    nonce = nonce if nonce is not None else secrets.token_hex(16)
    signature = hmac.new(
        hmac_secret.encode(), f"{api_key}.{ts}.{nonce}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Agent-Key": api_key,
        "X-Agent-Ts": str(ts),
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": signature,
    }


def test_handshake_succeeds_and_a_heartbeat_lands_in_the_database() -> None:
    settings = _settings()
    _refs, creds = asyncio.run(_provision_agent(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        headers = _headers(api_key=creds.api_key, hmac_secret=creds.hmac_secret)
        with client.websocket_connect("/api/v1/agent/ws", headers=headers) as ws:
            ws.send_json(
                {
                    "v": 1,
                    "type": "event.heartbeat",
                    "id": "evt-1",
                    "correlation_id": None,
                    "ts": "2026-09-07T09:14:22.318Z",
                    "payload": {
                        "agent_time": "2026-09-07T09:14:22.318000+00:00",
                        "broker_time": None,
                        "terminal_connected": True,
                        "trade_allowed": True,
                        "algo_trading_enabled": True,
                        "build": 4620,
                        "account": {},
                        "open_position_count": 0,
                        "open_position_hash": "",
                        "symbols": [],
                        "agent_version": "1.4.2",
                    },
                }
            )
            # The server processes the frame on its own task, concurrently
            # with this thread - there is no reply to wait on for a
            # heartbeat (SPEC-04 §5: fire-and-forget), so give it a bounded
            # window to land rather than assuming a happens-before
            # relationship the transport doesn't actually provide.
            heartbeat = asyncio.run(_wait_for_heartbeat(settings, creds.agent_id))

    assert heartbeat.terminal_connected is True

    async def _check_last_seen() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                agent_row = await session.get(Agent, creds.agent_id)
                assert agent_row is not None
                assert agent_row.last_seen_at is not None
        finally:
            await engine.dispose()

    asyncio.run(_check_last_seen())


async def _wait_for_heartbeat(
    settings: Settings, agent_id: UUID, *, timeout_seconds: float = 2.0
) -> AgentHeartbeat:
    engine = create_async_engine(settings.database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            async with session_factory() as session:
                result = await session.execute(
                    select(AgentHeartbeat).where(AgentHeartbeat.agent_id == agent_id)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    return row
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"no heartbeat recorded for agent {agent_id} within {timeout_seconds}s"
        )
    finally:
        await engine.dispose()


def test_handshake_rejects_a_bad_signature() -> None:
    settings = _settings()
    _refs, creds = asyncio.run(_provision_agent(settings))
    app = create_app(settings)

    headers = _headers(api_key=creds.api_key, hmac_secret="wrong-secret-entirely")
    with (
        TestClient(app) as client,
        pytest.raises(Exception),  # noqa: B017 - starlette raises on a closed handshake
        client.websocket_connect("/api/v1/agent/ws", headers=headers),
    ):
        pass


def test_handshake_rejects_an_unknown_api_key() -> None:
    settings = _settings()
    app = create_app(settings)

    headers = _headers(api_key="not-a-real-key", hmac_secret="whatever")
    with (
        TestClient(app) as client,
        pytest.raises(Exception),  # noqa: B017
        client.websocket_connect("/api/v1/agent/ws", headers=headers),
    ):
        pass


def test_handshake_rejects_a_stale_timestamp() -> None:
    settings = _settings()
    _refs, creds = asyncio.run(_provision_agent(settings))
    app = create_app(settings)

    stale_ms = int(time.time() * 1000) - 60_000  # 60s old, default max skew is 30s
    headers = _headers(api_key=creds.api_key, hmac_secret=creds.hmac_secret, ts_millis=stale_ms)
    with (
        TestClient(app) as client,
        pytest.raises(Exception),  # noqa: B017
        client.websocket_connect("/api/v1/agent/ws", headers=headers),
    ):
        pass


def test_handshake_rejects_a_reused_nonce() -> None:
    settings = _settings()
    _refs, creds = asyncio.run(_provision_agent(settings))
    app = create_app(settings)
    nonce = secrets.token_hex(16)

    with TestClient(app) as client:
        headers = _headers(api_key=creds.api_key, hmac_secret=creds.hmac_secret, nonce=nonce)
        with client.websocket_connect("/api/v1/agent/ws", headers=headers):
            pass  # first use succeeds and is accepted

        with (
            pytest.raises(Exception),  # noqa: B017
            client.websocket_connect(
                "/api/v1/agent/ws",
                headers=_headers(api_key=creds.api_key, hmac_secret=creds.hmac_secret, nonce=nonce),
            ),
        ):
            pass  # same nonce again must be rejected


def test_handshake_rejects_missing_headers() -> None:
    settings = _settings()
    app = create_app(settings)

    with (
        TestClient(app) as client,
        pytest.raises(Exception),  # noqa: B017
        client.websocket_connect("/api/v1/agent/ws"),
    ):
        pass
