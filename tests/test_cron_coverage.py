from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.runtime_test import cron_coverage


class CronCoverageTest(unittest.TestCase):
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
