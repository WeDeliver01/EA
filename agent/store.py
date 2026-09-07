"""SPEC-04 §6, §8: local SQLite dedup store and outbound event queue.

The agent's crash-recovery memory. Holds: dedup records keyed on
`client_order_id` (so a redelivered `place_order` never reaches the broker
twice), the outbound event queue (at-least-once delivery to the backend),
and seen-deal tickets (so the deal poller never re-emits `event.deal`).

One dedicated connection, `check_same_thread=False`, all access serialised
by `_lock` - sqlite3 objects are not safe to share across threads without it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_results (
    client_order_id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS outbound_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    acked_at TEXT
);

CREATE TABLE IF NOT EXISTS seen_deals (
    broker_deal_id TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS kv_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class OutboundEvent:
    id: int
    event_id: str
    event_type: str
    payload: dict[str, Any]


class AgentStore:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()

    # -- order dedup, SPEC-04 §4 step 1 -----------------------------------------

    async def get_order_result(self, client_order_id: str) -> dict[str, Any] | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM order_results WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    async def record_order_result(self, client_order_id: str, result: dict[str, Any]) -> None:
        """First write wins: SPEC-04 §6's "exactly once" effect on the broker
        means this table must never let a second write clobber the first,
        even if a caller somehow raced past the `get_order_result` check."""
        async with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO order_results (client_order_id, result_json) "
                "VALUES (?, ?)",
                (client_order_id, json.dumps(result)),
            )
            self._conn.commit()

    # -- outbound event queue, SPEC-04 §6 ---------------------------------------

    async def enqueue_event(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO outbound_events (event_id, event_type, payload_json) "
                "VALUES (?, ?, ?)",
                (event_id, event_type, json.dumps(payload)),
            )
            self._conn.commit()

    async def unacked_events(self, limit: int = 100) -> tuple[OutboundEvent, ...]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT id, event_id, event_type, payload_json FROM outbound_events "
                "WHERE acked_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(
                OutboundEvent(id=r[0], event_id=r[1], event_type=r[2], payload=json.loads(r[3]))
                for r in rows
            )

    async def ack_event(self, event_id: str) -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE outbound_events SET acked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE event_id = ?",
                (event_id,),
            )
            self._conn.commit()

    # -- deal dedup, SPEC-04 §5 "Deal polling" ----------------------------------

    async def is_deal_seen(self, broker_deal_id: str) -> bool:
        async with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_deals WHERE broker_deal_id = ?", (broker_deal_id,)
            ).fetchone()
            return row is not None

    async def mark_deal_seen(self, broker_deal_id: str) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_deals (broker_deal_id) VALUES (?)", (broker_deal_id,)
            )
            self._conn.commit()

    # -- small key/value facts, e.g. last processed command id -----------------

    async def get_meta(self, key: str) -> str | None:
        async with self._lock:
            row = self._conn.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv_meta (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()
