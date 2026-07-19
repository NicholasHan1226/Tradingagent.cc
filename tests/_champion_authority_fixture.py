from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from shared.portfolio.champion import (
    ChampionScoreReceipt,
    ChampionSelectionContext,
    ChampionSelectionVerification,
    FrozenChampionSpec,
    NumericPITFeatureSnapshotReceipt,
    NumericPITFeatureSnapshotVerification,
    NumericPITFeatureSource,
    create_numeric_pit_feature_snapshot,
    score_with_champion,
)


class FrozenChampionSelectionVerifier:
    def __init__(self, expected_context: ChampionSelectionContext) -> None:
        self.expected_context = expected_context

    def verify(self, context, *, champion, decision_time):
        if context != self.expected_context:
            raise ValueError("not_current_champion_selection")
        return ChampionSelectionVerification.create(
            context=context,
            verifier_id="test-frozen-champion-selection-authority",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(minutes=1),
            current=True,
            promotion_eligible=False,
        )


class FrozenNumericPITFeatureSnapshotVerifier:
    def __init__(
        self,
        expected_snapshot: NumericPITFeatureSnapshotReceipt
        | Iterable[NumericPITFeatureSnapshotReceipt],
    ) -> None:
        if isinstance(expected_snapshot, NumericPITFeatureSnapshotReceipt):
            snapshots = (expected_snapshot,)
        else:
            snapshots = tuple(expected_snapshot)
        self.expected_snapshots = {
            snapshot.receipt_sha256: snapshot for snapshot in snapshots
        }

    def verify(self, snapshot, *, decision_time):
        if self.expected_snapshots.get(snapshot.receipt_sha256) != snapshot:
            raise ValueError("not_authoritative_numeric_feature_snapshot")
        return NumericPITFeatureSnapshotVerification.create(
            snapshot=snapshot,
            verifier_id="test-frozen-numeric-pit-feature-authority",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(minutes=1),
            promotion_eligible=False,
        )


@dataclass(frozen=True)
class ChampionAuthorityFixture:
    selection_context: ChampionSelectionContext
    selection_verifier: FrozenChampionSelectionVerifier
    feature_snapshot: NumericPITFeatureSnapshotReceipt
    feature_verifier: FrozenNumericPITFeatureSnapshotVerifier
    score_receipt: ChampionScoreReceipt


def build_champion_authority_fixture(
    *,
    champion: FrozenChampionSpec,
    symbol: str,
    decision_time: datetime,
    feature_values: Mapping[str, float],
    selection_manifest_sha256: str = "c" * 64,
    source_id: str = "test-numeric-feature-source",
) -> ChampionAuthorityFixture:
    selection_context = ChampionSelectionContext(
        selection_receipt_sha256="1" * 64,
        selection_manifest_sha256=selection_manifest_sha256,
        selected_artifact_sha256="2" * 64,
        selected_model_id=champion.champion_id,
        selected_model_version=champion.version,
        frozen_champion_spec_manifest_sha256=champion.manifest_sha256,
        recorded_at=decision_time - timedelta(minutes=2),
        simulation_only=True,
    )
    selection_verifier = FrozenChampionSelectionVerifier(selection_context)
    feature_snapshot = create_numeric_pit_feature_snapshot(
        symbol=symbol,
        decision_time=decision_time,
        feature_namespace=champion.feature_namespace,
        feature_values=feature_values,
        sources=(
            NumericPITFeatureSource(
                dataset_id="ashare.phase1.numeric_features.v1",
                authority_receipt_id=source_id,
                authority_receipt_sha256="3" * 64,
                data_through=decision_time - timedelta(minutes=2),
                available_at=decision_time - timedelta(minutes=1),
                source_type="offline_fixture",
            ),
        ),
        feature_implementation_sha256="4" * 64,
        normalization_version="phase1-test-normalization-v1",
        data_vintage_id="phase1-test-vintage-v1",
        data_lineage_sha256="5" * 64,
    )
    feature_verifier = FrozenNumericPITFeatureSnapshotVerifier(feature_snapshot)
    score_receipt = score_with_champion(
        champion,
        feature_snapshot=feature_snapshot,
        selection_context=selection_context,
        selection_verifier=selection_verifier,
        feature_snapshot_verifier=feature_verifier,
        symbol=symbol,
        decision_time=decision_time,
    )
    return ChampionAuthorityFixture(
        selection_context=selection_context,
        selection_verifier=selection_verifier,
        feature_snapshot=feature_snapshot,
        feature_verifier=feature_verifier,
        score_receipt=score_receipt,
    )
