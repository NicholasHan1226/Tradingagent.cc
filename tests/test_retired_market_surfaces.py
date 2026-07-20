from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_MARKETS = {"ashare", "cnfutures", "crypto"}
RETIRED_DIRECTORIES = ("US", "PM", "HK")
RETIRED_WRAPPER_PREFIXES = ("job_us_", "job_pm_", "job_hk_")
RETIRED_WRAPPER_NAMES = {
    "job_hk_sim.sh",
    "job_pm_forward.sh",
    "job_pm_optimize.sh",
    "job_pm_promote.sh",
    "job_pm_report.sh",
    "job_pm_research_probability.sh",
    "job_pm_risk.sh",
    "job_pm_scan.sh",
    "job_pm_sim.sh",
    "job_us_hourly.sh",
    "job_us_postclose.sh",
    "job_us_premarket.sh",
    "job_us_scan.sh",
    "job_us_signal_review.sh",
    "job_us_sim.sh",
    "job_us_weekly.sh",
}
PHYSICALLY_RETIRED_PATHS = {
    "cron/evolution.sh",
    "shared/markets/market_rules.py",
    "shared/markets/style_config.py",
    "shared/markets/style_runner.py",
    "shared/markets/evolution_engine.py",
    "shared/markets/evolution_guard.py",
    "shared/markets/performance_tracker.py",
    "shared/portfolio/exit_manager.py",
    "shared/wrappers/job_equity_snapshots.sh",
    "shared/notify/pm/pm_report.jsonl",
    "shared/risk/pm/pm_risk_report.jsonl",
    "shared/signals/pm/pm_forward_signals.jsonl",
    "shared/strategies/pm/pm_optimize_params.json",
    "Crypto/tools/manifest.csv",
}
CURRENT_AUTHORITY_DIRECTORIES = (
    "shared/execution",
    "shared/portfolio",
    "shared/risk",
    "shared/markets",
    "shared/runtime",
    "shared/capital",
    "shared/universe",
    "shared/models",
    "shared/llm",
    "shared/strategy_router",
    "Ashare",
    "CNFutures",
    "Crypto",
)
LEGACY_READER_CONSUMER_ALLOWLIST = {
    "Ashare/market_phases/closing_auction.py",
    "Ashare/market_phases/opening_auction.py",
    "Ashare/research_evidence.py",
    "shared/data/__init__.py",
    "shared/research/multi_perspective.py",
    "shared/review/benchmark.py",
    "shared/review/daily_review.py",
    "shared/runtime_test/ashare_forward_label_ops.py",
    "shared/runtime_test/cn_futures_sample_ops.py",
    "shared/runtime_test/market_health.py",
    "shared/runtime_test/opening_acceptance.py",
    "shared/screening/candidate_pool.py",
    "shared/screening/condition_generator.py",
    "shared/screening/condition_monitor.py",
    "shared/screening/fundamental_analyzer.py",
    "shared/screening/patrol.py",
    "shared/screening/six_dimension_scorer.py",
    "shared/screening/universe_filter.py",
}


def _imports_module(path: Path, module_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module_name for alias in node.names):
                return True
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        imported = node.module or ""
        if imported == module_name:
            return True
        if node.level == 1 and path.parent == ROOT / "shared/data":
            if f"shared.data.{imported}" == module_name:
                return True
    return False


def _retired_scalar_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value.strip().lower() in {"us", "pm", "hk"} else set()
    if isinstance(value, dict):
        values: set[str] = set()
        for key, item in value.items():
            values.update(_retired_scalar_values(key))
            values.update(_retired_scalar_values(item))
        return values
    if isinstance(value, list):
        values: set[str] = set()
        for item in value:
            values.update(_retired_scalar_values(item))
        return values
    return set()


def test_market_lane_registry_is_exactly_the_three_owned_markets() -> None:
    payload = yaml.safe_load(
        (ROOT / "shared/governance/market_lanes.yaml").read_text(encoding="utf-8")
    )

    assert {lane["lane_id"] for lane in payload["lanes"]} == ACTIVE_MARKETS


def test_absent_desktop_investment_runtime_is_not_kept_as_active_legacy() -> None:
    inventory = (ROOT / "shared/governance/legacy_inventory.yaml").read_text(
        encoding="utf-8"
    )
    assert "historical_desktop_investment_runtime" not in inventory
    assert "~/Desktop/Investment" not in inventory


