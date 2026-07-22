from __future__ import annotations

import ast
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path

import pytest

from shared.data.research_snapshot import (
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)
from shared.runtime.ashare_observation_history import (
    ASHARE_OBSERVATION_HISTORY_READINESS_SCHEMA_ID,
    PROSPECTIVE_OBSERVATION_HISTORY,
    AshareObservationFeatureReadiness,
    AshareObservationHistoryCoverage,
    AshareObservationHistoryReadiness,
)
from shared.runtime.ashare_paper_planning import (
    ASHARE_PAPER_PLANNING_DECISION_SCHEMA_ID,
    AsharePaperPlanningContractError,
    AsharePaperPlanningStoreConflict,
    AsharePaperPlanningStoreCorruption,
    FileAsharePaperPlanningStore,
    build_ashare_daily_planning_decision,
)
from shared.runtime.ashare_observation_ledger import (
    OBSERVED_REASON_CODE,
    AshareObservationMembershipRecord,
    build_ashare_observation_membership_artifact,
)
from shared.runtime.ashare_runtime_ports import (
    ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID,
    AshareRuntimeAuthorityBundle,
    _promote_verified_committed_bundle,
)


PROFILE_ID = "ashare-phase1-current-observation-v1"
CATALOG_VERSION = "catalog-ashare-paper-planning-v1"
DECISION_AS_OF = "2026-07-22T15:30:00+08:00"
SESSION_DATE = "20260722"
TARGET_SYMBOL = "600000.SH"


def _unit_verified(
    bundle: AshareRuntimeAuthorityBundle,
) -> AshareRuntimeAuthorityBundle:
    """Mark a pure unit fixture; durable minting is tested in runtime_ports."""

    return _promote_verified_committed_bundle(bundle)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _dataset() -> ResearchDatasetSnapshot:
    rows = [
        {
            "amount": 1_000_000.0,
            "close": 10.0,
            "trade_date": SESSION_DATE,
            "ts_code": TARGET_SYMBOL,
        }
    ]
    return ResearchDatasetSnapshot(
        dataset_id="cn.equity.daily",
        role="required_execution",
        api_version="v1",
        catalog_version=CATALOG_VERSION,
        request_id="daily-request-20260722",
        receipt_id="daily-receipt-20260722",
        evidence_state="ready",
        evidence_action="accept",
        eligible=True,
        weight=1.0,
        reasons=(),
        source_proof_complete=True,
        lineage_sha256=_sha256("daily-lineage"),
        source_proof_sha256=_sha256("daily-source-proof"),
        data_through=DECISION_AS_OF,
        observed_at=DECISION_AS_OF,
        next_cursor=None,
        row_count=1,
        observation_mode="current_observation",
        historical_pit_eligible=False,
        query_as_of_mode="decision_as_of",
        minimum_row_count=1,
        max_pages=20,
        max_rows=100_000,
        identity_fields=("ts_code", "trade_date"),
        row_event_time_field="trade_date",
        row_event_time_format="yyyymmdd",
        row_event_timezone="Asia/Shanghai",
        row_event_time_semantic="session",
        identity_sha256=_sha256([[TARGET_SYMBOL, SESSION_DATE]]),
        row_observation_sha256=_sha256(rows),
        max_row_observed_at=DECISION_AS_OF,
        max_row_event_value=SESSION_DATE,
        page_count=1,
        pagination_trace_sha256=_sha256("daily-page-trace"),
        pagination_semantic_sha256=_sha256("daily-page-semantic"),
        page_request_set_sha256=_sha256("daily-page-requests"),
        page_response_set_sha256=_sha256("daily-page-responses"),
        cursor_chain_sha256=_sha256("daily-cursor-chain"),
        response_sha256=_sha256(["daily-response", rows]),
        _rows_json=json.dumps(rows, separators=(",", ":"), sort_keys=True),
    )


