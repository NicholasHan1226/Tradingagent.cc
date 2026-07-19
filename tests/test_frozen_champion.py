from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

import shared.portfolio.champion as champion_module

from shared.portfolio.champion import (
    ChampionContractError,
    FrozenChampionSpec,
    ShadowChallenger,
    cash_baseline,
    score_with_champion,
)


FEATURES = (
    "quality_score",
    "value_score",
    "momentum_score",
    "low_volatility_score",
)


def _champion() -> FrozenChampionSpec:
    return FrozenChampionSpec(
        champion_id="ashare-mainboard-rank-v1",
        version="1.0.0",
        feature_names=FEATURES,
        feature_weights=(0.30, 0.20, 0.30, 0.20),
        decision_horizon="5d",
        trained_through="2026-06-30",
    )


def test_champion_requires_four_to_eight_non_llm_features() -> None:
    with pytest.raises(ChampionContractError, match="feature_count_must_be_4_to_8"):
        FrozenChampionSpec(
            champion_id="bad",
            version="1",
            feature_names=("a", "b", "c"),
            feature_weights=(0.3, 0.3, 0.4),
            decision_horizon="5d",
            trained_through="2026-06-30",
        )
    with pytest.raises(
        ChampionContractError,
        match="feature_not_in_allowed_namespace",
    ):
        FrozenChampionSpec(
            champion_id="bad",
            version="1",
            feature_names=("quality", "value", "llm_belief", "momentum"),
            feature_weights=(0.25, 0.25, 0.25, 0.25),
            decision_horizon="5d",
            trained_through="2026-06-30",
        )


def test_champion_is_immutable_and_has_stable_manifest_hash() -> None:
    champion = _champion()
    same = _champion()
    assert champion.manifest_sha256 == same.manifest_sha256
    with pytest.raises(FrozenInstanceError):
        champion.version = "2"  # type: ignore[misc]


def test_score_is_rank_only_and_never_masquerades_as_probability() -> None:
    result = _score(
        feature_values={
            "quality_score": 0.8,
            "value_score": 0.6,
            "momentum_score": 0.9,
            "low_volatility_score": 0.7,
        },
    )
    assert result.rank_score == pytest.approx(0.77)
    assert result.score_semantics == "uncalibrated_deterministic_rank_score"
    assert result.calibrated_probability is None
    assert result.calibration_status == "not_calibrated"
    assert result.execution_eligible is True


def test_exact_feature_contract_fails_closed() -> None:
    champion = _champion()
    selection = _selection_context(champion)
    missing_snapshot = _feature_snapshot(
        champion,
        feature_values={
            "quality_score": 0.8,
            "value_score": 0.6,
            "momentum_score": 0.9,
        },
    )
    with pytest.raises(ChampionContractError, match="feature_contract_mismatch"):
        score_with_champion(
            champion,
            feature_snapshot=missing_snapshot,
            selection_context=selection,
            selection_verifier=_SelectionVerifier(selection),
            feature_snapshot_verifier=_FeatureVerifier(missing_snapshot),
            symbol="600000.SH",
            decision_time=DECISION_TIME,
        )
    with pytest.raises(ChampionContractError, match="feature_value_out_of_range"):
        _feature_snapshot(
            champion,
            feature_values={
                "quality_score": 1.2,
                "value_score": 0.6,
                "momentum_score": 0.9,
                "low_volatility_score": 0.7,
            },
        )


def test_challenger_is_shadow_only_and_cannot_be_promoted_automatically() -> None:
    challenger = ShadowChallenger(
        challenger_id="candidate-2",
        version="0.1.0",
        champion_manifest_sha256=_champion().manifest_sha256,
    )
    assert challenger.execution_eligible is False
    assert challenger.lifecycle_state == "shadow_only"
    with pytest.raises(ChampionContractError, match="automatic_promotion_forbidden"):
        challenger.promote_automatically()


