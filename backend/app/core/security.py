"""JWT (human auth), Argon2id (password hashing) and HMAC-SHA256 (agent auth)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

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


def hash_api_key(api_key: str) -> str:
    """SHA-256, not Argon2id: an agent's `api_key` is a high-entropy
    generated secret, not a human password, so a fast deterministic hash
    that supports direct lookup-by-hash is correct here - Argon2id's
    per-hash salt would make that lookup impossible without checking every
    row."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def _fernet(encryption_key: str) -> Fernet:
    # `agent_secret_encryption_key` is an operator-chosen string (env var),
    # not necessarily a valid Fernet key (32 url-safe base64 bytes) -
    # derive one deterministically so any non-empty string works.
    derived = base64.urlsafe_b64encode(hashlib.sha256(encryption_key.encode()).digest())
    return Fernet(derived)


def encrypt_secret(secret: bytes, *, encryption_key: str) -> bytes:
    return _fernet(encryption_key).encrypt(secret)


def decrypt_secret(token: bytes, *, encryption_key: str) -> bytes:
    try:
        return _fernet(encryption_key).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("hmac_secret_enc could not be decrypted - wrong encryption key?") from exc
