"""SPEC-03 §2 acceptance for `/auth/*`, against the real app (real
Postgres) via Starlette's `TestClient`, the same pattern
`test_agent_ws.py` uses - not mocking the DB or the JWT/Argon2id crypto.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app
from app.models.tables import User

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
        jwt_access_ttl_seconds=900,
        jwt_refresh_ttl_days=30,
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


async def _provision_user(
    settings: Settings, *, email: str = "op@example.com", is_active: bool = True
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        await _reset_database(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            now = datetime.now(UTC)
            session.add(
                User(
                    id=uuid4(),
                    email=email,
                    password_hash=hash_password(_PASSWORD, settings),
                    display_name="Test Operator",
                    role="operator",
                    is_active=is_active,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


def test_login_with_correct_credentials_returns_tokens() -> None:
    settings = _settings()
    asyncio.run(_provision_user(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] == 900


def test_login_with_wrong_password_is_rejected() -> None:
    settings = _settings()
    asyncio.run(_provision_user(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "op@example.com", "password": "wrong"}
        )

    assert response.status_code == 401


def test_login_for_unknown_email_is_rejected_not_a_500() -> None:
    settings = _settings()
    asyncio.run(_provision_user(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "nobody@example.com", "password": _PASSWORD}
        )

    assert response.status_code == 401


def test_inactive_user_cannot_log_in() -> None:
    settings = _settings()
    asyncio.run(_provision_user(settings, is_active=False))
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
        )

    assert response.status_code == 401


def test_me_requires_a_bearer_token() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_me_returns_the_authenticated_user() -> None:
    settings = _settings()
    asyncio.run(_provision_user(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
        )
        access_token = login.json()["access_token"]

        response = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "op@example.com"
    assert body["role"] == "operator"


def test_refresh_rotates_the_token_and_the_old_one_stops_working() -> None:
    settings = _settings()
    asyncio.run(_provision_user(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
        )
        old_refresh = login.json()["refresh_token"]

        refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert refreshed.status_code == 200
        new_refresh = refreshed.json()["refresh_token"]
        assert new_refresh != old_refresh

        # the old refresh token was rotated away - reusing it must fail
        reused = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert reused.status_code == 401

        # the new one still works
        again = client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
        assert again.status_code == 200


def test_refresh_with_an_unknown_token_is_rejected() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert response.status_code == 401


def test_logout_revokes_the_refresh_token() -> None:
    settings = _settings()
    asyncio.run(_provision_user(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"email": "op@example.com", "password": _PASSWORD}
        )
        refresh_token = login.json()["refresh_token"]

        logout = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
        assert logout.status_code == 200
        assert logout.json() == {"revoked": True}

        reused = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert reused.status_code == 401