def _runtime_authority() -> AshareRuntimeAuthorityBundle:
    dataset = _dataset()
    snapshot_payload = {
        "profile_id": PROFILE_ID,
        "profile_contract_sha256": _sha256("profile-contract"),
        "catalog_version": CATALOG_VERSION,
        "decision_as_of": DECISION_AS_OF,
        "datasets": [
            {
                "dataset_id": dataset.dataset_id,
                "role": dataset.role,
                "response_sha256": dataset.response_sha256,
            }
        ],
        "blocking_reasons": [],
    }
    snapshot = ResearchDataSnapshot(
        profile_id=PROFILE_ID,
        profile_contract_sha256=snapshot_payload["profile_contract_sha256"],
        catalog_version=CATALOG_VERSION,
        decision_as_of=DECISION_AS_OF,
        datasets=(dataset,),
        execution_eligible=True,
        historical_pit_eligible=False,
        blocking_reasons=(),
        snapshot_sha256=_sha256(snapshot_payload),
    )
    probe_receipt_sha256 = _sha256("probe-receipt")
    observation_unsigned = {
        "schema_id": "tradingagent.ashare.observation-receipt.v1",
        "profile_id": PROFILE_ID,
        "catalog_version": CATALOG_VERSION,
        "decision_as_of": DECISION_AS_OF,
        "manifest_sha256": _sha256("manifest"),
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe_receipt_sha256,
        "tradable_universe_count": 1,
        "tradable_universe_sha256": _sha256([TARGET_SYMBOL]),
        "excluded_reason_counts": {},
        "context_probe_roles": [],
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "real_trading_enabled": False,
        "historical_pit_eligible": False,
        "execution_authority": False,
    }
    observation_receipt = {
        **observation_unsigned,
        "receipt_sha256": _sha256(observation_unsigned),
    }
    membership = build_ashare_observation_membership_artifact(
        observation_session=SESSION_DATE,
        research_snapshot=snapshot,
        observation_receipt=observation_receipt,
        records=(
            AshareObservationMembershipRecord(
                symbol=TARGET_SYMBOL,
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
        ),
    )
    return _unit_verified(
        AshareRuntimeAuthorityBundle(
            research_snapshot=snapshot,
            profile_id=PROFILE_ID,
            catalog_version=CATALOG_VERSION,
            decision_as_of=DECISION_AS_OF,
            schema_major=2,
            snapshot_sha256=snapshot.snapshot_sha256,
            probe_receipt_sha256=probe_receipt_sha256,
            observation_receipt_sha256=str(observation_receipt["receipt_sha256"]),
            observation_membership_sha256=membership.content_sha256,
            observation_transaction_complete_sha256=_sha256(
                [
                    "transaction-complete",
                    snapshot.snapshot_sha256,
                    observation_receipt["receipt_sha256"],
                    membership.content_sha256,
                ]
            ),
            historical_pit_eligible=False,
            historical_feature_claims=(),
            observation_eligible=False,
            ranking_eligible=False,
            planning_eligible=False,
            execution_evidence_eligible=False,
            blockers=(
                "champion_numeric_features_unavailable",
                "minute_execution_evidence_unavailable",
            ),
            observation_membership=membership,
            schema_id=ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID,
        )
    )


def _history(session_count: int) -> AshareObservationHistoryReadiness:
    blockers = (
        "trading_session_continuity_authority_unavailable",
        "corporate_action_adjustment_authority_unavailable",
        *(() if session_count >= 21 else ("insufficient_prospective_sessions",)),
    )
    feature_readiness = tuple(
        AshareObservationFeatureReadiness(
            feature_id=feature_id,
            history_mode=PROSPECTIVE_OBSERVATION_HISTORY,
            required_sessions=21,
            observed_sessions=session_count,
            eligible=False,
            blockers=blockers,
        )
        for feature_id in ("momentum_20d", "low_volatility_20d", "adv_20d")
    )
    return AshareObservationHistoryReadiness(
        history_mode=PROSPECTIVE_OBSERVATION_HISTORY,
        session_count=session_count,
        min_required_sessions=21,
        history_identity_sha256=_sha256(["history", session_count]),
        prospective_history_eligible=False,
        feature_readiness=feature_readiness,
        coverage=AshareObservationHistoryCoverage(
            target_symbol=TARGET_SYMBOL,
            expected_session_count=session_count,
            complete_session_count=session_count,
            coverage_ratio=1.0,
            incomplete_sessions=(),
            missing_sessions=(),
            duplicate_row_sessions=(),
            invalid_value_sessions=(),
        ),
        blockers=blockers,
        schema_id=ASHARE_OBSERVATION_HISTORY_READINESS_SCHEMA_ID,
    )


def _decision(session_count: int = 19):
    return build_ashare_daily_planning_decision(
        runtime_authority=_runtime_authority(),
        history_readiness=_history(session_count),
    )


def test_nineteen_sessions_publish_only_a_deterministic_abstention() -> None:
    first = _decision(19)
    second = _decision(19)

    assert first == second
    assert first.schema_id == ASHARE_PAPER_PLANNING_DECISION_SCHEMA_ID
    assert first.observation_session == SESSION_DATE
    assert first.paper_trade_session is None
    assert first.status == "completed_with_blocks"
    assert first.action == "abstain"
    assert first.disposition == "observation_only"
    assert first.authority == "non_authority"
    assert first.simulation_only is True
    assert first.real_trading_enabled is False
    assert first.blockers == tuple(sorted(first.blockers))
    assert first.blockers == (
        "champion_numeric_features_unavailable",
        "corporate_action_adjustment_authority_unavailable",
        "insufficient_prospective_sessions",
        "minute_execution_evidence_unavailable",
        "next_trade_session_authority_unavailable",
        "trading_session_continuity_authority_unavailable",
    )
    assert first.snapshot_sha256 == _runtime_authority().snapshot_sha256
    assert first.probe_receipt_sha256 == _runtime_authority().probe_receipt_sha256
    assert (
        first.observation_receipt_sha256
        == _runtime_authority().observation_receipt_sha256
    )
    assert (
        first.observation_transaction_complete_sha256
        == _runtime_authority().observation_transaction_complete_sha256
    )
    assert first.history_identity_sha256 == _history(19).history_identity_sha256
    assert first.canonical_bytes() == second.canonical_bytes()
    assert hashlib.sha256(first.canonical_bytes()).hexdigest() == first.decision_sha256
    assert len(first.decision_id) == 64


def test_twenty_one_sessions_still_abstain_without_scientific_authorities() -> None:
    decision = _decision(21)

    assert decision.history_session_count == 21
    assert decision.blockers == (
        "champion_numeric_features_unavailable",
        "corporate_action_adjustment_authority_unavailable",
        "minute_execution_evidence_unavailable",
        "next_trade_session_authority_unavailable",
        "trading_session_continuity_authority_unavailable",
    )
    assert decision.status == "completed_with_blocks"
    assert decision.action == "abstain"
    assert decision.disposition == "observation_only"
    assert decision.authority == "non_authority"


def test_planner_rejects_twenty_close_requirement_for_twenty_day_returns() -> None:
    history = _history(21)
    weakened = replace(
        history,
        min_required_sessions=20,
        feature_readiness=tuple(
            replace(feature, required_sessions=20)
            for feature in history.feature_readiness
        ),
    )

    with pytest.raises(
        AsharePaperPlanningContractError,
        match="ashare_paper_planning_history_count_invalid",
    ):
        build_ashare_daily_planning_decision(
            runtime_authority=_runtime_authority(),
            history_readiness=weakened,
        )


def test_decision_contract_has_no_predictive_portfolio_or_execution_fields() -> None:
    names = {item.name for item in fields(type(_decision()))}
    forbidden = {
        "probability",
        "expected_return",
        "return_quantiles",
        "target_weight",
        "quantity",
        "orders",
        "fills",
        "capital",
        "cash",
        "positions",
        "reconciliation",
    }

    assert names.isdisjoint(forbidden)
    assert not hasattr(_decision(), "probability")
    assert not hasattr(_decision(), "orders")
    assert not hasattr(_decision(), "capital")


@pytest.mark.parametrize(
    "authority",
    (
        object(),
        replace(_runtime_authority(), observation_eligible=False),
    ),
)
def test_invalid_or_mismatched_runtime_authority_is_rejected(authority: object) -> None:
    with pytest.raises(AsharePaperPlanningContractError):
        build_ashare_daily_planning_decision(
            runtime_authority=authority,  # type: ignore[arg-type]
            history_readiness=_history(19),
        )


def test_caller_flipped_eligibility_without_loader_capability_is_rejected() -> None:
    forged = replace(_runtime_authority(), observation_eligible=False)
    object.__setattr__(forged, "observation_eligible", True)

    assert forged.committed_state_verified is False
    with pytest.raises(
        AsharePaperPlanningContractError,
        match="ashare_paper_planning_runtime_authority_scope_invalid",
    ):
        build_ashare_daily_planning_decision(
            runtime_authority=forged,
            history_readiness=_history(19),
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"snapshot_sha256": "f" * 64},
        {"planning_eligible": True},
        {"observation_membership_sha256": None},
        {"observation_transaction_complete_sha256": None},
    ),
)
def test_runtime_bundle_itself_rejects_forged_eligible_authority(
    changes: dict[str, object],
) -> None:
    with pytest.raises(
        ValueError,
        match="observation_eligible_requires_verified_committed_state",
    ):
        replace(_runtime_authority(), **changes)


@pytest.mark.parametrize(
    "history",
    (
        object(),
        replace(_history(19), history_identity_sha256=None),
        replace(
            _history(19),
            coverage=replace(_history(19).coverage, expected_session_count=18),
        ),
        replace(_history(21), prospective_history_eligible=True),
    ),
)
def test_invalid_or_mismatched_history_identity_is_rejected(history: object) -> None:
    with pytest.raises(AsharePaperPlanningContractError):
        build_ashare_daily_planning_decision(
            runtime_authority=_runtime_authority(),
            history_readiness=history,  # type: ignore[arg-type]
        )


def test_store_is_explicit_side_effect_free_until_first_compare_and_swap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper-planning"
    store = FileAsharePaperPlanningStore(root)

    assert root.is_absolute()
    assert not root.exists()
    assert store.load(observation_session=SESSION_DATE) is None
    assert not root.exists()

    with pytest.raises(ValueError, match="absolute"):
        FileAsharePaperPlanningStore(Path("relative-root"))
    with pytest.raises(ValueError, match="explicit"):
        FileAsharePaperPlanningStore("")


def test_store_publishes_content_addressed_artifact_and_immutable_day_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper-planning"
    store = FileAsharePaperPlanningStore(root)
    decision = _decision(19)

    store.compare_and_swap(decision=decision, expected_decision_sha256=None)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    recovered = store.load(observation_session=SESSION_DATE)
    store.compare_and_swap(decision=decision, expected_decision_sha256=None)
    after = {path.name: path.read_bytes() for path in root.iterdir()}

    assert recovered == decision
    assert before == after
    artifacts = list(root.glob("artifact-*.json"))
    bindings = list(root.glob("observation-*.json"))
    assert len(artifacts) == 1
    assert len(bindings) == 1
    assert decision.decision_sha256 in artifacts[0].name
    assert SESSION_DATE in bindings[0].name
    for path in (*artifacts, *bindings, *root.glob("*.lock")):
        assert path.is_file()
        assert path.stat().st_nlink == 1
        assert path.stat().st_mode & 0o777 == 0o600


