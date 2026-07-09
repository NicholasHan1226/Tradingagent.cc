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


if __name__ == "__main__":
    unittest.main()
