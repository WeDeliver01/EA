"""SPEC-04 §2: agent credential provisioning and handshake lookup.

`api_key` and `hmac_secret` are generated here and returned exactly once,
in plaintext, at creation - the same "shown once" contract SPEC-04 §2
describes. Only `api_key_hash` (SHA-256, for exact-match lookup) and
`hmac_secret_enc` (Fernet, keyed from `AGENT_SECRET_ENCRYPTION_KEY`) are
ever persisted.

`hmac_secret` is a `str`, not raw bytes: the agent (`agent/config.py`)
reads it from `.env` as a string and does `hmac_secret.encode()` before
using it as the HMAC key (`agent/transport.py`'s `handshake_headers`) -
generating it as arbitrary random bytes here would produce a secret the
operator can't even paste into a `.env` file intact, let alone one that
round-trips through `str.encode()` back to the same bytes the backend
verifies against.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret, hash_api_key
from app.models.tables import Account, Agent, AgentHeartbeat


@dataclass(frozen=True, slots=True)
class NewAgentCredentials:
    agent_id: UUID
    api_key: str
    hmac_secret: str


@dataclass(frozen=True, slots=True)
class AgentCredentials:
    agent_id: UUID
    account_id: UUID
    hmac_secret: str


class AgentRepository:
    def __init__(self, session: AsyncSession, *, encryption_key: str) -> None:
        self._session = session
        self._encryption_key = encryption_key

    async def create(
        self,
        *,
        account_id: UUID,
        name: str,
        created_at: datetime,
        transport: str = "python_ws",
    ) -> NewAgentCredentials:
        api_key = secrets.token_urlsafe(32)
        hmac_secret = secrets.token_urlsafe(32)
        row = Agent(
            id=uuid4(),
            account_id=account_id,
            name=name,
            transport=transport,
            api_key_hash=hash_api_key(api_key),
            hmac_secret_enc=encrypt_secret(
                hmac_secret.encode(), encryption_key=self._encryption_key
            ),
            is_active=True,
            created_at=created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return NewAgentCredentials(agent_id=row.id, api_key=api_key, hmac_secret=hmac_secret)

    async def get_by_api_key(self, api_key: str) -> AgentCredentials | None:
        """Returns `None` for an unknown, inactive, or (as a side effect of
        `hash_api_key` being an exact match) tampered-with key - the caller
        can't distinguish "no such agent" from "disabled agent", by design:
        SPEC-04 §2's handshake gives an attacker no signal either way."""
        result = await self._session.execute(
            select(Agent).where(Agent.api_key_hash == hash_api_key(api_key))
        )
        row = result.scalar_one_or_none()
        if row is None or not row.is_active:
            return None
        return AgentCredentials(
            agent_id=row.id,
            account_id=row.account_id,
            hmac_secret=decrypt_secret(
                bytes(row.hmac_secret_enc), encryption_key=self._encryption_key
            ).decode(),
        )

    async def touch_last_seen(self, agent_id: UUID, *, at: datetime) -> None:
        row = await self._session.get(Agent, agent_id)
        if row is None:
            raise LookupError(f"agent {agent_id} not found")
        row.last_seen_at = at
        await self._session.flush()

    async def record_heartbeat(
        self,
        *,
        agent_id: UUID,
        account_id: UUID,
        received_at: datetime,
        agent_time: datetime,
        broker_time: datetime | None,
        terminal_connected: bool,
        trade_allowed: bool,
        balance: Decimal | None,
        equity: Decimal | None,
        open_position_count: int | None,
    ) -> None:
        self._session.add(
            AgentHeartbeat(
                agent_id=agent_id,
                account_id=account_id,
                received_at=received_at,
                agent_time=agent_time,
                broker_time=broker_time,
                terminal_connected=terminal_connected,
                trade_allowed=trade_allowed,
                balance=balance,
                equity=equity,
                open_position_count=open_position_count,
            )
        )
        await self._session.flush()
    async def record_heartbeat(
        self,
        *,
        agent_id: UUID,
        account_id: UUID,
        received_at: datetime,
        agent_time: datetime,
        broker_time: datetime | None,
        terminal_connected: bool,
        trade_allowed: bool,
        balance: Decimal | None,
        equity: Decimal | None,
        open_position_count: int | None,
    ) -> None:
        self._session.add(
            AgentHeartbeat(
                agent_id=agent_id,
                account_id=account_id,
                received_at=received_at,
                agent_time=agent_time,
                broker_time=broker_time,
                terminal_connected=terminal_connected,
                trade_allowed=trade_allowed,
                balance=balance,
                equity=equity,
                open_position_count=open_position_count,
            )
        )

        account = await self._session.get(Account, account_id)

        if account is not None:
            if balance is not None:
                account.balance = balance

            if equity is not None:
                account.equity = equity

            account.server_time = broker_time
            account.state_reported_at = received_at

        await self._session.flush()