def test_retired_market_packages_and_dedicated_wrappers_are_absent() -> None:
    for directory in RETIRED_DIRECTORIES:
        assert not (ROOT / directory).exists(), directory

    remaining = {
        path.name
        for path in (ROOT / "shared/wrappers").glob("job_*.sh")
        if path.name.startswith(RETIRED_WRAPPER_PREFIXES)
    }
    assert remaining == set()


def test_retired_shared_market_facades_and_runtime_artifacts_are_absent() -> None:
    violations = sorted(
        path for path in PHYSICALLY_RETIRED_PATHS if (ROOT / path).exists()
    )
    assert violations == []

    crypto_styles = ROOT / "Crypto/styles"
    assert not crypto_styles.exists() or not any(crypto_styles.iterdir())


def test_cron_merger_strips_every_retired_market_wrapper_from_installed_state() -> None:
    merger = (ROOT / "tools/merge_tradingagent_crontab.py").read_text(encoding="utf-8")

    assert all(f'"/{name}"' in merger for name in RETIRED_WRAPPER_NAMES)


def test_active_cron_and_shared_runtime_do_not_route_retired_markets() -> None:
    sources = [
        ROOT / "shared/crontab.txt",
        ROOT / "crontab.txt",
        ROOT / "shared/wrappers/run_sim.py",
        ROOT / "shared/wrappers/tradings_cron_entry.py",
    ]
    forbidden = re.compile(
        r"(?:job_(?:us|pm|hk)_|\bmarket\s*==\s*['\"](?:US|PM|HK)['\"]|"
        r"\bfrom\s+(?:US|PM|HK)\b|\bimport\s+(?:US|PM|HK)\b)"
    )

    violations = {
        path.relative_to(ROOT).as_posix(): sorted(set(forbidden.findall(text)))
        for path in sources
        if path.exists()
        for text in [path.read_text(encoding="utf-8")]
        if forbidden.search(text)
    }
    assert violations == {}


def test_front_runtime_market_identity_excludes_retired_markets() -> None:
    runtime_files = tuple((ROOT / "front/src").rglob("*.ts")) + tuple(
        (ROOT / "front/src").rglob("*.tsx")
    )
    forbidden_literal = re.compile(r"(['\"])(?:US|PM|HK)\1")
    violations = {
        path.relative_to(ROOT).as_posix(): sorted(
            {match.group(0) for match in forbidden_literal.finditer(text)}
        )
        for path in runtime_files
        for text in [path.read_text(encoding="utf-8")]
        if forbidden_literal.search(text)
    }
    assert violations == {}


def test_front_runtime_crypto_identity_is_native_usdt_only() -> None:
    runtime_files = tuple((ROOT / "front/src").rglob("*.ts")) + tuple(
        (ROOT / "front/src").rglob("*.tsx")
    )
    forbidden = re.compile(r"-USD(?!T)|\bPERP\b", re.I)
    violations = {
        path.relative_to(ROOT).as_posix(): sorted(
            {match.group(0) for match in forbidden.finditer(text)}
        )
        for path in runtime_files
        if ".test." not in path.name
        for text in [path.read_text(encoding="utf-8")]
        if forbidden.search(text)
    }
    assert violations == {}


