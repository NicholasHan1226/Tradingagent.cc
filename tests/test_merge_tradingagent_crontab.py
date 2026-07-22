# ruff: noqa: E402
"""Tests for tools/merge_tradingagent_crontab.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from tools.merge_tradingagent_crontab import (
    _is_ta_schedule_line,
    _ta_coverage_ok,
    apply_merge,
    merge,
)

TA_TEMPLATE = """\
# TradingAgent cron snapshot
SHELL=/bin/bash
CRON_TZ=Asia/Shanghai
TZ=Asia/Shanghai
REAL_TRADING_ENABLED=false
ASHARE_SIM_HERMES_ENABLED=0
ASHARE_SIM_WEBHOOK_ENABLED=0
BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh

# Hypothetical post-handoff jobs used only to test generic merge mechanics.
*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_future_catalog_loop.sh
4,34 * * * * /opt/investment/tradingagent/shared/wrappers/job_future_sim_snapshot.sh
0 */4 * * * /opt/investment/tradingagent/cron/future_challenger_audit.sh
"""

PAUSED_TEMPLATE = """\
# TradingAgent cron snapshot
SHELL=/bin/bash
CRON_TZ=Asia/Shanghai
TZ=Asia/Shanghai
REAL_TRADING_ENABLED=false
ASHARE_SIM_HERMES_ENABLED=0
ASHARE_SIM_WEBHOOK_ENABLED=0
BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh
# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff
"""

EXTERNAL_BACKUP_DIR = Path("/opt/investment/release-evidence/tradingagent/test/cron")

CONTROLLED_ENVIRONMENT_ASSIGNMENTS = (
    ("SHELL=/bin/bash", "SHELL=/bin/sh"),
    ("CRON_TZ=Asia/Shanghai", "CRON_TZ=UTC"),
    ("TZ=Asia/Shanghai", "TZ=UTC"),
    ("REAL_TRADING_ENABLED=false", "REAL_TRADING_ENABLED=true"),
    ("ASHARE_SIM_HERMES_ENABLED=0", "ASHARE_SIM_HERMES_ENABLED=1"),
    ("ASHARE_SIM_WEBHOOK_ENABLED=0", "ASHARE_SIM_WEBHOOK_ENABLED=1"),
    (
        "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
        "BASH_ENV=/wrong/loader.sh",
    ),
)

CURRENT = """\
# TradingDatas market data
*/5 * * * * /opt/investment/tradingdatas/collectors/provider_transport.sh >> /opt/investment/tradingdatas/logs/provider_transport.log 2>&1

# MarketGraph research
30 */4 * * * /opt/investment/marketgraph/jobs/research_pipeline.sh >> /opt/investment/marketgraph/logs/research.log 2>&1

# OLD TA entries (should be removed)
1,6,11,16,21,26,31,36,41,46,51,56 9-15 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_sim_exec.sh >> /opt/investment/tradingagent/shared/logs/cron/job_ashare_sim_exec.log 2>&1
*/10 * * * * /opt/investment/tradingagent/cron/health_check.sh

# Old TA entry not in template (must be removed)
30 10 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_old_removed.sh >> /opt/investment/tradingagent/shared/logs/old.log 2>&1

