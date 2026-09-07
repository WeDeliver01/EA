"""SPEC-01 §1: execution enums."""

from __future__ import annotations

from enum import StrEnum


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionState(StrEnum):
    DETECTED = "DETECTED"
    VALIDATING = "VALIDATING"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    # terminal failures
    RISK_BLOCKED = "RISK_BLOCKED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    BROKER_ERROR = "BROKER_ERROR"
    UNKNOWN = "UNKNOWN"  # requires manual or reconciliation resolution


class DealType(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    SWAP = "SWAP"
    COMMISSION = "COMMISSION"
    CORRECTION = "CORRECTION"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ORPHANED = "ORPHANED"  # exists at broker, not attributable to any intent
