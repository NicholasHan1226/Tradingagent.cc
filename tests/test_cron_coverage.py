from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.runtime_test import cron_coverage


class CronCoverageTest(unittest.TestCase):
    @staticmethod
    def _installed_ta_block(template: str) -> str:
        environment = "\n".join(
            (
                "SHELL=/bin/bash",
                "CRON_TZ=Asia/Shanghai",
                "TZ=Asia/Shanghai",
                "REAL_TRADING_ENABLED=false",
                "ASHARE_SIM_HERMES_ENABLED=0",
                "ASHARE_SIM_WEBHOOK_ENABLED=0",
                "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
            )
        )
        schedules = "\n".join(cron_coverage.tradingagent_entries(template))
        return environment + "\n" + schedules

    def test_only_journal_research_job_is_scheduled(self) -> None:
        # Market-data jobs stay unscheduled pending the TradingDatas fresh
        # handoff; the single active entry is the journal-only, research-only
        # event-catalyst promotion gate (no collection, no broker, no
        # capital mutation), activated as a reviewed scheduler change.
        expected = [
            "40 16 * * 1-5 /opt/investment/tradingagent/cron/"
            "event_catalyst_promotion.sh"
        ]
        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            self.assertNotIn(
                "TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff",
                text,
            )
            self.assertEqual(cron_coverage.tradingagent_entries(text), expected)

    def test_template_entries_are_in_sync(self) -> None:
        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": "\n".join(
                    cron_coverage.tradingagent_entries(
                        (cron_coverage.ROOT / "shared/crontab.txt").read_text()
                    )
                ),
                "marketgraph_error": "",
                "root_text": "",
                "root_error": "no root crontab",
            }
        )

        self.assertEqual(report["template_drift_count"], 0)
        self.assertNotIn("template_drift", report["failures"])

    def test_retired_ashare_sample_ops_is_absent_from_active_templates(self) -> None:
        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            self.assertNotIn("job_ashare_sample_ops.sh", text)

    def test_cn_futures_reconcile_is_not_scheduled_before_fresh_market_handoff(
        self,
    ) -> None:
        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            self.assertNotIn("job_market_capital_reconcile.sh cn_futures", text)
            self.assertNotIn("job_market_capital_reconcile.sh ashare", text)

    def test_retired_ashare_sample_jobs_are_absent_from_active_templates(self) -> None:
        forbidden = {
            "job_ashare_sample_learning.sh",
            "job_ashare_formal_close_refresh.sh",
            "job_ashare_forward_validation.sh",
            "job_ashare_sample_target_monitor.sh",
        }
        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            for marker in forbidden:
                self.assertNotIn(marker, text)

    def test_root_template_is_only_a_generated_compatibility_mirror(self) -> None:
        mirror = (cron_coverage.ROOT / "crontab.txt").read_text()
        authority = (cron_coverage.ROOT / "shared/crontab.txt").read_text()

        self.assertIn("GENERATED COMPATIBILITY MIRROR", mirror)
        self.assertIn("Canonical template: shared/crontab.txt", mirror)
        self.assertEqual(
            cron_coverage.tradingagent_entries(mirror),
            cron_coverage.tradingagent_entries(authority),
        )

    def test_fails_when_installed_crontab_has_retired_tradingagent_entry(self) -> None:
        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_crypto_sim.sh\n",
                "marketgraph_error": "",
                "root_text": "",
                "root_error": "no root crontab",
            }
        )

        self.assertEqual(report["overall_status"], "fail")
        self.assertIn("installed_crontab_unexpected_entries", report["failures"])
        self.assertEqual(report["unexpected_count"], 1)

    def test_fails_when_tradingagent_entries_inherit_marketgraph_bash_env(self) -> None:
        template = (cron_coverage.ROOT / "shared/crontab.txt").read_text()
        schedules = "\n".join(
            line
            for line in template.splitlines()
            if cron_coverage._is_cron_schedule_line(line)
        )
        schedules = schedules or (
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/"
            "job_crypto_sim.sh"
        )
        installed = (
            "BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh\n"
            + schedules
        )

        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": installed,
                "marketgraph_error": "",
                "root_text": "",
                "root_error": "no root crontab",
            }
        )

        self.assertIn("installed_crontab_environment_mismatch", report["failures"])
        self.assertEqual(
            report["environment_mismatch_count"],
            1,
        )

    def test_accepts_tradingagent_block_after_marketgraph_loader(self) -> None:
        template = (cron_coverage.ROOT / "shared/crontab.txt").read_text()
        schedules = "\n".join(
            line
            for line in template.splitlines()
            if cron_coverage._is_cron_schedule_line(line)
        )
        installed = (
            "BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh\n"
            "SHELL=/bin/bash\n"
            "CRON_TZ=Asia/Shanghai\n"
            "TZ=Asia/Shanghai\n"
            "REAL_TRADING_ENABLED=false\n"
            "ASHARE_SIM_HERMES_ENABLED=0\n"
            "ASHARE_SIM_WEBHOOK_ENABLED=0\n"
            "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh\n" + schedules
        )

        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": installed,
                "marketgraph_error": "",
                "root_text": "",
                "root_error": "no root crontab",
            }
        )

        self.assertEqual(report["environment_mismatch_count"], 0)

    def test_shared_template_declares_sim_only_and_china_timezone(self) -> None:
        template = (cron_coverage.ROOT / "shared/crontab.txt").read_text()

        for assignment in (
            "SHELL=/bin/bash",
            "CRON_TZ=Asia/Shanghai",
            "TZ=Asia/Shanghai",
            "REAL_TRADING_ENABLED=false",
            "ASHARE_SIM_HERMES_ENABLED=0",
            "ASHARE_SIM_WEBHOOK_ENABLED=0",
            "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
        ):
            self.assertEqual(template.splitlines().count(assignment), 1)

    def test_rejects_live_or_wrong_timezone_for_every_tradingagent_entry(self) -> None:
        template = (cron_coverage.ROOT / "shared/crontab.txt").read_text()
        # The template now carries exactly one active journal-research
        # entry, so the installed block alone exercises the per-entry
        # environment checks.
        valid = self._installed_ta_block(template)
        expected_count = 1

        for old, new, expected_field in (
            (
                "REAL_TRADING_ENABLED=false",
                "REAL_TRADING_ENABLED=true",
                "real_trading_enabled",
            ),
            ("CRON_TZ=Asia/Shanghai", "CRON_TZ=UTC", "cron_tz"),
            ("TZ=Asia/Shanghai", "TZ=UTC", "timezone"),
            (
                "ASHARE_SIM_HERMES_ENABLED=0",
                "ASHARE_SIM_HERMES_ENABLED=1",
                "ashare_sim_hermes_enabled",
            ),
            (
                "ASHARE_SIM_WEBHOOK_ENABLED=0",
                "ASHARE_SIM_WEBHOOK_ENABLED=1",
                "ashare_sim_webhook_enabled",
            ),
        ):
            with self.subTest(expected_field=expected_field):
                report = cron_coverage.check_cron_coverage(
                    crontabs={
                        "marketgraph_text": valid.replace(old, new),
                        "marketgraph_error": "",
                        "root_text": "",
                        "root_error": "no root crontab",
                    }
                )
                self.assertIn(
                    "installed_crontab_environment_mismatch", report["failures"]
                )
                self.assertEqual(report["environment_mismatch_count"], expected_count)
                self.assertTrue(
                    all(
                        expected_field in item.get("mismatched_fields", [])
                        for item in report["environment_mismatches"]
                    )
                )

    def test_fails_when_root_has_tradingagent_residual_with_marketgraph_crontab(
        self,
    ) -> None:
        installed = "\n".join(
            cron_coverage.tradingagent_entries(
                (cron_coverage.ROOT / "shared/crontab.txt").read_text()
            )
        )
        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": installed,
                "marketgraph_error": "",
                "root_text": "20 13,15 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_forward_validation.sh\n",
                "root_error": "",
            }
        )

        self.assertEqual(report["overall_status"], "fail")
        self.assertIn("root_tradingagent_residual", report["failures"])
        self.assertEqual(report["root_residual_count"], 1)

    def test_non_root_runtime_does_not_reuse_current_user_crontab_as_root(self) -> None:
        with (
            patch.object(cron_coverage.os, "geteuid", return_value=1000),
            patch.object(
                cron_coverage, "_run_crontab", return_value=("MARKETGRAPH_CRON", "")
            ) as run_crontab,
        ):
            details = cron_coverage._read_installed_crontabs()

        self.assertEqual(details["marketgraph_text"], "MARKETGRAPH_CRON")
        self.assertEqual(details["root_text"], "")
        self.assertIn("root crontab unchecked", details["root_error"])
        run_crontab.assert_called_once_with(["crontab", "-u", "marketgraph", "-l"])

    def test_fails_when_runtime_permission_blockers_exist(self) -> None:
        installed = "\n".join(
            cron_coverage.tradingagent_entries(
                (cron_coverage.ROOT / "shared/crontab.txt").read_text()
            )
        )
        with patch.object(
            cron_coverage,
            "_runtime_permission_blockers",
            return_value=["runtime/state/job_unit.lock"],
        ):
            report = cron_coverage.check_cron_coverage(
                crontabs={
                    "marketgraph_text": installed,
                    "marketgraph_error": "",
                    "root_text": "",
                    "root_error": "no root crontab",
                }
            )

        self.assertEqual(report["overall_status"], "fail")
        self.assertIn("runtime_permission_blocked", report["failures"])
        self.assertEqual(report["runtime_permission_blocker_count"], 1)

    def test_runtime_permission_candidates_include_only_current_ashare_sample_outputs(
        self,
    ) -> None:
        candidates = {
            str(path.relative_to(cron_coverage.ROOT))
            for path in cron_coverage._runtime_permission_candidate_paths()
        }

        for current in (
            "shared/review/ashare/sample_journal.jsonl",
            "shared/review/ashare/sample_kpi_latest.json",
            "shared/review/ashare/sample_kpi_log.jsonl",
            "shared/review/ashare/evolution_decision_latest.json",
            "shared/review/ashare/evolution_decision_log.jsonl",
            "shared/review/ashare/market_maturity_latest.json",
            "shared/review/ashare/market_maturity_log.jsonl",
        ):
            self.assertIn(current, candidates)
        for legacy in (
            "shared/review/ashare/forward_validation_latest.json",
            "shared/review/ashare/portfolio_evolution_latest.json",
            "shared/review/ashare/sample_target_monitor_latest.json",
            "shared/review/ashare/sample_target_monitor_log.jsonl",
            "shared/review/ashare/sample_learning_latest.json",
            "shared/review/ashare/sample_learning_log.jsonl",
            "shared/review/ashare/tier_experiments_latest.json",
            "shared/logs/local_sim_tiers",
        ):
            self.assertNotIn(legacy, candidates)
        self.assertIn("shared/logs/trade_audit_trail.jsonl", candidates)

    def test_runtime_permission_candidates_include_capital_and_replay_outputs(
        self,
    ) -> None:
        candidates = {
            str(path.relative_to(cron_coverage.ROOT))
            for path in cron_coverage._runtime_permission_candidate_paths()
        }

        for current in (
            "shared/logs/capital/ashare",
            "shared/logs/capital/ashare/ashare_sim_capital_events.jsonl",
            "shared/logs/capital/ashare/ashare_sim_capital_latest.json",
            "shared/logs/capital/ashare/.ashare_sim_capital.lock",
            "shared/logs/capital/cn_futures",
            "shared/logs/capital/cn_futures/cn_futures_sim_capital_events.jsonl",
            "shared/logs/capital/cn_futures/cn_futures_sim_capital_latest.json",
            "shared/logs/capital/cn_futures/.cn_futures_sim_capital.lock",
            "shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1",
            "shared/review/cn_futures",
            "shared/review/cn_futures/replay_latest.json",
            "shared/review/cn_futures/replay_history.jsonl",
        ):
            self.assertIn(current, candidates)

    # -- Retired A-share review/email jobs remain available only as blocked
    # compatibility entrypoints; recurring cron must not invoke them. --

    _FORBIDDEN_RETIRED_ENTRYPOINTS = {
        "cron/daily_review.sh",
        "cron/health_check.sh",
        "shared/wrappers/job_ashare_night_calibration.sh",
        "shared/wrappers/job_daily_brief_morning.sh",
        "shared/wrappers/job_daily_brief_day.sh",
        "shared/wrappers/job_daily_brief_night.sh",
        "shared/wrappers/job_opening_acceptance.sh",
    }

    def test_retired_review_and_generic_jobs_are_absent_from_both_templates(
        self,
    ) -> None:
        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            for entrypoint in self._FORBIDDEN_RETIRED_ENTRYPOINTS:
                with self.subTest(path=str(path), entrypoint=entrypoint):
                    self.assertNotIn(entrypoint, text)

    def test_template_sync_includes_review_cadence(self) -> None:
        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": "\n".join(
                    cron_coverage.tradingagent_entries(
                        (cron_coverage.ROOT / "shared/crontab.txt").read_text()
                    )
                ),
                "marketgraph_error": "",
                "root_text": "",
                "root_error": "no root crontab",
            }
        )
        self.assertEqual(report["template_drift_count"], 0)
        self.assertNotIn("template_drift", report["failures"])


if __name__ == "__main__":
    unittest.main()
