"""SPEC-04 §2: agent credentials are shown once at creation and never
persisted in plaintext - `AgentRepository` is what makes both halves of
that true against a real database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Agent
from app.repositories.agents import AgentRepository
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration

_KEY = "test-encryption-key-not-for-production"


async def test_create_returns_plaintext_credentials_once(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = AgentRepository(db_session, encryption_key=_KEY)
    creds = await repo.create(
        account_id=refs.account_id, name="win-vps-1", created_at=datetime.now(UTC)
    )

    assert len(creds.api_key) > 20
    assert len(creds.hmac_secret) == 32


async def test_get_by_api_key_recovers_the_original_hmac_secret(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = AgentRepository(db_session, encryption_key=_KEY)
    created = await repo.create(
        account_id=refs.account_id, name="win-vps-2", created_at=datetime.now(UTC)
    )
    await db_session.commit()

    looked_up = await repo.get_by_api_key(created.api_key)

    assert looked_up is not None
    assert looked_up.agent_id == created.agent_id
    assert looked_up.account_id == refs.account_id
    assert looked_up.hmac_secret == created.hmac_secret


async def test_get_by_api_key_returns_none_for_unknown_key(db_session: AsyncSession) -> None:
    repo = AgentRepository(db_session, encryption_key=_KEY)
    assert await repo.get_by_api_key("not-a-real-key") is None


async def test_touch_last_seen_updates_the_row(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    repo = AgentRepository(db_session, encryption_key=_KEY)
    created = await repo.create(
        account_id=refs.account_id, name="win-vps-3", created_at=datetime.now(UTC)
    )
    await db_session.commit()

    at = datetime.now(UTC)
    await repo.touch_last_seen(created.agent_id, at=at)
    await db_session.commit()

    result = await db_session.execute(select(Agent).where(Agent.id == created.agent_id))
    assert result.scalar_one().last_seen_at is not None
