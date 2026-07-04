#!/usr/bin/env python3
"""Tests for China futures simulated execution."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CNFuturesSimTest(unittest.TestCase):
    def test_contract_rules_calculate_margin_and_round_trip_fee(self) -> None:
        from CNFutures.contract_rules import get_contract_rule
        from CNFutures.margin_model import estimate_order_cost
        from CNFutures.contract_rules import normalize_product

        rule = get_contract_rule("rb2601")
        suffixed_rule = get_contract_rule("RB2601.SHF")
        cost = estimate_order_cost(
            symbol="rb2601",
            side="buy",
            quantity=2,
            price=3500.0,
        )

        self.assertEqual(rule.exchange, "SHFE")
        self.assertEqual(suffixed_rule.product, "rb")
        self.assertEqual(normalize_product("I2509.DCE"), "i")
        self.assertEqual(rule.contract_multiplier, 10)
        self.assertEqual(cost.notional, 70000.0)
        self.assertEqual(cost.margin_required, 9100.0)
        self.assertEqual(cost.open_fee, 7.0)
        self.assertEqual(cost.estimated_close_fee, 7.0)
        self.assertEqual(cost.total_estimated_fee, 14.0)

    def test_sim_executor_registers_cn_futures_as_simulated_only(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        result = execute_sim_order(
            order={
                "order_id": "SIM-CNF-1",
                "symbol": "rb2601",
                "side": "buy",
                "quantity": 2,
                "price": 3500.0,
            },
            market="cn_futures",
            account={"account": "simnow"},
            config={"fee_mode": "round_trip_estimate"},
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.market, "cn_futures")
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.filled_qty, 2)
        self.assertEqual(result.avg_price, 3500.0)
        self.assertEqual(result.fee, 14.0)
        self.assertEqual(result.raw_response["symbol"], "rb2601")
        self.assertEqual(result.raw_response["contract_multiplier"], 10)
        self.assertEqual(result.raw_response["margin_required"], 9100.0)
        self.assertFalse(result.raw_response["real_trading_enabled"])

    def test_review_summarizes_errors_and_style_health(self) -> None:
        from CNFutures.review import summarize_errors, style_health

        errors = [
            {
                "stage": "data",
                "style": "trend",
                "symbol": "RB2601.SHF",
                "error": "stale_intraday_bar",
            },
            {
                "stage": "risk",
                "style": "breakout",
                "symbol": "RB2601.SHF",
                "error": "repeated_same_side_exposure",
            },
        ]
        records = [
            {
                "style": "breakout",
                "receipt": {"status": "filled"},
            }
        ]

        summary = summarize_errors(errors)
        health = style_health(records, errors)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_error"]["stale_intraday_bar"], 1)
        self.assertEqual(summary["by_stage"]["risk"], 1)
        self.assertEqual(health["trend"]["status"], "blocked")
        self.assertEqual(health["breakout"]["status"], "degraded")
        self.assertEqual(health["trend"]["suggested_action"], "inspect_data_or_risk_gate")

    def test_append_review_writes_dashboard_style_outputs(self) -> None:
        from CNFutures.review import append_review

        with tempfile.TemporaryDirectory() as tmp:
            review_path = Path(tmp) / "shared" / "review" / "data" / "cn_futures_sim_reviews.jsonl"
            record = {
                "style": "trend",
                "receipt": {
                    "status": "filled",
                    "fee": 2.0,
                    "raw_response": {"margin_required": 100.0, "notional": 1000.0},
                },
                "performance": {"realized_pnl": 5.0},
            }

            payload = append_review(
                date="20260706",
                market="cn_futures",
                records=[record],
                errors=[],
                path=review_path,
            )

            style_path = Path(payload["style_output_paths"]["style_comparison"])
            perf_path = Path(payload["style_output_paths"]["style_performance"])
            style_payload = json.loads(style_path.read_text(encoding="utf-8"))
            perf_rows = [json.loads(line) for line in perf_path.read_text(encoding="utf-8").splitlines() if line.strip()]

            self.assertTrue(style_path.exists())
            self.assertTrue(perf_path.exists())
            self.assertEqual(style_payload["market"], "cn_futures")
            self.assertEqual(style_payload["style_comparison"][0]["style_name"], "trend")
            self.assertEqual(perf_rows[0]["market"], "cn_futures")
            self.assertFalse(perf_rows[0]["real_execution"])


if __name__ == "__main__":
    unittest.main()
