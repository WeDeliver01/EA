"""One-off CLI: provision real agent credentials for a Windows-side agent.

Prints `api_key`/`hmac_secret` exactly once, in plaintext - the same
"shown once" contract `AgentRepository.create()` documents. Nothing else
persists them outside the database (encrypted/hashed).

Usage (inside the running `api` container, which already has DATABASE_URL/
AGENT_SECRET_ENCRYPTION_KEY configured):

    docker compose -f infra/docker-compose.yml --env-file .env \\
        exec api python scripts/provision_agent.py \\
        --account-id <uuid> --name "windows-vps-01"
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.repositories.agents import AgentRepository


async def _main(account_id: UUID, name: str) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            creds = await AgentRepository(
                session, encryption_key=settings.agent_secret_encryption_key
            ).create(account_id=account_id, name=name, created_at=datetime.now(tz=UTC))
            await session.commit()
    finally:
        await engine.dispose()

    print("Agent provisioned - copy these into the Windows host's agent .env now,")
    print("they are never shown again:")
    print(f"  AGENT_ID={creds.agent_id}")
    print(f"  AGENT_API_KEY={creds.api_key}")
    print(f"  AGENT_HMAC_SECRET={creds.hmac_secret}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, type=UUID)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.account_id, args.name))
