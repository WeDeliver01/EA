# SPEC-08: Infrastructure, Deployment and Operations

---

## 1. Services

`infra/docker-compose.yml`, production overrides in `docker-compose.prod.yml`.

| Service | Image | Replicas | Purpose |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | 1 | Database, bound to 127.0.0.1:5432 |
| `redis` | `redis:7-alpine` | 1 | Streams, locks, cache, bound to 127.0.0.1:6379 |
| `api` | app | 2 | FastAPI, Gunicorn with UvicornWorker |
| `scanner` | app | 1 | Candle close detection, market state cache |
| `strategy` | app | 1 | Engine evaluation |
| `execution` | app | 1 | Risk, intent creation, outbox dispatch |
| `positions` | app | 1 | Position management loop |
| `reconciler` | app | 1 | Reconciliation, discrepancy detection |
| `performance` | app | 1 | Trade closure processing, metrics |
| `scheduler` | app | 1 | APScheduler: rollovers, partition creation, pruning |
| `celery` | app | 2 | Research jobs |
| `frontend` | node | 1 | Next.js |

**Singleton services.** `strategy`, `execution`, `positions` and `reconciler` run exactly one replica each. They are not horizontally scalable in this design and must not be scaled without redesigning the locking. Enforce with a Redis leader lock at startup: a second instance of a singleton service logs a fatal error and exits. Do not rely on the compose file alone; someone will eventually type `--scale`.

Redis persistence: `appendonly yes`, `appendfsync everysec`. Redis holds outbound stream state; losing it means replaying from the outbox, which is safe but noisy.

Postgres: `shared_buffers` 25 percent of RAM, `work_mem` 32MB, `max_connections` 100, `wal_level` replica with archiving enabled.

---

## 2. Environment variables

`.env.example`, validated at startup by a Pydantic `Settings` model that **fails fast** with a listing of every missing or invalid variable. No defaults for anything security-relevant.

```bash
# ---- core
ENVIRONMENT=production                  # development | staging | production
LOG_LEVEL=INFO
LOG_FORMAT=json
GIT_SHA=                                # injected by CI, recorded on every run

# ---- database
DATABASE_URL=postgresql+asyncpg://dt:PASSWORD@postgres:5432/delicate_trader
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=5
DATABASE_ECHO=false

# ---- redis
REDIS_URL=redis://redis:6379/0
REDIS_STREAM_MAXLEN=100000

# ---- security
JWT_SECRET_KEY=                         # 64 random bytes, base64
JWT_ACCESS_TTL_SECONDS=900
JWT_REFRESH_TTL_DAYS=30
ARGON2_TIME_COST=3
ARGON2_MEMORY_COST=65536
ARGON2_PARALLELISM=4
AGENT_SECRET_ENCRYPTION_KEY=            # Fernet key for hmac_secret_enc column
CORS_ORIGINS=https://trading.example.com
REQUIRE_TOTP_FOR_LIVE=true

# ---- trading safety
QUOTE_STALE_SECONDS=5
ACCOUNT_STALE_SECONDS=10
AGENT_HEARTBEAT_TIMEOUT_SECONDS=10
AGENT_DISCONNECT_KILL_SECONDS=300
MARGIN_SAFETY_FACTOR=0.30
RECONCILIATION_INTERVAL_SECONDS=30
POSITION_MONITOR_INTERVAL_SECONDS=2
CANDLE_CLOSE_GRACE_MS=1500
EXECUTION_LOCK_TTL_SECONDS=60
GLOBAL_TRADING_ENABLED=false            # master switch, defaults OFF

# ---- research
BACKTEST_SNAPSHOT_SAMPLE_RATE=0.005
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2
CELERY_WORKER_CONCURRENCY=2

# ---- observability
SENTRY_DSN=
SENTRY_TRACES_SAMPLE_RATE=0.05
PROMETHEUS_ENABLED=true

# ---- notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ALERT_MIN_SEVERITY=warning
```

`GLOBAL_TRADING_ENABLED=false` is the outermost switch. It is checked in the execution guard independently of any database flag. Two people would have to make two different mistakes for an unintended live trade to happen.

---

## 3. Nginx

CloudPanel manages the vhosts. Two sites:

