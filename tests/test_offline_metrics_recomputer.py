from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from shared.review.sample_journal import FrozenJournalView, SampleJournal
from shared.review.offline_science import (
    _block_bootstrap,
    _moving_observed_trade_date_blocks,
    recompute_offline_metrics,
    verify_offline_metrics_report,
)
from shared.review.outcome_evaluation import canonical_sha256
from shared.runtime_test.ashare_offline_science import (
    AshareOfflineScienceError,
    run_offline_science,
    verify_offline_science_bundle,
)
from shared.runtime_test.ashare_forward_label_ops import (
    load_validation_plan_artifact_with_provenance,
)

from tests.test_outcome_evaluation import (
    AUTHORITY,
    MARKET_TRUTH_VERIFIER,
    PLAN_PROVENANCE_VERIFIER,
    TRUSTED_PLAN_PROVENANCE,
    VALIDATION_PLAN,
    build_outcome_evaluation,
    forward_label_update,
    prediction,
)
from tests._ashare_validation_plan_fixture import (
    write_non_production_validation_plan_artifact,
)


def _overlapping_rows(*, days: int, horizon: str = "5d") -> list[dict[str, object]]:
    return [
        {
            "decision_cluster_id": f"cluster:{index}",
            "trade_date": f"202607{index + 1:02d}",
            "primary_horizon": horizon,
            "label": {"net_return_after_costs": 0.01 if index % 2 == 0 else -0.005},
        }
        for index in range(days)
    ]


def _metrics(events, outcomes, *, iterations=200):
    return recompute_offline_metrics(
        events=events,
        outcome_report=outcomes,
        expected_as_of=outcomes["as_of"],
        expected_authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
        bootstrap_iterations=iterations,
    )


def _verify_metrics(report, events, outcomes, *, iterations=200):
    return verify_offline_metrics_report(
        report,
        events=events,
        outcome_report=outcomes,
        expected_as_of=outcomes["as_of"],
        expected_authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
        bootstrap_iterations=iterations,
    )


def test_five_day_overlap_uses_contiguous_observed_date_blocks() -> None:
    rows = _overlapping_rows(days=10)

    grouped, blocks, excluded = _moving_observed_trade_date_blocks(
        rows,
        block_length=5,
    )
    result = _block_bootstrap(
        rows,
        iterations=50,
        seed_material="a" * 64,
        propensity_weight_kish_n_eff=10.0,
    )

    assert excluded == 0
    assert sorted(grouped) == [f"202607{index:02d}" for index in range(1, 11)]
    assert len(blocks) == 6
    assert blocks[0] == tuple(f"202607{index:02d}" for index in range(1, 6))
    assert blocks[-1] == tuple(f"202607{index:02d}" for index in range(6, 11))
    assert result["method"] == "moving_observed_trade_date_block_bootstrap.v1"
    assert result["inference_status"] == "available"
    assert result["block_length_trading_days"] == 5
    assert result["observed_trading_day_count"] == 10
    assert result["candidate_contiguous_block_count"] == 6
    assert result["dependence_adjusted_sample_count"] == 2


def test_overlap_inference_is_unavailable_without_two_full_blocks() -> None:
    result = _block_bootstrap(
        _overlapping_rows(days=9),
        iterations=50,
        seed_material="b" * 64,
        propensity_weight_kish_n_eff=9.0,
    )

    assert result["block_length_trading_days"] == 5
    assert result["inference_status"] == (
        "unavailable_insufficient_contiguous_date_blocks"
    )
    assert result["mean_ci_90"] == {"lower": None, "upper": None}
    assert result["probability_mean_positive"] is None


def test_mixed_horizons_take_longest_block_and_invalid_dates_are_excluded() -> None:
    rows = _overlapping_rows(days=10, horizon="1d")
    rows[-1]["primary_horizon"] = "5d"
    rows.append(
        {
            "decision_cluster_id": "cluster:invalid-date",
            "trade_date": "unknown",
            "primary_horizon": "5d",
            "label": {"net_return_after_costs": 0.5},
        }
    )

    result = _block_bootstrap(
        rows,
        iterations=50,
        seed_material="c" * 64,
        propensity_weight_kish_n_eff=11.0,
    )

    assert result["block_length_trading_days"] == 5
    assert result["observed_trading_day_count"] == 10
    assert result["excluded_inference_row_count"] == 1
    assert result["dependence_adjusted_sample_count"] == 2


