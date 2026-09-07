"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-07

Transcribed from docs/specs/SPEC-02-database-schema.md. Written as raw SQL
(rather than op.create_table) because the schema leans on partitioning,
CHECK constraints, partial and expression indexes, RULEs and a constraint
trigger that Alembic's op.* helpers don't model cleanly.

Deviations from the spec, recorded here rather than silently:
  - CITEXT extension added (used by users.email but not listed in SPEC-02 §0).
  - market_bars partitions cover 2024-2028 (spec says 2004..current+1); a
    full historical backfill belongs to the data-import job, not the initial
    migration, since there is no data before the account exists.
  - market_ticks partitions cover 2026-01 through 2027-03 (15 months); the
    scheduler service (SPEC-08 §1, `scheduler`) is responsible for rolling
    these forward and dropping ones older than the 90-day retention window.
    Both are out of scope for this MVP (see docs/adr/0001-mvp-scope.md).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
-- ============================================================ 0. Extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "btree_gist";
CREATE EXTENSION IF NOT EXISTS "citext";

-- ==================================================== 1. Identity and accounts
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

CREATE UNIQUE INDEX one_enabled_live_account
    ON accounts ((TRUE)) WHERE environment = 'live' AND trading_enabled = TRUE;

-- ============================================================ 2. Instruments
CREATE TABLE instruments (
    id                      UUID PRIMARY KEY,
    broker_id               UUID NOT NULL REFERENCES brokers(id),
    symbol                  TEXT NOT NULL,
    canonical_symbol        TEXT NOT NULL,
    asset_class             TEXT NOT NULL,
    digits                  INT NOT NULL,
    point                   NUMERIC(20,10) NOT NULL,
    tick_size               NUMERIC(20,10) NOT NULL,
    tick_value               NUMERIC(20,10) NOT NULL,
    contract_size           NUMERIC(20,10) NOT NULL,
    volume_min              NUMERIC(20,10) NOT NULL,
    volume_max              NUMERIC(20,10) NOT NULL,
    volume_step              NUMERIC(20,10) NOT NULL,
    stops_level_points      INT NOT NULL DEFAULT 0,
    freeze_level_points     INT NOT NULL DEFAULT 0,
    margin_initial          NUMERIC(20,10),
    currency_profit         CHAR(3) NOT NULL,
    currency_margin         CHAR(3) NOT NULL,
    quote_currency           CHAR(3) NOT NULL,
    trading_hours           JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    spec_fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_id, symbol)
);
CREATE INDEX ON instruments (canonical_symbol);

-- ============================================================ 3. Market data
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

CREATE TABLE market_bars_2024 PARTITION OF market_bars FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
CREATE TABLE market_bars_2025 PARTITION OF market_bars FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE market_bars_2026 PARTITION OF market_bars FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE market_bars_2027 PARTITION OF market_bars FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
CREATE TABLE market_bars_2028 PARTITION OF market_bars FOR VALUES FROM ('2028-01-01') TO ('2029-01-01');

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

CREATE TABLE market_ticks_2026_01 PARTITION OF market_ticks FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE market_ticks_2026_02 PARTITION OF market_ticks FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE market_ticks_2026_03 PARTITION OF market_ticks FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE market_ticks_2026_04 PARTITION OF market_ticks FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE market_ticks_2026_05 PARTITION OF market_ticks FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE market_ticks_2026_06 PARTITION OF market_ticks FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE market_ticks_2026_07 PARTITION OF market_ticks FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE market_ticks_2026_08 PARTITION OF market_ticks FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE market_ticks_2026_09 PARTITION OF market_ticks FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE market_ticks_2026_10 PARTITION OF market_ticks FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE market_ticks_2026_11 PARTITION OF market_ticks FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE market_ticks_2026_12 PARTITION OF market_ticks FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE market_ticks_2027_01 PARTITION OF market_ticks FOR VALUES FROM ('2027-01-01') TO ('2027-02-01');
CREATE TABLE market_ticks_2027_02 PARTITION OF market_ticks FOR VALUES FROM ('2027-02-01') TO ('2027-03-01');
CREATE TABLE market_ticks_2027_03 PARTITION OF market_ticks FOR VALUES FROM ('2027-03-01') TO ('2027-04-01');

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
    oos_locked_until TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

