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
                "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
            )
        )
        schedules = "\n".join(cron_coverage.tradingagent_entries(template))
        return environment + "\n" + schedules

    def test_slow_market_sim_loops_are_half_hour_staggered(self) -> None:
        expected = {
            "10,40 10-14,22-23,0-4 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_us_sim.sh >> /opt/investment/tradingagent/shared/logs/cron/us_sim.log 2>&1",
            "8,38 * * * * /opt/investment/tradingagent/shared/wrappers/job_crypto_sim.sh >> /opt/investment/tradingagent/shared/logs/cron/crypto_sim.log 2>&1",
            "4,34 * * * * /opt/investment/tradingagent/shared/wrappers/job_pm_research_probability.sh >> /opt/investment/tradingagent/shared/logs/cron/job_pm_research_probability.log 2>&1",
            "7,37 * * * * /opt/investment/tradingagent/shared/wrappers/job_pm_sim.sh >> /opt/investment/tradingagent/shared/logs/cron/pm_sim.log 2>&1",
        }
        forbidden = {
            "*/5 10-14,22-23,0-4 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_us_sim.sh",
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_crypto_sim.sh",
            "2-59/10 * * * * /opt/investment/tradingagent/shared/wrappers/job_pm_research_probability.sh",
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_pm_sim.sh",
        }

        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            for line in expected:
                self.assertIn(line, text)
            for line in forbidden:
                self.assertNotIn(line, text)

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

    def test_ashare_sample_ops_has_required_label_and_review_checkpoints(self) -> None:
        expected = {
            "20 10,11,13,14,15 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_sample_ops.sh >> /opt/investment/tradingagent/shared/logs/cron/job_ashare_sample_ops.log 2>&1",
            "40 17,22 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_sample_ops.sh >> /opt/investment/tradingagent/shared/logs/cron/job_ashare_sample_ops.log 2>&1",
        }

        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            for line in expected:
                self.assertIn(line, text)

    def test_dual_market_reconcile_runs_before_opening_and_at_session_checkpoints(
        self,
    ) -> None:
        expected = {
            "32 9 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh ashare opening >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_ashare.log 2>&1",
            "2 13 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh ashare ops >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_ashare.log 2>&1",
            "58 14 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh ashare ops >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_ashare.log 2>&1",
            "32 15 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh ashare ops >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_ashare.log 2>&1",
            "58 8,20 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh cn_futures preopen >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_cn_futures.log 2>&1",
            "2 9,13,21 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh cn_futures opening >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_cn_futures.log 2>&1",
            "32 11 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh cn_futures ops >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_cn_futures.log 2>&1",
            "2 15,23 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh cn_futures ops >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_cn_futures.log 2>&1",
            "32 2 * * 2-6 /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh cn_futures ops >> /opt/investment/tradingagent/shared/logs/cron/job_market_capital_reconcile_cn_futures.log 2>&1",
        }

        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            for line in expected:
                self.assertIn(line, text)

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

    def test_fails_when_installed_crontab_misses_template_entry(self) -> None:
        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_crypto_sim.sh\n",
                "marketgraph_error": "",
                "root_text": "",
                "root_error": "no root crontab",
            }
        )

        self.assertEqual(report["overall_status"], "fail")
        self.assertIn("installed_crontab_missing_entries", report["failures"])
        self.assertGreater(report["missing_count"], 0)

    def test_fails_when_tradingagent_entries_inherit_marketgraph_bash_env(self) -> None:
        template = (cron_coverage.ROOT / "shared/crontab.txt").read_text()
        schedules = "\n".join(
            line
            for line in template.splitlines()
            if cron_coverage._is_cron_schedule_line(line)
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
            len(cron_coverage.tradingagent_entries(template)),
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
            "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
        ):
            self.assertEqual(template.splitlines().count(assignment), 1)

    def test_rejects_live_or_wrong_timezone_for_every_tradingagent_entry(self) -> None:
        template = (cron_coverage.ROOT / "shared/crontab.txt").read_text()
        valid = self._installed_ta_block(template)
        expected_count = len(cron_coverage.tradingagent_entries(template))

        for old, new, expected_field in (
            (
                "REAL_TRADING_ENABLED=false",
                "REAL_TRADING_ENABLED=true",
                "real_trading_enabled",
            ),
            ("CRON_TZ=Asia/Shanghai", "CRON_TZ=UTC", "cron_tz"),
            ("TZ=Asia/Shanghai", "TZ=UTC", "timezone"),
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

    # -- Review cadence coverage: 07:30 / 11:45 / 15:30 / 22:00 wrappers --

    _REVIEW_CADENCE_ENTRIES = {
        "30 7 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_daily_brief_morning.sh >> /opt/investment/tradingagent/shared/logs/cron/job_daily_brief_morning.log 2>&1",
        "45 11 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_daily_brief_day.sh >> /opt/investment/tradingagent/shared/logs/cron/job_daily_brief_day.log 2>&1",
        "30 15 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_daily_brief_night.sh >> /opt/investment/tradingagent/shared/logs/cron/job_daily_brief_night.log 2>&1",
        "0 22 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_night_calibration.sh >> /opt/investment/tradingagent/shared/logs/cron/job_ashare_night_calibration.log 2>&1",
    }

    _FORBIDDEN_DEPRECATED_ENTRIES = {
        "0 16 * * 1-5 /opt/investment/tradingagent/cron/daily_review.sh",
    }

    def test_review_cadence_wrappers_are_in_both_templates(self) -> None:
        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            for entry in self._REVIEW_CADENCE_ENTRIES:
                with self.subTest(path=str(path), entry=entry):
                    self.assertIn(entry, text)

    def test_deprecated_1600_daily_review_is_forbidden(self) -> None:
        for path in (
            cron_coverage.ROOT / "crontab.txt",
            cron_coverage.ROOT / "shared/crontab.txt",
        ):
            text = path.read_text()
            for entry in self._FORBIDDEN_DEPRECATED_ENTRIES:
                with self.subTest(path=str(path), entry=entry):
                    self.assertNotIn(entry, text)

    def test_night_calibration_wrapper_is_not_legacy(self) -> None:
        wrapper = cron_coverage.ROOT / "shared/wrappers/job_ashare_night_calibration.sh"
        text = wrapper.read_text()
        self.assertNotIn("LEGACY / NOT ACTIVE", text)

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
