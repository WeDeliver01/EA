"""JWT (human auth), Argon2id (password hashing) and HMAC-SHA256 (agent auth)."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import Settings


def _hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )


def hash_password(password: str, settings: Settings) -> str:
    return _hasher(settings).hash(password)


def verify_password(password: str, password_hash: str, settings: Settings) -> bool:
    try:
        return _hasher(settings).verify(password_hash, password)
    except VerifyMismatchError:
        return False


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    sub: str
    role: str
    jti: str
    exp: int
    iat: int


def encode_access_token(
    *, subject: str, role: str, jti: str, issued_at: int, ttl_seconds: int, secret_key: str
) -> str:
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "jti": jti,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def decode_access_token(token: str, secret_key: str) -> AccessTokenClaims:
    payload = jwt.decode(token, secret_key, algorithms=["HS256"])
    return AccessTokenClaims(
        sub=payload["sub"],
        role=payload["role"],
        jti=payload["jti"],
        exp=payload["exp"],
        iat=payload["iat"],
    )


def sign_agent_frame(*, secret: bytes, api_key: str, ts_millis: int, nonce: str) -> str:
    message = f"{api_key}.{ts_millis}.{nonce}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_agent_signature(
    *,
    secret: bytes,
    api_key: str,
    ts_millis: int,
    nonce: str,
    signature: str,
    max_skew_ms: int = 30_000,
) -> bool:
    now_ms = int(time.time() * 1000)
    if abs(now_ms - ts_millis) > max_skew_ms:
        return False
    expected = sign_agent_frame(secret=secret, api_key=api_key, ts_millis=ts_millis, nonce=nonce)
    return hmac.compare_digest(expected, signature)
