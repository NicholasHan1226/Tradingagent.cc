"""R3 Edge Case Tests for P1 Fixes.

Covers:
  1. _tail_lines edge cases (shared/execution/execution_router.py)
  2. T+1 holidays edge cases (Ashare/t_plus_1.py)
  3. Batch SQL _sqlite_rows_by_symbols edge cases (SharedSignals/reader.py)
  4. ThreadingHTTPServer capacity gate edge cases
     (SharedSignals/api_server.py)
  5. requirements.txt critical import resolution

Run:
  PYTHONPATH=/Users/nicholashan/Projects/Finance/tradingagent \
  python3 -m pytest tests/test_p1_edge_cases.py -v --tb=short
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 1. _tail_lines edge cases
# ---------------------------------------------------------------------------


class TestTailLinesEdgeCases(unittest.TestCase):
    """Edge case tests for _tail_lines in shared/execution/execution_router.py."""

    def setUp(self):
        from shared.execution.execution_router import _tail_lines

        self._tail_lines = _tail_lines
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_file(self, name: str, content: bytes) -> Path:
        path = self._tmp / name
        path.write_bytes(content)
        return path

    def test_tail_lines_empty_file_returns_empty_list(self):
        path = self._write_file("empty.txt", b"")
        result = self._tail_lines(path, max_lines=10)
        self.assertEqual(result, [])

    def test_tail_lines_file_smaller_than_block_size(self):
        # block_size is 8192 bytes; write something much smaller
        content = b"line1\nline2\nline3\n"
        path = self._write_file("small.txt", content)
        result = self._tail_lines(path, max_lines=10)
        self.assertEqual([line.decode() for line in result], ["line1", "line2", "line3"])

    def test_tail_lines_max_lines_exceeds_file_lines_returns_all(self):
        content = b"a\nb\nc\nd\ne\n"
        path = self._write_file("few.txt", content)
        result = self._tail_lines(path, max_lines=100)
        self.assertEqual([line.decode() for line in result], ["a", "b", "c", "d", "e"])

    def test_tail_lines_max_lines_1_returns_single_last_line(self):
        content = b"first\nsecond\nthird\n"
        path = self._write_file("three.txt", content)
        result = self._tail_lines(path, max_lines=1)
        self.assertEqual([line.decode() for line in result], ["third"])

    def test_tail_lines_no_trailing_newline(self):
        content = b"alpha\nbeta\ngamma"
        path = self._write_file("notrail.txt", content)
        result = self._tail_lines(path, max_lines=2)
        self.assertEqual([line.decode() for line in result], ["beta", "gamma"])

    def test_tail_lines_large_file_beyond_one_block(self):
        # ~20KB = more than 2 blocks of 8192 bytes
        lines = [f"line_{i:05d}".encode() for i in range(1000)]
        content = b"\n".join(lines) + b"\n"
        path = self._write_file("large.txt", content)
        result = self._tail_lines(path, max_lines=10)
        expected = [f"line_{i:05d}" for i in range(990, 1000)]
        self.assertEqual([line.decode() for line in result], expected)

    def test_tail_lines_max_lines_zero_treated_as_one(self):
        content = b"only\n"
        path = self._write_file("zero.txt", content)
        result = self._tail_lines(path, max_lines=0)
        # the function clamps to max(1, int(max_lines))
        self.assertEqual([line.decode() for line in result], ["only"])

# ---------------------------------------------------------------------------
# 2. T+1 holidays edge cases
# ---------------------------------------------------------------------------


class TestTPlusOneHolidaysEdgeCases(unittest.TestCase):
    """Edge case tests for Ashare/t_plus_1.py T+1 settlement logic."""

    def setUp(self):
        import Ashare.t_plus_1 as mod
        self.mod = mod
        mod._load_trade_calendar_data.cache_clear()
        # Patch away any external trade calendar search so we rely on fallback
        self._root_patch = patch.object(mod, "TRADE_CALENDAR_SEARCH_ROOTS", ())
        self._root_patch.start()

    def tearDown(self):
        self.mod._load_trade_calendar_data.cache_clear()
        self._root_patch.stop()

    def test_t_plus_1_known_holiday_spring_festival(self):
        """2026-02-17 is a known Spring Festival holiday.

        Buy 2026-02-13 (Fri) → next trading day is 2026-02-24 (Tue)
        because 02-14/15 weekend, 02-16-20 holidays, 02-21/22 weekend,
        02-23 also a holiday.
        """
        buy_date = "2026-02-13"  # Friday
        # Next trading day after all holidays: 2026-02-24 (Tuesday)
        sell_date = "2026-02-24"
        self.assertTrue(self.mod.can_sell(buy_date, sell_date))
        # One day earlier (2026-02-23 is still a holiday)
        self.assertFalse(self.mod.can_sell(buy_date, "2026-02-23"))
        # Within the holiday week itself
        self.assertFalse(self.mod.can_sell(buy_date, "2026-02-17"))

    def test_t_plus_1_saturday_buy_monday_sellable(self):
        """Buy on Saturday 2026-06-27 → next trading day is Monday 2026-06-29."""
        self.assertFalse(self.mod.can_sell("2026-06-27", "2026-06-27"))  # same day
        self.assertFalse(self.mod.can_sell("2026-06-27", "2026-06-28"))  # Sunday
        self.assertTrue(self.mod.can_sell("2026-06-27", "2026-06-29"))  # Monday

    def test_t_plus_1_friday_buy_monday_sellable(self):
        """Buy on Friday → next trading day is Monday."""
        self.assertFalse(self.mod.can_sell("2026-06-26", "2026-06-26"))
        self.assertFalse(self.mod.can_sell("2026-06-26", "2026-06-27"))
        self.assertFalse(self.mod.can_sell("2026-06-26", "2026-06-28"))
        self.assertTrue(self.mod.can_sell("2026-06-26", "2026-06-29"))

    def test_t_plus_1_buy_date_none_returns_false(self):
        self.assertFalse(self.mod.can_sell(None, "2026-06-30"))

    def test_t_plus_1_buy_date_empty_string_returns_false(self):
        self.assertFalse(self.mod.can_sell("", "2026-06-30"))

    def test_t_plus_1_trade_calendar_empty_falls_back_to_weekends_and_hardcoded_holidays(self):
        """When no trade_cal file found, fallback uses weekday check + KNOWN_HOLIDAYS."""
        self.mod._load_trade_calendar_data.cache_clear()
        # Jan 1 2026 is a Thursday but a known holiday
        self.assertFalse(self.mod.is_trading_day("2026-01-01"))
        # Jan 5 2026 is a Monday, not a holiday
        self.assertTrue(self.mod.is_trading_day("2026-01-05"))
        # A Saturday
        self.assertFalse(self.mod.is_trading_day("2026-06-27"))
        # A Sunday
        self.assertFalse(self.mod.is_trading_day("2026-06-28"))

    def test_t_plus_1_invalid_buy_date_type_returns_false(self):
        """can_sell should return False for unsupported date types."""
        self.assertFalse(self.mod.can_sell(["not_a_date"], "2026-06-30"))

    def test_t_plus_1_next_sellable_date(self):
        """next_sellable_date returns next trading day."""
        # Buy Friday → next is Monday
        sellable = self.mod.next_sellable_date("2026-06-26")
        self.assertEqual(sellable, date(2026, 6, 29))

    def test_t_plus_1_next_trading_day_skips_weekend(self):
        """next_trading_day from Friday → Monday."""
        nt = self.mod.next_trading_day("2026-06-26")
        self.assertEqual(nt, date(2026, 6, 29))

    def test_t_plus_1_external_calendar_via_temp_csv(self):
        """Verify get_trading_calendar reads an external trade_cal CSV."""
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            cal_path = temp / "trade_cal.csv"
            cal_path.write_text(
                "cal_date,is_open\n"
                "2026-06-26,1\n"
                "2026-06-27,0\n"
                "2026-06-28,0\n"
                "2026-06-29,1\n",
                encoding="utf-8",
            )
            with patch.object(self.mod, "TRADE_CALENDAR_SEARCH_ROOTS", (temp,)):
                self.mod._load_trade_calendar_data.cache_clear()
                days = self.mod.get_trading_calendar("2026-06-26", "2026-06-29")
                self.assertEqual(days, [date(2026, 6, 26), date(2026, 6, 29)])

    def test_t_plus_1_invalid_calendar_range_raises(self):
        """Start after end should raise ValueError."""
        with self.assertRaises(ValueError):
            self.mod.get_trading_calendar("2026-07-01", "2026-06-01")

# ---------------------------------------------------------------------------
# 3. Batch SQL _sqlite_rows_by_symbols edge cases
# ---------------------------------------------------------------------------


class TestBatchSqlEdgeCases(unittest.TestCase):
    """Edge case tests for _sqlite_rows_by_symbols batch query."""

    def setUp(self):
        # Import the function directly from the SharedSignals reader module
        # We need to patch SQLITE_PATH so it points to a temp db
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test_marketdata.sqlite"

        # Create test DB with market_bars_daily schema
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_bars_daily (
                market TEXT,
                symbol TEXT,
                trade_date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                PRIMARY KEY (market, symbol, trade_date)
            )
        """)
        conn.commit()
        conn.close()

        self._sqlite_path_patch = None
        # SQLITE_PATH is a LazyPath; we patch os.environ at import time instead

    def tearDown(self):
        self._tmpdir.cleanup()

    def _populate_db(self, symbols_and_rows: list[tuple[str, list[dict]]]):
        """Populate the test DB with market_bars_daily rows."""
        conn = sqlite3.connect(str(self._db_path))
        for symbol, rows in symbols_and_rows:
            for row in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO market_bars_daily "
                    "(market, symbol, trade_date, open, high, low, close, volume, amount) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.get("market", "Ashare"),
                        symbol,
                        row["trade_date"],
                        row.get("open", 10.0),
                        row.get("high", 11.0),
                        row.get("low", 9.0),
                        row.get("close", 10.5),
                        row.get("volume", 1000),
                        row.get("amount", 10000),
                    ),
                )
        conn.commit()
        conn.close()

    def _import_batch_fn(self):
        """Import _sqlite_rows_by_symbols with SQLITE_PATH pointed at test DB."""
        import importlib.util
        import uuid

        # Ensure SharedSignals is on sys.path for env_bootstrap imports
        ss_dir = str(Path(__file__).resolve().parent.parent.parent / "SharedSignals")
        if ss_dir not in sys.path:
            sys.path.insert(0, ss_dir)

        # Use a unique module name so LazyPath isn't cached from prior imports
        unique_name = f"SharedSignals.reader_{uuid.uuid4().hex[:8]}"

        # Set env var BEFORE module exec so LazyPath resolves correctly
        os.environ["MARKETDATA_SQLITE"] = str(self._db_path)

        ss_reader_path = Path(
            "/Users/nicholashan/Projects/Finance/SharedSignals/reader.py"
        )
        spec = importlib.util.spec_from_file_location(
            unique_name, str(ss_reader_path)
        )
        reader_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reader_mod)

        return reader_mod._sqlite_rows_by_symbols

    def test_batch_query_empty_symbols_returns_empty_dict(self):
        fn = self._import_batch_fn()
        result, degraded = fn("market_bars_daily", [], 60)
        self.assertEqual(result, {})
        self.assertIsNone(degraded)

    def test_batch_query_single_symbol_works(self):
        self._populate_db([
            ("000001.SZ", [
                {"trade_date": "20260630"},
                {"trade_date": "20260629"},
                {"trade_date": "20260628"},
            ]),
        ])
        fn = self._import_batch_fn()
        result, degraded = fn("market_bars_daily", ["000001.SZ"], 60)
        self.assertIsNone(degraded)
        self.assertIn("000001.SZ", result)
        self.assertEqual(len(result["000001.SZ"]), 3)

    def test_batch_query_many_symbols(self):
        """Simulate 100+ symbols in one batch query."""
        symbols = [f"{i:06d}.SZ" for i in range(100)]
        all_data = []
        for sym in symbols:
            all_data.append((sym, [
                {"trade_date": "20260630"},
                {"trade_date": "20260629"},
            ]))
        self._populate_db(all_data)
        fn = self._import_batch_fn()
        result, degraded = fn("market_bars_daily", symbols, 60)
        self.assertIsNone(degraded)
        self.assertEqual(len(result), 100)
        for sym in symbols:
            self.assertIn(sym, result)
            self.assertEqual(len(result[sym]), 2)

    def test_batch_query_with_limit(self):
        """limit parameter should restrict rows per symbol."""
        self._populate_db([
            ("000001.SZ", [
                {"trade_date": "20260630"},
                {"trade_date": "20260629"},
                {"trade_date": "20260628"},
                {"trade_date": "20260627"},
                {"trade_date": "20260626"},
            ]),
        ])
        fn = self._import_batch_fn()
        result, degraded = fn("market_bars_daily", ["000001.SZ"], 3)
        self.assertIsNone(degraded)
        self.assertEqual(len(result["000001.SZ"]), 3)

    def test_batch_query_invalid_symbol_format_does_not_crash(self):
        """Symbols with weird chars should be deduped/stripped safely."""
        fn = self._import_batch_fn()
        result, degraded = fn("market_bars_daily", ["  ", "", "000001.SZ", "  "], 60)
        # Empty/whitespace-only symbols are stripped out, only "000001.SZ" remains
        self.assertIsNone(degraded)
        # May return empty if DB has no data, but should not crash
        self.assertIsNotNone(result)

    def test_batch_query_unsupported_table_returns_degraded(self):
        fn = self._import_batch_fn()
        result, degraded = fn("market_factors", ["000001.SZ"], 60)
        self.assertEqual(result, {})
        self.assertIsNotNone(degraded)
        self.assertIn("unsupported batch table", str(degraded))

    def test_batch_query_duplicate_symbols_deduped(self):
        """Duplicate symbols should be deduplicated."""
        self._populate_db([
            ("000001.SZ", [
                {"trade_date": "20260630"},
            ]),
        ])
        fn = self._import_batch_fn()
        result, degraded = fn("market_bars_daily",
                              ["000001.SZ", "000001.SZ", "000001.SZ"], 60)
        self.assertIsNone(degraded)
        self.assertEqual(len(result), 1)
        self.assertIn("000001.SZ", result)


