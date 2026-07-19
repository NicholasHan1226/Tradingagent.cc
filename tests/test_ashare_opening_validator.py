from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from shared.runtime_test import ashare_opening_validator


def _mock_reader(
    *,
    assets: list[dict] | None = None,
    daily_rows: list[dict] | None = None,
    intraday_rows: list[dict] | None = None,
    api_available: bool = True,
) -> MagicMock:
    """Build a mock TradingagentDataReader with controlled API responses."""
    reader = MagicMock()

    if not api_available:
        reader.get_assets.side_effect = ConnectionError("API unavailable")
        reader.get_latest_daily_batch.side_effect = ConnectionError("API unavailable")
        reader.get_realtime_5min_batch.side_effect = ConnectionError("API unavailable")
        return reader

    reader.get_assets.return_value = assets or []
    reader.get_latest_daily_batch.return_value = daily_rows or []
    reader.get_realtime_5min_batch.return_value = intraday_rows or []

    return reader


def _daily_row(symbol: str, trade_date: str, close: float, amount: float = 100_000.0) -> dict:
    return {
        "symbol": symbol,
        "ts_code": symbol,
        "trade_date": trade_date,
        "close": close,
        "amount": amount,
    }


def _asset_row(symbol: str) -> dict:
    """Return an asset row. symbol should include suffix like '600000.SH'."""
    return {"symbol": symbol, "ts_code": symbol, "name": f"Test{symbol.split('.')[0]}", "status": "active"}


def _intraday_row(symbol: str, bar_time: str) -> dict:
    return {"symbol": symbol, "ts_code": symbol, "bar_time": bar_time, "trade_date": bar_time[:10].replace("-", "")}


