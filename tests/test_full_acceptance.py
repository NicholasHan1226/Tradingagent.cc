from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from shared.runtime_test import full_acceptance


class FullAcceptanceTest(unittest.TestCase):
    def test_quick_profile_runs_key_read_only_checks(self) -> None:
        args = full_acceptance.parse_args(["--profile", "quick"])
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):  # noqa: ANN001
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with patch.object(full_acceptance.subprocess, "run", side_effect=fake_run):
            report = full_acceptance.run_acceptance(args)

        self.assertEqual(report["overall_status"], "pass")
        self.assertEqual([check["name"] for check in report["checks"]], ["key_pytest"])
        self.assertTrue(any("pytest" in part for command in calls for part in command))

    def test_prod_profile_runs_runtime_checks(self) -> None:
        args = full_acceptance.parse_args(["--profile", "prod"])
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):  # noqa: ANN001
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with patch.object(full_acceptance.subprocess, "run", side_effect=fake_run):
            report = full_acceptance.run_acceptance(args)

        self.assertEqual([check["name"] for check in report["checks"]], [
            "sim_market_health",
            "ashare_no_trade_summary",
            "opening_acceptance",
        ])
        self.assertTrue(any("shared.runtime_test.market_health" in part for command in calls for part in command))

    def test_failed_command_fails_report(self) -> None:
        args = full_acceptance.parse_args(["--profile", "cn_futures"])

        with patch.object(
            full_acceptance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["unit"], 2, stdout="", stderr="boom"),
        ):
            report = full_acceptance.run_acceptance(args)

        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(report["checks"][0]["returncode"], 2)
        self.assertIn("boom", report["checks"][0]["tail"])


if __name__ == "__main__":
    unittest.main()
