#!/usr/bin/env python3
"""Read-only TradingAgent acceptance runner.

This is a thin wrapper around existing checks. It does not send emails, create
orders, mutate ledgers, or install cron entries.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

KEY_TESTS = [
    "tests/test_sharedsignals_v1.py",
    "tests/test_tradingdatas_query_pagination.py",
    "tests/test_tradingdatas_bearer_auth.py",
    "tests/test_sharedsignals_v1_runtime_gate.py",
    "tests/test_sharedsignals_v1_integration_probe.py",
    "tests/test_architecture_contract_guards.py",
    "tests/test_legacy_direct_entry_retirement.py",
    "tests/test_market_lane_governance.py",
    "tests/test_mini_hermes_retirement.py",
    "tests/test_ashare_sim.py",
    "tests/test_crypto_sim.py",
    "tests/test_cn_futures_sim.py",
    "tests/test_real_trading_gate.py",
    "tests/test_real_money_boundary.py",
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


def _env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) if not existing else f"{ROOT}{os.pathsep}{existing}"
    # run_acceptance rejects an explicitly enabled live environment before any
    # subprocess starts.  Pin the accepted child environment to simulation so
    # an unset variable cannot be interpreted differently by individual tools.
    env["REAL_TRADING_ENABLED"] = "false"
    for retired_name in (
        "SHAREDSIGNALS_API_URL",
        "SHAREDSIGNALS_CATALOG_VERSION",
        "SHAREDSIGNALS_ACCESS_POLICY_ID",
        "SHAREDSIGNALS_MARKET_PULSE_DATASET_IDS_JSON",
        "SHAREDSIGNALS_SCHEMA_MAJOR",
        "SHAREDSIGNALS_RUNTIME_TRANSPORT",
        "SHARED_SIGNALS_DB",
        "TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE",
    ):
        env.pop(retired_name, None)
    env.update(overrides or {})
    return env


def _tail(text: str, limit: int = 2400) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _status_from_json_output(name: str, text: str, returncode: int) -> tuple[str, str]:
    strict_json_checks = {
        "ashare_capital",
        "cn_futures_capital",
        "ashare_preopen",
        "ashare_opening",
        "ashare_forward_label_ops",
        "cn_futures_session_acceptance",
    }
    if returncode != 0:
        return "fail", f"exit={returncode}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        if name in strict_json_checks:
            return "fail", "invalid JSON acceptance output"
        return "pass", "ok"
    if isinstance(parsed, dict):
        # Handle market_capital_ops status output (ashare_capital / cn_futures_capital)
        if name in {"ashare_capital", "cn_futures_capital"}:
            mc_status = str(parsed.get("status") or "").lower()
            if mc_status == "market_capital_available":
                # Additional freshness/reconciled check
                if parsed.get("fresh") is True and parsed.get("reconciled") is True:
                    return "pass", "market_capital_available"
                return "warn", "market_capital_available_not_fresh"
            if mc_status == "market_capital_unavailable":
                return "fail", "market_capital_unavailable"
            return (
                "fail",
                f"unrecognized market capital status: {mc_status or 'missing'}",
            )

        overall = str(
            parsed.get("overall_status") or parsed.get("status") or ""
        ).lower()
        if name == "ashare_forward_label_ops" and overall == "pass":
            counts = (
                parsed.get("counts") if isinstance(parsed.get("counts"), dict) else {}
            )
            try:
                prediction_count = int(counts.get("prediction_count") or 0)
                ready_labels = int(counts.get("ready_labels") or 0)
                pending_labels = int(counts.get("pending_not_due") or 0)
            except (TypeError, ValueError):
                return "fail", "invalid A-share forward-label evidence counts"
            if prediction_count <= 0:
                return "fail", "no A-share prediction evidence for requested trade date"
            if ready_labels <= 0 and pending_labels > 0:
                return (
                    "warn",
                    "prediction evidence present but forward labels are not due",
                )
            if ready_labels <= 0:
                return (
                    "fail",
                    "prediction evidence has no ready or pending forward labels",
                )
        if overall in {"pass", "warn", "fail"}:
            return overall, overall
        if overall in {"blocked", "critical", "error"}:
            return "fail", overall
        if overall == "degraded":
            return "warn", overall
        if (
            name == "ashare_no_trade_summary"
            and parsed.get("evidence_status") == "incomplete"
        ):
            gaps = parsed.get("evidence_gaps") or []
            return "warn", f"incomplete evidence: {','.join(map(str, gaps))}"
        if name == "ashare_no_trade_summary":
            trade_source = (
                parsed.get("trade_source_check")
                if isinstance(parsed.get("trade_source_check"), dict)
                else {}
            )
            if trade_source.get("status") == "incomplete":
                return "warn", "incomplete trade source evidence"
        if name in strict_json_checks:
            return "fail", f"unrecognized acceptance status: {overall or 'missing'}"
    return "pass", "ok"


def _run(
    name: str,
    command: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 180,
    env_overrides: dict[str, str] | None = None,
) -> AcceptanceCheck:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=_env(env_overrides),
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
        output = "\n".join(
            part for part in (exc.stdout or "", exc.stderr or "") if part
        )
        return AcceptanceCheck(
            name=name,
            status="fail",
            duration_seconds=round(time.monotonic() - started, 2),
            summary=f"timeout>{timeout}s",
            command=command,
            returncode=124,
            tail=_tail(output),
        )


def _synthetic_failure(name: str, summary: str) -> AcceptanceCheck:
    return AcceptanceCheck(
        name=name,
        status="fail",
        duration_seconds=0.0,
        summary=summary,
        command=[],
        returncode=2,
    )


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalized_trade_date(value: object) -> str:
    normalized = "".join(
        character for character in str(value or "") if character.isdigit()
    )
    if len(normalized) != 8:
        return ""
    try:
        datetime.strptime(normalized, "%Y%m%d")
    except ValueError:
        return ""
    return normalized


def _capital_growth_input_gaps(args: argparse.Namespace) -> list[str]:
    gaps: list[str] = []
    if not _normalized_trade_date(args.trade_date):
        gaps.append("trade_date")

    journal = args.ashare_journal
    if journal is None:
        gaps.append("ashare_journal")
    elif not Path(journal).expanduser().is_file():
        gaps.append("ashare_journal_not_found")
    if not str(args.as_of or "").strip():
        gaps.append("as_of")

    records = args.cn_futures_records
    if records is None:
        gaps.append("cn_futures_records")
    elif not Path(records).expanduser().is_file():
        gaps.append("cn_futures_records_not_found")
    if not str(args.cn_futures_sessions or "").strip():
        gaps.append("cn_futures_sessions")
    return gaps


def _capital_growth_profiles(
    args: argparse.Namespace,
) -> list[tuple[str, list[str], Path, int]]:
    trade_date = _normalized_trade_date(args.trade_date)
    if not trade_date:
        return []

    python = sys.executable
    iso_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

    # Dual independent market capital checks via market_capital_ops.py.
    # Never summed — ashare and cn_futures are separate accounts.
    ashare_capital_command = [
        python,
        str(ROOT / "tools/market_capital_ops.py"),
        "status",
        "--market",
        "ashare",
        "--trade-date",
        trade_date,
    ]
    cn_futures_capital_command = [
        python,
        str(ROOT / "tools/market_capital_ops.py"),
        "status",
        "--market",
        "cn_futures",
        "--trade-date",
        trade_date,
    ]
    if args.ashare_capital_root is not None:
        ashare_capital_command.extend(
            ["--root", str(Path(args.ashare_capital_root).expanduser())]
        )
    if args.cn_futures_capital_root is not None:
        cn_futures_capital_command.extend(
            ["--root", str(Path(args.cn_futures_capital_root).expanduser())]
        )

    profiles: list[tuple[str, list[str], Path, int]] = [
        ("ashare_capital", ashare_capital_command, ROOT, 30),
        ("cn_futures_capital", cn_futures_capital_command, ROOT, 30),
        (
            "ashare_preopen",
            [
                python,
                "-m",
                "shared.runtime_test.ashare_preopen_dry_run",
                "--now",
                f"{iso_date}T09:20:00+08:00",
                "--json",
                "--pretty",
                "--send-on",
                "never",
                "--no-write",
            ],
            ROOT,
            180,
        ),
        (
            "ashare_opening",
            [
                python,
                "-m",
                "shared.runtime_test.ashare_opening_validator",
                "--now",
                f"{iso_date}T09:35:00+08:00",
                "--pretty",
            ],
            ROOT,
            120,
        ),
    ]

    journal = Path(args.ashare_journal).expanduser() if args.ashare_journal else None
    if journal is not None and journal.is_file() and str(args.as_of or "").strip():
        profiles.append(
            (
                "ashare_forward_label_ops",
                [
                    python,
                    "-m",
                    "shared.runtime_test.ashare_forward_label_ops",
                    "--journal-path",
                    str(journal),
                    "--trade-date",
                    trade_date,
                    "--as-of",
                    str(args.as_of),
                    "--pretty",
                ],
                ROOT,
                180,
            )
        )

    records = (
        Path(args.cn_futures_records).expanduser() if args.cn_futures_records else None
    )
    sessions = str(args.cn_futures_sessions or "").strip()
    if records is not None and records.is_file() and sessions:
        profiles.append(
            (
                "cn_futures_session_acceptance",
                [
                    python,
                    "-m",
                    "shared.runtime_test.cn_futures_session_acceptance",
                    "--input",
                    str(records),
                    "--trade-date",
                    trade_date,
                    "--sessions",
                    sessions,
                    "--pretty",
                ],
                ROOT,
                60,
            )
        )
    return profiles


def _profiles(args: argparse.Namespace) -> list[tuple[str, list[str], Path, int]]:
    python = sys.executable
    profiles: list[tuple[str, list[str], Path, int]] = []
    if args.profile in {"quick", "all"}:
        profiles.append(
            (
                "key_pytest",
                [python, "-m", "pytest", "-q", *KEY_TESTS],
                ROOT,
                args.test_timeout,
            )
        )
    if args.profile in {"prod", "all"}:
        profiles.append(
            (
                "cron_coverage",
                [python, "-m", "shared.runtime_test.cron_coverage", "--pretty"],
                ROOT,
                30,
            )
        )
        profiles.append(
            (
                "tradingdatas_v1_runtime_gate",
                [
                    python,
                    "-m",
                    "shared.runtime_test.sharedsignals_v1_gate",
                    "--market",
                    "ashare",
                    "--json",
                ],
                ROOT,
                30,
            )
        )
        profiles.append(
            (
                "ashare_no_trade_summary",
                [
                    python,
                    "-m",
                    "shared.runtime_test.ashare_no_trade_summary",
                    "--pretty",
                ],
                ROOT,
                60,
            )
        )
        profiles.append(
            (
                "self_evolution_health",
                [python, "-m", "shared.runtime_test.self_evolution_health", "--pretty"],
                ROOT,
                60,
            )
        )
    if args.profile in {"full", "all"}:
        profiles.append(
            ("full_pytest", [python, "-m", "pytest", "-q"], ROOT, args.test_timeout)
        )
    if args.profile in {"front", "all"}:
        front = ROOT / "front"
        profiles.append(
            ("front_build", ["npm", "run", "build"], front, args.front_timeout)
        )
        profiles.append(
            ("front_build_api", ["npm", "run", "build:api"], front, args.front_timeout)
        )
        if args.front_tests:
            profiles.append(
                ("front_lint", ["npm", "run", "lint"], front, args.front_timeout)
            )
            profiles.append(("front_test", ["npm", "test"], front, args.front_timeout))
    if args.profile == "cn_futures":
        profiles.append(
            (
                "cn_futures_contract_tests",
                [
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_cn_futures_sim.py",
                    "tests/test_cn_futures_sim_runner.py",
                    "tests/test_cn_futures_sim_executor_evidence.py",
                    "tests/test_cn_futures_order_events.py",
                    "tests/test_market_authority_binding.py",
                ],
                ROOT,
                args.test_timeout,
            )
        )
    if args.profile == "capital_growth":
        profiles.extend(_capital_growth_profiles(args))
    return profiles


def _run_source_read_only_forward_labels(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env_overrides: dict[str, str] | None = None,
) -> AcceptanceCheck:
    """Run label materialization against an isolated copy of the source journal."""

    path_index = command.index("--journal-path") + 1
    source = Path(command[path_index])
    with tempfile.TemporaryDirectory(
        prefix=".full-acceptance-", dir=str(ROOT / "tests")
    ) as temporary:
        staged = Path(temporary) / source.name
        shutil.copyfile(source, staged)
        staged_command = list(command)
        staged_command[path_index] = str(staged)
        check = _run(
            "ashare_forward_label_ops",
            staged_command,
            cwd=cwd,
            timeout=timeout,
            env_overrides=env_overrides,
        )
    check.summary = f"{check.summary}; source journal preserved via isolated copy"
    return check


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    live_environment = _truthy(os.environ.get("REAL_TRADING_ENABLED"))
    if live_environment:
        checks = [
            _synthetic_failure(
                "sim_only_safety_gate",
                "REAL_TRADING_ENABLED is true; no acceptance subprocess was started",
            )
        ]
    else:
        checks = []
        env_overrides: dict[str, str] = {}
        if args.profile == "capital_growth" and args.ashare_capital_root is not None:
            env_overrides["TRADINGAGENT_ASHARE_CAPITAL_ROOT"] = str(
                Path(args.ashare_capital_root).expanduser()
            )
        if (
            args.profile == "capital_growth"
            and args.cn_futures_capital_root is not None
        ):
            env_overrides["TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT"] = str(
                Path(args.cn_futures_capital_root).expanduser()
            )
        if args.profile == "capital_growth":
            gaps = _capital_growth_input_gaps(args)
            if gaps:
                checks.append(
                    _synthetic_failure(
                        "capital_growth_inputs",
                        "missing or invalid runtime evidence: " + ",".join(gaps),
                    )
                )
        for name, command, cwd, timeout in _profiles(args):
            if name == "ashare_forward_label_ops":
                checks.append(
                    _run_source_read_only_forward_labels(
                        command,
                        cwd=cwd,
                        timeout=timeout,
                        env_overrides=env_overrides,
                    )
                )
            else:
                checks.append(
                    _run(
                        name,
                        command,
                        cwd=cwd,
                        timeout=timeout,
                        env_overrides=env_overrides,
                    )
                )
    overall = (
        "fail"
        if any(check.status == "fail" for check in checks)
        else ("warn" if any(check.status == "warn" for check in checks) else "pass")
    )
    return {
        "overall_status": overall,
        "profile": args.profile,
        "execution_boundary": "blocked_live_environment"
        if live_environment
        else "sim_only_acceptance",
        "real_trading_enabled": live_environment,
        "check_count": len(checks),
        "checks": [asdict(check) for check in checks],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run TradingAgent read-only acceptance checks."
    )
    parser.add_argument(
        "--profile",
        choices=[
            "quick",
            "full",
            "front",
            "prod",
            "all",
            "cn_futures",
            "capital_growth",
        ],
        default="quick",
        help=(
            "quick=current fixture/contract tests; prod=explicit TradingDatas V1 "
            "runtime gate (expected to fail closed before fresh handoff); "
            "cn_futures=market-specific simulation contract tests; "
            "capital_growth=50k sample-loop evidence; all includes historical tests."
        ),
    )
    parser.add_argument(
        "--front-tests",
        action="store_true",
        help="When profile includes front, also run lint and vitest.",
    )
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--front-timeout", type=int, default=180)
    parser.add_argument(
        "--trade-date",
        default=None,
        help="Explicit YYYYMMDD evidence date for capital_growth.",
    )
    parser.add_argument("--ashare-capital-root", type=Path, default=None)
    parser.add_argument("--cn-futures-capital-root", type=Path, default=None)
    parser.add_argument("--ashare-journal", type=Path, default=None)
    parser.add_argument(
        "--as-of", default=None, help="Explicit forward-label evidence cutoff."
    )
    parser.add_argument("--cn-futures-records", type=Path, default=None)
    parser.add_argument(
        "--cn-futures-sessions",
        default=None,
        help="Comma-separated valid CNFutures sessions.",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_acceptance(args)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report["overall_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
