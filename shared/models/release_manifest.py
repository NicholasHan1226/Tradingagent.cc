"""Immutable, simulation-only model release manifest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Tuple


class ReleaseManifestContractError(ValueError):
    """Raised when a release manifest is mutable, ambiguous, or unsafe."""


_RESERVED_SAFETY_MARKERS = frozenset(
    {
        "account_type",
        "automatic_promotion_enabled",
        "automatic_risk_expansion_enabled",
        "broker_connected",
        "catalog_version",
        "capital_layer",
        "deployment_mode",
        "live_transition_authorized",
        "real_trading_enabled",
        "research_snapshot_sha256",
        "validation_evidence_sha256",
        "validation_plan_sha256",
    }
)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseManifestContractError("%s_must_be_nonempty_text" % field_name)


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ReleaseManifestContractError("%s_invalid" % field_name)


@dataclass(frozen=True)
class ModelReleaseManifest:
    """Content-addressed model identity, not a deployment authorization."""

    manifest_id: str
    model_id: str
    model_version: str
    artifact_sha256: str
    training_data_version: str
    feature_contract_version: str
    validation_plan_sha256: str
    research_snapshot_sha256: str
    catalog_version: str
    validation_evidence_sha256: str
    source_commit: str
    created_at: datetime
    created_by: str
    intended_mode: str
    metadata: Tuple[Tuple[str, str], ...] = ()
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    real_trading_enabled: bool = False
    live_transition_authorized: bool = False
    automatic_promotion_enabled: bool = False
    automatic_risk_expansion_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "manifest_id",
            "model_id",
            "model_version",
            "training_data_version",
            "feature_contract_version",
            "catalog_version",
            "source_commit",
            "created_by",
            "intended_mode",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        _require_sha256(self.validation_plan_sha256, "validation_plan_sha256")
        _require_sha256(self.research_snapshot_sha256, "research_snapshot_sha256")
        _require_sha256(
            self.validation_evidence_sha256,
            "validation_evidence_sha256",
        )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ReleaseManifestContractError("created_at_must_be_timezone_aware")
        if self.intended_mode not in {"shadow", "paper", "backtest"}:
            raise ReleaseManifestContractError("intended_mode_must_be_offline")
        if not isinstance(self.metadata, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
            for item in self.metadata
        ):
            raise ReleaseManifestContractError("metadata_must_be_immutable_text_pairs")
        if any(
            key.strip().lower() in _RESERVED_SAFETY_MARKERS
            for key, _value in self.metadata
        ):
            raise ReleaseManifestContractError("reserved_safety_marker_in_metadata")
        if (
            self.capital_layer != "simulated"
            or self.account_type != "simulated"
            or self.real_trading_enabled is not False
            or self.live_transition_authorized is not False
            or self.automatic_promotion_enabled is not False
            or self.automatic_risk_expansion_enabled is not False
        ):
            raise ReleaseManifestContractError("simulation_only_contract_violated")

    def canonical_payload(self) -> dict:
        return {
            "account_type": self.account_type,
            "artifact_sha256": self.artifact_sha256,
            "automatic_promotion_enabled": self.automatic_promotion_enabled,
            "automatic_risk_expansion_enabled": self.automatic_risk_expansion_enabled,
            "catalog_version": self.catalog_version,
            "capital_layer": self.capital_layer,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "feature_contract_version": self.feature_contract_version,
            "intended_mode": self.intended_mode,
            "live_transition_authorized": self.live_transition_authorized,
            "manifest_id": self.manifest_id,
            "metadata": list(self.metadata),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "real_trading_enabled": self.real_trading_enabled,
            "research_snapshot_sha256": self.research_snapshot_sha256,
            "source_commit": self.source_commit,
            "training_data_version": self.training_data_version,
            "validation_evidence_sha256": self.validation_evidence_sha256,
            "validation_plan_sha256": self.validation_plan_sha256,
        }

    def sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
