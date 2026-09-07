"""SPEC-10 Phase 0 acceptance: `/system/ready` returns 200 once DB and Redis
are reachable. Exercised here as an ASGI request rather than a live socket
(equivalent to what `make up` proves against a running container)."""

from __future__ import annotations

import os

import httpx
import pytest
from httpx import ASGITransport

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://dt:dt_dev_password@localhost:5432/delicate_trader"
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        environment="development",
    )


async def test_health_is_always_200() -> None:
    app = create_app(_settings())
    transport = ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        response = await client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_reports_database_and_redis_ok() -> None:
    app = create_app(_settings())
    transport = ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        response = await client.get("/api/v1/system/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


async def test_status_reports_global_trading_disabled_by_default() -> None:
    app = create_app(_settings())
    transport = ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        response = await client.get("/api/v1/system/status")
    assert response.status_code == 200
    assert response.json()["global_trading_enabled"] is False


async def test_correlation_id_is_echoed_on_every_response() -> None:
    app = create_app(_settings())
    transport = ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        response = await client.get(
            "/api/v1/system/health", headers={"X-Correlation-Id": "test-correlation-id"}
        )
    assert response.headers["X-Correlation-Id"] == "test-correlation-id"
