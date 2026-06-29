"""Regression tests for backtest/data/loaders/prev_close.py."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# ---------------------------------------------------------------------------
# Import under test — the module resolves DATA_ROOT relative to its own path,
# so we patch FILTERED_DIR after import to point at our temp directory.
# ---------------------------------------------------------------------------
import importlib
import data.loaders.prev_close as _mod


def _write_trades(path: Path, timestamps_ns: list[int], prices: list[float]):
    table = pa.table(
        {
            "sip_timestamp": pa.array(timestamps_ns, type=pa.int64()),
            "price": pa.array(prices, type=pa.float64()),
            "size": pa.array([1] * len(prices), type=pa.int64()),
        }
    )
    pq.write_table(table, str(path))


def _ns_for_et(date_str: str, hour: int, minute: int = 0) -> int:
    """Return unix nanoseconds for HH:MM ET on date_str, using the same DST logic as the module."""
    from datetime import date as dt_date

    d = dt_date.fromisoformat(date_str)
    year, month, day = d.year, d.month, d.day
    midnight_utc_s = int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
    if 3 < month < 11 or (month == 3 and day >= 8) or (month == 11 and day < 7):
        et_offset_s = 4 * 3600  # EDT
    else:
        et_offset_s = 5 * 3600  # EST
    return (midnight_utc_s + hour * 3600 + minute * 60 + et_offset_s) * 1_000_000_000


class TestTryPriorTradesParquet:
    """Test that _try_prior_trades_parquet uses a 16:00 ET cutoff, not 20:00 ET."""

    def test_returns_last_trade_before_1600(self, tmp_path, monkeypatch):
        """Trade at 15:55 ET should be returned; trade at 16:05 ET should be excluded."""
        # Create a fake prior-event directory: AAAA_2024-01-10_MOM
        prior_dir = tmp_path / "AAAA_2024-01-10_MOM"
        prior_dir.mkdir()

        # Two trades on 2024-01-10: one before 16:00, one after
        ts_1555 = _ns_for_et("2024-01-10", 15, 55)  # 15:55 ET — should be returned
        ts_1605 = _ns_for_et("2024-01-10", 16, 5)   # 16:05 ET — should be excluded

        _write_trades(
            prior_dir / "trades.parquet",
            timestamps_ns=[ts_1555, ts_1605],
            prices=[42.0, 99.0],  # 99.0 is the post-market price; must NOT be returned
        )

        monkeypatch.setattr(_mod, "FILTERED_DIR", tmp_path)

        # Event date is 2024-01-11, so 2024-01-10 is a prior date
        result = _mod._try_prior_trades_parquet("AAAA", "2024-01-11")
        assert result == pytest.approx(42.0), (
            f"Expected 42.0 (last trade before 16:00 ET) but got {result}. "
            "Post-market trade at 16:05 ET must be excluded by the 16:00 cutoff."
        )

    def test_excludes_all_trades_at_or_after_1600(self, tmp_path, monkeypatch):
        """If only trades at 16:00+ ET exist, function should return None (or try next candidate)."""
        prior_dir = tmp_path / "BBBB_2024-03-14_MOM"
        prior_dir.mkdir()

        ts_1600 = _ns_for_et("2024-03-14", 16, 0)   # exactly 16:00 ET — excluded
        ts_1700 = _ns_for_et("2024-03-14", 17, 0)   # 17:00 ET — excluded

        _write_trades(
            prior_dir / "trades.parquet",
            timestamps_ns=[ts_1600, ts_1700],
            prices=[50.0, 55.0],
        )

        monkeypatch.setattr(_mod, "FILTERED_DIR", tmp_path)

        result = _mod._try_prior_trades_parquet("BBBB", "2024-03-15")
        # No trade before 16:00 ET — should return None (no candidates resolve)
        assert result is None, (
            f"Expected None when all trades are at/after 16:00 ET but got {result}."
        )

    def test_post_market_trade_not_returned(self, tmp_path, monkeypatch):
        """Explicitly verify a 20:00 ET trade is excluded (regression against old 20h cutoff)."""
        prior_dir = tmp_path / "CCCC_2024-06-04_MOM"
        prior_dir.mkdir()

        ts_1500 = _ns_for_et("2024-06-04", 15, 0)   # 15:00 ET — should be returned
        ts_2000 = _ns_for_et("2024-06-04", 20, 0)   # 20:00 ET — post-market, must NOT be returned

        _write_trades(
            prior_dir / "trades.parquet",
            timestamps_ns=[ts_1500, ts_2000],
            prices=[10.0, 25.0],  # 25.0 is inflated PM price; must not be returned
        )

        monkeypatch.setattr(_mod, "FILTERED_DIR", tmp_path)

        result = _mod._try_prior_trades_parquet("CCCC", "2024-06-05")
        assert result == pytest.approx(10.0), (
            f"Expected 10.0 (last RTH trade at 15:00 ET) but got {result}. "
            "The old 20:00 ET cutoff would have returned 25.0 — regression check."
        )
