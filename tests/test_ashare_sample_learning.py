from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ashare.sample_learning import (
    _account_objectives,
    build_hypothesis_id,
    build_sample_learning_report,
    write_sample_learning_report,
)


EPOCH_STATE = {
    "current_epoch_id": 2,
    "capital_cny": 50_000.0,
    "cutover_timestamp": "2026-07-10T20:56:58+00:00",
}


class AshareSampleLearningTest(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_build_hypothesis_id_is_stable_and_human_readable(self) -> None:
        hypothesis_id = build_hypothesis_id(
            trade_date="20260710",
            symbol="600584.SH",
            side="buy",
            execution_source="ashare_candidate_layer",
            candidate_pool_layer="candidate",
            score=0.6118,
        )

        self.assertEqual(hypothesis_id, "ashare-20260710-buy-600584.SH-candidate-s061")

    def test_sample_learning_report_combines_quality_attribution_accounts_and_factors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_dir = root / "shared" / "review" / "ashare"
            trades_path = root / "shared" / "logs" / "local_sim" / "local_sim_trades.jsonl"
            no_trade_path = root / "shared" / "logs" / "ashare_no_trade_explanations.jsonl"
            hypothesis_id = "ashare-20260710-buy-600584.SH-candidate-s061"
            self._write_jsonl(
                trades_path,
                [
                    {
                        "trade_id": "T1",
                        "order_id": "O1",
                        "market": "ashare",
                        "account": "ashare_sim",
                        "trade_date": "20260710",
                        "ts_code": "600584.SH",
                        "side": "buy",
                        "quantity": 300,
                        "filled_price": 94.11,
                        "capital_layer": "simulated",
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                        "fill_price_source_class": "market_data",
                        "fill_price_source": "market_snapshot.ask_price",
                        "trade_timestamp_bj": "2026-07-10T10:01:00+08:00",
                        "hypothesis_id": hypothesis_id,
                        "research_hypothesis": {
                            "hypothesis_id": hypothesis_id,
                            "factor_snapshot": {
                                "combined": 0.6118,
                                "capital": 1.0,
                                "technical": 1.0,
                                "fundamental": 0.4511,
                            },
                        },
                    },
                    {
                        "trade_id": "T2",
                        "order_id": "O2",
                        "market": "ashare",
                        "account": "ashare_sim",
                        "trade_date": "20260710",
                        "ts_code": "000001.SZ",
                        "side": "buy",
                        "quantity": 100,
                        "filled_price": 10.0,
                        "capital_layer": "simulated",
                        "candidate_pool_layer": "watch",
                        "execution_source": "manual_test",
                        "trade_timestamp_bj": "2026-07-10T15:20:00+08:00",
                    },
                ],
            )
            self._write_json(
                review_dir / "forward_validation_latest.json",
                {
                    "labels": [
                        {
                            "trade_id": "T1",
                            "status": "labeled",
                            "labels": {
                                "close": {"status": "labeled", "return_pct": 0.012},
                                "next_day": {"status": "pending"},
                            },
                        }
                    ]
                },
            )
            self._write_json(
                review_dir / "sample_target_monitor_latest.json",
                {
                    "trade_date": "20260710",
                    "overall_status": "warn",
                    "state": "sample_debt",
                    "blockers": ["capital_plan_defensive"],
                    "daily_target": {"target": 1, "today_strategy_sample_count": 0},
                },
            )
            self._write_json(
                review_dir / "tier_experiments_latest.json",
                {
                    "accounts": [
                        {"account": "ashare_50000", "capital": 50000, "trade_count": 1, "pnl": {"total_pnl": 12.0}},
                        {"account": "ashare_100000", "capital": 100000, "trade_count": 1, "pnl": {"total_pnl": -5.0}},
                    ]
                },
            )
            self._write_jsonl(
                no_trade_path,
                [
                    {
                        "trade_date": "20260710",
                        "no_trade_explanation": {
                            "category": "capital_plan_defensive",
                            "capital_plan_decision": {"risk_mode": "defensive", "position_capacity": 0},
                            "counts": {"candidates": 2, "orders": 0, "risk_rejections": 1},
                        },
                    }
                ],
            )

            report = build_sample_learning_report(
                trade_date="20260710",
                review_dir=review_dir,
                local_trades_path=trades_path,
                no_trade_log_path=no_trade_path,
                min_factor_samples=3,
            )
            written = write_sample_learning_report(
                trade_date="20260710",
                review_dir=review_dir,
                local_trades_path=trades_path,
                no_trade_log_path=no_trade_path,
                min_factor_samples=3,
            )
            latest_exists = (review_dir / "sample_learning_latest.json").exists()

        self.assertEqual(report["overall_status"], "warn")
        self.assertEqual(report["sample_quality"]["tier_counts"]["high_quality_strategy_sample"], 1)
        self.assertEqual(report["sample_quality"]["tier_counts"]["chain_validation_sample"], 1)
        self.assertEqual(report["hypothesis_registry"]["hypothesis_count"], 1)
        self.assertEqual(report["postclose_attribution"]["primary_blocker"], "capital_plan_defensive")
        self.assertGreaterEqual(report["dynamic_probe_budget"]["recommended_allocation"], 5000.0)
        self.assertLessEqual(report["dynamic_probe_budget"]["recommended_allocation"], 8750.0)
        self.assertEqual(report["account_objectives"]["ashare_50000"]["primary_goal"], "drawdown_controlled_growth")
        self.assertEqual(report["account_objectives"]["ashare_100000"]["note"], "historical_experiment_epoch")
        self.assertNotIn("ashare_200000", report["account_objectives"])
        self.assertEqual(report["factor_research"]["status"], "sample_debt")
        self.assertIn("combined", report["factor_research"]["factors"])
        self.assertFalse(report["writes_orders"])
        self.assertTrue(latest_exists)
        self.assertEqual(written["latest_path"], str(review_dir / "sample_learning_latest.json"))

    # -- RED: dynamic primary capital tests (currently failing) -----------------
    def test_account_objectives_does_not_list_primary_as_experiment_when_50k(self) -> None:
        """When primary capital is 50k, ashare_50000 is the PRIMARY, not an experiment tier."""
        tier_manifest = {
            "accounts": [
                {"account": "ashare_100000", "capital": 100000, "trade_count": 1, "pnl": {"total_pnl": -5.0}},
            ]
        }
        portfolio = {
            "strategy_sample_count": 5,
            "pnl": {"total_pnl": 120.0},
        }
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "50000"}, clear=False):
            objectives = _account_objectives(tier_manifest, portfolio)

        # The primary account objective should reflect the canonical capital
        self.assertIn("ashare_50000", objectives,
            "ashare_50000 should appear as the primary account objective")
        # Primary should have trade_count and total_pnl from the portfolio
        self.assertEqual(objectives["ashare_50000"]["trade_count"], 5)
        self.assertEqual(objectives["ashare_50000"]["total_pnl"], 120.0)
        # Legacy 200k should NOT appear as current
        self.assertNotIn("ashare_200000", objectives,
            "ashare_200000 is a legacy epoch and must not appear as current")

    def test_account_objectives_primary_derives_from_portfolio_not_hardcoded(self) -> None:
        """The primary account (from canonical capital) gets portfolio-level stats."""
        tier_manifest = {"accounts": []}
        portfolio = {
            "strategy_sample_count": 42,
            "pnl": {"total_pnl": 999.0},
        }
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "50000"}, clear=False):
            objectives = _account_objectives(tier_manifest, portfolio)

        self.assertIn("ashare_50000", objectives)
        self.assertEqual(objectives["ashare_50000"]["trade_count"], 42)
        self.assertEqual(objectives["ashare_50000"]["total_pnl"], 999.0)
        self.assertNotIn("ashare_200000", objectives)

    def test_account_objectives_ignore_legacy_env_override(self) -> None:
        """Production primary capital remains 50k even if a legacy env value exists."""
        tier_manifest = {
            "accounts": [
                {"account": "ashare_50000", "capital": 50000, "trade_count": 1, "pnl": {"total_pnl": 12.0}},
                {"account": "ashare_100000", "capital": 100000, "trade_count": 1, "pnl": {"total_pnl": -5.0}},
            ]
        }
        portfolio = {
            "strategy_sample_count": 10,
            "pnl": {"total_pnl": 200.0},
        }
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "200000"}, clear=False):
            objectives = _account_objectives(tier_manifest, portfolio)

        self.assertNotIn("ashare_200000", objectives)
        self.assertEqual(objectives["ashare_50000"]["trade_count"], 10)
        self.assertEqual(objectives["ashare_50000"]["total_pnl"], 200.0)
        self.assertIn("ashare_100000", objectives)


    # --- RED: probe budget scales with canonical capital ---

    def test_probe_budget_scales_with_50k_canonical_capital(self) -> None:
        """Dynamic probe budget must scale proportionally with capital,
        not stay hardcoded at 20k-35k."""
        from Ashare.sample_learning import _dynamic_probe_budget

        old_tier = os.environ.get("ASHARE_SIM_CAPITAL_TIER")
        os.environ["ASHARE_SIM_CAPITAL_TIER"] = "50000"
        try:
            # Simulate some trades with scores to drive the budget calculation
            trades: list[dict] = [
                {
                    "trade_id": "T1",
                    "ts_code": "600584.SH",
                    "research_hypothesis": {
                        "factor_snapshot": {"combined": 0.65},
                    },
                },
            ]
            sample_monitor: dict = {"overall_status": "ok"}
            budget = _dynamic_probe_budget(trades, sample_monitor)
        finally:
            if old_tier is None:
                os.environ.pop("ASHARE_SIM_CAPITAL_TIER", None)
            else:
                os.environ["ASHARE_SIM_CAPITAL_TIER"] = old_tier

        # On 50k, the probe budget must be proportionally smaller.
        # Not 20k-35k (which is 40-70% of 50k account!)
        self.assertLessEqual(budget["recommended_allocation"], 10000.0,
                            "50k probe budget must be <= 10k, not 20k-35k")
        self.assertGreaterEqual(budget["recommended_allocation"], 4000.0)
        self.assertLessEqual(budget["min"], 7500.0)
        self.assertLessEqual(budget["max"], 12000.0)

    def test_rejects_old_epoch_review_inputs_and_writes_current_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_dir = root / "review"
            trades_path = root / "local_sim_trades.jsonl"
            trade = {
                "trade_id": "T-current",
                "order_id": "O-current",
                "capital_epoch": 2,
                "trade_date": "20260711",
                "market": "ashare",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "filled_price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "fill_price_source_class": "market_data",
                "trade_timestamp_bj": "2026-07-11T10:00:00+08:00",
                "hypothesis_id": "H-current",
                "research_hypothesis": {"hypothesis_id": "H-current", "factor_snapshot": {"combined": 0.8}},
            }
            self._write_jsonl(trades_path, [trade])
            old_review = {
                "capital_epoch": 1,
                "capital_cny": 200_000.0,
                "generated_at": "2026-07-10T08:00:00+00:00",
                "labels": [{"trade_id": "T-current", "labels": {"close": {"return_pct": 0.5}}}],
                "overall_status": "pass",
                "daily_target": {"today_strategy_sample_count": 99},
            }
            self._write_json(review_dir / "forward_validation_latest.json", old_review)
            self._write_json(review_dir / "sample_target_monitor_latest.json", old_review)

            with patch("Ashare.sample_learning.read_epoch_state", return_value=EPOCH_STATE):
                report = write_sample_learning_report(
                    trade_date="20260711",
                    review_dir=review_dir,
                    local_trades_path=trades_path,
                    min_factor_samples=1,
                )

            persisted = json.loads((review_dir / "sample_learning_latest.json").read_text(encoding="utf-8"))

        self.assertEqual(report["capital_epoch"], 2)
        self.assertEqual(report["capital_cny"], 50_000.0)
        self.assertEqual(report["epoch_cutover_timestamp"], EPOCH_STATE["cutover_timestamp"])
        self.assertEqual(report["factor_research"]["status"], "sample_debt")
        self.assertEqual(report["epoch_input_rejections"]["forward_validation"], "capital_epoch_mismatch")
        self.assertEqual(report["epoch_input_rejections"]["sample_target_monitor"], "capital_epoch_mismatch")
        self.assertEqual(persisted["capital_epoch"], 2)

    def test_rejects_matching_epoch_review_with_wrong_cutover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp) / "review"
            wrong = {
                "capital_epoch": 2,
                "capital_cny": 50_000.0,
                "epoch_cutover_timestamp": "2026-07-01T00:00:00+00:00",
                "generated_at": "2026-07-11T03:00:00+00:00",
                "labels": [],
            }
            self._write_json(review_dir / "forward_validation_latest.json", wrong)

            with patch("Ashare.sample_learning.read_epoch_state", return_value=EPOCH_STATE):
                report = build_sample_learning_report(
                    trade_date="20260711",
                    review_dir=review_dir,
                    local_trades_path=Path(tmp) / "trades.jsonl",
                )

        self.assertEqual(
            report["epoch_input_rejections"]["forward_validation"],
            "epoch_cutover_timestamp_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
