"""One-off CLI: provision real agent credentials for a Windows-side agent.

Prints a complete, ready-to-paste `agent/.env` block exactly once, in
plaintext - the same "shown once" contract `AgentRepository.create()`
documents. Nothing else persists `api_key`/`hmac_secret` outside the
database (encrypted/hashed).

The printed variable names match `agent/config.py`'s `env_prefix="AGENT_"`
(so `agent_id` -> `AGENT_AGENT_ID`, not `AGENT_ID`) - pasting the block
as-is is what makes `agent/main.py` pick it up.

Usage (inside the running `api` container, which already has DATABASE_URL/
AGENT_SECRET_ENCRYPTION_KEY configured):

    docker compose -f infra/docker-compose.yml --env-file .env \\
        exec api python scripts/provision_agent.py \\
        --account-id <uuid> --name "windows-vps-01" \\
        --backend-ws-url ws://<vps-ip>:8000/api/v1/agent/ws
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.repositories.agents import AgentRepository


async def _main(account_id: UUID, name: str, backend_ws_url: str) -> None:
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

    print("Agent provisioned. Paste this block into agent/.env on the Windows host")
    print("exactly as-is - it is never shown again:")
    print()
    print(f"AGENT_AGENT_ID={creds.agent_id}")
    print(f"AGENT_ACCOUNT_ID={account_id}")
    print(f"AGENT_API_KEY={creds.api_key}")
    print(f"AGENT_HMAC_SECRET={creds.hmac_secret}")
    print(f"AGENT_BACKEND_WS_URL={backend_ws_url}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, type=UUID)
    parser.add_argument("--name", required=True)
    parser.add_argument("--backend-ws-url", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.account_id, args.name, args.backend_ws_url))
