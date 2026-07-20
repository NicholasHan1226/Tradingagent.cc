#!/usr/bin/env python3
# ruff: noqa: E402
"""Merge TradingAgent crontab into a multi-repo crontab.

Only allowed way to install/update TA cron entries. Never run ``crontab
shared/crontab.txt`` directly; it would overwrite TradingDatas/MarketGraph.

Default: dry-run to stdout. --apply: backup -> install -> readback verification;
auto-rollback on readback/coverage failure.  --current-file/--output: file-mode.

Usage
-----
    python3 tools/merge_tradingagent_crontab.py               # dry-run
    python3 tools/merge_tradingagent_crontab.py --current-file /tmp/cron.txt
    sudo python3 tools/merge_tradingagent_crontab.py --apply  # production
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.runtime_test.cron_coverage import (
    TRADINGAGENT_ASHARE_SIM_HERMES_ENABLED,
    TRADINGAGENT_ASHARE_SIM_WEBHOOK_ENABLED,
    TRADINGAGENT_BASH_ENV as TRADINGAGENT_BASH_ENV_PATH,
    TRADINGAGENT_CRON_TZ,
    TRADINGAGENT_REAL_TRADING_ENABLED,
    TRADINGAGENT_SHELL,
    TRADINGAGENT_TIMEZONE,
    _tradingagent_environment_mismatches,
    tradingagent_entries,
)

TEMPLATE_PATH = ROOT / "shared" / "crontab.txt"
BACKUP_DIR = ROOT / "runtime" / "backups" / "crontab"
USER = "marketgraph"
TRADINGAGENT_BASH_ENV = f"BASH_ENV={TRADINGAGENT_BASH_ENV_PATH}"
TRADINGAGENT_MANAGED_BLOCK_BEGIN = "# BEGIN TRADINGAGENT MANAGED CRON"
TRADINGAGENT_MANAGED_BLOCK_END = "# END TRADINGAGENT MANAGED CRON"
TRADINGAGENT_SCHEDULE_PAUSED_MARKER = (
    "# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff"
)
TRADINGAGENT_ENVIRONMENT_LINES = (
    f"SHELL={TRADINGAGENT_SHELL}",
    f"CRON_TZ={TRADINGAGENT_CRON_TZ}",
    f"TZ={TRADINGAGENT_TIMEZONE}",
    f"REAL_TRADING_ENABLED={TRADINGAGENT_REAL_TRADING_ENABLED}",
    f"ASHARE_SIM_HERMES_ENABLED={TRADINGAGENT_ASHARE_SIM_HERMES_ENABLED}",
    f"ASHARE_SIM_WEBHOOK_ENABLED={TRADINGAGENT_ASHARE_SIM_WEBHOOK_ENABLED}",
    TRADINGAGENT_BASH_ENV,
)
RETIRED_GENERIC_SCHEDULE_MARKERS = (
    "/cron/auto_pipeline.sh",
    "/cron/evolution.sh",
    "/cron/health_check.sh",
    "/cron/daily_review.sh",
    "/job_equity_snapshots.sh",
    "/job_pm_research_probability.sh",
    "/job_self_heal.sh",
    "/job_self_heal_night.sh",
    "/job_market_capital_reconcile.sh",
    "/job_cn_futures_observation_report.sh",
    "/job_cn_futures_sample_ops.sh",
    "/job_cn_futures_calibration_report.sh",
    "/job_cn_futures_replay.sh",
    "/job_cn_futures_pre_open_validation.sh",
    "/job_cn_futures_opening_validation.sh",
    "/job_cn_futures_first_sample_alert.sh",
    "/job_opening_acceptance.sh",
    "/job_daily_brief_morning.sh",
    "/job_daily_brief_day.sh",
    "/job_daily_brief_night.sh",
    "/job_email_notify.sh",
    "/job_premarket_signals.sh",
    "/job_us_sim.sh",
    "/job_crypto_sim.sh",
    "/job_pm_sim.sh",
    "/job_cn_futures_sim.sh",
)


# ---------------------------------------------------------------------------
# TA schedule-line detection (matches cron_coverage._is_cron_schedule_line)
# ---------------------------------------------------------------------------


def _is_ta_schedule_line(line: str) -> bool:
    """Return whether a line is a TradingAgent cron schedule entry."""
    return bool(tradingagent_entries(line))


def _is_retired_ta_schedule_line(line: str) -> bool:
    """Reject recurring work whose compatibility entrypoint is fail-closed."""

    return (
        "/job_ashare_" in line
        or "/job_market_capital_reconcile.sh ashare" in line
        or any(marker in line for marker in RETIRED_GENERIC_SCHEDULE_MARKERS)
    )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _without_existing_managed_block(lines: list[str]) -> list[str] | None:
    """Remove one prior managed block, rejecting malformed or duplicate markers."""

    kept: list[str] = []
    inside = False
    seen = False
    for line in lines:
        marker = line.strip()
        if marker == TRADINGAGENT_MANAGED_BLOCK_BEGIN:
            if inside or seen:
                return None
            inside = True
            seen = True
            continue
        if marker == TRADINGAGENT_MANAGED_BLOCK_END:
            if not inside:
                return None
            inside = False
            continue
        if not inside:
            kept.append(line)
    if inside:
        return None
    return kept


def merge(current_text: str, template_text: str) -> str | None:
    """Return merged crontab, including an explicitly paused zero-job block.

    Strips TA schedule lines from *current_text*, then appends the TradingAgent
    self-contained simulation-only/timezone environment followed by raw TA
    schedule lines from *template_text*. All other lines are preserved as-is in
    order.
    """
    expected = tradingagent_entries(template_text)
    ta_raw = [line for line in template_text.splitlines() if _is_ta_schedule_line(line)]
    template_lines = [line.strip() for line in template_text.splitlines()]
    explicitly_paused = TRADINGAGENT_SCHEDULE_PAUSED_MARKER in template_lines
    if (
        (not expected and not explicitly_paused)
        or (expected and explicitly_paused)
        or len(expected) != len(set(expected))
        or len(ta_raw) != len(expected)
        or any(
            template_lines.count(line) != 1 for line in TRADINGAGENT_ENVIRONMENT_LINES
        )
        or any(_is_retired_ta_schedule_line(line) for line in ta_raw)
    ):
        return None
    unmanaged = _without_existing_managed_block(current_text.splitlines())
    if unmanaged is None:
        return None
    kept = [
        line
        for line in unmanaged
        if not _is_ta_schedule_line(line) and line.strip() != TRADINGAGENT_BASH_ENV
    ]
    managed = [
        TRADINGAGENT_MANAGED_BLOCK_BEGIN,
        *TRADINGAGENT_ENVIRONMENT_LINES,
        *([TRADINGAGENT_SCHEDULE_PAUSED_MARKER] if explicitly_paused else []),
        *ta_raw,
        TRADINGAGENT_MANAGED_BLOCK_END,
    ]
    return "\n".join(kept + managed) + "\n"


# ---------------------------------------------------------------------------
# System helpers
# ---------------------------------------------------------------------------


def _crontab(user: str, args: list[str], stdin: str | None = None) -> tuple[str, str]:
    cmd = ["crontab", "-u", user] + args
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return "", f"{' '.join(cmd)}: {type(exc).__name__}: {exc}"
    if r.returncode == 0:
        return r.stdout, ""
    err = r.stderr.strip() or r.stdout.strip()
    if "-l" in args and "no crontab" in err.lower():
        return "", ""
    return "", f"{' '.join(cmd)}: {err}"


def _read(user: str) -> tuple[str, str]:
    return _crontab(user, ["-l"])


def _write(user: str, text: str) -> tuple[str, str]:
    return _crontab(user, ["-"], stdin=text)


def _backup(text: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = BACKUP_DIR / f"marketgraph_crontab_{ts}.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Apply workflow
# ---------------------------------------------------------------------------


def _ta_coverage_ok(text: str, template_text: str) -> bool:
    expected = tradingagent_entries(template_text)
    actual = tradingagent_entries(text)
    if actual != expected or _tradingagent_environment_mismatches(text):
        return False
    if TRADINGAGENT_SCHEDULE_PAUSED_MARKER in template_text:
        lines = [line.strip() for line in text.splitlines()]
        return (
            lines.count(TRADINGAGENT_MANAGED_BLOCK_BEGIN) == 1
            and lines.count(TRADINGAGENT_MANAGED_BLOCK_END) == 1
            and lines.count(TRADINGAGENT_SCHEDULE_PAUSED_MARKER) == 1
            and all(lines.count(line) == 1 for line in TRADINGAGENT_ENVIRONMENT_LINES)
        )
    return bool(expected)


def apply_merge(
    template_text: str,
    *,
    user: str = USER,
    dry_run: bool = True,
    output_path: str | None = None,
) -> dict:
    report: dict = {"action": "dry_run" if dry_run else "apply", "user": user}

    current, err = _read(user)
    if err:
        return {**report, "status": "fail", "failure": "read_current", "error": err}

    merged = merge(current, template_text)
    if merged is None:
        return {**report, "status": "fail", "failure": "empty_template"}

    if dry_run:
        if output_path:
            Path(output_path).write_text(merged, encoding="utf-8")
            report["output_path"] = output_path
        report["status"] = "pass"
        report["merged_preview"] = merged
        return report

    # --apply: backup -> install -> readback verification -> rollback on failure
    try:
        report["backup_path"] = str(_backup(current))
    except OSError as exc:
        return {
            **report,
            "status": "fail",
            "failure": "backup_failed",
            "error": str(exc),
        }

    _, err = _write(user, merged)
    if err:
        return {**report, "status": "fail", "failure": "install_failed", "error": err}

    readback, err = _read(user)
    if err or not _ta_coverage_ok(readback, template_text):
        _, rollback_error = _write(user, current)
        rb2, rollback_read_error = _read(user)
        if rollback_error or rollback_read_error or rb2 != current:
            return {
                **report,
                "status": "fail",
                "failure": "rollback_readback_mismatch",
                "error": rollback_error or rollback_read_error,
            }
        reason = "readback_failed" if err else "coverage_mismatch"
        return {**report, "status": "fail", "failure": reason}

    report["status"] = "pass"
    if output_path:
        Path(output_path).write_text(merged, encoding="utf-8")
        report["output_path"] = output_path
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge TradingAgent crontab into a multi-repo crontab."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--current-file")
    parser.add_argument("--output", "-o")
    parser.add_argument("--user", default=USER)
    args = parser.parse_args(argv)

    if args.apply and args.current_file:
        print(
            "ERROR: --apply and --current-file are mutually exclusive.", file=sys.stderr
        )
        return 2

    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")

    if args.current_file:
        current_text = Path(args.current_file).read_text(encoding="utf-8")
        merged = merge(current_text, template_text)
        if merged is None:
            print(
                "ERROR: template has no TradingAgent schedule entries.", file=sys.stderr
            )
            return 2
        if args.output:
            Path(args.output).write_text(merged, encoding="utf-8")
        else:
            sys.stdout.write(merged)
        return 0

    report = apply_merge(
        template_text, user=args.user, dry_run=not args.apply, output_path=args.output
    )
    if report["status"] == "pass":
        print("status: pass")
        if not args.apply and not args.output:
            sys.stdout.write("\n" + report.get("merged_preview", ""))
        return 0
    else:
        print(f"status: fail ({report.get('failure', 'unknown')})")
        if "error" in report:
            print(f"error: {report['error']}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
