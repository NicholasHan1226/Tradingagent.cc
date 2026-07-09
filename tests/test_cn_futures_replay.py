from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from CNFutures import replay


class FakeFuturesReader:
    def get_bars_intraday(self, market: str, symbol: str, interval: str, start: str, end: str) -> list[dict[str, object]]:
        return [
            {"bar_time": "2026-07-09 09:00:00", "close": 100.0, "volume": 100},
            {"bar_time": "2026-07-09 09:05:00", "close": 100.2, "volume": 110},
            {"bar_time": "2026-07-09 09:10:00", "close": 100.5, "volume": 120},
            {"bar_time": "2026-07-09 09:15:00", "close": 101.0, "volume": 150},
            {"bar_time": "2026-07-09 09:20:00", "close": 101.8, "volume": 180},
            {"bar_time": "2026-07-09 09:25:00", "close": 103.0, "volume": 220},
        ]


class FakeAdapter:
    def __init__(self, reader=None) -> None:
        self.reader = reader or FakeFuturesReader()

    def get_universe(self, date: str) -> list[str]:
        return ["CU2607.SHF"]

    def get_strategy_config(self) -> dict[str, object]:
        return {"strategies": [{"name": "trend", "signal_threshold": 0.005, "products": ["cu"]}]}


class CNFuturesReplayTest(unittest.TestCase):
    def test_replay_is_read_only_and_counts_actionable_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = replay.build_replay_report(
                date="20260709",
                reader=FakeFuturesReader(),
                symbols=["CU2607.SHF"],
                styles=[{"name": "trend", "signal_threshold": 0.005, "products": ["cu"]}],
                output=None,
                history=None,
            )
            self.assertFalse((root / "signals").exists())

        self.assertTrue(report["read_only"])
        self.assertFalse(report["real_trading_enabled"])
        self.assertGreater(report["window_count"], 0)
        self.assertIn("trend", report["style_summary"])
        self.assertGreater(report["style_summary"]["trend"]["action_counts"].get("buy", 0), 0)

    def test_replay_filters_symbols_not_allowed_by_style_products(self) -> None:
        report = replay.build_replay_report(
            date="20260709",
            reader=FakeFuturesReader(),
            symbols=["CU2607.SHF", "I2609.DCE"],
            styles=[{"name": "index_intraday_directional", "signal_threshold": 0.001, "products": ["if", "ih", "ic", "im"]}],
            output=None,
            history=None,
        )

        summary = report["style_summary"]["index_intraday_directional"]
        self.assertEqual(summary["symbols_seen"], 0)
        self.assertEqual(summary["non_executable_reasons"]["product_not_allowed"], 2)
        self.assertEqual(report["actionable_examples"], [])

    def test_replay_marks_lunch_boundary_actions_non_executable(self) -> None:
        class BoundaryReader(FakeFuturesReader):
            def get_bars_intraday(self, market: str, symbol: str, interval: str, start: str, end: str) -> list[dict[str, object]]:
                return [
                    {"bar_time": "2026-07-09 11:05:00", "close": 100.0, "volume": 100},
                    {"bar_time": "2026-07-09 11:10:00", "close": 100.5, "volume": 120},
                    {"bar_time": "2026-07-09 11:15:00", "close": 101.2, "volume": 140},
                    {"bar_time": "2026-07-09 11:20:00", "close": 102.0, "volume": 160},
                    {"bar_time": "2026-07-09 11:25:00", "close": 103.0, "volume": 180},
                    {"bar_time": "2026-07-09 11:30:00", "close": 105.0, "volume": 220},
                ]

        report = replay.build_replay_report(
            date="20260709",
            reader=BoundaryReader(),
            symbols=["IF2609.CFX"],
            styles=[{"name": "index_intraday_directional", "signal_threshold": 0.001, "products": ["if"], "max_margin_usage": 0.8}],
            output=None,
            history=None,
        )

        boundary_examples = [row for row in report["actionable_examples"] if row["bar_time"] == "2026-07-09 11:30:00"]
        self.assertTrue(boundary_examples)
        self.assertEqual(boundary_examples[0]["execution_reason"], "session_boundary_not_executable")


if __name__ == "__main__":
    unittest.main()
