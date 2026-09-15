"""SPEC-03 §2: refresh token issuance, lookup and rotation.

Rotation, not reuse: `POST /auth/refresh` always issues a brand new refresh
token and revokes the presented one, linking old -> new via `replaced_by`
(SPEC-03 §2's "old token revoked"). A refresh token is stored only as its
SHA-256 hash (`hash_refresh_token`, `app.core.security`) - the raw value is
returned to the caller exactly once, at issuance, the same "shown once"
pattern `AgentRepository.create()` already uses for agent credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import RefreshToken


@dataclass(frozen=True, slots=True)
class RefreshTokenRecord:
    id: UUID
    user_id: UUID
    expires_at: datetime
    revoked_at: datetime | None


def _to_record(row: RefreshToken) -> RefreshTokenRecord:
    return RefreshTokenRecord(
        id=row.id, user_id=row.user_id, expires_at=row.expires_at, revoked_at=row.revoked_at
    )


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
        user_agent: str | None,
        ip: str | None,
    ) -> UUID:
        row = RefreshToken(
            id=uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def get_valid_by_hash(
        self, token_hash: str, *, as_of: datetime
    ) -> RefreshTokenRecord | None:
        """`None` for a hash that doesn't exist, is already revoked, or has
        expired - callers don't need to distinguish which (all three mean
        "not usable"), matching the same fail-closed shape as an unknown
        credential."""
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        row = result.scalar_one_or_none()
        if row is None or row.revoked_at is not None or row.expires_at <= as_of:
            return None
        return _to_record(row)

    async def rotate(self, old_id: UUID, *, new_id: UUID, at: datetime) -> None:
        row = await self._session.get(RefreshToken, old_id)
        if row is None:
            raise LookupError(f"refresh token {old_id} not found")
        row.revoked_at = at
        row.replaced_by = new_id
        await self._session.flush()

    async def revoke_by_hash(self, token_hash: str, *, at: datetime) -> None:
        """Idempotent: revoking an already-revoked or unknown token is a
        no-op, not an error - a logout call should never fail just because
        the token was already gone."""
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        row = result.scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = at
            await self._session.flush()
