from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.runtime_test import cron_coverage


class CronCoverageTest(unittest.TestCase):
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

        for path in (cron_coverage.ROOT / "crontab.txt", cron_coverage.ROOT / "shared/crontab.txt"):
            text = path.read_text()
            for line in expected:
                self.assertIn(line, text)
            for line in forbidden:
                self.assertNotIn(line, text)

    def test_template_entries_are_in_sync(self) -> None:
        report = cron_coverage.check_cron_coverage(
            crontabs={
                "marketgraph_text": "\n".join(cron_coverage.tradingagent_entries((cron_coverage.ROOT / "shared/crontab.txt").read_text())),
                "marketgraph_error": "",
                "root_text": "",
                "root_error": "no root crontab",
            }
        )

        self.assertEqual(report["template_drift_count"], 0)
        self.assertNotIn("template_drift", report["failures"])

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

    def test_fails_when_root_has_tradingagent_residual_with_marketgraph_crontab(self) -> None:
        installed = "\n".join(cron_coverage.tradingagent_entries((cron_coverage.ROOT / "shared/crontab.txt").read_text()))
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

    def test_fails_when_runtime_permission_blockers_exist(self) -> None:
        installed = "\n".join(cron_coverage.tradingagent_entries((cron_coverage.ROOT / "shared/crontab.txt").read_text()))
        with patch.object(cron_coverage, "_runtime_permission_blockers", return_value=["runtime/state/job_unit.lock"]):
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


if __name__ == "__main__":
    unittest.main()
