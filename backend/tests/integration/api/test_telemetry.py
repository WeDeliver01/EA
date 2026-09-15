"""SPEC-03 §8 acceptance for `/telemetry/gate-rejections`."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app
from app.models.tables import AnalysisGate, AnalysisRun, User
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


async def _seed_wait_runs_with_gates(settings: Settings, refs: SeededRefs) -> None:
    """Two WAIT runs failing `CONFLUENCE_BELOW_THRESHOLD`, one also failing
    `SESSION_NOT_PERMITTED` - mirrors the SPEC-03 §8 example shape."""
    engine = create_async_engine(settings.database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            base_now = datetime.now(UTC)
            for i in range(2):
                run_id = uuid.uuid4()
                now = base_now - timedelta(minutes=i * 15)
                session.add(
                    AnalysisRun(
                        id=run_id,
                        account_id=refs.account_id,
                        instrument_id=refs.instrument_id,
                        strategy_version_id=refs.strategy_version_id,
                        timeframe="M15",
                        as_of=now,
                        mode="live",
                        regime="UNKNOWN",
                        outcome="WAIT",
                        confluence_score="2.0",
                        confluence_band="LOW",
                        narrative="",
                        engine_duration_ms=3,
                        market_snapshot={},
                        created_at=now,
                    )
                )
                await session.flush()
                session.add(
                    AnalysisGate(
                        analysis_run_id=run_id,
                        code="CONFLUENCE_BELOW_THRESHOLD",
                        passed=False,
                        detail={},
                    )
                )
                if i == 0:
                    session.add(
                        AnalysisGate(
                            analysis_run_id=run_id,
                            code="SESSION_NOT_PERMITTED",
                            passed=False,
                            detail={},
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


def test_gate_rejections_counts_by_code() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    asyncio.run(_seed_wait_runs_with_gates(settings, refs))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            "/api/v1/telemetry/gate-rejections",
            params={"account_id": str(refs.account_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    # seed_minimal_refs's own scaffolding TRADE row plus our 2 WAIT rows
    assert body["total_evaluations"] == 3
    assert body["trades"] == 1
    rejections = {r["code"]: r["count"] for r in body["rejections"]}
    assert rejections["CONFLUENCE_BELOW_THRESHOLD"] == 2
    assert rejections["SESSION_NOT_PERMITTED"] == 1


def test_gate_rejections_defaults_to_a_7_day_window() -> None:
    settings = _settings()
    refs = asyncio.run(_provision(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        token = _login(client)
        response = client.get(
            "/api/v1/telemetry/gate-rejections",
            params={"account_id": str(refs.account_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    period_start = datetime.fromisoformat(body["period"]["from"])
    period_end = datetime.fromisoformat(body["period"]["to"])
    assert (period_end - period_start) == timedelta(days=7)
