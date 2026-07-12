from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.execution.auto_pipeline import (
    ACTIVE_MARKETS,
    AutoPipeline,
    LocalStyleSimulator,
    _candidate_price,
)


class SnapshotReader:
    def get_assets(self, market):
        if market in {"ashare", "Ashare"}:
            return [
                {
                    "symbol": "000001.SZ",
                    "name": "",
                    "status": "active",
                    "list_date": "19910403",
                },
                {
                    "symbol": "600000.SH",
                    "name": "浦发银行",
                    "status": "active",
                    "list_date": "19991110",
                },
            ]
        return []

    def get_universe(self, market, trade_date=None, **kwargs):
        if market == "ashare":
            return []
        return [{"symbol": "600000.SH", "ts_code": "600000.SH", "price": 10.2}]

    def get_coverage(self, market, date):
        return [
            {"symbol": "000001.SZ", "coverage_status": "normal"},
            {"symbol": "600000.SH", "coverage_status": "normal"},
        ]

    def get_bars_intraday(
        self, market, symbol, interval="5min", start_time="", end_time=""
    ):
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
        if market in {"ashare", "Ashare"} and symbol == "000001.SZ":
            return [
                {
                    "trade_date": "20260703",
                    "close": 9.8,
                    "volume": 100000,
                    "amount": 80_000,
                },
                {
                    "trade_date": "20260704",
                    "close": 10.2,
                    "volume": 120000,
                    "amount": 80_000,
                },
            ]
        if market in {"ashare", "Ashare"} and symbol == "600000.SH":
            return [
                {
                    "trade_date": "20260703",
                    "close": 9.8,
                    "volume": 100000,
                    "amount": 80_000,
                },
                {
                    "trade_date": "20260704",
                    "close": 10.2,
                    "volume": 120000,
                    "amount": 80_000,
                },
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
                evolution_fn=lambda market, review_root=None: {
                    "state": "ok",
                    "market": market,
                },
                review_root=Path(tmp),
                max_candidates=1,
            )

            result = pipeline.run(
                trade_date="20260704", markets=["crypto"], stage="daily_review"
            )

            self.assertEqual(result["capital_layer"], "simulated")
            self.assertEqual(
                result["markets"][0]["stages"]["daily_review"]["state"], "ok"
            )

    def test_local_style_simulator_refuses_retired_ashare_authority(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ashare.*retired"):
            LocalStyleSimulator("ashare")

    def test_local_style_simulator_uses_matching_engine(self) -> None:
        simulator = LocalStyleSimulator("us")

        fill = simulator.simulate(
            {
                "order_id": "SIM-AUTO-US",
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
            },
            {
                "initial_capital": 50_000.0,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        )

        self.assertEqual(fill["status"], "filled")
        self.assertEqual(fill["broker"], "local_matching_engine")
        self.assertEqual(fill["engine_record"]["state"], "filled")

    def test_local_style_simulator_uses_bar_volume_when_book_size_missing(self) -> None:
        simulator = LocalStyleSimulator("us")

        fill = simulator.simulate(
            {
                "order_id": "SIM-AUTO-US-BARVOL",
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 300,
                "price": 10.0,
                "bar_volume": 1500,
            },
            {
                "initial_capital": 50_000.0,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        )

        self.assertEqual(fill["status"], "partial")
        self.assertEqual(fill["filled_qty"], 75.0)

    def test_active_markets_exclude_authoritative_ashare_and_cn_futures(self) -> None:
        self.assertNotIn("ashare", ACTIVE_MARKETS)
        self.assertNotIn("cn_futures", ACTIVE_MARKETS)

    def test_explicit_ashare_pipeline_is_fail_closed_before_any_stage(self) -> None:
        pipeline = AutoPipeline()

        with self.assertRaisesRegex(RuntimeError, "ashare.*retired"):
            pipeline.run(trade_date="20260713", markets=["ashare"], stage="all")

        with self.assertRaisesRegex(RuntimeError, "ashare.*retired"):
            pipeline.run(
                trade_date="20260713", markets=["ashare"], stage="daily_review"
            )

    def test_direct_ashare_execution_and_review_are_fail_closed(self) -> None:
        pipeline = AutoPipeline()

        with self.assertRaisesRegex(RuntimeError, "ashare.*retired"):
            pipeline.run_execution("ashare", {"positions": []}, [], "20260713")

        with self.assertRaisesRegex(RuntimeError, "ashare.*retired"):
            pipeline.run_review("ashare", "20260713")

    def test_non_ashare_fallback_preserves_existing_requested_allocations(self) -> None:
        pipeline = AutoPipeline(decision_engine=object())
        decisions = [
            {
                "symbol": f"CRYPTO-{index}",
                "ts_code": f"CRYPTO-{index}",
                "action": "buy",
                "belief_score": 0.90,
                "position_pct": 0.12,
                "price": 1.0,
            }
            for index in range(10)
        ]

        portfolio = pipeline._fallback_portfolio_rebalance(
            "crypto", decisions, "20260713"
        )

        self.assertEqual(len(portfolio["positions"]), 10)
        self.assertAlmostEqual(portfolio["allocated_pct"], 1.20, places=6)

    def test_auto_pipeline_ashare_read_and_signal_helpers_are_fail_closed(self) -> None:
        pipeline = AutoPipeline(
            reader=SnapshotReader(),
            decision_engine=object(),
            fundamental_analyzer=object(),
            perspective_analyzer=object(),
            evolution_fn=lambda market, review_root=None: {
                "state": "ok",
                "market": market,
            },
            max_candidates=5,
        )

        with self.assertRaisesRegex(RuntimeError, "ashare.*retired"):
            pipeline.load_universe("ashare", "20260704")

        with self.assertRaisesRegex(RuntimeError, "ashare.*retired"):
            pipeline._signals_from_positions(
                "ashare",
                {"positions": [{"ts_code": "600000.SH", "side": "buy", "price": 10.2}]},
                [{"ts_code": "600000.SH", "belief_score": 0.8, "conviction": 0.8}],
                "20260704",
            )

    def test_auto_pipeline_ashare_candidate_price_never_defaults_to_one(self) -> None:
        self.assertEqual(_candidate_price({}, "ashare"), 0.0)

    def test_full_pipeline_runs_for_supported_market(self) -> None:
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
                evolution_fn=lambda market, review_root=None: {
                    "state": "ok",
                    "market": market,
                },
                review_root=Path(tmp),
                max_candidates=1,
            )

            result = pipeline.run(
                trade_date="20260704", markets=["crypto"], stage="all"
            )

            execution = result["markets"][0]["stages"]["execute_sim"]
            self.assertEqual(execution["state"], "ok")
            self.assertGreaterEqual(execution["performance_records"], 0)


if __name__ == "__main__":
    unittest.main()