def test_cash_baseline_is_non_predictive_and_keeps_full_optionality() -> None:
    baseline = cash_baseline()
    assert baseline.target_cash_pct == 1.0
    assert baseline.target_stock_gross_pct == 0.0
    assert baseline.calibrated_probability is None
    assert baseline.score_semantics == "non_predictive_cash_reference"


def test_champion_score_is_a_content_addressed_pit_data_receipt() -> None:
    receipt_type = getattr(champion_module, "ChampionScoreReceipt", None)
    verify = getattr(champion_module, "verify_champion_score_receipt", None)

    assert receipt_type is not None
    assert callable(verify)
    assert "feature_namespace" in FrozenChampionSpec.__dataclass_fields__

    decision_time = datetime(2026, 7, 16, 1, 5, tzinfo=timezone.utc)
    champion = _champion()
    selection = _selection_context(champion)
    snapshot = _feature_snapshot(
        champion,
        feature_values={
            "quality_score": 0.8,
            "value_score": 0.6,
            "momentum_score": 0.9,
            "low_volatility_score": 0.7,
        },
    )
    receipt = score_with_champion(
        champion,
        feature_snapshot=snapshot,
        selection_context=selection,
        selection_verifier=_SelectionVerifier(selection),
        feature_snapshot_verifier=_FeatureVerifier(snapshot),
        symbol="600000.SH",
        decision_time=decision_time,
    )

    assert isinstance(receipt, receipt_type)
    assert receipt.symbol == "600000.SH"
    assert receipt.decision_time == decision_time
    assert receipt.champion_selection_manifest_sha256 == "a" * 64
    assert len(receipt.feature_snapshot_sha256) == 64
    assert len(receipt.receipt_sha256) == 64
    assert receipt.statistical_promotion_eligible is False
    verify(
        receipt,
        expected_symbol="600000.SH",
        expected_decision_time=decision_time,
        current_selection_context=selection,
        selection_verifier=_SelectionVerifier(selection),
        feature_snapshot_verifier=_FeatureVerifier(snapshot),
    )

    with pytest.raises(ChampionContractError, match="score_receipt_rank_mismatch"):
        replace(receipt, rank_score=0.99)


def test_champion_authority_contracts_exist_before_rank_can_be_scored() -> None:
    required = {
        "ChampionSelectionContext",
        "ChampionSelectionVerification",
        "ChampionSelectionVerifier",
        "NumericPITFeatureSource",
        "NumericPITFeatureSnapshotReceipt",
        "NumericPITFeatureSnapshotVerification",
        "NumericPITFeatureSnapshotVerifier",
        "create_numeric_pit_feature_snapshot",
    }

    assert required <= set(dir(champion_module))


DECISION_TIME = datetime(2026, 7, 16, 1, 5, tzinfo=timezone.utc)


def _selection_context(champion: FrozenChampionSpec, *, suffix: str = "1"):
    return champion_module.ChampionSelectionContext(
        selection_receipt_sha256=suffix * 64,
        selection_manifest_sha256="a" * 64,
        selected_artifact_sha256="b" * 64,
        selected_model_id=champion.champion_id,
        selected_model_version=champion.version,
        frozen_champion_spec_manifest_sha256=champion.manifest_sha256,
        recorded_at=DECISION_TIME - timedelta(minutes=2),
        simulation_only=True,
    )


class _SelectionVerifier:
    verifier_id = "frozen-test-selection-authority"
    verifier_version = "1"

    def __init__(self, expected_context) -> None:
        self.expected_context = expected_context

    def verify(self, context, *, champion, decision_time):
        if context != self.expected_context:
            raise ValueError("not_current_selection")
        if champion.manifest_sha256 != context.frozen_champion_spec_manifest_sha256:
            raise ValueError("not_current_frozen_spec")
        return champion_module.ChampionSelectionVerification.create(
            context=context,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(minutes=1),
            current=True,
            promotion_eligible=False,
        )


