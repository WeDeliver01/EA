"""Shared FastAPI dependencies for human-authenticated endpoints.

Deliberately separate from `app/api/v1/agent_ws.py`'s auth path - SPEC-03
§10: "Human JWTs are rejected on `/agent/*`. Agent keys are rejected
everywhere else. Enforce with two distinct dependency chains, not a shared
one with a role check." This module is that other chain.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.repositories.users import UserRecord, UserRepository

_bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserRecord:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")

    settings = request.app.state.settings
    try:
        claims = decode_access_token(credentials.credentials, settings.jwt_secret_key)
        user_id = UUID(claims.sub)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="invalid or expired token"
        ) from exc

    user = await UserRepository(session).get_by_id(user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user not found or inactive")
    return user


CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