def test_store_rejects_same_day_content_conflict_and_bad_cas(tmp_path: Path) -> None:
    store = FileAsharePaperPlanningStore(tmp_path / "paper-planning")
    current = _decision(19)
    conflicting_history = replace(
        _history(19),
        history_identity_sha256=_sha256("different-valid-history-identity"),
    )
    conflict = build_ashare_daily_planning_decision(
        runtime_authority=_runtime_authority(),
        history_readiness=conflicting_history,
    )
    store.compare_and_swap(decision=current, expected_decision_sha256=None)

    with pytest.raises(AsharePaperPlanningStoreConflict, match="compare_and_swap"):
        store.compare_and_swap(
            decision=current,
            expected_decision_sha256="f" * 64,
        )
    with pytest.raises(AsharePaperPlanningStoreConflict, match="immutable_day"):
        store.compare_and_swap(
            decision=conflict,
            expected_decision_sha256=current.decision_sha256,
        )


def test_store_rejects_corruption_bad_mode_hardlink_and_symlink(
    tmp_path: Path,
) -> None:
    decision = _decision()

    corrupt_root = tmp_path / "corrupt"
    corrupt = FileAsharePaperPlanningStore(corrupt_root)
    corrupt.compare_and_swap(decision=decision, expected_decision_sha256=None)
    artifact = next(corrupt_root.glob("artifact-*.json"))
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(AsharePaperPlanningStoreCorruption):
        corrupt.load(observation_session=SESSION_DATE)

    mode_root = tmp_path / "mode"
    mode = FileAsharePaperPlanningStore(mode_root)
    mode.compare_and_swap(decision=decision, expected_decision_sha256=None)
    mode_artifact = next(mode_root.glob("artifact-*.json"))
    mode_artifact.chmod(0o644)
    with pytest.raises(AsharePaperPlanningStoreCorruption, match="mode"):
        mode.load(observation_session=SESSION_DATE)

    linked_root = tmp_path / "hardlink"
    linked = FileAsharePaperPlanningStore(linked_root)
    linked.compare_and_swap(decision=decision, expected_decision_sha256=None)
    binding = next(linked_root.glob("observation-*.json"))
    os.link(binding, tmp_path / "binding-alias.json")
    with pytest.raises(AsharePaperPlanningStoreCorruption, match="hardlink"):
        linked.load(observation_session=SESSION_DATE)

    target_root = tmp_path / "target"
    target_root.mkdir()
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(target_root, target_is_directory=True)
    with pytest.raises(AsharePaperPlanningStoreCorruption, match="symlink"):
        FileAsharePaperPlanningStore(symlink_root)

    binding_root = tmp_path / "symlink-binding"
    symlinked = FileAsharePaperPlanningStore(binding_root)
    symlinked.compare_and_swap(decision=decision, expected_decision_sha256=None)
    binding = next(binding_root.glob("observation-*.json"))
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(binding.read_bytes())
    binding.unlink()
    binding.symlink_to(replacement)
    with pytest.raises(AsharePaperPlanningStoreCorruption, match="symlink"):
        symlinked.load(observation_session=SESSION_DATE)


