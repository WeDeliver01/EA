# DelicateTrader

An autonomous trading platform: Python decides, a risk engine approves,
MT5 executes, PostgreSQL remembers every decision including every decision
not to trade. Full design in [`docs/specs/`](docs/specs/) - start with
[`SPEC-00-overview.md`](docs/specs/SPEC-00-overview.md).

## Status: MVP build, Phases 0-4 of 8

This build covers **Foundation, Domain & persistence, the Strategy engine,
the Research engine (backtester), and Paper execution** - the parts
buildable and testable without a live broker, an MT5 terminal, or weeks of
demo trading. See
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
**What doesn't exist yet:** a connection to a real broker. There is no
Windows agent, no MT5 terminal, no live quote feed, and
`GLOBAL_TRADING_ENABLED` has no effect on anything - everything above runs
against synthetic bar data and a fake broker (see the ADR for exactly what
that stands in for and what it doesn't).

| Phase | Status |
|---|---|
| 0 Foundation | ✅ Done |
| 1 Domain & persistence | ✅ Done |
| 2 Strategy engine | ✅ Done |
| 3 Research engine | ✅ Done (synthetic data only - see ADR) |
| 4 Paper execution | ✅ Done (simulated broker only - see ADR) |
| 5 MT5 bridge | Not started - needs a real Windows/MT5 host |
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
agent/, ea/, frontend/, infra/nginx/, infra/prometheus/, docs/runbooks/
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
make check   # lint, mypy --strict on domain/engines, import-linter, purity grep, tests
```

- **342 tests**, unit + integration (against real Postgres/Redis) + golden
  engine fixtures.
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

Per `SPEC-10`, the next build increment is **Phase 5 (the MT5 bridge)** -
and it cannot happen in this environment at all. It needs a Windows host
running a real MetaTrader5 terminal and a real broker demo account; nothing
about that can be substituted. `app/execution/` was built so that Phase 5's
job is narrow: implement `SimulatedBroker`'s command/event surface
(`place_order`, `modify_position`, `close_position`, `get_positions`,
`get_deals`) against a real WSS-connected agent. The dispatcher, event
consumer, position manager and reconciler should not need to change.

Two things remain open on the research side, independent of Phase 5:
`SPEC-07` §8's v1 experiment needs to run against real XAUUSD history
before any conclusion about a real trading edge can be trusted, and two
research-engine sub-stages remain unbuilt (parameter perturbation surfaces,
SPEC-07 §6 Stage 4, and the cost-sensitivity table, Stage 7). See the ADR
for the full list of what Phase 4 itself left out - most of Phase 6
(reconciliation's kill-switch triggers, alerting, Prometheus metrics, the
nightly determinism replay job) and all of Phases 7-8.
