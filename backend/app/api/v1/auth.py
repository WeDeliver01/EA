"""SPEC-03 §2: human authentication.

TOTP is deferred: SPEC-03 §2 makes it mandatory only for the `operator`
role on a `live` account, and only for the trading-control endpoints
(SPEC-03 §4), which aren't built yet. `LoginRequest.totp` is accepted for
wire-compatibility with the spec but currently ignored - a documented gap,
not a silent one, closed when the control endpoints are built.
"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.v1.deps import CurrentUser, DbSession
from app.core.config import Settings
from app.core.security import encode_access_token, hash_refresh_token, verify_password
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRecord, UserRepository

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str
    totp: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


def _client_ip(request: Request) -> str | None:
    """`refresh_tokens.ip` is a native `INET` column - only a real,
    parseable address is worth storing (a proxied or test-client host like
    `"testclient"` isn't one); this is an audit field, not something worth
    failing the whole login over."""
    if request.client is None:
        return None
    try:
        ipaddress.ip_address(request.client.host)
    except ValueError:
        return None
    return request.client.host


async def _issue_tokens(
    user: UserRecord, *, session: DbSession, settings: Settings, now: datetime, request: Request
) -> tuple[TokenResponse, uuid.UUID]:
    access_token = encode_access_token(
        subject=str(user.id),
        role=user.role,
        jti=str(uuid.uuid4()),
        issued_at=int(now.timestamp()),
        ttl_seconds=settings.jwt_access_ttl_seconds,
        secret_key=settings.jwt_secret_key,
    )
    raw_refresh = secrets.token_urlsafe(48)
    new_id = await RefreshTokenRepository(session).create(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        issued_at=now,
        expires_at=now + timedelta(days=settings.jwt_refresh_ttl_days),
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    response = TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.jwt_access_ttl_seconds,
    )
    return response, new_id


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, session: DbSession) -> TokenResponse:
    settings = request.app.state.settings
    user = await UserRepository(session).get_by_email(body.email)
    if (
        user is None
        or not user.is_active
        or not verify_password(body.password, user.password_hash, settings)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    now = datetime.now(UTC)
    tokens, _ = await _issue_tokens(
        user, session=session, settings=settings, now=now, request=request
    )
    await session.commit()
    return tokens


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, request: Request, session: DbSession) -> TokenResponse:
    settings = request.app.state.settings
    now = datetime.now(UTC)
    token_repo = RefreshTokenRepository(session)

    presented = await token_repo.get_valid_by_hash(
        hash_refresh_token(body.refresh_token), as_of=now
    )
    if presented is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or expired refresh token")

    user = await UserRepository(session).get_by_id(presented.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user not found or inactive")

    tokens, new_id = await _issue_tokens(
        user, session=session, settings=settings, now=now, request=request
    )
    await token_repo.rotate(presented.id, new_id=new_id, at=now)
    await session.commit()
    return tokens


@router.post("/logout")
async def logout(body: RefreshRequest, session: DbSession) -> dict[str, bool]:
    await RefreshTokenRepository(session).revoke_by_hash(
        hash_refresh_token(body.refresh_token), at=datetime.now(UTC)
    )
    await session.commit()
    return {"revoked": True}


@router.get("/me")
async def me(user: CurrentUser) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }
