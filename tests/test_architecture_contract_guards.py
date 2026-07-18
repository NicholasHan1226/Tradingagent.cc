from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

from shared.governance.contracts import (
    LLM_EVIDENCE_CONTRACT_ID,
    SHARED_SIGNALS_QUERY_CONTRACT_ID,
    UNIVERSE_SCOPE_CONTRACT_ID,
    load_legacy_inventory,
    load_system_state_matrix,
)


ROOT = Path(__file__).resolve().parents[1]


TRADING_AUTHORITY_DIRECTORIES = (
    "shared/runtime",
    "shared/portfolio",
    "shared/capital",
    "shared/risk",
    "shared/execution",
)


TRADING_AUTHORITY_MODULE_PREFIXES = (
    "shared.runtime",
    "shared.portfolio",
    "shared.capital",
    "shared.risk",
    "shared.execution",
)


CURRENT_V1_SS_CONSUMER_PATHS = (
    "shared/data/sharedsignals_v1.py",
    "shared/data/evidence_gate.py",
    "shared/runtime_test/sharedsignals_v1_gate.py",
    "shared/runtime_test/sharedsignals_v1_integration_probe.py",
    "shared/runtime/stage_ports.py",
    "shared/runtime/composition.py",
    "shared/runtime/day_loop.py",
    "front/src/server/sharedSignalsMarketPulse.ts",
    "front/src/server/tradingAgentSnapshot.ts",
    "front/src/api/tradingAgentReadModel.ts",
)


INTEGRATION_READINESS_PREFLIGHT_PATHS = (
    "shared/runtime_test/integration_readiness_gate.py",
    "shared/runtime_test/integration_readiness_profile.py",
)


FORBIDDEN_CURRENT_V1_SS_ROUTES = (
    "/source_status",
    "/cache/status",
    "/capabilities",
    "/tushare",
    "/realtime_5min",
    "/market_data",
    "/capital_flow",
    "/pm_prices",
    "/crypto?",
    "/events",
)


def _python_dependency_names(path: Path) -> set[str]:
    """Return static and dynamic-import dependency names for one Python module."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative_module = path.relative_to(ROOT).with_suffix("")
    package_parts = list(relative_module.parts[:-1])
    dependencies: set[str] = set()
    importlib_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "importlib"
    }
    import_module_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "importlib"
        for alias in node.names
        if alias.name == "import_module"
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parent_parts = package_parts[:]
                if node.level > 1:
                    parent_parts = parent_parts[: -(node.level - 1)]
                module_parts = parent_parts
                if node.module:
                    module_parts = [*module_parts, *node.module.split(".")]
                module = ".".join(module_parts)
                dependencies.add(module)
                dependencies.update(
                    f"{module}.{alias.name}" for alias in node.names if module
                )
            elif node.module:
                dependencies.add(node.module)
                dependencies.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        dynamic_name = node.args[0]
        if not isinstance(dynamic_name, ast.Constant) or not isinstance(
            dynamic_name.value, str
        ):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in {
            "__import__",
            *import_module_names,
        }:
            dependencies.add(dynamic_name.value)
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in importlib_names
        ):
            dependencies.add(dynamic_name.value)

    return dependencies


def _matches_module_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


def _python_string_literals(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


ASHARE_LEGACY_RUNTIME_WRAPPERS = (
    "shared/wrappers/job_ashare_first_sample_alert.sh",
    "shared/wrappers/job_ashare_health_check.sh",
    "shared/wrappers/job_ashare_night_calibration.sh",
    "shared/wrappers/job_ashare_opening_validation.sh",
    "shared/wrappers/job_ashare_pre_open_validation.sh",
    "shared/wrappers/job_ashare_preopen_dry_run.sh",
    "shared/wrappers/job_ashare_research_evidence.sh",
    "shared/wrappers/job_ashare_sample_ops.sh",
    "shared/wrappers/job_ashare_sim_exec.sh",
    "shared/wrappers/job_opportunity_funnel_sync.sh",
)


MIXED_LEGACY_ASHARE_ENTRYPOINTS = (
    "cron/health_check.sh",
    "cron/daily_review.sh",
    "shared/wrappers/job_opening_acceptance.sh",
    "shared/wrappers/job_daily_brief_morning.sh",
    "shared/wrappers/job_daily_brief_day.sh",
    "shared/wrappers/job_daily_brief_night.sh",
    "shared/wrappers/job_premarket_signals.sh",
    "shared/wrappers/job_email_notify.sh",
)


RETIRED_ASHARE_PYTHON_JOBS = (
    "job_premarket_signals",
    "job_ashare_sim_exec",
    "job_ashare_night_calibration",
    "job_daily_brief_morning",
    "job_daily_brief_day",
    "job_daily_brief_night",
    "job_email_notify",
)


def test_system_state_matrix_has_one_truthful_entry_per_required_boundary() -> None:
    matrix = load_system_state_matrix()
    entries = {entry.entry_id: entry for entry in matrix.entries}

    assert {
        "sharedsignals_v1_query",
        "tradingagent_sharedsignals_v1_client",
        "tradingagent_sharedsignals_v1_integration_probe",
        "tradingagent_integration_readiness_local_gate",
        "tradingagent_mainboard_scope",
        "tradingagent_small_account_optimizer",
        "tradingagent_thesis_risk_authority",
        "tradingagent_small_account_plan_binding",
        "tradingagent_canonical_account_authority",
        "tradingagent_champion_authority_binding",
        "tradingagent_market_evidence_authority",
        "tradingagent_trusted_execution_clock",
        "tradingagent_phase1_industry_shadow_slice",
        "tradingagent_llm_evidence",
        "tradingagent_llm_evidence_journal",
        "tradingagent_deepseek_provider_config",
        "tradingagent_model_lifecycle",
        "tradingagent_metrics_verification_authority",
        "tradingagent_trusted_evolution_clock",
        "tradingagent_scientific_validation_contract",
        "tradingagent_offline_science_projection",
        "tradingagent_drift_runtime_binding",
        "tradingagent_day_loop",
        "tradingagent_paper_runtime_composition",
        "tradingagent_capital_backed_paper_runtime_composition",
        "tradingagent_phase1_fixture_cli",
        "tradingagent_run_bundle_store",
        "tradingagent_decision_ledger",
        "tradingagent_label_maturity",
        "tradingagent_capital_authority",
        "tradingagent_execution_lineage",
        "tradingagent_sample_journal",
        "tradingagent_repository_cron_template",
        "tradingagent_installed_cron",
        "tradingagent_production_runtime",
        "tradingagent_front",
        "tradingagent_opportunity_intelligence",
        "tradingagent_multihorizon_forecast",
        "tradingagent_multistyle_router",
        "tradingagent_deepseek_provider_transport",
        "tradingagent_llm_ashare_frozen_evaluation",
        "tradingagent_live_paper_scheduler",
    }.issubset(entries)
    assert entries["sharedsignals_v1_query"].state == "TARGET_CONTRACT"
    assert entries["sharedsignals_v1_query"].production_verified is False
    assert entries["tradingagent_sharedsignals_v1_client"].state == ("CURRENT_VERIFIED")
    readiness = entries["tradingagent_integration_readiness_local_gate"]
    assert readiness.state == "CURRENT_VERIFIED"
    assert readiness.layer == "local_isolated_candidate"
    assert readiness.canonical_path == (
        "shared/runtime_test/integration_readiness_gate.py"
    )
    assert readiness.production_verified is False
    assert "authenticate_receipt_origin_or_prove_probe_execution" in (
        readiness.prohibited_uses
    )
    assert "authorize_capital_position_order_or_sample_journal_writes" in (
        readiness.prohibited_uses
    )
    assert entries["tradingagent_small_account_optimizer"].state == ("CURRENT_VERIFIED")
    assert entries["tradingagent_thesis_risk_authority"].state == ("CURRENT_VERIFIED")
    assert entries["tradingagent_thesis_risk_authority"].production_verified is False
    assert "verify_complete_candidate_position_and_pending_exposure_set" in (
        entries["tradingagent_thesis_risk_authority"].allowed_uses
    )
    assert "accept_runtime_self_signed_policy_or_exposure_proof" in (
        entries["tradingagent_thesis_risk_authority"].prohibited_uses
    )
    assert entries["tradingagent_small_account_plan_binding"].state == (
        "CURRENT_VERIFIED"
    )
    assert entries["tradingagent_canonical_account_authority"].state == (
        "CURRENT_VERIFIED"
    )
    assert (
        entries["tradingagent_canonical_account_authority"].production_verified is False
    )
    assert entries["tradingagent_champion_authority_binding"].state == (
        "CURRENT_VERIFIED"
    )
    assert (
        "accept_caller_self_certified_champion_or_numeric_feature_authority"
        in entries["tradingagent_champion_authority_binding"].prohibited_uses
    )
    assert entries["tradingagent_market_evidence_authority"].state == (
        "CURRENT_VERIFIED"
    )
    assert (
        "claim_local_integrity_hash_as_external_authentication"
        in entries["tradingagent_market_evidence_authority"].prohibited_uses
    )
    assert entries["tradingagent_trusted_execution_clock"].state == ("CURRENT_VERIFIED")
    assert (
        "claim_fixture_clock_as_production_time_authority"
        in entries["tradingagent_trusted_execution_clock"].prohibited_uses
    )
    assert entries["tradingagent_phase1_industry_shadow_slice"].state == (
        "CURRENT_VERIFIED"
    )
    assert (
        entries["tradingagent_phase1_industry_shadow_slice"].production_verified
        is False
    )
    assert (
        entries["tradingagent_phase1_industry_shadow_slice"].successor
        == "tradingagent.industry_shadow_basket.v2"
    )
    assert (
        "accept_unverified_industry_activity_score"
        in entries["tradingagent_phase1_industry_shadow_slice"].prohibited_uses
    )
    assert (
        "independently_recompute_canonical_cost_policy"
        in entries["tradingagent_small_account_plan_binding"].allowed_uses
    )
    assert (
        "independently_verified_complete_account_snapshot_input"
        in entries["tradingagent_small_account_optimizer"].allowed_uses
    )
    assert (
        "accept_caller_self_asserted_account_authority"
        in entries["tradingagent_small_account_optimizer"].prohibited_uses
    )
    assert entries["tradingagent_day_loop"].state == "CURRENT_VERIFIED"
    assert entries["tradingagent_capital_backed_paper_runtime_composition"].state == (
        "CURRENT_VERIFIED"
    )
    assert (
        entries[
            "tradingagent_capital_backed_paper_runtime_composition"
        ].production_verified
        is False
    )
    assert entries["tradingagent_deepseek_provider_config"].state == (
        "CURRENT_VERIFIED"
    )
    assert entries["tradingagent_deepseek_provider_config"].production_verified is False
    assert "make_network_calls_from_provider_config" in (
        entries["tradingagent_deepseek_provider_config"].prohibited_uses
    )
    assert "accept_arbitrary_injected_transport_callable" in (
        entries["tradingagent_deepseek_provider_config"].prohibited_uses
    )
    assert entries["tradingagent_drift_runtime_binding"].state == ("CURRENT_VERIFIED")
    assert entries["tradingagent_drift_runtime_binding"].successor == (
        "tradingagent.drift_constrained_risk_stage.v2"
    )
    assert "reread_latest_latch_immediately_before_network_closed_simulation" in (
        entries["tradingagent_drift_runtime_binding"].allowed_uses
    )
    assert entries["tradingagent_scientific_validation_contract"].state == (
        "CURRENT_VERIFIED"
    )
    assert "derive_ashare_forward_targets_from_same_verified_calendar_proof" in (
        entries["tradingagent_scientific_validation_contract"].allowed_uses
    )
    assert "claim_production_calendar_authority_or_real_market_truth_verified" in (
        entries["tradingagent_scientific_validation_contract"].prohibited_uses
    )
    offline_science = entries["tradingagent_offline_science_projection"]
    assert offline_science.state == "CURRENT_VERIFIED"
    assert offline_science.layer == "local_isolated_candidate"
    assert offline_science.production_verified is False
    assert "read_one_explicit_frozen_sample_journal_view" in (
        offline_science.allowed_uses
    )
    assert "append_or_modify_sample_journal_facts" in (offline_science.prohibited_uses)
    assert entries["tradingagent_metrics_verification_authority"].state == (
        "CURRENT_VERIFIED"
    )
    assert (
        "claim_local_receipt_hash_as_signature_or_external_recompute"
        in entries["tradingagent_metrics_verification_authority"].prohibited_uses
    )
    assert entries["tradingagent_llm_evidence_journal"].state == ("CURRENT_VERIFIED")
    assert entries["tradingagent_llm_evidence_journal"].layer == (
        "local_isolated_candidate"
    )
    assert entries["tradingagent_llm_evidence_journal"].production_verified is False
    assert (
        "claim_local_head_anchor_as_external_seal_or_tamper_proof_authority"
        in entries["tradingagent_llm_evidence_journal"].prohibited_uses
    )
    assert entries["tradingagent_trusted_evolution_clock"].state == ("CURRENT_VERIFIED")
    assert entries["tradingagent_trusted_evolution_clock"].layer == (
        "local_isolated_candidate"
    )
    assert entries["tradingagent_trusted_evolution_clock"].production_verified is False
    assert (
        "provide_an_implicit_or_default_wall_clock"
        in entries["tradingagent_trusted_evolution_clock"].prohibited_uses
    )
    for shadow_candidate in (
        "tradingagent_opportunity_intelligence",
        "tradingagent_multihorizon_forecast",
        "tradingagent_multistyle_router",
    ):
        assert entries[shadow_candidate].state == "CURRENT_VERIFIED"
        assert entries[shadow_candidate].layer == "local_isolated_candidate"
        assert entries[shadow_candidate].production_verified is False
    transport = entries["tradingagent_deepseek_provider_transport"]
    assert transport.state == "CURRENT_VERIFIED"
    assert transport.layer == "local_isolated_candidate"
    assert transport.production_verified is False
    assert "claim_any_real_provider_request_or_authenticated_model_readback" in (
        transport.prohibited_uses
    )
    llm_frozen_eval = entries["tradingagent_llm_ashare_frozen_evaluation"]
    assert llm_frozen_eval.state == "CURRENT_VERIFIED"
    assert llm_frozen_eval.layer == "local_isolated_candidate"
    assert llm_frozen_eval.production_verified is False
    assert "report_provider_call_verified_false_for_offline_capture" in (
        llm_frozen_eval.allowed_uses
    )
    assert "claim_authenticated_deepseek_model_quality_or_live_provider_call" in (
        llm_frozen_eval.prohibited_uses
    )
    assert (
        entries["tradingagent_live_paper_scheduler"].state == "PLANNED_NOT_IMPLEMENTED"
    )
    assert entries["tradingagent_capital_authority"].state == "CURRENT_VERIFIED"
    assert all(entry.owner and entry.canonical_path for entry in matrix.entries)


def test_target_and_unverified_runtime_entries_cannot_claim_production() -> None:
    matrix = load_system_state_matrix()

    for entry in matrix.entries:
        if entry.state in {
            "TARGET_CONTRACT",
            "PLANNED_NOT_IMPLEMENTED",
            "HISTORICAL_READ_ONLY",
            "RETIREMENT_PENDING_VERIFICATION",
        }:
            assert entry.production_verified is False
        if entry.production_verified:
            assert entry.state == "CURRENT_VERIFIED"
            assert entry.layer == "production_runtime"


def test_legacy_inventory_is_timeboxed_and_has_deletion_gates() -> None:
    inventory = load_legacy_inventory()
    ids = [entry.legacy_id for entry in inventory.entries]

    assert len(ids) == len(set(ids))
    assert "ashare_classic_sharedsignals_client" in ids
    assert "ashare_direct_sqlite_reader" in ids
    assert "ashare_legacy_adapter_and_market_phases" in ids
    assert "ashare_legacy_screening_and_research_stack" in ids
    assert "ashare_legacy_review_and_runtime_wrappers" in ids
    assert "ashare_legacy_belief_sizing_stack" in ids
    assert "ashare_legacy_opportunity_funnel" in ids
    for entry in inventory.entries:
        assert entry.owner
        assert entry.replacement
        assert entry.sunset_phase
        assert entry.deletion_preconditions
        assert entry.compatibility_mode in {
            "timeboxed_read_only",
            "historical_read_only",
            "retirement_pending_verification",
        }


def test_v1_decision_chain_cannot_consume_llm_belief_or_legacy_sizers() -> None:
    """LLM evidence must remain outside the V1 sizing and order data path."""

    active_paths = (
        "shared/portfolio/champion.py",
        "shared/portfolio/small_account_optimizer.py",
        "shared/runtime/stage_ports.py",
        "shared/runtime/day_loop.py",
        "shared/runtime/composition.py",
        "shared/execution/sim_executor_registry.py",
    )
    forbidden_imports = {
        "shared.portfolio.constructor",
        "shared.portfolio.position_sizer",
        "shared.adversarial.bull_bear_debate",
    }

    for relative_path in active_paths:
        path = ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        constants: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                constants.append(node.value.lower())

        assert imported.isdisjoint(forbidden_imports), relative_path
        assert "belief_score" not in constants, relative_path

    adversarial_rules = (ROOT / "shared/adversarial/AGENTS.md").read_text(
        encoding="utf-8"
    )
    portfolio_rules = (ROOT / "shared/portfolio/AGENTS.md").read_text(encoding="utf-8")
    assert "LLM 仅提供证据" in adversarial_rules
    assert "不得进入仓位" in adversarial_rules
    assert "V1" in portfolio_rules
    assert "LLM" in portfolio_rules


def test_shadow_research_sidecars_cannot_import_or_enter_trading_authority() -> None:
    shadow_roots = (
        ROOT / "shared" / "opportunity",
        ROOT / "shared" / "forecast",
        ROOT / "shared" / "strategy_router",
        ROOT / "shared" / "llm" / "evidence_journal.py",
    )
    forbidden_prefixes = (
        "shared.capital",
        "shared.execution",
        "shared.portfolio",
        "shared.risk",
        "shared.runtime",
        "shared.markets.style_runner",
    )
    for root in shadow_roots:
        paths = (root,) if root.is_file() else tuple(sorted(root.rglob("*.py")))
        assert paths, root
        for path in paths:
            dependencies = _python_dependency_names(path)
            assert not any(
                _matches_module_prefix(dependency, prefix)
                for dependency in dependencies
                for prefix in forbidden_prefixes
            ), path.relative_to(ROOT)

    active_authority_paths = tuple(
        path
        for relative_directory in TRADING_AUTHORITY_DIRECTORIES
        for path in sorted((ROOT / relative_directory).rglob("*.py"))
    )
    forbidden_shadow_prefixes = (
        "shared.opportunity",
        "shared.forecast",
        "shared.strategy_router",
        "shared.llm.evidence_journal",
    )
    for path in active_authority_paths:
        dependencies = _python_dependency_names(path)
        assert not any(
            _matches_module_prefix(dependency, prefix)
            for dependency in dependencies
            for prefix in forbidden_shadow_prefixes
        ), path.relative_to(ROOT)


def test_integration_readiness_preflight_is_disconnected_from_trading_authority() -> (
    None
):
    """Receipt compatibility checks must remain preflight-only and read-only."""

    forbidden_preflight_dependencies = (
        *TRADING_AUTHORITY_MODULE_PREFIXES,
        "shared.review.sample_journal",
        "shared.review.projection_generation",
    )
    for relative_path in INTEGRATION_READINESS_PREFLIGHT_PATHS:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        dependencies = _python_dependency_names(path)
        assert not any(
            _matches_module_prefix(dependency, prefix)
            for dependency in dependencies
            for prefix in forbidden_preflight_dependencies
        ), relative_path

    preflight_prefixes = (
        "shared.runtime_test.integration_readiness_gate",
        "shared.runtime_test.integration_readiness_profile",
    )
    violations: dict[str, list[str]] = {}
    for relative_directory in TRADING_AUTHORITY_DIRECTORIES:
        for path in sorted((ROOT / relative_directory).rglob("*.py")):
            blocked = sorted(
                dependency
                for dependency in _python_dependency_names(path)
                if any(
                    _matches_module_prefix(dependency, prefix)
                    for prefix in preflight_prefixes
                )
            )
            if blocked:
                violations[str(path.relative_to(ROOT))] = blocked

    assert violations == {}, (
        "integration readiness is a runtime_test preflight only; trading "
        f"authority modules must not import it: {violations}"
    )


def test_trading_authority_modules_cannot_depend_on_llm_sidecar() -> None:
    """The LLM sidecar must never enter an active trading authority package."""

    violations: dict[str, list[str]] = {}
    for relative_directory in TRADING_AUTHORITY_DIRECTORIES:
        for path in sorted((ROOT / relative_directory).rglob("*.py")):
            blocked = sorted(
                dependency
                for dependency in _python_dependency_names(path)
                if _matches_module_prefix(dependency, "shared.llm")
            )
            if blocked:
                violations[str(path.relative_to(ROOT))] = blocked

    assert violations == {}, (
        "LLM evidence is research-only and cannot be imported or dynamically "
        f"consumed by trading authority modules: {violations}"
    )


def test_llm_sidecar_cannot_depend_on_trading_authority_modules() -> None:
    """The research sidecar must not acquire portfolio, risk, or order authority."""

    violations: dict[str, list[str]] = {}
    for path in sorted((ROOT / "shared/llm").rglob("*.py")):
        blocked = sorted(
            dependency
            for dependency in _python_dependency_names(path)
            if any(
                _matches_module_prefix(dependency, prefix)
                for prefix in TRADING_AUTHORITY_MODULE_PREFIXES
            )
        )
        if blocked:
            violations[str(path.relative_to(ROOT))] = blocked

    assert violations == {}, (
        "The LLM sidecar cannot import or dynamically consume any trading "
        f"authority module: {violations}"
    )


def test_llm_package_exports_only_stable_evidence_contracts() -> None:
    import shared.llm as llm

    required = {
        "GatewayAnalysisResult",
        "ProviderTransportReceipt",
        "ProviderTransportReceiptError",
        "ProviderEvidenceBindingError",
        "ProviderOutputSensitiveError",
        "LLMEvidenceEnvelope",
        "LLMEvidenceEnvelopeError",
        "LLMEvidenceJournal",
        "LLMEvidenceJournalError",
        "LLMEvidenceJournalReadback",
        "EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256",
    }

    assert required.issubset(set(llm.__all__))
    assert "ProviderInvocationResult" not in llm.__all__


def test_capital_runtime_composition_uses_only_public_stage_contracts() -> None:
    source = (ROOT / "shared/runtime/composition.py").read_text(encoding="utf-8")
    forbidden_private_couplings = (
        "._approved_order_map",
        "._strict_order",
        "._validated_market_snapshot",
        "._release_unfilled",
        "canonical_small_account_decision_port._account",
        "canonical_small_account_decision_port._trade_date",
        "canonical_small_account_decision_port._decision_time",
    )

    assert not [token for token in forbidden_private_couplings if token in source], (
        "capital composition must depend on public stage contracts only"
    )


def test_capital_runtime_consumes_generation_and_lineage_from_current_snapshot() -> (
    None
):
    source = (ROOT / "shared/runtime/composition.py").read_text(encoding="utf-8")

    assert "ASHARE_AUTHORITY_GENERATION" not in source
    assert "ASHARE_EXECUTION_LINEAGE_ID" not in source


def test_declared_tradingagent_legacy_paths_are_real_and_still_timeboxed() -> None:
    inventory = load_legacy_inventory()
    entries = {entry.legacy_id: entry for entry in inventory.entries}
    required_ids = {
        "ashare_classic_sharedsignals_client",
        "ashare_direct_sqlite_reader",
        "ashare_legacy_adapter_and_market_phases",
        "ashare_legacy_screening_and_research_stack",
        "ashare_legacy_review_and_runtime_wrappers",
        "ashare_legacy_belief_sizing_stack",
        "ashare_legacy_opportunity_funnel",
    }

    for legacy_id in required_ids:
        entry = entries[legacy_id]
        assert entry.sunset_phase == "phase_3"
        assert entry.compatibility_mode in {
            "timeboxed_read_only",
            "retirement_pending_verification",
        }
        for relative_path in entry.paths:
            assert (ROOT / relative_path).exists(), (
                f"declared legacy path does not exist: {relative_path}"
            )
        for relative_path in entry.runtime_paths:
            ignored = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", "--", relative_path],
                cwd=ROOT,
                check=False,
            )
            assert ignored.returncode == 0, (
                "declared legacy runtime path must be intentionally ignored: "
                f"{relative_path}"
            )


def test_legacy_runtime_paths_cannot_escape_the_repository(tmp_path: Path) -> None:
    inventory_path = tmp_path / "legacy_inventory.yaml"
    inventory_path.write_text(
        """\
