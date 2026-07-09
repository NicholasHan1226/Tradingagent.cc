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
    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, float]]:
        return [{"close": 10.0}]

    def get_bars_intraday(self, market: str, symbol: str, interval: str = "5m", start: str = "", end: str = "") -> list[dict[str, float]]:
        return []


class AshareOpportunityCostThresholdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)

    def _write_forward_validation(self, labels: list[dict[str, object]]) -> Path:
        path = self.tmp_path / "forward_validation_latest.json"
        report = {
            "market": "ashare",
            "report_type": "ashare_forward_validation",
            "date": "",
            "generated_at": "2026-07-09T12:00:00+00:00",
            "trade_count": len(labels),
            "strategy_label_count": sum(1 for row in labels if row.get("status") == "labeled"),
            "pending_count": 0,
            "labels": labels,
            "read_only": True,
            "real_trading_enabled": False,
        }
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
        self.assertIn("forward_validation_summary", result)
        self.assertIn("sample_quality_summary", result)

    def test_never_drops_below_hard_floor(self) -> None:
        labels = [
            {
                "trade_id": "t1",
                "symbol": "600000.SH",
                "trade_date": "20260708",
                "side": "buy",
                "entry_price": 10.0,
                "strategy_sample_valid": True,
                "status": "labeled",
                "labels": {
                    "m30": {"status": "labeled", "return_pct": 0.10},
                    "m60": {"status": "labeled", "return_pct": 0.12},
                    "close": {"status": "labeled", "return_pct": 0.15},
                    "next_day": {
                        "status": "labeled",
                        "open_return_pct": 0.10,
                        "high_return_pct": 0.20,
                        "close_return_pct": 0.15,
                    },
                },
            }
        ]
        fv_path = self._write_forward_validation(labels)
        trades = [
            {
                "trade_id": "t1",
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
        ]
        trades_path = self._write_trades(trades)

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
            forward_validation_path=fv_path,
            local_trades_path=trades_path,
        )

        self.assertGreaterEqual(result["min_score_gap"], 0.12)

    def test_widens_gap_when_recent_forward_validation_is_poor(self) -> None:
        labels = [
            {
                "trade_id": f"t{i}",
                "symbol": "600000.SH",
                "trade_date": "20260708",
                "side": "buy",
                "entry_price": 10.0,
                "strategy_sample_valid": True,
                "status": "labeled",
                "labels": {
                    "m30": {"status": "labeled", "return_pct": -0.02},
                    "m60": {"status": "labeled", "return_pct": -0.03},
                    "close": {"status": "labeled", "return_pct": -0.01},
                    "next_day": {
                        "status": "labeled",
                        "open_return_pct": -0.01,
                        "high_return_pct": 0.01,
                        "close_return_pct": -0.02,
                    },
                },
            }
            for i in range(5)
        ]
        fv_path = self._write_forward_validation(labels)

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
            forward_validation_path=fv_path,
        )

        self.assertGreater(result["min_score_gap"], 0.12)
        self.assertEqual(result["action"], "widened_gap")
        self.assertIn("poor_recent_forward_validation", result["reasons"])

    def test_widens_gap_when_position_scores_are_low(self) -> None:
        positions = [
            {"ts_code": "600000.SH", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0}
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

    def test_widens_gap_when_sample_quality_is_poor(self) -> None:
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
        trades.extend([
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
        ])
        trades_path = self._write_trades(trades)

        result = _ashare_opportunity_cost_thresholds(
            market="ashare",
            date="20260709",
            min_entry_score=0.70,
            min_score_gap=0.12,
            existing_positions=[],
            scores_by_symbol={},
            local_trades_path=trades_path,
        )

        self.assertGreater(result["min_score_gap"], 0.12)
        self.assertIn("poor_sample_quality", result["reasons"])

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
            {"ts_code": "600000.SH", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0}
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
            row for row in result["sells"]
            if "opportunity_cost" in (row.get("rebalance_reasons") or [])
        ]
        self.assertEqual(len(opportunity_sells), 0)

    def test_rebalance_still_sells_when_gap_exceeds_widened_threshold(self) -> None:
        positions = [
            {"ts_code": "600000.SH", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0}
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
            row for row in result["sells"]
            if "opportunity_cost" in (row.get("rebalance_reasons") or [])
        ]
        self.assertEqual(len(opportunity_sells), 1)
        self.assertEqual(opportunity_sells[0]["ts_code"], "600000.SH")


if __name__ == "__main__":
    unittest.main()