```
trading.<domain>  -> frontend:3000
api.<domain>      -> api:8000
```

Required directives on the API vhost:

```nginx
# WebSocket upgrade
location /api/v1/ws {
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}

# Agent long-poll needs a long read timeout
location /api/v1/agent/commands {
    proxy_pass http://api:8000;
    proxy_read_timeout 40s;
    proxy_buffering off;
}

location / {
    proxy_pass http://api:8000;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

client_max_body_size 20m;   # historical data uploads
```

Security headers on both: HSTS with a 1 year max-age, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, a restrictive CSP on the frontend, and `Referrer-Policy: strict-origin-when-cross-origin`.

Rate limiting at the Nginx layer as a second line: 20 requests per second per IP on `/api/`, burst 40, with `/api/v1/auth/login` at 1 per second burst 5.

Firewall: only 22, 80 and 443 inbound. SSH key-only, root login disabled, fail2ban on. Postgres and Redis never leave loopback.

---

## 4. Redis key conventions

```
Streams
  stream:bars:closed                    XADD on each confirmed candle close
  stream:intents:pending
  stream:agent:events
  stream:ws:{channel}                   bounded, MAXLEN ~ 500, for WS resume

Locks (SET NX EX, value = holder uuid, released by Lua compare-and-delete)
  lock:analyse:{account}:{symbol}:{tf}
  lock:execute:{account}:{symbol}
  lock:reconcile:{account}
  lock:singleton:{service}

Hot state (TTL always set)
  state:quote:{instrument_id}           TTL 30s
  state:account:{account_id}            TTL 30s
  state:agent:{agent_id}                TTL 30s
  state:risk:{account_id}               TTL 10s
  state:bars:{instrument_id}:{tf}       last N bars, TTL 2 * tf seconds
  state:indicator:{instrument}:{tf}:{name}:{params_hash}:{bar_time}   TTL 300s

Counters
  count:trades:{account_id}:{yyyymmdd}
  nonce:agent:{agent_id}:{nonce}        TTL 120s
```

Every key has a TTL. A Redis key without a TTL in this system is a memory leak and a stale-data hazard. Enforce with a lint script that greps for `set(` calls without an `ex=` argument.

---

## 5. Observability

### Structured logging
`structlog`, JSON output, every log line carries `correlation_id`, `service`, `account_id` where applicable, and `git_sha`. Never log: passwords, JWTs, agent keys, HMAC secrets, full account numbers. Log broker order payloads in full; they are operationally essential and contain no secrets.

### Prometheus metrics

```
dt_analysis_runs_total{symbol,timeframe,outcome}
dt_analysis_duration_seconds{symbol}                 histogram
dt_gate_rejections_total{code}
dt_signals_total{symbol,direction}
dt_intents_total{state}
dt_order_latency_seconds                             histogram
dt_slippage_points                                   histogram
dt_agent_heartbeat_age_seconds                       gauge
dt_agent_connected                                   gauge 0/1
dt_open_positions                                    gauge
dt_account_equity                                    gauge
dt_open_risk_pct                                     gauge
dt_drawdown_pct                                      gauge
dt_discrepancies_unresolved{severity}                gauge
dt_outbox_pending                                    gauge
dt_stream_lag_seconds{stream}                        gauge
dt_worker_last_run_timestamp{worker}                 gauge
```

### Alerts

Telegram, with severity routing.

| Alert | Severity | Condition |
|---|---|---|
| Agent disconnected | critical | > 30 seconds |
| Unresolved critical discrepancy | critical | any |
| Daily loss limit hit | critical | on trigger |
| Kill switch auto-activated | critical | on trigger |
| Broker error streak | critical | 3 in 5 minutes |
| Worker stalled | critical | `worker_last_run` older than 3x its interval |
| Outbox backlog | warning | > 10 pending for > 60 seconds |
| Position opened / closed | info | on event, with R multiple |
| Drawdown threshold | warning | on crossing |
| Nightly determinism replay mismatch | critical | any |

Uptime Kuma monitors: `api /system/ready`, the agent's local `/health` through an outbound push heartbeat, and the frontend root.

---

## 6. CI/CD

`.github/workflows/ci.yml`:

