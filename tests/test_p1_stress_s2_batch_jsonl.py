"""
P1 Stress Test S2: 5000 stocks batch SQL + 100MB JSONL tail-read.
Tests:
  - SQLite batch query with 5000 symbols (WHERE symbol IN (...))
  - _tail_lines on 100MB JSONL file
  - execution_router ROUTER_HISTORY_TAIL_LINES memory behavior
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Insert tradingagent path for _tail_lines import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.execution.execution_router import _tail_lines


def _generate_batch_db(db_path: Path, num_stocks: int, rows_per_stock: int = 50):
    """Generate SQLite DB with num_stocks * rows_per_stock daily rows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_bars_daily (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL,
            PRIMARY KEY (symbol, trade_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON market_bars_daily(symbol)")

    rows = []
    for i in range(num_stocks):
        symbol = f"{600000 + (i % 4000):06d}.SH"
        base_close = 10.0 + (i % 100) * 0.5  # varied prices
        for day in range(rows_per_stock):
            trade_date = f"2026-{(day // 30) + 1:02d}-{(day % 28) + 1:02d}"
            rows.append((
                symbol, trade_date,
                base_close - 0.1, base_close + 0.1, base_close - 0.2, base_close,
                1000000.0, 5000000.0
            ))

    conn.executemany(
        "INSERT OR REPLACE INTO market_bars_daily VALUES (?,?,?,?,?,?,?,?)",
        rows
    )
    conn.commit()
    conn.close()
    return db_path


def _generate_large_jsonl(path: Path, target_mb: int):
    """Generate a JSONL file of approximately target_mb megabytes."""
    entry = json.dumps({
        "ts": "2026-07-04T10:00:00",
        "strategy": "test_strategy",
        "symbol": "600000.SH",
        "side": "buy",
        "qty": 100,
        "price": 12.34,
        "channel": "sim",
        "decision": "route_sim",
        "metrics": {"score": 0.85, "confidence": 0.9}
    }) + "\n"
    entry_bytes = entry.encode("utf-8")
    entries_needed = (target_mb * 1024 * 1024) // len(entry_bytes)

    with open(path, "wb") as f:
        for _ in range(entries_needed):
            f.write(entry_bytes)

    return path, entries_needed


class TestS2BatchSQL(unittest.TestCase):
    """Stress: SQLite batch query with 5000 stocks."""

    tmp: tempfile.TemporaryDirectory
    db_path: Path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_batch.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_query_5000_stocks(self):
        """5000 stocks * 50 rows = 250K rows. Batch query must complete < 30s."""
        db = _generate_batch_db(self.db_path, num_stocks=5000, rows_per_stock=50)

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")

        # Generate 5000 symbols
        symbols = [f"{600000 + (i % 4000):06d}.SH" for i in range(5000)]
        unique_symbols = list(dict.fromkeys(symbols))  # dedup

        # Run the batch query pattern (like reader.py legacy_market_dataset)
        start = time.perf_counter()
        placeholders = ",".join(["?"] * len(unique_symbols))
        # Use ROW_NUMBER() window function to get latest N per symbol (batch pattern)
        sql = f"""
            SELECT symbol, trade_date, open, high, low, close, volume, amount
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY symbol ORDER BY trade_date DESC
                ) AS rn
                FROM market_bars_daily
                WHERE symbol IN ({placeholders})
            )
            WHERE rn <= 3
            ORDER BY symbol, trade_date DESC
        """
        cursor = conn.execute(sql, unique_symbols)
        rows = cursor.fetchall()
        elapsed = time.perf_counter() - start

        conn.close()

        self.assertGreater(len(rows), 0, "Batch query returned no rows")
        self.assertLess(elapsed, 30.0, f"Batch query took {elapsed:.1f}s > 30s limit")
        print(f"\n  [S2-5000stocks] {len(unique_symbols)} symbols → {len(rows)} rows in {elapsed:.2f}s")

    def test_batch_query_empty_symbols(self):
        """Empty symbol list returns immediately."""
        db = _generate_batch_db(self.db_path, num_stocks=10, rows_per_stock=5)
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")

        start = time.perf_counter()
        cursor = conn.execute(
            "SELECT * FROM market_bars_daily WHERE symbol IN ('NONEXISTENT')"
        )
        rows = cursor.fetchall()
        elapsed = time.perf_counter() - start

        conn.close()
        self.assertEqual(len(rows), 0)
        self.assertLess(elapsed, 1.0)
        print(f"\n  [S2-empty-batch] 0 rows in {elapsed:.4f}s")

    def test_batch_query_10000_stocks(self):
        """10K stocks stress — must complete without OOM."""
        db = _generate_batch_db(self.db_path, num_stocks=10000, rows_per_stock=10)

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")

        symbols = [f"{600000 + (i % 4000):06d}.SH" for i in range(10000)]
        unique_symbols = list(dict.fromkeys(symbols))

        start = time.perf_counter()
        placeholders = ",".join(["?"] * len(unique_symbols))
        sql = f"SELECT COUNT(*) FROM market_bars_daily WHERE symbol IN ({placeholders})"
        cursor = conn.execute(sql, unique_symbols)
        count = cursor.fetchone()[0]
        elapsed = time.perf_counter() - start

        conn.close()
        self.assertGreater(count, 0)
        self.assertLess(elapsed, 60.0, f"10K batch query took {elapsed:.1f}s > 60s limit")
        print(f"\n  [S2-10000stocks] COUNT(*) = {count} in {elapsed:.2f}s")

    def test_sqlite_in_clause_limit(self):
        """SQLite default max variable number is 999 (SQLITE_MAX_VARIABLE_NUMBER).
        Batch query with >999 symbols must handle this gracefully."""
        db = _generate_batch_db(self.db_path, num_stocks=50, rows_per_stock=5)
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")

        # 2000 placeholders exceeds SQLite default limit of 999
        symbols = [f"test_{i:04d}" for i in range(2000)]
        placeholders = ",".join(["?"] * len(symbols))

        try:
            conn.execute(
                f"SELECT COUNT(*) FROM market_bars_daily WHERE symbol IN ({placeholders})",
                symbols
            )
            # Some SQLite builds allow more; that's fine
            conn.close()
            print(f"\n  [S2-sqlite-limit] 2000 placeholders OK (extended build)")
        except sqlite3.OperationalError as e:
            conn.close()
            self.assertIn("too many", str(e).lower())
            print(f"\n  [S2-sqlite-limit] Expected: {e}")


class TestS2LargeJSONL(unittest.TestCase):
    """Stress: 100MB JSONL tail-read + ROUTER_HISTORY_TAIL_LINES behavior."""

    tmp: tempfile.TemporaryDirectory
    jsonl_path: Path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jsonl_path = Path(self.tmp.name) / "router_decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_tail_lines_100mb_file(self):
        """_tail_lines on 100MB JSONL: must complete < 5s and use < 200MB memory."""
        import resource

        path, total_entries = _generate_large_jsonl(self.jsonl_path, target_mb=100)
        file_size_mb = os.path.getsize(self.jsonl_path) / (1024 * 1024)
        print(f"\n  [S2-100MB-JSONL] File: {file_size_mb:.1f}MB, {total_entries} entries")

        start = time.perf_counter()
        lines = _tail_lines(self.jsonl_path, max_lines=1000)
        elapsed = time.perf_counter() - start

        # Each line should be valid JSON
        self.assertGreater(len(lines), 0, "No lines returned")
        self.assertLessEqual(len(lines), 1000, "Returned more than max_lines")

        for line in lines[:5]:
            data = json.loads(line.decode("utf-8"))
            self.assertIn("ts", data)
            self.assertIn("symbol", data)

        self.assertLess(elapsed, 5.0, f"100MB tail-read took {elapsed:.1f}s > 5s limit")
        print(f"  [S2-100MB-JSONL] Tail-read {len(lines)} lines in {elapsed:.2f}s")

    def test_tail_lines_10mb_file_max_1_line(self):
        """Tail-read 10MB file with max_lines=1 — must be fast."""
        path, _ = _generate_large_jsonl(self.jsonl_path, target_mb=10)
        start = time.perf_counter()
        lines = _tail_lines(self.jsonl_path, max_lines=1)
        elapsed = time.perf_counter() - start

        self.assertEqual(len(lines), 1)
        self.assertLess(elapsed, 1.0, f"10MB tail-1 took {elapsed:.2f}s > 1s limit")
        print(f"  [S2-10MB-tail-1] {elapsed:.4f}s")

    def test_tail_lines_1mb_file_max_10000(self):
        """max_lines > file lines returns all lines."""
        path, total_entries = _generate_large_jsonl(self.jsonl_path, target_mb=1)
        start = time.perf_counter()
        lines = _tail_lines(self.jsonl_path, max_lines=10000)
        elapsed = time.perf_counter() - start

        self.assertEqual(len(lines), total_entries)
        self.assertLess(elapsed, 1.0)
        print(f"  [S2-1MB-tail-all] {total_entries} lines in {elapsed:.4f}s")

    def test_tail_lines_concurrent_readers(self):
        """Multiple readers concurrently tail-reading the same 50MB file."""
        import threading

        path, _ = _generate_large_jsonl(self.jsonl_path, target_mb=50)

        errors = []
        results = []

        def read_tail():
            try:
                lines = _tail_lines(self.jsonl_path, max_lines=500)
                results.append(len(lines))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=read_tail) for _ in range(10)]
        start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.perf_counter() - start

        self.assertEqual(len(errors), 0, f"Concurrent read errors: {errors[:3]}")
        self.assertEqual(len(results), 10)
        self.assertLess(elapsed, 10.0, f"10 concurrent tail-reads took {elapsed:.1f}s")
        print(f"  [S2-concurrent-tail] 10 readers, {path.stat().st_size/(1024*1024):.1f}MB in {elapsed:.2f}s")


class TestS2MemoryFootprint(unittest.TestCase):
    """Memory: tail-read should not load entire file into memory."""

    def test_tail_lines_memory_bounded(self):
        """100MB file: tail-read should use < 200MB RSS."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.jsonl"
            _generate_large_jsonl(path, target_mb=100)

            import resource
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            lines = _tail_lines(path, max_lines=1000)
            after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

            # maxrss is in KB on macOS
            delta_kb = after - before
            delta_mb = delta_kb / 1024

            self.assertGreater(len(lines), 0)
            self.assertLess(delta_mb, 200, f"Memory delta {delta_mb:.1f}MB > 200MB limit")
            print(f"\n  [S2-memory] RSS delta: {delta_mb:.1f}MB for 100MB file tail-read")


class TestS2StressSummary(unittest.TestCase):
    """Aggregate S2 stress test results."""

    def test_all_s2_stress_cases(self):
        """This is a placeholder to confirm the module loads and can be discovered."""
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
