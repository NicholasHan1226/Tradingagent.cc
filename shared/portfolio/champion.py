"""Frozen, deterministic rank-score Champion contracts for Phase 0-3."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Mapping, Protocol


class ChampionContractError(ValueError):
    """Raised when a model artifact would violate the frozen Champion contract."""


PHASE1_NUMERIC_FEATURE_NAMESPACE = "tradingagent.numeric_pit_features.v1"
_PHASE1_ALLOWED_FEATURES = frozenset(
    {
        "quality_score",
        "value_score",
        "momentum_score",
        "low_volatility_score",
        "liquidity_score",
        "cash_conversion_quality",
        "industry_relative_strength",
        "price_pass_through_gap",
    }
)
_SHA256_CHARS = frozenset("0123456789abcdef")
_NUMERIC_SOURCE_TYPES = frozenset(
    {
        "canonical_dataset",
        "verified_derived_numeric",
        "offline_fixture",
    }
)


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ChampionContractError(f"{field_name}_invalid")
    return value


def _sha256_text(value: object, *, field_name: str) -> str:
    text = _text(value, field_name=field_name)
    if len(text) != 64 or any(character not in _SHA256_CHARS for character in text):
        raise ChampionContractError(f"{field_name}_invalid")
    return text


def _aware(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ChampionContractError(f"{field_name}_timezone_required")
    if value.utcoffset() is None:
        raise ChampionContractError(f"{field_name}_timezone_required")
    return value


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class FrozenChampionSpec:
    champion_id: str
    version: str
    feature_names: tuple[str, ...]
    feature_weights: tuple[float, ...]
    decision_horizon: str
    trained_through: str
    feature_namespace: str = PHASE1_NUMERIC_FEATURE_NAMESPACE
    score_semantics: str = "uncalibrated_deterministic_rank_score"
    automatic_promotion_enabled: bool = False
    risk_expansion_enabled: bool = False
    manifest_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.champion_id, field_name="champion_id")
        _text(self.version, field_name="version")
        _text(self.decision_horizon, field_name="decision_horizon")
        try:
            date.fromisoformat(
                _text(self.trained_through, field_name="trained_through")
            )
        except ValueError as exc:
            raise ChampionContractError("trained_through_invalid") from exc
        if not 4 <= len(self.feature_names) <= 8:
            raise ChampionContractError("feature_count_must_be_4_to_8")
        if len(self.feature_names) != len(set(self.feature_names)):
            raise ChampionContractError("feature_names_must_be_unique")
        if len(self.feature_weights) != len(self.feature_names):
            raise ChampionContractError("feature_weight_count_mismatch")
        if self.feature_namespace != PHASE1_NUMERIC_FEATURE_NAMESPACE:
            raise ChampionContractError("feature_namespace_not_allowed")
        for name in self.feature_names:
            normalized = _text(name, field_name="feature_name")
            if normalized not in _PHASE1_ALLOWED_FEATURES:
                raise ChampionContractError("feature_not_in_allowed_namespace")
        weights: list[float] = []
        for weight in self.feature_weights:
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ChampionContractError("feature_weight_invalid")
            number = float(weight)
            if not math.isfinite(number) or number < 0:
                raise ChampionContractError("feature_weight_invalid")
            weights.append(number)
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            raise ChampionContractError("feature_weights_must_sum_to_one")
        if self.automatic_promotion_enabled or self.risk_expansion_enabled:
            raise ChampionContractError("automatic_authority_forbidden")
        payload = {
            "champion_id": self.champion_id,
            "version": self.version,
            "feature_names": list(self.feature_names),
            "feature_weights": weights,
            "decision_horizon": self.decision_horizon,
            "trained_through": self.trained_through,
            "feature_namespace": self.feature_namespace,
            "score_semantics": self.score_semantics,
            "automatic_promotion_enabled": False,
            "risk_expansion_enabled": False,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "manifest_sha256", digest)


@dataclass(frozen=True)
class ChampionSelectionContext:
    """Externally selected model identity bound to one frozen Champion spec."""

    selection_receipt_sha256: str
    selection_manifest_sha256: str
    selected_artifact_sha256: str
    selected_model_id: str
    selected_model_version: str
    frozen_champion_spec_manifest_sha256: str
    recorded_at: datetime
    simulation_only: bool
    context_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("selection_receipt_sha256", self.selection_receipt_sha256),
            ("selection_manifest_sha256", self.selection_manifest_sha256),
            ("selected_artifact_sha256", self.selected_artifact_sha256),
            (
                "frozen_champion_spec_manifest_sha256",
                self.frozen_champion_spec_manifest_sha256,
            ),
        ):
            _sha256_text(value, field_name=field_name)
        _text(self.selected_model_id, field_name="selected_model_id")
        _text(self.selected_model_version, field_name="selected_model_version")
        _aware(self.recorded_at, field_name="selection_recorded_at")
        if self.simulation_only is not True:
            raise ChampionContractError("champion_selection_must_be_simulation_only")
        object.__setattr__(
            self,
            "context_sha256",
            _canonical_sha256(_champion_selection_context_payload(self)),
        )


def _champion_selection_context_payload(
    context: ChampionSelectionContext,
) -> dict[str, object]:
    return {
        "contract": "tradingagent.champion_selection_context.v1",
        "selection_receipt_sha256": context.selection_receipt_sha256,
        "selection_manifest_sha256": context.selection_manifest_sha256,
        "selected_artifact_sha256": context.selected_artifact_sha256,
        "selected_model_id": context.selected_model_id,
        "selected_model_version": context.selected_model_version,
        "frozen_champion_spec_manifest_sha256": (
            context.frozen_champion_spec_manifest_sha256
        ),
        "recorded_at": _instant(context.recorded_at),
        "simulation_only": True,
    }


@dataclass(frozen=True)
class ChampionSelectionVerification:
    """Detached proof that a selection context is the current model authority."""

    verifier_id: str
    verifier_version: str
    selection_context_sha256: str
    selection_receipt_sha256: str
    selection_manifest_sha256: str
    selected_artifact_sha256: str
    selected_model_id: str
    selected_model_version: str
    frozen_champion_spec_manifest_sha256: str
    verified_at: datetime
    valid_until: datetime
    current: bool
    promotion_eligible: bool
    verification_receipt_sha256: str

    def __post_init__(self) -> None:
        _text(self.verifier_id, field_name="selection_verifier_id")
        _text(self.verifier_version, field_name="selection_verifier_version")
        for field_name, value in (
            ("selection_context_sha256", self.selection_context_sha256),
            ("selection_receipt_sha256", self.selection_receipt_sha256),
            ("selection_manifest_sha256", self.selection_manifest_sha256),
            ("selected_artifact_sha256", self.selected_artifact_sha256),
            (
                "frozen_champion_spec_manifest_sha256",
                self.frozen_champion_spec_manifest_sha256,
            ),
            ("verification_receipt_sha256", self.verification_receipt_sha256),
        ):
            _sha256_text(value, field_name=field_name)
        _text(self.selected_model_id, field_name="selected_model_id")
        _text(self.selected_model_version, field_name="selected_model_version")
        verified_at = _aware(self.verified_at, field_name="selection_verified_at")
        valid_until = _aware(self.valid_until, field_name="selection_valid_until")
        if valid_until < verified_at:
            raise ChampionContractError("selection_verification_window_invalid")
        if self.current is not True:
            raise ChampionContractError("champion_selection_not_current")
        if self.promotion_eligible is not False:
            raise ChampionContractError("champion_selection_promotion_forbidden")
        if self.verification_receipt_sha256 != _canonical_sha256(
            self._receipt_payload()
        ):
            raise ChampionContractError("selection_verification_receipt_mismatch")

    def _receipt_payload(self) -> dict[str, object]:
        return {
            "contract": "tradingagent.champion_selection_verification.v1",
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "selection_context_sha256": self.selection_context_sha256,
            "selection_receipt_sha256": self.selection_receipt_sha256,
            "selection_manifest_sha256": self.selection_manifest_sha256,
            "selected_artifact_sha256": self.selected_artifact_sha256,
            "selected_model_id": self.selected_model_id,
            "selected_model_version": self.selected_model_version,
            "frozen_champion_spec_manifest_sha256": (
                self.frozen_champion_spec_manifest_sha256
            ),
            "verified_at": _instant(self.verified_at),
            "valid_until": _instant(self.valid_until),
            "current": True,
            "promotion_eligible": False,
        }

    @classmethod
    def create(
        cls,
        *,
        context: ChampionSelectionContext,
        verifier_id: str,
        verifier_version: str,
        verified_at: datetime,
        valid_until: datetime,
        current: bool,
        promotion_eligible: bool,
    ) -> ChampionSelectionVerification:
        payload = {
            "contract": "tradingagent.champion_selection_verification.v1",
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "selection_context_sha256": context.context_sha256,
            "selection_receipt_sha256": context.selection_receipt_sha256,
            "selection_manifest_sha256": context.selection_manifest_sha256,
            "selected_artifact_sha256": context.selected_artifact_sha256,
            "selected_model_id": context.selected_model_id,
            "selected_model_version": context.selected_model_version,
            "frozen_champion_spec_manifest_sha256": (
                context.frozen_champion_spec_manifest_sha256
            ),
            "verified_at": _instant(verified_at),
            "valid_until": _instant(valid_until),
            "current": current,
            "promotion_eligible": promotion_eligible,
        }
        return cls(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            selection_context_sha256=context.context_sha256,
            selection_receipt_sha256=context.selection_receipt_sha256,
            selection_manifest_sha256=context.selection_manifest_sha256,
            selected_artifact_sha256=context.selected_artifact_sha256,
            selected_model_id=context.selected_model_id,
            selected_model_version=context.selected_model_version,
            frozen_champion_spec_manifest_sha256=(
                context.frozen_champion_spec_manifest_sha256
            ),
            verified_at=verified_at,
            valid_until=valid_until,
            current=current,
            promotion_eligible=promotion_eligible,
            verification_receipt_sha256=_canonical_sha256(payload),
        )


class ChampionSelectionVerifier(Protocol):
    """Port implemented by the current selection registry reader."""

    def verify(
        self,
        context: ChampionSelectionContext,
        *,
        champion: FrozenChampionSpec,
        decision_time: datetime,
    ) -> ChampionSelectionVerification: ...


@dataclass(frozen=True)
class NumericPITFeatureSource:
    """One provider-neutral authority source used by a numeric feature snapshot."""

    dataset_id: str
    authority_receipt_id: str
    authority_receipt_sha256: str
    data_through: datetime
    available_at: datetime
    source_type: str

    def __post_init__(self) -> None:
        _text(self.dataset_id, field_name="numeric_source_dataset_id")
        _text(
            self.authority_receipt_id,
            field_name="numeric_source_authority_receipt_id",
        )
        _sha256_text(
            self.authority_receipt_sha256,
            field_name="numeric_source_authority_receipt_sha256",
        )
        data_through = _aware(
            self.data_through,
            field_name="numeric_source_data_through",
        )
        available_at = _aware(
            self.available_at,
            field_name="numeric_source_available_at",
        )
        if data_through > available_at:
            raise ChampionContractError("numeric_source_time_order_invalid")
        if self.source_type not in _NUMERIC_SOURCE_TYPES:
            raise ChampionContractError("numeric_source_type_not_allowed")


def _numeric_source_payload(source: NumericPITFeatureSource) -> dict[str, object]:
    return {
        "dataset_id": source.dataset_id,
        "authority_receipt_id": source.authority_receipt_id,
        "authority_receipt_sha256": source.authority_receipt_sha256,
        "data_through": _instant(source.data_through),
        "available_at": _instant(source.available_at),
        "source_type": source.source_type,
    }


@dataclass(frozen=True)
class NumericPITFeatureSnapshotReceipt:
    """Content-addressed numeric features with explicit PIT producer lineage."""

    symbol: str
    decision_time: datetime
    feature_namespace: str
    feature_values: tuple[tuple[str, float], ...]
    sources: tuple[NumericPITFeatureSource, ...]
    feature_implementation_sha256: str
    normalization_version: str
    data_vintage_id: str
    data_lineage_sha256: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        _text(self.symbol, field_name="feature_snapshot_symbol")
        decision_time = _aware(
            self.decision_time,
            field_name="feature_snapshot_decision_time",
        )
        _text(self.feature_namespace, field_name="feature_snapshot_namespace")
        if not isinstance(self.feature_values, tuple) or not self.feature_values:
            raise ChampionContractError("feature_snapshot_values_invalid")
        names: list[str] = []
        for name, raw in self.feature_values:
            normalized_name = _text(name, field_name="feature_snapshot_name")
            if normalized_name not in _PHASE1_ALLOWED_FEATURES:
                raise ChampionContractError("feature_not_in_allowed_namespace")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ChampionContractError("feature_value_invalid")
            value = float(raw)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ChampionContractError("feature_value_out_of_range")
            names.append(normalized_name)
        if len(names) != len(set(names)) or names != sorted(names):
            raise ChampionContractError("feature_snapshot_names_not_canonical")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ChampionContractError("feature_snapshot_sources_required")
        source_keys: list[tuple[str, str]] = []
        for source in self.sources:
            if not isinstance(source, NumericPITFeatureSource):
                raise ChampionContractError("numeric_feature_source_invalid")
            if source.available_at > decision_time:
                raise ChampionContractError("source_available_after_decision")
            source_keys.append((source.dataset_id, source.authority_receipt_id))
        if source_keys != sorted(source_keys) or len(source_keys) != len(
            set(source_keys)
        ):
            raise ChampionContractError("numeric_feature_sources_not_canonical")
        _sha256_text(
            self.feature_implementation_sha256,
            field_name="feature_implementation_sha256",
        )
        _text(self.normalization_version, field_name="normalization_version")
        _text(self.data_vintage_id, field_name="data_vintage_id")
        _sha256_text(self.data_lineage_sha256, field_name="data_lineage_sha256")
        _sha256_text(self.receipt_sha256, field_name="feature_snapshot_receipt_sha256")
        if self.receipt_sha256 != _canonical_sha256(
            _numeric_feature_snapshot_payload(self)
        ):
            raise ChampionContractError("feature_snapshot_receipt_hash_mismatch")

    @property
    def data_receipt_ids(self) -> tuple[str, ...]:
        return tuple(source.authority_receipt_id for source in self.sources)


def _numeric_feature_snapshot_payload(
    snapshot: NumericPITFeatureSnapshotReceipt,
) -> dict[str, object]:
    return {
        "contract": "tradingagent.numeric_pit_feature_snapshot.v1",
        "symbol": snapshot.symbol,
        "decision_time": _instant(snapshot.decision_time),
        "feature_namespace": snapshot.feature_namespace,
        "feature_values": [list(item) for item in snapshot.feature_values],
        "sources": [_numeric_source_payload(source) for source in snapshot.sources],
        "feature_implementation_sha256": snapshot.feature_implementation_sha256,
        "normalization_version": snapshot.normalization_version,
        "data_vintage_id": snapshot.data_vintage_id,
        "data_lineage_sha256": snapshot.data_lineage_sha256,
    }


def create_numeric_pit_feature_snapshot(
    *,
    symbol: str,
    decision_time: datetime,
    feature_namespace: str,
    feature_values: Mapping[str, object],
    sources: tuple[NumericPITFeatureSource, ...],
    feature_implementation_sha256: str,
    normalization_version: str,
    data_vintage_id: str,
    data_lineage_sha256: str,
) -> NumericPITFeatureSnapshotReceipt:
    if not isinstance(feature_values, Mapping) or not feature_values:
        raise ChampionContractError("feature_snapshot_values_invalid")
    normalized_rows: list[tuple[str, float]] = []
    for name, raw_value in feature_values.items():
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ChampionContractError("feature_value_invalid")
        normalized_rows.append((name, float(raw_value)))
    normalized_values = tuple(sorted(normalized_rows))
    ordered_sources = tuple(
        sorted(sources, key=lambda item: (item.dataset_id, item.authority_receipt_id))
    )
    placeholder = NumericPITFeatureSnapshotReceipt.__new__(
        NumericPITFeatureSnapshotReceipt
    )
    for field_name, value in (
        ("symbol", symbol),
        ("decision_time", decision_time),
        ("feature_namespace", feature_namespace),
        ("feature_values", normalized_values),
        ("sources", ordered_sources),
        ("feature_implementation_sha256", feature_implementation_sha256),
        ("normalization_version", normalization_version),
        ("data_vintage_id", data_vintage_id),
        ("data_lineage_sha256", data_lineage_sha256),
    ):
        object.__setattr__(placeholder, field_name, value)
    receipt_sha256 = _canonical_sha256(_numeric_feature_snapshot_payload(placeholder))
    return NumericPITFeatureSnapshotReceipt(
        symbol=symbol,
        decision_time=decision_time,
        feature_namespace=feature_namespace,
        feature_values=normalized_values,
        sources=ordered_sources,
        feature_implementation_sha256=feature_implementation_sha256,
        normalization_version=normalization_version,
        data_vintage_id=data_vintage_id,
        data_lineage_sha256=data_lineage_sha256,
        receipt_sha256=receipt_sha256,
    )


@dataclass(frozen=True)
class NumericPITFeatureSnapshotVerification:
    """Detached verifier proof for a complete numeric PIT feature snapshot."""

    verifier_id: str
    verifier_version: str
    feature_snapshot_receipt_sha256: str
    symbol: str
    decision_time: datetime
    feature_implementation_sha256: str
    normalization_version: str
    source_bindings_sha256: str
    verified_at: datetime
    valid_until: datetime
    promotion_eligible: bool
    verification_receipt_sha256: str

    def __post_init__(self) -> None:
        _text(self.verifier_id, field_name="feature_verifier_id")
        _text(self.verifier_version, field_name="feature_verifier_version")
        for field_name, value in (
            (
                "feature_snapshot_receipt_sha256",
                self.feature_snapshot_receipt_sha256,
            ),
            ("feature_implementation_sha256", self.feature_implementation_sha256),
            ("source_bindings_sha256", self.source_bindings_sha256),
            ("verification_receipt_sha256", self.verification_receipt_sha256),
        ):
            _sha256_text(value, field_name=field_name)
        _text(self.symbol, field_name="feature_verification_symbol")
        _aware(self.decision_time, field_name="feature_verification_decision_time")
        _text(self.normalization_version, field_name="normalization_version")
        verified_at = _aware(self.verified_at, field_name="feature_verified_at")
        valid_until = _aware(self.valid_until, field_name="feature_valid_until")
        if valid_until < verified_at:
            raise ChampionContractError("feature_verification_window_invalid")
        if self.promotion_eligible is not False:
            raise ChampionContractError("feature_snapshot_promotion_forbidden")
        if self.verification_receipt_sha256 != _canonical_sha256(
            self._receipt_payload()
        ):
            raise ChampionContractError("feature_verification_receipt_mismatch")

    def _receipt_payload(self) -> dict[str, object]:
        return {
            "contract": "tradingagent.numeric_pit_feature_verification.v1",
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "feature_snapshot_receipt_sha256": (self.feature_snapshot_receipt_sha256),
            "symbol": self.symbol,
            "decision_time": _instant(self.decision_time),
            "feature_implementation_sha256": self.feature_implementation_sha256,
            "normalization_version": self.normalization_version,
            "source_bindings_sha256": self.source_bindings_sha256,
            "verified_at": _instant(self.verified_at),
            "valid_until": _instant(self.valid_until),
            "promotion_eligible": False,
        }

    @classmethod
    def create(
        cls,
        *,
        snapshot: NumericPITFeatureSnapshotReceipt,
        verifier_id: str,
        verifier_version: str,
        verified_at: datetime,
        valid_until: datetime,
        promotion_eligible: bool,
    ) -> NumericPITFeatureSnapshotVerification:
        source_sha = _canonical_sha256(
            [_numeric_source_payload(source) for source in snapshot.sources]
        )
        payload = {
            "contract": "tradingagent.numeric_pit_feature_verification.v1",
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "feature_snapshot_receipt_sha256": snapshot.receipt_sha256,
            "symbol": snapshot.symbol,
            "decision_time": _instant(snapshot.decision_time),
            "feature_implementation_sha256": (snapshot.feature_implementation_sha256),
            "normalization_version": snapshot.normalization_version,
            "source_bindings_sha256": source_sha,
            "verified_at": _instant(verified_at),
            "valid_until": _instant(valid_until),
            "promotion_eligible": promotion_eligible,
        }
        return cls(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            feature_snapshot_receipt_sha256=snapshot.receipt_sha256,
            symbol=snapshot.symbol,
            decision_time=snapshot.decision_time,
            feature_implementation_sha256=snapshot.feature_implementation_sha256,
            normalization_version=snapshot.normalization_version,
            source_bindings_sha256=source_sha,
            verified_at=verified_at,
            valid_until=valid_until,
            promotion_eligible=promotion_eligible,
            verification_receipt_sha256=_canonical_sha256(payload),
        )


class NumericPITFeatureSnapshotVerifier(Protocol):
    """Port implemented by an independent numeric-feature authority reader."""

    def verify(
        self,
        snapshot: NumericPITFeatureSnapshotReceipt,
        *,
        decision_time: datetime,
    ) -> NumericPITFeatureSnapshotVerification: ...


@dataclass(frozen=True)
class ChampionScoreReceipt:
    """Content-addressed rank evidence, never a probability or sizing authority."""

    champion: FrozenChampionSpec
    selection_context: ChampionSelectionContext
    selection_verification_receipt_sha256: str
    feature_snapshot: NumericPITFeatureSnapshotReceipt
    feature_verification_receipt_sha256: str
    rank_score: float
    receipt_sha256: str
    score_semantics: str = "uncalibrated_deterministic_rank_score"
    evidence_class: str = "champion_numeric_pit_score_receipt"
    calibrated_probability: None = None
    calibration_status: str = "not_calibrated"
    execution_eligible: bool = True
    statistical_promotion_eligible: bool = False

    def __post_init__(self) -> None:
        _verify_champion_score_receipt_integrity(self)

    @property
    def symbol(self) -> str:
        return self.feature_snapshot.symbol

    @property
    def decision_time(self) -> datetime:
        return self.feature_snapshot.decision_time

    @property
    def feature_namespace(self) -> str:
        return self.feature_snapshot.feature_namespace

    @property
    def feature_values(self) -> tuple[tuple[str, float], ...]:
        return self.feature_snapshot.feature_values

    @property
    def data_receipt_ids(self) -> tuple[str, ...]:
        return self.feature_snapshot.data_receipt_ids

    @property
    def data_vintage_id(self) -> str:
        return self.feature_snapshot.data_vintage_id

    @property
    def data_lineage_sha256(self) -> str:
        return self.feature_snapshot.data_lineage_sha256

    @property
    def feature_snapshot_sha256(self) -> str:
        return self.feature_snapshot.receipt_sha256

    @property
    def feature_snapshot_receipt_sha256(self) -> str:
        return self.feature_snapshot.receipt_sha256

    @property
    def champion_selection_manifest_sha256(self) -> str:
        return self.selection_context.selection_manifest_sha256

    @property
    def selected_artifact_sha256(self) -> str:
        return self.selection_context.selected_artifact_sha256

    @property
    def selected_model_id(self) -> str:
        return self.selection_context.selected_model_id

    @property
    def selected_model_version(self) -> str:
        return self.selection_context.selected_model_version

    @property
    def frozen_champion_spec_manifest_sha256(self) -> str:
        return self.selection_context.frozen_champion_spec_manifest_sha256


@dataclass(frozen=True)
class FixtureRankEvidence:
    """Explicit non-promotable rank evidence for network-closed fixtures only."""

    champion_selection_manifest_sha256: str
    symbol: str
    decision_time: datetime
    fixture_id: str
    source_fixture_sha256: str
    rank_score: float
    receipt_sha256: str
    score_semantics: str = "uncalibrated_deterministic_rank_score"
    evidence_class: str = "offline_engineering_fixture_rank"
    execution_eligible: bool = True
    statistical_promotion_eligible: bool = False

    def __post_init__(self) -> None:
        verify_fixture_rank_evidence(
            self,
            expected_symbol=self.symbol,
            expected_decision_time=self.decision_time,
            expected_champion_selection_manifest_sha256=(
                self.champion_selection_manifest_sha256
            ),
        )


@dataclass(frozen=True)
class CashBaseline:
    baseline_id: str = "ashare-cash-baseline-v1"
    target_cash_pct: float = 1.0
    target_stock_gross_pct: float = 0.0
    score_semantics: str = "non_predictive_cash_reference"
    calibrated_probability: None = None
    execution_eligible: bool = True


def cash_baseline() -> CashBaseline:
    """Return the immutable no-position counterfactual for Phase 0-3."""

    return CashBaseline()


def score_with_champion(
    champion: FrozenChampionSpec,
    *,
    feature_snapshot: NumericPITFeatureSnapshotReceipt,
    selection_context: ChampionSelectionContext,
    selection_verifier: ChampionSelectionVerifier,
    feature_snapshot_verifier: NumericPITFeatureSnapshotVerifier,
    symbol: str,
    decision_time: datetime,
) -> ChampionScoreReceipt:
    if not isinstance(champion, FrozenChampionSpec):
        raise ChampionContractError("frozen_champion_required")
    if not isinstance(feature_snapshot, NumericPITFeatureSnapshotReceipt):
        raise ChampionContractError("numeric_pit_feature_snapshot_required")
    if not isinstance(selection_context, ChampionSelectionContext):
        raise ChampionContractError("champion_selection_context_required")
    normalized_symbol = _text(symbol, field_name="symbol")
    resolved_decision_time = _aware(decision_time, field_name="decision_time")
    if feature_snapshot.symbol != normalized_symbol:
        raise ChampionContractError("feature_snapshot_symbol_mismatch")
    if _instant(feature_snapshot.decision_time) != _instant(resolved_decision_time):
        raise ChampionContractError("feature_snapshot_decision_time_mismatch")
    if feature_snapshot.feature_namespace != champion.feature_namespace:
        raise ChampionContractError("feature_snapshot_namespace_mismatch")
    values_by_name = dict(feature_snapshot.feature_values)
    if set(values_by_name) != set(champion.feature_names):
        raise ChampionContractError("feature_contract_mismatch")
    selection_proof = _verify_current_champion_selection(
        context=selection_context,
        champion=champion,
        decision_time=resolved_decision_time,
        verifier=selection_verifier,
    )
    feature_proof = _verify_numeric_pit_feature_snapshot(
        snapshot=feature_snapshot,
        decision_time=resolved_decision_time,
        verifier=feature_snapshot_verifier,
    )
    values: list[float] = []
    for name in champion.feature_names:
        raw = values_by_name[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ChampionContractError("feature_value_invalid")
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ChampionContractError("feature_value_out_of_range")
        values.append(value)
    rank_score = round(
        sum(
            value * float(weight)
            for value, weight in zip(values, champion.feature_weights)
        ),
        12,
    )
    receipt_payload = _score_receipt_payload(
        champion=champion,
        selection_context=selection_context,
        selection_verification_receipt_sha256=(
            selection_proof.verification_receipt_sha256
        ),
        feature_snapshot=feature_snapshot,
        feature_verification_receipt_sha256=(feature_proof.verification_receipt_sha256),
        rank_score=rank_score,
    )
    return ChampionScoreReceipt(
        champion=champion,
        selection_context=selection_context,
        selection_verification_receipt_sha256=(
            selection_proof.verification_receipt_sha256
        ),
        feature_snapshot=feature_snapshot,
        feature_verification_receipt_sha256=(feature_proof.verification_receipt_sha256),
        rank_score=rank_score,
        receipt_sha256=_canonical_sha256(receipt_payload),
        score_semantics=champion.score_semantics,
    )


def _verify_current_champion_selection(
    *,
    context: ChampionSelectionContext,
    champion: FrozenChampionSpec,
    decision_time: datetime,
    verifier: ChampionSelectionVerifier,
) -> ChampionSelectionVerification:
    if (
        context.selected_model_id != champion.champion_id
        or context.selected_model_version != champion.version
        or context.frozen_champion_spec_manifest_sha256 != champion.manifest_sha256
    ):
        raise ChampionContractError("champion_selection_model_binding_mismatch")
    if context.recorded_at > decision_time:
        raise ChampionContractError("champion_selection_after_decision")
    verify = getattr(verifier, "verify", None)
    if not callable(verify):
        raise ChampionContractError("champion_selection_verifier_required")
    try:
        proof = verify(context, champion=champion, decision_time=decision_time)
    except Exception as exc:
        raise ChampionContractError("champion_selection_verification_failed") from exc
    if not isinstance(proof, ChampionSelectionVerification):
        raise ChampionContractError("champion_selection_verification_failed")
    expected = {
        "selection_context_sha256": context.context_sha256,
        "selection_receipt_sha256": context.selection_receipt_sha256,
        "selection_manifest_sha256": context.selection_manifest_sha256,
        "selected_artifact_sha256": context.selected_artifact_sha256,
        "selected_model_id": context.selected_model_id,
        "selected_model_version": context.selected_model_version,
        "frozen_champion_spec_manifest_sha256": (
            context.frozen_champion_spec_manifest_sha256
        ),
    }
    if any(getattr(proof, name) != value for name, value in expected.items()):
        raise ChampionContractError("champion_selection_verification_binding_mismatch")
    if proof.verified_at < context.recorded_at:
        raise ChampionContractError("champion_selection_proof_predates_selection")
    if proof.verified_at > decision_time:
        raise ChampionContractError("champion_selection_proof_after_decision")
    if proof.valid_until < decision_time:
        raise ChampionContractError("champion_selection_proof_expired")
    return proof


def _verify_numeric_pit_feature_snapshot(
    *,
    snapshot: NumericPITFeatureSnapshotReceipt,
    decision_time: datetime,
    verifier: NumericPITFeatureSnapshotVerifier,
) -> NumericPITFeatureSnapshotVerification:
    verify = getattr(verifier, "verify", None)
    if not callable(verify):
        raise ChampionContractError("feature_snapshot_verifier_required")
    try:
        proof = verify(snapshot, decision_time=decision_time)
    except Exception as exc:
        raise ChampionContractError("feature_snapshot_verification_failed") from exc
    if not isinstance(proof, NumericPITFeatureSnapshotVerification):
        raise ChampionContractError("feature_snapshot_verification_failed")
    source_sha = _canonical_sha256(
        [_numeric_source_payload(source) for source in snapshot.sources]
    )
    expected = {
        "feature_snapshot_receipt_sha256": snapshot.receipt_sha256,
        "symbol": snapshot.symbol,
        "decision_time": snapshot.decision_time,
        "feature_implementation_sha256": snapshot.feature_implementation_sha256,
        "normalization_version": snapshot.normalization_version,
        "source_bindings_sha256": source_sha,
    }
    if any(getattr(proof, name) != value for name, value in expected.items()):
        raise ChampionContractError("feature_snapshot_verification_binding_mismatch")
    latest_source_availability = max(source.available_at for source in snapshot.sources)
    if proof.verified_at < latest_source_availability:
        raise ChampionContractError("feature_snapshot_proof_predates_source")
    if proof.verified_at > decision_time:
        raise ChampionContractError("feature_snapshot_proof_after_decision")
    if proof.valid_until < decision_time:
        raise ChampionContractError("feature_snapshot_proof_expired")
    return proof


def _score_receipt_payload(
    *,
    champion: FrozenChampionSpec,
    selection_context: ChampionSelectionContext,
    selection_verification_receipt_sha256: str,
    feature_snapshot: NumericPITFeatureSnapshotReceipt,
    feature_verification_receipt_sha256: str,
    rank_score: float,
) -> dict[str, object]:
    return {
        "contract": "tradingagent.champion_score_receipt.v2",
        "champion_id": champion.champion_id,
        "champion_version": champion.version,
        "champion_spec_manifest_sha256": champion.manifest_sha256,
        "selection_context_sha256": selection_context.context_sha256,
        "selection_receipt_sha256": selection_context.selection_receipt_sha256,
        "champion_selection_manifest_sha256": (
            selection_context.selection_manifest_sha256
        ),
        "selected_artifact_sha256": selection_context.selected_artifact_sha256,
        "selected_model_id": selection_context.selected_model_id,
        "selected_model_version": selection_context.selected_model_version,
        "frozen_champion_spec_manifest_sha256": (
            selection_context.frozen_champion_spec_manifest_sha256
        ),
        "selection_verification_receipt_sha256": (
            selection_verification_receipt_sha256
        ),
        "feature_snapshot_receipt_sha256": feature_snapshot.receipt_sha256,
        "feature_implementation_sha256": (
            feature_snapshot.feature_implementation_sha256
        ),
        "normalization_version": feature_snapshot.normalization_version,
        "feature_verification_receipt_sha256": (feature_verification_receipt_sha256),
        "rank_score": rank_score,
        "score_semantics": champion.score_semantics,
        "calibration_status": "not_calibrated",
        "statistical_promotion_eligible": False,
    }


def verify_champion_score_receipt(
    receipt: ChampionScoreReceipt,
    *,
    expected_symbol: str,
    expected_decision_time: datetime,
    current_selection_context: ChampionSelectionContext,
    selection_verifier: ChampionSelectionVerifier,
    feature_snapshot_verifier: NumericPITFeatureSnapshotVerifier,
) -> None:
    _verify_champion_score_receipt_integrity(receipt)
    if receipt.selection_context != current_selection_context:
        raise ChampionContractError("score_receipt_champion_selection_mismatch")
    selection_proof = _verify_current_champion_selection(
        context=current_selection_context,
        champion=receipt.champion,
        decision_time=_aware(
            expected_decision_time,
            field_name="expected_decision_time",
        ),
        verifier=selection_verifier,
    )
    if (
        receipt.selection_verification_receipt_sha256
        != selection_proof.verification_receipt_sha256
    ):
        raise ChampionContractError("score_receipt_selection_proof_mismatch")
    feature_proof = _verify_numeric_pit_feature_snapshot(
        snapshot=receipt.feature_snapshot,
        decision_time=_aware(
            expected_decision_time,
            field_name="expected_decision_time",
        ),
        verifier=feature_snapshot_verifier,
    )
    if (
        receipt.feature_verification_receipt_sha256
        != feature_proof.verification_receipt_sha256
    ):
        raise ChampionContractError("score_receipt_feature_proof_mismatch")
    if receipt.symbol != _text(expected_symbol, field_name="expected_symbol"):
        raise ChampionContractError("score_receipt_symbol_mismatch")
    if _instant(receipt.decision_time) != _instant(
        _aware(expected_decision_time, field_name="expected_decision_time")
    ):
        raise ChampionContractError("score_receipt_decision_time_mismatch")


def _verify_champion_score_receipt_integrity(
    receipt: ChampionScoreReceipt,
) -> None:
    if not isinstance(receipt, ChampionScoreReceipt):
        raise ChampionContractError("champion_score_receipt_required")
    if not isinstance(receipt.champion, FrozenChampionSpec):
        raise ChampionContractError("frozen_champion_required")
    if not isinstance(receipt.selection_context, ChampionSelectionContext):
        raise ChampionContractError("champion_selection_context_required")
    if not isinstance(receipt.feature_snapshot, NumericPITFeatureSnapshotReceipt):
        raise ChampionContractError("numeric_pit_feature_snapshot_required")
    for field_name, value in (
        (
            "selection_verification_receipt_sha256",
            receipt.selection_verification_receipt_sha256,
        ),
        (
            "feature_verification_receipt_sha256",
            receipt.feature_verification_receipt_sha256,
        ),
    ):
        _sha256_text(value, field_name=field_name)
    if (
        receipt.selection_context.selected_model_id != receipt.champion.champion_id
        or receipt.selection_context.selected_model_version != receipt.champion.version
        or receipt.selection_context.frozen_champion_spec_manifest_sha256
        != receipt.champion.manifest_sha256
    ):
        raise ChampionContractError("champion_selection_model_binding_mismatch")
    if receipt.feature_namespace != receipt.champion.feature_namespace:
        raise ChampionContractError("score_receipt_feature_namespace_mismatch")
    values_by_name = dict(receipt.feature_values)
    if set(values_by_name) != set(receipt.champion.feature_names):
        raise ChampionContractError("score_receipt_feature_contract_mismatch")
    values: list[float] = []
    for name in receipt.champion.feature_names:
        raw = values_by_name[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ChampionContractError("feature_value_invalid")
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ChampionContractError("feature_value_out_of_range")
        values.append(value)
    expected_rank = round(
        sum(
            value * float(weight)
            for value, weight in zip(values, receipt.champion.feature_weights)
        ),
        12,
    )
    if receipt.rank_score != expected_rank:
        raise ChampionContractError("score_receipt_rank_mismatch")
    if (
        receipt.score_semantics != receipt.champion.score_semantics
        or receipt.evidence_class != "champion_numeric_pit_score_receipt"
        or receipt.calibrated_probability is not None
        or receipt.calibration_status != "not_calibrated"
        or receipt.execution_eligible is not True
        or receipt.statistical_promotion_eligible is not False
    ):
        raise ChampionContractError("score_receipt_authority_invalid")
    expected_receipt_sha = _canonical_sha256(
        _score_receipt_payload(
            champion=receipt.champion,
            selection_context=receipt.selection_context,
            selection_verification_receipt_sha256=(
                receipt.selection_verification_receipt_sha256
            ),
            feature_snapshot=receipt.feature_snapshot,
            feature_verification_receipt_sha256=(
                receipt.feature_verification_receipt_sha256
            ),
            rank_score=receipt.rank_score,
        )
    )
    if receipt.receipt_sha256 != expected_receipt_sha:
        raise ChampionContractError("champion_score_receipt_hash_mismatch")


def fixture_rank_evidence(
    *,
    champion_selection_manifest_sha256: str,
    symbol: str,
    decision_time: datetime,
    fixture_id: str,
    source_fixture_sha256: str,
    rank_score: object,
) -> FixtureRankEvidence:
    selection_manifest = _sha256_text(
        champion_selection_manifest_sha256,
        field_name="champion_selection_manifest_sha256",
    )
    normalized_symbol = _text(symbol, field_name="symbol")
    resolved_decision_time = _aware(decision_time, field_name="decision_time")
    normalized_fixture_id = _text(fixture_id, field_name="fixture_id")
    source_sha = _sha256_text(
        source_fixture_sha256,
        field_name="source_fixture_sha256",
    )
    if isinstance(rank_score, bool) or not isinstance(rank_score, (int, float)):
        raise ChampionContractError("rank_score_invalid")
    score = float(rank_score)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ChampionContractError("rank_score_out_of_range")
    payload = _fixture_rank_payload(
        champion_selection_manifest_sha256=selection_manifest,
        symbol=normalized_symbol,
        decision_time=resolved_decision_time,
        fixture_id=normalized_fixture_id,
        source_fixture_sha256=source_sha,
        rank_score=score,
    )
    return FixtureRankEvidence(
        champion_selection_manifest_sha256=selection_manifest,
        symbol=normalized_symbol,
        decision_time=resolved_decision_time,
        fixture_id=normalized_fixture_id,
        source_fixture_sha256=source_sha,
        rank_score=score,
        receipt_sha256=_canonical_sha256(payload),
    )


def _fixture_rank_payload(
    *,
    champion_selection_manifest_sha256: str,
    symbol: str,
    decision_time: datetime,
    fixture_id: str,
    source_fixture_sha256: str,
    rank_score: float,
) -> dict[str, object]:
    return {
        "contract": "tradingagent.offline_fixture_rank_evidence.v1",
        "champion_selection_manifest_sha256": (champion_selection_manifest_sha256),
        "symbol": symbol,
        "decision_time": _instant(decision_time),
        "fixture_id": fixture_id,
        "source_fixture_sha256": source_fixture_sha256,
        "rank_score": rank_score,
        "score_semantics": "uncalibrated_deterministic_rank_score",
        "evidence_class": "offline_engineering_fixture_rank",
        "statistical_promotion_eligible": False,
    }


def verify_fixture_rank_evidence(
    evidence: FixtureRankEvidence,
    *,
    expected_symbol: str,
    expected_decision_time: datetime,
    expected_champion_selection_manifest_sha256: str,
) -> None:
    if not isinstance(evidence, FixtureRankEvidence):
        raise ChampionContractError("fixture_rank_evidence_required")
    if evidence.symbol != _text(expected_symbol, field_name="expected_symbol"):
        raise ChampionContractError("fixture_rank_symbol_mismatch")
    if _instant(evidence.decision_time) != _instant(
        _aware(expected_decision_time, field_name="expected_decision_time")
    ):
        raise ChampionContractError("fixture_rank_decision_time_mismatch")
    expected_manifest = _sha256_text(
        expected_champion_selection_manifest_sha256,
        field_name="expected_champion_selection_manifest_sha256",
    )
    if evidence.champion_selection_manifest_sha256 != expected_manifest:
        raise ChampionContractError("fixture_rank_champion_selection_mismatch")
    _text(evidence.fixture_id, field_name="fixture_id")
    _sha256_text(evidence.source_fixture_sha256, field_name="source_fixture_sha256")
    if (
        not math.isfinite(evidence.rank_score)
        or not 0.0 <= evidence.rank_score <= 1.0
        or evidence.score_semantics != "uncalibrated_deterministic_rank_score"
        or evidence.evidence_class != "offline_engineering_fixture_rank"
        or evidence.execution_eligible is not True
        or evidence.statistical_promotion_eligible is not False
    ):
        raise ChampionContractError("fixture_rank_authority_invalid")
    expected_receipt_sha = _canonical_sha256(
        _fixture_rank_payload(
            champion_selection_manifest_sha256=(
                evidence.champion_selection_manifest_sha256
            ),
            symbol=evidence.symbol,
            decision_time=evidence.decision_time,
            fixture_id=evidence.fixture_id,
            source_fixture_sha256=evidence.source_fixture_sha256,
            rank_score=evidence.rank_score,
        )
    )
    if evidence.receipt_sha256 != expected_receipt_sha:
        raise ChampionContractError("fixture_rank_receipt_hash_mismatch")


@dataclass(frozen=True)
class ShadowChallenger:
    challenger_id: str
    version: str
    champion_manifest_sha256: str
    lifecycle_state: str = "shadow_only"
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        _text(self.challenger_id, field_name="challenger_id")
        _text(self.version, field_name="version")
        if (
            not isinstance(self.champion_manifest_sha256, str)
            or len(self.champion_manifest_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.champion_manifest_sha256
            )
        ):
            raise ChampionContractError("champion_manifest_sha256_invalid")
        if self.lifecycle_state != "shadow_only" or self.execution_eligible:
            raise ChampionContractError("challenger_must_be_shadow_only")

    def promote_automatically(self) -> None:
        raise ChampionContractError("automatic_promotion_forbidden")