SHELL=/bin/sh
BASH_ENV=/some/old/path
"""


class MergeTests(unittest.TestCase):
    """Core merge logic without system calls."""

    def test_appended_ta_block_resets_effective_bash_env(self):
        """TA schedule block must carry its own BASH_ENV line before entries."""
        current = (
            CURRENT
            + "BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh\n"
        )
        result = merge(current, TA_TEMPLATE)
        lines = result.splitlines()
        first_ta = next(i for i, line in enumerate(lines) if _is_ta_schedule_line(line))
        self.assertEqual(
            lines[first_ta - 1],
            "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
        )
        self.assertEqual(
            lines[first_ta - 7 : first_ta],
            [
                "SHELL=/bin/bash",
                "CRON_TZ=Asia/Shanghai",
                "TZ=Asia/Shanghai",
                "REAL_TRADING_ENABLED=false",
                "ASHARE_SIM_HERMES_ENABLED=0",
                "ASHARE_SIM_WEBHOOK_ENABLED=0",
                "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
            ],
        )

    def test_repeated_merge_does_not_accumulate_trailing_bash_env(self):
        once = merge(CURRENT, TA_TEMPLATE)

        twice = merge(once, TA_TEMPLATE)

        marker = "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh"
        self.assertEqual(twice.count(marker), once.count(marker))
        for assignment in (
            "CRON_TZ=Asia/Shanghai",
            "TZ=Asia/Shanghai",
            "REAL_TRADING_ENABLED=false",
            "ASHARE_SIM_HERMES_ENABLED=0",
            "ASHARE_SIM_WEBHOOK_ENABLED=0",
        ):
            self.assertEqual(twice.count(assignment), once.count(assignment))

    def test_repeated_merge_removes_old_marker_before_trailing_comments(self):
        once = merge(CURRENT, TA_TEMPLATE)
        current = once + "# appended by another repository\n"

        twice = merge(current, TA_TEMPLATE)

        marker = "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh"
        self.assertEqual(twice.count(marker), 1)

    def test_readback_coverage_rejects_wrong_effective_bash_env(self):
        merged = merge(CURRENT, TA_TEMPLATE)
        wrong = merged.replace(
            "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
            "BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh",
        )

        self.assertFalse(_ta_coverage_ok(wrong, TA_TEMPLATE))

    def test_readback_coverage_rejects_live_or_wrong_timezone_environment(self):
        merged = merge(CURRENT, TA_TEMPLATE)

        self.assertFalse(
            _ta_coverage_ok(
                merged.replace(
                    "REAL_TRADING_ENABLED=false", "REAL_TRADING_ENABLED=true"
                ),
                TA_TEMPLATE,
            )
        )
        self.assertFalse(
            _ta_coverage_ok(
                merged.replace("CRON_TZ=Asia/Shanghai", "CRON_TZ=UTC"),
                TA_TEMPLATE,
            )
        )
        self.assertFalse(
            _ta_coverage_ok(
                merged.replace("TZ=Asia/Shanghai", "TZ=UTC"),
                TA_TEMPLATE,
            )
        )
        self.assertFalse(
            _ta_coverage_ok(
                merged.replace(
                    "ASHARE_SIM_HERMES_ENABLED=0", "ASHARE_SIM_HERMES_ENABLED=1"
                ),
                TA_TEMPLATE,
            )
        )
        self.assertFalse(
            _ta_coverage_ok(
                merged.replace(
                    "ASHARE_SIM_WEBHOOK_ENABLED=0", "ASHARE_SIM_WEBHOOK_ENABLED=1"
                ),
                TA_TEMPLATE,
            )
        )

    def test_template_with_mismatched_bash_env_fails_closed(self):
        mismatched = TA_TEMPLATE.replace(
            "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh",
            "BASH_ENV=/wrong/loader.sh",
        )

        self.assertIsNone(merge(CURRENT, mismatched))

    def test_template_missing_any_required_simulation_environment_fails_closed(self):
        for assignment in (
            "SHELL=/bin/bash\n",
            "CRON_TZ=Asia/Shanghai\n",
            "TZ=Asia/Shanghai\n",
            "REAL_TRADING_ENABLED=false\n",
            "ASHARE_SIM_HERMES_ENABLED=0\n",
            "ASHARE_SIM_WEBHOOK_ENABLED=0\n",
            "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh\n",
        ):
            with self.subTest(assignment=assignment.strip()):
                self.assertIsNone(merge(CURRENT, TA_TEMPLATE.replace(assignment, "")))

    def test_preserves_cross_repo_and_env_comments(self):
        """SS/MG entries, env vars, comments survive verbatim in order.
        Also validates _is_ta_schedule_line edge cases."""
        result = merge(CURRENT, TA_TEMPLATE)
        preserved = (
            "\n".join(
                line for line in CURRENT.splitlines() if not _is_ta_schedule_line(line)
            )
            + "\n"
        )
        self.assertTrue(result.startswith(preserved))
        self.assertIn("/opt/investment/tradingdatas/", result)
        self.assertIn("/opt/investment/marketgraph/", result)
        self.assertIn("# TradingDatas market data", result)
        self.assertIn("# MarketGraph research", result)
        self.assertIn("SHELL=/bin/sh", result)
        self.assertIn("BASH_ENV=/some/old/path", result)
        self.assertLess(
            result.index("BASH_ENV=/some/old/path"),
            result.index("CRON_TZ=Asia/Shanghai"),
        )
        # _is_ta_schedule_line edge cases
        self.assertTrue(
            _is_ta_schedule_line(
                "*/10 * * * * /opt/investment/tradingagent/cron/health_check.sh"
            )
        )
        self.assertFalse(_is_ta_schedule_line(""))
        self.assertFalse(_is_ta_schedule_line("SHELL=/bin/bash"))
        self.assertFalse(
            _is_ta_schedule_line(
                "*/5 * * * * /opt/investment/tradingdatas/collectors/provider_transport.sh"
            )
        )

    def test_replaces_old_ta_no_duplicates(self):
        """Old TA lines gone; template TA lines appear exactly once."""
        result = merge(CURRENT, TA_TEMPLATE)
        self.assertNotIn("job_old_removed.sh", result)
        self.assertNotIn("/cron/health_check.sh", result)
        self.assertNotIn("job_ashare_sim_exec.sh", result)
        self.assertNotIn("job_sim_market_health.sh", result)
        self.assertEqual(result.count("job_future_sim_snapshot.sh"), 1)

    def test_empty_template_fails(self):
        """Template with zero TA schedule entries returns None."""
        self.assertIsNone(merge(CURRENT, "# no schedule lines\nSHELL=/bin/bash\n"))
        duplicate = (
            TA_TEMPLATE
            + "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_future_catalog_loop.sh\n"
        )
        self.assertIsNone(merge(CURRENT, duplicate))

    def test_template_with_retired_ashare_or_generic_job_fails_closed(self):
        retired_schedules = (
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_ashare_sample_ops.sh",
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh ashare ops",
            "*/10 * * * * /opt/investment/tradingagent/cron/health_check.sh",
            "30 7 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_daily_brief_morning.sh",
            "8,38 * * * * /opt/investment/tradingagent/shared/wrappers/job_us_sim.sh",
            "9,39 * * * * /opt/investment/tradingagent/shared/wrappers/job_crypto_sim.sh",
            "7,37 * * * * /opt/investment/tradingagent/shared/wrappers/job_pm_sim.sh",
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_cn_futures_sim.sh",
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_equity_snapshots.sh",
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_self_heal.sh",
            "*/5 * * * * /opt/investment/tradingagent/shared/wrappers/job_market_capital_reconcile.sh cn_futures ops",
            "0 */4 * * * /opt/investment/tradingagent/cron/evolution.sh",
            "0 9 * * 1-5 /opt/investment/tradingagent/cron/auto_pipeline.sh",
        )

        for schedule in retired_schedules:
            with self.subTest(schedule=schedule):
                self.assertIsNone(merge(CURRENT, TA_TEMPLATE + schedule + "\n"))

    def test_shared_crontab_is_the_only_merge_authority(self):
        from tools import merge_tradingagent_crontab

        self.assertEqual(
            merge_tradingagent_crontab.TEMPLATE_PATH,
            _HERE.parent / "shared" / "crontab.txt",
        )

    def test_malformed_or_duplicate_managed_block_fails_closed(self):
        begin = "# BEGIN TRADINGAGENT MANAGED CRON"
        end = "# END TRADINGAGENT MANAGED CRON"

        self.assertIsNone(merge(CURRENT + begin + "\n", TA_TEMPLATE))
        self.assertIsNone(merge(CURRENT + end + "\n", TA_TEMPLATE))
        self.assertIsNone(
            merge(
                CURRENT + begin + "\n" + end + "\n" + begin + "\n" + end + "\n",
                TA_TEMPLATE,
            )
        )

    def test_current_no_ta_adds_all(self):
        """Current without any TA lines gets all template entries appended."""
        result = merge("# other repo\n*/5 * * * * /usr/bin/foo\n", TA_TEMPLATE)
        self.assertIn("/usr/bin/foo", result)
        self.assertNotIn("job_sim_market_health.sh", result)
        self.assertIn("job_future_sim_snapshot.sh", result)
        self.assertNotIn("/cron/health_check.sh", result)
        self.assertNotIn("job_ashare_", result)
        self.assertIn("future_challenger_audit.sh", result)

    def test_explicit_paused_template_removes_all_ta_jobs(self):
        result = merge(CURRENT, PAUSED_TEMPLATE)

        self.assertIsNotNone(result)
        self.assertEqual(
            [line for line in result.splitlines() if _is_ta_schedule_line(line)],
            [],
        )
        self.assertIn(
            "# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff",
            result,
        )
        self.assertTrue(_ta_coverage_ok(result, PAUSED_TEMPLATE))

    def test_paused_coverage_preserves_other_project_shell_assignment(self):
        current = (
            "# TradingDatas runtime environment\n"
            "SHELL=/bin/bash\n"
            "*/5 * * * * /opt/investment/tradingdatas/collectors/provider_transport.sh\n"
        )

        result = merge(current, PAUSED_TEMPLATE)

        self.assertIsNotNone(result)
        self.assertEqual(result.count("SHELL=/bin/bash"), 2)
        self.assertTrue(_ta_coverage_ok(result, PAUSED_TEMPLATE))

    def test_coverage_rejects_extra_controlled_assignment_inside_managed_block(self):
        result = merge("", TA_TEMPLATE)
        self.assertIsNotNone(result)

        for expected, wrong in CONTROLLED_ENVIRONMENT_ASSIGNMENTS:
            variants = {
                "before_expected": result.replace(
                    expected + "\n", wrong + "\n" + expected + "\n", 1
                ),
                "after_expected": result.replace(
                    expected + "\n", expected + "\n" + wrong + "\n", 1
                ),
                "after_jobs": result.replace(
                    "# END TRADINGAGENT MANAGED CRON",
                    wrong + "\n# END TRADINGAGENT MANAGED CRON",
                    1,
                ),
            }
            for position, invalid in variants.items():
                with self.subTest(
                    variable=expected.split("=", 1)[0], position=position
                ):
                    self.assertFalse(_ta_coverage_ok(invalid, TA_TEMPLATE))

    def test_coverage_rejects_schedule_state_namespace_variants(self):
        paused = merge("", PAUSED_TEMPLATE)
        active = merge("", TA_TEMPLATE)
        self.assertIsNotNone(paused)
        self.assertIsNotNone(active)
        expected_marker = (
            "# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff"
        )

        for value in ("active", "paused_wrong"):
            variant = f"# TRADINGAGENT_SCHEDULE_STATE={value}"
            with self.subTest(template="paused", value=value):
                self.assertFalse(
                    _ta_coverage_ok(
                        paused.replace(
                            expected_marker,
                            expected_marker + "\n" + variant,
                            1,
                        ),
                        PAUSED_TEMPLATE,
                    )
                )
            with self.subTest(template="active", value=value):
                self.assertFalse(
                    _ta_coverage_ok(
                        active.replace(
                            "# END TRADINGAGENT MANAGED CRON",
                            variant + "\n# END TRADINGAGENT MANAGED CRON",
                            1,
                        ),
                        TA_TEMPLATE,
                    )
                )

    def test_coverage_rejects_schedule_state_markers_outside_managed_block(self):
        paused = merge("", PAUSED_TEMPLATE)
        active = merge("", TA_TEMPLATE)
        self.assertIsNotNone(paused)
        self.assertIsNotNone(active)

        for marker in (
            "# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff",
            "# TRADINGAGENT_SCHEDULE_STATE=active",
            "# TRADINGAGENT_SCHEDULE_STATE=paused_wrong",
        ):
            with self.subTest(template="paused", marker=marker):
                self.assertFalse(
                    _ta_coverage_ok(marker + "\n" + paused, PAUSED_TEMPLATE)
                )
            with self.subTest(template="active", marker=marker):
                self.assertFalse(_ta_coverage_ok(marker + "\n" + active, TA_TEMPLATE))

    def test_coverage_retains_exact_env_rejection_and_outside_assignments(self):
        active = merge("", TA_TEMPLATE)
        paused = merge("", PAUSED_TEMPLATE)
        self.assertIsNotNone(active)
        self.assertIsNotNone(paused)

        for expected, _wrong in CONTROLLED_ENVIRONMENT_ASSIGNMENTS:
            with self.subTest(variable=expected.split("=", 1)[0]):
                self.assertFalse(
                    _ta_coverage_ok(
                        active.replace(
                            expected + "\n", expected + "\n" + expected + "\n", 1
                        ),
                        TA_TEMPLATE,
                    )
                )

        outside = "\n".join(
            expected
            for expected, _wrong in CONTROLLED_ENVIRONMENT_ASSIGNMENTS
            if not expected.startswith("BASH_ENV=")
        )
        self.assertTrue(_ta_coverage_ok(outside + "\n" + active, TA_TEMPLATE))
        self.assertTrue(_ta_coverage_ok(outside + "\n" + paused, PAUSED_TEMPLATE))

    def test_merge_preserves_other_repo_env_and_sanitizes_orphan_ta_bash_env(self):
        ta_bash_env = "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh"
        preserved_lines = [
            "# TradingDatas runtime environment",
            *(
                expected
                for expected, _wrong in CONTROLLED_ENVIRONMENT_ASSIGNMENTS
                if not expected.startswith("BASH_ENV=")
            ),
            "BASH_ENV=/opt/investment/tradingdatas/runtime/env_loader.sh",
            "*/5 * * * * /opt/investment/tradingdatas/collectors/provider_transport.sh",
        ]
        current = "\n".join([*preserved_lines[:-1], ta_bash_env, preserved_lines[-1]])

        result = merge(current + "\n", PAUSED_TEMPLATE)

        self.assertIsNotNone(result)
        unmanaged, marker, _managed = result.partition(
            "# BEGIN TRADINGAGENT MANAGED CRON"
        )
        self.assertEqual(marker, "# BEGIN TRADINGAGENT MANAGED CRON")
        self.assertEqual(unmanaged, "\n".join(preserved_lines) + "\n")
        self.assertEqual(result.count(ta_bash_env), 1)
        self.assertTrue(_ta_coverage_ok(result, PAUSED_TEMPLATE))


class ApplyWorkflowTests(unittest.TestCase):
    """apply_merge with mocked system calls."""

    def test_dry_run_no_system_write(self):
        with (
            patch("tools.merge_tradingagent_crontab._read") as mr,
            patch("tools.merge_tradingagent_crontab._backup") as mb,
            patch("tools.merge_tradingagent_crontab._write") as mw,
        ):
            mr.return_value = (CURRENT, "")
            report = apply_merge(TA_TEMPLATE, dry_run=True)
            self.assertEqual(report["status"], "pass")
            self.assertIn("merged_preview", report)
            mb.assert_not_called()
            mw.assert_not_called()

    def test_backup_failure_no_install(self):
        with (
            patch("tools.merge_tradingagent_crontab._read") as mr,
            patch("tools.merge_tradingagent_crontab._write") as mw,
        ):
            mr.return_value = (CURRENT, "")
            with patch(
                "tools.merge_tradingagent_crontab._backup",
                side_effect=OSError("disk full"),
            ):
                report = apply_merge(
                    TA_TEMPLATE,
                    dry_run=False,
                    backup_dir=EXTERNAL_BACKUP_DIR,
                )
                self.assertEqual(report["status"], "fail")
                self.assertEqual(report["failure"], "backup_failed")
                mw.assert_not_called()

    def test_install_failure(self):
        with patch("tools.merge_tradingagent_crontab._read") as mr:
            mr.return_value = (CURRENT, "")
            with patch(
                "tools.merge_tradingagent_crontab._backup",
                return_value=Path("/tmp/backup.txt"),
            ):
                with patch("tools.merge_tradingagent_crontab._write") as mw:
                    mw.return_value = ("", "permission denied")
                    report = apply_merge(
                        TA_TEMPLATE,
                        dry_run=False,
                        backup_dir=EXTERNAL_BACKUP_DIR,
                    )
                    self.assertEqual(report["status"], "fail")
                    self.assertEqual(report["failure"], "install_failed")
                    mw.assert_called_once_with(
                        "marketgraph", merge(CURRENT, TA_TEMPLATE)
                    )

    def test_readback_failure_rollback(self):
        merged = merge("", TA_TEMPLATE)
        reads = iter([("", ""), ("", "readback failed"), ("", "")])

        with patch("tools.merge_tradingagent_crontab._read") as mr:
            mr.side_effect = lambda user: next(reads)
            with patch(
                "tools.merge_tradingagent_crontab._backup",
                return_value=Path("/tmp/backup.txt"),
            ) as mb:
                with patch("tools.merge_tradingagent_crontab._write") as mw:
                    mw.return_value = ("", "")
                    report = apply_merge(
                        TA_TEMPLATE,
                        dry_run=False,
                        backup_dir=EXTERNAL_BACKUP_DIR,
                    )
                    self.assertEqual(report["status"], "fail")
                    self.assertEqual(report["failure"], "readback_failed")
                    mb.assert_called_once_with("", EXTERNAL_BACKUP_DIR)
                    self.assertEqual(
                        mw.call_args_list,
                        [call("marketgraph", merged), call("marketgraph", "")],
                    )

    def test_readback_coverage_mismatch_rollback(self):
        # Readback missing a TA entry triggers rollback.
        bad_readback = merge(CURRENT, TA_TEMPLATE).replace(
            "future_challenger_audit.sh", ""
        )
        reads = iter([(CURRENT, ""), (bad_readback, ""), (CURRENT, "")])

        with patch("tools.merge_tradingagent_crontab._read") as mr:
            mr.side_effect = lambda user: next(reads)
            with patch(
                "tools.merge_tradingagent_crontab._backup",
                return_value=Path("/tmp/backup.txt"),
            ):
                with patch("tools.merge_tradingagent_crontab._write") as mw:
                    mw.return_value = ("", "")
                    report = apply_merge(
                        TA_TEMPLATE,
                        dry_run=False,
                        backup_dir=EXTERNAL_BACKUP_DIR,
                    )
                    self.assertEqual(report["status"], "fail")
                    self.assertEqual(report["failure"], "coverage_mismatch")
                    self.assertEqual(
                        mw.call_args_list,
                        [
                            call("marketgraph", merge(CURRENT, TA_TEMPLATE)),
                            call("marketgraph", CURRENT),
                        ],
                    )

    def test_apply_success(self):
        merged = merge(CURRENT, TA_TEMPLATE)
        reads = iter([(CURRENT, ""), (merged, ""), (merged, "")])

        with patch("tools.merge_tradingagent_crontab._read") as mr:
            mr.side_effect = lambda user: next(reads)
            with patch(
                "tools.merge_tradingagent_crontab._backup",
                return_value=Path("/tmp/backup.txt"),
            ):
                with patch("tools.merge_tradingagent_crontab._write") as mw:
                    mw.return_value = ("", "")
                    report = apply_merge(
                        TA_TEMPLATE,
                        dry_run=False,
                        backup_dir=EXTERNAL_BACKUP_DIR,
                    )
                    self.assertEqual(report["status"], "pass")
                    self.assertEqual(report["action"], "apply")
                    mw.assert_called_once_with("marketgraph", merged)

    def test_apply_requires_external_backup_directory_before_system_write(self):
        with (
            patch("tools.merge_tradingagent_crontab._read") as mr,
            patch("tools.merge_tradingagent_crontab._backup") as mb,
            patch("tools.merge_tradingagent_crontab._write") as mw,
        ):
            mr.return_value = (CURRENT, "")

            report = apply_merge(TA_TEMPLATE, dry_run=False)

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["failure"], "backup_dir_required")
            mb.assert_not_called()
            mw.assert_not_called()

    def test_apply_rejects_repository_local_backup_directory(self):
        repo_local = _HERE.parent / "runtime" / "backups" / "crontab"
        with (
            patch("tools.merge_tradingagent_crontab._read") as mr,
            patch("tools.merge_tradingagent_crontab._backup") as mb,
            patch("tools.merge_tradingagent_crontab._write") as mw,
        ):
            mr.return_value = (CURRENT, "")

            report = apply_merge(
                TA_TEMPLATE,
                dry_run=False,
                backup_dir=repo_local,
            )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["failure"], "backup_dir_invalid")
            mb.assert_not_called()
            mw.assert_not_called()

    def test_apply_rejects_symlinked_parent_into_repository(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            repository = root / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            runtime = repository / "runtime"
            runtime.mkdir()
            alias = root / "alias"
            alias.symlink_to(runtime, target_is_directory=True)
            backup_dir = alias / "evidence"
            with (
                patch("tools.merge_tradingagent_crontab._read") as mr,
                patch("tools.merge_tradingagent_crontab._backup") as mb,
                patch("tools.merge_tradingagent_crontab._write") as mw,
            ):
                mr.return_value = (CURRENT, "")

                report = apply_merge(
                    TA_TEMPLATE,
                    dry_run=False,
                    backup_dir=backup_dir,
                )

                self.assertEqual(report["status"], "fail")
                self.assertEqual(report["failure"], "backup_dir_invalid")
                mb.assert_not_called()
                mw.assert_not_called()


class FileModeTests(unittest.TestCase):
    """--current-file / --output flow."""

    def test_file_mode_with_output(self):
        with (
            tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as cf,
            tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as of,
        ):
            cf.write(CURRENT)
            current_path = cf.name
            output_path = of.name
        try:
            from tools.merge_tradingagent_crontab import main

            rc = main(["--current-file", current_path, "--output", output_path])
            self.assertEqual(rc, 0)
            content = Path(output_path).read_text()
            self.assertNotIn("job_sim_market_health.sh", content)
            self.assertNotIn("job_equity_snapshots.sh", content)
            self.assertIn(
                "TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff",
                content,
            )
            self.assertNotIn("/cron/health_check.sh", content)
            self.assertNotIn("job_ashare_", content)
            self.assertNotIn("job_market_capital_reconcile.sh ashare", content)
            self.assertNotIn("job_daily_brief_", content)
            self.assertIn("/opt/investment/tradingdatas/", content)
            self.assertNotIn("job_old_removed.sh", content)
        finally:
            os.unlink(current_path)
            os.unlink(output_path)

    def test_file_mode_stdout(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as cf:
            cf.write(CURRENT)
            current_path = cf.name
        try:
            from tools.merge_tradingagent_crontab import main
            import io

            saved = sys.stdout
            sys.stdout = io.StringIO()
            try:
                rc = main(["--current-file", current_path])
            finally:
                out = sys.stdout.getvalue()
                sys.stdout = saved
            self.assertEqual(rc, 0)
            self.assertNotIn("job_sim_market_health.sh", out)
            self.assertNotIn("job_equity_snapshots.sh", out)
            self.assertIn(
                "TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff",
                out,
            )
            self.assertNotIn("/cron/health_check.sh", out)
            self.assertNotIn("job_ashare_", out)
            self.assertNotIn("job_market_capital_reconcile.sh ashare", out)
            self.assertNotIn("job_daily_brief_", out)
            self.assertIn("/opt/investment/tradingdatas/", out)
        finally:
            os.unlink(current_path)


if __name__ == "__main__":
    unittest.main()