def test_metrics_are_deterministic_cluster_based_and_reuse_canonical_kpi() -> None:
    first_prediction = prediction(snapshot_id="prediction:one", net_return=None)
    second_prediction = prediction(
        snapshot_id="prediction:two", symbol="600001.SH", net_return=None
    )
    second_prediction["decision_cluster_id"] = "decision:two"
    events = [
        first_prediction,
        forward_label_update(first_prediction, net_return=0.02),
        second_prediction,
        forward_label_update(second_prediction, net_return=-0.01),
    ]
    outcomes = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )

    first = _metrics(events, outcomes)
    second = _metrics(deepcopy(events), deepcopy(outcomes))

    assert first == second
    assert _verify_metrics(first, events, outcomes) is True
    assert first["eligible_unique_decision_cluster_count"] == 2
    assert first["eligible_unambiguous_decision_cluster_count"] == 2
    assert first["observed_trading_day_count"] == 1
    assert first["unique_decision_cluster_count"] == 2
    assert first["propensity_weight_kish_n_eff"] == 2.0
    assert first["dependence_adjusted_sample_count"] == 1
    assert first["performance"]["bootstrap"]["inference_status"] == (
        "unavailable_insufficient_contiguous_date_blocks"
    )
    assert first["performance"]["mean_net_return_after_costs"] == 0.005
    assert first["performance"]["positive_rate"] == 0.5
    assert (
        first["canonical_sample_kpi"]["sample_size_evidence"][
            "unique_decision_cluster_count"
        ]
        == 2
    )
    assert first["authority"]["automatic_promotion_enabled"] is False


