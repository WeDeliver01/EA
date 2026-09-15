# DelicateTrader

An autonomous trading platform: Python decides, a risk engine approves,
MT5 executes, PostgreSQL remembers every decision including every decision
not to trade. Full design in [`docs/specs/`](docs/specs/) - start with
[`SPEC-00-overview.md`](docs/specs/SPEC-00-overview.md).

## Status: MVP build, Phases 0-4 of 8 done, Phase 5 connected and round-trip proven

This build covers **Foundation, Domain & persistence, the Strategy engine,
the Research engine (backtester), and Paper execution** to their full
acceptance criteria, plus **Phase 5 (the MT5 bridge)**: the MT5-facing half
(`agent/`, live-verified against a real FTMO-Demo account) and the
backend-facing half (`app/transport/`, the real WS gateway agents connect
to) are now deployed as two separate machines (a Windows host running MT5,
an Ubuntu VPS running the backend) and connected over a real socket - a
real `command.place_order` sent from the deployed backend was executed by
the live agent against the real MT5 terminal, and its broker-level reply
was correlated back correctly. See
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
`agent/mt5_client.py` can place, modify, close and read orders/positions/
deals against a **real** MT5 terminal and a real broker demo account -
live-verified, not simulated - and `app/transport/`, deployed on a real
Ubuntu VPS, authenticates a real agent's WebSocket handshake, dispatches a
command, correlates its reply, and ingests heartbeats and deals into the
database. The two halves are now connected: a real `command.place_order`
sent from the deployed backend was executed by the live agent against the
real MT5 terminal, and a genuine broker-level reply (a rejection, in the
one attempt made so far - see the ADR) was correlated back to the waiting
caller correctly. `dispatcher.py`/`position_manager.py`/`reconciliation.py`
use `WSAgentBroker` instead of the simulated one with no code changes
(that's the whole point of the `AsyncBroker` seam).
**What doesn't exist yet:** `GLOBAL_TRADING_ENABLED` has no effect on
anything, and there's no scheduler that calls `dispatch_pending`/
`Reconciler.run` on its own - both remain test-only (or one-off manual
endpoint) entry points; nothing has originated a trade on its own timer.
Reconnection replay (SPEC-04 §7.2-§7.4) is also not implemented on either
side (see the ADR for the full breakdown).

| Phase | Status |
|---|---|
| 0 Foundation | ✅ Done |
| 1 Domain & persistence | ✅ Done |
| 2 Strategy engine | ✅ Done |
| 3 Research engine | ✅ Done (synthetic data only - see ADR) |
| 4 Paper execution | ✅ Done (simulated broker only - see ADR) |
| 5 MT5 bridge | 🟡 Connected, round trip proven - scheduler and reconnection replay still missing - see ADR |
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
  app/execution/   Paper trading: simulated broker (+ AsyncBroker protocol/adapter),
                   intent service, outbox dispatcher, agent event consumer, live
                   position manager, reconciler. AsyncBroker is the seam a real
                   network-connected broker plugs into with no other code changes.
  app/transport/   The real WS gateway agents connect to: envelope codec, connection
                   registry (WSAgentBroker, an AsyncBroker impl over a live socket),
                   unsolicited-event router (heartbeat, deal). See the ADR - built
                   and tested, but never yet connected to a real agent.
  app/models/      SQLAlchemy ORM.
  app/repositories/ DB <-> domain translation. Never leaks ORM instances.
  app/api/         FastAPI routes: /system/*, and /agent/ws (the real agent WebSocket
                   endpoint - handshake auth, then routes to app/transport/).
  migrations/      Alembic, one initial schema migration.
  tests/           unit/, integration/ (real Postgres+Redis), golden/ (engine fixtures)
agent/            MT5 execution agent (SPEC-04 §8), Python 3.12, standalone package.
                   mt5_client.py + store.py: implemented, live-verified, unit-tested.
                   transport.py/executor.py/watcher.py/heartbeat.py/health.py/main.py:
                   scaffolded and wired, not yet connected to a real backend - see the ADR.
                   Ships independently of backend/ - Windows-only, never imports app.*.
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

- **405 backend tests** (unit + integration against real Postgres/Redis +
  golden engine fixtures, including a real WebSocket handshake against
  the actual FastAPI app) plus **31 agent tests**, all running against a
  fake `MetaTrader5` module (`agent/tests/fake_mt5.py`) so they run
  anywhere - `MetaTrader5` itself is a Windows-only dependency and is never
  imported in CI.
- **~95% overall branch coverage**, **100% on `engines/risk/`** (SPEC-10
  Phase 2's explicit bar) **and on `execution/`** (verified once it had a
  real implementation to hold to that bar).
- `mypy --strict` clean on `domain/` and `engines/`; `mypy` clean on the rest.
- 5/5 import-linter contracts enforcing the dependency rule from
  `SPEC-00` §5 (`domain` imports nothing from the project; `engines` imports
  only `domain`; `repositories` sits between `engines` and the impure
  `execution`/`research`/`workers` layer; `transport` sits above that layer
  and below `api`, and `execution`/`research`/`workers` may never import
  it back; nothing imports `api`).
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
Both halves are now deployed as two separate machines and connected -
`agent/mt5_client.py` live-verified against an FTMO-Demo account,
`app/transport/` deployed on an Ubuntu VPS and handshaking with the real
agent over a real socket, and a real `command.place_order`/reply round
trip has been observed end to end (see the ADR). What's left to finish
Phase 5:

1. **Build a scheduler** that calls `OutboxDispatcher.dispatch_pending`
   and `Reconciler.run` on an interval now that an agent is actually
   connected - neither has ever been called outside a test or the one-off
   `/agent/test-trade` endpoint; this is a pre-existing gap from Phase 4,
   not new, but it's what turns "a signal was approved" into "an order was
   actually sent" without a human hitting an endpoint by hand.
2. **Implement reconnection replay** (SPEC-04 §7.2-§7.4) on both sides: on
   reconnect, replay the local event queue from the last acked `event_id`,
   then send a full position snapshot and a deal batch covering the
   disconnected window with 5-minute overlap. `agent/store.py` has the
   primitives (`unacked_events`/`ack_event`); nothing calls them yet.
3. **Add tests for `agent/executor.py`, `watcher.py`, `heartbeat.py`,
   `health.py`, `main.py`** - currently wired but unverified beyond import
   and manual reasoning.
4. **Rate limiting/backpressure** on the heartbeat and the `event.quote`
   throttle (SPEC-04 §5's 4/sec max) - not implemented on either side.
5. **A live quote source**, so a real order can actually fill rather than
   being rejected for a stale stop-loss level - `event.quote` is currently
   logged and dropped (see the ADR); the one real order attempted so far
   used a hardcoded price from whenever the demo endpoint was written and
   was rejected by the broker (`retcode=10016`, "Invalid stops") because
   the market has since moved.
6. **Harden the deployment**: the VPS's `api` service is bound to
   `0.0.0.0:8000` (reachable from anywhere, not just the agent's IP) to get
   connected quickly - this needs a firewall rule scoped to the agent's IP
   or TLS/nginx in front of it before this goes anywhere near real money.

Two things remain open on the research side, independent of Phase 5:
`SPEC-07` §8's v1 experiment needs to run against real XAUUSD history
before any conclusion about a real trading edge can be trusted, and two
research-engine sub-stages remain unbuilt (parameter perturbation surfaces,
SPEC-07 §6 Stage 4, and the cost-sensitivity table, Stage 7). See the ADR
for the full list of what Phase 4 itself left out - most of Phase 6
(reconciliation's kill-switch triggers, alerting, Prometheus metrics, the
nightly determinism replay job) and all of Phases 7-8.
