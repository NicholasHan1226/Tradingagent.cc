from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.orchestrator import (
    ASHARE_OPPORTUNITY_COST_MIN_ENTRY_SCORE,
    ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP,
    _ashare_opportunity_cost_thresholds,
    _ashare_rebalance_plan,
)


class StubReader:
    def get_bars_daily(
        self, market: str, symbol: str, start: object = None, end: object = None
    ) -> list[dict[str, float]]:
        return [{"close": 10.0}]

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5m",
        start: str = "",
        end: str = "",
    ) -> list[dict[str, float]]:
        return []


class AshareOpportunityCostThresholdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)

    def _write_sample_kpi(self, **overrides: object) -> Path:
        path = self.tmp_path / "sample_kpi_latest.json"
        report: dict[str, object] = {
            "report_type": "sample_journal_kpi",
            "evidence_source": "sample_journal_kpi",
            "trade_date": "20260709",
            "authority_scope": {
                "capital_authority_id": "ashare-capital-v1",
                "authority_generation": 1,
                "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            },
            "sample_layer_totals": {
                "observation_counterfactual": 12,
                "exploration_fill": 2,
                "exploitation_fill": 1,
                "completed_round_trip": 1,
            },
            "styles": {
                "trend_breakout": {
                    "prediction_count": 12,
                    "forward_label_counts": {
                        "m30": {"ready": 8},
                        "m60": {"pending_not_due": 4},
                    },
                }
            },
            "scientific_evidence": {"promotion_evidence_ready": False},
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
        }
        report.update(overrides)
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def _write_trades(self, trades: list[dict[str, object]]) -> Path:
        path = self.tmp_path / "local_sim_trades.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for trade in trades:
                handle.write(json.dumps(trade) + "\n")
        return path

    def test_default_thresholds_when_no_validation_data(self) -> None:
        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=ASHARE_OPPORTUNITY_COST_MIN_ENTRY_SCORE,
            min_score_gap=ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP,
            existing_positions=[],
            scores_by_symbol={},
        )

        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["min_entry_score"], 0.70)
        self.assertEqual(result["min_score_gap"], 0.12)
        self.assertEqual(result["action"], "standard_gap")
        self.assertEqual(
            result["sample_evidence_summary"]["status"],
            "missing_current_sample_kpi",
        )

    def test_never_drops_below_hard_floor(self) -> None:
        sample_kpi_path = self._write_sample_kpi()

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
            sample_kpi_path=sample_kpi_path,
        )

        self.assertGreaterEqual(result["min_score_gap"], 0.12)
        self.assertEqual(result["sample_evidence_summary"]["status"], "available")
        self.assertEqual(result["sample_evidence_summary"]["prediction_count"], 12)
        self.assertEqual(
            result["sample_evidence_summary"]["ready_forward_label_count"], 8
        )

    def test_current_sample_projection_is_diagnostic_and_cannot_auto_tighten_policy(
        self,
    ) -> None:
        sample_kpi_path = self._write_sample_kpi(
            scientific_evidence={"promotion_evidence_ready": True},
            automatic_promotion_enabled=True,
            automatic_risk_expansion_enabled=True,
        )

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
            sample_kpi_path=sample_kpi_path,
        )

        self.assertEqual(result["min_score_gap"], 0.12)
        self.assertEqual(result["action"], "standard_gap")
        self.assertEqual(result["reasons"], [])
        self.assertFalse(
            result["sample_evidence_summary"]["automatic_promotion_enabled"]
        )
        self.assertFalse(
            result["sample_evidence_summary"]["automatic_risk_expansion_enabled"]
        )

    def test_legacy_forward_validation_file_is_not_read_or_returned(self) -> None:
        legacy_path = self.tmp_path / "forward_validation_latest.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "report_type": "ashare_forward_validation",
                    "labels": [
                        {
                            "status": "labeled",
                            "strategy_sample_valid": True,
                            "trade_date": "20260708",
                            "labels": {
                                "m30": {"status": "labeled", "return_pct": -1.0},
                                "m60": {"status": "labeled", "return_pct": -1.0},
                                "close": {"status": "labeled", "return_pct": -1.0},
                            },
                        }
                    ]
                    * 10,
                    "real_trading_enabled": False,
                }
            ),
            encoding="utf-8",
        )

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
            sample_kpi_path=self.tmp_path / "sample_kpi_latest.json",
        )

        self.assertEqual(result["min_score_gap"], 0.12)
        self.assertNotIn("forward_validation_summary", result)
        self.assertNotIn("poor_recent_forward_validation", result["reasons"])

    def test_widens_gap_when_position_scores_are_low(self) -> None:
        positions = [
            {
                "ts_code": "600000.SH",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
            }
        ]
        scores_by_symbol = {"600000.SH": {"combined": 0.60}}

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=positions,
            scores_by_symbol=scores_by_symbol,
        )

        self.assertGreater(result["min_score_gap"], 0.12)
        self.assertIn("low_position_score", result["reasons"])

    def test_legacy_local_trade_sample_quality_no_longer_auto_tightens_policy(
        self,
    ) -> None:
        trades = [
            {
                "trade_id": f"t{i}",
                "market": "ashare",
                "trade_date": "2026-07-08",
                "ts_code": "600000.SH",
                "side": "buy",
                "filled_price": 10.0,
                "quantity": 100,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "fill_price_source": "market_snapshot",
                "trade_timestamp_bj": "2026-07-08T10:00:00+08:00",
            }
            for i in range(2)
        ]
        # Inject two invalid samples to pull the valid ratio below 0.7.
        trades.extend(
            [
                {
                    "trade_id": "t_invalid_1",
                    "market": "ashare",
                    "trade_date": "2026-07-08",
                    "ts_code": "600001.SH",
                    "side": "buy",
                    "filled_price": 10.0,
                    "quantity": 100,
                    "candidate_pool_layer": "watch",
                    "execution_source": "ashare_candidate_layer",
                    "fill_price_source": "market_snapshot",
                    "trade_timestamp_bj": "2026-07-08T10:00:00+08:00",
                },
                {
                    "trade_id": "t_invalid_2",
                    "market": "ashare",
                    "trade_date": "2026-07-08",
                    "ts_code": "600002.SH",
                    "side": "buy",
                    "filled_price": 10.0,
                    "quantity": 100,
                    "candidate_pool_layer": "candidate",
                    "execution_source": "orchestrator_sim_loop",
                    "fill_price_source": "market_snapshot",
                    "trade_timestamp_bj": "2026-07-08T10:00:00+08:00",
                },
            ]
        )
        trades_path = self._write_trades(trades)

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
            sample_kpi_path=self.tmp_path / "missing_sample_kpi.json",
            local_trades_path=trades_path,
        )

        self.assertEqual(result["min_score_gap"], 0.12)
        self.assertNotIn("poor_sample_quality", result["reasons"])

    def test_disabled_for_non_ashare_market(self) -> None:
        result = _ashare_opportunity_cost_thresholds(
            market="us",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
        )

        self.assertEqual(result["enabled"], False)
        self.assertEqual(result["min_score_gap"], 0.12)
        self.assertEqual(result["action"], "disabled")


