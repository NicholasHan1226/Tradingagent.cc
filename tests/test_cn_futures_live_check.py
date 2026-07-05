from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.runtime_test import cn_futures_live_check as live_check


class CNFuturesLiveCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "TradingAgent"
        self.sharedsignals = Path(self.tmp.name) / "SharedSignals"
        (self.root / "shared/runtime_test").mkdir(parents=True)
        (self.sharedsignals / "tools").mkdir(parents=True)
        (self.sharedsignals / "tools/check_cn_futures_5min_freshness.py").write_text("# fake\n", encoding="utf-8")

        patches = [
            patch.object(live_check, "ROOT", self.root),
            patch.object(live_check, "CN_FUTURES_REVIEW", self.root / "shared/review/data/cn_futures_sim_reviews.jsonl"),
            patch.object(live_check, "CN_FUTURES_STYLE_COMPARISON", self.root / "shared/review/cn_futures/style_comparison.json"),
            patch.object(live_check, "CN_FUTURES_STYLE_PERFORMANCE", self.root / "shared/review/cn_futures/style_performance.jsonl"),
            patch.object(live_check, "CN_FUTURES_EVOLUTION_PLAN", self.root / "shared/review/cn_futures/evolution_plan.json"),
            patch.object(live_check, "CN_FUTURES_STYLE_WEIGHTS", self.root / "shared/review/cn_futures/style_weights.json"),
            patch.object(live_check, "CN_FUTURES_SIM_LOG", self.root / "shared/logs/cron/cn_futures_sim.log"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _completed(self, payload: dict[str, object], returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["python"], returncode, stdout=json.dumps(payload), stderr="")

    def _fake_runner(self, payload: dict[str, object], returncode: int = 0):
        def run_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return self._completed(payload, returncode)

        return run_command

    def _write_jsonl(self, rel: str, rows: list[dict[str, object]]) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def _write_ready_outputs(self) -> None:
        self._write_jsonl(
            "shared/review/data/cn_futures_sim_reviews.jsonl",
            [
                {
                    "generated_at": "2026-07-06T01:05:00+00:00",
                    "state": "ok",
                    "filled_count": 2,
                    "error_count": 0,
                    "style_health": {"trend": {"status": "active_sample"}},
                }
            ],
        )
        comparison = self.root / "shared/review/cn_futures/style_comparison.json"
        comparison.parent.mkdir(parents=True, exist_ok=True)
        comparison.write_text(json.dumps([{"style_name": "trend", "filled_count": 2}]), encoding="utf-8")
        self._write_jsonl(
            "shared/review/cn_futures/style_performance.jsonl",
            [{"style_name": "trend", "market": "cn_futures", "trades": 2}],
        )
        plan = self.root / "shared/review/cn_futures/evolution_plan.json"
        plan.write_text(json.dumps({"state": "observed", "actions": [], "real_trading_enabled": False}), encoding="utf-8")
        weights = self.root / "shared/review/cn_futures/style_weights.json"
        weights.write_text(
            json.dumps({"real_trading_enabled": False, "styles": {"trend": {"status": "active", "weight": 1.0}}}),
            encoding="utf-8",
        )
        log = self.root / "shared/logs/cron/cn_futures_sim.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('noise\n{"market":"cn_futures","status":"ok","signals":2}\n', encoding="utf-8")

    def test_warns_without_data_but_structure_is_readable(self) -> None:
        with patch.object(live_check, "check_existing_health_surfaces", return_value=live_check.Check("cn_futures_existing_health_surfaces", "pass", "ok")):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner({"status": "no_data", "error": "no bars"}, returncode=1),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_evolution.sh\njob_cn_futures_observation_report.sh",
            )

        self.assertEqual(report["overall_status"], "warn")
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["observation_phase"], "waiting_for_5min_data")
        freshness = next(check for check in report["checks"] if check["name"] == "sharedsignals_5min_freshness")
        self.assertEqual(freshness["status"], "warn")
        self.assertFalse(report["real_trading_enabled"])

    def test_passes_when_freshness_cron_review_and_style_outputs_are_ready(self) -> None:
        self._write_ready_outputs()

        with patch.object(live_check, "check_existing_health_surfaces", return_value=live_check.Check("cn_futures_existing_health_surfaces", "pass", "ok")):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner({"status": "fresh", "latest_bar_time": "2026-07-06T09:05:00+08:00"}, returncode=0),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_evolution.sh\njob_cn_futures_observation_report.sh",
            )

        self.assertEqual(report["overall_status"], "pass")
        self.assertEqual(report["summary"], {"pass": 7, "warn": 0, "fail": 0})
        self.assertEqual(report["observation_phase"], "ready_to_observe")
        self.assertEqual(report["alerts"], [])

    def test_fails_when_sharedsignals_freshness_script_errors(self) -> None:
        with patch.object(live_check, "check_existing_health_surfaces", return_value=live_check.Check("cn_futures_existing_health_surfaces", "pass", "ok")):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner({"status": "error", "error": "database missing"}, returncode=2),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_evolution.sh",
            )

        self.assertEqual(report["overall_status"], "fail")
        freshness = next(check for check in report["checks"] if check["name"] == "sharedsignals_5min_freshness")
        self.assertEqual(freshness["status"], "fail")
        self.assertEqual(report["observation_phase"], "blocked")

    def test_cron_missing_is_hard_failure_when_crontab_is_readable(self) -> None:
        check = live_check.check_cron_entries("job_cn_futures_sim.sh")

        self.assertEqual(check.status, "fail")
        self.assertIn("sharedsignals_collector", check.details["missing"])
        self.assertIn("tradingagent_evolution", check.details["missing"])

    def test_alerts_when_market_session_has_no_fresh_5min_data(self) -> None:
        with patch.object(live_check, "check_existing_health_surfaces", return_value=live_check.Check("cn_futures_existing_health_surfaces", "pass", "ok")):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner(
                    {
                        "status": "stale",
                        "latest_bar_time": None,
                        "session": {"current": "day", "in_session": True},
                    },
                    returncode=1,
                ),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_evolution.sh",
            )

        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("futures_5min_missing_in_session", codes)


if __name__ == "__main__":
    unittest.main()
