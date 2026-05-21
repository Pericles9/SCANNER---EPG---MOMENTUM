# Column lists matching init_db.sql — used by asyncpg copy_records_to_table.
# Order must match the table column order exactly (excluding auto-generated id).

TICKS_COLUMNS = [
    "ticker", "session_date", "sip_timestamp",
    "price", "size", "side", "session_bucket",
]

QUOTES_COLUMNS = [
    "ticker", "session_date", "sip_timestamp",
    "bid_price", "ask_price", "bid_size", "ask_size", "session_bucket",
]

SIGNAL_EVENTS_COLUMNS = [
    "strategy_id", "ticker", "session_date",
    "event_ns", "event_type",
    "lambda_hat", "lambda_ref",
    "epg_state_before", "epg_state_after", "notes",
]

HAWKES_REFITS_COLUMNS = [
    "strategy_id", "ticker", "session_date",
    "refit_ns", "refit_n", "trades_at_refit",
    "mu_buy", "mu_sell", "alpha_buy_self", "alpha_sell_self",
    "n_base", "log_likelihood",
]
