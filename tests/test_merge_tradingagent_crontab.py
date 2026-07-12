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
BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh

# A-share simulated execution
1,6,11,16,21,26,31,36,41,46,51,56 9-15 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_sim_exec.sh >> /opt/investment/tradingagent/shared/logs/cron/job_ashare_sim_exec.log 2>&1
35 8 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_preopen_dry_run.sh >> /opt/investment/tradingagent/shared/logs/cron/job_ashare_preopen_dry_run.log 2>&1
40 17,22 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_ashare_sample_ops.sh >> /opt/investment/tradingagent/shared/logs/cron/job_ashare_sample_ops.log 2>&1

# Health and evolution
*/10 * * * * /opt/investment/tradingagent/cron/health_check.sh
0 */4 * * * /opt/investment/tradingagent/cron/evolution.sh
"""

CURRENT = """\
# SharedSignals market data
*/5 * * * * /opt/investment/sharedsignals/collectors/quote_collector.sh >> /opt/investment/sharedsignals/logs/quote.log 2>&1

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
            lines[first_ta - 5 : first_ta],
            [
                "SHELL=/bin/bash",
                "CRON_TZ=Asia/Shanghai",
                "TZ=Asia/Shanghai",
                "REAL_TRADING_ENABLED=false",
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
        self.assertIn("/opt/investment/sharedsignals/", result)
        self.assertIn("/opt/investment/marketgraph/", result)
        self.assertIn("# SharedSignals market data", result)
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
                "*/5 * * * * /opt/investment/sharedsignals/collectors/quote.sh"
            )
        )

    def test_replaces_old_ta_no_duplicates(self):
        """Old TA lines gone; template TA lines appear exactly once."""
        result = merge(CURRENT, TA_TEMPLATE)
        self.assertNotIn("job_old_removed.sh", result)
        self.assertEqual(result.count("health_check.sh"), 1)
        self.assertEqual(result.count("job_ashare_sim_exec.sh"), 1)

    def test_empty_template_fails(self):
        """Template with zero TA schedule entries returns None."""
        self.assertIsNone(merge(CURRENT, "# no schedule lines\nSHELL=/bin/bash\n"))
        duplicate = (
            TA_TEMPLATE
            + "*/10 * * * * /opt/investment/tradingagent/cron/health_check.sh\n"
        )
        self.assertIsNone(merge(CURRENT, duplicate))

    def test_template_with_retired_sample_job_fails_closed(self):
        retired = TA_TEMPLATE.replace(
            "job_ashare_sample_ops.sh",
            "job_ashare_sample_learning.sh",
        )

        self.assertIsNone(merge(CURRENT, retired))

    def test_template_without_unified_sample_ops_fails_closed(self):
        without_sample_ops = "\n".join(
            line
            for line in TA_TEMPLATE.splitlines()
            if "job_ashare_sample_ops.sh" not in line
        )

        self.assertIsNone(merge(CURRENT, without_sample_ops))

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
        self.assertIn("health_check.sh", result)
        self.assertIn("evolution.sh", result)


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
                report = apply_merge(TA_TEMPLATE, dry_run=False)
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
                    report = apply_merge(TA_TEMPLATE, dry_run=False)
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
                    report = apply_merge(TA_TEMPLATE, dry_run=False)
                    self.assertEqual(report["status"], "fail")
                    self.assertEqual(report["failure"], "readback_failed")
                    mb.assert_called_once_with("")
                    self.assertEqual(
                        mw.call_args_list,
                        [call("marketgraph", merged), call("marketgraph", "")],
                    )

    def test_readback_coverage_mismatch_rollback(self):
        # Readback missing a TA entry triggers rollback.
        bad_readback = merge(CURRENT, TA_TEMPLATE).replace("evolution.sh", "")
        reads = iter([(CURRENT, ""), (bad_readback, ""), (CURRENT, "")])

        with patch("tools.merge_tradingagent_crontab._read") as mr:
            mr.side_effect = lambda user: next(reads)
            with patch(
                "tools.merge_tradingagent_crontab._backup",
                return_value=Path("/tmp/backup.txt"),
            ):
                with patch("tools.merge_tradingagent_crontab._write") as mw:
                    mw.return_value = ("", "")
                    report = apply_merge(TA_TEMPLATE, dry_run=False)
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
                    report = apply_merge(TA_TEMPLATE, dry_run=False)
                    self.assertEqual(report["status"], "pass")
                    self.assertEqual(report["action"], "apply")
                    mw.assert_called_once_with("marketgraph", merged)


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
            self.assertIn("health_check.sh", content)
            self.assertIn("/opt/investment/sharedsignals/", content)
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
            self.assertIn("health_check.sh", out)
            self.assertIn("/opt/investment/sharedsignals/", out)
        finally:
            os.unlink(current_path)


if __name__ == "__main__":
    unittest.main()
