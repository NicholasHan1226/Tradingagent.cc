from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
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

        self.assertEqual(
            [check["name"] for check in report["checks"]],
            [
                "cron_coverage",
                "tradingdatas_v1_runtime_gate",
                "ashare_no_trade_summary",
                "self_evolution_health",
            ],
        )
        self.assertTrue(
            any(
                "shared.runtime_test.cron_coverage" in part
                for command in calls
                for part in command
            )
        )
        self.assertTrue(
            any(
                "shared.runtime_test.sharedsignals_v1_gate" in part
                for command in calls
                for part in command
            )
        )
        flattened = " ".join(part for command in calls for part in command)
        self.assertNotIn("shared.runtime_test.sharedsignals_evidence_contract", flattened)
        self.assertNotIn("shared.runtime_test.market_health", flattened)
        self.assertNotIn("shared.runtime_test.opening_acceptance", flattened)

    def test_failed_command_fails_report(self) -> None:
        args = full_acceptance.parse_args(["--profile", "cn_futures"])

        with patch.object(
            full_acceptance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["unit"], 2, stdout="", stderr="boom"
            ),
        ):
            report = full_acceptance.run_acceptance(args)

        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(report["checks"][0]["returncode"], 2)
        self.assertIn("boom", report["checks"][0]["tail"])
        self.assertEqual(report["checks"][0]["name"], "cn_futures_contract_tests")

    def test_json_warn_status_is_preserved(self) -> None:
        args = full_acceptance.parse_args(["--profile", "prod"])

        with patch.object(
            full_acceptance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["unit"], 0, stdout='{"overall_status":"warn"}', stderr=""
            ),
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

    # ------------------------------------------------------------------
    # New: capital_growth uses dual market_capital_ops checks
    # ------------------------------------------------------------------

    def test_capital_growth_profile_uses_dual_market_capital_checks(self) -> None:
        """capital_growth profile replaces master_capital_acceptance with
        ashare_capital + cn_futures_capital via market_capital_ops.py."""
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "tests") as tmp:
            root = Path(tmp)
            journal = root / "ashare_samples.jsonl"
            records = root / "cn_futures_records.jsonl"
            ashare_capital_root = root / "ashare_market_capital"
            cn_futures_capital_root = root / "cn_futures_market_capital"
            journal.write_text('{"immutable":"source"}\n', encoding="utf-8")
            records.write_text(
                json.dumps(
                    {
                        "trade_date": "20260713",
                        "session": "day_morning",
                        "record_type": "prediction",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = full_acceptance.parse_args(
                [
                    "--profile",
                    "capital_growth",
                    "--trade-date",
                    "20260713",
                    "--ashare-capital-root",
                    str(ashare_capital_root),
                    "--cn-futures-capital-root",
                    str(cn_futures_capital_root),
                    "--ashare-journal",
                    str(journal),
                    "--as-of",
                    "2026-07-13T16:00:00+08:00",
                    "--cn-futures-records",
                    str(records),
                    "--cn-futures-sessions",
                    "day_morning,day_afternoon",
                ]
            )
            calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_run(command, **kwargs):  # noqa: ANN001
                calls.append((command, kwargs))
                joined = " ".join(command)
                if "ashare_forward_label_ops" in joined:
                    staged = Path(command[command.index("--journal-path") + 1])
                    self.assertNotEqual(staged, journal)
                    self.assertEqual(
                        staged.read_text(encoding="utf-8"),
                        journal.read_text(encoding="utf-8"),
                    )
                    staged.write_text("staged-only-write\n", encoding="utf-8")
                    payload = {
                        "status": "pass",
                        "counts": {
                            "prediction_count": 2,
                            "ready_labels": 1,
                            "pending_not_due": 11,
                        },
                    }
                elif "cn_futures_session_acceptance" in joined:
                    payload = {"status": "pass", "ready": True}
                elif "market_capital_ops.py" in joined:
                    payload = {
                        "status": "market_capital_available",
                        "market": "ashare",
                        "fresh": True,
                        "reconciled": True,
                        "real_trading_enabled": False,
                    }
                else:
                    payload = {"status": "pass", "real_trading_enabled": False}
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )

            with patch.object(full_acceptance.subprocess, "run", side_effect=fake_run):
                report = full_acceptance.run_acceptance(args)

            self.assertEqual(report["overall_status"], "pass")
            check_names = [check["name"] for check in report["checks"]]
            self.assertIn("ashare_capital", check_names)
            self.assertIn("cn_futures_capital", check_names)
            self.assertIn("ashare_preopen", check_names)
            self.assertIn("ashare_opening", check_names)
            # Master capital acceptance must NOT be in the list
            self.assertNotIn("master_capital_acceptance", check_names)
            joined_calls = [" ".join(command) for command, _ in calls]
            # Uses market_capital_ops.py NOT master_capital_ops.py for capital checks
            self.assertFalse(
                any(
                    "master_capital_ops.py" in call and "acceptance" in call
                    for call in joined_calls
                )
            )
            self.assertTrue(
                any(
                    "market_capital_ops.py" in call and "--market ashare" in call
                    for call in joined_calls
                )
            )
            self.assertTrue(
                any(
                    "market_capital_ops.py" in call and "--market cn_futures" in call
                    for call in joined_calls
                )
            )
            self.assertTrue(
                any(
                    "--market ashare" in call
                    and f"--root {ashare_capital_root}" in call
                    for call in joined_calls
                )
            )
            self.assertTrue(
                any(
                    "--market cn_futures" in call
                    and f"--root {cn_futures_capital_root}" in call
                    for call in joined_calls
                )
            )
            self.assertTrue(
                any(
                    "ashare_preopen_dry_run" in call and "--no-write" in call
                    for call in joined_calls
                )
            )
            self.assertTrue(
                any("ashare_opening_validator" in call for call in joined_calls)
            )
            self.assertTrue(
                any("--trade-date 20260713" in call for call in joined_calls)
            )
            self.assertTrue(
                any(
                    "--sessions day_morning,day_afternoon" in call
                    for call in joined_calls
                )
            )
            self.assertTrue(
                all(
                    kwargs["env"]["REAL_TRADING_ENABLED"] == "false"
                    for _, kwargs in calls
                )
            )
            self.assertTrue(
                all(
                    kwargs["env"]["TRADINGAGENT_ASHARE_CAPITAL_ROOT"]
                    == str(ashare_capital_root)
                    for _, kwargs in calls
                )
            )
            self.assertTrue(
                all(
                    kwargs["env"]["TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT"]
                    == str(cn_futures_capital_root)
                    for _, kwargs in calls
                )
            )
            self.assertTrue(
                all(
                    "TRADINGAGENT_MASTER_CAPITAL_ROOT" not in kwargs["env"]
                    for _, kwargs in calls
                )
            )
            self.assertEqual(
                journal.read_text(encoding="utf-8"), '{"immutable":"source"}\n'
            )

    def test_ashare_and_cn_futures_checks_are_independent(self) -> None:
        """The two market checks never sum. One failing does not block the other."""
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "tests") as tmp:
            root = Path(tmp)
            journal = root / "ashare_samples.jsonl"
            records = root / "cn_futures_records.jsonl"
            journal.write_text('{"immutable":"source"}\n', encoding="utf-8")
            records.write_text(
                json.dumps(
                    {
                        "trade_date": "20260713",
                        "session": "day_morning",
                        "record_type": "prediction",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = full_acceptance.parse_args(
                [
                    "--profile",
                    "capital_growth",
                    "--trade-date",
                    "20260713",
                    "--ashare-journal",
                    str(journal),
                    "--as-of",
                    "2026-07-13T16:00:00+08:00",
                    "--cn-futures-records",
                    str(records),
                    "--cn-futures-sessions",
                    "day_morning",
                ]
            )
            call_statuses: dict[str, int] = {}

            def fake_run(command, **kwargs):  # noqa: ANN001
                joined = " ".join(command)
                if "ashare_forward_label_ops" in joined:
                    payload = json.dumps(
                        {
                            "status": "pass",
                            "counts": {
                                "prediction_count": 2,
                                "ready_labels": 1,
                                "pending_not_due": 11,
                            },
                        }
                    )
                elif "cn_futures_session_acceptance" in joined:
                    payload = json.dumps({"status": "pass", "ready": True})
                elif "market_capital_ops.py" in joined:
                    if "--market ashare" in joined:
                        # ashare passes with fresh + reconciled
                        payload = json.dumps(
                            {
                                "status": "market_capital_available",
                                "market": "ashare",
                                "fresh": True,
                                "reconciled": True,
                                "real_trading_enabled": False,
                            }
                        )
                        call_statuses["ashare_capital"] = 0
                    elif "--market cn_futures" in joined:
                        # cn_futures fails independently
                        payload = json.dumps(
                            {
                                "status": "market_capital_unavailable",
                                "market": "cn_futures",
                                "real_trading_enabled": False,
                            }
                        )
                        call_statuses["cn_futures_capital"] = 2
                    else:
                        payload = "{}"
                else:
                    payload = json.dumps({"status": "pass"})
                rc = (
                    call_statuses.get("cn_futures_capital", 0)
                    if "cn_futures" in joined
                    else call_statuses.get("ashare_capital", 0)
                )
                return subprocess.CompletedProcess(
                    command, rc, stdout=payload, stderr=""
                )

            with patch.object(full_acceptance.subprocess, "run", side_effect=fake_run):
                report = full_acceptance.run_acceptance(args)

            # cn_futures_capital failure makes overall fail
            self.assertEqual(report["overall_status"], "fail")
            # ashare_capital is independent and passes
            ashare_check = next(
                c for c in report["checks"] if c["name"] == "ashare_capital"
            )
            self.assertEqual(ashare_check["status"], "pass")
            cn_check = next(
                c for c in report["checks"] if c["name"] == "cn_futures_capital"
            )
            self.assertEqual(cn_check["status"], "fail")

    def test_removed_master_capital_entry_is_never_called(self) -> None:
        """Capital-growth acceptance only invokes the two market authorities."""
        args = full_acceptance.parse_args(
            [
                "--profile",
                "capital_growth",
                "--trade-date",
                "20260713",
            ]
        )

        with patch.object(
            full_acceptance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["unit"],
                0,
                stdout='{"status":"pass","real_trading_enabled":false}',
                stderr="",
            ),
        ):
            report = full_acceptance.run_acceptance(args)

        # capital_growth_inputs check fails due to missing evidence files
        self.assertEqual(report["overall_status"], "fail")
        # No master_capital_ops subprocess was launched
        joined_names = [check["name"] for check in report["checks"]]
        self.assertNotIn("master_capital_acceptance", joined_names)
        # The missing runtime evidence remains independently fail-closed.
        self.assertIn("capital_growth_inputs", joined_names)

    def test_capital_growth_missing_runtime_evidence_fails_without_silent_skip(
        self,
    ) -> None:
        args = full_acceptance.parse_args(
            ["--profile", "capital_growth", "--trade-date", "20260713"]
        )
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):  # noqa: ANN001
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"status":"pass","real_trading_enabled":false}',
                stderr="",
            )

        with patch.object(full_acceptance.subprocess, "run", side_effect=fake_run):
            report = full_acceptance.run_acceptance(args)

        self.assertEqual(report["overall_status"], "fail")
        input_check = next(
            check
            for check in report["checks"]
            if check["name"] == "capital_growth_inputs"
        )
        self.assertEqual(input_check["status"], "fail")
        self.assertIn("ashare_journal", input_check["summary"])
        self.assertIn("as_of", input_check["summary"])
        self.assertIn("cn_futures_records", input_check["summary"])
        self.assertIn("cn_futures_sessions", input_check["summary"])
        joined_calls = [" ".join(command) for command in calls]
        self.assertFalse(
            any("ashare_forward_label_ops" in call for call in joined_calls)
        )
        self.assertFalse(
            any("cn_futures_session_acceptance" in call for call in joined_calls)
        )

    def test_capital_growth_missing_evidence_file_is_explicit_failure(self) -> None:
        args = full_acceptance.parse_args(
            [
                "--profile",
                "capital_growth",
                "--trade-date",
                "20260713",
                "--ashare-journal",
                "/definitely/missing/ashare.jsonl",
                "--as-of",
                "2026-07-13T16:00:00+08:00",
                "--cn-futures-records",
                "/definitely/missing/cn.jsonl",
                "--cn-futures-sessions",
                "day_morning",
            ]
        )

        with patch.object(
            full_acceptance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["unit"], 0, stdout='{"status":"pass"}', stderr=""
            ),
        ):
            report = full_acceptance.run_acceptance(args)

        input_check = next(
            check
            for check in report["checks"]
            if check["name"] == "capital_growth_inputs"
        )
        self.assertIn("ashare_journal_not_found", input_check["summary"])
        self.assertIn("cn_futures_records_not_found", input_check["summary"])
        self.assertEqual(report["overall_status"], "fail")

    def test_real_trading_environment_blocks_entire_runner_before_subprocesses(
        self,
    ) -> None:
        args = full_acceptance.parse_args(["--profile", "capital_growth"])
        with (
            patch.dict(os.environ, {"REAL_TRADING_ENABLED": "true"}, clear=False),
            patch.object(full_acceptance.subprocess, "run") as run,
        ):
            report = full_acceptance.run_acceptance(args)

        run.assert_not_called()
        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(report["checks"][0]["name"], "sim_only_safety_gate")
        self.assertIn("REAL_TRADING_ENABLED", report["checks"][0]["summary"])

    def test_forward_label_pass_without_predictions_is_not_silently_green(self) -> None:
        status, summary = full_acceptance._status_from_json_output(
            "ashare_forward_label_ops",
            '{"status":"pass","counts":{"prediction_count":0,"ready_labels":0}}',
            0,
        )

        self.assertEqual(status, "fail")
        self.assertIn("prediction", summary)

    def test_market_capital_check_with_invalid_json_is_not_silently_green(self) -> None:
        """New ashare_capital / cn_futures_capital checks reject invalid JSON."""
        for name in ("ashare_capital", "cn_futures_capital"):
            with self.subTest(name=name):
                status, summary = full_acceptance._status_from_json_output(
                    name,
                    "not-json",
                    0,
                )

                self.assertEqual(status, "fail")
                self.assertIn("invalid JSON", summary)

    def test_market_capital_check_rejects_nonzero_exit(self) -> None:
        """New checks reject non-zero exit codes directly."""
        for name in ("ashare_capital", "cn_futures_capital"):
            with self.subTest(name=name):
                status, summary = full_acceptance._status_from_json_output(
                    name,
                    '{"status":"market_capital_available"}',
                    2,
                )

                self.assertEqual(status, "fail")
                self.assertIn("exit=2", summary)

    def test_market_capital_check_accepts_available_status(self) -> None:
        """market_capital_available → pass when fresh and reconciled."""
        status, summary = full_acceptance._status_from_json_output(
            "ashare_capital",
            '{"status":"market_capital_available","market":"ashare","fresh":true,"reconciled":true,"real_trading_enabled":false}',
            0,
        )
        self.assertEqual(status, "pass")

    def test_market_capital_check_rejects_unavailable(self) -> None:
        """market_capital_unavailable → fail."""
        status, summary = full_acceptance._status_from_json_output(
            "ashare_capital",
            '{"status":"market_capital_unavailable","market":"ashare"}',
            0,
        )
        self.assertEqual(status, "fail")


if __name__ == "__main__":
    unittest.main()