version: 1
entries:
  - legacy_id: unsafe_runtime_path
    owner: TradingAgent
    paths:
      - README.md
    runtime_paths:
      - ../outside-runtime
    replacement: safe_replacement
    compatibility_mode: timeboxed_read_only
    sunset_phase: phase_3
    remaining_consumers:
      - none_verified
    deletion_preconditions:
      - installed_runtime_readback
    rollback: preserve_read_only_evidence
""",
        encoding="utf-8",
    )

    try:
        load_legacy_inventory(inventory_path)
    except ValueError as exc:
        assert "repository-relative" in str(exc)
    else:
        raise AssertionError("escaping runtime path must fail closed")


def test_contract_ids_are_synchronized_with_active_docs() -> None:
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    data_contract = (ROOT / "docs" / "data_contract.md").read_text(encoding="utf-8")

    assert SHARED_SIGNALS_QUERY_CONTRACT_ID in data_contract
    assert UNIVERSE_SCOPE_CONTRACT_ID in architecture
    assert LLM_EVIDENCE_CONTRACT_ID in architecture

    system_state_doc = (ROOT / "docs" / "system_state_matrix.md").read_text(
        encoding="utf-8"
    )
    universe_doc = (ROOT / "docs" / "universe_contract.md").read_text(encoding="utf-8")
    assert "system_state_matrix.yaml" in system_state_doc
    assert UNIVERSE_SCOPE_CONTRACT_ID in universe_doc
    assert "context_only=true" in universe_doc


def test_new_ashare_architecture_has_no_legacy_data_import_or_endpoint() -> None:
    candidates = [
        ROOT / "shared" / "data" / "sharedsignals_v1.py",
        ROOT / "shared" / "data" / "evidence_gate.py",
        ROOT / "shared" / "data" / "research_snapshot.py",
        ROOT / "shared" / "data" / "research_snapshot_store.py",
        ROOT / "shared" / "portfolio" / "small_account_optimizer.py",
    ]
    for directory in (
        ROOT / "shared" / "runtime",
        ROOT / "shared" / "universe",
        ROOT / "shared" / "models",
        ROOT / "shared" / "llm",
    ):
        candidates.extend(directory.glob("*.py"))

    for path in sorted(set(candidates)):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: list[str] = []
        string_values: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                string_values.append(node.value.lower())

        assert "shared.data.reader" not in imported, path
        assert "shared.data.shared_signals_api" not in imported, path
        assert all("/tushare" not in value for value in string_values), path


def test_ta_tests_do_not_import_or_reimplement_upstream_data_servers() -> None:
    """TA tests may mock its client transport, but never inspect an upstream repo."""

    violations: dict[str, list[str]] = {}
    this_file = Path(__file__).resolve()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.resolve() == this_file:
            continue
        dependencies = _python_dependency_names(path)
        source = path.read_text(encoding="utf-8")
        source_lower = source.lower()
        blocked: list[str] = []
        if "http.server" in dependencies:
            blocked.append("http.server")
        for token in (
            "_sibling_project(",
            "SharedSignalsHTTPServer",
            "_sqlite_rows_by_symbols",
            "MARKETDATA_SQLITE",
        ):
            if token in source:
                blocked.append(token)
        if "sharedsignals" in source_lower:
            for token in ("reader.py", "api_server.py", "spec_from_file_location"):
                if token in source_lower:
                    blocked.append(f"sharedsignals+{token}")
        if blocked:
            violations[str(path.relative_to(ROOT))] = sorted(set(blocked))

    assert violations == {}, (
        "TradingAgent owns a consumer contract, not sibling repository readers, "
        f"SQLite internals or HTTP servers: {violations}"
    )


def test_current_v1_consumers_use_only_catalog_and_query_routes() -> None:
    violations: dict[str, list[str]] = {}

    for relative_path in CURRENT_V1_SS_CONSUMER_PATHS:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        source = path.read_text(encoding="utf-8").lower()
        blocked = sorted(
            route for route in FORBIDDEN_CURRENT_V1_SS_ROUTES if route in source
        )
        if blocked:
            violations[relative_path] = blocked

    assert violations == {}, (
        "Current V1 consumers may use only GET /v1/catalog and POST /v1/query; "
        f"legacy or provider-specific routes found: {violations}"
    )


def test_v1_candidate_manifest_excludes_legacy_data_and_server_tests() -> None:
    manifest = ROOT / "tests" / "ta_v1_candidate_manifest.txt"
    entries = tuple(
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    forbidden_imports = {
        "shared.data.reader",
        "shared.data.shared_signals_api",
        "shared.runtime_test.opening_acceptance",
        "shared.runtime_test.sharedsignals_source_status",
    }
    violations: dict[str, list[str]] = {}

    for relative_path in entries:
        path = ROOT / relative_path
        dependencies = _python_dependency_names(path)
        blocked = sorted(
            dependency
            for dependency in dependencies
            if any(
                _matches_module_prefix(dependency, prefix)
                for prefix in forbidden_imports
            )
        )
        if "http.server" in dependencies:
            blocked.append("http.server")
        if blocked:
            violations[relative_path] = sorted(set(blocked))

    assert violations == {}, (
        "The TA V1 candidate cannot include active-compatibility readers, legacy "
        f"runtime gates or upstream server tests: {violations}"
    )


def test_legacy_data_and_research_paths_are_classified_outside_current_v1() -> None:
    entries = {entry.legacy_id: entry for entry in load_legacy_inventory().entries}

    classic = entries["ashare_classic_sharedsignals_client"]
    direct = entries["ashare_direct_sqlite_reader"]
    screening = entries["ashare_legacy_screening_and_research_stack"]
    wrappers = entries["ashare_legacy_review_and_runtime_wrappers"]
    multi_market = entries["multi_market_legacy_sim_wrappers"]
    assert classic.compatibility_mode == "retirement_pending_verification"
    assert "legacy_regression_tests_not_in_v1_candidate" in classic.remaining_consumers
    assert "cnfutures_and_non_ashare_active_compatibility" in direct.remaining_consumers
    assert all("current_v1" not in item for item in direct.remaining_consumers)
    assert all("current_v1" not in item for item in screening.remaining_consumers)
    assert "hard_blocked_ashare_wrappers" in wrappers.remaining_consumers
    assert multi_market.compatibility_mode == "retirement_pending_verification"
    assert "migration_probe_only_not_scheduled" in multi_market.remaining_consumers

    screening_rules = (ROOT / "shared/screening/AGENTS.md").read_text(encoding="utf-8")
    benchmark_rules = (ROOT / "shared/benchmark/AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    for expected in ("current-v1", "active-compatibility", "hard-blocked"):
        assert expected in readme
        assert expected in status
    assert "active-compatibility / retirement-pending" in screening_rules
    assert "active-compatibility、只读分析" in benchmark_rules


def test_active_front_readers_are_v1_only_and_have_no_retired_sqlite_fallback() -> None:
    pulse_reader = (
        ROOT / "front" / "src" / "server" / "sharedSignalsMarketPulse.ts"
    ).read_text(encoding="utf-8")
    snapshot_reader = (
        ROOT / "front" / "src" / "server" / "tradingAgentSnapshot.ts"
    ).read_text(encoding="utf-8")
    read_model = (
        ROOT / "front" / "src" / "api" / "tradingAgentReadModel.ts"
    ).read_text(encoding="utf-8")
    active_front = "\n".join((pulse_reader, snapshot_reader, read_model))

    assert "/v1/catalog" in pulse_reader
    assert "/v1/query" in pulse_reader
    for forbidden_endpoint in (
        "/realtime_5min?",
        "/crypto?",
        "/pm_prices?",
        "/market_data?",
        "/capital_flow?",
        "/tushare?",
    ):
        assert forbidden_endpoint not in active_front
    assert "node:sqlite" not in snapshot_reader
    assert "DatabaseSync" not in snapshot_reader
    assert "position_ledger_simulated" not in snapshot_reader
    assert "ashare-sim-fresh-20260712-v1" not in active_front


def test_retained_legacy_endpoint_and_sharedsignals_sqlite_paths_are_in_inventory() -> (
    None
):
    inventory = load_legacy_inventory()
    inventoried_paths = {
        path
        for entry in inventory.entries
        for path in (*entry.paths, *entry.runtime_paths)
    }
    retained_legacy_paths = {
        "CNFutures/adapter.py",
        "CNFutures/opening_validator.py",
        "CNFutures/replay.py",
        "shared/data/reader.py",
        "shared/data/shared_signals_api.py",
        "shared/review/benchmark.py",
        "shared/runtime_test/ashare_preopen_dry_run.py",
        "shared/runtime_test/cn_futures_live_check.py",
        "shared/runtime_test/market_health.py",
        "shared/runtime_test/opening_acceptance.py",
        "shared/runtime_test/sharedsignals_evidence_contract.py",
    }

    assert retained_legacy_paths.issubset(inventoried_paths), (
        "retained legacy endpoint/SQLite readers must be explicitly timeboxed; missing: "
        f"{sorted(retained_legacy_paths - inventoried_paths)}"
    )


def test_retired_ashare_runtime_wrappers_are_executable_fail_closed() -> None:
    guard_name = "block_retired_ashare_runtime"
    common = ROOT / "shared" / "wrappers" / "_common.sh"
    common_source = common.read_text(encoding="utf-8")

    assert f"{guard_name}()" in common_source
    assert "return 78" in common_source
    assert "ASHARE_LEGACY_RUNTIME_ENABLED" not in common_source

    for relative_path in ASHARE_LEGACY_RUNTIME_WRAPPERS:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "_common.sh" in source, relative_path
        assert Path(relative_path).name in common_source, relative_path
        assert 'source "${SHARED_DIR}/env_loader.sh"' not in source, relative_path

    reconcile = (
        ROOT / "shared" / "wrappers" / "job_market_capital_reconcile.sh"
    ).read_text(encoding="utf-8")
    assert "_common.sh" in reconcile
    assert "job_market_capital_reconcile.sh" in common_source

    env = os.environ.copy()
    env["TRADINGAGENT_ENV_LOADER_READY"] = "1"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {common!s}; {guard_name} architecture-test",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 78
    assert "blocked=retired_ashare_runtime" in result.stderr


def test_mixed_legacy_entrypoints_with_ashare_data_or_email_are_fail_closed(
    tmp_path: Path,
) -> None:
    """Generic job names must not hide an active A-share legacy data edge."""

    env_sentinel = tmp_path / "env-was-sourced"
    python_sentinel = tmp_path / "python-was-run"
    runtime_root = tmp_path / "runtime-must-not-exist"
    logs_root = tmp_path / "logs-must-not-exist"
    env_file = tmp_path / "malicious.env"
    env_file.write_text(
        f"touch {env_sentinel!s}\nexport REAL_TRADING_ENABLED=false\n",
        encoding="utf-8",
    )
    python_probe = tmp_path / "python-probe.sh"
    python_probe.write_text(
        f"#!/bin/bash\ntouch {python_sentinel!s}\nexit 99\n",
        encoding="utf-8",
    )
    python_probe.chmod(0o755)

    env = os.environ.copy()
    env.pop("TRADINGAGENT_ENV_LOADER_READY", None)
    env.update(
        {
            "BASH_ENV": str(ROOT / "shared" / "env_loader.sh"),
            "TRADINGAGENT_ROOT": str(ROOT),
            "TRADINGAGENT_ENV_FILE": str(env_file),
            "FINANCE_SHARED_ENV_FILE": str(env_file),
            "TRADINGS_RUNTIME_ROOT": str(runtime_root),
            "TRADINGS_LOG_ROOT": str(logs_root),
            "TRADINGAGENT_PYTHON": str(python_probe),
            "PYTHON_BIN": str(python_probe),
            "PYTHONPATH": str(ROOT),
            "REAL_TRADING_ENABLED": "false",
        }
    )

    retired_invocations = [
        *((relative_path, ()) for relative_path in ASHARE_LEGACY_RUNTIME_WRAPPERS),
        *((relative_path, ()) for relative_path in MIXED_LEGACY_ASHARE_ENTRYPOINTS),
        ("shared/wrappers/job_market_capital_reconcile.sh", ("ashare", "ops")),
    ]
    for relative_path, args in retired_invocations:
        path = ROOT / relative_path
        source = path.read_text(encoding="utf-8")
        assert "_common.sh" in source, relative_path
        result = subprocess.run(
            ["bash", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 78, relative_path
        assert "blocked=retired_ashare_runtime" in result.stderr, relative_path

    assert not env_sentinel.exists()
    assert not python_sentinel.exists()
    assert not runtime_root.exists()
    assert not logs_root.exists()


def test_linux_style_bash_env_preflight_cannot_source_retired_wrapper_env(
    tmp_path: Path,
) -> None:
    """BASH_ENV must stay side-effect free when Bash exposes the script as $0."""

    env_sentinel = tmp_path / "env-was-sourced"
    env_file = tmp_path / "malicious.env"
    env_file.write_text(
        f"touch {env_sentinel!s}\nexport REAL_TRADING_ENABLED=false\n",
        encoding="utf-8",
    )
    wrapper = ROOT / ASHARE_LEGACY_RUNTIME_WRAPPERS[0]
    env = os.environ.copy()
    env.pop("TRADINGAGENT_ENV_LOADER_READY", None)
    env.update(
        {
            "BASH_ENV": str(ROOT / "shared" / "env_loader.sh"),
            "TRADINGAGENT_BASH_ENV_PREFLIGHT_DONE": "externally-seeded",
            "TRADINGAGENT_ROOT": str(ROOT),
            "TRADINGAGENT_ENV_FILE": str(env_file),
            "FINANCE_SHARED_ENV_FILE": str(tmp_path / "missing.env"),
            "REAL_TRADING_ENABLED": "false",
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"',
            str(wrapper),
            str(wrapper),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 78
    assert "blocked=retired_ashare_runtime" in result.stderr
    assert not env_sentinel.exists()


def test_sourced_retired_ashare_entrypoints_block_before_env_file_side_effects(
    tmp_path: Path,
) -> None:
    """Sourcing a retired wrapper must be as safe as executing it directly."""

    env_sentinel = tmp_path / "env-was-sourced"
    runtime_root = tmp_path / "runtime-must-not-exist"
    env_file = tmp_path / "malicious.env"
    env_file.write_text(
        f"touch {env_sentinel!s}\nexport REAL_TRADING_ENABLED=false\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("BASH_ENV", None)
    env.pop("TRADINGAGENT_ENV_LOADER_READY", None)
    env.update(
        {
            "TRADINGAGENT_ROOT": str(ROOT),
            "TRADINGAGENT_ENV_FILE": str(env_file),
            "FINANCE_SHARED_ENV_FILE": str(tmp_path / "missing.env"),
            "TRADINGS_RUNTIME_ROOT": str(runtime_root),
            "REAL_TRADING_ENABLED": "false",
        }
    )

    invocations = [
        *((relative_path, ()) for relative_path in ASHARE_LEGACY_RUNTIME_WRAPPERS),
        ("shared/wrappers/job_market_capital_reconcile.sh", ("ashare", "ops")),
    ]
    for relative_path, args in invocations:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$@"',
                "bash",
                str(ROOT / relative_path),
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 78, relative_path
        assert "blocked=retired_ashare_runtime" in result.stderr, relative_path
        assert not env_sentinel.exists(), relative_path
        assert not runtime_root.exists(), relative_path


def test_cron_templates_do_not_schedule_unconditionally_retired_generic_jobs() -> None:
    retired_generic = (
        "cron/health_check.sh",
        "shared/wrappers/job_opening_acceptance.sh",
        "shared/wrappers/job_daily_brief_morning.sh",
        "shared/wrappers/job_daily_brief_day.sh",
        "shared/wrappers/job_daily_brief_night.sh",
    )
    retained_non_ashare = {
        "shared/wrappers/job_sim_market_health.sh": (
            'export TRADINGAGENT_SIM_MARKETS="crypto,pm,us,cn_futures"',
        ),
        "shared/wrappers/job_equity_snapshots.sh": (
            'MARKETS="crypto,pm,us,cn_futures"',
            '--markets "${MARKETS}"',
        ),
    }

    for relative_crontab in ("shared/crontab.txt", "crontab.txt"):
        schedule = (ROOT / relative_crontab).read_text(encoding="utf-8")
        for entrypoint in retired_generic:
            assert entrypoint not in schedule, (relative_crontab, entrypoint)
        for entrypoint in retained_non_ashare:
            assert entrypoint in schedule, (relative_crontab, entrypoint)

    for relative_path, expected_contracts in retained_non_ashare.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "block_retired_ashare_runtime" not in source, relative_path
        assert "ashare" not in source.lower(), relative_path
        for expected_contract in expected_contracts:
            assert expected_contract in source, (relative_path, expected_contract)


def test_retained_multi_market_wrappers_execute_without_ashare_scope(
    tmp_path: Path,
) -> None:
    """The surviving generic jobs must execute, but only for non-A markets."""

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    probe_file = tmp_path / "python-probe.log"
    python_probe = tmp_path / "python-probe.sh"
    python_probe.write_text(
        "#!/bin/bash\n"
        "printf 'SIM_MARKETS=%s ARGS=%s\\n' "
        '"${TRADINGAGENT_SIM_MARKETS:-}" "$*" >> "${PROBE_FILE}"\n',
        encoding="utf-8",
    )
    python_probe.chmod(0o755)
    flock_probe = tmp_path / "flock"
    flock_probe.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    flock_probe.chmod(0o755)
    timeout_probe = tmp_path / "timeout"
    timeout_probe.write_text(
        '#!/bin/bash\nshift\nexec "$@"\n',
        encoding="utf-8",
    )
    timeout_probe.chmod(0o755)

    env = os.environ.copy()
    env.pop("BASH_ENV", None)
    env.pop("TRADINGAGENT_ENV_LOADER_READY", None)
    env.update(
        {
            "TRADINGAGENT_ROOT": str(runtime_root),
            "TRADINGAGENT_ENV_FILE": str(tmp_path / "missing-ta.env"),
            "FINANCE_SHARED_ENV_FILE": str(tmp_path / "missing-finance.env"),
            "TRADINGAGENT_PYTHON": str(python_probe),
            "PROBE_FILE": str(probe_file),
            "REAL_TRADING_ENABLED": "false",
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
        }
    )

    for relative_path in (
        "shared/wrappers/job_sim_market_health.sh",
        "shared/wrappers/job_equity_snapshots.sh",
    ):
        result = subprocess.run(
            ["bash", str(ROOT / relative_path)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (relative_path, result.stderr)

    probe = probe_file.read_text(encoding="utf-8").lower()
    assert "ashare" not in probe
    assert "sim_markets=crypto,pm,us,cn_futures" in probe
    assert "--markets crypto,pm,us,cn_futures" in probe


def test_tradings_cron_entry_blocks_retired_ashare_and_brief_jobs_before_dispatch(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "REAL_TRADING_ENABLED": "true",
            # Deliberately omit the repository root.  A correct early block
            # exits before project imports; a regression fails safely with an
            # import error instead of reaching a real data or email handler.
            "PYTHONPATH": str(tmp_path),
        }
    )
    entrypoint = ROOT / "shared" / "wrappers" / "tradings_cron_entry.py"

    for job_name in RETIRED_ASHARE_PYTHON_JOBS:
        result = subprocess.run(
            [sys.executable, str(entrypoint), "--job", job_name],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 78, job_name
        assert "blocked=retired_ashare_runtime" in result.stderr, job_name


def test_tradings_cron_entry_early_guard_matches_final_job_argument_semantics(
    tmp_path: Path,
) -> None:
    """Abbreviation and duplicate options must not reach project imports first."""

    env = os.environ.copy()
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "REAL_TRADING_ENABLED": "true",
            "PYTHONPATH": str(tmp_path),
        }
    )
    entrypoint = ROOT / "shared" / "wrappers" / "tradings_cron_entry.py"
    invocations = (
        ("--jo", "job_email_notify"),
        ("--job", "job_crypto_sim", "--job", "job_email_notify"),
    )

    for argv in invocations:
        result = subprocess.run(
            [sys.executable, str(entrypoint), *argv],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 78, argv
        assert "blocked=retired_ashare_runtime" in result.stderr, argv
        assert "ModuleNotFoundError" not in result.stderr, argv

    source = entrypoint.read_text(encoding="utf-8")
    assert "ArgumentParser(allow_abbrev=False)" in source


def test_tradings_cron_entry_retirement_scope_does_not_include_other_markets() -> None:
    from shared.wrappers.tradings_cron_entry import is_retired_runtime_job

    for job_name in (
        "job_trading_signals",
        "job_crypto_sim",
        "job_us_sim",
        "job_pm_sim",
        "job_cn_futures_sim",
    ):
        assert is_retired_runtime_job(job_name) is False


def test_retired_ashare_entrypoints_are_not_scheduled() -> None:
    """A blocked compatibility wrapper must not remain recurring cron work."""

    for relative_crontab in ("shared/crontab.txt", "crontab.txt"):
        for line in (ROOT / relative_crontab).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert "job_ashare_" not in stripped, (relative_crontab, stripped)
            assert "job_market_capital_reconcile.sh ashare" not in stripped, (
                relative_crontab,
                stripped,
            )


def test_active_operations_do_not_recommend_retired_ashare_entrypoints() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

    for retired_text in (
        "/source_status",
        "shared.runtime_test.ashare_preopen_dry_run",
        "shared.runtime_test.ashare_opening_validator",
        "shared.runtime_test.ashare_sample_ops",
    ):
        assert retired_text not in operations


def test_server_sidecar_safe_env_disables_legacy_localhost_clients() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    safe_env = operations.split("SAFE_ENV=(", 1)[1].split("\n)", 1)[0]

    assert "SHAREDSIGNALS_API_URL=" in safe_env
    assert "MARKETGRAPH_API_URL=" in safe_env


def test_human_state_matrix_names_every_machine_governance_entry() -> None:
    matrix = load_system_state_matrix()
    human_matrix = (ROOT / "docs" / "system_state_matrix.md").read_text(
        encoding="utf-8"
    )

    missing = [
        entry.entry_id for entry in matrix.entries if entry.entry_id not in human_matrix
    ]
    assert missing == []
    stale_rows = [
        entry.entry_id
        for entry in matrix.entries
        if f"| `{entry.entry_id}` | `{entry.state} / {entry.layer}` |"
        not in human_matrix
    ]
    assert stale_rows == []


def test_machine_state_test_evidence_is_closed_by_candidate_manifest() -> None:
    matrix = load_system_state_matrix()
    manifest = ROOT / "tests" / "ta_v1_candidate_manifest.txt"
    entries = {
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    referenced_tests = {
        evidence
        for entry in matrix.entries
        for evidence in entry.evidence
        if evidence.startswith("tests/")
        and evidence.endswith(".py")
        and Path(evidence).name.startswith("test_")
    }

    assert referenced_tests.issubset(entries)


def test_candidate_check_manifest_is_complete_and_resolves_to_tests() -> None:
    manifest = ROOT / "tests" / "ta_v1_candidate_manifest.txt"
    entries = tuple(
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    required = {
        "tests/test_architecture_contract_guards.py",
        "tests/test_sharedsignals_v1.py",
        "tests/test_sharedsignals_v1_runtime_gate.py",
        "tests/test_sharedsignals_v1_integration_probe.py",
        "tests/test_data_evidence_gate.py",
        "tests/test_research_data_snapshot.py",
        "tests/test_research_snapshot_store.py",
        "tests/test_mainboard_scope_policy.py",
        "tests/test_three_universe_snapshots.py",
        "tests/test_industry_shadow_slice.py",
        "tests/test_market_capital.py",
        "tests/test_ashare_capital_plan.py",
        "tests/test_small_account_optimizer.py",
        "tests/test_small_account_stage.py",
        "tests/test_frozen_champion.py",
        "tests/test_model_lifecycle.py",
        "tests/test_drift_policy.py",
        "tests/test_drift_action_store.py",
        "tests/test_drift_runtime.py",
        "tests/test_drift_constrained_risk_stage.py",
        "tests/test_negative_only_evolution.py",
        "tests/test_llm_sidecar.py",
        "tests/test_llm_evidence_artifact.py",
        "tests/test_llm_evidence_journal.py",
        "tests/test_llm_evaluation.py",
        "tests/test_opportunity_radar_contracts.py",
        "tests/test_opportunity_ledger.py",
        "tests/test_opportunity_funnel_cron.py",
        "tests/test_multihorizon_forecast_contract.py",
        "tests/test_strategy_router_contracts.py",
        "tests/test_runtime_stage_ports.py",
        "tests/test_paper_runtime_composition.py",
        "tests/test_ashare_day_loop.py",
        "tests/test_day_loop_recovery.py",
        "tests/test_run_bundle_file_store.py",
        "tests/test_run_bundle_chain_validation.py",
        "tests/test_run_bundle_publisher.py",
        "tests/test_phase1_paper_fixture_cli.py",
        "tests/test_decision_ledger.py",
        "tests/test_decision_ledger_persistence.py",
        "tests/test_label_maturity.py",
        "tests/test_forward_labels.py",
        "tests/test_ashare_no_legacy_sim_fallback.py",
        "tests/test_cron_coverage.py",
        "tests/test_merge_tradingagent_crontab.py",
    }

    assert len(entries) == len(set(entries))
    assert required.issubset(entries)
    for relative_path in entries:
        assert relative_path.startswith("tests/test_") and relative_path.endswith(".py")
        assert (ROOT / relative_path).is_file(), relative_path
