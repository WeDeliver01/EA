# SPEC-02: PostgreSQL Schema

PostgreSQL 16. All timestamps are `TIMESTAMPTZ` stored in UTC. All money and volume columns are `NUMERIC`. No `FLOAT` anywhere in this schema.

Alembic owns migrations. The DDL below is the target state, written as it should appear in the initial migration.

---

## 0. Extensions and conventions

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "btree_gist";

-- UUIDv7 generation is done in Python (uuid6 package). Do not use gen_random_uuid()
-- for domain entities; time-ordered ids matter for index locality on hot tables.
```

Naming: snake_case, plural table names, `_at` suffix on timestamps, `_id` on foreign keys.
Numeric precision:
- prices and volumes: `NUMERIC(20,10)`
- account currency amounts: `NUMERIC(20,4)`
- percentages and ratios: `NUMERIC(12,6)`

---

## 1. Identity and accounts

```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY,
    email           CITEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'operator'
                    CHECK (role IN ('operator','viewer','admin')),
    totp_secret     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    replaced_by     UUID REFERENCES refresh_tokens(id),
    user_agent      TEXT,
    ip              INET
);
CREATE INDEX ON refresh_tokens (user_id) WHERE revoked_at IS NULL;

CREATE TABLE brokers (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    server          TEXT NOT NULL,
    timezone_offset_minutes INT NOT NULL DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id                  UUID PRIMARY KEY,
    user_id             UUID NOT NULL REFERENCES users(id),
    broker_id           UUID NOT NULL REFERENCES brokers(id),
    mt5_login           TEXT NOT NULL,
    label               TEXT NOT NULL,
    environment         TEXT NOT NULL CHECK (environment IN ('backtest','paper','demo','live')),
    currency            CHAR(3) NOT NULL,
    leverage            INT NOT NULL,
    balance             NUMERIC(20,4) NOT NULL DEFAULT 0,
    equity              NUMERIC(20,4) NOT NULL DEFAULT 0,
    margin              NUMERIC(20,4) NOT NULL DEFAULT 0,
    free_margin         NUMERIC(20,4) NOT NULL DEFAULT 0,
    margin_level        NUMERIC(12,6),
    peak_equity         NUMERIC(20,4) NOT NULL DEFAULT 0,
    server_time         TIMESTAMPTZ,
    state_reported_at   TIMESTAMPTZ,
    trading_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    kill_switch_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_id, mt5_login)
);

-- Only one live account may have trading enabled at a time. Enforced deliberately.
CREATE UNIQUE INDEX one_enabled_live_account
    ON accounts ((TRUE)) WHERE environment = 'live' AND trading_enabled = TRUE;
```

`kill_switch_active` defaults to `TRUE`. A newly created account cannot trade until a human turns it off. This is P6.

---

## 2. Instruments

```sql
CREATE TABLE instruments (
    id                      UUID PRIMARY KEY,
    broker_id               UUID NOT NULL REFERENCES brokers(id),
    symbol                  TEXT NOT NULL,
    canonical_symbol        TEXT NOT NULL,       -- 'XAUUSD' regardless of broker suffix
    asset_class             TEXT NOT NULL,
    digits                  INT NOT NULL,
    point                   NUMERIC(20,10) NOT NULL,
    tick_size               NUMERIC(20,10) NOT NULL,
    tick_value              NUMERIC(20,10) NOT NULL,
    contract_size           NUMERIC(20,10) NOT NULL,
    volume_min              NUMERIC(20,10) NOT NULL,
    volume_max              NUMERIC(20,10) NOT NULL,
    volume_step             NUMERIC(20,10) NOT NULL,
    stops_level_points      INT NOT NULL DEFAULT 0,
    freeze_level_points     INT NOT NULL DEFAULT 0,
    margin_initial          NUMERIC(20,10),
    currency_profit         CHAR(3) NOT NULL,
    currency_margin         CHAR(3) NOT NULL,
    quote_currency          CHAR(3) NOT NULL,
    trading_hours           JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    spec_fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_id, symbol)
);
CREATE INDEX ON instruments (canonical_symbol);
```

Broker symbol suffixes (`XAUUSD.r`, `XAUUSDm`) are real. `canonical_symbol` is what strategy configuration references; `symbol` is what goes on the wire to MT5.

---

## 3. Market data

```sql
CREATE TABLE market_bars (
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    timeframe       TEXT NOT NULL,
    open_time       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(20,10) NOT NULL,
    high            NUMERIC(20,10) NOT NULL,
    low             NUMERIC(20,10) NOT NULL,
    close           NUMERIC(20,10) NOT NULL,
    tick_volume     BIGINT NOT NULL,
    real_volume     BIGINT,
    spread_points   INT,
    source          TEXT NOT NULL CHECK (source IN ('mt5','import','synthetic')),
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, timeframe, open_time)
) PARTITION BY RANGE (open_time);

