"""SPEC-03 §7 acceptance for `/signals`."""

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
from app.models.tables import User
from tests.integration.seed import SeededRefs, seed_minimal_refs, seed_signal

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


async def _provision(settings: Settings) -> tuple[SeededRefs, uuid.UUID]:
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
            signal_id = await seed_signal(
                session, refs, reference=f"SIG-API-{uuid.uuid4().hex[:8]}"
            )
            await session.commit()
        return refs, signal_id
    finally:
        await engine.dispose()


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
    )
    assert response.status_code == 200
    token: str = response.json()["access_token"]
    return token


def test_list_signals_returns_the_seeded_signal() -> None:
    settings = _settings()
    refs, signal_id = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get("/api/v1/signals", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(signal_id)
    assert body[0]["symbol"] == "XAUUSD"
    assert body[0]["direction"] == "LONG"


def test_list_signals_filters_by_direction() -> None:
    settings = _settings()
    asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            "/api/v1/signals",
            params={"direction": "SHORT"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == []


def test_get_signal_detail_with_no_linked_intent() -> None:
    settings = _settings()
    refs, signal_id = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/signals/{signal_id}", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(signal_id)
    assert len(body["take_profits"]) == 1
    assert body["trade_intent_id"] is None
    assert body["trade_intent_state"] is None


def test_get_signal_404_for_unknown_id() -> None:
    settings = _settings()
    asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/signals/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 404
