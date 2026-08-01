from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from CNFutures import replay


class FakeFuturesReader:
    def get_bars_intraday(
        self, market: str, symbol: str, interval: str, start: str, end: str
    ) -> list[dict[str, object]]:
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
        return ["M2609.DCE"]

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "strategies": [
                {
                    "name": "commodity_intraday_trend",
                    "style_family": "commodity_intraday_trend",
                    "signal_threshold": 0.0015,
                    "moving_average_bars": 5,
                    "max_late_chase_pct": 0.1,
                    "products": ["m"],
                }
            ]
        }


class CNFuturesReplayTest(unittest.TestCase):
    def test_replay_preserves_all_counterfactual_reason_counters(self) -> None:
        class ReasonReader:
            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str,
                start: str,
                end: str,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "bar_time": f"2026-07-09 09:{index:02d}:00",
                        "close": 3_500.0,
                        "volume": 1_000,
                    }
                    for index in range(17)
                ]

        annotation_calls: list[int] = []

        def annotation(**_: object) -> dict[str, object]:
            index = len(annotation_calls)
            annotation_calls.append(index)
            return {
                "execution_eligible": False,
                "execution_reason": "account_state_unavailable",
                "counterfactual_only": True,
                "counterfactual_reason": f"reason_{index}",
                "execution_class": "counterfactual_only",
            }

        with (
            patch.object(
                replay,
                "generate_style_signal",
                return_value={"action": "buy", "price": 3_500.0, "reason": "trigger"},
            ),
            patch.object(replay, "_execution_annotation", side_effect=annotation),
        ):
            report = replay.build_replay_report(
                date="20260709",
                reader=ReasonReader(),
                symbols=["RB2610.SHF"],
                styles=[{"name": "trend", "products": ["rb"]}],
                min_bars=6,
                output=None,
                history=None,
            )

        self.assertEqual(len(annotation_calls), 12)
        self.assertEqual(
            len(report["style_summary"]["trend"]["counterfactual_reasons"]), 12
        )
        self.assertEqual(len(report["execution_summary"]["counterfactual_reasons"]), 12)

    def test_replay_counts_every_actionable_window_before_limiting_examples(
        self,
    ) -> None:
        class WindowReader:
            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str,
                start: str,
                end: str,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "bar_time": f"2026-07-09 09:{index:02d}:00",
                        "close": 3_500.0 + index,
                        "volume": 1_000,
                    }
                    for index in range(34)
                ]

        styles = [
            {
                "name": style_name,
                "products": ["rb"],
                "risk_per_trade": 0.10,
                "max_margin_usage": 0.30,
                "weight": 1.0,
            }
            for style_name in ("trend", "reversal")
        ]
        with patch.object(
            replay,
            "generate_style_signal",
            return_value={
                "action": "buy",
                "side": "buy",
                "price": 3_500.0,
                "reason": "test_trigger",
                "confidence": 0.75,
            },
        ):
            report = replay.build_replay_report(
                date="20260709",
                reader=WindowReader(),
                symbols=["RB2610.SHF"],
                styles=styles,
                min_bars=6,
                output=None,
                history=None,
            )

        self.assertEqual(len(report["actionable_examples"]), 20)
        self.assertEqual(report["actionable_example_limit"], 20)
        self.assertEqual(report["execution_summary"]["actionable_count"], 58)
        self.assertEqual(report["execution_summary"]["execution_eligible_count"], 0)
        self.assertEqual(report["execution_summary"]["counterfactual_only_count"], 58)
        self.assertEqual(
            report["execution_summary"]["execution_class_counts"],
            {"counterfactual_only": 58},
        )
        for style_name in ("trend", "reversal"):
            summary = report["style_summary"][style_name]
            self.assertEqual(summary["prediction_count"], 29)
            self.assertEqual(summary["actionable_count"], 29)
            self.assertEqual(summary["execution_rejected_count"], 29)
            self.assertEqual(summary["execution_eligible_count"], 0)
            self.assertEqual(
                summary["non_executable_reasons"]["account_state_unavailable"],
                29,
            )
            self.assertEqual(
                summary["execution_class_counts"]["counterfactual_only"],
                29,
            )
            self.assertEqual(sum(summary["counterfactual_reasons"].values()), 29)

    def test_replay_is_read_only_and_counts_actionable_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = replay.build_replay_report(
                date="20260709",
                reader=FakeFuturesReader(),
                symbols=["M2609.DCE"],
                styles=[
                    {
                        "name": "commodity_intraday_trend",
                        "style_family": "commodity_intraday_trend",
                        "signal_threshold": 0.0015,
                        "moving_average_bars": 5,
                        "max_late_chase_pct": 0.1,
                        "products": ["m"],
                    }
                ],
                output=None,
                history=None,
            )
            self.assertFalse((root / "signals").exists())

        self.assertTrue(report["read_only"])
        self.assertFalse(report["real_trading_enabled"])
        self.assertGreater(report["window_count"], 0)
        self.assertIn("commodity_intraday_trend", report["style_summary"])
        self.assertGreater(
            report["style_summary"]["commodity_intraday_trend"]["action_counts"].get("buy", 0), 0
        )

    def test_replay_filters_symbols_not_allowed_by_style_products(self) -> None:
        report = replay.build_replay_report(
            date="20260709",
            reader=FakeFuturesReader(),
            symbols=["CU2607.SHF", "I2609.DCE"],
            styles=[
                {
                    "name": "index_intraday_directional",
                    "signal_threshold": 0.001,
                    "products": ["if", "ih", "ic", "im"],
                }
            ],
            output=None,
            history=None,
        )

        summary = report["style_summary"]["index_intraday_directional"]
        self.assertEqual(summary["symbols_seen"], 0)
        self.assertEqual(summary["non_executable_reasons"]["product_not_allowed"], 2)
        self.assertEqual(report["actionable_examples"], [])

    def test_replay_marks_lunch_boundary_actions_non_executable(self) -> None:
        class BoundaryReader(FakeFuturesReader):
            def get_bars_intraday(
                self, market: str, symbol: str, interval: str, start: str, end: str
            ) -> list[dict[str, object]]:
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
            symbols=["M2609.DCE"],
            styles=[
                {
                    "name": "commodity_intraday_trend",
                    "style_family": "commodity_intraday_trend",
                    "signal_threshold": 0.001,
                    "moving_average_bars": 5,
                    "max_late_chase_pct": 0.1,
                    "products": ["m"],
                    "max_margin_usage": 0.1,
                }
            ],
            output=None,
            history=None,
        )

        boundary_examples = [
            row
            for row in report["actionable_examples"]
            if row["bar_time"] == "2026-07-09 11:30:00"
        ]
        self.assertTrue(boundary_examples)
        self.assertEqual(
            boundary_examples[0]["execution_reason"], "session_boundary_not_executable"
        )

    # --- RED: canonical capital sourcing ---

    def test_replay_execution_annotation_uses_canonical_capital_not_hardcoded_200k(
        self,
    ) -> None:
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

        # Replay has no authoritative current account state, so it is never
        # executable. The nested decision still proves the 50k counterfactual.
        self.assertFalse(annotation["execution_eligible"])
        self.assertEqual(annotation["execution_reason"], "account_state_unavailable")
        self.assertTrue(annotation["counterfactual_only"])
        self.assertFalse(annotation["counterfactual_eligible"])
        self.assertLessEqual(annotation["margin_cap"], 10_000.0)

    def test_replay_legacy_200k_style_capital_cannot_override_current_50k(self) -> None:
        """A stale style capital field must not create a parallel account."""
        old_tier = os.environ.get("CN_FUTURES_SIM_CAPITAL_TIER")
        os.environ["CN_FUTURES_SIM_CAPITAL_TIER"] = "50000"
        try:
            annotation = replay._execution_annotation(
                symbol="cu2607",
                style={
                    "name": "trend",
                    "capital": 200000.0,
                    "risk_per_trade": 0.20,
                    "max_margin_usage": 0.20,
                    "products": ["cu"],
                },
                action="buy",
                price=18000.0,
                bar_time="2026-07-09 09:30:00",
            )
        finally:
            if old_tier is None:
                os.environ.pop("CN_FUTURES_SIM_CAPITAL_TIER", None)
            else:
                os.environ["CN_FUTURES_SIM_CAPITAL_TIER"] = old_tier

        # Replay remains anchored to the current independent 50k account, and
        # cannot claim either execution or 200k counterfactual affordability.
        self.assertFalse(annotation["execution_eligible"])
        self.assertEqual(annotation["execution_reason"], "account_state_unavailable")
        self.assertTrue(annotation["counterfactual_only"])
        self.assertFalse(annotation["counterfactual_eligible"])
        self.assertLessEqual(annotation["margin_cap"], 10_000.0)


if __name__ == "__main__":
    unittest.main()