def _feature_snapshot(
    champion: FrozenChampionSpec,
    *,
    feature_implementation_sha256: str = "e" * 64,
    feature_values: dict[str, float] | None = None,
):
    return champion_module.create_numeric_pit_feature_snapshot(
        symbol="600000.SH",
        decision_time=DECISION_TIME,
        feature_namespace=champion.feature_namespace,
        feature_values=(
            feature_values
            or {
                "quality_score": 0.8,
                "value_score": 0.6,
                "momentum_score": 0.9,
                "low_volatility_score": 0.7,
            }
        ),
        sources=(
            champion_module.NumericPITFeatureSource(
                dataset_id="ashare.daily.v1",
                authority_receipt_id="daily-receipt-1",
                authority_receipt_sha256="c" * 64,
                data_through=DECISION_TIME - timedelta(minutes=10),
                available_at=DECISION_TIME - timedelta(minutes=5),
                source_type="canonical_dataset",
            ),
            champion_module.NumericPITFeatureSource(
                dataset_id="ashare.daily_basic.v1",
                authority_receipt_id="daily-basic-receipt-1",
                authority_receipt_sha256="d" * 64,
                data_through=DECISION_TIME - timedelta(minutes=10),
                available_at=DECISION_TIME - timedelta(minutes=4),
                source_type="canonical_dataset",
            ),
        ),
        feature_implementation_sha256=feature_implementation_sha256,
        normalization_version="phase1-cross-sectional-v1",
        data_vintage_id="pit-vintage-20260716T010000Z",
        data_lineage_sha256="f" * 64,
    )


class _FeatureVerifier:
    verifier_id = "frozen-test-numeric-pit-authority"
    verifier_version = "1"

    def __init__(self, expected_snapshot) -> None:
        self.expected_snapshot = expected_snapshot

    def verify(self, snapshot, *, decision_time):
        if snapshot != self.expected_snapshot:
            raise ValueError("feature_snapshot_not_authoritative")
        return champion_module.NumericPITFeatureSnapshotVerification.create(
            snapshot=snapshot,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(minutes=1),
            promotion_eligible=False,
        )


def _score(*, feature_values: dict[str, float]):
    champion = _champion()
    selection = _selection_context(champion)
    snapshot = _feature_snapshot(champion, feature_values=feature_values)
    return score_with_champion(
        champion,
        feature_snapshot=snapshot,
        selection_context=selection,
        selection_verifier=_SelectionVerifier(selection),
        feature_snapshot_verifier=_FeatureVerifier(snapshot),
        symbol="600000.SH",
        decision_time=DECISION_TIME,
    )


def test_score_receipt_binds_current_selected_artifact_model_and_frozen_spec() -> None:
    champion = _champion()
    selection = _selection_context(champion)
    snapshot = _feature_snapshot(champion)

    receipt = score_with_champion(
        champion,
        feature_snapshot=snapshot,
        selection_context=selection,
        selection_verifier=_SelectionVerifier(selection),
        feature_snapshot_verifier=_FeatureVerifier(snapshot),
        symbol="600000.SH",
        decision_time=DECISION_TIME,
    )

    assert receipt.champion_selection_manifest_sha256 == "a" * 64
    assert receipt.selected_artifact_sha256 == "b" * 64
    assert receipt.selected_model_id == champion.champion_id
    assert receipt.selected_model_version == champion.version
    assert receipt.frozen_champion_spec_manifest_sha256 == champion.manifest_sha256
    assert receipt.feature_snapshot_receipt_sha256 == snapshot.receipt_sha256
    assert receipt.selection_verification_receipt_sha256
    assert receipt.feature_verification_receipt_sha256


def test_score_rejects_selection_context_that_is_not_current() -> None:
    champion = _champion()
    current = _selection_context(champion, suffix="1")
    caller_declared = _selection_context(champion, suffix="2")
    snapshot = _feature_snapshot(champion)

    with pytest.raises(ChampionContractError, match="champion_selection_verification"):
        score_with_champion(
            champion,
            feature_snapshot=snapshot,
            selection_context=caller_declared,
            selection_verifier=_SelectionVerifier(current),
            feature_snapshot_verifier=_FeatureVerifier(snapshot),
            symbol="600000.SH",
            decision_time=DECISION_TIME,
        )


