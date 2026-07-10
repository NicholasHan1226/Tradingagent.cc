from __future__ import annotations

import os
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


    # --- RED: canonical capital sourcing ---

    def test_replay_execution_annotation_uses_canonical_capital_not_hardcoded_200k(self) -> None:
        """_execution_annotation fallback must use default_sim_capital('cn_futures'),
        not a hardcoded 200_000."""
        old_tier = os.environ.get("CN_FUTURES_SIM_CAPITAL_TIER")
        os.environ["CN_FUTURES_SIM_CAPITAL_TIER"] = "50000"
        try:
            annotation = replay._execution_annotation(
                symbol="cu2607",
                style={"name": "trend", "max_margin_usage": 0.20, "products": ["cu"]},
                action="buy",
                price=18000.0,
                bar_time="2026-07-09 09:30:00",
            )
        finally:
            if old_tier is None:
                os.environ.pop("CN_FUTURES_SIM_CAPITAL_TIER", None)
            else:
                os.environ["CN_FUTURES_SIM_CAPITAL_TIER"] = old_tier

        # On 50k, margin cap = 50000 * 0.20 = 10000.
        # CU at 18000 with multiplier 5, margin 0.12 → 10800 > 10000.
        # So execution should be ineligible due to margin cap.
        self.assertFalse(annotation["execution_eligible"])
        self.assertEqual(annotation["execution_reason"], "margin_cap_exceeded")
        # margin_cap must reflect 50k, not 200k (200k * 0.20 = 40000)
        self.assertLessEqual(annotation["margin_cap"], 11000.0)

    def test_replay_200k_style_capital_field_overrides_fallback(self) -> None:
        """When style explicitly sets 'capital': 200000, that must be used
        regardless of env tier. Historical fixtures retain explicit 200k."""
        old_tier = os.environ.get("CN_FUTURES_SIM_CAPITAL_TIER")
        os.environ["CN_FUTURES_SIM_CAPITAL_TIER"] = "50000"
        try:
            annotation = replay._execution_annotation(
                symbol="cu2607",
                style={"name": "trend", "capital": 200000.0, "max_margin_usage": 0.20, "products": ["cu"]},
                action="buy",
                price=18000.0,
                bar_time="2026-07-09 09:30:00",
            )
        finally:
            if old_tier is None:
                os.environ.pop("CN_FUTURES_SIM_CAPITAL_TIER", None)
            else:
                os.environ["CN_FUTURES_SIM_CAPITAL_TIER"] = old_tier

        # With explicit 200k capital, margin cap = 200000 * 0.20 = 40000.
        # CU at 18000: margin = 18000 * 5 * 0.12 = 10800 < 40000.
        # So execution should be eligible.
        self.assertTrue(annotation["execution_eligible"])
        self.assertEqual(annotation["execution_reason"], "execution_eligible")


if __name__ == "__main__":
    unittest.main()
