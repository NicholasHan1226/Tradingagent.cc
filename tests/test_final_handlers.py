from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import capital_ledger, position_ledger
from shared.review import benchmark, daily_review, self_heal_loop, weekly_review
from shared.wrappers import tradings_cron_entry as cron


class FinalCronHandlersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.shared = self.root / "shared"
        self.ledger_dir = self.shared / "logs"
        self.shadow_log = self.shared / "logs" / "shadow" / "shadow_trades.jsonl"
        self.review_data = self.shared / "review" / "data"
        self.filled_dir = self.root / "signals" / "filled"

        self._patch(cron, "ROOT", self.root)
        self._patch(cron, "SHARED", self.shared)
        self._patch(cron, "trade_date", lambda: "20260703")
        self._patch(cron, "now_iso", lambda: "2026-07-03T08:00:00+00:00")
        self._patch(daily_review, "SHADOW_TRADES_LOG", self.shadow_log)
        self._patch(daily_review, "FILLED_SIGNALS_DIR", self.filled_dir)
        self._patch(daily_review, "DAILY_LOG", self.review_data / "daily_reviews.jsonl")
        self._patch(benchmark, "LAST_PERIOD_STORE", self.review_data / "last_period_return.json")
        self._patch(benchmark, "BENCHMARK_STORE", self.review_data / "benchmark_history.json")
        self._patch(position_ledger, "LEDGER_DIR", self.ledger_dir)
        self._patch(position_ledger, "POSITION_CSV", self.ledger_dir / "position_ledger.csv")
        self._patch(position_ledger, "POSITION_LOCK", self.ledger_dir / "position_ledger.csv.lock")
        self._patch(capital_ledger, "LEDGER_DIR", self.ledger_dir)
        self._patch(capital_ledger, "CAPITAL_CSV", self.ledger_dir / "capital_ledger.csv")
        self._patch(capital_ledger, "CAPITAL_LOCK", self.ledger_dir / "capital_ledger.csv.lock")
        self._patch(weekly_review, "WEEKLY_LOG", self.review_data / "weekly_reviews.jsonl")
        self._patch(weekly_review, "WEEKLY_STATE", self.review_data / "weekly_state.json")
        self._patch(self_heal_loop, "MEMORY_STORE", self.review_data / "heal_memory.json")
        self._patch(self_heal_loop, "HEAL_LOG", self.review_data / "heal_log.jsonl")
        self._patch(self_heal_loop, "RULES_STORE", self.review_data / "heal_rules.json")

    def _patch(self, module: object, name: str, value: object) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _seed_ledgers_and_trades(self) -> None:
        capital_ledger.record_deposit(100000.0, "2026-07-03T00:00:00", capital_layer="shadow")
        position_ledger.open_position(
            "600000.SH",
            1000,
            10.0,
            capital_layer="shadow",
            entry_date="2026-06-01",
            note="technical trend",
        )
        position_ledger.open_position(
            "PM-ELECTION",
            500,
            0.60,
            capital_layer="shadow",
            entry_date="2026-06-20",
            note="pm event",
        )
        self._append_jsonl(
            self.shadow_log,
            {
                "trade_date": "2026-07-03",
                "created_at": "2026-07-03T10:05:00+00:00",
                "ts_code": "600000.SH",
                "market": "Ashare",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "pnl": 120.0,
                "dimension": "technical",
                "strategy": "trend",
                "condition": "breakout",
                "capital_layer": "shadow",
            },
        )
        self._append_jsonl(
            self.shadow_log,
            {
                "trade_date": "2026-07-03",
                "created_at": "2026-07-03T12:05:00+00:00",
                "ts_code": "PM-ELECTION",
                "market": "PM",
                "side": "buy",
                "quantity": 50,
                "price": 0.62,
                "pnl": -15.0,
                "dimension": "event",
                "strategy": "event_driven",
                "condition": "high_vol",
                "capital_layer": "shadow",
            },
        )

    def test_final_handlers_execute_downstream_and_write_outputs(self) -> None:
        self._seed_ledgers_and_trades()

        with patch.object(
            cron,
            "send_template_email",
            return_value={"status": "saved_local", "provider": "unit-test"},
        ):
            morning = cron.run_daily_brief_morning()
        auto_position = cron.run_auto_position()
        pm_risk = cron.run_pm_risk()
        pm_optimize = cron.run_pm_optimize()
        stress = cron.run_stress_test()
        strategy_version = cron.run_strategy_version()
        attribution = cron.run_attribution("job_strategy_attribution", "review/attribution/strategy_attribution.jsonl")
        self_heal_night = cron.run_self_heal_night()

        results = [morning, auto_position, pm_risk, pm_optimize, stress, strategy_version, attribution, self_heal_night]
        forbidden_states = {"planned" + "_only", "scaff" + "olded"}
        for result in results:
            self.assertNotIn(result["state"], forbidden_states)
            self.assertIn(result["state"], {"ok", "degraded", "saved_local", "email_sent"})

        self.assertGreaterEqual(auto_position["source_position_count"], 2)
        self.assertGreaterEqual(len(auto_position["positions"]), 2)
        self.assertEqual(pm_risk["market"], "PM")
        self.assertEqual(pm_risk["position_count"], 1)
        self.assertEqual(pm_optimize["market"], "PM")
        self.assertGreaterEqual(stress["position_count"], 2)
        self.assertGreaterEqual(len(stress["results"]), 3)
        self.assertGreaterEqual(strategy_version["market_count"], 4)
        self.assertEqual(attribution["shadow_trade_count"], 2)
        self.assertIn("issues_found", self_heal_night)

        self.assertTrue((self.shared / "review" / "daily" / "morning_brief.json").exists())
        self.assertTrue((self.shared / "accounting" / "position_plan.jsonl").exists())
        self.assertTrue((self.shared / "risk" / "pm" / "pm_risk_report.jsonl").exists())
        self.assertTrue((self.shared / "review" / "pm" / "pm_optimize_params.json").exists())
        self.assertFalse((self.shared / "strategies" / "pm" / "pm_optimize_params.json").exists())
        self.assertTrue((self.shared / "risk" / "reports" / "stress_test_report.json").exists())
        self.assertTrue((self.shared / "review" / "strategies" / "strategy_version.jsonl").exists())
        self.assertTrue((self.shared / "review" / "attribution" / "strategy_attribution.jsonl").exists())
        self.assertTrue((self.shared / "review" / "heal" / "heal_report.json").exists())

    def test_no_final_handler_terms_remain_and_legacy_jobs_are_routed(self) -> None:
        source = Path(cron.__file__).read_text(encoding="utf-8")
        terms = ["place" + "holder", "planned" + "_only", "scaff" + "olded", "PLACE" + "HOLDER"]
        self.assertIsNone(re.search("|".join(terms), source))

        for job in (
            "job_premarket_signals",
            "job_us_postclose",
            "job_crypto_weekly",
            "job_pm_forward",
            "job_pm_optimize",
            "job_pm_promote",
            "job_gate_review_night",
            "job_gate_review_day",
            "job_us_signal_review",
            "job_cross_market_review",
            "job_backtest_report",
            "job_research_report",
            "job_pm_report",
        ):
            self.assertIn(job, cron.JOB_HANDLERS)


if __name__ == "__main__":
    unittest.main()