def test_empty_metrics_are_explicitly_unavailable() -> None:
    outcomes = build_outcome_evaluation(
        [prediction(net_return=None)],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    report = _metrics([prediction(net_return=None)], outcomes, iterations=50)
    assert report["performance"]["status"] == "unavailable_no_ready_outcomes"
    assert report["performance"]["mean_net_return_after_costs"] is None
    assert report["performance"]["expected_shortfall_q10"] is None
    assert report["unique_decision_cluster_count"] == 0
    assert report["eligible_unique_decision_cluster_count"] == 0
    assert report["eligible_unambiguous_decision_cluster_count"] == 0
    assert report["propensity_weight_kish_n_eff"] == 0.0
    assert report["dependence_adjusted_sample_count"] == 0


def test_foreign_label_update_cannot_pollute_kish_or_performance() -> None:
    base = prediction(net_return=None)
    valid = forward_label_update(base, net_return=0.02)
    baseline_events = [base, valid]
    baseline_outcomes = build_outcome_evaluation(
        baseline_events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    baseline = _metrics(baseline_events, baseline_outcomes)

    foreign = forward_label_update(
        base,
        net_return=0.99,
        authority={
            "capital_authority_id": "foreign-capital",
            "authority_generation": 999,
            "execution_lineage_id": "foreign-lineage",
        },
    )
    attacked_events = [base, valid, foreign]
    attacked_outcomes = build_outcome_evaluation(
        attacked_events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    attacked = _metrics(attacked_events, attacked_outcomes)

    assert attacked_outcomes["ignored_invalid_forward_label_update_snapshot_count"] == 1
    assert (
        attacked["propensity_weight_kish_n_eff"]
        == baseline["propensity_weight_kish_n_eff"]
    )
    assert attacked["performance"] == baseline["performance"]


def test_rehashed_offline_metrics_tamper_is_rejected_against_exact_sources() -> None:
    base = prediction(net_return=None)
    events = [base, forward_label_update(base)]
    outcomes = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    report = _metrics(events, outcomes)
    tampered = deepcopy(report)
    tampered["performance"]["mean_net_return_after_costs"] = 0.99
    unsigned = deepcopy(tampered)
    unsigned.pop("report_sha256")
    tampered["report_sha256"] = canonical_sha256(unsigned)

    with pytest.raises(
        ValueError,
        match="offline_metrics_do_not_match_exact_sources",
    ):
        _verify_metrics(tampered, events, outcomes)


def test_cli_runner_reads_frozen_view_and_only_writes_external_projection(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "absent-journal.jsonl"
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(json.dumps(AUTHORITY), encoding="utf-8")
    validation_plan_path = write_non_production_validation_plan_artifact(
        tmp_path / "validation-plan.json"
    )

    result = run_offline_science(
        journal_path=journal_path,
        authority_manifest_path=authority_path,
        validation_plan_path=validation_plan_path,
        as_of="2026-07-18T00:00:00+08:00",
        output_root=tmp_path / "projections",
        bootstrap_iterations=50,
    )

    output_dir = Path(result["output_dir"])
    assert journal_path.exists() is False
    assert {path.name for path in output_dir.iterdir()} == {
        "outcome_evaluation.json",
        "counterfactual_books.json",
        "offline_metrics.json",
        "calibration_ablation.json",
        "run_receipt.json",
    }
    assert result["journal_write_count"] == 0
    assert result["network_call_count"] == 0
    assert result["automatic_promotion_enabled"] is False
    assert result["validation_plan_binding"] == {
        "validation_plan_sha256": VALIDATION_PLAN.sha256(),
        "trading_session_calendar_sha256": (
            VALIDATION_PLAN.trading_session_calendar.calendar_sha256
        ),
        "trading_session_calendar_verification_proof_sha256": (
            VALIDATION_PLAN.trading_session_calendar_verification.proof_sha256
        ),
    }
    artifacts = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in output_dir.iterdir()
    }
    plan, provenance = load_validation_plan_artifact_with_provenance(
        validation_plan_path
    )
    frozen_view = SampleJournal(journal_path).read_frozen(
        as_of=result["journal_view"]["data_as_of"]
    )
    assert (
        verify_offline_science_bundle(
            artifacts,
            frozen_view=frozen_view,
            authority=AUTHORITY,
            validation_plan=plan,
            validation_plan_provenance=provenance,
            expected_as_of=result["as_of"],
            bootstrap_iterations=50,
        )
        is True
    )

    tampered = deepcopy(artifacts)
    tampered["offline_metrics.json"]["dependence_adjusted_sample_count"] = 999
    with pytest.raises(
        AshareOfflineScienceError,
        match="offline_science_bundle_exact_source_mismatch",
    ):
        verify_offline_science_bundle(
            tampered,
            frozen_view=frozen_view,
            authority=AUTHORITY,
            validation_plan=plan,
            validation_plan_provenance=provenance,
            expected_as_of=result["as_of"],
            bootstrap_iterations=50,
        )

    forged_head = replace(frozen_view, journal_head_sha256="0" * 64)
    with pytest.raises(
        AshareOfflineScienceError,
        match="frozen_journal_view_integrity_invalid",
    ):
        verify_offline_science_bundle(
            artifacts,
            frozen_view=forged_head,
            authority=AUTHORITY,
            validation_plan=plan,
            validation_plan_provenance=provenance,
            expected_as_of=result["as_of"],
            bootstrap_iterations=50,
        )

    class ForgedFrozenJournalView(FrozenJournalView):
        pass

    subclass_view = ForgedFrozenJournalView(**frozen_view.__dict__)
    with pytest.raises(
        AshareOfflineScienceError,
        match="frozen_journal_view_required",
    ):
        verify_offline_science_bundle(
            artifacts,
            frozen_view=subclass_view,
            authority=AUTHORITY,
            validation_plan=plan,
            validation_plan_provenance=provenance,
            expected_as_of=result["as_of"],
            bootstrap_iterations=50,
        )

    with pytest.raises(
        AshareOfflineScienceError,
        match="frozen_journal_view_as_of_mismatch",
    ):
        verify_offline_science_bundle(
            artifacts,
            frozen_view=frozen_view,
            authority=AUTHORITY,
            validation_plan=plan,
            validation_plan_provenance=provenance,
            expected_as_of="2026-07-17T23:59:59+08:00",
            bootstrap_iterations=50,
        )

    (output_dir / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(
        AshareOfflineScienceError,
        match="existing_run_artifact_set_mismatch",
    ):
        run_offline_science(
            journal_path=journal_path,
            authority_manifest_path=authority_path,
            validation_plan_path=validation_plan_path,
            as_of="2026-07-18T00:00:00+08:00",
            output_root=tmp_path / "projections",
            bootstrap_iterations=50,
        )

    with pytest.raises(
        AshareOfflineScienceError, match="output_root_must_be_outside_repository"
    ):
        run_offline_science(
            journal_path=journal_path,
            authority_manifest_path=authority_path,
            validation_plan_path=validation_plan_path,
            as_of="2026-07-18T00:00:00+08:00",
            output_root=Path(__file__).resolve().parent,
            bootstrap_iterations=50,
        )
