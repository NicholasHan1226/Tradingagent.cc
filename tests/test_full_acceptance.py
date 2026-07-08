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

    def test_json_warn_status_is_preserved(self) -> None:
        args = full_acceptance.parse_args(["--profile", "prod"])

        with patch.object(
            full_acceptance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["unit"], 0, stdout='{"overall_status":"warn"}', stderr=""),
        ):
            report = full_acceptance.run_acceptance(args)

        self.assertEqual(report["overall_status"], "warn")
        self.assertTrue(all(check["status"] == "warn" for check in report["checks"]))

    def test_no_trade_incomplete_evidence_is_warn(self) -> None:
        status, summary = full_acceptance._status_from_json_output(
            "ashare_no_trade_summary",
            '{"evidence_status":"incomplete","evidence_gaps":["candidate_decision_trace_missing"]}',
            0,
        )

        self.assertEqual(status, "warn")
        self.assertIn("candidate_decision_trace_missing", summary)


if __name__ == "__main__":
    unittest.main()