-- ==================================================== 4. Strategy configuration
CREATE TABLE strategy_versions (
    id                  UUID PRIMARY KEY,
    name                TEXT NOT NULL,
    semver              TEXT NOT NULL,
    config               JSONB NOT NULL,
    config_sha256       TEXT NOT NULL,
    engine_git_sha      TEXT NOT NULL,
    label               TEXT NOT NULL,
    description         TEXT,
    created_by          UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, semver, config_sha256)
);
CREATE SEQUENCE strategy_version_seq;

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

-- ==================================================== 5. Analysis and decisions
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
    cost_model          JSONB NOT NULL,
    fill_model          TEXT NOT NULL,
    engine_git_sha      TEXT NOT NULL,
    seed                BIGINT,
    status              TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON backtest_runs (strategy_version_id, created_at DESC);

CREATE TABLE analysis_runs (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    timeframe           TEXT NOT NULL,
    as_of               TIMESTAMPTZ NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('live','paper','backtest','replay')),
    backtest_run_id     UUID REFERENCES backtest_runs(id) ON DELETE CASCADE,
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
    market_snapshot     JSONB NOT NULL,
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

-- ==================================================== 6. Signals and execution
CREATE TABLE signals (
    id                  UUID PRIMARY KEY,
    reference           TEXT NOT NULL UNIQUE,
    analysis_run_id     UUID NOT NULL REFERENCES analysis_runs(id),
    account_id          UUID NOT NULL REFERENCES accounts(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    direction           TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    setup_kind          TEXT NOT NULL,
    setup_fingerprint   TEXT NOT NULL,
    entry               NUMERIC(20,10) NOT NULL,
    stop_loss           NUMERIC(20,10) NOT NULL,
    take_profits        JSONB NOT NULL,
    confluence_score    NUMERIC(12,6) NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON signals (account_id, created_at DESC);
CREATE INDEX ON signals (instrument_id, setup_fingerprint, created_at DESC);

CREATE TABLE trade_intents (
    id                  UUID PRIMARY KEY,
    client_order_id     TEXT NOT NULL UNIQUE,
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
    settled_at          TIMESTAMPTZ,
    CONSTRAINT sl_correct_side CHECK (
        (side = 'BUY'  AND stop_loss < COALESCE(limit_price, stop_loss + 1)) OR
        (side = 'SELL' AND stop_loss > COALESCE(limit_price, stop_loss - 1)) OR
        limit_price IS NULL
    ),
    CONSTRAINT volume_positive CHECK (volume > 0)
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
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT volume_positive CHECK (volume > 0)
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
    comment              TEXT,
    raw                 JSONB NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, broker_deal_id)
);
CREATE INDEX ON deals (broker_position_id);
CREATE INDEX ON deals (account_id, executed_at DESC);
CREATE INDEX ON deals (trade_intent_id) WHERE trade_intent_id IS NOT NULL;

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
    event_type          TEXT NOT NULL,
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
    mae                 NUMERIC(20,10),
    mfe                 NUMERIC(20,10),
    mae_r               NUMERIC(12,6),
    mfe_r               NUMERIC(12,6),
    entry_slippage_points  INT,
    exit_slippage_points   INT,
    entry_spread_points    INT,
    session             TEXT,
    regime              TEXT,
    confluence_score    NUMERIC(12,6),
    exit_reason         TEXT NOT NULL,
    backtest_run_id     UUID REFERENCES backtest_runs(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON trades (account_id, exit_time DESC);
CREATE INDEX ON trades (strategy_version_id, exit_time);
CREATE INDEX ON trades (backtest_run_id) WHERE backtest_run_id IS NOT NULL;
CREATE INDEX ON trades (regime, session);

-- ================================================ 7. Reliability infrastructure
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

CREATE TABLE risk_events (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    type                TEXT NOT NULL,
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
    source              TEXT NOT NULL,
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
    action              TEXT NOT NULL,
    target_type         TEXT,
    target_id           UUID,
    before               JSONB,
    after                JSONB,
    ip                  INET,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================ 8. Research tables
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

-- ================================================== 9. Triggers and constraints
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

-- Append-only enforcement. Same rule shape on every immutable table (P5).
CREATE RULE deals_no_update AS ON UPDATE TO deals DO INSTEAD NOTHING;
CREATE RULE deals_no_delete AS ON DELETE TO deals DO INSTEAD NOTHING;

CREATE RULE analysis_runs_no_update AS ON UPDATE TO analysis_runs DO INSTEAD NOTHING;
CREATE RULE analysis_runs_no_delete AS ON DELETE TO analysis_runs DO INSTEAD NOTHING;

CREATE RULE analysis_evidence_no_update AS ON UPDATE TO analysis_evidence DO INSTEAD NOTHING;
CREATE RULE analysis_evidence_no_delete AS ON DELETE TO analysis_evidence DO INSTEAD NOTHING;

CREATE RULE analysis_gates_no_update AS ON UPDATE TO analysis_gates DO INSTEAD NOTHING;
CREATE RULE analysis_gates_no_delete AS ON DELETE TO analysis_gates DO INSTEAD NOTHING;

CREATE RULE signals_no_update AS ON UPDATE TO signals DO INSTEAD NOTHING;
CREATE RULE signals_no_delete AS ON DELETE TO signals DO INSTEAD NOTHING;

CREATE RULE execution_transitions_no_update AS ON UPDATE TO execution_transitions DO INSTEAD NOTHING;
CREATE RULE execution_transitions_no_delete AS ON DELETE TO execution_transitions DO INSTEAD NOTHING;

CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;
"""


DOWNGRADE_SQL = """
DROP RULE IF EXISTS audit_log_no_delete ON audit_log;
DROP RULE IF EXISTS audit_log_no_update ON audit_log;
DROP RULE IF EXISTS execution_transitions_no_delete ON execution_transitions;
DROP RULE IF EXISTS execution_transitions_no_update ON execution_transitions;
DROP RULE IF EXISTS signals_no_delete ON signals;
DROP RULE IF EXISTS signals_no_update ON signals;
DROP RULE IF EXISTS analysis_gates_no_delete ON analysis_gates;
DROP RULE IF EXISTS analysis_gates_no_update ON analysis_gates;
DROP RULE IF EXISTS analysis_evidence_no_delete ON analysis_evidence;
DROP RULE IF EXISTS analysis_evidence_no_update ON analysis_evidence;
DROP RULE IF EXISTS analysis_runs_no_delete ON analysis_runs;
DROP RULE IF EXISTS analysis_runs_no_update ON analysis_runs;
DROP RULE IF EXISTS deals_no_delete ON deals;
DROP RULE IF EXISTS deals_no_update ON deals;

DROP TRIGGER IF EXISTS trg_transition_logged ON trade_intents;
DROP FUNCTION IF EXISTS assert_transition_logged();

DROP TABLE IF EXISTS equity_curve_points;
DROP TABLE IF EXISTS backtest_metrics;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS system_events;
DROP TABLE IF EXISTS risk_events;
DROP TABLE IF EXISTS discrepancies;
DROP TABLE IF EXISTS reconciliation_runs;
DROP TABLE IF EXISTS agent_heartbeats;
DROP TABLE IF EXISTS agents;
DROP TABLE IF EXISTS agent_events;
DROP TABLE IF EXISTS outbox;
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS position_events;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS deals;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS execution_transitions;
DROP TABLE IF EXISTS trade_intents;
DROP TABLE IF EXISTS signals;
DROP TABLE IF EXISTS analysis_gates;
DROP TABLE IF EXISTS analysis_evidence;
DROP TABLE IF EXISTS analysis_runs;
DROP TABLE IF EXISTS backtest_runs;
DROP TABLE IF EXISTS risk_profiles;
DROP TABLE IF EXISTS strategy_assignments;
DROP SEQUENCE IF EXISTS strategy_version_seq;
DROP TABLE IF EXISTS strategy_versions;
DROP TABLE IF EXISTS calendar_events;
DROP TABLE IF EXISTS research_datasets;
DROP TABLE IF EXISTS market_ticks;
DROP TABLE IF EXISTS market_bars;
DROP TABLE IF EXISTS instruments;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS brokers;
DROP TABLE IF EXISTS refresh_tokens;
DROP TABLE IF EXISTS users;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
