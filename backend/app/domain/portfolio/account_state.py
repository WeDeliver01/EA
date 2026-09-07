"""SPEC-01 §8: AccountState.

``is_stale`` is computed at construction from the injected clock, not read
inside the engine. If ``is_stale`` is true, the ``PRICE_STALE`` gate fails and
no trade is possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AccountState:
    account_id: UUID
    broker: str
    login: str
    currency: str
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_level: Decimal | None
    leverage: int
    server_time: datetime
    reported_at: datetime
    is_stale: bool
