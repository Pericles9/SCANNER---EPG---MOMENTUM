-- migrate_v1.sql — v1 schema migration
-- Run once before first paper session.
-- Safe to run against a fresh DB (IF NOT EXISTS skips columns already created by init_db.sql).
-- Idempotent: safe to run on every container startup.

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS filled_qty      INTEGER         NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS remaining_qty   INTEGER         NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS status          TEXT            NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS expected_price  NUMERIC(12,4),
    ADD COLUMN IF NOT EXISTS slippage_bps    NUMERIC(8,2);

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS account_equity_start      NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS theoretical_equity_start  NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS theoretical_equity_end    NUMERIC(12,2);

-- Register scanner_vwap strategy (idempotent — safe on every startup)
INSERT INTO strategies (id, display_name, active)
VALUES ('scanner_vwap', 'Scanner × VWAP v1', TRUE)
ON CONFLICT (id) DO NOTHING;
