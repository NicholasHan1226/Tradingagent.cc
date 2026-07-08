from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.runtime_test import ashare_no_trade_summary
from shared.runtime_test.ashare_no_trade_summary import summarize_no_trade_log, summarize_trade_source_check


class AshareNoTradeSummaryTest(unittest.TestCase):
    def _log(self, rows: list[dict[str, object]]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "ashare_no_trade_explanations.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        return path

    def _trade_log(self, rows: list[dict[str, object]]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "local_sim_trades.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_summary_marks_new_candidate_evidence_ready(self) -> None:
        path = self._log(
            [
                {
                    "date": "20260708",
                    "no_trade_explanation": {
                        "category": "no_portfolio_orders",
                        "counts": {"universe": 3213, "candidates": 3, "orders": 0, "risk_rejections": 0},
                        "candidate_decision_trace": [{"symbol": "AAA", "drop_reason": "capital_plan_capacity_zero"}],
                        "capital_plan_decision": {"position_capacity": 0, "risk_mode": "defensive"},
                        "portfolio_decision": {"allowed_buy_count": 0},
                    },
                }
            ]
        )

        report = summarize_no_trade_log(path, "2026-07-08")

        self.assertEqual(report["row_count"], 1)
        self.assertEqual(report["category_counts"], {"no_portfolio_orders": 1})
        self.assertEqual(report["count_ranges"]["universe"]["latest"], 3213)
        self.assertEqual(report["count_ranges"]["candidates"]["latest"], 3)
        self.assertEqual(report["count_ranges"]["orders"]["latest"], 0)
        self.assertEqual(report["evidence_status"], "ready")
        self.assertEqual(report["evidence_gaps"], [])

    def test_summary_flags_legacy_candidate_order_gap_as_incomplete(self) -> None:
        path = self._log(
            [
                {
                    "date": "20260708",
                    "no_trade_explanation": {
                        "category": "no_portfolio_orders",
                        "counts": {"universe": 3213, "candidates": 3, "orders": 0},
                    },
                }
            ]
        )

        report = summarize_no_trade_log(path, "20260708")

        self.assertEqual(report["evidence_status"], "incomplete")
        self.assertEqual(
            report["evidence_gaps"],
            [
                "candidate_decision_trace_missing",
                "capital_plan_decision_missing",
                "portfolio_decision_missing",
            ],
        )

    def test_summary_aggregates_same_day_rows_and_uses_latest_counts(self) -> None:
        path = self._log(
            [
                {
                    "date": "20260708",
                    "no_trade_explanation": {
                        "category": "all_candidates_missing_price",
                        "counts": {"universe": 3214, "candidates": 3, "orders": 0, "skipped_candidates": 3},
                    },
                },
                {
                    "date": "20260708",
                    "no_trade_explanation": {
                        "category": "no_portfolio_orders",
                        "counts": {"universe": 3213, "candidates": 3, "orders": 0, "skipped_candidates": 0},
                        "candidate_decision_trace": [{"symbol": "AAA", "drop_reason": "capital_plan_capacity_zero"}],
                        "capital_plan_decision": {"position_capacity": 0},
                        "portfolio_decision": {"allowed_buy_count": 0},
                    },
                },
            ]
        )

        report = summarize_no_trade_log(path, "20260708")

        self.assertEqual(report["row_count"], 2)
        self.assertEqual(report["category_counts"]["all_candidates_missing_price"], 1)
        self.assertEqual(report["category_counts"]["no_portfolio_orders"], 1)
        self.assertEqual(report["count_ranges"]["universe"], {"min": 3213, "max": 3214, "latest": 3213})
        self.assertEqual(report["count_ranges"]["skipped_candidates"], {"min": 0, "max": 3, "latest": 0})
        self.assertEqual(report["latest_no_trade_log"]["category"], "no_portfolio_orders")

    def test_summary_reports_no_rows_for_unmatched_date(self) -> None:
        path = self._log(
            [
                {
                    "date": "20260708",
                    "no_trade_explanation": {
                        "category": "no_candidates",
                        "counts": {"universe": 3213, "candidates": 0, "orders": 0},
                    },
                }
            ]
        )

        report = summarize_no_trade_log(path, "20260709")

        self.assertEqual(report["row_count"], 0)
        self.assertEqual(report["evidence_status"], "no_rows")
        self.assertEqual(report["category_counts"], {})

    def test_cli_write_latest_writes_read_only_report(self) -> None:
        path = self._log(
            [
                {
                    "date": "20260708",
                    "no_trade_explanation": {
                        "category": "no_candidates",
                        "counts": {"universe": 3213, "candidates": 0, "orders": 0},
                    },
                }
            ]
        )
        latest = path.parent / "latest.json"

        with patch.object(ashare_no_trade_summary, "LATEST_REPORT", latest), patch(
            "sys.argv",
            [
                "ashare_no_trade_summary.py",
                "--log-path",
                str(path),
                "--date",
                "20260708",
                "--write-latest",
            ],
        ):
            self.assertEqual(ashare_no_trade_summary.main(), 0)

        report = json.loads(latest.read_text(encoding="utf-8"))
        self.assertEqual(report["report_type"], "ashare_no_trade_summary")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["real_trading_enabled"])

    def test_trade_source_check_accepts_traceable_candidate_buy_and_rebalance_sell(self) -> None:
        path = self._trade_log(
            [
                {
                    "filled_at": "2026-07-08T10:05:00+08:00",
                    "status": "filled",
                    "side": "buy",
                    "symbol": "600000.SH",
                    "execution_source": "ashare_sim_loop",
                    "candidate_pool_layer": "candidate",
                },
                {
                    "filled_at": "2026-07-08T10:15:00+08:00",
                    "status": "filled",
                    "side": "sell",
                    "symbol": "600010.SH",
                    "execution_source": "ashare_rebalance_sell",
                    "candidate_pool_layer": "ashare_rebalance_sell",
                },
            ]
        )

        report = summarize_trade_source_check(path, "20260708")

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["filled_count"], 2)
        self.assertEqual(report["invalid_rows"], [])

    def test_trade_source_check_flags_missing_or_invalid_source_evidence(self) -> None:
        path = self._trade_log(
            [
                {
                    "filled_at": "2026-07-08T10:05:00+08:00",
                    "status": "filled",
                    "side": "buy",
                    "symbol": "600000.SH",
                    "candidate_pool_layer": "watch",
                },
                {
                    "filled_at": "2026-07-08T10:15:00+08:00",
                    "status": "filled",
                    "side": "sell",
                    "symbol": "600010.SH",
                    "execution_source": "manual",
                    "candidate_pool_layer": "candidate",
                },
            ]
        )

        report = summarize_trade_source_check(path, "20260708")

        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["filled_count"], 2)
        self.assertEqual(report["missing_source_count"], 1)
        self.assertEqual(report["invalid_layer_count"], 2)


if __name__ == "__main__":
    unittest.main()
