from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from shared.runtime_test import ashare_opening_validator


class AshareOpeningValidatorTest(unittest.TestCase):
    def _db(self, intraday_rows: list[tuple[str, str]], daily_rows: list[tuple[str, str, float]] | None = None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / "marketdata.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE market_bars_intraday (
                market TEXT,
                symbol TEXT,
                bar_time TEXT,
                interval TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE market_bars_daily (
                market TEXT,
                symbol TEXT,
                trade_date TEXT,
                close REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO market_bars_intraday VALUES (?, ?, ?, ?)",
            [("Ashare", symbol, bar_time, "5min") for symbol, bar_time in intraday_rows],
        )
        for row in daily_rows or []:
            conn.execute("INSERT INTO market_bars_daily VALUES (?, ?, ?, ?)", ("Ashare", row[0], row[1], row[2]))
        conn.commit()
        conn.close()
        return path

    def test_pre_open_passes_when_daily_bars_ready(self) -> None:
        db_path = self._db([], [("600000.SH", "20260706", 10.0), ("000001.SZ", "20260706", 12.0)])
        report = ashare_opening_validator.validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "pre_open_acceptance_passed")
        self.assertFalse(report["real_trading_enabled"])

    def test_pre_open_warns_when_daily_bars_missing(self) -> None:
        db_path = self._db([])
        report = ashare_opening_validator.validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "pre_open_daily_bars_missing")

    def test_pre_open_warns_when_latest_daily_bars_are_stale(self) -> None:
        db_path = self._db([], [("600000.SH", "20260625", 10.0), ("000001.SZ", "20260625", 12.0)])
        report = ashare_opening_validator.validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "pre_open_daily_bars_stale")
        self.assertGreater(report["latest_daily_age_days"], report["max_daily_age_days"])

    def test_opening_passes_with_5min_bars(self) -> None:
        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.validate_opening(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "opening_session_5min_data_ready")
        self.assertEqual(report["symbol_count"], 2)

    def test_opening_warns_outside_session(self) -> None:
        db_path = self._db([])
        report = ashare_opening_validator.validate_opening(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T16:00:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "outside_ashare_session")

    def test_first_sample_alerts_when_bars_exist_but_no_trade_sample(self) -> None:
        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            sqlite_db=db_path,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )
        self.assertEqual(report["status"], "warn")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("ashare_first_sim_trade_missing", codes)
        self.assertNotIn("ashare_first_receipt_missing", codes)
        self.assertIn("ashare_review_not_yet_run", codes)
        self.assertEqual(report["no_trade_explanation"]["category"], "no_signal_cards_created")
        self.assertEqual(report["no_trade_explanation"]["next_action"], "check_signal_generation_thresholds")

    def test_first_sample_alerts_when_trade_sample_has_no_receipt(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        local_sim = root / "local_sim_trades.jsonl"
        local_sim.write_text(json.dumps({"trade_id": "t1", "trade_date": "20260706"}) + "\n", encoding="utf-8")
        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            sqlite_db=db_path,
            local_sim_path=local_sim,
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("ashare_first_receipt_missing", codes)
        self.assertEqual(report["no_trade_explanation"]["category"], "receipt_missing")

    def test_first_sample_ready_when_all_samples_present(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        local_sim = root / "local_sim_trades.jsonl"
        local_sim.write_text(json.dumps({"trade_id": "t1", "trade_date": "20260706"}) + "\n", encoding="utf-8")
        receipts = root / "receipts.jsonl"
        receipts.write_text(
            json.dumps({"market": "ashare", "trade_date": "20260706", "receipt_at": "2026-07-06T09:35:00+08:00"}) + "\n",
            encoding="utf-8",
        )
        review = root / "daily_reviews.jsonl"
        review.write_text(json.dumps({"session": "close"}) + "\n", encoding="utf-8")
        signals = root / "signals"
        filled = signals / "filled"
        filled.mkdir(parents=True)
        (filled / "SIM-ASHARE-1.json").write_text(
            json.dumps({"market": "ashare", "trade_date": "20260706"}), encoding="utf-8"
        )

        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            sqlite_db=db_path,
            local_sim_path=local_sim,
            receipt_path=receipts,
            review_path=review,
            signals_dir=signals,
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "first_sample_ready")
        self.assertEqual(report["samples"]["local_sim_trades"], 1)
        self.assertEqual(report["samples"]["sim_execution_receipts"], 1)
        self.assertEqual(report["samples"]["filled_signals"], 1)
        self.assertEqual(report["no_trade_explanation"]["category"], "trade_loop_ready")

    def test_first_sample_ignores_old_local_sim_trades(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        local_sim = root / "local_sim_trades.jsonl"
        local_sim.write_text(json.dumps({"trade_id": "old", "trade_date": "20260703"}) + "\n", encoding="utf-8")
        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            sqlite_db=db_path,
            local_sim_path=local_sim,
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["samples"]["local_sim_trades"], 0)
        self.assertEqual(report["no_trade_explanation"]["category"], "no_signal_cards_created")

    def test_first_sample_uses_latest_no_trade_log_for_precise_category(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        no_trade_log = root / "ashare_no_trade_explanations.jsonl"
        no_trade_log.write_text(
            json.dumps(
                {
                    "date": "20260706",
                    "generated_at": "2026-07-06T09:41:00+08:00",
                    "state": "ok",
                    "no_trade_explanation": {
                        "category": "all_rejected_by_risk",
                        "action": "review_risk_rejections",
                        "counts": {"risk_rejections": 3},
                        "score_diagnostics": {
                            "scored_count": 500,
                            "candidate_threshold": 0.55,
                            "top_scores": [{"symbol": "000623.SZ", "combined": 0.6764}],
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            sqlite_db=db_path,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            no_trade_log_path=no_trade_log,
            signals_dir=Path("/tmp/nonexistent-signals"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["no_trade_explanation"]["category"], "all_rejected_by_risk")
        self.assertEqual(report["no_trade_explanation"]["next_action"], "review_risk_rejections")
        self.assertEqual(report["no_trade_explanation"]["latest_no_trade_log"]["counts"]["risk_rejections"], 3)
        self.assertEqual(report["no_trade_explanation"]["latest_no_trade_log"]["score_diagnostics"]["scored_count"], 500)
        self.assertEqual(report["no_trade_explanation"]["latest_no_trade_log"]["score_diagnostics"]["top_scores"][0]["symbol"], "000623.SZ")

    def test_first_sample_surfaces_score_diagnostics_for_no_trade(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        no_trade_log = root / "ashare_no_trade_explanations.jsonl"
        no_trade_log.write_text(
            json.dumps(
                {
                    "date": "20260706",
                    "generated_at": "2026-07-06T09:41:00+08:00",
                    "state": "ok",
                    "no_trade_explanation": {
                        "category": "no_candidates",
                        "action": "check_candidate_pool_thresholds_and_universe_filter",
                        "counts": {"candidate_count": 0, "watch_count": 8},
                        "score_diagnostics": {
                            "scored_count": 500,
                            "candidate_threshold": 0.55,
                            "candidate_above_threshold_count": 0,
                            "watch_above_threshold_count": 8,
                            "max_combined": 0.5412,
                            "candidate_pool_status": "strategy_threshold_not_met_watch_only",
                            "data_quality_status": "research_dimensions_mostly_neutral",
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            sqlite_db=db_path,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            no_trade_log_path=no_trade_log,
            signals_dir=Path("/tmp/nonexistent-signals"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        explanation = report["no_trade_explanation"]
        latest_log = explanation["latest_no_trade_log"]
        self.assertEqual(explanation["category"], "no_candidates")
        self.assertEqual(explanation["next_action"], "review_research_dimension_coverage")
        self.assertEqual(explanation["diagnostic_summary"]["reason"], "research_dimensions_neutral")
        self.assertEqual(latest_log["candidate_pool_status"], "strategy_threshold_not_met_watch_only")
        self.assertEqual(latest_log["data_quality_status"], "research_dimensions_mostly_neutral")
        self.assertEqual(latest_log["max_combined"], 0.5412)

    def test_first_sample_maps_evidence_reason_to_targeted_next_actions(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        no_trade_log = root / "ashare_no_trade_explanations.jsonl"
        no_trade_log.write_text(
            json.dumps(
                {
                    "date": "20260706",
                    "generated_at": "2026-07-06T09:41:00+08:00",
                    "state": "ok",
                    "no_trade_explanation": {
                        "category": "no_candidates",
                        "action": "check_candidate_pool_thresholds_and_universe_filter",
                        "counts": {"candidate_count": 0},
                        "score_diagnostics": {
                            "scored_count": 500,
                            "candidate_threshold": 0.55,
                            "candidate_above_threshold_count": 0,
                            "watch_above_threshold_count": 0,
                            "max_combined": 0.5,
                            "candidate_pool_status": "strategy_threshold_not_met",
                            "data_quality_status": "missing_evidence_default_like",
                            "evidence_reason_summary": {
                                "capital": {"missing_capital_flow_rows": 500},
                                "technical": {"insufficient_daily_bars": 500},
                            },
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        db_path = self._db(
            [
                ("600000.SH", "2026-07-06 09:35:00"),
                ("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            sqlite_db=db_path,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            no_trade_log_path=no_trade_log,
            signals_dir=Path("/tmp/nonexistent-signals"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        explanation = report["no_trade_explanation"]
        self.assertEqual(explanation["next_action"], "check_sharedsignals_daily_bar_history")
        self.assertEqual(explanation["diagnostic_summary"]["reason"], "research_evidence_missing_default_neutral")
        self.assertEqual(
            explanation["diagnostic_summary"]["next_actions"],
            ["check_sharedsignals_daily_bar_history", "check_sharedsignals_capital_flow"],
        )

    def test_score_diagnostic_summary_distinguishes_no_trade_causes(self) -> None:
        cases = [
            (
                {
                    "candidate_pool_status": "strategy_threshold_not_met",
                    "data_quality_status": "ok",
                    "candidate_above_threshold_count": 0,
                    "watch_above_threshold_count": 0,
                    "max_combined": 0.532,
                    "candidate_threshold": 0.55,
                },
                "strategy_threshold_not_met",
                "monitor_strategy_threshold_gap",
            ),
            (
                {
                    "candidate_pool_status": "strategy_threshold_not_met_watch_only",
                    "data_quality_status": "research_dimensions_mostly_neutral",
                    "candidate_above_threshold_count": 0,
                    "watch_above_threshold_count": 8,
                    "max_combined": 0.5412,
                    "candidate_threshold": 0.55,
                },
                "research_dimensions_neutral",
                "review_research_dimension_coverage",
            ),
            (
                {
                    "candidate_pool_status": "strategy_threshold_not_met_watch_only",
                    "data_quality_status": "missing_evidence_default_like",
                    "candidate_above_threshold_count": 0,
                    "watch_above_threshold_count": 8,
                    "max_combined": 0.5,
                    "candidate_threshold": 0.55,
                },
                "research_evidence_missing_default_neutral",
                "review_sharedsignals_marketgraph_dimension_evidence",
            ),
            (
                {
                    "candidate_pool_status": "no_scored_symbols",
                    "data_quality_status": "ok",
                    "candidate_above_threshold_count": 0,
                    "watch_above_threshold_count": 0,
                    "max_combined": 0,
                    "candidate_threshold": 0.55,
                },
                "score_coverage_missing",
                "check_ashare_score_universe_and_data_reader",
            ),
            (
                {
                    "candidate_pool_status": "pool_empty_despite_threshold_scores",
                    "data_quality_status": "ok",
                    "candidate_above_threshold_count": 2,
                    "watch_above_threshold_count": 0,
                    "max_combined": 0.6764,
                    "candidate_threshold": 0.55,
                },
                "candidate_pool_anomaly",
                "review_candidate_pool_layering_anomaly",
            ),
        ]

        for diagnostics, reason, next_action in cases:
            with self.subTest(reason=reason):
                summary = ashare_opening_validator._score_diagnostic_summary(diagnostics)
                self.assertEqual(summary["reason"], reason)
                self.assertEqual(summary["next_action"], next_action)


if __name__ == "__main__":
    unittest.main()