def test_concurrent_writers_are_idempotent_or_fail_closed_on_day_conflict(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper-planning"
    first = _decision(19)
    second = build_ashare_daily_planning_decision(
        runtime_authority=_runtime_authority(),
        history_readiness=replace(
            _history(19),
            history_identity_sha256=_sha256("concurrent-second-history"),
        ),
    )

    def write(decision: object) -> str:
        try:
            FileAsharePaperPlanningStore(root).compare_and_swap(
                decision=decision,  # type: ignore[arg-type]
                expected_decision_sha256=None,
            )
        except AsharePaperPlanningStoreConflict:
            return "conflict"
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, [first, second] * 8))

    assert "ok" in results
    assert "conflict" in results
    recovered = FileAsharePaperPlanningStore(root).load(
        observation_session=SESSION_DATE
    )
    assert recovered in {first, second}
    assert len(list(root.glob("artifact-*.json"))) == 1
    assert len(list(root.glob("observation-*.json"))) == 1


def test_module_has_no_forbidden_runtime_or_side_effect_imports() -> None:
    source_path = (
        Path(__file__).parents[1] / "shared" / "runtime" / "ashare_paper_planning.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_prefixes = (
        "shared.data.tradingdatas_transport",
        "shared.runtime.day_loop",
        "shared.runtime.file_store",
        "shared.runtime.llm",
        "shared.runtime.marketgraph",
        "shared.execution",
        "shared.capital",
        "shared.review.ashare.sample_journal",
        "socket",
        "urllib",
        "requests",
        "httpx",
    )

    assert not any(
        name == prefix or name.startswith(f"{prefix}.")
        for name in imported
        for prefix in forbidden_prefixes
    )
