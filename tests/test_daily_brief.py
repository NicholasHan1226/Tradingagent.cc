from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import position_ledger
from shared.review import benchmark, daily_review
from shared.wrappers import tradings_cron_entry as cron


class DailyBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        self.shared_dir = self.tmp_path / "shared"
        self.shadow_dir = self.shared_dir / "logs" / "shadow"
        self.filled_dir = self.tmp_path / "signals" / "filled"
        self.review_dir = self.shared_dir / "review" / "data"
        self.ledger_dir = self.shared_dir / "logs"

        self._patch(cron, "SHARED", self.shared_dir)
        self._patch(cron, "trade_date", lambda: "20260630")
        self._patch(cron, "now_iso", lambda: "2026-06-30T00:00:00+00:00")
        self._patch(daily_review, "SHADOW_TRADES_LOG", self.shadow_dir / "shadow_trades.jsonl")
        self._patch(daily_review, "FILLED_SIGNALS_DIR", self.filled_dir)
        self._patch(daily_review, "DAILY_LOG", self.review_dir / "daily_reviews.jsonl")
        self._patch(benchmark, "LAST_PERIOD_STORE", self.review_dir / "last_period_return.json")
        self._patch(benchmark, "BENCHMARK_STORE", self.review_dir / "benchmark_history.json")
        self._patch(position_ledger, "LEDGER_DIR", self.ledger_dir)
        self._patch(position_ledger, "POSITION_CSV", self.ledger_dir / "position_ledger.csv")
        self._patch(position_ledger, "POSITION_LOCK", self.ledger_dir / "position_ledger.csv.lock")

        self._seed_positions()
        self._seed_trades()

    def _patch(self, module: object, name: str, value: object) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write_shadow_trade(self, payload: dict[str, object]) -> None:
        self._append_jsonl(self.shadow_dir / "shadow_trades.jsonl", payload)

    def _seed_positions(self) -> None:
        position_ledger.open_position(
            "600000.SH",
            100,
            10.0,
            capital_layer="shadow",
            entry_date="2026-06-30",
            note="ashare-test",
        )
        position_ledger.open_position(
            "BTCUSDT",
            1,
            65000.0,
            capital_layer="shadow",
            entry_date="2026-06-30",
            note="crypto-test",
        )

    def _seed_trades(self) -> None:
        rows = [
            {
                "trade_id": "T1",
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T10:05:00",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "pnl": 120.0,
                "signal_id": "sig-ashare",
                "strategy_name": "trend",
                "market": "Ashare",
                "capital_layer": "shadow",
            },
            {
                "trade_id": "T2",
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T11:10:00",
                "ts_code": "BTCUSDT",
                "side": "sell",
                "quantity": 1,
                "price": 65000.0,
                "pnl": -30.0,
                "signal_id": "sig-crypto",
                "strategy_name": "breakout",
                "market": "Crypto",
                "capital_layer": "shadow",
            },
            {
                "trade_id": "T3",
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T13:20:00",
                "ts_code": "AAPL",
                "side": "buy",
                "quantity": 10,
                "price": 210.0,
                "pnl": 80.0,
                "signal_id": "sig-us",
                "strategy_name": "mean_revert",
                "market": "US",
                "capital_layer": "shadow",
            },
            {
                "trade_id": "T4",
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T14:50:00",
                "ts_code": "PM-ELECTION",
                "side": "buy",
                "quantity": 5,
                "price": 0.62,
                "pnl": 15.0,
                "signal_id": "sig-pm",
                "strategy_name": "event",
                "market": "PM",
                "capital_layer": "shadow",
            },
        ]
        for row in rows:
            self._write_shadow_trade(row)

    def _capture_email(self) -> tuple[list[dict[str, object]], object]:
        sent: list[dict[str, object]] = []

        def _send(template_name: str, data: dict[str, object]) -> dict[str, object]:
            sent.append({"template_name": template_name, "data": data})
            return {"status": "sent", "provider": "unit-test", "message_id": f"{template_name}-1"}

        return sent, _send

    def _seed_review_outputs(self) -> None:
        lunch = daily_review.run_daily_review("20260630", session="lunch")
        close = daily_review.run_daily_review("20260630", session="close")
        self._append_jsonl(self.shared_dir / "review" / "daily" / "midday_review.jsonl", lunch)
        self._append_jsonl(self.shared_dir / "review" / "daily" / "daily_brief.jsonl", close)

    def test_run_daily_brief_morning_renders_and_sends_pre_market_plan(self) -> None:
        self._seed_review_outputs()
        sent, sender = self._capture_email()

        with patch.object(cron, "send_template_email", side_effect=sender):
            result = cron.run_daily_brief_morning()

        self.assertEqual(result["job"], "job_daily_brief_morning")
        self.assertEqual(result["state"], "sent")
        self.assertEqual(sent[0]["template_name"], "pre_market_plan")
        data = sent[0]["data"]
        self.assertEqual(len(data["sector_focus"]), 4)
        self.assertEqual(data["strategy"][0]["name"], "系统健康")
        self.assertEqual(result["system_health"]["status"], "healthy")
        self.assertEqual(result["signal_count"], 4)
        self.assertTrue((self.shared_dir / "review" / "daily" / "morning_brief.json").exists())

    def test_run_daily_brief_day_renders_and_sends_midday_review(self) -> None:
        sent, sender = self._capture_email()

        with patch.object(cron, "send_template_email", side_effect=sender):
            result = cron.run_daily_brief_day()

        self.assertEqual(result["job"], "job_daily_brief_day")
        self.assertEqual(result["state"], "sent")
        self.assertEqual(sent[0]["template_name"], "midday_review")
        data = sent[0]["data"]
        self.assertEqual(len(data["morning_trades"]), 2)
        self.assertGreaterEqual(len(data["afternoon_plan"]), 1)
        self.assertIn("上午胜率", data["summary"])
        review_rows = (self.shared_dir / "review" / "daily" / "midday_review.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertTrue(review_rows)

    def test_run_daily_brief_night_renders_and_sends_daily_report(self) -> None:
        sent, sender = self._capture_email()

        with patch.object(cron, "send_template_email", side_effect=sender):
            result = cron.run_daily_brief_night()

        self.assertEqual(result["job"], "job_daily_brief_night")
        self.assertEqual(result["state"], "sent")
        self.assertEqual(sent[0]["template_name"], "daily_report")
        data = sent[0]["data"]
        self.assertEqual(len(data["trades"]), 4)
        self.assertGreaterEqual(len(data["tomorrow_plan"]), 3)
        self.assertEqual(data["tomorrow_plan"][0]["action"], "对比目标")
        self.assertIn("vs benchmark", data["summary"])
        review_rows = (self.shared_dir / "review" / "daily" / "daily_brief.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertTrue(review_rows)

    def test_daily_briefs_keep_degraded_notification_outcome(self) -> None:
        notification = {
            "status": "degraded",
            "provider": "local_file",
            "fallback_error": "PermissionError: fallback denied",
            "audit_status": "degraded",
        }

        with (
            patch.object(cron, "send_template_email", return_value=notification),
            patch.object(daily_review, "run_daily_review", side_effect=lambda *_args, **_kwargs: {}),
        ):
            results = [
                cron.run_daily_brief_morning(),
                cron.run_daily_brief_day(),
                cron.run_daily_brief_night(),
            ]

        for result in results:
            self.assertEqual(result["state"], "degraded")
            self.assertEqual(result["notification_status"], "degraded")
            self.assertEqual(result["email_notification"]["fallback_error"], notification["fallback_error"])
            self.assertEqual(result["email_notification"]["audit_status"], "degraded")


if __name__ == "__main__":
    unittest.main()
