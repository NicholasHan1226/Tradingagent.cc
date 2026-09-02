"""Frozen hashed first paper Champion for the 科技+医药 capital-backed session.

This is the designated simulation-only rank Champion for
``Ashare.capital_backed_paper_runner``.  It reuses the existing V1
``FrozenChampionSpec`` identity (``ashare-mainboard-rank-v1``) already
exercised by repository tests.  Binding it to the hashed 科技+医药 universe
does **not** claim predictive validity, calibrated probability, SampleJournal
KPI, completed round trips, or a promotion from evolution evidence.

The spec is content-addressed in-repo so a host can replay
``Ashare.paper_champion_bootstrap`` into an explicit registry root.  An empty
registry still fail-closes as ``champion_current_unavailable``; this module
never writes a handwritten ``current.json``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Mapping

from shared.portfolio.champion import (
    PHASE1_NUMERIC_FEATURE_NAMESPACE,
    FrozenChampionSpec,
)

from .capital_backed_paper_universe import (
    FROZEN_UNIVERSE_SHA256,
    UNIVERSE_CONTRACT_ID,
)


PAPER_CHAMPION_BOOTSTRAP_CONTRACT_ID = (
    "tradingagent.ashare.paper_champion_bootstrap.v1"
)
PAPER_CHAMPION_SELECTION_ID = "ashare-paper-champion-bootstrap-v1"
PAPER_CHAMPION_MODEL_ID = "ashare-mainboard-rank-v1"
PAPER_CHAMPION_MODEL_VERSION = "1.0.0"
PAPER_CHAMPION_FEATURE_NAMES: tuple[str, ...] = (
    "quality_score",
    "value_score",
    "momentum_score",
    "low_volatility_score",
)
PAPER_CHAMPION_FEATURE_WEIGHTS: tuple[float, ...] = (0.30, 0.20, 0.30, 0.20)
PAPER_CHAMPION_DECISION_HORIZON = "5d"
PAPER_CHAMPION_TRAINED_THROUGH = "2026-06-30"
PAPER_CHAMPION_CATALOG_VERSION = "ashare-paper-champion-catalog-v1"
PAPER_CHAMPION_CREATED_BY = "ashare-paper-champion-bootstrap"

# Frozen causal timestamps so the manifest, receipt and pointer are replayable.
PLAN_FROZEN_AT = datetime(2026, 6, 30, 16, 0, tzinfo=timezone.utc)
MANIFEST_CREATED_AT = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
LIFECYCLE_RECORDED_AT = datetime(2026, 7, 1, 0, 1, tzinfo=timezone.utc)
SELECTION_RECORDED_AT = datetime(2026, 7, 1, 0, 2, tzinfo=timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def frozen_paper_champion_spec() -> FrozenChampionSpec:
    """Return the immutable V1 rank spec bound to this paper designation."""

    return FrozenChampionSpec(
        champion_id=PAPER_CHAMPION_MODEL_ID,
        version=PAPER_CHAMPION_MODEL_VERSION,
        feature_names=PAPER_CHAMPION_FEATURE_NAMES,
        feature_weights=PAPER_CHAMPION_FEATURE_WEIGHTS,
        decision_horizon=PAPER_CHAMPION_DECISION_HORIZON,
        trained_through=PAPER_CHAMPION_TRAINED_THROUGH,
    )


def paper_champion_designation_payload() -> dict[str, object]:
    """Content-addressed designation.  Not SampleJournal promotion evidence."""

    spec = frozen_paper_champion_spec()
    return {
        "account_type": "simulated",
        "automatic_risk_expansion_enabled": False,
        "capital_layer": "simulated",
        "champion_id": spec.champion_id,
        "champion_version": spec.version,
        "contract_id": PAPER_CHAMPION_BOOTSTRAP_CONTRACT_ID,
        "decision_horizon": spec.decision_horizon,
        "feature_names": list(spec.feature_names),
        "feature_namespace": spec.feature_namespace,
        "feature_weights": [float(weight) for weight in spec.feature_weights],
        "frozen_champion_spec_manifest_sha256": spec.manifest_sha256,
        "live_transition_authorized": False,
        "not_predictive_evidence": True,
        "not_sample_journal_promotion": True,
        "real_trading_enabled": False,
        "score_semantics": spec.score_semantics,
        "simulation_only": True,
        "trained_through": spec.trained_through,
        "universe_contract_id": UNIVERSE_CONTRACT_ID,
        "universe_sha256": FROZEN_UNIVERSE_SHA256,
    }


def paper_champion_designation_sha256() -> str:
    return _canonical_sha256(paper_champion_designation_payload())


def paper_champion_validation_plan_fields() -> dict[str, object]:
    """Generic bootstrap plan.  Not a production A-share calendar or OOS claim."""

    designation = paper_champion_designation_sha256()
    return {
        "train_start": date(2025, 1, 2),
        "train_end": date(2025, 1, 31),
        "validation_start": date(2025, 2, 10),
        "validation_end": date(2025, 2, 28),
        "test_start": date(2025, 3, 10),
        "test_end": date(2025, 3, 31),
        "purge_days": 5,
        "embargo_days": 5,
        "label_horizon_days": 5,
        "max_feature_lookback_days": 5,
        "event_cluster_embargo_days": 5,
        "decision_cluster_key": "decision_cluster_id",
        "decision_cluster_deduplicated": True,
        "registered_trial_count": 1,
        "multiple_testing_trial_budget": 20,
        "pbo_required": True,
        "deflated_sharpe_required": True,
        "oos_reuse_count": 0,
        "max_oos_reuse_count": 1,
        "oos_used_for_tuning": False,
        "oos_authority_receipt_sha256": designation,
        "experiment_family_id": PAPER_CHAMPION_BOOTSTRAP_CONTRACT_ID,
        "experiment_id": PAPER_CHAMPION_SELECTION_ID,
        "frozen_test_set_id": "ashare-paper-champion-bootstrap-designation",
        "frozen_at": PLAN_FROZEN_AT,
        "market": "generic",
    }


def paper_champion_research_snapshot_sha256() -> str:
    spec = frozen_paper_champion_spec()
    return _canonical_sha256(
        {
            "frozen_champion_spec_manifest_sha256": spec.manifest_sha256,
            "universe_sha256": FROZEN_UNIVERSE_SHA256,
        }
    )


__all__ = [
    "LIFECYCLE_RECORDED_AT",
    "MANIFEST_CREATED_AT",
    "PAPER_CHAMPION_BOOTSTRAP_CONTRACT_ID",
    "PAPER_CHAMPION_CATALOG_VERSION",
    "PAPER_CHAMPION_CREATED_BY",
    "PAPER_CHAMPION_DECISION_HORIZON",
    "PAPER_CHAMPION_FEATURE_NAMES",
    "PAPER_CHAMPION_FEATURE_WEIGHTS",
    "PAPER_CHAMPION_MODEL_ID",
    "PAPER_CHAMPION_MODEL_VERSION",
    "PAPER_CHAMPION_SELECTION_ID",
    "PAPER_CHAMPION_TRAINED_THROUGH",
    "PHASE1_NUMERIC_FEATURE_NAMESPACE",
    "PLAN_FROZEN_AT",
    "SELECTION_RECORDED_AT",
    "frozen_paper_champion_spec",
    "paper_champion_designation_payload",
    "paper_champion_designation_sha256",
    "paper_champion_research_snapshot_sha256",
    "paper_champion_validation_plan_fields",
]
