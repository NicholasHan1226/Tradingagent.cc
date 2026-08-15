from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from Ashare.evolution_controller import build_evolution_decision
from Ashare.promotion_executor import (
    PromotionExecutionError,
    execute_automatic_promotion,
)
from shared.models.champion_registry import (
    ChampionRegistryError,
    ChampionSelectionRegistry,
)
from shared.models.lifecycle import ValidationPlan
from tests._ashare_validation_plan_fixture import (
    build_non_production_ashare_validation_plan,
)


NOW = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)

AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}


def _ready_decision(**overrides: object) -> dict[str, object]:
    sample_kpi: dict[str, object] = {
        "report_type": "sample_journal_kpi",
        "evidence_source": "sample_journal_kpi",
        "market": "ashare",
        "trade_date": "20260815",
        "authority_scope": dict(AUTHORITY),
        "journal_event_count": 80,
        "sample_size_evidence": {
            "raw_N": 24,
            "unique_decision_cluster_count": 20,
            "independent_trading_day_count": 10,
            "N_eff": 20.0,
        },
        "styles": {
            "trend_breakout": {
                "prediction_count": 24,
                "exploration_fill_count": 3,
                "exploitation_fill_count": 0,
                "completed_round_trip_count": 10,
                "post_cost_pnl_cny": 120.0,
                "forward_label_counts": {"5d": {"ready": 15}},
            }
        },
        "scientific_evidence": {
            "point_in_time_lineage_complete": True,
            "costs_evidence_complete": True,
            "fill_evidence_revalidated": True,
            "duplicate_cluster_control_passed": True,
            "calibration_evidence_sufficient": True,
            "promotion_evidence_ready": True,
        },
    }
    sample_kpi.update(overrides)
    decision = build_evolution_decision(
        sample_kpi,
        authority_scope=AUTHORITY,
        target_trade_date="20260815",
    )
    assert decision["promotion_evidence_ready"] is True
    return decision


def _validation_plan() -> ValidationPlan:
    return ValidationPlan(
        train_start=date(2025, 1, 1),
        train_end=date(2025, 1, 31),
        validation_start=date(2025, 2, 10),
        validation_end=date(2025, 2, 28),
        test_start=date(2025, 3, 10),
        test_end=date(2025, 3, 31),
        purge_days=5,
        embargo_days=5,
        label_horizon_days=5,
        max_feature_lookback_days=5,
        event_cluster_embargo_days=5,
        decision_cluster_key="decision_cluster_id",
        decision_cluster_deduplicated=True,
        registered_trial_count=1,
        multiple_testing_trial_budget=20,
        pbo_required=True,
        deflated_sharpe_required=True,
        oos_reuse_count=0,
        max_oos_reuse_count=1,
        oos_used_for_tuning=False,
        oos_authority_receipt_sha256="d" * 64,
        experiment_family_id="ashare-challenger-family",
        experiment_id="ashare-challenger-experiment",
        frozen_test_set_id="ashare-challenger-oos",
        frozen_at=NOW - timedelta(days=1),
    )


def _challenger(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "challenger_id": "trend_breakout_strength_continuation",
        "challenger_version": "2.0.0",
        "artifact_sha256": "a" * 64,
        "training_data_version": "training-20260815",
        "feature_contract_version": "features-v1",
        "research_snapshot_sha256": "b" * 64,
        "catalog_version": "catalog-v1",
        "validation_evidence_sha256": "c" * 64,
        "source_commit": "source-20260815",
        "created_by": "offline-learning",
        "validation_plan": _validation_plan(),
    }
    candidate.update(overrides)
    return candidate


