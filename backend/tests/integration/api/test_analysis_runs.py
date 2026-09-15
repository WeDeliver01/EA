"""SPEC-03 §7 acceptance for `/analysis-runs` - the decision journal,
WAIT outcomes included."""

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
from app.models.tables import AnalysisRun, User
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
            await session.commit()
        return refs
    finally:
        await engine.dispose()


async def _seed_wait_run(settings: Settings, refs: SeededRefs) -> uuid.UUID:
    engine = create_async_engine(settings.database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            now = datetime.now(UTC)
            run_id = uuid.uuid4()
            session.add(
                AnalysisRun(
                    id=run_id,
                    account_id=refs.account_id,
                    instrument_id=refs.instrument_id,
                    strategy_version_id=refs.strategy_version_id,
                    timeframe="M15",
                    as_of=now,
                    mode="live",
                    regime="TRENDING_DOWN",
                    outcome="WAIT",
                    confluence_score="3.5",
                    confluence_band="LOW",
                    narrative="not enough confluence",
                    engine_duration_ms=4,
                    market_snapshot={},
                    created_at=now,
                )
            )
            await session.commit()
            return run_id
    finally:
        await engine.dispose()


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
    )
    assert response.status_code == 200
    token: str = response.json()["access_token"]
    return token


def test_list_analysis_runs_includes_wait_outcomes() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    asyncio.run(_seed_wait_run(settings, refs))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get("/api/v1/analysis-runs", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    # seed_minimal_refs itself seeds one TRADE row (mode="paper") plus our WAIT row
    outcomes = {r["outcome"] for r in body}
    assert "WAIT" in outcomes
    assert len(body) == 2


def test_list_analysis_runs_filters_by_outcome() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    asyncio.run(_seed_wait_run(settings, refs))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            "/api/v1/analysis-runs",
            params={"outcome": "WAIT"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["outcome"] == "WAIT"
    assert body[0]["symbol"] == "XAUUSD"


def test_get_analysis_run_detail_includes_evidence_and_gates() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/analysis-runs/{refs.analysis_run_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(refs.analysis_run_id)
    assert body["outcome"] == "TRADE"
    assert "evidence" in body
    assert "gates" in body
    assert "narrative" in body


def test_get_analysis_run_404_for_unknown_id() -> None:
    settings = _settings()
    asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            f"/api/v1/analysis-runs/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404