-- Yearly partitions, created by migration for 2004..current+1, then by a
-- scheduled job each December.
CREATE TABLE market_bars_2026 PARTITION OF market_bars
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

CREATE INDEX ON market_bars (instrument_id, timeframe, open_time DESC);

CREATE TABLE market_ticks (
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    ts              TIMESTAMPTZ NOT NULL,
    bid             NUMERIC(20,10) NOT NULL,
    ask             NUMERIC(20,10) NOT NULL,
    volume          NUMERIC(20,10),
    flags           INT,
    PRIMARY KEY (instrument_id, ts)
) PARTITION BY RANGE (ts);
-- Monthly partitions. Retention: 90 days for live capture, unlimited for
-- explicitly imported research datasets (see research_datasets).
```

**Tick data policy.** Live tick capture exists for spread and slippage measurement, not for strategy input. Research-grade tick history is imported per broker into `research_datasets` and never mixed with live capture, because the two have different quality guarantees.

```sql
CREATE TABLE research_datasets (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    broker_id       UUID REFERENCES brokers(id),
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    kind            TEXT NOT NULL CHECK (kind IN ('bars','ticks')),
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    row_count       BIGINT NOT NULL,
    checksum        TEXT NOT NULL,
    quality_report  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`quality_report` records gap count, largest gap, weekend leakage, duplicate timestamps, zero-volume bar count and spread distribution. A dataset failing the quality thresholds in `SPEC-07` §3 cannot be used for a walk-forward run.

```sql
CREATE TABLE calendar_events (
    id              UUID PRIMARY KEY,
    event_time      TIMESTAMPTZ NOT NULL,
    currency        CHAR(3) NOT NULL,
    impact          TEXT NOT NULL CHECK (impact IN ('low','medium','high')),
    title           TEXT NOT NULL,
    actual          TEXT, forecast TEXT, previous TEXT,
    source          TEXT NOT NULL,
    UNIQUE (event_time, currency, title)
);
CREATE INDEX ON calendar_events (event_time);
```

---

## 4. Strategy configuration

```sql
CREATE TABLE strategy_versions (
    id                  UUID PRIMARY KEY,
    name                TEXT NOT NULL,
    semver              TEXT NOT NULL,
    config              JSONB NOT NULL,
    config_sha256       TEXT NOT NULL,
    engine_git_sha      TEXT NOT NULL,
    label               TEXT NOT NULL,      -- 'tdip@2.1.0+9f3ac41b7de2'
    description         TEXT,
    created_by          UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, semver, config_sha256)
);

CREATE TABLE strategy_assignments (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    primary_timeframe   TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT FALSE,
    paused_until        TIMESTAMPTZ,
    activated_at        TIMESTAMPTZ,
    deactivated_at      TIMESTAMPTZ,
    UNIQUE (account_id, instrument_id, primary_timeframe, strategy_version_id)
);
CREATE UNIQUE INDEX one_active_strategy_per_market
    ON strategy_assignments (account_id, instrument_id, primary_timeframe)
    WHERE is_active = TRUE;

CREATE TABLE risk_profiles (
    id                          UUID PRIMARY KEY,
    account_id                  UUID NOT NULL REFERENCES accounts(id),
    version                     INT NOT NULL,
    risk_per_trade_pct          NUMERIC(12,6) NOT NULL,
    max_daily_loss_pct          NUMERIC(12,6) NOT NULL,
    max_weekly_loss_pct         NUMERIC(12,6) NOT NULL,
    max_open_risk_pct           NUMERIC(12,6) NOT NULL,
    max_daily_trades            INT NOT NULL,
    max_open_positions          INT NOT NULL,
    max_positions_per_symbol    INT NOT NULL DEFAULT 1,
    max_correlated_positions    INT NOT NULL DEFAULT 1,
    min_rr                      NUMERIC(12,6) NOT NULL,
    max_spread_multiple_of_atr  NUMERIC(12,6) NOT NULL,
    pause_after_consecutive_losses INT NOT NULL,
    pause_duration_minutes      INT NOT NULL,
    max_lot_size                NUMERIC(20,10) NOT NULL,
    news_blackout_before_min    INT NOT NULL DEFAULT 15,
    news_blackout_after_min     INT NOT NULL DEFAULT 15,
    is_active                   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, version)
);
CREATE UNIQUE INDEX one_active_risk_profile
    ON risk_profiles (account_id) WHERE is_active = TRUE;
```

Risk profiles are versioned and never edited in place. Changing a limit creates version N+1. Every trade intent stores the `risk_profile_id` that approved it.

---

## 5. Analysis and decisions

```sql
CREATE TABLE analysis_runs (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    timeframe           TEXT NOT NULL,
    as_of               TIMESTAMPTZ NOT NULL,     -- primary bar close time
    mode                TEXT NOT NULL CHECK (mode IN ('live','paper','backtest','replay')),
    backtest_run_id     UUID,                      -- FK added after backtest_runs
    regime              TEXT NOT NULL,
    outcome             TEXT NOT NULL CHECK (outcome IN ('TRADE','WAIT')),
    confluence_score    NUMERIC(12,6) NOT NULL,
    confluence_band     TEXT NOT NULL,
    direction           TEXT,
    setup_kind          TEXT,
    setup_fingerprint   TEXT,
    entry               NUMERIC(20,10),
    stop_loss           NUMERIC(20,10),
    narrative           TEXT NOT NULL DEFAULT '',
    engine_duration_ms  INT NOT NULL,
    market_snapshot     JSONB NOT NULL,   -- see note below
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, instrument_id, timeframe, as_of, strategy_version_id, mode)
);
CREATE INDEX ON analysis_runs (instrument_id, as_of DESC);
CREATE INDEX ON analysis_runs (outcome, as_of DESC);
CREATE INDEX ON analysis_runs (backtest_run_id) WHERE backtest_run_id IS NOT NULL;

CREATE TABLE analysis_evidence (
    id                  BIGSERIAL PRIMARY KEY,
    analysis_run_id     UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    type                TEXT NOT NULL,
    direction           TEXT,
    present             BOOLEAN NOT NULL,
    weight              NUMERIC(12,6) NOT NULL,
    score               NUMERIC(12,6) NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ON analysis_evidence (analysis_run_id);
CREATE INDEX ON analysis_evidence (type, present);

CREATE TABLE analysis_gates (
    id                  BIGSERIAL PRIMARY KEY,
    analysis_run_id     UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    code                TEXT NOT NULL,
    passed              BOOLEAN NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ON analysis_gates (analysis_run_id);
CREATE INDEX ON analysis_gates (code, passed);
```

`market_snapshot` stores the minimum needed to replay the decision: the last N bars per timeframe as compact arrays, the quote, session, and the account and risk snapshot. Target under 32 KB per row. For backtest mode, snapshots are written only for `TRADE` outcomes and a 1-in-200 sample of `WAIT` outcomes, controlled by `BACKTEST_SNAPSHOT_SAMPLE_RATE`, otherwise the table grows without bound.

The `analysis_gates` index on `(code, passed)` is what answers "why is the bot not trading", which is the single most common operational question.

---

## 6. Signals and execution

```sql
CREATE TABLE signals (
    id                  UUID PRIMARY KEY,
    reference           TEXT NOT NULL UNIQUE,      -- SIG-20260907-XAUUSD-M15-000183
    analysis_run_id     UUID NOT NULL REFERENCES analysis_runs(id),
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    direction           TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    setup_kind          TEXT NOT NULL,
    setup_fingerprint   TEXT NOT NULL,
    entry               NUMERIC(20,10) NOT NULL,
    stop_loss           NUMERIC(20,10) NOT NULL,
    take_profits        JSONB NOT NULL,            -- [{level, fraction, r_multiple}]
    confluence_score    NUMERIC(12,6) NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON signals (account_id, created_at DESC);
CREATE INDEX ON signals (instrument_id, setup_fingerprint, created_at DESC);

CREATE TABLE trade_intents (
    id                  UUID PRIMARY KEY,
    client_order_id     TEXT NOT NULL UNIQUE,      -- ULID, the idempotency key
    signal_id           UUID NOT NULL REFERENCES signals(id),
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    risk_profile_id     UUID NOT NULL REFERENCES risk_profiles(id),
    state               TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type          TEXT NOT NULL,
    volume              NUMERIC(20,10) NOT NULL,
    limit_price         NUMERIC(20,10),
    stop_loss           NUMERIC(20,10) NOT NULL,
    take_profit         NUMERIC(20,10),
    max_slippage_points INT NOT NULL,
    magic               BIGINT NOT NULL,
    risk_amount         NUMERIC(20,4) NOT NULL,
    risk_pct_actual     NUMERIC(12,6) NOT NULL,
    sizing_calculation  JSONB NOT NULL,
    risk_state_snapshot JSONB NOT NULL,
    expires_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at          TIMESTAMPTZ
);
CREATE INDEX ON trade_intents (state) WHERE settled_at IS NULL;
CREATE INDEX ON trade_intents (account_id, created_at DESC);
CREATE UNIQUE INDEX one_intent_per_signal ON trade_intents (signal_id);

CREATE TABLE execution_transitions (
    id                  BIGSERIAL PRIMARY KEY,
    trade_intent_id     UUID NOT NULL REFERENCES trade_intents(id),
    from_state          TEXT,
    to_state            TEXT NOT NULL,
    reason              TEXT NOT NULL,
    actor               TEXT NOT NULL CHECK (actor IN ('worker','reconciler','user','agent','system')),
    correlation_id      UUID,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON execution_transitions (trade_intent_id, occurred_at);
```

`trade_intents.state` is the only mutable column on that table besides `settled_at`. Every write to it is accompanied, in the same transaction, by an `execution_transitions` row. Enforce with a trigger.

```sql
CREATE TABLE orders (
    id                  UUID PRIMARY KEY,
    trade_intent_id     UUID NOT NULL REFERENCES trade_intents(id),
    client_order_id     TEXT NOT NULL,
    broker_order_id     TEXT,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    purpose             TEXT NOT NULL CHECK (purpose IN
                        ('ENTRY','EXIT','PARTIAL_EXIT','MODIFY_SL','MODIFY_TP','CLOSE_ALL')),
    side                TEXT NOT NULL,
    order_type          TEXT NOT NULL,
    volume              NUMERIC(20,10) NOT NULL,
    price               NUMERIC(20,10),
    stop_loss           NUMERIC(20,10),
    take_profit         NUMERIC(20,10),
    retcode             INT,
    retcode_text        TEXT,
    sent_at             TIMESTAMPTZ,
    acknowledged_at     TIMESTAMPTZ,
    latency_ms          INT,
    raw_request         JSONB,
    raw_response        JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON orders (client_order_id, purpose, volume, created_at);
CREATE INDEX ON orders (broker_order_id) WHERE broker_order_id IS NOT NULL;
CREATE INDEX ON orders (trade_intent_id);

CREATE TABLE deals (
    id                  UUID PRIMARY KEY,
    broker_deal_id      TEXT NOT NULL,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    order_id            UUID REFERENCES orders(id),
    trade_intent_id     UUID REFERENCES trade_intents(id),
    broker_order_id     TEXT,
    broker_position_id  TEXT NOT NULL,
    deal_type           TEXT NOT NULL,
    side                TEXT NOT NULL,
    volume              NUMERIC(20,10) NOT NULL,
    price               NUMERIC(20,10) NOT NULL,
    commission          NUMERIC(20,4) NOT NULL DEFAULT 0,
    swap                NUMERIC(20,4) NOT NULL DEFAULT 0,
    profit              NUMERIC(20,4) NOT NULL DEFAULT 0,
    executed_at         TIMESTAMPTZ NOT NULL,
    magic               BIGINT,
    comment             TEXT,
    raw                 JSONB NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, broker_deal_id)
);
CREATE INDEX ON deals (broker_position_id);
CREATE INDEX ON deals (account_id, executed_at DESC);
CREATE INDEX ON deals (trade_intent_id) WHERE trade_intent_id IS NOT NULL;
```

`deals` is the financial source of truth. It is append-only. `positions` and `trades` are projections that can be rebuilt from `deals` alone. Write a `rebuild_projections(account_id)` command and test that it reproduces the live tables byte for byte.

```sql
CREATE TABLE positions (
    id                  UUID PRIMARY KEY,
    broker_position_id  TEXT NOT NULL,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    trade_intent_id     UUID REFERENCES trade_intents(id),
    signal_id           UUID REFERENCES signals(id),
    direction           TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED','ORPHANED')),
    volume              NUMERIC(20,10) NOT NULL,
    initial_volume      NUMERIC(20,10) NOT NULL,
    entry_price         NUMERIC(20,10) NOT NULL,
    stop_loss           NUMERIC(20,10),
    take_profit         NUMERIC(20,10),
    initial_stop_loss   NUMERIC(20,10),
    initial_risk        NUMERIC(20,4),
    realised_pnl        NUMERIC(20,4) NOT NULL DEFAULT 0,
    unrealised_pnl      NUMERIC(20,4) NOT NULL DEFAULT 0,
    breakeven_moved     BOOLEAN NOT NULL DEFAULT FALSE,
    partials_taken      INT NOT NULL DEFAULT 0,
    opened_at           TIMESTAMPTZ NOT NULL,
    closed_at           TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, broker_position_id)
);
CREATE INDEX ON positions (account_id, status);

CREATE TABLE position_events (
    id                  BIGSERIAL PRIMARY KEY,
    position_id         UUID NOT NULL REFERENCES positions(id),
    event_type          TEXT NOT NULL,   -- OPENED, SL_MOVED, BREAKEVEN, PARTIAL_TP,
                                         -- TRAIL, TP_HIT, SL_HIT, MANUAL_CLOSE,
                                         -- TIME_EXIT, STRUCTURAL_EXIT, EMERGENCY_EXIT
    old_value           JSONB,
    new_value           JSONB,
    reason              TEXT,
    actor               TEXT NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON position_events (position_id, occurred_at);

CREATE TABLE trades (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    position_id         UUID NOT NULL REFERENCES positions(id) UNIQUE,
    signal_id           UUID REFERENCES signals(id),
    strategy_version_id UUID REFERENCES strategy_versions(id),
    direction           TEXT NOT NULL,
    entry_time          TIMESTAMPTZ NOT NULL,
    exit_time           TIMESTAMPTZ NOT NULL,
    holding_seconds     INT NOT NULL,
    entry_price         NUMERIC(20,10) NOT NULL,
    exit_price          NUMERIC(20,10) NOT NULL,
    volume              NUMERIC(20,10) NOT NULL,
    gross_pnl           NUMERIC(20,4) NOT NULL,
    commission          NUMERIC(20,4) NOT NULL,
    swap                NUMERIC(20,4) NOT NULL,
    net_pnl             NUMERIC(20,4) NOT NULL,
    risk_amount         NUMERIC(20,4) NOT NULL,
    r_multiple          NUMERIC(12,6) NOT NULL,
    mae                 NUMERIC(20,10),      -- max adverse excursion, price
    mfe                 NUMERIC(20,10),      -- max favourable excursion, price
    mae_r               NUMERIC(12,6),
    mfe_r               NUMERIC(12,6),
    entry_slippage_points  INT,
    exit_slippage_points   INT,
    entry_spread_points    INT,
    session             TEXT,
    regime              TEXT,
    confluence_score    NUMERIC(12,6),
    exit_reason         TEXT NOT NULL,
    backtest_run_id     UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON trades (account_id, exit_time DESC);
CREATE INDEX ON trades (strategy_version_id, exit_time);
CREATE INDEX ON trades (backtest_run_id) WHERE backtest_run_id IS NOT NULL;
CREATE INDEX ON trades (regime, session);
```

`mae` and `mfe` are what let you answer "were my stops too tight" and "did I leave money on the table" with evidence. Populate them from tick or M1 data over the holding window.

---

## 7. Reliability infrastructure

```sql
-- Transactional outbox. Nothing is dispatched to the agent except from here.
CREATE TABLE outbox (
    id                  BIGSERIAL PRIMARY KEY,
    aggregate_type      TEXT NOT NULL,
    aggregate_id        UUID NOT NULL,
    command_type        TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    payload             JSONB NOT NULL,
    available_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at       TIMESTAMPTZ,
    attempts            INT NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (command_type, idempotency_key)
);
CREATE INDEX ON outbox (available_at) WHERE dispatched_at IS NULL;

-- Inbox for agent-originated events. Deduplicates redelivery.
CREATE TABLE agent_events (
    id                  BIGSERIAL PRIMARY KEY,
    agent_id            UUID NOT NULL,
    event_id            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    payload             JSONB NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ,
    process_error       TEXT,
    UNIQUE (agent_id, event_id)
);
CREATE INDEX ON agent_events (processed_at) WHERE processed_at IS NULL;

CREATE TABLE agents (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    name                TEXT NOT NULL,
    transport           TEXT NOT NULL CHECK (transport IN ('python_ws','mql5_http')),
    api_key_hash        TEXT NOT NULL,
    hmac_secret_enc     BYTEA NOT NULL,
    build_version       TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, name)
);

CREATE TABLE agent_heartbeats (
    id                  BIGSERIAL PRIMARY KEY,
    agent_id            UUID NOT NULL REFERENCES agents(id),
    account_id          UUID NOT NULL REFERENCES accounts(id),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_time          TIMESTAMPTZ NOT NULL,
    broker_time         TIMESTAMPTZ,
    round_trip_ms       INT,
    terminal_connected  BOOLEAN NOT NULL,
    trade_allowed       BOOLEAN NOT NULL,
    balance             NUMERIC(20,4),
    equity              NUMERIC(20,4),
    open_position_count INT,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ON agent_heartbeats (agent_id, received_at DESC);
-- Retention: 7 days, pruned nightly.

CREATE TABLE reconciliation_runs (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL CHECK (status IN ('running','clean','discrepancies','failed')),
    local_position_count  INT,
    broker_position_count INT,
    discrepancy_count   INT NOT NULL DEFAULT 0,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE discrepancies (
    id                  UUID PRIMARY KEY,
    reconciliation_run_id UUID NOT NULL REFERENCES reconciliation_runs(id),
    account_id          UUID NOT NULL REFERENCES accounts(id),
    kind                TEXT NOT NULL CHECK (kind IN
                        ('MISSING_AT_BROKER','UNKNOWN_AT_BROKER','VOLUME_MISMATCH',
                         'SL_MISMATCH','TP_MISMATCH','PRICE_MISMATCH','ORPHAN_INTENT',
                         'DUPLICATE_POSITION','BALANCE_MISMATCH')),
    severity            TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    local_state         JSONB,
    broker_state        JSONB,
    resolution          TEXT,
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON discrepancies (account_id, resolved_at) WHERE resolved_at IS NULL;
```

**Critical operational rule enforced by this table:** any unresolved `critical` discrepancy sets the `RECONCILIATION_UNRESOLVED` hard gate for that account. Trading stops until a human resolves it. This is P6.

```sql
CREATE TABLE risk_events (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    type                TEXT NOT NULL,       -- DAILY_LOSS_HIT, DRAWDOWN_ALERT,
                                             -- CONSECUTIVE_LOSS_PAUSE, KILL_SWITCH,
                                             -- LIMIT_CHANGED, MARGIN_WARNING
    severity            TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    message             TEXT NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
    acknowledged_at     TIMESTAMPTZ,
    acknowledged_by     UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON risk_events (account_id, created_at DESC);

CREATE TABLE system_events (
    id                  BIGSERIAL PRIMARY KEY,
    type                TEXT NOT NULL,
    severity            TEXT NOT NULL,
    source              TEXT NOT NULL,       -- worker or service name
    correlation_id      UUID,
    message             TEXT NOT NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON system_events (occurred_at DESC);
CREATE INDEX ON system_events (type, occurred_at DESC);

CREATE TABLE audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             UUID REFERENCES users(id),
    action              TEXT NOT NULL,       -- ENABLE_TRADING, KILL_SWITCH_ON,
                                             -- CLOSE_ALL, RISK_PROFILE_CHANGE,
                                             -- STRATEGY_ACTIVATE, MANUAL_CLOSE
    target_type         TEXT,
    target_id           UUID,
    before              JSONB,
    after               JSONB,
    ip                  INET,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Every control action a human takes in the terminal writes to `audit_log`. When something goes wrong at 03:00 you need to know whether the bot did it or you did.

---

## 8. Research tables

```sql
CREATE TABLE backtest_runs (
    id                  UUID PRIMARY KEY,
    name                TEXT NOT NULL,
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    dataset_id          UUID REFERENCES research_datasets(id),
    kind                TEXT NOT NULL CHECK (kind IN
                        ('single','walk_forward_fold','monte_carlo','perturbation','paper')),
    parent_run_id       UUID REFERENCES backtest_runs(id),
    period_start        TIMESTAMPTZ NOT NULL,
    period_end          TIMESTAMPTZ NOT NULL,
    is_out_of_sample    BOOLEAN NOT NULL DEFAULT FALSE,
    initial_balance     NUMERIC(20,4) NOT NULL,
    risk_profile        JSONB NOT NULL,
    cost_model          JSONB NOT NULL,     -- spread, commission, slippage assumptions
    fill_model          TEXT NOT NULL,      -- 'next_bar_open','tick','pessimistic'
    engine_git_sha      TEXT NOT NULL,
    seed                BIGINT,
    status              TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    error               TEXT
);
CREATE INDEX ON backtest_runs (strategy_version_id, created_at DESC);

ALTER TABLE analysis_runs
    ADD CONSTRAINT fk_analysis_backtest
    FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE;
ALTER TABLE trades
    ADD CONSTRAINT fk_trades_backtest
    FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE;

CREATE TABLE backtest_metrics (
    backtest_run_id     UUID PRIMARY KEY REFERENCES backtest_runs(id) ON DELETE CASCADE,
    trade_count         INT NOT NULL,
    win_rate            NUMERIC(12,6),
    profit_factor       NUMERIC(12,6),
    expectancy_r        NUMERIC(12,6),
    avg_win_r           NUMERIC(12,6),
    avg_loss_r          NUMERIC(12,6),
    net_profit          NUMERIC(20,4),
    cagr                NUMERIC(12,6),
    max_drawdown_pct    NUMERIC(12,6),
    max_drawdown_duration_days INT,
    sharpe              NUMERIC(12,6),
    sortino             NUMERIC(12,6),
    calmar              NUMERIC(12,6),
    longest_loss_streak INT,
    longest_win_streak  INT,
    time_in_market_pct  NUMERIC(12,6),
    trades_per_year     NUMERIC(12,6),
    profit_concentration_top1_pct  NUMERIC(12,6),
    profit_concentration_top5_pct  NUMERIC(12,6),
    best_year_pct       NUMERIC(12,6),
    worst_year_pct      NUMERIC(12,6),
    profitable_years    INT,
    total_years         INT,
    longest_no_new_high_days INT,
    yearly_returns      JSONB NOT NULL DEFAULT '{}'::jsonb,
    regime_breakdown    JSONB NOT NULL DEFAULT '{}'::jsonb,
    session_breakdown   JSONB NOT NULL DEFAULT '{}'::jsonb,
    gate_rejection_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE equity_curve_points (
    backtest_run_id     UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    balance             NUMERIC(20,4) NOT NULL,
    equity              NUMERIC(20,4) NOT NULL,
    drawdown_pct        NUMERIC(12,6) NOT NULL,
    open_positions      INT NOT NULL,
    PRIMARY KEY (backtest_run_id, ts)
);
```

`profit_concentration_top1_pct` is a promotion gate, not a vanity metric. If one trade produced more than 25 percent of net profit, the run fails validation regardless of how good the headline numbers look.

---

## 9. Triggers and constraints worth writing

```sql
-- 1. trade_intents.state changes must be accompanied by a transition row.
CREATE FUNCTION assert_transition_logged() RETURNS trigger AS $$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        IF NOT EXISTS (
            SELECT 1 FROM execution_transitions
            WHERE trade_intent_id = NEW.id AND to_state = NEW.state
              AND occurred_at > now() - interval '5 seconds'
        ) THEN
            RAISE EXCEPTION 'state change without transition log: % -> %', OLD.state, NEW.state;
        END IF;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_transition_logged
    AFTER UPDATE ON trade_intents DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_transition_logged();

-- 2. Append-only enforcement on deals.
CREATE RULE deals_no_update AS ON UPDATE TO deals DO INSTEAD NOTHING;
CREATE RULE deals_no_delete AS ON DELETE TO deals DO INSTEAD NOTHING;
-- Same for analysis_runs, analysis_evidence, analysis_gates, signals,
-- execution_transitions, audit_log.

-- 3. Stop loss must be on the correct side of entry.
ALTER TABLE trade_intents ADD CONSTRAINT sl_correct_side CHECK (
    (side = 'BUY'  AND stop_loss < COALESCE(limit_price, stop_loss + 1)) OR
    (side = 'SELL' AND stop_loss > COALESCE(limit_price, stop_loss - 1)) OR
    limit_price IS NULL
);

-- 4. Volume must be positive.
ALTER TABLE trade_intents ADD CONSTRAINT volume_positive CHECK (volume > 0);
ALTER TABLE orders        ADD CONSTRAINT volume_positive CHECK (volume > 0);
```

---

## 10. Retention

| Table | Retention |
|---|---|
| `market_ticks` (live capture) | 90 days, partition drop |
| `agent_heartbeats` | 7 days |
| `system_events` | 90 days |
| `analysis_runs` (backtest mode) | Deleted with the parent `backtest_run` |
| `analysis_runs` (live mode) | Forever |
| `deals`, `trades`, `signals`, `audit_log`, `execution_transitions` | Forever, never pruned |
| `equity_curve_points` | Downsample to daily after 1 year for backtest runs |

Backups: `pg_dump` nightly, plus WAL archiving to a second Vultr region or object storage. Test the restore quarterly. An untested backup is not a backup.
