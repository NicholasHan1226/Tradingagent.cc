#!/usr/bin/env python3
"""Read-only TradingAgent acceptance runner.

This is a thin wrapper around existing checks. It does not send emails, create
orders, mutate ledgers, or install cron entries.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

KEY_TESTS = [
    "tests/test_market_health.py",
    "tests/test_ashare_opening_validator.py",
    "tests/test_ashare_no_trade_summary.py",
    "tests/test_ashare_preopen_dry_run.py",
    "tests/test_run_sim_wrapper.py",
    "tests/test_pnl_summary.py",
    "tests/test_equity_snapshots.py",
    "tests/test_real_trading_gate.py",
    "tests/test_real_money_boundary.py",
    "tests/test_capital_layer_isolation.py",
    "tests/test_cron_coverage.py",
    "tests/test_opportunity_funnel_cron.py",
    "tests/test_sharedsignals_evidence_contract.py",
]


@dataclass
class AcceptanceCheck:
    name: str
    status: str
    duration_seconds: float
    summary: str
    command: list[str]
    returncode: int
    tail: str = ""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) if not existing else f"{ROOT}{os.pathsep}{existing}"
    return env


def _tail(text: str, limit: int = 2400) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _status_from_json_output(name: str, text: str, returncode: int) -> tuple[str, str]:
    if returncode != 0:
        return "fail", f"exit={returncode}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return "pass", "ok"
    if isinstance(parsed, dict):
        overall = str(parsed.get("overall_status") or "").lower()
        if overall in {"pass", "warn", "fail"}:
            return overall, overall
        if name == "ashare_no_trade_summary" and parsed.get("evidence_status") == "incomplete":
            gaps = parsed.get("evidence_gaps") or []
            return "warn", f"incomplete evidence: {','.join(map(str, gaps))}"
        if name == "ashare_no_trade_summary":
            trade_source = parsed.get("trade_source_check") if isinstance(parsed.get("trade_source_check"), dict) else {}
            if trade_source.get("status") == "incomplete":
                return "warn", "incomplete trade source evidence"
    return "pass", "ok"


def _run(name: str, command: list[str], *, cwd: Path = ROOT, timeout: int = 180) -> AcceptanceCheck:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        status, summary = _status_from_json_output(name, output, result.returncode)
        return AcceptanceCheck(
            name=name,
            status=status,
            duration_seconds=round(time.monotonic() - started, 2),
            summary=summary,
            command=command,
            returncode=result.returncode,
            tail=_tail(output),
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in (exc.stdout or "", exc.stderr or "") if part)
        return AcceptanceCheck(
            name=name,
            status="fail",
            duration_seconds=round(time.monotonic() - started, 2),
            summary=f"timeout>{timeout}s",
            command=command,
            returncode=124,
            tail=_tail(output),
        )


def _profiles(args: argparse.Namespace) -> list[tuple[str, list[str], Path, int]]:
    python = sys.executable
    profiles: list[tuple[str, list[str], Path, int]] = []
    if args.profile in {"quick", "all"}:
        profiles.append(("key_pytest", [python, "-m", "pytest", "-q", *KEY_TESTS], ROOT, args.test_timeout))
    if args.profile in {"prod", "all"}:
        profiles.append(("cron_coverage", [python, "-m", "shared.runtime_test.cron_coverage", "--pretty"], ROOT, 30))
        profiles.append(("sharedsignals_evidence_contract", [python, "-m", "shared.runtime_test.sharedsignals_evidence_contract", "--pretty"], ROOT, 30))
        profiles.append(("sim_market_health", [python, "-m", "shared.runtime_test.market_health", "--market", "sim", "--pretty"], ROOT, 120))
        profiles.append(("ashare_no_trade_summary", [python, "-m", "shared.runtime_test.ashare_no_trade_summary", "--pretty"], ROOT, 60))
        profiles.append(("self_evolution_health", [python, "-m", "shared.runtime_test.self_evolution_health", "--pretty"], ROOT, 60))
        profiles.append(("opening_acceptance", [python, "-m", "shared.runtime_test.opening_acceptance", "--json", "--pretty", "--send-on", "never"], ROOT, 120))
    if args.profile in {"full", "all"}:
        profiles.append(("full_pytest", [python, "-m", "pytest", "-q"], ROOT, args.test_timeout))
    if args.profile in {"front", "all"}:
        front = ROOT / "front"
        profiles.append(("front_build", ["npm", "run", "build"], front, args.front_timeout))
        profiles.append(("front_build_api", ["npm", "run", "build:api"], front, args.front_timeout))
        if args.front_tests:
            profiles.append(("front_lint", ["npm", "run", "lint"], front, args.front_timeout))
            profiles.append(("front_test", ["npm", "test"], front, args.front_timeout))
    if args.profile == "cn_futures":
        profiles.append(("cn_futures_live_check", [python, "-m", "shared.runtime_test.cn_futures_live_check", "--json", "--pretty"], ROOT, 120))
    return profiles


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    checks = [_run(name, command, cwd=cwd, timeout=timeout) for name, command, cwd, timeout in _profiles(args)]
    overall = "fail" if any(check.status == "fail" for check in checks) else ("warn" if any(check.status == "warn" for check in checks) else "pass")
    return {
        "overall_status": overall,
        "profile": args.profile,
        "check_count": len(checks),
        "checks": [asdict(check) for check in checks],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run TradingAgent read-only acceptance checks.")
    parser.add_argument(
        "--profile",
        choices=["quick", "full", "front", "prod", "all", "cn_futures"],
        default="quick",
        help="quick=key tests; prod=runtime checks; all=quick+prod+full pytest+front build.",
    )
    parser.add_argument("--front-tests", action="store_true", help="When profile includes front, also run lint and vitest.")
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--front-timeout", type=int, default=180)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_acceptance(args)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report["overall_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
