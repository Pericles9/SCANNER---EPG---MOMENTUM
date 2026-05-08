---
tags:
  - type/implementation
  - domain/data
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-04-04
last_reviewed: 2026-04-04
linked_code: "[[client.py]]"
---

# data/client.py

## Purpose
DuckDB connection manager for the `hawkes-ofi-impact` data layer. Provides a single module-level connection configured for HPC workloads (10 threads, 24 GB memory limit) and a utility for reading specific parquet columns directly into numpy arrays via DuckDB.

## Key Functions / Classes
| Name | Type | Description |
|------|------|-------------|
| `get_connection()` | function | Returns a configured, reusable `DuckDBPyConnection` |
| `read_parquet_to_numpy(path, columns)` | function | Reads validated columns from a parquet file into numpy arrays, sorted by `sip_timestamp` |
| `_VALID_COLUMNS` | constant | Allowlist of permitted column names (injection guard) |

## Inputs / Outputs
- `get_connection()`: no inputs; returns the module-level singleton connection
- `read_parquet_to_numpy(path, columns)`: path string + list of column names → `dict` of numpy arrays keyed by column name

## Dependencies
- `duckdb`
- `data/schemas/mom_db.py` (for `DATA_ROOT`)

## Usage Example
```python
from data.client import get_connection, read_parquet_to_numpy

con = get_connection()

arrays = read_parquet_to_numpy(
    "data/filtered/AAPL_2024-01-15_52.3/trades.parquet",
    ["sip_timestamp", "price", "size"],
)
timestamps = arrays["sip_timestamp"]
```

## Notes
- Column names are validated against `_VALID_COLUMNS` before query construction — raises `ValueError` on unrecognized names.
- Connection is a process-level singleton; safe within a single process (DuckDB is internally thread-safe). Use separate processes for parallelism.
- Thread/memory config: `SET threads = 10`, `SET memory_limit = '24GB'` per HPC spec Section 1.2. Adjust if running on a different machine.
- Most calibration code uses `data/loaders/trades.py` and `data/loaders/quotes.py` directly via pyarrow rather than this client.

## Related
- [[data/Schema.md]] — column names and types
- [[Trade Loader]] — higher-level loader using pyarrow directly
- [[Quote Loader]] — higher-level loader using pyarrow directly
