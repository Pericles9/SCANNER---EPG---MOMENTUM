-- EPG live paper trading schema
-- Strategy ID: epg_v1
-- Source: live_system_architecture.md Section 9 (exact match)

BEGIN;

CREATE TABLE IF NOT EXISTS strategies (
    id           VARCHAR PRIMARY KEY,
    display_name VARCHAR,
    version      VARCHAR,
    config_json  TEXT,
    deployed_at  BIGINT,
    active       BOOLEAN DEFAULT TRUE
);

INSERT INTO strategies (id, display_name, active)
VALUES ('epg_v1', 'EPG — Event Participation Gate', TRUE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO strategies (id, display_name, active)
VALUES ('scanner_vwap', 'Scanner × VWAP v1', TRUE)
ON CONFLICT (id) DO NOTHING;

-- Strategy-agnostic: one row per scanner poll with ≥1 qualifying ticker.
CREATE TABLE IF NOT EXISTS scanner_snapshots (
    id              BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    snapshot_ns     BIGINT      NOT NULL,
    session_date    DATE        NOT NULL,
    n_qualifying    INTEGER,
    heat_p75        DOUBLE PRECISION,
    snapshot_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_date
    ON scanner_snapshots (session_date);

-- Strategy-agnostic: one row per incoming trade from Polygon WS.
CREATE TABLE IF NOT EXISTS ticks (
    ticker          VARCHAR     NOT NULL,
    session_date    DATE        NOT NULL,
    sip_timestamp   BIGINT      NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    size            INTEGER     NOT NULL,
    side            SMALLINT,
    session_bucket  VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_ticks_ticker_date
    ON ticks (ticker, session_date);

-- Strategy-agnostic: one row per incoming quote from Polygon WS.
CREATE TABLE IF NOT EXISTS quotes (
    ticker          VARCHAR     NOT NULL,
    session_date    DATE        NOT NULL,
    sip_timestamp   BIGINT      NOT NULL,
    bid_price       DOUBLE PRECISION,
    ask_price       DOUBLE PRECISION,
    bid_size        INTEGER,
    ask_size        INTEGER,
    session_bucket  VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_quotes_ticker_date
    ON quotes (ticker, session_date);

-- Per-strategy position tracking. Composite PK: one row per open position.
-- On close, the row is deleted; trades table preserves the history.
CREATE TABLE IF NOT EXISTS positions (
    strategy_id     VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker          VARCHAR     NOT NULL,
    session_date    DATE        NOT NULL,
    qty             INTEGER     NOT NULL,
    avg_entry_price DOUBLE PRECISION,
    open_ns         BIGINT,
    PRIMARY KEY (strategy_id, ticker, session_date)
);

-- One row per order submission.
CREATE TABLE IF NOT EXISTS orders (
    id               BIGINT      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id      VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker           VARCHAR     NOT NULL,
    session_date     DATE        NOT NULL,
    session_bucket   VARCHAR,
    submitted_ns     BIGINT      NOT NULL,
    filled_ns        BIGINT,
    side             VARCHAR     NOT NULL,
    qty              INTEGER     NOT NULL,
    order_type       VARCHAR,
    limit_price      DOUBLE PRECISION,
    fill_price       DOUBLE PRECISION,
    notional         DOUBLE PRECISION,
    status           VARCHAR,
    cancel_reason    VARCHAR,
    broker_order_id  VARCHAR,
    signal_reason    VARCHAR,
    filled_qty       INTEGER         NOT NULL DEFAULT 0,
    remaining_qty    INTEGER         NOT NULL DEFAULT 0,
    expected_price   DOUBLE PRECISION,
    slippage_bps     DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_orders_strategy_ticker
    ON orders (strategy_id, ticker, session_date);

-- One row per completed round-trip.
CREATE TABLE IF NOT EXISTS trades (
    id                           BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id                  VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker                       VARCHAR     NOT NULL,
    session_date                 DATE        NOT NULL,
    session_bucket               VARCHAR,
    entry_order_id               BIGINT      REFERENCES orders(id),
    exit_order_id                BIGINT      REFERENCES orders(id),
    entry_ns                     BIGINT,
    exit_ns                      BIGINT,
    entry_t_sec                  DOUBLE PRECISION,
    exit_t_sec                   DOUBLE PRECISION,
    hold_sec                     DOUBLE PRECISION,
    entry_price                  DOUBLE PRECISION,
    exit_price                   DOUBLE PRECISION,
    qty                          INTEGER,
    pnl_pct                      DOUBLE PRECISION,
    pnl_dollar                   DOUBLE PRECISION,
    prev_close                   DOUBLE PRECISION,
    intraday_pct_at_entry        DOUBLE PRECISION,
    entry_type                   VARCHAR,
    exit_reason                  VARCHAR,
    epg_state_at_entry           VARCHAR,
    lambda_buy_at_entry          DOUBLE PRECISION,
    lambda_sell_at_entry         DOUBLE PRECISION,
    lambda_v_at_entry            DOUBLE PRECISION,
    lambda_v_peak_at_entry       DOUBLE PRECISION,
    cvd_at_entry                 DOUBLE PRECISION,
    exit_d_disabled              BOOLEAN,
    scanner_rank_at_entry        INTEGER,
    scanner_quartile_at_entry    INTEGER,
    scanner_heat_at_entry        DOUBLE PRECISION,
    scanner_n_at_entry           INTEGER,
    natural_exit_ns              BIGINT,
    natural_exit_price           DOUBLE PRECISION,
    natural_exit_pnl_pct         DOUBLE PRECISION,
    natural_exit_reason          VARCHAR,
    drawdown_from_window_high    DOUBLE PRECISION,
    current_window_high_at_entry DOUBLE PRECISION,
    prior_window_peak_at_entry   DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_trades_strategy_ticker
    ON trades (strategy_id, ticker, session_date);

-- One row per strategy-ticker-session.
CREATE TABLE IF NOT EXISTS sessions (
    id                    BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id           VARCHAR     NOT NULL REFERENCES strategies(id),
    session_date          DATE        NOT NULL,
    ticker                VARCHAR     NOT NULL,
    scanner_snapshot_id   BIGINT      REFERENCES scanner_snapshots(id),
    scanner_fire_ns       BIGINT,
    prev_close            DOUBLE PRECISION,
    scanner_rank          INTEGER,
    scanner_n             INTEGER,
    scanner_heat          DOUBLE PRECISION,
    scanner_quartile      INTEGER,
    multi_day_runner      BOOLEAN,
    context_fetch_ms      INTEGER,
    cold_start_n          INTEGER,
    degraded_mode         BOOLEAN,
    lambda_ref_global     DOUBLE PRECISION,
    lambda_ref_fitted     DOUBLE PRECISION,
    mu_buy_fitted         DOUBLE PRECISION,
    mu_sell_fitted        DOUBLE PRECISION,
    alpha_buy_fitted      DOUBLE PRECISION,
    alpha_sell_fitted     DOUBLE PRECISION,
    n_base_at_cold_start  DOUBLE PRECISION,
    n_refits              INTEGER,
    t_event_ns            BIGINT,
    setup_filter_score    DOUBLE PRECISION,
    setup_filter_passes   BOOLEAN,
    closed_ns                BIGINT,
    close_reason             VARCHAR,
    theoretical_equity_start DOUBLE PRECISION,
    theoretical_equity_end   DOUBLE PRECISION,
    account_equity_start     DOUBLE PRECISION,
    UNIQUE (strategy_id, ticker, session_date)
);

-- One row per online Hawkes refit.
CREATE TABLE IF NOT EXISTS hawkes_refits (
    id               BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id      VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker           VARCHAR     NOT NULL,
    session_date     DATE        NOT NULL,
    refit_ns         BIGINT      NOT NULL,
    refit_n          INTEGER     NOT NULL,
    trades_at_refit  INTEGER,
    mu_buy           DOUBLE PRECISION,
    mu_sell          DOUBLE PRECISION,
    alpha_buy_self   DOUBLE PRECISION,
    alpha_sell_self  DOUBLE PRECISION,
    n_base           DOUBLE PRECISION,
    log_likelihood   DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_hawkes_refits_ticker_date
    ON hawkes_refits (ticker, session_date);

-- One row per key state transition (not per tick).
CREATE TABLE IF NOT EXISTS signal_events (
    id                BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id       VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker            VARCHAR     NOT NULL,
    session_date      DATE        NOT NULL,
    event_ns          BIGINT      NOT NULL,
    event_type        VARCHAR     NOT NULL,
    lambda_hat        DOUBLE PRECISION,
    lambda_ref        DOUBLE PRECISION,
    epg_state_before  VARCHAR,
    epg_state_after   VARCHAR,
    notes             VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_signal_events_ticker_date
    ON signal_events (ticker, session_date);

COMMIT;
