from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.execution.auto_pipeline import AutoPipeline, LocalStyleSimulator


class SnapshotReader:
    def get_universe(self, market, trade_date=None, **kwargs):
        return [{"symbol": "600000.SH", "ts_code": "600000.SH", "price": 10.2}]

    def get_bars_intraday(self, market, symbol, interval="5min", start_time="", end_time=""):
        if market == "Ashare" and symbol == "600000.SH":
            return [
                {
                    "bar_time": "2026-07-04 14:55:00",
                    "close": 10.2,
                    "volume": 1500,
                    "ask_price": 10.21,
                    "ask_size": 200,
                }
            ]
        return []

    def get_bars_daily(self, market, symbol, start_date="", end_date=""):
        if market == "Ashare" and symbol == "600000.SH":
            return [
                {"trade_date": "20260703", "close": 9.8, "volume": 100000},
                {"trade_date": "20260704", "close": 10.2, "volume": 120000},
            ]
        return []


class AutoPipelineSmokeTest(unittest.TestCase):
    def test_import_init_and_review_stage_no_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = AutoPipeline(
                reader=object(),
                decision_engine=object(),
                fundamental_analyzer=object(),
                perspective_analyzer=object(),
                simulator_factory=lambda market: object(),
                evolution_fn=lambda market, review_root=None: {"state": "ok", "market": market},
                review_root=Path(tmp),
                max_candidates=1,
            )

            result = pipeline.run(trade_date="20260704", markets=["crypto"], stage="daily_review")

            self.assertEqual(result["capital_layer"], "simulated")
            self.assertEqual(result["markets"][0]["stages"]["daily_review"]["state"], "ok")

    def test_local_style_simulator_uses_matching_engine(self) -> None:
        simulator = LocalStyleSimulator("ashare")

        fill = simulator.simulate(
            {
                "order_id": "SIM-AUTO-ASHARE",
                "symbol": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
            },
            {"initial_capital": 100_000.0, "capital_layer": "simulated", "account_type": "simulated"},
        )

        self.assertEqual(fill["status"], "filled")
        self.assertEqual(fill["broker"], "local_matching_engine")
        self.assertEqual(fill["engine_record"]["state"], "filled")

    def test_local_style_simulator_uses_bar_volume_when_book_size_missing(self) -> None:
        simulator = LocalStyleSimulator("ashare")

        fill = simulator.simulate(
            {
                "order_id": "SIM-AUTO-ASHARE-BARVOL",
                "symbol": "600000.SH",
                "side": "buy",
                "quantity": 300,
                "price": 10.0,
                "bar_volume": 1500,
            },
            {"initial_capital": 100_000.0, "capital_layer": "simulated", "account_type": "simulated"},
        )

        self.assertEqual(fill["status"], "partial")
        self.assertEqual(fill["filled_qty"], 100)

    def test_signals_include_sharedsignals_market_snapshot(self) -> None:
        pipeline = AutoPipeline(
            reader=SnapshotReader(),
            decision_engine=object(),
            fundamental_analyzer=object(),
            perspective_analyzer=object(),
            evolution_fn=lambda market, review_root=None: {"state": "ok", "market": market},
            max_candidates=1,
        )

        signals = pipeline._signals_from_positions(
            "ashare",
            {"positions": [{"ts_code": "600000.SH", "side": "buy", "price": 10.2}]},
            [{"ts_code": "600000.SH", "belief_score": 0.8, "conviction": 0.8}],
            "20260704",
        )

        snapshot = signals[0]["market_snapshot"]
        self.assertEqual(snapshot["bar_volume"], 1500)
        self.assertEqual(snapshot["previous_close"], 9.8)
        self.assertEqual(snapshot["ask_price"], 10.21)

    def test_full_pipeline_runs_with_default_decision_engine_contract(self) -> None:
        class Fundamental:
            def analyze(self, symbol, **kwargs):
                return {"symbol": symbol, "composite_score": 90.0, "red_flags": []}

        class Perspective:
            def analyze(self, symbol, **kwargs):
                return {
                    "bull": {"score": 85},
                    "bear": {"score": 10},
                    "macro": {"score": 80},
                    "technical": {"score": 85},
                }

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = AutoPipeline(
                reader=SnapshotReader(),
                fundamental_analyzer=Fundamental(),
                perspective_analyzer=Perspective(),
                evolution_fn=lambda market, review_root=None: {"state": "ok", "market": market},
                review_root=Path(tmp),
                max_candidates=1,
            )

            result = pipeline.run(trade_date="20260704", markets=["ashare"], stage="all")

            execution = result["markets"][0]["stages"]["execute_sim"]
            self.assertEqual(execution["state"], "ok")
            self.assertGreaterEqual(execution["performance_records"], 0)


if __name__ == "__main__":
    unittest.main()