```
1. Ruff check + format check
2. mypy --strict on domain/ and engines/, mypy on the rest
3. import-linter contract check (the dependency rule in SPEC-00 §5)
4. Purity grep: no datetime.now / time.time / random / uuid4 / os.environ
   under engines/; no strategy vocabulary under agent/
5. pytest unit                     (fast, no containers)
6. pytest integration              (testcontainers: postgres + redis + fake agent)
7. pytest golden                   (engine fixtures)
8. Coverage gate: 100% branch on engines/risk and execution/,
                  85% overall, fail below
9. Alembic check: migrations are linear, and upgrade+downgrade round-trips clean
10. Build and push images tagged with the git SHA
```

`deploy.yml`, triggered on tag:

```
1. Require the CI workflow to have passed on the same SHA
2. SSH to the Vultr host
3. Pull images
4. Run `alembic upgrade head` in a one-shot container
5. Recreate services, api first with a health check, then workers
6. Post-deploy smoke: /system/ready, agent connectivity, one replay determinism check
7. Roll back to the previous tag automatically if the smoke test fails
```

Deployment rule: **never deploy while a position is open** unless the deploy is an emergency fix. The deploy script checks `SELECT count(*) FROM positions WHERE status='OPEN'` and refuses unless `--force` is passed with a reason that is written to `audit_log`.

---

## 7. Windows execution node

Documented fully in `docs/runbooks/windows-node.md`.

- Windows Server 2022, 2 vCPU, 4 GB RAM minimum. Choose a region with low latency to the broker's server, which is not necessarily near you. Measure it before committing.
- MT5 terminal: auto-start, auto-login with saved credentials, Algo Trading enabled, the API URL added to the allowed-URL list if the MQL5 fallback is used.
- Python 3.12 with the agent installed as a Windows Service under NSSM, restart on failure with a 10 second delay, restart on boot.
- Windows Update set to manual. An unattended reboot mid-position is a real operational risk. Schedule patching deliberately, during a market close, with positions flat.
- Local `agent_state.db` backed up daily to the Linux host over the same outbound channel.
- No inbound ports. RDP through Tailscale or WireGuard only.
- A watchdog scheduled task that checks every 5 minutes whether both `terminal64.exe` and the agent service are running, restarts them if not, and reports through the agent's push heartbeat.

**Second node.** Once live, run a second Windows node in standby with the agent installed but not started. Recovering from a dead node should be starting a service, not provisioning a machine. Document the failover in the runbook, including the fact that both nodes must never run simultaneously against the same account.

---

## 8. Backups and disaster recovery

| Asset | Method | Frequency | Retention |
|---|---|---|---|
| PostgreSQL | `pg_dump` custom format, plus WAL archiving | Dump nightly, WAL continuous | 30 daily, 12 monthly |
| Backup destination | Object storage in a different region | | |
| Redis | AOF snapshot | Daily | 7 days |
| `agent_state.db` | File copy over the agent channel | Daily | 14 days |
| Secrets | Password manager, not in the repo, not in the backup | On change | |

Recovery objectives: RPO 15 minutes for the database, RTO 2 hours for the full stack.

Quarterly drill, documented in `docs/runbooks/dr-drill.md`: restore last night's dump to a scratch host, run `rebuild_projections()`, and verify that `positions` and `trades` match the originals exactly. A backup that has not been restored is a hypothesis.

---

## 9. Runbooks to write

Each one a numbered procedure, written before it is needed:

1. `agent-disconnected.md` - diagnose, restore, verify reconciliation clean before re-enabling
2. `unresolved-discrepancy.md` - how to inspect, how to resolve, when to close manually
3. `unknown-intent.md` - the manual resolution path
4. `emergency-flatten.md` - closing everything when the platform is down, directly in MT5, and how to make the system aware afterwards
5. `broker-rejects-orders.md` - retcode reference, spec refresh, symbol suffix issues
6. `deploy-and-rollback.md`
7. `windows-node.md`
8. `dr-drill.md`
9. `strategy-promotion.md` - the checklist from research to live
10. `daily-checks.md` - what to look at each morning: reconciliation status, gate telemetry, agent uptime, drawdown, and whether the determinism replay passed

Runbook 4 matters most and is written first. The scenario where the Linux side is down and a position is open is the one that costs money, and the procedure must not require thinking.