class AshareRebalancePlanDynamicThresholdTest(unittest.TestCase):
    def test_rebalance_uses_dynamic_gap_and_records_action(self) -> None:
        positions = [
            {
                "ts_code": "600000.SH",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
            }
        ]
        scores_by_symbol = {
            "600000.SH": {"combined": 0.70},
            "000001.SZ": {"combined": 0.82},
        }
        capital_plan = {"enabled": True, "target_positions": 1, "max_new_positions": 1}
        buy_candidates = [{"ts_code": "000001.SZ", "combined": 0.82}]

        result = _ashare_rebalance_plan(
            market="ashare",
            date="20260709",
            reader=StubReader(),
            existing_positions=positions,
            capital_plan=capital_plan,
            scores_by_symbol=scores_by_symbol,
            max_portfolio_positions=1,
            default_price=10.0,
            capital=200000.0,
            buy_candidates=buy_candidates,
        )

        self.assertIn("dynamic_thresholds", result)
        self.assertEqual(result["dynamic_thresholds"]["min_score_gap"], 0.13)
        self.assertEqual(result["dynamic_thresholds"]["action"], "widened_gap")
        # Gap 0.12 is below the widened threshold 0.13, so no opportunity-cost sell.
        opportunity_sells = [
            row
            for row in result["sells"]
            if "opportunity_cost" in (row.get("rebalance_reasons") or [])
        ]
        self.assertEqual(len(opportunity_sells), 0)

    def test_rebalance_still_sells_when_gap_exceeds_widened_threshold(self) -> None:
        positions = [
            {
                "ts_code": "600000.SH",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
            }
        ]
        scores_by_symbol = {
            "600000.SH": {"combined": 0.60},
            "000001.SZ": {"combined": 0.84},
        }
        capital_plan = {"enabled": True, "target_positions": 1, "max_new_positions": 1}
        buy_candidates = [{"ts_code": "000001.SZ", "combined": 0.84}]

        result = _ashare_rebalance_plan(
            market="ashare",
            date="20260709",
            reader=StubReader(),
            existing_positions=positions,
            capital_plan=capital_plan,
            scores_by_symbol=scores_by_symbol,
            max_portfolio_positions=1,
            default_price=10.0,
            capital=200000.0,
            buy_candidates=buy_candidates,
        )

        self.assertIn("dynamic_thresholds", result)
        # Position score 0.60 widens gap to 0.15; actual gap is 0.24.
        self.assertEqual(result["dynamic_thresholds"]["min_score_gap"], 0.15)
        opportunity_sells = [
            row
            for row in result["sells"]
            if "opportunity_cost" in (row.get("rebalance_reasons") or [])
        ]
        self.assertEqual(len(opportunity_sells), 1)
        self.assertEqual(opportunity_sells[0]["ts_code"], "600000.SH")

    def test_defensive_target_positions_zero_blocks_opportunity_cost_sells(
        self,
    ) -> None:
        """Risk-1 regression: defensive target_positions=0 must not allow
        opportunity_cost sells; stop_loss/score_drop risk sells are preserved."""
        positions = [
            {
                "ts_code": "600000.SH",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
            },
            {
                "ts_code": "000001.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
            },
        ]
        scores_by_symbol = {
            "600000.SH": {"combined": 0.65},
            "000001.SZ": {"combined": 0.65},
            "300001.SZ": {"combined": 0.85},
        }
        capital_plan = {
            "enabled": True,
            "target_positions": 0,
            "max_new_positions": 0,
            "risk_mode": "defensive",
        }
        buy_candidates = [{"ts_code": "300001.SZ", "combined": 0.85}]

        result = _ashare_rebalance_plan(
            market="ashare",
            date="20260709",
            reader=StubReader(),
            existing_positions=positions,
            capital_plan=capital_plan,
            scores_by_symbol=scores_by_symbol,
            max_portfolio_positions=3,
            default_price=10.0,
            capital=200000.0,
            buy_candidates=buy_candidates,
        )

        opportunity_sells = [
            row
            for row in result["sells"]
            if "opportunity_cost" in (row.get("rebalance_reasons") or [])
        ]
        self.assertEqual(
            len(opportunity_sells),
            0,
            "opportunity_cost sells must not be generated when target_positions=0 (defensive)",
        )

    def test_defensive_mode_still_allows_stop_loss_and_score_drop(self) -> None:
        """Risk-1 regression: defensive target_positions=0 preserves stop_loss
        and score_drop risk sells."""
        positions = [
            {
                "ts_code": "600000.SH",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 12.0,
                "last_price": 9.0,
            },
            {
                "ts_code": "000001.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
            },
        ]
        scores_by_symbol = {
            "600000.SH": {"combined": 0.45},
            "000001.SZ": {"combined": 0.65},
            "300001.SZ": {"combined": 0.85},
        }
        capital_plan = {
            "enabled": True,
            "target_positions": 0,
            "max_new_positions": 0,
            "risk_mode": "defensive",
        }
        buy_candidates = [{"ts_code": "300001.SZ", "combined": 0.85}]

        result = _ashare_rebalance_plan(
            market="ashare",
            date="20260709",
            reader=StubReader(),
            existing_positions=positions,
            capital_plan=capital_plan,
            scores_by_symbol=scores_by_symbol,
            max_portfolio_positions=3,
            default_price=10.0,
            capital=200000.0,
            buy_candidates=buy_candidates,
        )

        # stop_loss: 600000.SH avg_price=12, last_price=9 -> pnl_pct = -0.25 <= -0.08
        stop_loss_sells = [
            row
            for row in result["sells"]
            if "stop_loss" in (row.get("rebalance_reasons") or [])
        ]
        # score_drop: 000001.SZ combined=0.45 < 0.55
        score_drop_sells = [
            row
            for row in result["sells"]
            if "score_drop" in (row.get("rebalance_reasons") or [])
        ]
        # No opportunity_cost
        opportunity_sells = [
            row
            for row in result["sells"]
            if "opportunity_cost" in (row.get("rebalance_reasons") or [])
        ]
        self.assertGreaterEqual(
            len(stop_loss_sells),
            1,
            "stop_loss sells must be preserved in defensive mode",
        )
        self.assertGreaterEqual(
            len(score_drop_sells),
            1,
            "score_drop sells must be preserved in defensive mode",
        )
        self.assertEqual(
            len(opportunity_sells),
            0,
            "opportunity_cost sells must be blocked in defensive mode",
        )


if __name__ == "__main__":
    unittest.main()
