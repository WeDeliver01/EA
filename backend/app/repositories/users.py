"""SPEC-03 §2: human user lookups for authentication."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import User


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: UUID
    email: str
    password_hash: str
    display_name: str
    role: str
    totp_secret: str | None
    is_active: bool


def _to_record(row: User) -> UserRecord:
    return UserRecord(
        id=row.id,
        email=row.email,
        password_hash=row.password_hash,
        display_name=row.display_name,
        role=row.role,
        totp_secret=row.totp_secret,
        is_active=row.is_active,
    )


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> UserRecord | None:
        result = await self._session.execute(select(User).where(User.email == email))
        row = result.scalar_one_or_none()
        return _to_record(row) if row is not None else None

    async def get_by_id(self, user_id: UUID) -> UserRecord | None:
        row = await self._session.get(User, user_id)
        return _to_record(row) if row is not None else None