class AshareOpeningValidatorTest(unittest.TestCase):
    # -- validate_pre_open tests --

    def test_pre_open_passes_when_daily_bars_ready(self) -> None:
        reader = _mock_reader(
            assets=[_asset_row("600000.SH"), _asset_row("000001.SZ")],
            daily_rows=[
                _daily_row("600000.SH", "20260706", 10.0),
                _daily_row("000001.SZ", "20260706", 12.0),
            ],
        )
        report = ashare_opening_validator.validate_pre_open(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "pass")
        self.assertIn("api_daily", report.get("reason", ""))
        self.assertFalse(report["real_trading_enabled"])
        data_source = report.get("data_source", "")
        self.assertIn("legacy compatibility bulk reader", data_source)
        self.assertNotIn("SharedSignals API", data_source)

    def test_pre_open_fails_when_daily_bars_missing(self) -> None:
        reader = _mock_reader(assets=[_asset_row("600000.SH"), _asset_row("000001.SZ")], daily_rows=[])
        report = ashare_opening_validator.validate_pre_open(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "api_daily_bars_missing")

    def test_pre_open_fails_when_daily_bars_stale(self) -> None:
        reader = _mock_reader(
            assets=[_asset_row("600000.SH"), _asset_row("000001.SZ")],
            daily_rows=[
                _daily_row("600000.SH", "20260625", 10.0),
                _daily_row("000001.SZ", "20260625", 12.0),
            ],
        )
        report = ashare_opening_validator.validate_pre_open(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertIn("stale", report.get("reason", ""))
        self.assertGreater(report["latest_daily_age_days"], report["max_daily_age_days"])

    def test_pre_open_fails_when_coverage_below_threshold(self) -> None:
        reader = _mock_reader(
            assets=[_asset_row(f"{600000 + i:06d}.SH") for i in range(100)],
            daily_rows=[_daily_row(f"{600000 + i:06d}.SH", "20260706", 10.0) for i in range(44)],
        )
        report = ashare_opening_validator.validate_pre_open(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
            min_coverage_ratio=0.90,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "api_daily_coverage_incomplete")
        self.assertLess(report["daily_coverage_ratio"], 0.90)

    def test_pre_open_fails_when_api_unavailable(self) -> None:
        reader = _mock_reader(api_available=False)
        report = ashare_opening_validator.validate_pre_open(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "api_daily_unavailable")

    def test_pre_open_outside_window_returns_closed(self) -> None:
        reader = _mock_reader()
        report = ashare_opening_validator.validate_pre_open(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T16:00:00+08:00"),
        )
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "not_in_pre_open_window")

    # -- validate_opening tests --

    def test_opening_passes_with_5min_bars(self) -> None:
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "opening_session_5min_data_ready")
        self.assertEqual(report["symbol_count"], 2)
        self.assertEqual(report["data_source"], "SharedSignals API")

    def test_opening_warns_outside_session(self) -> None:
        reader = _mock_reader()
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T16:00:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "outside_ashare_session")

    def test_opening_fails_when_no_5min_bars(self) -> None:
        reader = _mock_reader(intraday_rows=[])
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "opening_session_has_no_5min_bars")

    def test_opening_fails_when_api_unavailable(self) -> None:
        reader = _mock_reader(api_available=False)
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "opening_validation_api_unavailable")

    # -- first_sample_alerts tests --

    def test_first_sample_alerts_when_bars_exist_but_no_trade_sample(self) -> None:
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
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
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
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
        local_sim.write_text(
            json.dumps({"trade_id": "t1", "order_id": "o1", "trade_date": "20260706"}) + "\n",
            encoding="utf-8",
        )
        receipts = root / "receipts.jsonl"
        receipts.write_text(
            json.dumps({"market": "ashare", "trade_id": "t1", "order_id": "o1", "trade_date": "20260706", "receipt_at": "2026-07-06T09:35:00+08:00"}) + "\n",
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

        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
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
        self.assertEqual(report["samples"]["receipt_audit"]["missing_receipt_count"], 0)
        self.assertEqual(report["samples"]["filled_signals"], 1)
        self.assertEqual(report["no_trade_explanation"]["category"], "trade_loop_ready")

    def test_first_sample_warns_when_trade_receipt_pair_is_missing(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        local_sim = root / "local_sim_trades.jsonl"
        local_sim.write_text(
            "\n".join(
                [
                    json.dumps({"trade_id": "t1", "order_id": "o1", "trade_date": "20260706", "ts_code": "600000.SH", "side": "buy", "quantity": 100}),
                    json.dumps({"trade_id": "t2", "order_id": "o2", "trade_date": "20260706", "ts_code": "600001.SH", "side": "buy", "quantity": 100}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        receipts = root / "receipts.jsonl"
        receipts.write_text(
            json.dumps({"market": "ashare", "trade_id": "t1", "order_id": "o1", "trade_date": "20260706"}) + "\n",
            encoding="utf-8",
        )
        review = root / "daily_reviews.jsonl"
        review.write_text(json.dumps({"session": "close"}) + "\n", encoding="utf-8")

        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
            local_sim_path=local_sim,
            receipt_path=receipts,
            review_path=review,
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["samples"]["receipt_audit"]["missing_receipt_count"], 1)
        self.assertEqual(report["samples"]["receipt_audit"]["missing_receipts"][0]["trade_id"], "t2")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("ashare_local_sim_orphan_trade", codes)

    def test_first_sample_ignores_old_local_sim_trades(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        local_sim = root / "local_sim_trades.jsonl"
        local_sim.write_text(json.dumps({"trade_id": "old", "trade_date": "20260703"}) + "\n", encoding="utf-8")
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
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
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
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

    def test_first_sample_no_portfolio_orders_is_observation_not_missing_execution(self) -> None:
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
                        "category": "no_portfolio_orders",
                        "action": "continue_monitoring_capital_plan",
                        "counts": {"candidate_count": 3, "orders": 0, "risk_rejections": 0},
                        "candidate_decision_trace": [{"symbol": "600000.SH", "drop_reason": "capital_plan_capacity_zero"}],
                        "capital_plan_decision": {"position_capacity": 0},
                        "portfolio_decision": {"allowed_buy_count": 0},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            no_trade_log_path=no_trade_log,
            signals_dir=Path("/tmp/nonexistent-signals"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "first_sample_no_trade_explained")
        self.assertEqual(report["no_trade_explanation"]["category"], "no_portfolio_orders")
        self.assertEqual(report["no_trade_explanation"]["latest_no_trade_log"]["evidence_status"], "ready")
        self.assertNotIn("ashare_first_sim_trade_missing", {alert["code"] for alert in report["alerts"]})

    def test_first_sample_warns_when_no_portfolio_orders_lacks_trace_evidence(self) -> None:
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
                        "category": "no_portfolio_orders",
                        "action": "continue_monitoring_capital_plan",
                        "counts": {"candidate_count": 3, "orders": 0},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            no_trade_log_path=no_trade_log,
            signals_dir=Path("/tmp/nonexistent-signals"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "first_sample_alerts_present")
        self.assertIn("ashare_first_sim_trade_missing", {alert["code"] for alert in report["alerts"]})
        latest_log = report["no_trade_explanation"]["latest_no_trade_log"]
        self.assertEqual(latest_log["evidence_status"], "incomplete")
        self.assertEqual(
            latest_log["evidence_gaps"],
            [
                "candidate_decision_trace_missing",
                "capital_plan_decision_missing",
                "portfolio_decision_missing",
            ],
        )

    def test_first_sample_warns_when_risk_rejections_lack_trace_evidence(self) -> None:
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
                        "counts": {"candidates": 3, "orders": 0, "risk_rejections": 3},
                        "sample_risk_rejections": [{"symbol": "600000.SH", "reasons": ["unit risk"]}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            no_trade_log_path=no_trade_log,
            signals_dir=Path("/tmp/nonexistent-signals"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["status"], "warn")
        self.assertIn("ashare_first_sim_trade_missing", {alert["code"] for alert in report["alerts"]})
        latest_log = report["no_trade_explanation"]["latest_no_trade_log"]
        self.assertEqual(latest_log["evidence_status"], "incomplete")
        self.assertIn("candidate_decision_trace_missing", latest_log["evidence_gaps"])

    def test_first_sample_api_unavailable_fail_closed(self) -> None:
        reader = _mock_reader(api_available=False)
        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("ashare_5min_api_unavailable", codes)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "first_sample_5min_data_gate_failed")
        self.assertEqual(report["no_trade_explanation"]["category"], "data_query_failed")

    def test_first_sample_fails_when_5min_rows_missing(self) -> None:
        report = ashare_opening_validator.first_sample_alerts(
            reader=_mock_reader(intraday_rows=[]),
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "first_sample_5min_data_gate_failed")
        self.assertIn("ashare_5min_missing_in_session", {alert["code"] for alert in report["alerts"]})

    def test_first_sample_fails_when_5min_coverage_low(self) -> None:
        report = ashare_opening_validator.first_sample_alerts(
            reader=_mock_reader(intraday_rows=[_intraday_row("600000.SH", "2026-07-06 09:40:00")]),
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:45:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["status"], "fail")
        self.assertIn("ashare_5min_coverage_low", {alert["code"] for alert in report["alerts"]})

    def test_first_sample_fails_when_latest_5min_bar_is_stale(self) -> None:
        report = ashare_opening_validator.first_sample_alerts(
            reader=_mock_reader(
                intraday_rows=[
                    _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                    _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
                ]
            ),
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:50:00+08:00"),
            min_symbols=2,
            wait_minutes=5,
        )

        self.assertEqual(report["status"], "fail")
        self.assertIn("ashare_5min_stale_in_session", {alert["code"] for alert in report["alerts"]})

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
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
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
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )

        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
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

    def test_first_sample_not_due_returns_pass(self) -> None:
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.first_sample_alerts(
            reader=reader,
            local_sim_path=Path("/tmp/nonexistent-ashare-local-sim.jsonl"),
            receipt_path=Path("/tmp/nonexistent-ashare-receipts.jsonl"),
            review_path=Path("/tmp/nonexistent-ashare-review.jsonl"),
            signals_dir=Path("/tmp/nonexistent-signals"),
            no_trade_log_path=Path("/tmp/nonexistent-ashare-no-trade.jsonl"),
            now=datetime.fromisoformat("2026-07-06T09:35:00+08:00"),  # only 5min elapsed, wait_minutes=10
            min_symbols=2,
            wait_minutes=10,
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "first_sample_check_not_due")

    # -- asset universe fail-closed --

    def test_pre_open_fails_when_asset_universe_empty(self) -> None:
        """asset_count=0 with valid daily data → fail closed."""
        reader = _mock_reader(
            assets=[],
            daily_rows=[
                _daily_row("600000.SH", "20260706", 10.0),
                _daily_row("000001.SZ", "20260706", 12.0),
            ],
        )
        report = ashare_opening_validator.validate_pre_open(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertIn("asset_universe", report.get("reason", ""))

    # -- 5-minute staleness & coverage --

    def test_opening_fails_when_5min_bars_stale(self) -> None:
        """Latest bar > 10 min ago → fail."""
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:40:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:40:00"),
            ]
        )
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:55:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "opening_5min_bars_stale")
        self.assertGreater(report["latest_bar_age_minutes"], 10)

    def test_opening_filters_non_ashare_symbols_from_5min(self) -> None:
        """B-shares (200xxx) excluded from valid symbol count."""
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("200001.SZ", "2026-07-06 09:35:00"),
                _intraday_row("200002.SZ", "2026-07-06 09:35:00"),
                _intraday_row("900901.SH", "2026-07-06 09:35:00"),
                _intraday_row("600000.SH", "2026-07-06 09:36:00"),
            ]
        )
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=1,
        )
        self.assertEqual(report["symbol_count"], 1)
        self.assertEqual(report["bar_count"], 1)

    def test_opening_returns_latest_bar_age_minutes(self) -> None:
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:30:00"),
                _intraday_row("000001.SZ", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=2,
        )
        self.assertIsNotNone(report.get("latest_bar_age_minutes"))
        self.assertAlmostEqual(report["latest_bar_age_minutes"], 5.0, delta=0.1)

    def test_opening_accepts_iso_bar_time_with_microseconds(self) -> None:
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06T09:35:00.123456+08:00"),
            ]
        )
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=1,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["latest_bar_time"], "2026-07-06T09:35:00+08:00")

    def test_opening_fails_when_no_ashare_5min_symbols(self) -> None:
        """Only one valid Ashare symbol but min_symbols=2 → fail on coverage."""
        reader = _mock_reader(
            intraday_rows=[
                _intraday_row("600000.SH", "2026-07-06 09:35:00"),
            ]
        )
        report = ashare_opening_validator.validate_opening(
            reader=reader,
            now=datetime.fromisoformat("2026-07-06T09:40:00+08:00"),
            min_symbols=2,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "opening_session_symbol_coverage_low")


if __name__ == "__main__":
    unittest.main()
