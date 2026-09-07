"""SPEC-04 §2's handshake crypto, and the at-rest encryption for agent
credentials - both built ahead of Phase 5 needing them (Phase 0/1) but
never exercised by a test until now."""

from __future__ import annotations

import time

import pytest

from app.core.security import (
    decrypt_secret,
    encrypt_secret,
    hash_api_key,
    sign_agent_frame,
    verify_agent_signature,
)

pytestmark = pytest.mark.unit


def test_sign_and_verify_round_trips() -> None:
    secret = b"0" * 32
    now_ms = int(time.time() * 1000)
    sig = sign_agent_frame(secret=secret, api_key="key-1", ts_millis=now_ms, nonce="a" * 32)
    assert verify_agent_signature(
        secret=secret, api_key="key-1", ts_millis=now_ms, nonce="a" * 32, signature=sig
    )


def test_verify_rejects_wrong_secret() -> None:
    now_ms = int(time.time() * 1000)
    sig = sign_agent_frame(secret=b"0" * 32, api_key="key-1", ts_millis=now_ms, nonce="a" * 32)
    assert not verify_agent_signature(
        secret=b"1" * 32, api_key="key-1", ts_millis=now_ms, nonce="a" * 32, signature=sig
    )


def test_verify_rejects_tampered_api_key() -> None:
    secret = b"0" * 32
    now_ms = int(time.time() * 1000)
    sig = sign_agent_frame(secret=secret, api_key="key-1", ts_millis=now_ms, nonce="a" * 32)
    assert not verify_agent_signature(
        secret=secret, api_key="key-2", ts_millis=now_ms, nonce="a" * 32, signature=sig
    )


def test_verify_rejects_stale_timestamp() -> None:
    secret = b"0" * 32
    old_ms = int(time.time() * 1000) - 60_000  # 60s old, default max_skew is 30s
    sig = sign_agent_frame(secret=secret, api_key="key-1", ts_millis=old_ms, nonce="a" * 32)
    assert not verify_agent_signature(
        secret=secret, api_key="key-1", ts_millis=old_ms, nonce="a" * 32, signature=sig
    )


def test_verify_accepts_within_configured_skew() -> None:
    secret = b"0" * 32
    ts_ms = int(time.time() * 1000) - 20_000  # 20s old
    sig = sign_agent_frame(secret=secret, api_key="key-1", ts_millis=ts_ms, nonce="a" * 32)
    assert verify_agent_signature(
        secret=secret,
        api_key="key-1",
        ts_millis=ts_ms,
        nonce="a" * 32,
        signature=sig,
        max_skew_ms=30_000,
    )


def test_hash_api_key_is_deterministic_and_distinct() -> None:
    assert hash_api_key("abc") == hash_api_key("abc")
    assert hash_api_key("abc") != hash_api_key("abd")


def test_encrypt_decrypt_round_trips() -> None:
    secret = b"super-secret-32-bytes-of-hmac!!"
    token = encrypt_secret(secret, encryption_key="test-key")
    assert token != secret
    assert decrypt_secret(token, encryption_key="test-key") == secret


def test_decrypt_fails_with_wrong_key() -> None:
    token = encrypt_secret(b"payload", encryption_key="right-key")
    with pytest.raises(ValueError, match="could not be decrypted"):
        decrypt_secret(token, encryption_key="wrong-key")