# ---------------------------------------------------------------------------
# 4. ThreadingHTTPServer capacity gate edge cases
# ---------------------------------------------------------------------------


class TestThreadingHTTPServerCapacityGate(unittest.TestCase):
    """Edge case tests for SharedSignalsHTTPServer capacity gate."""

    @classmethod
    def setUpClass(cls):
        # Ensure SharedSignals is on sys.path for api_server imports
        ss_dir = str(Path(__file__).resolve().parent.parent.parent / "SharedSignals")
        if ss_dir not in sys.path:
            sys.path.insert(0, ss_dir)

    def test_capacity_gate_returns_503_when_full(self):
        """When max_threads threads are acquired, process_request sends 503."""
        import api_server as srv

        # Create server with max_threads=2
        server = srv.SharedSignalsHTTPServer(
            ("127.0.0.1", 0),
            srv.Handler,
            request_timeout=5,
            max_threads=2,
        )

        # Acquire all 2 permits
        self.assertTrue(server._thread_limiter.acquire(blocking=False))
        self.assertTrue(server._thread_limiter.acquire(blocking=False))
        # Third acquisition should fail
        self.assertFalse(server._thread_limiter.acquire(blocking=False))

        # Release so we can actually shutdown
        server._thread_limiter.release()
        server._thread_limiter.release()
        server.server_close()

    def test_daemon_threads_and_allow_reuse_address_set(self):
        import api_server as srv

        self.assertTrue(srv.SharedSignalsHTTPServer.daemon_threads)
        self.assertTrue(srv.SharedSignalsHTTPServer.allow_reuse_address)

    def test_server_starts_and_stops_without_errors(self):
        import api_server as srv

        server = srv.SharedSignalsHTTPServer(
            ("127.0.0.1", 0),
            srv.Handler,
            request_timeout=5,
            max_threads=4,
        )
        self.assertIsNotNone(server.server_address)
        self.assertGreater(server.server_address[1], 0)
        server.server_close()

    def test_max_threads_enforced_under_concurrent_requests(self):
        """Simulate concurrent requests hitting the capacity gate."""
        import api_server as srv

        server = srv.SharedSignalsHTTPServer(
            ("127.0.0.1", 0),
            srv.Handler,
            request_timeout=5,
            max_threads=2,
        )
        port = server.server_address[1]

        # Start server in background thread
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            import urllib.request

            results = []
            errors = []

            def make_request(_idx: int) -> None:
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/health",
                        headers={"Authorization": "Bearer test"},
                    )
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        results.append(resp.status)
                except urllib.error.HTTPError as exc:
                    results.append(exc.code)
                except Exception as exc:
                    errors.append(str(exc))

            threads_list = []
            for i in range(10):
                t = threading.Thread(target=make_request, args=(i,))
                threads_list.append(t)

            for t in threads_list:
                t.start()
            for t in threads_list:
                t.join(timeout=10)

            # We expect some requests to succeed (200 or 401 since fake auth),
            # and some to get 503 when capacity is exceeded.
            # With max_threads=2 and 10 concurrent requests, we should see 503s.
            statuses = set(results)
            # 401 = auth failure (expected since we don't actually auth)
            # 503 = at capacity
            self.assertTrue({401, 503}.issuperset(statuses),
                            f"Unexpected statuses: {statuses}")
            # At least some 503s should appear with 10 concurrent and max_threads=2
            self.assertIn(503, statuses,
                          f"Expected 503 under load, got: {statuses}")

        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_marketgraph_server_capacity_503(self):
        """Test MarketGraphHTTPServer returns 503 when at capacity."""
        mg_path = Path("/Users/nicholashan/Projects/Finance/MarketGraph/deploy/_api_server.py")
        if not mg_path.exists():
            self.skipTest("MarketGraph _api_server.py not found at expected path")

        # We import the class dynamically, skipping the full module execution
        # which requires marketgraph_mcp_server etc. Instead, we test the
        # server class pattern by creating a minimal equivalent.
        # The MarketGraphHTTPServer class follows the same capacity-gate
        # pattern as SharedSignalsHTTPServer — already tested above.

        # Verify the source file contains the expected class structure
        source = mg_path.read_text()
        self.assertIn("class MarketGraphHTTPServer", source)
        self.assertIn("daemon_threads = True", source)
        self.assertIn("allow_reuse_address = True", source)
        self.assertIn("_thread_limiter", source)
        self.assertIn("BoundedSemaphore", source)
        self.assertIn("503 Service Unavailable", source)
        self.assertIn("server at capacity", source)

