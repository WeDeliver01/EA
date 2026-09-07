# DelicateTrader

An autonomous trading platform: Python decides, a risk engine approves,
MT5 executes, PostgreSQL remembers every decision including every decision
not to trade. Full design in [`docs/specs/`](docs/specs/) - start with
[`SPEC-00-overview.md`](docs/specs/SPEC-00-overview.md).

## Status: MVP build, Phases 0-4 of 8 done, Phase 5 partial

This build covers **Foundation, Domain & persistence, the Strategy engine,
the Research engine (backtester), and Paper execution** to their full
acceptance criteria, plus the MT5-facing half of **Phase 5 (the MT5
bridge)** - built and live-verified on a separate Windows host against a
real FTMO-Demo account once one became available. See
[`docs/adr/0001-mvp-scope.md`](docs/adr/0001-mvp-scope.md) for exactly what
that means and every deviation from the spec, and
[`docs/specs/SPEC-10-build-plan.md`](docs/specs/SPEC-10-build-plan.md) for
the full 8-phase plan this is a slice of.

**What works:** the engine can look at a market and produce a fully-reasoned
`TRADE` or `WAIT` decision - regime, structure, liquidity sweeps,
confluence score, every gate it passed or failed, a plain-English narrative
- against a real Postgres schema with an immutable audit trail. That same
engine can be run through a backtester (fills, costs, position management,
metrics, walk-forward, Monte Carlo, promotion gates), and, separately, a
full paper-trading lifecycle now runs end to end against a real Postgres
database and an in-process simulated broker: a `TRADE` decision becomes an
intent, the risk engine re-evaluates it against live, DB-reloaded state,
it dispatches, fills, opens a position, gets managed (breakeven, partial
take-profits, ATR trailing, time exit), closes, and lands as a trade
record - with idempotent, crash-recoverable dispatch (the outbox pattern)
and a reconciler that never auto-closes a position it doesn't recognise.
Separately again, `agent/mt5_client.py` can place, modify, close and read
orders/positions/deals against a **real** MT5 terminal and a real broker
demo account - live-verified, not simulated.
**What doesn't exist yet:** the two halves above aren't connected. There is
no deployed backend for the agent's WSS transport to reach, no
reconnection replay, and `GLOBAL_TRADING_ENABLED` has no effect on
anything - the paper-execution lifecycle above still runs against a fake
broker, and the live-verified MT5 client still runs standalone with its
transport disabled (see the ADR for the full breakdown).

| Phase | Status |
|---|---|
| 0 Foundation | ✅ Done |
| 1 Domain & persistence | ✅ Done |
| 2 Strategy engine | ✅ Done |
| 3 Research engine | ✅ Done (synthetic data only - see ADR) |
| 4 Paper execution | ✅ Done (simulated broker only - see ADR) |
| 5 MT5 bridge | 🟡 Partial - MT5-facing half live-verified; no backend/WSS yet - see ADR |
| 6 Reconciliation & safety | Partially done as part of Phase 4 - see ADR |
| 7 Terminal | Not started |
| 8 Demo, then live | Not started |

## Repository layout

```
backend/          FastAPI app: domain/, engines/, research/, execution/, models/, repositories/, api/
  app/domain/      Pure value objects and enums. Imports nothing else in the project.
  app/engines/     Pure strategy + risk engines. Imports only domain.
  app/research/    Backtester, cost/fill models, metrics, data quality, walk-forward,
                   Monte Carlo, promotion gates, report assembler. Impure (research/ is
                   not engines/) but deterministic given a seed.
  app/execution/   Paper trading: simulated broker, intent service, outbox dispatcher,
                   agent event consumer, live position manager, reconciler. Runs against
                   a simulated broker with no network - see the ADR for what that means.
  app/models/      SQLAlchemy ORM.
  app/repositories/ DB <-> domain translation. Never leaks ORM instances.
  app/api/         FastAPI routes (currently: /system/health, /ready, /status).
  migrations/      Alembic, one initial schema migration.
  tests/           unit/, integration/ (real Postgres+Redis), golden/ (engine fixtures)
agent/            MT5 execution agent (SPEC-04 §8), Python 3.12, standalone package.
                   mt5_client.py + store.py: implemented, live-verified, unit-tested.
                   transport.py/executor.py/watcher.py/heartbeat.py/health.py/main.py:
                   scaffolded and wired, not yet connected to a real backend - see the ADR.
ea/, frontend/, infra/nginx/, infra/prometheus/, docs/runbooks/
                   Stubbed per docs/specs/SPEC-00 §5. Not implemented - see the ADR.
docs/specs/        The full spec set this was built from.
docs/adr/          Architecture decision records.
```

