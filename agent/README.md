# Execution agent

Runs on the Windows VPS beside the MT5 terminal. See `docs/specs/SPEC-04-agent-protocol.md`
for the wire protocol and `docs/specs/SPEC-10-build-plan.md` Phase 5 for the
build plan and acceptance criteria.

## Status

The MT5-facing half is built and verified against a live demo account
(FTMO-Demo): `mt5_client.py` implements `place_order`, `modify_position`,
`close_position` (full and partial), `get_positions`, `get_deals`,
`get_symbol_spec`, `get_tick`, `terminal_health` and `account_snapshot`,
matching `backend/app/execution/broker.py`'s `SimulatedBroker` surface.
`agent/scripts/probe_mt5.py` and `agent/scripts/live_test_mt5_client.py` are
manual, human-supervised scripts for exercising it against a real terminal
(not part of CI - they move real positions).

Real broker quirks this had to account for (see `mt5_client.py` comments):
- `terminal_info().trade_allowed` reflects the terminal's "Algo Trading"
  toggle, not an account permission - must be on before any order works.
- On this broker's execution mode, `order_send`'s synchronous result can
  carry `price=0`/`deal=0`; the real fill is looked up from
  `history_deals_get` afterwards.
- `positions_get(ticket=...)` can briefly return empty right after another
  order on the same ticket settles (the terminal's local table is
  mid-update) - both `modify_position` and `close_position` retry.
- `history_deals_get`'s date bounds are **naive datetimes in broker-server
  time**, not UTC and not tz-aware/int timestamps. The broker here runs
  several hours ahead of the agent's (UTC) system clock. `mt5_client.py`
  computes `broker_time - agent_time` (SPEC-04 §8.4) and converts both the
  query bounds and the returned deal timestamps.
- Dates near the 1970 epoch crash `history_deals_get` on Windows; there is
  no legitimate reason to query that far back, so the default lookback is
  90 days.

`config.py`, `store.py` (SQLite dedup + outbound queue), `transport.py`
(HMAC handshake, envelope codec, backoff - all unit tested), `executor.py`,
`watcher.py`, `heartbeat.py`, `health.py` and `main.py` are scaffolded per
SPEC-04 §8 and wired together, but **the WSS connection to a real backend
has not been exercised** - no backend is deployed anywhere reachable from
this box yet. `main.py` defaults to `--enable-transport` off for that
reason; the MT5-facing half runs standalone.

Known gap for whoever wires up the SPEC-04 §8.5 CI grep check ("no
strategy vocabulary in the agent package"): a literal grep for `signal`
will flag `main.py`'s use of the stdlib `signal` module for shutdown
handling. That's a false positive, not a strategy-logic leak - the check
needs a word-boundary or an allowlist for that import.

## Setup

```
pip install -e .[dev]
```

Requires the `MetaTrader5` package (Windows only) and a running,
logged-in MT5 terminal with **Algo Trading enabled** in the toolbar.

## Tests

```
pytest agent/tests/
```

All unit tests run against a fake `MetaTrader5` module
(`agent/tests/fake_mt5.py`) - no terminal required. The `scripts/` live
tests do require one and are not part of this suite.