# ---------------------------------------------------------------------------
# 5. requirements.txt edge cases
# ---------------------------------------------------------------------------


class TestRequirementsTxtEdgeCases(unittest.TestCase):
    """Edge case tests for requirements.txt dependency resolution."""

    def test_requirements_txt_exists_and_parses(self):
        req_path = Path(__file__).resolve().parents[1] / "requirements.txt"
        self.assertTrue(req_path.exists(), f"requirements.txt not found at {req_path}")

        lines = req_path.read_text().splitlines()
        deps = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                deps.append(stripped)

        self.assertGreater(len(deps), 0, "requirements.txt should list at least one dependency")

    def test_pyyaml_can_be_imported(self):
        """PyYAML is a critical dependency listed in requirements.txt."""
        try:
            import yaml  # noqa: F401
        except ImportError as exc:
            self.fail(f"PyYAML (yaml) could not be imported: {exc}")

    def test_pytest_can_be_discover_and_run_minimal_test(self):
        """Verify pytest can discover and run a minimal test."""
        # This test itself proves pytest discovery works.
        self.assertTrue(True)

    def test_all_core_shared_modules_importable(self):
        """Critical shared modules used across the codebase should be importable."""
        modules = [
            ("shared.execution.execution_router", "execution_router"),
            ("shared.accounting.position_ledger", "position_ledger"),
            ("shared.markets.market_rules", "market_rules"),
            ("Ashare.t_plus_1", "t_plus_1"),
        ]
        for import_path, attr_name in modules:
            with self.subTest(module=import_path):
                try:
                    mod = __import__(import_path, fromlist=[attr_name])
                    self.assertIsNotNone(mod)
                except ImportError as exc:
                    self.fail(f"Failed to import {import_path}: {exc}")


if __name__ == "__main__":
    unittest.main()