def test_current_authority_python_surfaces_have_no_retired_market_identity() -> None:
    violations: dict[str, list[str]] = {}
    for directory in CURRENT_AUTHORITY_DIRECTORIES:
        for path in sorted((ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            retired = sorted(
                {
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.strip().lower() in {"us", "pm", "hk"}
                }
            )
            if retired:
                violations[path.relative_to(ROOT).as_posix()] = retired
    assert violations == {}


def test_current_authority_configs_have_no_retired_market_identity() -> None:
    violations: dict[str, list[str]] = {}
    for directory in CURRENT_AUTHORITY_DIRECTORIES:
        for path in sorted((ROOT / directory).rglob("*")):
            if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
                continue
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            retired = sorted(_retired_scalar_values(payload))
            if retired:
                violations[path.relative_to(ROOT).as_posix()] = retired
    assert violations == {}


def test_legacy_data_reader_consumer_inventory_can_only_shrink() -> None:
    candidates = (
        tuple((ROOT / "Ashare").rglob("*.py"))
        + tuple((ROOT / "CNFutures").rglob("*.py"))
        + tuple((ROOT / "Crypto").rglob("*.py"))
        + tuple((ROOT / "shared").rglob("*.py"))
    )
    consumers = {
        path.relative_to(ROOT).as_posix()
        for path in candidates
        if path.name != "reader" + ".py" and _imports_module(path, "shared.data.reader")
    }
    assert consumers == LEGACY_READER_CONSUMER_ALLOWLIST, (
        "the explicit time-boxed reader inventory must match current consumers; "
        "new consumers are forbidden and retired consumers must be removed from "
        "the allowlist in the same change: "
        f"added={sorted(consumers - LEGACY_READER_CONSUMER_ALLOWLIST)}, "
        f"retired={sorted(LEGACY_READER_CONSUMER_ALLOWLIST - consumers)}"
    )

    direct_api_consumers = {
        path.relative_to(ROOT).as_posix()
        for path in candidates
        if _imports_module(path, "shared.data.shared_signals_api")
    }
    legacy_api_boundary = (Path("shared/data") / ("reader" + ".py")).as_posix()
    assert direct_api_consumers == {legacy_api_boundary}, (
        "the provider-specific legacy API may only remain behind the old reader: "
        f"{sorted(direct_api_consumers)}"
    )


def test_current_code_cannot_import_legacy_reader_exports_from_shared_data() -> None:
    legacy_exports = {
        "MarketGraphCSVReader",
        "Shared" + "SignalsReader",
        "TradingagentDataReader",
    }
    violations: dict[str, list[str]] = {}
    candidates = (
        tuple((ROOT / "Ashare").rglob("*.py"))
        + tuple((ROOT / "CNFutures").rglob("*.py"))
        + tuple((ROOT / "Crypto").rglob("*.py"))
        + tuple((ROOT / "shared").rglob("*.py"))
    )
    for path in candidates:
        if path == ROOT / "shared/data/__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = sorted(
            {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == "shared.data"
                for alias in node.names
                if alias.name in legacy_exports
            }
        )
        if imported:
            violations[path.relative_to(ROOT).as_posix()] = imported
    assert violations == {}


def test_retired_style_artifacts_cannot_feed_current_readiness_or_pnl() -> None:
    current_readers = (
        ROOT / "front/src/server/tradingAgentSnapshot.ts",
        ROOT / "shared/runtime_test/self_evolution_health.py",
        ROOT / "shared/review/metrics_dashboard.py",
    )
    violations = {
        path.relative_to(ROOT).as_posix(): artifact
        for path in current_readers
        for artifact in ("style_performance.jsonl", "style_comparison.json")
        if artifact in path.read_text(encoding="utf-8")
    }
    assert violations == {}

    current_writers = (
        ROOT / "CNFutures/review.py",
        ROOT / "cron/health_check.sh",
    )
    writer_violations = {
        path.relative_to(ROOT).as_posix(): artifact
        for path in current_writers
        for artifact in ("style_performance.jsonl", "style_comparison.json")
        if artifact in path.read_text(encoding="utf-8")
    }
    assert writer_violations == {}

    source_refs = (ROOT / "front/src/api/tradingAgentReadModel.ts").read_text(
        encoding="utf-8"
    )
    assert (
        "legacy frozen forensic only: shared/review/*/style_performance.jsonl"
        in source_refs
    )
    assert (
        "legacy frozen forensic only: shared/review/*/style_comparison.json"
        in source_refs
    )


def test_retired_health_probe_has_no_implicit_marketgraph_localhost() -> None:
    source = (ROOT / "cron/health_check.sh").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8080" not in source
    assert "MARKETGRAPH_API_URL:-}" in source
    assert "explicit_marketgraph_health_url_required" in source


def test_retired_prediction_market_route_is_absent() -> None:
    client = (ROOT / "shared/data/marketgraph_api.py").read_text(encoding="utf-8")
    assert "/pm/" not in client
    assert "get_pm_research_probabilities" not in client

    workbench = (ROOT / "front/src/lib/workbenchViewModel.ts").read_text(
        encoding="utf-8"
    )
    assert "pm_waiting_for_market_data" not in workbench