def test_evidence_ready_decision_promotes_challenger_into_registry(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "champion_registry"
    decision = _ready_decision()

    result = execute_automatic_promotion(
        decision,
        registry_root=registry_root,
        challenger_candidates=[_challenger()],
        recorded_at=NOW,
    )

    assert result["status"] == "promoted"
    assert result["actor"] == "automation"
    assert result["selected_model_id"] == "trend_breakout_strength_continuation"
    assert result["selected_model_version"] == "2.0.0"
    assert result["promotion_evidence_reference"].startswith("promotion-evidence:")
    assert result["simulation_only"] is True
    assert result["real_trading_enabled"] is False
    assert result["live_transition_authorized"] is False
    assert result["automatic_risk_expansion_enabled"] is False

    registry = ChampionSelectionRegistry(registry_root)
    current = registry.load_current()
    assert current.receipt_sha256 == result["receipt_sha256"]
    assert current.selected_manifest_sha256 == result["selected_manifest_sha256"]
    assert current.human_approval_reference == result["promotion_evidence_reference"]
    assert current.automatic_promotion_enabled is True
    assert current.real_trading_enabled is False
    assert current.live_transition_authorized is False
    assert current.automatic_risk_expansion_enabled is False
    assert current.simulation_only is True


def test_promotion_replay_is_idempotent_per_decision_and_challenger(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "champion_registry"
    decision = _ready_decision()

    first = execute_automatic_promotion(
        decision,
        registry_root=registry_root,
        challenger_candidates=[_challenger()],
        recorded_at=NOW,
    )
    replay = execute_automatic_promotion(
        decision,
        registry_root=registry_root,
        challenger_candidates=[_challenger()],
        recorded_at=NOW,
    )

    assert first["status"] == "promoted"
    assert replay["status"] == "already_promoted"
    assert replay["receipt_sha256"] == first["receipt_sha256"]
    assert len(ChampionSelectionRegistry(registry_root).load_history()) == 1


def test_decision_without_ready_evidence_is_an_explicit_no_op(
    tmp_path: Path,
) -> None:
    decision = _ready_decision()
    decision["promotion_evidence_ready"] = False
    registry_root = tmp_path / "champion_registry"

    result = execute_automatic_promotion(
        decision,
        registry_root=registry_root,
        challenger_candidates=[_challenger()],
        recorded_at=NOW,
    )

    assert result["status"] == "no_op"
    assert result["reason"] == "promotion_evidence_not_ready"
    assert not registry_root.exists()


def test_missing_or_unqualified_challenger_is_an_explicit_no_op(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "champion_registry"
    decision = _ready_decision()

    empty = execute_automatic_promotion(
        decision,
        registry_root=registry_root,
        challenger_candidates=[],
        recorded_at=NOW,
    )
    assert empty["status"] == "no_op"
    assert empty["reason"] == "no_qualified_challenger"
    assert empty["challenger_rejections"] == []

    unqualified = execute_automatic_promotion(
        decision,
        registry_root=registry_root,
        challenger_candidates=[_challenger(artifact_sha256="not-a-sha256")],
        recorded_at=NOW,
    )
    assert unqualified["status"] == "no_op"
    assert unqualified["reason"] == "no_qualified_challenger"
    assert unqualified["challenger_rejections"] == [
        "challenger_artifact_sha256_invalid"
    ]
    assert not registry_root.exists()


def test_sample_ops_executes_and_publishes_evidence_ready_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.runtime_test.ashare_sample_ops as sample_ops

    class _EmptyReader:
        def get_bars_intraday(self, market, symbol, interval, start, end):
            return []

        def get_bars_daily(self, market, symbol, start, end):
            return []

    decision = _ready_decision()
    monkeypatch.setattr(
        sample_ops,
        "build_evolution_decision",
        lambda *args, **kwargs: dict(decision),
    )
    review_dir = tmp_path / "review"

    report = sample_ops.run_ashare_sample_ops(
        journal_path=tmp_path / "sample_journal.jsonl",
        trade_date="20260815",
        as_of="2026-08-15T16:00:00+08:00",
        review_dir=review_dir,
        reader=_EmptyReader(),
        environ={},
        validation_plan=build_non_production_ashare_validation_plan(),
        challenger_candidates=[_challenger()],
    )

    execution = report["evolution_decision"]["promotion_execution"]
    assert execution["status"] == "promoted"
    assert execution["actor"] == "automation"
    assert report["real_trading_enabled"] is False
    assert report["live_execution_enabled"] is False

    registry = ChampionSelectionRegistry(review_dir / "champion_registry")
    current = registry.load_current()
    assert current.receipt_sha256 == execution["receipt_sha256"]
    assert current.automatic_promotion_enabled is True
    assert current.real_trading_enabled is False

    published = json.loads(
        (review_dir / "evolution_decision_latest.json").read_text(encoding="utf-8")
    )
    assert published["promotion_execution"]["status"] == "promoted"
    # Canonical projection publisher contract: top-level safety fields stay off.
    assert published["automatic_promotion_enabled"] is False
    assert published["real_trading_enabled"] is False
    assert published["live_transition_authorized"] is False
    assert published["policy"]["automatic_promotion_enabled"] is True


def test_sample_ops_records_no_op_when_no_challenger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.runtime_test.ashare_sample_ops as sample_ops

    class _EmptyReader:
        def get_bars_intraday(self, market, symbol, interval, start, end):
            return []

        def get_bars_daily(self, market, symbol, start, end):
            return []

    decision = _ready_decision()
    monkeypatch.setattr(
        sample_ops,
        "build_evolution_decision",
        lambda *args, **kwargs: dict(decision),
    )
    review_dir = tmp_path / "review"

    report = sample_ops.run_ashare_sample_ops(
        journal_path=tmp_path / "sample_journal.jsonl",
        trade_date="20260815",
        as_of="2026-08-15T16:00:00+08:00",
        review_dir=review_dir,
        reader=_EmptyReader(),
        environ={},
        validation_plan=build_non_production_ashare_validation_plan(),
    )

    execution = report["evolution_decision"]["promotion_execution"]
    assert execution["status"] == "no_op"
    assert execution["reason"] == "no_qualified_challenger"
    assert not (review_dir / "champion_registry").exists()


def test_unsafe_decision_flags_fail_closed(tmp_path: Path) -> None:
    decision = _ready_decision()
    decision["real_trading_enabled"] = True

    with pytest.raises(
        PromotionExecutionError,
        match="not_simulation_only",
    ):
        execute_automatic_promotion(
            decision,
            registry_root=tmp_path / "champion_registry",
            challenger_candidates=[_challenger()],
            recorded_at=NOW,
        )


def test_automation_receipt_still_rejects_real_trading_flag(tmp_path: Path) -> None:
    registry_root = tmp_path / "champion_registry"
    result = execute_automatic_promotion(
        _ready_decision(),
        registry_root=registry_root,
        challenger_candidates=[_challenger()],
        recorded_at=NOW,
    )
    assert result["status"] == "promoted"

    receipt_path = next((registry_root / "receipts").iterdir())
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["real_trading_enabled"] = True
    unsigned = dict(payload)
    unsigned.pop("receipt_sha256")
    encoded = json.dumps(
        unsigned,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload["receipt_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    receipt_path.chmod(0o600)
    tampered_path = receipt_path.with_name(
        f"{payload['sequence']:020d}-{payload['receipt_sha256']}.json"
    )
    receipt_path.rename(tampered_path)
    tampered_path.write_text(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ChampionRegistryError, match="simulation_only"):
        ChampionSelectionRegistry(registry_root).load_history()
