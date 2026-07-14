from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import position_ledger
from shared.notify import email_sender
from shared.review import (
    benchmark,
    daily_review,
    self_heal_loop,
    sim_ledger_reader,
    weekly_review,
)
from shared.wrappers import tradings_cron_entry as cron


class CronHandlersV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        self.shared_dir = self.tmp_path / "shared"
        self.shadow_dir = self.shared_dir / "logs" / "shadow"
        self.cron_dir = self.shared_dir / "logs" / "cron"
        self.review_data_dir = self.shared_dir / "review" / "data"
        self.heal_dir = self.shared_dir / "review" / "heal"
        self.filled_dir = self.tmp_path / "signals" / "filled"
        self.ledger_dir = self.shared_dir / "logs"
        self.sim_ledger_dir = self.ledger_dir / "sim_ledger"
        self.local_sim_trades = self.ledger_dir / "local_sim" / "local_sim_trades.jsonl"

        self._patch(cron, "ROOT", self.tmp_path)
        self._patch(cron, "SHARED", self.shared_dir)
        self._patch(cron, "trade_date", lambda: "20260703")
        self._patch(cron, "now_iso", lambda: "2026-07-03T08:00:00+00:00")
        self._patch(
            daily_review, "SHADOW_TRADES_LOG", self.shadow_dir / "shadow_trades.jsonl"
        )
        self._patch(daily_review, "FILLED_SIGNALS_DIR", self.filled_dir)
        self._patch(
            daily_review, "DAILY_LOG", self.review_data_dir / "daily_reviews.jsonl"
        )
        self._patch(
            daily_review,
            "DIRECTION_HIT_LOG",
            self.review_data_dir / "direction_hit_reviews.jsonl",
        )
        self._patch(sim_ledger_reader, "DEFAULT_SIM_LEDGER_ROOT", self.sim_ledger_dir)
        self._patch(
            sim_ledger_reader, "DEFAULT_LOCAL_SIM_TRADES", self.local_sim_trades
        )
        self._patch(
            benchmark,
            "LAST_PERIOD_STORE",
            self.review_data_dir / "last_period_return.json",
        )
        self._patch(
            benchmark,
            "BENCHMARK_STORE",
            self.review_data_dir / "benchmark_history.json",
        )
        self._patch(position_ledger, "LEDGER_DIR", self.ledger_dir)
        self._patch(
            position_ledger, "POSITION_CSV", self.ledger_dir / "position_ledger.csv"
        )
        self._patch(
            position_ledger,
            "POSITION_LOCK",
            self.ledger_dir / "position_ledger.csv.lock",
        )
        self._patch(
            weekly_review, "WEEKLY_LOG", self.review_data_dir / "weekly_reviews.jsonl"
        )
        self._patch(
            weekly_review, "WEEKLY_STATE", self.review_data_dir / "weekly_state.json"
        )
        self._patch(
            self_heal_loop, "MEMORY_STORE", self.review_data_dir / "heal_memory.json"
        )
        self._patch(self_heal_loop, "HEAL_LOG", self.review_data_dir / "heal_log.jsonl")
        self._patch(
            self_heal_loop, "RULES_STORE", self.review_data_dir / "heal_rules.json"
        )

    def _patch(self, module: object, name: str, value: object) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _write_cron_log(self, job_name: str, lines: list[str]) -> None:
        path = self.cron_dir / f"{job_name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _capture_send_email(self) -> tuple[list[dict[str, object]], object]:
        sent: list[dict[str, object]] = []

        def _send(
            to: str,
            subject: str,
            body: str,
            html_body: str | None = None,
            *,
            channel: str = "trading",
            from_addr: str | None = None,
        ) -> dict[str, object]:
            sent.append(
                {
                    "to": to,
                    "subject": subject,
                    "body": body,
                    "html_body": html_body or "",
                    "channel": channel,
                    "from_addr": from_addr or "",
                }
            )
            return {
                "status": "sent",
                "provider": "unit-test",
                "message_id": f"{channel}-1",
                "to": to,
                "subject": subject,
                "channel": channel,
            }

        return sent, _send

    def _seed_week_trades(self) -> None:
        rows = [
            {
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T10:15:00+00:00",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "pnl": 120.0,
                "strategy_name": "trend",
                "signal_id": "sig-trend-1",
                "market": "Ashare",
                "capital_layer": "shadow",
            },
            {
                "trade_date": "2026-07-01",
                "created_at": "2026-07-01T10:35:00+00:00",
                "ts_code": "BTCUSDT",
                "side": "sell",
                "quantity": 1,
                "price": 64000.0,
                "pnl": 80.0,
                "strategy_name": "trend",
                "signal_id": "sig-trend-2",
                "market": "Crypto",
                "capital_layer": "shadow",
            },
            {
                "trade_date": "2026-07-02",
                "created_at": "2026-07-02T11:00:00+00:00",
                "ts_code": "AAPL",
                "side": "buy",
                "quantity": 10,
                "price": 210.0,
                "pnl": -60.0,
                "strategy_name": "mean_revert",
                "signal_id": "sig-mr-1",
                "market": "US",
                "capital_layer": "shadow",
            },
            {
                "trade_date": "2026-07-03",
                "created_at": "2026-07-03T14:20:00+00:00",
                "ts_code": "PM-ELECTION",
                "side": "buy",
                "quantity": 5,
                "price": 0.61,
                "pnl": -10.0,
                "strategy_name": "mean_revert",
                "signal_id": "sig-mr-2",
                "market": "PM",
                "capital_layer": "shadow",
            },
        ]
        for row in rows:
            self._append_jsonl(self.shadow_dir / "shadow_trades.jsonl", row)

    def test_ashare_no_trade_log_hydrates_top_level_decision_evidence(self) -> None:
        result = {
            "no_trade_explanation": {
                "category": "no_portfolio_orders",
                "counts": {"candidates": 3, "orders": 0},
            },
            "candidate_decision_trace": [
                {"symbol": "AAA", "drop_reason": "capital_plan_capacity_zero"}
            ],
            "capital_plan_decision": {"risk_mode": "defensive", "position_capacity": 0},
            "portfolio_decision": {"allowed_buy_count": 0},
        }

        explanation = cron._hydrate_ashare_no_trade_explanation(result)

        self.assertEqual(explanation["candidate_decision_trace"][0]["symbol"], "AAA")
        self.assertEqual(explanation["capital_plan_decision"]["risk_mode"], "defensive")
        self.assertEqual(explanation["portfolio_decision"]["allowed_buy_count"], 0)

    def test_run_weekly_review_renders_weekly_report_and_sends(self) -> None:
        self._seed_week_trades()
        self._write_json(
            self.review_data_dir / "weekly_state.json",
            {
                "strategies": {
                    "shadow:trend": {
                        "consecutive_positive_weeks": 1,
                        "consecutive_below50_weeks": 0,
                    },
                    "shadow:mean_revert": {
                        "consecutive_positive_weeks": 0,
                        "consecutive_below50_weeks": 1,
                    },
                }
            },
        )
        sent, sender = self._capture_send_email()

        with patch.object(email_sender, "send_email", side_effect=sender):
            result = cron.run_weekly_review(
                "job_weekly_review", "review/weekly/weekly_review.json"
            )

        self.assertEqual(result["job"], "job_weekly_review")
        self.assertEqual(result["state"], "sent")
        self.assertEqual(result["shadow_trade_count"], 4)
        self.assertEqual(result["email_notification"]["status"], "sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["channel"], "trading")
        self.assertIn("tradingagent 周报", str(sent[0]["subject"]))
        self.assertIn("Weekly Report", str(sent[0]["html_body"]))
        self.assertIn("策略统计", str(sent[0]["html_body"]))
        self.assertIn("升级候选=无", result["email_data"]["summary"])
        self.assertIn("人工复核候选=trend", result["email_data"]["summary"])
        self.assertIn("降级候选=mean_revert", result["email_data"]["summary"])
        self.assertTrue(
            (self.shared_dir / "review" / "weekly" / "weekly_review.json").exists()
        )

    def test_run_weekly_review_uses_simulated_ledgers_when_no_shadow_trades(
        self,
    ) -> None:
        self._append_jsonl(
            self.sim_ledger_dir / "pm" / "grid" / "trade_journal.jsonl",
            {
                "timestamp": "2026-07-03T13:15:00+00:00",
                "order_id": "SIM-PM-1",
                "fill_id": "FILL-PM-1",
                "symbol": "PM-ELECTION",
                "side": "buy",
                "fill_qty": 10,
                "fill_price": 0.61,
                "realized_pnl": 12.0,
                "capital_layer": "simulated",
            },
        )
        self._write_json(
            self.review_data_dir / "weekly_state.json",
            {
                "strategies": {
                    "simulated:grid": {
                        "consecutive_positive_weeks": 1,
                        "consecutive_below50_weeks": 0,
                    }
                }
            },
        )
        sent, sender = self._capture_send_email()

        with patch.object(email_sender, "send_email", side_effect=sender):
            result = cron.run_weekly_review(
                "job_pm_weekly", "review/weekly/pm_weekly.json"
            )

        self.assertEqual(result["capital_layer"], "simulated")
        self.assertEqual(result["simulated_trade_count"], 1)
        self.assertEqual(result["shadow_trade_count"], 0)
        self.assertEqual(result["review_trade_count"], 1)
        self.assertEqual(result["email_notification"]["status"], "sent")
        self.assertEqual(len(sent), 1)
        self.assertIn("升级候选=无", result["email_data"]["summary"])
        self.assertIn("人工复核候选=grid", result["email_data"]["summary"])
        self.assertTrue(
            (self.shared_dir / "review" / "weekly" / "pm_weekly.json").exists()
        )

    def test_run_alert_renders_system_health_and_sends(self) -> None:
        self._append_jsonl(
            self.heal_dir / "self_heal_actions.jsonl",
            {
                "job": "job_self_heal",
                "state": "critical",
                "cycle_at": "2026-07-03T07:50:00+00:00",
                "issues_found": 2,
                "issues_fixed": 1,
                "issues_escalated": 1,
                "rule_updates": [{"type": "freeze_violation"}],
            },
        )
        self._write_cron_log(
            "job_daily_brief_day",
            [
                "[2026-07-03T07:35:00+0000] job_daily_brief_day attempt=1 phase=lunch",
                "[2026-07-03T07:35:05+0000] job_daily_brief_day success attempt=1",
            ],
        )
        self._write_cron_log(
            "job_trading_signals",
            [
                "[2026-07-03T07:40:00+0000] job_trading_signals attempt=1 phase=intraday",
                "[2026-07-03T07:40:10+0000] job_trading_signals failed attempt=1 error=timeout",
            ],
        )
        sent, sender = self._capture_send_email()

        with patch.object(email_sender, "send_email", side_effect=sender):
            result = cron.run_alert()

        self.assertEqual(result["job"], "job_alert")
        self.assertEqual(result["state"], "sent")
        self.assertEqual(result["email_data"]["overall_status"], "critical")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["channel"], "system")
        self.assertIn("tradingagent 系统健康", str(sent[0]["subject"]))
        self.assertIn("System Health", str(sent[0]["html_body"]))
        self.assertIn(
            "job_trading_signals", json.dumps(result["cron_health"], ensure_ascii=False)
        )
        self.assertTrue(
            (self.shared_dir / "notify" / "logs" / "alert_log.jsonl").exists()
        )

    def test_weekly_and_system_health_keep_degraded_notification_outcome(self) -> None:
        self._seed_week_trades()
        notification = {
            "status": "degraded",
            "provider": "local_file",
            "fallback_error": "PermissionError: fallback denied",
            "audit_status": "degraded",
        }

        with patch.object(cron, "send_template_email", return_value=notification):
            weekly = cron.run_weekly_review(
                "job_weekly_review", "review/weekly/weekly_review.json"
            )
            system_health = cron.run_alert()

        for result in (weekly, system_health):
            self.assertEqual(result["state"], "degraded")
            self.assertEqual(result["notification_status"], "degraded")
            self.assertEqual(result["email_notification"]["fallback_error"], notification["fallback_error"])
            self.assertEqual(result["email_notification"]["audit_status"], "degraded")

    def test_run_self_heal_executes_real_cycle_and_logs_result(self) -> None:
        pending_dir = self.tmp_path / "signals" / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(
            pending_dir / "EXPIRED-SELF-HEAL.json",
            {
                "order_id": "EXPIRED-SELF-HEAL",
                "status": "pending",
                "valid_until": "2020-01-01",
            },
        )
        position_ledger.open_position(
            "600000.SH",
            2000,
            10.0,
            capital_layer="shadow",
            entry_date="2026-07-03",
            note="self-heal-test",
        )
        self._write_cron_log(
            "job_trading_signals",
            [
                "[2026-07-03T07:40:00+0000] job_trading_signals attempt=1 phase=intraday",
                "[2026-07-03T07:40:10+0000] job_trading_signals failed attempt=1 error=timeout",
            ],
        )

        result = cron.run_self_heal()

        self.assertEqual(result["job"], "job_self_heal")
        self.assertNotEqual(result["state"], "scaffolded")
        self.assertGreaterEqual(int(result.get("issues_found", 0) or 0), 1)
        self.assertGreaterEqual(int(result.get("issues_fixed", 0) or 0), 1)
        self.assertEqual(result["signal_sweep_expired"]["expired_count"], 1)
        self.assertTrue(
            (self.tmp_path / "signals" / "expired" / "EXPIRED-SELF-HEAL.json").exists()
        )
        self.assertTrue(
            (self.shared_dir / "review" / "heal" / "self_heal_actions.jsonl").exists()
        )
        self.assertTrue(
            (self.shared_dir / "logs" / "cron" / "signal_sweep_expired.jsonl").exists()
        )
        self.assertTrue(self_heal_loop.MEMORY_STORE.exists())
        self.assertTrue(self_heal_loop.RULES_STORE.exists())

    def test_signal_sweep_expired_is_registered_cron_handler(self) -> None:
        self.assertIn("job_signal_sweep_expired", cron.JOB_HANDLERS)


if __name__ == "__main__":
    unittest.main()
