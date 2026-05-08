---
tags:
  - type/data-schema
  - domain/data
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-04-04
---

# Mom_db Data Schema & Build Provenance

## Scope

This document maps each top-level folder under `data/` to:

1. File/folder format
2. Naming schema
3. Script(s) used to build it (when found in current workspace)
4. Target snapshot duration

## Provenance Status Legend

- **Confirmed**: explicit writer logic found in current workspace.
- **Inferred**: structure verified on disk, but writer script not found here.
- **Unknown**: no reliable writer evidence found in current workspace.

## Archived Script Note

No workspace-local `archived` folder was found during repo search. If historical collection scripts exist outside this checkout, the unresolved provenance entries below should be reconciled against that external archive.

## Folder-by-Folder Schema

| Folder | File / Folder Format | Naming Schema (observed) | Build Script(s) Found | Target Duration | Provenance |
|---|---|---|---|---|---|
| `collection_scripts/` | Python scripts + logs | `collect_massive_data.py`, `filter_events_power_law.py`, `inspect_parquet_columns.py`, `collection_log.txt` | N/A (script source folder) | N/A | Confirmed |
| `momentum_events/` | Parquet + CSV event tables | `filtered_events_power_law_q05.parquet`, `filtered_events_power_law_q05.csv`; plus momentum scan parquet inputs | `collection_scripts/filter_events_power_law.py` | Event-level records (no fixed window; one row per event) | Confirmed |
| `filtered/` | Per-event folders containing 2 parquet files | Folder: `{TICKER}_{YYYY-MM-DD}_{momentum_pct_2dp}`; files: `trades.parquet`, `quotes.parquet` | `collection_scripts/collect_massive_data.py` (input: `momentum_events/filtered_events_power_law_q05.parquet`) | **7 trading-day window** centered on event date (T-3 ... T+3) | Confirmed |
| `daily/` | Parquet files | `{TICKER}_daily.parquet` | Not found in current workspace | 1-day bars aggregated across multi-date history per symbol file | Inferred |
| `minute/` | Folder-per-symbol with daily parquet files | `{TICKER}/{YYYY-MM-DD}.parquet` | Not found in current workspace | 1-minute bars for one trading session per file | Inferred |
| `second10/` | Folder-per-symbol with daily parquet files | `{TICKER}/{TICKER}_{YYYY-MM-DD}.parquet` | Not found in current workspace | 10-second bars for one trading session per file | Inferred |
| `quote_data/` | Flat parquet files | `{TICKER}_quotes_{YYYY}_{MM}_{DD}.parquet` | Not found in current workspace | Quote ticks for one symbol-day per file | Inferred |
| `trade_data/` | Mixed folders + parquet + JSON progress files | Subfolders: `batches/`, `by_date/`, `by_ticker/`, `enhanced/`, `high_momentum/`, `logs/`, `metadata/`; files: `momentum_events_for_collection.parquet`, `*_progress.json` | Legacy helper reference only in `debug_schema.py`; no active writer found | Likely symbol/day or batch snapshots (depends on subfolder) | Unknown |
| `metadata/` | Parquet | `collection_stats.parquet`, `symbols_metadata.parquet` | Not found in current workspace | Dataset-level summary snapshots (non-time-series payloads) | Unknown |
| `market-hours/` | JSON | `market-hours-database.json` | Not found in current workspace | Calendar/session metadata (date-level) | Unknown |
| `symbol-properties/` | CSV | `symbol-properties-database.csv` | Not found in current workspace | Point-in-time symbol attributes | Unknown |
| `nautilus_catalog/` | Nested parquet catalog layout | `data/equity/{SYMBOL}.{VENUE}/...parquet`, `data/trade_tick/{SYMBOL}.{VENUE}/...parquet` | No catalog writer found in current workspace (many readers/consumers exist) | Instrument/venue-partitioned history; file-level duration not established here | Inferred |
| `collection_scripts/collection_log.txt` (artifact) | Text log | Line-oriented run log of collection process | Written by `collect_massive_data.py` logging | Runtime log across full collection batch | Confirmed |

## Confirmed Build Pipeline (Current Workspace)

1. **Momentum filter stage**
	- Script: `collection_scripts/filter_events_power_law.py`
	- Reads momentum scan parquet source files.
	- Fits q=0.05 quantile regression in log-space.
	- Writes `momentum_events/filtered_events_power_law_q05.parquet` (+ CSV).

