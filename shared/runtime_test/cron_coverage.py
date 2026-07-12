#!/usr/bin/env python3
"""Read-only cron coverage guard for TradingAgent production entries."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TRADING_PREFIX = "/opt/investment/tradingagent/"
ROOT_UID = 0
ROOT_GID = 0
TRADINGAGENT_SHELL = "/bin/bash"
TRADINGAGENT_CRON_TZ = "Asia/Shanghai"
TRADINGAGENT_TIMEZONE = "Asia/Shanghai"
TRADINGAGENT_REAL_TRADING_ENABLED = "false"
TRADINGAGENT_BASH_ENV = "/opt/investment/tradingagent/shared/env_loader.sh"
TRADINGAGENT_REQUIRED_ENVIRONMENT = {
    "shell": ("SHELL", TRADINGAGENT_SHELL),
    "cron_tz": ("CRON_TZ", TRADINGAGENT_CRON_TZ),
    "timezone": ("TZ", TRADINGAGENT_TIMEZONE),
    "real_trading_enabled": (
        "REAL_TRADING_ENABLED",
        TRADINGAGENT_REAL_TRADING_ENABLED,
    ),
    "bash_env": ("BASH_ENV", TRADINGAGENT_BASH_ENV),
}


def _run_crontab(command: list[str]) -> tuple[str, str]:
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=5
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"{' '.join(command)}: {exc.__class__.__name__}: {exc}"
    if result.returncode == 0:
        return result.stdout, ""
    return "", f"{' '.join(command)}: {result.stderr.strip() or result.stdout.strip()}"


def _read_installed_crontabs() -> dict[str, str]:
    marketgraph_text, marketgraph_error = _run_crontab(
        ["crontab", "-u", "marketgraph", "-l"]
    )
    if os.geteuid() == ROOT_UID:
        root_text, root_error = _run_crontab(["crontab", "-l"])
    else:
        root_text, root_error = (
            "",
            "root crontab unchecked: run cron_coverage as root for residual audit",
        )
    return {
        "marketgraph_text": marketgraph_text,
        "marketgraph_error": marketgraph_error,
        "root_text": root_text,
        "root_error": root_error,
    }


def _is_cron_schedule_line(line: str) -> bool:
    if not line or line.startswith("#"):
        return False
    if (
        "=" in line
        and not line.split()[0].startswith(("@", "*"))
        and not line.split()[0][0].isdigit()
    ):
        return False
    return TRADING_PREFIX in line


def _normalize_entry(line: str) -> str:
    line = line.strip()
    line = re.sub(r"\s+>>\s+\S+.*$", "", line)
    line = re.sub(r"\s+", " ", line)
    return line


def tradingagent_entries(text: str) -> list[str]:
    return [
        _normalize_entry(line)
        for line in text.splitlines()
        if _is_cron_schedule_line(line.strip())
    ]


def _tradingagent_environment_mismatches(text: str) -> list[dict[str, Any]]:
    effective_environment = {
        variable: "" for variable, _ in TRADINGAGENT_REQUIRED_ENVIRONMENT.values()
    }
    mismatches: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if "=" in line and not line.startswith("#"):
            variable, value = line.split("=", 1)
            variable = variable.strip()
            if variable in effective_environment:
                effective_environment[variable] = value.strip()
                continue
        if _is_cron_schedule_line(line):
            mismatched_fields = [
                field
                for field, (
                    variable,
                    expected,
                ) in TRADINGAGENT_REQUIRED_ENVIRONMENT.items()
                if effective_environment[variable] != expected
            ]
            if not mismatched_fields:
                continue
            mismatches.append(
                {
                    "entry": _normalize_entry(line),
                    "mismatched_fields": mismatched_fields,
                    "effective_environment": dict(effective_environment),
                    "effective_bash_env": effective_environment["BASH_ENV"],
                }
            )
    return mismatches


def _template_entries(path: Path) -> list[str]:
    return tradingagent_entries(path.read_text(encoding="utf-8"))


def _template_log_targets(path: Path) -> list[Path]:
    targets: list[Path] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if TRADING_PREFIX not in line or ">>" not in line:
            continue
        raw = line.split(">>", 1)[1].strip().split()[0]
        if raw.endswith(".log"):
            targets.append(Path(raw.replace(TRADING_PREFIX, str(ROOT) + "/")))
    return targets


def _runtime_permission_candidate_paths() -> set[Path]:
    """Return active runtime paths that marketgraph cron needs to write."""
    candidates: set[Path] = {
        ROOT / "runtime/state",
        ROOT / "shared/review/ashare",
        ROOT / "shared/review/ashare/sample_journal.jsonl",
        ROOT / "shared/review/ashare/sample_kpi_latest.json",
        ROOT / "shared/review/ashare/sample_kpi_log.jsonl",
        ROOT / "shared/review/ashare/evolution_decision_latest.json",
        ROOT / "shared/review/ashare/evolution_decision_log.jsonl",
        ROOT / "shared/review/ashare/market_maturity_latest.json",
        ROOT / "shared/review/ashare/market_maturity_log.jsonl",
        ROOT / "shared/review/opportunities",
        ROOT / "shared/logs/trade_audit_trail.jsonl",
        ROOT / "shared/logs/cron/sim_market_health.log",
        ROOT / "shared/logs/cron/equity_snapshots.log",
    }
    candidates.update(_template_log_targets(ROOT / "shared/crontab.txt"))
    for pattern in (
        "runtime/state/*.lock",
        "shared/review/ashare/*.json",
        "shared/review/ashare/*.jsonl",
        "shared/review/opportunities/*.jsonl",
        "shared/logs/cron/job_*.log",
    ):
        candidates.update(ROOT.glob(pattern))
    retired_ashare_projection_names = {
        "forward_validation_latest.json",
        "forward_validation.jsonl",
        "portfolio_evolution_latest.json",
        "portfolio_evolution_log.jsonl",
        "sample_target_monitor_latest.json",
        "sample_target_monitor_log.jsonl",
        "sample_learning_latest.json",
        "sample_learning_log.jsonl",
        "tier_experiments_latest.json",
    }
    candidates = {
        path
        for path in candidates
        if not (
            path.parent == ROOT / "shared/review/ashare"
            and path.name in retired_ashare_projection_names
        )
        and not path.is_relative_to(ROOT / "shared/logs/local_sim_tiers")
    }
    return candidates


def _runtime_permission_blockers() -> list[str]:
    """Return active runtime paths that a marketgraph cron cannot safely write."""
    candidates = _runtime_permission_candidate_paths()

    blockers: list[str] = []
    for path in sorted(candidates):
        if not path.exists():
            continue
        try:
            stat_result = path.stat()
        except OSError:
            blockers.append(str(path.relative_to(ROOT)))
            continue
        if stat_result.st_uid == ROOT_UID or stat_result.st_gid == ROOT_GID:
            blockers.append(str(path.relative_to(ROOT)))
    return blockers


def check_cron_coverage(*, crontabs: dict[str, str] | None = None) -> dict[str, Any]:
    shared_template = ROOT / "shared/crontab.txt"
    root_template = ROOT / "crontab.txt"
    details = crontabs if crontabs is not None else _read_installed_crontabs()

    shared_entries = _template_entries(shared_template)
    root_entries = _template_entries(root_template)
    template_drift = [
        entry for entry in shared_entries if entry not in set(root_entries)
    ]
    template_extra = [
        entry for entry in root_entries if entry not in set(shared_entries)
    ]

    installed_text = details.get("marketgraph_text") or details.get("root_text") or ""
    installed_source = (
        "marketgraph"
        if details.get("marketgraph_text")
        else ("root" if details.get("root_text") else "")
    )
    installed_entries = set(tradingagent_entries(installed_text))
    missing_entries = [
        entry for entry in shared_entries if entry not in installed_entries
    ]

    env_mismatches = _tradingagent_environment_mismatches(installed_text)

    root_residual_entries: list[str] = []
    if details.get("marketgraph_text"):
        root_residual_entries = tradingagent_entries(details.get("root_text") or "")
    permission_blockers = _runtime_permission_blockers()

    failures: list[str] = []
    if template_drift or template_extra:
        failures.append("template_drift")
    if not installed_text:
        failures.append("installed_crontab_unreadable")
    if missing_entries:
        failures.append("installed_crontab_missing_entries")
    if env_mismatches:
        failures.append("installed_crontab_environment_mismatch")
    if root_residual_entries:
        failures.append("root_tradingagent_residual")
    if permission_blockers:
        failures.append("runtime_permission_blocked")

    return {
        "overall_status": "fail" if failures else "pass",
        "failures": failures,
        "installed_source": installed_source,
        "template_entry_count": len(shared_entries),
        "installed_entry_count": len(installed_entries),
        "missing_count": len(missing_entries),
        "missing_entries": missing_entries,
        "template_drift_count": len(template_drift) + len(template_extra),
        "template_drift": {
            "missing_from_root_crontab_txt": template_drift,
            "extra_in_root_crontab_txt": template_extra,
        },
        "environment_mismatch_count": len(env_mismatches),
        "environment_mismatches": env_mismatches,
        "root_residual_count": len(root_residual_entries),
        "root_residual_entries": root_residual_entries,
        "runtime_permission_blocker_count": len(permission_blockers),
        "runtime_permission_blockers": permission_blockers,
        "crontab_errors": {
            "marketgraph": details.get("marketgraph_error", ""),
            "root": details.get("root_error", ""),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check TradingAgent cron template and production coverage."
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = check_cron_coverage()
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report["overall_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
