"""Strict adapters from frozen research and learning authorities into a day run.

These ports have no implicit endpoint, storage root, clock, broker or fallback.
They are suitable for isolated paper replays and for the frozen TradingDatas V1
consumer contract. Producer ownership remains external to TradingAgent.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from Ashare.sample_pipeline import persist_simulation_outcomes
from shared.data.evidence_gate import DataEvidenceGate
from shared.data.research_snapshot import (
    ResearchDataProfile,
    ResearchDataSnapshot,
    build_research_data_snapshot,
)
from shared.data.research_snapshot_store import (
    FileResearchSnapshotStore,
    ResearchSnapshotStoreConflict,
    ResearchSnapshotStoreCorruption,
)
from shared.data.sharedsignals_v1 import QueryRequest, SharedSignalsV1Client
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)
from shared.review.decision_ledger import (
    DecisionExposureRecord,
    ExposureDisposition,
    SampleJournalDecisionLedger,
)
from shared.review.sample_journal import SampleJournal

from .day_loop import StageRequest, StageResult
from .publisher import LocalRunBundlePublisher, RunBundlePublishError
from .run_bundle import ComponentIdentity, RunStage


class StagePortContractError(RuntimeError):
    """Raised when a stage adapter cannot preserve its frozen contract."""


_CANDIDATE_SET_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_set_id",
        "generated_at",
        "selection_policy_version",
        "exploration_seed",
        "candidates",
    }
)
_CANDIDATE_ENTRY_FIELDS = frozenset(
    {
        "candidate_id",
        "decision_id",
        "prediction_snapshot_id",
        "symbol",
        "selected",
        "selection_reason",
        "selection_propensity",
        "strategy_version",
    }
)


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StagePortContractError("stage_port_noncanonical_payload") from exc
    return hashlib.sha256(encoded).hexdigest()


def _aware_utc(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StagePortContractError(f"{field_name}_invalid") from exc
    else:
        raise StagePortContractError(f"{field_name}_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StagePortContractError(f"{field_name}_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


def _mapping_rows(value: object, *, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise StagePortContractError(f"{field_name}_must_be_list")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise StagePortContractError(f"{field_name}_item_must_be_mapping")
        rows.append(deepcopy(dict(item)))
    return rows


class SharedSignalsResearchEvidencePort:
    """Build one immutable current-observation snapshot from V1 responses.

    Provider-native rows are traversed to a bounded terminal page and bound to
    their envelope receipt, lineage and observation metadata. This adapter does
    not promote current observations to historical point-in-time evidence.
    """

    def __init__(
        self,
        *,
        identity: ComponentIdentity,
        client: SharedSignalsV1Client,
        profile: ResearchDataProfile,
        requests: Mapping[str, QueryRequest],
        evidence_gate: DataEvidenceGate,
        decision_as_of: datetime,
        snapshot_store: FileResearchSnapshotStore,
    ) -> None:
        if (
            not isinstance(identity, ComponentIdentity)
            or identity.stage is not RunStage.EVIDENCE_READY
        ):
            raise ValueError("identity must belong to evidence_ready")
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        if not isinstance(profile, ResearchDataProfile):
            raise TypeError("profile must be ResearchDataProfile")
        if not isinstance(evidence_gate, DataEvidenceGate):
            raise TypeError("evidence_gate must be DataEvidenceGate")
        if not isinstance(snapshot_store, FileResearchSnapshotStore):
            raise TypeError("snapshot_store must be FileResearchSnapshotStore")
        decision_instant = _aware_utc(decision_as_of, field_name="decision_as_of")

        expected = set(profile.dataset_ids)
        if set(requests) != expected:
            raise ValueError("request_dataset_set_mismatch")
        if client.config.expected_catalog_version != profile.catalog_version:
            raise ValueError("client_profile_catalog_version_mismatch")
        if client.config.dataset_ids != frozenset(expected):
            raise ValueError("client_profile_dataset_set_mismatch")

        ordered: dict[str, QueryRequest] = {}
        for requirement in profile.requirements:
            query = requests[requirement.dataset_id]
            if not isinstance(query, QueryRequest):
                raise TypeError("requests must contain QueryRequest values")
            if query.dataset_id != requirement.dataset_id:
                raise ValueError("request_dataset_identity_mismatch")
            if query.cursor is not None:
                raise ValueError("initial_request_cursor_forbidden")
            if requirement.query_as_of_mode == "decision_as_of":
                if (
                    query.as_of is None
                    or _aware_utc(query.as_of, field_name="request_as_of")
                    != decision_instant
                ):
                    raise ValueError("request_decision_as_of_mismatch")
            elif query.as_of is not None:
                raise ValueError("request_as_of_must_be_omitted")
            ordered[requirement.dataset_id] = query

        self.identity = identity
        self._client = client
        self._profile = profile
        self._requests = ordered
        self._evidence_gate = evidence_gate
        self._decision_as_of = decision_instant
        self._snapshot_store = snapshot_store
        self._results: dict[str, StageResult] = {}
        self._snapshots: dict[str, ResearchDataSnapshot] = {}

    def execute(self, request: StageRequest) -> StageResult:
        if not isinstance(request, StageRequest) or request.stage is not (
            RunStage.EVIDENCE_READY
        ):
            raise StagePortContractError("evidence_stage_request_invalid")
        cached = self._results.get(request.idempotency_key)
        if cached is not None:
            return cached

        try:
            recovered = self._snapshot_store.load_bound_decision(
                profile_id=self._profile.profile_id,
                decision_as_of=self._decision_as_of,
                catalog_version=self._profile.catalog_version,
            )
        except ResearchSnapshotStoreCorruption as exc:
            raise StagePortContractError("research_snapshot_store_invalid") from exc
        if recovered is not None:
            result = StageResult(payload=recovered.to_evidence_payload())
            self._snapshots[request.idempotency_key] = recovered
            self._results[request.idempotency_key] = result
            return result

        catalog = self._client.get_catalog()
        if catalog.catalog_version != self._profile.catalog_version:
            raise StagePortContractError("catalog_version_mismatch")

        page_runs = []
        decisions = []
        for requirement in self._profile.requirements:
            try:
                page_run = collect_query_pages(
                    client=self._client,
                    request=self._requests[requirement.dataset_id],
                    identity_fields=requirement.identity_fields,
                    max_pages=requirement.max_pages,
                    max_rows=requirement.max_rows,
                )
            except PaginationContractError as exc:
                raise StagePortContractError(str(exc)) from exc
            page_runs.append(page_run)
            decisions.append(self._evidence_gate.evaluate(page_run.envelope))

        snapshot = build_research_data_snapshot(
            profile=self._profile,
            page_runs=tuple(page_runs),
            decisions=tuple(decisions),
            decision_as_of=self._decision_as_of,
        )
        try:
            self._snapshot_store.compare_and_swap(
                snapshot=snapshot,
                expected_snapshot_sha256=None,
            )
            recovered = self._snapshot_store.load(
                profile_id=snapshot.profile_id,
                decision_as_of=snapshot.decision_as_of,
                expected_snapshot_sha256=snapshot.snapshot_sha256,
                catalog_version=snapshot.catalog_version,
                receipt_ids={
                    item.dataset_id: item.receipt_id for item in snapshot.datasets
                },
            )
        except (ResearchSnapshotStoreConflict, ResearchSnapshotStoreCorruption) as exc:
            raise StagePortContractError(
                "research_snapshot_store_commit_failed"
            ) from exc
        if recovered != snapshot:
            raise StagePortContractError("research_snapshot_store_readback_mismatch")
        result = StageResult(payload=recovered.to_evidence_payload())
        self._snapshots[request.idempotency_key] = recovered
        self._results[request.idempotency_key] = result
        return result

    def snapshot_for(self, idempotency_key: str) -> ResearchDataSnapshot:
        """Return the same-process immutable snapshot for research consumers."""

        try:
            return self._snapshots[idempotency_key]
        except KeyError as exc:
            try:
                recovered = self._snapshot_store.load_bound_decision(
                    profile_id=self._profile.profile_id,
                    decision_as_of=self._decision_as_of,
                    catalog_version=self._profile.catalog_version,
                )
            except ResearchSnapshotStoreCorruption as store_exc:
                raise StagePortContractError(
                    "research_snapshot_store_invalid"
                ) from store_exc
            if recovered is None:
                raise StagePortContractError("research_snapshot_not_loaded") from exc
            self._snapshots[idempotency_key] = recovered
            return recovered


class LocalTodayReportPort:
    """Publish the pre-report RunBundle as a read-only local candidate artifact.

    The completed bundle is published by the composition root after the report
    receipt has been durably appended.  This port proves that the report stage
    itself wrote and read back the exact input bundle; it never claims a
    production publication or creates another business authority.
    """

    def __init__(
        self,
        *,
        identity: ComponentIdentity,
        publisher: LocalRunBundlePublisher,
    ) -> None:
        if (
            not isinstance(identity, ComponentIdentity)
            or identity.stage is not RunStage.REPORTED
        ):
            raise ValueError("identity must belong to reported")
        if not isinstance(publisher, LocalRunBundlePublisher):
            raise TypeError("publisher must be LocalRunBundlePublisher")
        self.identity = identity
        self._publisher = publisher
        self._results: dict[str, StageResult] = {}

    def execute(self, request: StageRequest) -> StageResult:
        if not isinstance(request, StageRequest) or request.stage is not (
            RunStage.REPORTED
        ):
            raise StagePortContractError("report_stage_request_invalid")
        cached = self._results.get(request.idempotency_key)
        if cached is not None:
            return cached
        try:
            published = self._publisher.publish(request.bundle)
            artifact = published.immutable_path.read_bytes()
            readback = published.latest_path.read_bytes()
        except (OSError, RunBundlePublishError) as exc:
            raise StagePortContractError("local_report_publish_failed") from exc
        if artifact != readback:
            raise StagePortContractError("local_report_readback_mismatch")
        artifact_sha256 = hashlib.sha256(artifact).hexdigest()
        artifact_path = published.immutable_path.relative_to(self._publisher.root)
        latest_path = published.latest_path.relative_to(self._publisher.root)
        result = StageResult(
            payload={
                "reported": True,
                "report_id": (
                    f"today-run:{request.run_id}:{request.input_bundle_sha256[:16]}"
                ),
                "source_run_id": request.run_id,
                "source_input_bundle_sha256": request.input_bundle_sha256,
                "projection_type": "today_run_local_candidate",
                "local_candidate": True,
                "production_verified": False,
                "artifact_sha256": artifact_sha256,
                "readback_sha256": hashlib.sha256(readback).hexdigest(),
                "artifact_path": artifact_path.as_posix(),
                "latest_path": latest_path.as_posix(),
                "artifact_bundle_sha256": request.bundle.bundle_sha256,
                "idempotent": published.idempotent,
            }
        )
        self._results[request.idempotency_key] = result
        return result


class SampleJournalLearningPort:
    """Commit prediction, execution and chain evidence to SampleJournal only.

    Decision and execution stages must supply complete journal-ready facts.
    This adapter never manufactures prices, quantities, costs, timestamps or
    outcomes. It only binds the frozen run authority and appends through the
    existing canonical SampleJournal validation path.
    """

    def __init__(
        self,
        *,
        identity: ComponentIdentity,
        journal: SampleJournal,
    ) -> None:
        if (
            not isinstance(identity, ComponentIdentity)
            or identity.stage is not RunStage.LEARNING_RECORDED
        ):
            raise ValueError("identity must belong to learning_recorded")
        if not isinstance(journal, SampleJournal):
            raise TypeError("journal must be SampleJournal")
        self.identity = identity
        self._journal = journal
        self._results: dict[str, StageResult] = {}

    @staticmethod
    def _bind_run_authority(
        value: Mapping[str, Any],
        *,
        request: StageRequest,
    ) -> dict[str, Any]:
        row = deepcopy(dict(value))
        context = request.bundle.context
        expected = {
            "market": context.market,
            "capital_authority_id": context.authority_id,
            "authority_generation": context.authority_generation,
            "execution_lineage_id": context.execution_lineage,
            "source_run_id": request.run_id,
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }
        for field_name, expected_value in expected.items():
            if field_name in row and row[field_name] != expected_value:
                raise StagePortContractError(f"journal_record_{field_name}_mismatch")
            row[field_name] = expected_value
        return row

    @staticmethod
    def _validate_prediction_bindings(
        *,
        predictions: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> None:
        decision_symbols: dict[str, str] = {}
        for decision in decisions:
            decision_id = decision.get("decision_id")
            symbol = decision.get("symbol")
            if not isinstance(decision_id, str) or not decision_id:
                raise StagePortContractError("journal_decision_identity_invalid")
            if decision_id in decision_symbols:
                raise StagePortContractError("journal_decision_identity_duplicate")
            if not isinstance(symbol, str) or not symbol:
                raise StagePortContractError("journal_decision_symbol_invalid")
            decision_symbols[decision_id] = symbol

        prediction_ids: set[str] = set()
        for prediction in predictions:
            decision_id = prediction.get("decision_id")
            if not isinstance(decision_id, str) or decision_id not in decision_symbols:
                raise StagePortContractError("journal_prediction_without_decision")
            if decision_id in prediction_ids:
                raise StagePortContractError("journal_prediction_decision_duplicate")
            if prediction.get("symbol") != decision_symbols[decision_id]:
                raise StagePortContractError("journal_prediction_symbol_mismatch")
            prediction_ids.add(decision_id)
        missing_prediction_ids = set(decision_symbols) - prediction_ids
        decision_by_id = {decision["decision_id"]: decision for decision in decisions}
        if any(
            decision_by_id[decision_id].get("action") != "hold"
            for decision_id in missing_prediction_ids
        ):
            raise StagePortContractError("journal_prediction_set_incomplete")

    @staticmethod
    def _candidate_set_event(
        *,
        request: StageRequest,
        raw_receipt: object,
        predictions: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(raw_receipt, Mapping):
            raise StagePortContractError("candidate_set_receipt_missing")
        receipt = deepcopy(dict(raw_receipt))
        if set(receipt) != _CANDIDATE_SET_FIELDS:
            raise StagePortContractError("candidate_set_receipt_fields_invalid")
        if receipt.get("schema_version") != 1:
            raise StagePortContractError("candidate_set_schema_invalid")
        for field_name in (
            "candidate_set_id",
            "selection_policy_version",
            "exploration_seed",
        ):
            value = receipt.get(field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise StagePortContractError(f"candidate_set_{field_name}_invalid")
        generated_at = _aware_utc(
            receipt.get("generated_at"),
            field_name="candidate_set_generated_at",
        )
        research_source = SampleJournalLearningPort._research_source_payload(
            request.bundle
        )
        if generated_at > _aware_utc(
            research_source["decision_as_of"],
            field_name="decision_as_of",
        ):
            raise StagePortContractError("candidate_set_generated_after_decision")

        candidates = _mapping_rows(
            receipt.get("candidates"),
            field_name="candidate_set_candidates",
        )
        decision_by_id = {
            decision.get("decision_id"): decision for decision in decisions
        }
        prediction_by_id = {
            prediction.get("snapshot_id"): prediction for prediction in predictions
        }
        candidate_ids: set[str] = set()
        decision_ids: set[str] = set()
        prediction_ids: set[str] = set()
        for candidate in candidates:
            if set(candidate) != _CANDIDATE_ENTRY_FIELDS:
                raise StagePortContractError("candidate_set_entry_fields_invalid")
            for field_name in (
                "candidate_id",
                "decision_id",
                "prediction_snapshot_id",
                "symbol",
                "selection_reason",
                "strategy_version",
            ):
                value = candidate.get(field_name)
                if not isinstance(value, str) or not value or value != value.strip():
                    raise StagePortContractError(f"candidate_set_{field_name}_invalid")
            candidate_id = candidate["candidate_id"]
            decision_id = candidate["decision_id"]
            prediction_id = candidate["prediction_snapshot_id"]
            if (
                candidate_id in candidate_ids
                or decision_id in decision_ids
                or prediction_id in prediction_ids
            ):
                raise StagePortContractError("candidate_set_identity_duplicate")
            candidate_ids.add(candidate_id)
            decision_ids.add(decision_id)
            prediction_ids.add(prediction_id)
            decision = decision_by_id.get(decision_id)
            prediction = prediction_by_id.get(prediction_id)
            if decision is None or prediction is None:
                raise StagePortContractError("candidate_set_binding_missing")
            if (
                decision.get("symbol") != candidate["symbol"]
                or prediction.get("symbol") != candidate["symbol"]
                or prediction.get("decision_id") != decision_id
                or prediction.get("strategy_version") != candidate["strategy_version"]
            ):
                raise StagePortContractError("candidate_set_binding_mismatch")
            selected = candidate.get("selected")
            if type(selected) is not bool:
                raise StagePortContractError("candidate_set_selected_invalid")
            if selected != (decision.get("action") != "hold"):
                raise StagePortContractError("candidate_set_selection_action_mismatch")
            propensity = candidate.get("selection_propensity")
            if (
                isinstance(propensity, bool)
                or not isinstance(propensity, (int, float))
                or not math.isfinite(float(propensity))
                or not 0.0 <= float(propensity) <= 1.0
                or (selected and float(propensity) <= 0.0)
            ):
                raise StagePortContractError("candidate_set_propensity_invalid")
        optimizer_only_decision_ids = set(decision_by_id) - decision_ids
        if prediction_ids != set(prediction_by_id) or any(
            decision_by_id[decision_id].get("action") != "hold"
            for decision_id in optimizer_only_decision_ids
        ):
            raise StagePortContractError("candidate_set_incomplete")

        return SampleJournalLearningPort._bind_run_authority(
            {
                "event_id": (
                    f"candidate-set:{request.run_id}:{request.input_bundle_sha256[:16]}"
                ),
                "record_type": "candidate_set_receipt",
                "classification": "candidate_set_receipt",
                "sample_layer": "observation_counterfactual",
                "receipt_at": generated_at.isoformat(),
                "candidate_set_id": receipt["candidate_set_id"],
                "candidate_set_sha256": _canonical_sha256(receipt),
                "candidate_count": len(candidates),
                "selection_policy_version": receipt["selection_policy_version"],
                "exploration_seed": receipt["exploration_seed"],
                "candidates": candidates,
                "eligible_for_statistical_learning": False,
                "promotion_eligible": False,
            },
            request=request,
        )

    @staticmethod
    def _research_source_payload(bundle: Any) -> dict[str, Any]:
        evidence = bundle.receipt_for(RunStage.EVIDENCE_READY).payload
        datasets = evidence.get("datasets")
        if not isinstance(datasets, list):
            raise StagePortContractError("journal_research_datasets_invalid")
        receipt_ids: dict[str, str] = {}
        for item in datasets:
            if not isinstance(item, Mapping):
                raise StagePortContractError("journal_research_dataset_invalid")
            dataset_id = item.get("dataset_id")
            receipt_id = item.get("receipt_id")
            if (
                not isinstance(dataset_id, str)
                or not dataset_id
                or dataset_id in receipt_ids
                or not isinstance(receipt_id, str)
                or not receipt_id
            ):
                raise StagePortContractError("journal_research_receipt_invalid")
            receipt_ids[dataset_id] = receipt_id
        expected = {
            "profile_id": evidence.get("profile_id"),
            "catalog_version": evidence.get("catalog_version"),
            "decision_as_of": evidence.get("decision_as_of"),
            "research_snapshot_sha256": evidence.get("snapshot_sha256"),
            "dataset_receipt_ids": receipt_ids,
        }
        if any(value in (None, "") for value in expected.values()):
            raise StagePortContractError("journal_research_source_invalid")
        return expected

    @classmethod
    def _validate_prediction_sources(
        cls,
        *,
        bundle: Any,
        predictions: list[dict[str, Any]],
    ) -> None:
        expected_payload = cls._research_source_payload(bundle)
        expected_sha256 = _canonical_sha256(expected_payload)
        for prediction in predictions:
            if prediction.get("source_snapshot_payload") != expected_payload:
                raise StagePortContractError(
                    "journal_prediction_research_source_mismatch"
                )
            supplied_sha256 = prediction.get("source_snapshot_sha256")
            if (
                not isinstance(supplied_sha256, str)
                or supplied_sha256 != expected_sha256
            ):
                raise StagePortContractError(
                    "journal_prediction_research_source_sha_mismatch"
                )

    @staticmethod
    def _canonical_outcome_records(
        *,
        request: StageRequest,
        predictions: list[dict[str, Any]],
        execution_receipts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        risk_payload = request.bundle.receipt_for(RunStage.RISK_CHECKED).payload
        approved_orders = _mapping_rows(
            risk_payload.get("approved_orders"),
            field_name="approved_orders",
        )
        order_map: dict[str, dict[str, Any]] = {}
        for order in approved_orders:
            order_id = order.get("order_id")
            if not isinstance(order_id, str) or not order_id or order_id in order_map:
                raise StagePortContractError("canonical_outcome_order_identity_invalid")
            order_map[order_id] = order
        prediction_map = {
            prediction.get("snapshot_id"): prediction for prediction in predictions
        }
        records: list[dict[str, Any]] = []
        for receipt in execution_receipts:
            if receipt.get("status") not in {"filled", "partial"}:
                continue
            order_id = receipt.get("order_id")
            order = order_map.get(order_id)
            if order is None:
                raise StagePortContractError("canonical_outcome_without_approved_order")
            prediction_id = order.get("prediction_snapshot_id")
            prediction = prediction_map.get(prediction_id)
            if prediction is None:
                raise StagePortContractError("canonical_outcome_prediction_missing")
            intent = order.get("intent")
            expected_side_by_intent = {
                "open": "buy",
                "increase": "buy",
                "reduce": "sell",
                "exit": "sell",
            }
            expected_side = expected_side_by_intent.get(intent)
            if expected_side is None:
                raise StagePortContractError("canonical_outcome_intent_invalid")
            if order.get("side") != expected_side:
                raise StagePortContractError("canonical_outcome_side_invalid")
            if order.get("prediction_source_snapshot_sha256") != prediction.get(
                "source_snapshot_sha256"
            ):
                raise StagePortContractError(
                    "canonical_outcome_prediction_source_mismatch"
                )
            sample_intent = order.get("sample_intent")
            if sample_intent not in {"exploration", "exploitation"}:
                raise StagePortContractError("canonical_outcome_sample_intent_invalid")
            required_receipt_fields = (
                "filled_at",
                "available_at",
                "ingested_at",
                "retrieved_as_of",
            )
            if not all(
                isinstance(receipt.get(field_name), str)
                and bool(receipt.get(field_name))
                for field_name in required_receipt_fields
            ):
                raise StagePortContractError("canonical_outcome_evidence_time_missing")
            if receipt.get("execution_eligible") is not True:
                raise StagePortContractError("canonical_outcome_not_execution_eligible")
            context = request.bundle.context
            pipeline_order = deepcopy(order)
            pipeline_order.update(
                {
                    "ts_code": order.get("symbol"),
                    "execution_lineage_id": context.execution_lineage,
                    "prediction_snapshot_id": prediction_id,
                    "prediction_source_snapshot_sha256": prediction.get(
                        "source_snapshot_sha256"
                    ),
                }
            )
            pipeline_receipt = deepcopy(receipt)
            pipeline_receipt.update(
                {
                    "execution_eligible": True,
                    "filled_price": receipt.get("filled_price_cny"),
                    "fee_cny": receipt.get("fee_cny"),
                    "slippage_cny": receipt.get("slippage_cny"),
                    "execution_lineage_id": context.execution_lineage,
                    "prediction_snapshot_id": prediction_id,
                    "prediction_source_snapshot_sha256": prediction.get(
                        "source_snapshot_sha256"
                    ),
                }
            )
            records.append(
                {
                    "symbol": order.get("symbol"),
                    "account": "ashare_sim",
                    "source_run_id": request.run_id,
                    "source_input_bundle_sha256": request.input_bundle_sha256,
                    "capital_authority_id": context.authority_id,
                    "authority_generation": context.authority_generation,
                    "execution_lineage_id": context.execution_lineage,
                    "prediction_snapshot_id": prediction_id,
                    "prediction_source_snapshot_sha256": prediction.get(
                        "source_snapshot_sha256"
                    ),
                    "order": pipeline_order,
                    "receipt": pipeline_receipt,
                }
            )
        return records

    @staticmethod
    def _decision_exposure_records(
        *,
        request: StageRequest,
        decisions: list[dict[str, Any]],
        predictions: list[dict[str, Any]],
        execution_receipts: list[dict[str, Any]],
    ) -> list[DecisionExposureRecord]:
        decision_payload = request.bundle.receipt_for(RunStage.DECISION_READY).payload
        manifest_sha256 = decision_payload.get("champion_manifest_sha256")
        risk_payload = request.bundle.receipt_for(RunStage.RISK_CHECKED).payload
        approved_orders = _mapping_rows(
            risk_payload.get("approved_orders"),
            field_name="approved_orders",
        )
        rejected_decisions = _mapping_rows(
            risk_payload.get("rejected_decisions"),
            field_name="rejected_decisions",
        )

        prediction_by_decision: dict[str, dict[str, Any]] = {}
        for prediction in predictions:
            decision_id = prediction.get("decision_id")
            if (
                not isinstance(decision_id, str)
                or decision_id in prediction_by_decision
            ):
                raise StagePortContractError(
                    "decision_exposure_prediction_identity_invalid"
                )
            prediction_by_decision[decision_id] = prediction

        order_by_decision: dict[str, dict[str, Any]] = {}
        order_ids: set[str] = set()
        for order in approved_orders:
            decision_id = order.get("decision_id")
            order_id = order.get("order_id")
            if (
                not isinstance(decision_id, str)
                or not decision_id
                or decision_id in order_by_decision
                or not isinstance(order_id, str)
                or not order_id
                or order_id in order_ids
            ):
                raise StagePortContractError(
                    "decision_exposure_approved_order_identity_invalid"
                )
            order_by_decision[decision_id] = order
            order_ids.add(order_id)

        receipt_by_order: dict[str, dict[str, Any]] = {}
        for receipt in execution_receipts:
            order_id = receipt.get("order_id")
            if (
                not isinstance(order_id, str)
                or not order_id
                or order_id in receipt_by_order
            ):
                raise StagePortContractError(
                    "decision_exposure_receipt_identity_invalid"
                )
            receipt_by_order[order_id] = receipt
        if set(receipt_by_order) != order_ids:
            raise StagePortContractError("decision_exposure_receipt_set_incomplete")

        rejection_by_decision: dict[str, str] = {}
        for rejection in rejected_decisions:
            decision_id = rejection.get("decision_id")
            reason = rejection.get("reason")
            if (
                not isinstance(decision_id, str)
                or not decision_id
                or decision_id in rejection_by_decision
                or not isinstance(reason, str)
                or not reason
            ):
                raise StagePortContractError(
                    "decision_exposure_rejection_identity_invalid"
                )
            rejection_by_decision[decision_id] = reason
        if set(order_by_decision) & set(rejection_by_decision):
            raise StagePortContractError("decision_exposure_disposition_conflict")

        records: list[DecisionExposureRecord] = []
        decision_ids: set[str] = set()
        for decision in decisions:
            decision_id = decision.get("decision_id")
            if (
                not isinstance(decision_id, str)
                or not decision_id
                or decision_id in decision_ids
            ):
                raise StagePortContractError(
                    "decision_exposure_decision_identity_invalid"
                )
            prediction = prediction_by_decision.get(decision_id)
            if prediction is None:
                if (
                    decision.get("action") == "hold"
                    and decision_id not in order_by_decision
                    and decision_id not in rejection_by_decision
                ):
                    continue
                raise StagePortContractError("decision_exposure_prediction_missing")
            decision_ids.add(decision_id)
            cluster_id = decision.get("decision_cluster_id")
            if (
                not isinstance(cluster_id, str)
                or not cluster_id
                or prediction.get("decision_cluster_id") != cluster_id
            ):
                raise StagePortContractError(
                    "decision_exposure_cluster_binding_invalid"
                )

            action = decision.get("action")
            disposition: ExposureDisposition
            filled_quantity = 0
            filled_notional_cny = 0.0
            actual_cost_cny = 0.0
            simulated_fill_id = None
            rejection_reason = None
            nonfill_reason = None
            order = order_by_decision.get(decision_id)
            rejection_reason = rejection_by_decision.get(decision_id)
            if action == "hold":
                if order is not None or rejection_reason is not None:
                    raise StagePortContractError(
                        "decision_exposure_hold_disposition_invalid"
                    )
                disposition = ExposureDisposition.OBSERVATION_ONLY
            elif rejection_reason is not None:
                disposition = ExposureDisposition.REJECTED
            elif order is not None:
                receipt = receipt_by_order[order["order_id"]]
                status = receipt.get("status")
                if status in {"filled", "partial"}:
                    filled_quantity = receipt.get("filled_quantity")
                    filled_price = receipt.get("filled_price_cny")
                    fee_cny = receipt.get("fee_cny")
                    slippage_cny = receipt.get("slippage_cny")
                    try:
                        filled_notional_cny = float(filled_quantity) * float(
                            filled_price
                        )
                        actual_cost_cny = float(fee_cny) + float(slippage_cny)
                    except (TypeError, ValueError) as exc:
                        raise StagePortContractError(
                            "decision_exposure_fill_economics_invalid"
                        ) from exc
                    simulated_fill_id = receipt.get("execution_receipt_id")
                    disposition = ExposureDisposition.PAPER_FILLED
                elif status in {"rejected", "cancelled", "not_filled"}:
                    nonfill_reason = receipt.get("nonfill_reason")
                    if not isinstance(nonfill_reason, str) or not nonfill_reason:
                        raise StagePortContractError(
                            "decision_exposure_nonfill_reason_missing"
                        )
                    disposition = ExposureDisposition.PAPER_NOT_FILLED
                else:
                    raise StagePortContractError(
                        "decision_exposure_execution_status_invalid"
                    )
            else:
                raise StagePortContractError("decision_exposure_disposition_missing")

            try:
                record = DecisionExposureRecord(
                    decision_id=decision_id,
                    decision_cluster_id=cluster_id,
                    decision_time=_aware_utc(
                        prediction.get("prediction_at"),
                        field_name="prediction_at",
                    ),
                    symbol=decision.get("symbol"),
                    model_id=prediction.get("style"),
                    model_version=prediction.get("strategy_version"),
                    manifest_sha256=manifest_sha256,
                    action=action,
                    disposition=disposition,
                    requested_notional_cny=decision.get("requested_notional_cny"),
                    filled_quantity=filled_quantity,
                    filled_notional_cny=filled_notional_cny,
                    actual_cost_cny=actual_cost_cny,
                    simulated_fill_id=simulated_fill_id,
                    rejection_reason=rejection_reason,
                    nonfill_reason=nonfill_reason,
                )
            except (TypeError, ValueError) as exc:
                raise StagePortContractError(
                    "decision_exposure_record_invalid"
                ) from exc
            records.append(record)

        if (
            set(prediction_by_decision) != decision_ids
            or not set(order_by_decision).issubset(decision_ids)
            or not set(rejection_by_decision).issubset(decision_ids)
        ):
            raise StagePortContractError("decision_exposure_set_mismatch")
        return records

    def execute(self, request: StageRequest) -> StageResult:
        if not isinstance(request, StageRequest) or request.stage is not (
            RunStage.LEARNING_RECORDED
        ):
            raise StagePortContractError("learning_stage_request_invalid")
        cached = self._results.get(request.idempotency_key)
        if cached is not None:
            return cached

        decision_payload = request.bundle.receipt_for(RunStage.DECISION_READY).payload
        execution_payload = request.bundle.receipt_for(
            RunStage.ORDERS_SIMULATED
        ).payload
        reconcile_payload = request.bundle.receipt_for(RunStage.RECONCILED).payload
        reconciled_instant = _aware_utc(
            reconcile_payload.get("reconciled_at"),
            field_name="reconciled_at",
        )
        decisions = _mapping_rows(
            decision_payload.get("decisions"),
            field_name="decisions",
        )
        predictions = _mapping_rows(
            decision_payload.get("journal_predictions"),
            field_name="journal_predictions",
        )
        execution_receipts = _mapping_rows(
            execution_payload.get("order_receipts"),
            field_name="order_receipts",
        )
        self._validate_prediction_bindings(
            predictions=predictions,
            decisions=decisions,
        )
        candidate_set_event = self._candidate_set_event(
            request=request,
            raw_receipt=decision_payload.get("candidate_set_receipt"),
            predictions=predictions,
            decisions=decisions,
        )
        if execution_payload.get("journal_samples") not in (None, []):
            raise StagePortContractError("direct_execution_sample_append_forbidden")
        self._validate_prediction_sources(
            bundle=request.bundle,
            predictions=predictions,
        )

        bound_predictions = [
            self._bind_run_authority(row, request=request) for row in predictions
        ]
        canonical_outcomes = self._canonical_outcome_records(
            request=request,
            predictions=bound_predictions,
            execution_receipts=execution_receipts,
        )
        decision_exposures = self._decision_exposure_records(
            request=request,
            decisions=decisions,
            predictions=bound_predictions,
            execution_receipts=execution_receipts,
        )
        prediction_results = self._journal.append_predictions(bound_predictions)
        outcome_report: dict[str, Any] = {
            "exploration_fill_count": 0,
            "exploitation_fill_count": 0,
            "exit_stop_count": 0,
            "pairing_rejection_count": 0,
            "skipped_outcome_count": 0,
        }
        if canonical_outcomes:
            outcome_report = persist_simulation_outcomes(
                journal_path=self._journal.path,
                trade_date=request.bundle.context.trade_date,
                records=canonical_outcomes,
                risk_rejections=[],
                authority_scope={
                    "capital_authority_id": request.bundle.context.authority_id,
                    "authority_generation": (
                        request.bundle.context.authority_generation
                    ),
                    "execution_lineage_id": (request.bundle.context.execution_lineage),
                },
            )
            if outcome_report.get("skipped_outcome_count") != 0:
                raise StagePortContractError("canonical_outcome_pipeline_rejected")
            # The journal location is operational metadata. Persisting an
            # absolute host path in a StageReceipt makes otherwise identical
            # business evidence hash differently across output roots.
            outcome_report = dict(outcome_report)
            outcome_report.pop("journal_path", None)
        decision_ledger = SampleJournalDecisionLedger(
            journal=self._journal,
            source_run_id=request.run_id,
            input_bundle_sha256=request.input_bundle_sha256,
            capital_authority_id=request.bundle.context.authority_id,
            authority_generation=request.bundle.context.authority_generation,
            execution_lineage_id=request.bundle.context.execution_lineage,
        )
        for exposure in decision_exposures:
            decision_ledger.append(exposure, receipt_time=reconciled_instant)
        decision_ids = {exposure.decision_id for exposure in decision_exposures}
        decision_event_ids = [
            entry.journal_event_id
            for entry in decision_ledger.audit_records()
            if entry.record.decision_id in decision_ids
        ]
        chain_event = self._bind_run_authority(
            {
                "event_id": (
                    f"paper-day-chain:{request.run_id}:"
                    f"{request.input_bundle_sha256[:16]}"
                ),
                "record_type": "chain_validation",
                "classification": "chain_validation",
                "sample_layer": "chain_validation",
                "receipt_at": reconciled_instant.isoformat(),
                "trade_date": request.bundle.context.trade_date,
                "run_bundle_sha256": request.input_bundle_sha256,
                "stage_receipt_ids": [
                    receipt.receipt_id for receipt in request.bundle.stage_receipts
                ],
                "run_status_before_learning": request.bundle.status,
                "stop_new_risk": request.bundle.stop_new_risk,
                "position_authority_valid": request.bundle.position_authority_valid,
                "block_reasons": list(request.bundle.block_reasons),
            },
            request=request,
        )

        sample_results = self._journal.append_samples(
            [candidate_set_event, chain_event]
        )
        owned_outcomes = {
            (
                str(record["order"]["order_id"]),
                str(record["prediction_snapshot_id"]),
            )
            for record in canonical_outcomes
        }
        readback = self._journal.read_frozen(as_of=reconciled_instant)
        outcome_event_ids = [
            str(event.get("journal_event_id") or "")
            for event in readback.copy_events()
            if (
                event.get("record_type") == "fill"
                and (
                    str(event.get("order_id") or ""),
                    str(event.get("prediction_snapshot_id") or ""),
                )
                in owned_outcomes
                and event.get("source_run_id") == request.run_id
                and event.get("source_input_bundle_sha256")
                == request.input_bundle_sha256
                and event.get("capital_authority_id")
                == request.bundle.context.authority_id
                and event.get("authority_generation")
                == request.bundle.context.authority_generation
                and event.get("execution_lineage_id")
                == request.bundle.context.execution_lineage
            )
            and str(event.get("journal_event_id") or "")
        ]
        if len(outcome_event_ids) != len(canonical_outcomes):
            raise StagePortContractError("canonical_outcome_owned_readback_incomplete")
        event_ids = (
            [
                result["record"]["journal_event_id"]
                for result in (*prediction_results, *sample_results)
            ]
            + outcome_event_ids
            + decision_event_ids
        )
        event_ids = list(dict.fromkeys(event_ids))
        readback_event_ids = {
            str(event.get("journal_event_id") or "") for event in readback.copy_events()
        }
        if not set(event_ids).issubset(readback_event_ids):
            raise StagePortContractError("journal_authority_readback_incomplete")
        result = StageResult(
            payload={
                "recorded": True,
                "record_id": sample_results[-1]["record"]["journal_event_id"],
                "journal_authority": "SampleJournal",
                "source_run_id": request.run_id,
                "source_input_bundle_sha256": request.input_bundle_sha256,
                "authority_readback_verified": True,
                "prediction_count": len(prediction_results),
                "sample_count": (
                    len(sample_results)
                    + len(outcome_event_ids)
                    + len(decision_event_ids)
                ),
                "decision_exposure_count": len(decision_event_ids),
                "candidate_count": candidate_set_event["candidate_count"],
                "candidate_set_event_id": sample_results[0]["record"][
                    "journal_event_id"
                ],
                "canonical_outcome_count": len(canonical_outcomes),
                "canonical_outcome_report": outcome_report,
                "journal_event_ids": event_ids,
                "journal_event_ids_sha256": _canonical_sha256(event_ids),
                "journal_head_event_count": readback.journal_head_event_count,
                "journal_head_sha256": readback.journal_head_sha256,
                "journal_source_sha256": readback.journal_source_sha256,
            }
        )
        self._results[request.idempotency_key] = result
        return result


__all__ = [
    "LocalTodayReportPort",
    "SampleJournalLearningPort",
    "SharedSignalsResearchEvidencePort",
    "StagePortContractError",
]
