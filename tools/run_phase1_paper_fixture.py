#!/usr/bin/env python3
"""Run one deterministic, offline-only Phase 1 A-share paper-day fixture.

This entrypoint is intentionally unable to construct a real HTTP or broker
adapter.  It reads frozen response data, injects the TradingDatas V1 wire
contract through the retained compatibility client, and publishes only a
local-candidate report under an explicit output root.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.data.evidence_gate import (  # noqa: E402
    DatasetEvidencePolicy,
    EvidenceAction,
)
from shared.data.research_snapshot import (  # noqa: E402
    DatasetRequirement,
    ResearchDataProfile,
)
from shared.data.research_snapshot_store import (  # noqa: E402
    FileResearchSnapshotStore,
    ResearchSnapshotStoreConflict,
    ResearchSnapshotStoreCorruption,
)
from shared.data.sharedsignals_v1 import (  # noqa: E402
    CATALOG_PATH,
    QUERY_PATH,
    HTTPResponse,
    QueryRequest,
)
from shared.models.drift_action_store import (  # noqa: E402
    DriftActionStore,
    DriftActionStoreError,
)
from shared.models.drift_runtime import DriftRuntimeRiskAdapter  # noqa: E402
from shared.portfolio.small_account_optimizer import (  # noqa: E402
    AccountAuthorityVerification,
    AccountAuthoritySnapshot,
    AccountAuthorityVerifier,
    AccountPositionSnapshot,
    CandidateAllocationInput,
    PositionReductionIntent,
)
from shared.portfolio.champion import fixture_rank_evidence  # noqa: E402
from shared.portfolio.thesis_risk import (  # noqa: E402
    ThesisRiskDimensionCap,
    ThesisRiskExposureReceipt,
    ThesisRiskExposureSetReceipt,
    ThesisRiskExposureSetVerification,
    ThesisRiskExposureVerification,
    ThesisRiskGroups,
    ThesisRiskPolicy,
    ThesisRiskPolicyVerification,
    ThesisRiskRuntimeAuthority,
)
from shared.review.sample_journal import JournalError, SampleJournal  # noqa: E402
import shared.review.sample_journal as _sample_journal_module  # noqa: E402
from shared.runtime.composition import (  # noqa: E402
    FrozenFixtureHTTPTransport,
    FrozenFixtureStagePort,
    PaperRuntimeConfig,
    PaperRuntimeCompositionError,
    compose_paper_runtime,
)
from shared.runtime.file_store import (  # noqa: E402
    FileRunBundleStore,
    RunBundleStoreCorruption,
)
from shared.runtime.publisher import (  # noqa: E402
    LocalRunBundlePublisher,
    RunBundlePublishError,
)
from shared.runtime.run_bundle import ComponentIdentity, RunStage  # noqa: E402
from shared.runtime.small_account_stage import (  # noqa: E402
    SmallAccountDecisionStagePort,
    SmallAccountStageContractError,
)
from shared.runtime.stage_ports import StagePortContractError  # noqa: E402
from shared.universe.policy import (  # noqa: E402
    CanonicalMainboardScopePolicy,
)
import Ashare.sample_pipeline as _ashare_sample_pipeline  # noqa: E402


_BUSINESS_STAGES = frozenset(
    {
        RunStage.PREOPEN,
        RunStage.UNIVERSE_READY,
        RunStage.DECISION_READY,
        RunStage.RISK_CHECKED,
        RunStage.ORDERS_SIMULATED,
        RunStage.RECONCILED,
    }
)
_MANAGED_STAGES = frozenset(
    {
        RunStage.EVIDENCE_READY,
        RunStage.LEARNING_RECORDED,
        RunStage.REPORTED,
    }
)


class FrozenFixtureAccountAuthorityVerifier(AccountAuthorityVerifier):
    """Detached, non-promotable proof for this network-closed fixture only."""

    def __init__(
        self,
        *,
        expected_snapshot: AccountAuthoritySnapshot,
        decision_time: datetime,
    ) -> None:
        self._proof = AccountAuthorityVerification.create(
            snapshot=expected_snapshot,
            verifier_id="phase1-paper-fixture-account-authority",
            verifier_version="1",
            verified_at=expected_snapshot.account_as_of,
            valid_until=decision_time,
            promotion_eligible=False,
        )

    def verify(
        self,
        snapshot: AccountAuthoritySnapshot,
        *,
        decision_time: datetime,
    ) -> AccountAuthorityVerification:
        return self._proof


_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "offline_fixture",
        "config",
        "transport_responses",
        "managed_stage_identities",
        "scope_policy_identity",
        "business_stage_payloads",
        "small_account_optimizer",
    }
)
_CONFIG_FIELDS = frozenset(
    {
        "trade_date",
        "decision_as_of",
        "tradingdatas_v1_base_url",
        "tradingdatas_catalog_version",
        "tradingdatas_access_policy_id",
        "capital_authority_id",
        "authority_generation",
        "execution_lineage",
        "champion_manifest_sha256",
        "real_trading_enabled",
        "live_execution_enabled",
        "network_enabled",
        "profile_id",
        "datasets",
    }
)
_DATASET_FIELDS = frozenset(
    {
        "dataset_id",
        "schema_major",
        "role",
        "fields",
        "filters",
        "identity_fields",
        "observation_mode",
        "query_as_of_mode",
        "row_event_time_field",
        "row_event_time_format",
        "row_event_timezone",
        "row_event_time_semantic",
        "minimum_row_count",
        "max_pages",
        "max_rows",
        "limit",
        "cursor",
        "evidence_policy",
    }
)
_EVIDENCE_POLICY_FIELDS = frozenset(
    {
        "degraded_action",
        "stale_action",
        "degraded_weight",
        "stale_weight",
    }
)
_IDENTITY_FIELDS = frozenset({"component_id", "version", "artifact_sha256"})
_SMALL_ACCOUNT_OPTIMIZER_FIELDS = frozenset(
    {
        "identity",
        "runtime_environment",
        "promotion_eligible",
        "decision_time",
        "account_snapshot",
        "candidates",
        "reduction_intents",
        "thesis_risk_authority",
    }
)
_ACCOUNT_SNAPSHOT_FIELDS = frozenset(
    {
        "capital_authority_id",
        "authority_generation",
        "account_as_of",
        "available_cash_cny",
        "current_gross_cny",
        "positions",
        "position_snapshot_receipt_id",
        "position_snapshot_sha256",
        "verification_receipt_sha256",
        "authority_source_class",
    }
)
_POSITION_FIELDS = frozenset(
    {
        "symbol",
        "total_shares",
        "sellable_shares",
        "mark_price_cny",
        "price_observed_at",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "symbol",
        "fixture_rank_score",
        "score_evidence_class",
        "price_observed_at",
        "decision_reference_price",
        "source_fixture_sha256",
    }
)
_REDUCTION_INTENT_FIELDS = frozenset({"intent_id", "symbol", "action", "target_shares"})
_THESIS_RISK_AUTHORITY_FIELDS = frozenset(
    {
        "decision_time",
        "policy",
        "policy_proof",
        "exposure_receipts",
        "exposure_proofs",
        "exposure_set_receipt",
        "exposure_set_proof",
        "initial_group_exposures",
        "authority_sha256",
    }
)
_THESIS_RISK_GROUP_FIELDS = frozenset(
    {
        "industry",
        "thesis",
        "raw_material",
        "policy_event",
        "crowding",
        "model_family",
    }
)
_THESIS_RISK_CAP_FIELDS = frozenset({"dimension", "max_exposure_cny"})
_THESIS_RISK_POLICY_FIELDS = frozenset(
    {
        "policy_id",
        "reviewed_by",
        "review_reference",
        "effective_at",
        "valid_until",
        "dimension_caps",
        "policy_sha256",
    }
)
_THESIS_RISK_POLICY_PROOF_FIELDS = frozenset(
    {
        "verifier_id",
        "verifier_version",
        "policy_id",
        "policy_sha256",
        "reviewed_by",
        "review_reference",
        "verified_at",
        "valid_until",
        "promotion_eligible",
        "proof_sha256",
    }
)
_THESIS_RISK_EXPOSURE_FIELDS = frozenset(
    {
        "exposure_id",
        "exposure_kind",
        "symbol",
        "groups",
        "notional_cny",
        "as_of",
        "available_at",
        "source_dataset_id",
        "source_receipt_id",
        "source_lineage_sha256",
        "source_content_sha256",
        "binding_reference_id",
        "binding_sha256",
        "receipt_sha256",
        "pending_action",
    }
)
_THESIS_RISK_EXPOSURE_PROOF_FIELDS = frozenset(
    {
        "verifier_id",
        "verifier_version",
        "exposure_id",
        "exposure_receipt_sha256",
        "authority_notional_cny",
        "authority_binding_reference_id",
        "authority_binding_sha256",
        "verified_at",
        "valid_until",
        "promotion_eligible",
        "proof_sha256",
    }
)
_THESIS_RISK_SET_FIELDS = frozenset(
    {
        "exposure_set_id",
        "decision_time",
        "as_of",
        "available_at",
        "source_id",
        "source_generation",
        "source_lineage_sha256",
        "exposure_receipt_sha256s",
        "candidate_count",
        "position_count",
        "pending_count",
        "receipt_sha256",
    }
)
_THESIS_RISK_SET_PROOF_FIELDS = frozenset(
    {
        "verifier_id",
        "verifier_version",
        "exposure_set_id",
        "exposure_set_receipt_sha256",
        "source_generation",
        "source_lineage_sha256",
        "verified_at",
        "valid_until",
        "promotion_eligible",
        "proof_sha256",
    }
)
_THESIS_RISK_INITIAL_EXPOSURE_FIELDS = frozenset(
    {"dimension", "group_id", "exposure_cny"}
)
TA_PROJECT_ROOT = (
    REPO_ROOT.parent.parent if REPO_ROOT.parent.name == ".worktrees" else REPO_ROOT
)
_FIXTURE_RUNTIME_RELATIVE_ROOT = (
    Path("shared") / "runtime_test" / "phase1_paper_fixture"
)


class FixtureCLIError(RuntimeError):
    """Raised when the local fixture is incomplete or unsafe."""


def _fixture_record(record: Mapping[str, Any]) -> dict[str, Any]:
    marked = dict(record)
    marked["source_class"] = "fixture"
    marked["promotion_eligible"] = False
    return marked


class _OfflineFixtureSampleJournal(SampleJournal):
    """Seal fixture provenance before any journal event is hashed or written."""

    def append_predictions(
        self,
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return super().append_predictions(
            [_fixture_record(candidate) for candidate in candidates]
        )

    def append_samples(
        self,
        samples: Sequence[Mapping[str, Any]],
        *,
        expected_event_count: int | None = None,
    ) -> list[dict[str, Any]]:
        return super().append_samples(
            [_fixture_record(sample) for sample in samples],
            expected_event_count=expected_event_count,
        )


@contextmanager
def _offline_fixture_journal_factory():
    """Keep the offline outcome pipeline on the provenance-sealing journal."""

    original = _ashare_sample_pipeline.SampleJournal
    _ashare_sample_pipeline.SampleJournal = _OfflineFixtureSampleJournal
    try:
        yield
    finally:
        _ashare_sample_pipeline.SampleJournal = original


@contextmanager
def _offline_fixture_authority_scope(
    *,
    capital_authority_id: str,
    authority_generation: int,
    execution_lineage: str,
):
    """Temporarily isolate authority checks inside this offline CLI process."""

    original_authority = _ashare_sample_pipeline.ASHARE_CAPITAL_AUTHORITY_ID
    original_generation = _ashare_sample_pipeline.ASHARE_AUTHORITY_GENERATION
    original_lineage = _ashare_sample_pipeline.ASHARE_EXECUTION_LINEAGE_ID
    if (
        capital_authority_id == original_authority
        or execution_lineage == original_lineage
    ):
        raise FixtureCLIError("fixture_authority_or_lineage_not_isolated")
    journal_original_authority = _sample_journal_module.ASHARE_CAPITAL_AUTHORITY_ID
    journal_original_generation = _sample_journal_module.ASHARE_AUTHORITY_GENERATION
    journal_original_lineage = _sample_journal_module.ASHARE_EXECUTION_LINEAGE_ID
    if (
        isinstance(authority_generation, bool)
        or not isinstance(authority_generation, int)
        or authority_generation <= 0
    ):
        raise FixtureCLIError("fixture_authority_generation_invalid")
    _ashare_sample_pipeline.ASHARE_CAPITAL_AUTHORITY_ID = capital_authority_id
    _ashare_sample_pipeline.ASHARE_AUTHORITY_GENERATION = authority_generation
    _ashare_sample_pipeline.ASHARE_EXECUTION_LINEAGE_ID = execution_lineage
    _sample_journal_module.ASHARE_CAPITAL_AUTHORITY_ID = capital_authority_id
    _sample_journal_module.ASHARE_AUTHORITY_GENERATION = authority_generation
    _sample_journal_module.ASHARE_EXECUTION_LINEAGE_ID = execution_lineage
    try:
        yield
    finally:
        _ashare_sample_pipeline.ASHARE_CAPITAL_AUTHORITY_ID = original_authority
        _ashare_sample_pipeline.ASHARE_AUTHORITY_GENERATION = original_generation
        _ashare_sample_pipeline.ASHARE_EXECUTION_LINEAGE_ID = original_lineage
        _sample_journal_module.ASHARE_CAPITAL_AUTHORITY_ID = journal_original_authority
        _sample_journal_module.ASHARE_AUTHORITY_GENERATION = journal_original_generation
        _sample_journal_module.ASHARE_EXECUTION_LINEAGE_ID = journal_original_lineage


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_fixture_output_root(output_root: Path) -> Path:
    """Reject the TA source roots, including aliases through existing symlinks."""

    lexical_root = Path(os.path.abspath(os.fspath(output_root)))
    try:
        resolved_root = lexical_root.resolve(strict=False)
        protected_roots = {
            REPO_ROOT.resolve(strict=False),
            TA_PROJECT_ROOT.resolve(strict=False),
            (REPO_ROOT / "shared" / "review").resolve(strict=False),
            (TA_PROJECT_ROOT / "shared" / "review").resolve(strict=False),
        }
    except (OSError, RuntimeError) as exc:
        raise FixtureCLIError("fixture_output_root_resolution_failed") from exc
    if any(_is_within(resolved_root, root) for root in protected_roots):
        raise FixtureCLIError("fixture_output_root_protected")
    # Preserve the lexical path so downstream stores can still reject generic
    # symlink components instead of silently following them.
    return lexical_root


def _object(
    value: object,
    *,
    field_name: str,
    exact_fields: frozenset[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureCLIError(f"{field_name}_must_be_object")
    copied = dict(value)
    if exact_fields is not None and set(copied) != exact_fields:
        raise FixtureCLIError(f"{field_name}_fields_invalid")
    if any(not isinstance(key, str) or not key for key in copied):
        raise FixtureCLIError(f"{field_name}_keys_invalid")
    return copied


def _sequence(value: object, *, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise FixtureCLIError(f"{field_name}_must_be_array")
    return list(value)


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FixtureCLIError(f"{field_name}_invalid")
    return value


def _native_false(value: object, *, field_name: str) -> bool:
    if type(value) is not bool or value:
        raise FixtureCLIError(f"{field_name}_must_be_native_false")
    return False


def _instant(value: object, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            _text(value, field_name=field_name).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise FixtureCLIError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FixtureCLIError(f"{field_name}_timezone_required")
    return parsed


def _reject_json_constant(value: str) -> None:
    raise FixtureCLIError(f"fixture_non_finite_json_forbidden:{value}")


def _load_fixture(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FixtureCLIError("fixture_path_must_be_regular_file")
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureCLIError("fixture_json_invalid") from exc
    fixture = _object(
        decoded,
        field_name="fixture",
        exact_fields=_TOP_LEVEL_FIELDS,
    )
    if fixture["schema_version"] != 2:
        raise FixtureCLIError("fixture_schema_version_invalid")
    if type(fixture["offline_fixture"]) is not bool or not fixture["offline_fixture"]:
        raise FixtureCLIError("offline_fixture_must_be_native_true")
    return fixture


def _response(value: object, *, field_name: str) -> HTTPResponse:
    response = _object(
        value,
        field_name=field_name,
        exact_fields=frozenset({"status_code", "json_body"}),
    )
    return HTTPResponse(
        status_code=response["status_code"],
        json_body=_object(response["json_body"], field_name=f"{field_name}.json_body"),
    )


@dataclass(frozen=True)
class _ParsedFixture:
    config: PaperRuntimeConfig
    transport: FrozenFixtureHTTPTransport
    requests: Mapping[str, QueryRequest]
    business_ports: Mapping[RunStage, FrozenFixtureStagePort]
    small_account_decision_port: SmallAccountDecisionStagePort
    managed_identities: Mapping[RunStage, ComponentIdentity]
    scope_policy: CanonicalMainboardScopePolicy


def _identity(value: object, *, stage: RunStage | None, field_name: str):
    raw = _object(value, field_name=field_name, exact_fields=_IDENTITY_FIELDS)
    return ComponentIdentity(
        stage=stage,
        component_id=raw["component_id"],
        version=raw["version"],
        artifact_sha256=raw["artifact_sha256"],
    )


def _finite_nonnegative(value: object, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise FixtureCLIError(f"{field_name}_invalid")
    return float(value)


def _parse_thesis_risk_authority(
    value: object,
    *,
    decision_time: datetime,
) -> ThesisRiskRuntimeAuthority:
    """Parse one fully materialized, non-promotable fixture authority.

    Every policy, receipt, detached proof, membership receipt, and content hash
    must already exist in the fixture.  This parser verifies those artifacts;
    it never signs, defaults, infers, or refreshes risk evidence at runtime.
    """

    raw = _object(
        value,
        field_name="small_account_optimizer.thesis_risk_authority",
        exact_fields=_THESIS_RISK_AUTHORITY_FIELDS,
    )
    authority_decision_time = _instant(
        raw["decision_time"],
        field_name="thesis_risk_authority.decision_time",
    )
    if authority_decision_time != decision_time:
        raise FixtureCLIError("thesis_risk_authority_decision_time_mismatch")
    try:
        policy_raw = _object(
            raw["policy"],
            field_name="thesis_risk_authority.policy",
            exact_fields=_THESIS_RISK_POLICY_FIELDS,
        )
        caps = tuple(
            ThesisRiskDimensionCap(
                **_object(
                    item,
                    field_name=f"thesis_risk_authority.policy.dimension_caps[{index}]",
                    exact_fields=_THESIS_RISK_CAP_FIELDS,
                )
            )
            for index, item in enumerate(
                _sequence(
                    policy_raw["dimension_caps"],
                    field_name="thesis_risk_authority.policy.dimension_caps",
                )
            )
        )
        policy = ThesisRiskPolicy(
            policy_id=policy_raw["policy_id"],
            reviewed_by=policy_raw["reviewed_by"],
            review_reference=policy_raw["review_reference"],
            effective_at=_instant(
                policy_raw["effective_at"],
                field_name="thesis_risk_authority.policy.effective_at",
            ),
            valid_until=_instant(
                policy_raw["valid_until"],
                field_name="thesis_risk_authority.policy.valid_until",
            ),
            dimension_caps=caps,
            policy_sha256=policy_raw["policy_sha256"],
        )

        policy_proof_raw = _object(
            raw["policy_proof"],
            field_name="thesis_risk_authority.policy_proof",
            exact_fields=_THESIS_RISK_POLICY_PROOF_FIELDS,
        )
        policy_proof = ThesisRiskPolicyVerification(
            verifier_id=policy_proof_raw["verifier_id"],
            verifier_version=policy_proof_raw["verifier_version"],
            policy_id=policy_proof_raw["policy_id"],
            policy_sha256=policy_proof_raw["policy_sha256"],
            reviewed_by=policy_proof_raw["reviewed_by"],
            review_reference=policy_proof_raw["review_reference"],
            verified_at=_instant(
                policy_proof_raw["verified_at"],
                field_name="thesis_risk_authority.policy_proof.verified_at",
            ),
            valid_until=_instant(
                policy_proof_raw["valid_until"],
                field_name="thesis_risk_authority.policy_proof.valid_until",
            ),
            promotion_eligible=_native_false(
                policy_proof_raw["promotion_eligible"],
                field_name=("thesis_risk_authority.policy_proof.promotion_eligible"),
            ),
            proof_sha256=policy_proof_raw["proof_sha256"],
        )

        receipts: list[ThesisRiskExposureReceipt] = []
        for index, item in enumerate(
            _sequence(
                raw["exposure_receipts"],
                field_name="thesis_risk_authority.exposure_receipts",
            )
        ):
            receipt_raw = _object(
                item,
                field_name=f"thesis_risk_authority.exposure_receipts[{index}]",
                exact_fields=_THESIS_RISK_EXPOSURE_FIELDS,
            )
            group_raw = _object(
                receipt_raw["groups"],
                field_name=(f"thesis_risk_authority.exposure_receipts[{index}].groups"),
                exact_fields=_THESIS_RISK_GROUP_FIELDS,
            )
            receipts.append(
                ThesisRiskExposureReceipt(
                    exposure_id=receipt_raw["exposure_id"],
                    exposure_kind=receipt_raw["exposure_kind"],
                    symbol=receipt_raw["symbol"],
                    groups=ThesisRiskGroups(**group_raw),
                    notional_cny=receipt_raw["notional_cny"],
                    as_of=_instant(
                        receipt_raw["as_of"],
                        field_name=f"thesis_risk_receipt[{index}].as_of",
                    ),
                    available_at=_instant(
                        receipt_raw["available_at"],
                        field_name=f"thesis_risk_receipt[{index}].available_at",
                    ),
                    source_dataset_id=receipt_raw["source_dataset_id"],
                    source_receipt_id=receipt_raw["source_receipt_id"],
                    source_lineage_sha256=receipt_raw["source_lineage_sha256"],
                    source_content_sha256=receipt_raw["source_content_sha256"],
                    binding_reference_id=receipt_raw["binding_reference_id"],
                    binding_sha256=receipt_raw["binding_sha256"],
                    receipt_sha256=receipt_raw["receipt_sha256"],
                    pending_action=receipt_raw["pending_action"],
                )
            )

        exposure_proofs: list[ThesisRiskExposureVerification] = []
        for index, item in enumerate(
            _sequence(
                raw["exposure_proofs"],
                field_name="thesis_risk_authority.exposure_proofs",
            )
        ):
            proof_raw = _object(
                item,
                field_name=f"thesis_risk_authority.exposure_proofs[{index}]",
                exact_fields=_THESIS_RISK_EXPOSURE_PROOF_FIELDS,
            )
            exposure_proofs.append(
                ThesisRiskExposureVerification(
                    verifier_id=proof_raw["verifier_id"],
                    verifier_version=proof_raw["verifier_version"],
                    exposure_id=proof_raw["exposure_id"],
                    exposure_receipt_sha256=(proof_raw["exposure_receipt_sha256"]),
                    authority_notional_cny=proof_raw["authority_notional_cny"],
                    authority_binding_reference_id=(
                        proof_raw["authority_binding_reference_id"]
                    ),
                    authority_binding_sha256=(proof_raw["authority_binding_sha256"]),
                    verified_at=_instant(
                        proof_raw["verified_at"],
                        field_name=f"thesis_risk_exposure_proof[{index}].verified_at",
                    ),
                    valid_until=_instant(
                        proof_raw["valid_until"],
                        field_name=f"thesis_risk_exposure_proof[{index}].valid_until",
                    ),
                    promotion_eligible=_native_false(
                        proof_raw["promotion_eligible"],
                        field_name=(
                            f"thesis_risk_exposure_proof[{index}].promotion_eligible"
                        ),
                    ),
                    proof_sha256=proof_raw["proof_sha256"],
                )
            )

        set_raw = _object(
            raw["exposure_set_receipt"],
            field_name="thesis_risk_authority.exposure_set_receipt",
            exact_fields=_THESIS_RISK_SET_FIELDS,
        )
        exposure_set_receipt = ThesisRiskExposureSetReceipt(
            exposure_set_id=set_raw["exposure_set_id"],
            decision_time=_instant(
                set_raw["decision_time"],
                field_name="thesis_risk_exposure_set.decision_time",
            ),
            as_of=_instant(
                set_raw["as_of"],
                field_name="thesis_risk_exposure_set.as_of",
            ),
            available_at=_instant(
                set_raw["available_at"],
                field_name="thesis_risk_exposure_set.available_at",
            ),
            source_id=set_raw["source_id"],
            source_generation=set_raw["source_generation"],
            source_lineage_sha256=set_raw["source_lineage_sha256"],
            exposure_receipt_sha256s=tuple(
                _sequence(
                    set_raw["exposure_receipt_sha256s"],
                    field_name="thesis_risk_exposure_set.receipt_sha256s",
                )
            ),
            candidate_count=set_raw["candidate_count"],
            position_count=set_raw["position_count"],
            pending_count=set_raw["pending_count"],
            receipt_sha256=set_raw["receipt_sha256"],
        )

        set_proof_raw = _object(
            raw["exposure_set_proof"],
            field_name="thesis_risk_authority.exposure_set_proof",
            exact_fields=_THESIS_RISK_SET_PROOF_FIELDS,
        )
        exposure_set_proof = ThesisRiskExposureSetVerification(
            verifier_id=set_proof_raw["verifier_id"],
            verifier_version=set_proof_raw["verifier_version"],
            exposure_set_id=set_proof_raw["exposure_set_id"],
            exposure_set_receipt_sha256=(set_proof_raw["exposure_set_receipt_sha256"]),
            source_generation=set_proof_raw["source_generation"],
            source_lineage_sha256=set_proof_raw["source_lineage_sha256"],
            verified_at=_instant(
                set_proof_raw["verified_at"],
                field_name="thesis_risk_exposure_set_proof.verified_at",
            ),
            valid_until=_instant(
                set_proof_raw["valid_until"],
                field_name="thesis_risk_exposure_set_proof.valid_until",
            ),
            promotion_eligible=_native_false(
                set_proof_raw["promotion_eligible"],
                field_name=("thesis_risk_exposure_set_proof.promotion_eligible"),
            ),
            proof_sha256=set_proof_raw["proof_sha256"],
        )

        initial_exposures = []
        for index, item in enumerate(
            _sequence(
                raw["initial_group_exposures"],
                field_name="thesis_risk_authority.initial_group_exposures",
            )
        ):
            initial_raw = _object(
                item,
                field_name=f"thesis_risk_initial_exposure[{index}]",
                exact_fields=_THESIS_RISK_INITIAL_EXPOSURE_FIELDS,
            )
            initial_exposures.append(
                (
                    initial_raw["dimension"],
                    initial_raw["group_id"],
                    _finite_nonnegative(
                        initial_raw["exposure_cny"],
                        field_name=(
                            f"thesis_risk_initial_exposure[{index}].exposure_cny"
                        ),
                    ),
                )
            )

        if (
            policy_proof.policy_id != policy.policy_id
            or policy_proof.policy_sha256 != policy.policy_sha256
            or policy_proof.reviewed_by != policy.reviewed_by
            or policy_proof.review_reference != policy.review_reference
        ):
            raise ValueError("thesis_risk_policy_proof_binding_mismatch")
        if (
            exposure_set_proof.exposure_set_id != exposure_set_receipt.exposure_set_id
            or exposure_set_proof.exposure_set_receipt_sha256
            != exposure_set_receipt.receipt_sha256
            or exposure_set_proof.source_generation
            != exposure_set_receipt.source_generation
            or exposure_set_proof.source_lineage_sha256
            != exposure_set_receipt.source_lineage_sha256
        ):
            raise ValueError("thesis_risk_exposure_set_proof_binding_mismatch")

        return ThesisRiskRuntimeAuthority(
            decision_time=authority_decision_time,
            policy=policy,
            policy_proof=policy_proof,
            exposure_receipts=tuple(receipts),
            exposure_proofs=tuple(exposure_proofs),
            exposure_set_receipt=exposure_set_receipt,
            exposure_set_proof=exposure_set_proof,
            initial_group_exposures=tuple(initial_exposures),
            authority_sha256=raw["authority_sha256"],
        )
    except FixtureCLIError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise FixtureCLIError("thesis_risk_authority_invalid") from exc


def _parse_fixture(fixture: Mapping[str, Any]) -> _ParsedFixture:
    config_raw = _object(
        fixture["config"],
        field_name="config",
        exact_fields=_CONFIG_FIELDS,
    )
    _native_false(
        config_raw["real_trading_enabled"],
        field_name="real_trading_enabled",
    )
    _native_false(
        config_raw["live_execution_enabled"],
        field_name="live_execution_enabled",
    )
    _native_false(
        config_raw["network_enabled"],
        field_name="network_enabled",
    )
    decision_as_of = _instant(
        config_raw["decision_as_of"],
        field_name="decision_as_of",
    )

    requirements: list[DatasetRequirement] = []
    requests: dict[str, QueryRequest] = {}
    evidence_policies: dict[str, DatasetEvidencePolicy] = {}
    for index, item in enumerate(
        _sequence(config_raw["datasets"], field_name="config.datasets")
    ):
        dataset = _object(
            item,
            field_name=f"config.datasets[{index}]",
            exact_fields=_DATASET_FIELDS,
        )
        dataset_id = _text(dataset["dataset_id"], field_name="dataset_id")
        if dataset_id in requests:
            raise FixtureCLIError(f"duplicate_dataset_id:{dataset_id}")
        requirements.append(
            DatasetRequirement(
                dataset_id=dataset_id,
                role=dataset["role"],
                identity_fields=tuple(
                    _sequence(
                        dataset["identity_fields"],
                        field_name=f"{dataset_id}.identity_fields",
                    )
                ),
                observation_mode=dataset["observation_mode"],
                query_as_of_mode=dataset["query_as_of_mode"],
                row_event_time_field=dataset["row_event_time_field"],
                row_event_time_format=dataset["row_event_time_format"],
                row_event_timezone=dataset["row_event_timezone"],
                row_event_time_semantic=dataset["row_event_time_semantic"],
                minimum_row_count=dataset["minimum_row_count"],
                max_pages=dataset["max_pages"],
                max_rows=dataset["max_rows"],
            )
        )
        fields = _sequence(dataset["fields"], field_name=f"{dataset_id}.fields")
        requests[dataset_id] = QueryRequest(
            dataset_id=dataset_id,
            schema_major=dataset["schema_major"],
            fields=tuple(fields),
            filters=_object(dataset["filters"], field_name=f"{dataset_id}.filters"),
            as_of=(
                decision_as_of.isoformat()
                if dataset["query_as_of_mode"] == "decision_as_of"
                else None
            ),
            limit=dataset["limit"],
            cursor=dataset["cursor"],
        )
        policy = _object(
            dataset["evidence_policy"],
            field_name=f"{dataset_id}.evidence_policy",
            exact_fields=_EVIDENCE_POLICY_FIELDS,
        )
        try:
            degraded_action = EvidenceAction(policy["degraded_action"])
            stale_action = EvidenceAction(policy["stale_action"])
        except (TypeError, ValueError) as exc:
            raise FixtureCLIError(f"{dataset_id}.evidence_action_invalid") from exc
        evidence_policies[dataset_id] = DatasetEvidencePolicy(
            dataset_id=dataset_id,
            degraded_action=degraded_action,
            stale_action=stale_action,
            degraded_weight=policy["degraded_weight"],
            stale_weight=policy["stale_weight"],
        )

    profile = ResearchDataProfile(
        profile_id=config_raw["profile_id"],
        catalog_version=config_raw["tradingdatas_catalog_version"],
        requirements=tuple(requirements),
    )
    config = PaperRuntimeConfig(
        trade_date=config_raw["trade_date"],
        decision_as_of=decision_as_of,
        tradingdatas_v1_base_url=config_raw["tradingdatas_v1_base_url"],
        tradingdatas_catalog_version=config_raw["tradingdatas_catalog_version"],
        tradingdatas_access_policy_id=config_raw[
            "tradingdatas_access_policy_id"
        ],
        dataset_profile=profile,
        dataset_requests=requests,
        evidence_policies=evidence_policies,
        capital_authority_id=config_raw["capital_authority_id"],
        authority_generation=config_raw["authority_generation"],
        execution_lineage=config_raw["execution_lineage"],
        champion_manifest_sha256=config_raw["champion_manifest_sha256"],
        real_trading_enabled=config_raw["real_trading_enabled"],
        live_execution_enabled=config_raw["live_execution_enabled"],
        network_enabled=config_raw["network_enabled"],
    )

    optimizer_raw = _object(
        fixture["small_account_optimizer"],
        field_name="small_account_optimizer",
        exact_fields=_SMALL_ACCOUNT_OPTIMIZER_FIELDS,
    )
    optimizer_decision_time = _instant(
        optimizer_raw["decision_time"],
        field_name="small_account_optimizer.decision_time",
    )
    if optimizer_decision_time != decision_as_of:
        raise FixtureCLIError("small_account_decision_time_config_mismatch")
    account_raw = _object(
        optimizer_raw["account_snapshot"],
        field_name="small_account_optimizer.account_snapshot",
        exact_fields=_ACCOUNT_SNAPSHOT_FIELDS,
    )
    positions: list[AccountPositionSnapshot] = []
    for index, item in enumerate(
        _sequence(
            account_raw["positions"],
            field_name="small_account_optimizer.account_snapshot.positions",
        )
    ):
        position = _object(
            item,
            field_name=(f"small_account_optimizer.account_snapshot.positions[{index}]"),
            exact_fields=_POSITION_FIELDS,
        )
        positions.append(
            AccountPositionSnapshot(
                symbol=position["symbol"],
                total_shares=position["total_shares"],
                sellable_shares=position["sellable_shares"],
                mark_price_cny=position["mark_price_cny"],
                price_observed_at=_instant(
                    position["price_observed_at"],
                    field_name=f"position[{index}].price_observed_at",
                ),
            )
        )
    account_snapshot = AccountAuthoritySnapshot(
        capital_authority_id=account_raw["capital_authority_id"],
        authority_generation=account_raw["authority_generation"],
        account_as_of=_instant(
            account_raw["account_as_of"],
            field_name="small_account_optimizer.account_snapshot.account_as_of",
        ),
        available_cash_cny=account_raw["available_cash_cny"],
        current_gross_cny=account_raw["current_gross_cny"],
        positions=tuple(positions),
        position_snapshot_receipt_id=account_raw["position_snapshot_receipt_id"],
        position_snapshot_sha256=account_raw["position_snapshot_sha256"],
        verification_receipt_sha256=account_raw["verification_receipt_sha256"],
        authority_source_class=account_raw["authority_source_class"],
    )
    if (
        account_snapshot.capital_authority_id != config.capital_authority_id
        or account_snapshot.authority_generation != config.authority_generation
    ):
        raise FixtureCLIError("small_account_authority_config_mismatch")
    account_authority_verifier = FrozenFixtureAccountAuthorityVerifier(
        expected_snapshot=account_snapshot,
        decision_time=optimizer_decision_time,
    )

    candidates: list[CandidateAllocationInput] = []
    for index, item in enumerate(
        _sequence(
            optimizer_raw["candidates"],
            field_name="small_account_optimizer.candidates",
        )
    ):
        candidate = _object(
            item,
            field_name=f"small_account_optimizer.candidates[{index}]",
            exact_fields=_CANDIDATE_FIELDS,
        )
        if candidate["score_evidence_class"] != "offline_engineering_fixture_rank":
            raise FixtureCLIError("candidate_score_evidence_class_invalid")
        candidates.append(
            CandidateAllocationInput(
                symbol=candidate["symbol"],
                score_evidence=fixture_rank_evidence(
                    champion_selection_manifest_sha256=(
                        config.champion_manifest_sha256
                    ),
                    symbol=candidate["symbol"],
                    decision_time=optimizer_decision_time,
                    fixture_id=f"phase1-paper-candidate-{index}",
                    source_fixture_sha256=candidate["source_fixture_sha256"],
                    rank_score=candidate["fixture_rank_score"],
                ),
                decision_time=optimizer_decision_time,
                price_observed_at=_instant(
                    candidate["price_observed_at"],
                    field_name=f"candidate[{index}].price_observed_at",
                ),
                decision_reference_price=candidate["decision_reference_price"],
            )
        )

    reduction_intents: list[PositionReductionIntent] = []
    for index, item in enumerate(
        _sequence(
            optimizer_raw["reduction_intents"],
            field_name="small_account_optimizer.reduction_intents",
        )
    ):
        intent = _object(
            item,
            field_name=f"small_account_optimizer.reduction_intents[{index}]",
            exact_fields=_REDUCTION_INTENT_FIELDS,
        )
        reduction_intents.append(
            PositionReductionIntent(
                intent_id=intent["intent_id"],
                symbol=intent["symbol"],
                action=intent["action"],
                target_shares=intent["target_shares"],
                decision_time=optimizer_decision_time,
            )
        )
    small_account_decision_port = SmallAccountDecisionStagePort(
        identity=_identity(
            optimizer_raw["identity"],
            stage=RunStage.DECISION_READY,
            field_name="small_account_optimizer.identity",
        ),
        account_snapshot=account_snapshot,
        candidates=tuple(candidates),
        reduction_intents=tuple(reduction_intents),
        decision_time=optimizer_decision_time,
        account_authority_verifier=account_authority_verifier,
        thesis_risk_authority=_parse_thesis_risk_authority(
            optimizer_raw["thesis_risk_authority"],
            decision_time=optimizer_decision_time,
        ),
        runtime_environment=_text(
            optimizer_raw["runtime_environment"],
            field_name="small_account_optimizer.runtime_environment",
        ),
        promotion_eligible=_native_false(
            optimizer_raw["promotion_eligible"],
            field_name="small_account_optimizer.promotion_eligible",
        ),
    )

    response_raw = _object(
        fixture["transport_responses"],
        field_name="transport_responses",
        exact_fields=frozenset({"catalog", "queries"}),
    )
    queries_raw = _object(
        response_raw["queries"],
        field_name="transport_responses.queries",
    )
    if set(queries_raw) != set(requests):
        raise FixtureCLIError("fixture_query_response_set_incomplete")
    transport = FrozenFixtureHTTPTransport(
        (
            _response(
                response_raw["catalog"],
                field_name="transport_responses.catalog",
            ),
            *(
                _response(
                    queries_raw[requirement.dataset_id],
                    field_name=(
                        f"transport_responses.queries.{requirement.dataset_id}"
                    ),
                )
                for requirement in requirements
            ),
        )
    )

    payloads_raw = _object(
        fixture["business_stage_payloads"],
        field_name="business_stage_payloads",
    )
    if set(payloads_raw) != {stage.value for stage in _BUSINESS_STAGES}:
        raise FixtureCLIError("business_stage_payload_set_incomplete")
    business_ports = {
        stage: FrozenFixtureStagePort(
            stage,
            _object(
                payloads_raw[stage.value],
                field_name=f"business_stage_payloads.{stage.value}",
            ),
        )
        for stage in _BUSINESS_STAGES
    }

    managed_raw = _object(
        fixture["managed_stage_identities"],
        field_name="managed_stage_identities",
    )
    if set(managed_raw) != {stage.value for stage in _MANAGED_STAGES}:
        raise FixtureCLIError("managed_stage_identity_set_incomplete")
    managed_identities = {
        stage: _identity(
            managed_raw[stage.value],
            stage=stage,
            field_name=f"managed_stage_identities.{stage.value}",
        )
        for stage in _MANAGED_STAGES
    }
    scope_policy = CanonicalMainboardScopePolicy()
    supplied_scope_identity = _identity(
        fixture["scope_policy_identity"],
        stage=None,
        field_name="scope_policy_identity",
    )
    if supplied_scope_identity != scope_policy.identity:
        raise FixtureCLIError("scope_policy_identity_not_canonical")
    return _ParsedFixture(
        config=config,
        transport=transport,
        requests=requests,
        business_ports=business_ports,
        small_account_decision_port=small_account_decision_port,
        managed_identities=managed_identities,
        scope_policy=scope_policy,
    )


def _ensure_simulation_boundary(argument_value: str) -> None:
    if argument_value != "false":
        raise FixtureCLIError("real_trading_enabled_argument_must_be_false")
    environment_value = os.environ.get("REAL_TRADING_ENABLED")
    if environment_value is not None and environment_value != "false":
        raise FixtureCLIError("REAL_TRADING_ENABLED_environment_must_be_false")


def _transport_call_summary(parsed: _ParsedFixture) -> list[dict[str, str | None]]:
    calls = parsed.transport.calls
    if not calls:
        return []
    expected: list[tuple[str, str, Mapping[str, Any] | None]] = [
        (
            "GET",
            f"{parsed.config.tradingdatas_v1_base_url}{CATALOG_PATH}",
            None,
        )
    ]
    expected.extend(
        (
            "POST",
            f"{parsed.config.tradingdatas_v1_base_url}{QUERY_PATH}",
            parsed.requests[dataset_id].to_payload(),
        )
        for dataset_id in parsed.config.dataset_profile.dataset_ids
    )
    if len(calls) != len(expected):
        raise FixtureCLIError("fixture_transport_call_sequence_invalid")
    summary: list[dict[str, str | None]] = []
    for call, (method, url, json_body) in zip(calls, expected):
        if (
            call.get("method") != method
            or call.get("url") != url
            or call.get("json_body") != json_body
            or call.get("headers", {}).get("Accept") != "application/json"
            or "X-Access-Policy" in call.get("headers", {})
        ):
            raise FixtureCLIError("fixture_transport_call_sequence_invalid")
        if method == "GET":
            if "Content-Type" in call.get("headers", {}):
                raise FixtureCLIError("fixture_transport_call_sequence_invalid")
            dataset_id = None
            path = CATALOG_PATH
        else:
            if call.get("headers", {}).get("Content-Type") != "application/json":
                raise FixtureCLIError("fixture_transport_call_sequence_invalid")
            dataset_id = json_body["dataset_id"] if json_body is not None else None
            path = QUERY_PATH
        summary.append({"dataset_id": dataset_id, "method": method, "path": path})
    return summary


def run_fixture(*, fixture_path: Path, output_root: Path) -> dict[str, Any]:
    output_root = _validated_fixture_output_root(output_root)
    parsed = _parse_fixture(_load_fixture(fixture_path))
    runtime_root = output_root / _FIXTURE_RUNTIME_RELATIVE_ROOT
    runtime = compose_paper_runtime(
        config=parsed.config,
        transport_fixture=parsed.transport,
        research_snapshot_store=FileResearchSnapshotStore(
            runtime_root / "research_snapshots"
        ),
        run_bundle_store=FileRunBundleStore(runtime_root / "run_bundle_events"),
        sample_journal=_OfflineFixtureSampleJournal(
            runtime_root / "ashare" / "sample_journal.jsonl"
        ),
        business_stage_ports=parsed.business_ports,
        small_account_decision_port=parsed.small_account_decision_port,
        drift_risk_adapter=DriftRuntimeRiskAdapter(
            DriftActionStore(runtime_root / "drift_actions")
        ),
        managed_stage_identities=parsed.managed_identities,
        scope_policy=parsed.scope_policy,
        local_publisher=LocalRunBundlePublisher(runtime_root / "run_bundles"),
    )
    with (
        _offline_fixture_authority_scope(
            capital_authority_id=parsed.config.capital_authority_id,
            authority_generation=parsed.config.authority_generation,
            execution_lineage=parsed.config.execution_lineage,
        ),
        _offline_fixture_journal_factory(),
    ):
        result = runtime.run()
    return {
        "artifact_path": str(result.publication.latest_path),
        "authority": "non_authority",
        "bundle_sha256": result.bundle.bundle_sha256,
        "environment": "local_candidate",
        "idempotent": result.publication.idempotent,
        "immutable_artifact_path": str(result.publication.immutable_path),
        "mode": "offline_fixture",
        "production_verified": False,
        "real_trading_enabled": False,
        "run_id": result.bundle.run_id,
        "status": result.bundle.status,
        "transport_calls": _transport_call_summary(parsed),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the offline-only Phase 1 A-share paper fixture.",
    )
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--real-trading-enabled",
        default="false",
        help="Simulation boundary; the only accepted value is 'false'.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _ensure_simulation_boundary(args.real_trading_enabled)
        summary = run_fixture(
            fixture_path=args.fixture,
            output_root=args.output_root,
        )
    except (
        FixtureCLIError,
        DriftActionStoreError,
        JournalError,
        OSError,
        PaperRuntimeCompositionError,
        ResearchSnapshotStoreConflict,
        ResearchSnapshotStoreCorruption,
        RunBundlePublishError,
        RunBundleStoreCorruption,
        SmallAccountStageContractError,
        StagePortContractError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "authority": "non_authority",
                    "environment": "local_candidate",
                    "error": str(exc),
                    "mode": "offline_fixture",
                    "production_verified": False,
                    "real_trading_enabled": False,
                    "status": "failed",
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            summary,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
