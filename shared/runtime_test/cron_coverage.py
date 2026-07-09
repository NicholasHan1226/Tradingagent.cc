#!/usr/bin/env python3
"""Read-only cron coverage guard for TradingAgent production entries."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TRADING_PREFIX = "/opt/investment/tradingagent/"
ROOT_UID = 0
ROOT_GID = 0


def _run_crontab(command: list[str]) -> tuple[str, str]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
    except Exception as exc:  # noqa: BLE001
        return "", f"{' '.join(command)}: {exc.__class__.__name__}: {exc}"
    if result.returncode == 0:
        return result.stdout, ""
    return "", f"{' '.join(command)}: {result.stderr.strip() or result.stdout.strip()}"


def _read_installed_crontabs() -> dict[str, str]:
    marketgraph_text, marketgraph_error = _run_crontab(["crontab", "-u", "marketgraph", "-l"])
    root_text, root_error = _run_crontab(["crontab", "-l"])
    return {
        "marketgraph_text": marketgraph_text,
        "marketgraph_error": marketgraph_error,
        "root_text": root_text,
        "root_error": root_error,
    }


def _is_cron_schedule_line(line: str) -> bool:
    if not line or line.startswith("#"):
        return False
    if "=" in line and not line.split()[0].startswith(("@", "*")) and not line.split()[0][0].isdigit():
        return False
    return TRADING_PREFIX in line


def _normalize_entry(line: str) -> str:
    line = line.strip()
    line = re.sub(r"\s+>>\s+\S+.*$", "", line)
    line = re.sub(r"\s+", " ", line)
    return line


def tradingagent_entries(text: str) -> list[str]:
    return [_normalize_entry(line) for line in text.splitlines() if _is_cron_schedule_line(line.strip())]


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
        ROOT / "shared/review/ashare/forward_validation_latest.json",
        ROOT / "shared/review/ashare/forward_validation.jsonl",
        ROOT / "shared/review/ashare/portfolio_evolution_latest.json",
        ROOT / "shared/review/ashare/portfolio_evolution_log.jsonl",
        ROOT / "shared/review/ashare/tier_experiments_latest.json",
        ROOT / "shared/review/opportunities",
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
    template_drift = [entry for entry in shared_entries if entry not in set(root_entries)]
    template_extra = [entry for entry in root_entries if entry not in set(shared_entries)]

    installed_text = details.get("marketgraph_text") or details.get("root_text") or ""
    installed_source = "marketgraph" if details.get("marketgraph_text") else ("root" if details.get("root_text") else "")
    installed_entries = set(tradingagent_entries(installed_text))
    missing_entries = [entry for entry in shared_entries if entry not in installed_entries]

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
    parser = argparse.ArgumentParser(description="Check TradingAgent cron template and production coverage.")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = check_cron_coverage()
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report["overall_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