def test_score_rejects_unverified_or_tampered_numeric_pit_features() -> None:
    champion = _champion()
    selection = _selection_context(champion)
    snapshot = _feature_snapshot(champion)
    with pytest.raises(
        ChampionContractError,
        match="feature_snapshot_receipt_hash_mismatch",
    ):
        replace(snapshot, feature_implementation_sha256="0" * 64)
    caller_snapshot = _feature_snapshot(
        champion,
        feature_implementation_sha256="0" * 64,
    )

    with pytest.raises(ChampionContractError, match="feature_snapshot_verification"):
        score_with_champion(
            champion,
            feature_snapshot=caller_snapshot,
            selection_context=selection,
            selection_verifier=_SelectionVerifier(selection),
            feature_snapshot_verifier=_FeatureVerifier(snapshot),
            symbol="600000.SH",
            decision_time=DECISION_TIME,
        )

    with pytest.raises(ChampionContractError, match="feature_snapshot_verifier"):
        score_with_champion(
            champion,
            feature_snapshot=snapshot,
            selection_context=selection,
            selection_verifier=_SelectionVerifier(selection),
            feature_snapshot_verifier=object(),
            symbol="600000.SH",
            decision_time=DECISION_TIME,
        )


def test_score_rejects_feature_proof_that_predates_source_availability() -> None:
    champion = _champion()
    selection = _selection_context(champion)
    snapshot = _feature_snapshot(champion)

    class _PredatedFeatureVerifier:
        def verify(self, value, *, decision_time):
            return champion_module.NumericPITFeatureSnapshotVerification.create(
                snapshot=value,
                verifier_id="predated-test-feature-authority",
                verifier_version="1",
                verified_at=decision_time - timedelta(minutes=30),
                valid_until=decision_time + timedelta(minutes=1),
                promotion_eligible=False,
            )

    with pytest.raises(
        ChampionContractError,
        match="feature_snapshot_proof_predates_source",
    ):
        score_with_champion(
            champion,
            feature_snapshot=snapshot,
            selection_context=selection,
            selection_verifier=_SelectionVerifier(selection),
            feature_snapshot_verifier=_PredatedFeatureVerifier(),
            symbol="600000.SH",
            decision_time=DECISION_TIME,
        )


def test_numeric_pit_snapshot_rejects_future_or_llm_sources() -> None:
    champion = _champion()
    source = champion_module.NumericPITFeatureSource(
        dataset_id="ashare.daily.v1",
        authority_receipt_id="daily-receipt-1",
        authority_receipt_sha256="c" * 64,
        data_through=DECISION_TIME - timedelta(minutes=1),
        available_at=DECISION_TIME + timedelta(seconds=1),
        source_type="canonical_dataset",
    )
    with pytest.raises(ChampionContractError, match="source_available_after_decision"):
        champion_module.create_numeric_pit_feature_snapshot(
            symbol="600000.SH",
            decision_time=DECISION_TIME,
            feature_namespace=champion.feature_namespace,
            feature_values={name: 0.5 for name in champion.feature_names},
            sources=(source,),
            feature_implementation_sha256="e" * 64,
            normalization_version="phase1-cross-sectional-v1",
            data_vintage_id="vintage-1",
            data_lineage_sha256="f" * 64,
        )

    with pytest.raises(ChampionContractError, match="numeric_source_type_not_allowed"):
        replace(source, available_at=DECISION_TIME, source_type="llm_generated")


@pytest.mark.parametrize("invalid_value", [True, "0.5"])
def test_numeric_pit_snapshot_rejects_non_numeric_values_before_normalization(
    invalid_value: object,
) -> None:
    values: dict[str, object] = {name: 0.5 for name in FEATURES}
    values["quality_score"] = invalid_value

    with pytest.raises(ChampionContractError, match="feature_value_invalid"):
        _feature_snapshot(
            _champion(),
            feature_values=values,  # type: ignore[arg-type]
        )