2. **Event snapshot collection stage**
	- Script: `collection_scripts/collect_massive_data.py`
	- Reads filtered event parquet.
	- For each event, fetches trades + quotes and writes:
	  - `filtered/{TICKER}_{DATE}_{MOM}/trades.parquet`
	  - `filtered/{TICKER}_{DATE}_{MOM}/quotes.parquet`
	- Uses a 7-trading-day window around event date.

## DuckDB Implementation (Current)

### Components

- `src/data/db.py`
	- Connection manager (`get_connection`) for DuckDB.
	- Creates parent directory before opening DB.
	- Supports externalized DB location via:
		1. `db_path` argument
		2. `MOM_DB_DUCKDB_PATH`
		3. `MOM_DB_DATABASE_ROOT/main.duckdb`
		4. fallback `data/duckdb/main.duckdb`

- `src/data/ingest.py`
	- Multi-dataset ingest CLI (`--all`, `--dataset`, `--verify-only`).
	- Data root defaults to `MOM_DB_DATA_ROOT` (if set), else `data/` in repo.
	- Optional `--db-path` to write into any DuckDB file.
	- Creates materialized tables for most datasets and live views for Nautilus catalog parquet globs.

- `src/data/paths.py`
	- Central path resolution for data/database split scenarios.
	- Single source of truth for project default roots and env override precedence.

### Ingested Tables / Views

| Dataset Key | Table / View Name | Type |
|---|---|---|
| `filtered` | `filtered_trades`, `filtered_quotes` | Table |
| `daily` | `daily_bars` | Table |
| `minute` | `minute_bars` | Table |
| `second10` | `second10_bars` | Table |
| `quote_data` | `raw_quotes` | Table |
| `momentum_events` | `momentum_events` | Table |
| `metadata` | `collection_stats`, `symbols_metadata` | Table |
| `market_hours` | `market_hours` | Table |
| `symbol_properties` | `symbol_properties` | Table |
| `trade_data` | `trade_data_events`, `trade_data_*` | Table |
| `nautilus_catalog` | `nautilus_equity`, `nautilus_trade_tick` | View |

### CLI Reference

```bash
# Full ingest using resolved defaults
python -m src.data.ingest --all

# Selective ingest
python -m src.data.ingest --dataset filtered --dataset minute

# Override both roots explicitly
python -m src.data.ingest --data-root D:/mom_db_storage/data --db-path D:/mom_db_storage/data/duckdb/main.duckdb

# Verify inventory only
python -m src.data.ingest --verify-only
```

## Database Directory Split Prep (New)

To support splitting this research repo from storage, a migration prep utility is now available:

- Script: `src/data/prepare_database_split.py`
- Purpose:
	1. Create target directory scaffold
	2. Build `migration_manifest.json` with dataset sizes and source/target mapping
	3. Emit `env.example` with `MOM_DB_*` variables
	4. Optional `--copy` to physically copy datasets

```bash
# Plan only (no copy)
python -m src.data.prepare_database_split --target-root D:/mom_db_storage

# Plan + copy data
python -m src.data.prepare_database_split --target-root D:/mom_db_storage --copy
```

### Recommended Split Layout

```text
D:/mom_db_storage/
├── data/
│   ├── duckdb/
│   │   └── main.duckdb
│   ├── filtered/
│   ├── daily/
│   ├── minute/
│   ├── second10/
│   ├── quote_data/
│   ├── trade_data/
│   ├── momentum_events/
│   ├── metadata/
│   ├── market-hours/
│   ├── symbol-properties/
│   └── nautilus_catalog/
├── migration_manifest.json
└── env.example
```

### Environment Variables for Split Mode

- `MOM_DB_DATA_ROOT`
- `MOM_DB_DATABASE_ROOT`
- `MOM_DB_DUCKDB_PATH`

These let the same codebase run unchanged whether storage is local to this repo or external.

## Known Gaps / Follow-up Needed

The following datasets are present on disk but currently lack writer provenance in this checkout:

- `daily/`
- `minute/`
- `second10/`
- `quote_data/`
- Most of `trade_data/` generation flow
- `metadata/`, `market-hours/`, `symbol-properties/`
- `nautilus_catalog/` build process

If an external or missing archive exists, reconcile these entries there and update this file from **Inferred/Unknown** to **Confirmed** with script paths.