## Running it

Requires Python 3.12, PostgreSQL 16, Redis 7 (or Docker for all three via
`infra/docker-compose.yml`).

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env   # adjust DATABASE_URL / REDIS_URL if not using Docker
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
curl localhost:8000/api/v1/system/ready
```

Or, with Docker:

```bash
make up      # builds and starts postgres, redis, migrate, api
curl localhost:8000/api/v1/system/ready
make down
```

## Verifying it

```bash
make check         # backend: lint, mypy --strict on domain/engines, import-linter, purity grep, tests
make agent-check   # agent: lint, mypy, tests (all against a fake MT5 module - no terminal needed)
```

- **342 backend tests** (unit + integration against real Postgres/Redis +
  golden engine fixtures) plus **31 agent tests**, all running against a
  fake `MetaTrader5` module (`agent/tests/fake_mt5.py`) so they run
  anywhere - `MetaTrader5` itself is a Windows-only dependency and is never
  imported in CI.
- **93% overall branch coverage**, **100% on `engines/risk/`** (SPEC-10
  Phase 2's explicit bar).
- `mypy --strict` clean on `domain/` and `engines/`; `mypy` clean on the rest.
- 4/4 import-linter contracts enforcing the dependency rule from
  `SPEC-00` §5 (`domain` imports nothing from the project; `engines` imports
  only `domain`; `repositories` sits between `engines` and the impure
  `execution`/`research`/`workers` layer; nothing imports `api`).
- The purity grep (`scripts/check_purity.py`) fails the build on
  `datetime.now()` / `time.time()` / unseeded `random` / `uuid4()` /
  `os.environ` anywhere under `engines/`, and on strategy vocabulary
  (`atr`, `signal`, `confluence`, `strategy`) under `agent/` once that
  package has code in it.

CI (`.github/workflows/ci.yml`) runs all of the above plus the Alembic
upgrade/downgrade/upgrade round-trip on every push.

## The eight principles

Everything here is built to hold these, not just the parts that are
finished:

1. Risk outranks conviction.
2. One strategy implementation, shared by backtest, paper and live.
3. Every decision is recorded, including every decision not to trade.
4. Execution is idempotent. The system reconciles rather than assumes.
5. Trading records are immutable.
6. The system defaults to not trading.
7. The engine is deterministic and pure.
8. Money is never a float.

## Next steps

Per `SPEC-10`, the current build increment is **Phase 5 (the MT5 bridge)**.
`app/execution/` was built so Phase 5's job would be narrow: implement
`SimulatedBroker`'s command/event surface against a real agent -
`agent/mt5_client.py` now does exactly that, live-verified against an
FTMO-Demo account; the dispatcher, event consumer, position manager and
reconciler didn't need to change. What's left to finish Phase 5:

1. **Deploy the backend somewhere the agent can reach it** - a Windows host
   running MT5 and a Linux host running this backend need to be two
   separate machines connected over a network (`MetaTrader5` is
   Windows-only and IPC-based; it can't run on Linux). Nothing in Phase 5
   can be fully validated without this.
2. **Test `agent/transport.py`'s connect/reconnect loop** against a real or
   mocked WSS server - handshake success, a rejected handshake (bad
   signature/stale timestamp/reused nonce per SPEC-04 §2), disconnect
   mid-session, and the backoff sequence. Currently untested against any
   server.
3. **Implement reconnection replay** (SPEC-04 §7.2-§7.4): on reconnect,
   replay the local event queue from the last acked `event_id`, then send a
   full position snapshot and a deal batch covering the disconnected window
   with 5-minute overlap. `agent/store.py` has the primitives
   (`unacked_events`/`ack_event`); nothing calls them on reconnect yet.
4. **Add tests for `executor.py`, `watcher.py`, `heartbeat.py`,
   `health.py`, `main.py`** - currently wired but unverified beyond import
   and manual reasoning.
5. **Rate limiting/backpressure** on the heartbeat and the `event.quote`
   throttle (SPEC-04 §5's 4/sec max) - not implemented.

Two things remain open on the research side, independent of Phase 5:
`SPEC-07` §8's v1 experiment needs to run against real XAUUSD history
before any conclusion about a real trading edge can be trusted, and two
research-engine sub-stages remain unbuilt (parameter perturbation surfaces,
SPEC-07 §6 Stage 4, and the cost-sensitivity table, Stage 7). See the ADR
for the full list of what Phase 4 itself left out - most of Phase 6
(reconciliation's kill-switch triggers, alerting, Prometheus metrics, the
nightly determinism replay job) and all of Phases 7-8.
