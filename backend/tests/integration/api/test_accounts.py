"""SPEC-03 §3 acceptance for the account read endpoints - against the real
app (real Postgres) via Starlette's `TestClient`, same pattern as
`test_auth.py`/`test_agent_ws.py`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app
from app.models.tables import Account, PositionRow, User
from tests.integration.seed import SeededRefs, seed_minimal_refs

pytestmark = pytest.mark.integration

_JWT_SECRET = "test-jwt-secret-not-for-production"
_PASSWORD = "correct horse battery staple"


def _settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://dt:dt_dev_password@localhost:5432/delicate_trader"
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        environment="development",
        jwt_secret_key=_JWT_SECRET,
    )


async def _reset_database(engine) -> None:  # type: ignore[no-untyped-def]
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
    yield

    async def _cleanup() -> None:
        engine = create_async_engine(_settings().database_url)
        try:
            await _reset_database(engine)
        finally:
            await engine.dispose()

    asyncio.run(_cleanup())


async def _provision(settings: Settings) -> SeededRefs:
    """A real operator login (separate from `seed_minimal_refs`'s own
    placeholder `User` row, which has no real password hash) plus the
    account/instrument/risk-profile chain the endpoints under test read."""
    engine = create_async_engine(settings.database_url)
    try:
        await _reset_database(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            now = datetime.now(UTC)
            session.add(
                User(
                    id=uuid.uuid4(),
                    email="op@example.com",
                    password_hash=hash_password(_PASSWORD, settings),
                    display_name="Test Operator",
                    role="operator",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            refs = await seed_minimal_refs(session)
            account = await session.get(Account, refs.account_id)
            assert account is not None
            account.trading_enabled = True
            account.kill_switch_active = False
            await session.commit()
        return refs
    finally:
        await engine.dispose()


async def _open_position(settings: Settings, refs: SeededRefs, *, initial_risk: str) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            now = datetime.now(UTC)
            session.add(
                PositionRow(
                    id=uuid.uuid4(),
                    broker_position_id=f"pos-{uuid.uuid4().hex[:8]}",
                    account_id=refs.account_id,
                    instrument_id=refs.instrument_id,
                    direction="LONG",
                    status="OPEN",
                    volume="0.10",
                    initial_volume="0.10",
                    entry_price="3400.00",
                    initial_risk=initial_risk,
                    opened_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
    )
    assert response.status_code == 200
    token: str = response.json()["access_token"]
    return token


def test_list_accounts_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        response = client.get("/api/v1/accounts")
    assert response.status_code == 401


def test_list_accounts_returns_the_seeded_account() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(refs.account_id)
    assert body[0]["label"] == "Test Account"
    assert body[0]["trading_enabled"] is True
    assert body[0]["kill_switch_active"] is False


def test_get_account_detail_includes_agent_connected_false_when_no_agent() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/accounts/{refs.account_id}", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_connected"] is False
    assert body["leverage"] == 500


def test_get_account_detail_404_for_unknown_account() -> None:
    settings = _settings()
    asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/accounts/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 404


def test_get_account_state() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/accounts/{refs.account_id}/state",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["balance"] == "10000.0000"
    assert body["currency"] == "USD"


def test_get_risk_state() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/accounts/{refs.account_id}/risk-state",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["trading_enabled"] is True
    assert body["open_position_count"] == 0


def test_get_exposure_groups_open_risk_by_symbol() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    asyncio.run(_open_position(settings, refs, initial_risk="50.00"))
    asyncio.run(_open_position(settings, refs, initial_risk="30.00"))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/accounts/{refs.account_id}/exposure",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total_open_risk"] == "80.0000"
    assert len(body["by_symbol"]) == 1
    assert body["by_symbol"][0]["symbol"] == "XAUUSD"
    assert body["by_symbol"][0]["open_position_count"] == 2
