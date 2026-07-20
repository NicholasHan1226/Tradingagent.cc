"""R3 Edge Case Tests for P1 Fixes.

Covers:
  1. _tail_lines edge cases (shared/execution/execution_router.py)
  2. T+1 holidays edge cases (Ashare/t_plus_1.py)
  3. requirements.txt critical import resolution

Run:
  PYTHONPATH=/Users/nicholashan/Projects/Finance/TradingAgent \
  python3 -m pytest tests/test_p1_edge_cases.py -v --tb=short
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(
            [line.decode() for line in result], ["line1", "line2", "line3"]
        )

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
        # The retired reader path is absent; these tests exercise only the
        # explicit file calendar and conservative built-in fallback.

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

    def test_t_plus_1_trade_calendar_empty_falls_back_to_weekends_and_hardcoded_holidays(
        self,
    ):
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
# 3. requirements.txt edge cases
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

        self.assertGreater(
            len(deps), 0, "requirements.txt should list at least one dependency"
        )

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
