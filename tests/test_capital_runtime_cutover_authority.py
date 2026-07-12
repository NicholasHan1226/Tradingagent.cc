from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

ACTIVE_RUNTIME_PATHS = (
    "Ashare/capital_plan.py",
    "Ashare/sim_executor.py",
    "shared/orchestrator.py",
    "shared/execution/local_sim_ledger.py",
    "CNFutures/sim_runner.py",
    "shared/runtime_test/ashare_preopen_dry_run.py",
    "shared/runtime_test/full_acceptance.py",
)

LEGACY_CALL_NAMES = {
    "load_master_capital_provider_state",
    "record_master_capital_realized_pnl",
    "release_master_capital",
    "reserve_master_capital",
    "verify_master_capital_reservation",
}

LEGACY_RUNTIME_MARKERS = {
    "TRADINGAGENT_MASTER_CAPITAL_ROOT",
    "single_shared_50000_cny_portfolio",
    "single_shared_portfolio",
    "protected_cash_reserve_cny",
    "ashare_notional_limit_cny",
    "cn_futures_margin_limit_cny",
    "MCAP:2",
}


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_active_runtime_does_not_import_or_call_legacy_master_capital() -> None:
    violations: list[str] = []
    for relative_path in ACTIVE_RUNTIME_PATHS:
        source = _source(relative_path)
        tree = ast.parse(source, filename=relative_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                if node.module in {"shared", "shared.capital"} and (
                    "capital" in imported or imported & LEGACY_CALL_NAMES
                ):
                    violations.append(
                        f"{relative_path}:{node.lineno}:legacy import {sorted(imported)}"
                    )
            if isinstance(node, ast.Attribute) and node.attr in LEGACY_CALL_NAMES:
                violations.append(
                    f"{relative_path}:{node.lineno}:legacy attribute {node.attr}"
                )
            if isinstance(node, ast.Name) and node.id in LEGACY_CALL_NAMES:
                violations.append(
                    f"{relative_path}:{node.lineno}:legacy name {node.id}"
                )
    assert violations == []


def test_active_runtime_has_no_old_shared_portfolio_markers() -> None:
    violations: list[str] = []
    for relative_path in ACTIVE_RUNTIME_PATHS:
        source = _source(relative_path)
        for marker in sorted(LEGACY_RUNTIME_MARKERS):
            if marker in source:
                violations.append(f"{relative_path}:{marker}")
    assert violations == []


def test_writable_legacy_shared_capital_implementation_is_retired() -> None:
    retired_paths = (
        "shared/capital/capital_policy.yaml",
        "shared/capital/policy.py",
        "shared/capital/master_ledger.py",
    )
    assert [path for path in retired_paths if (ROOT / path).exists()] == []


def test_legacy_cli_entry_is_physically_removed_after_runtime_migration() -> None:
    assert not (ROOT / "tools/master_capital_ops.py").exists()
