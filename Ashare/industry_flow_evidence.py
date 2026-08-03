"""Caller-invoked receipt-bound raw A-share industry-flow context facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Protocol

from Ashare.event_evidence import (
    FIXED_CATALOG_ROUTE,
    FIXED_QUERY_ROUTE,
    SHANGHAI,
    AshareEvidenceAuditLedger,
    AshareEvidenceAuditRecord,
    _active_catalog_row,
    _aware,
    _complete_lineage,
    _fresh,
    _parse_aware_iso,
    _sha256,
    _text,
    _valid_quality,
)
from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_pagination import (
    PagedQueryRun,
    PaginationContractError,
    collect_query_pages,
)
from shared.governance.evidence_readiness import dataset_contract_fingerprint

INDUSTRY_FLOW_DATASET_IDS = (
    "cn.dataset.moneyflow_ind_dc",
    "cn.dataset.moneyflow_ind_ths",
)
_IDENTITIES = MappingProxyType(
    {
        "cn.dataset.moneyflow_ind_dc": ("trade_date", "content_type", "ts_code"),
        "cn.dataset.moneyflow_ind_ths": ("trade_date", "ts_code"),
    }
)
_REQUIRED = MappingProxyType(
    {
        "cn.dataset.moneyflow_ind_dc": (
            "trade_date",
            "content_type",
            "ts_code",
            "net_amount",
        ),
        "cn.dataset.moneyflow_ind_ths": ("trade_date", "ts_code", "net_amount"),
    }
)


class AshareIndustryFlowContractError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


AshareIndustryFlowEvidenceAuditLedger = AshareEvidenceAuditLedger


def _strings(value: object, reason: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AshareIndustryFlowContractError(reason)
    result = tuple(_text(item, reason) for item in value)
    if len(set(result)) != len(result):
        raise AshareIndustryFlowContractError(reason)
    return result


def _trade_date(value: object, observed: datetime) -> str:
    raw = _text(value, "ashare_industry_flow_time_missing")
    try:
        parsed = date.fromisoformat(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    except ValueError:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_time_invalid"
            ) from exc
    if parsed > observed.astimezone(SHANGHAI).date():
        raise AshareIndustryFlowContractError(
            "ashare_industry_flow_time_after_availability"
        )
    return raw


@dataclass(frozen=True)
class IndustryFlowDatasetProfile:
    expected_catalog_version: str
    observed_catalog_version: str
    dataset_id: str
    schema_major: int
    default_fields: tuple[str, ...]
    default_order: tuple[str, ...]
    identity_fields: tuple[str, ...]
    dataset_contract_fingerprint: str
    consumer_profile_sha256: str
    max_pages: int
    max_rows: int
    page_limit: int
    time_field: str = "trade_date"
    timezone: str = "Asia/Shanghai"
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE

    def __post_init__(self) -> None:
        if self.dataset_id not in INDUSTRY_FLOW_DATASET_IDS:
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_dataset_not_allowlisted"
            )
        if self.schema_major <= 0 or isinstance(self.schema_major, bool):
            raise AshareIndustryFlowContractError("ashare_industry_flow_schema_invalid")
        if self.identity_fields != _IDENTITIES[self.dataset_id] or not set(
            _REQUIRED[self.dataset_id]
        ).issubset(self.default_fields):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_catalog_identity_mismatch"
            )
        if (
            self.time_field != "trade_date"
            or self.timezone != "Asia/Shanghai"
            or self.catalog_route != FIXED_CATALOG_ROUTE
            or self.query_route != FIXED_QUERY_ROUTE
        ):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_profile_semantics_invalid"
            )

    @classmethod
    def from_catalog_row(
        cls,
        catalog: CatalogEnvelope,
        row: Mapping[str, Any],
        *,
        expected_catalog_version: str,
    ) -> IndustryFlowDatasetProfile:
        dataset_id = _text(
            row.get("dataset_id"), "ashare_industry_flow_catalog_dataset_invalid"
        )
        if dataset_id not in INDUSTRY_FLOW_DATASET_IDS:
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_dataset_not_allowlisted"
            )
        if not _active_catalog_row(row):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_dataset_not_active"
            )
        fields = _strings(
            row.get("default_fields"), "ashare_industry_flow_catalog_fields_invalid"
        )
        identity = _strings(
            row.get("identity_fields"), "ashare_industry_flow_catalog_identity_invalid"
        )
        schema = row.get("schema_major")
        limits = row.get("limits")
        limit = limits.get("max_page_size") if isinstance(limits, Mapping) else None
        if (
            not isinstance(schema, int)
            or isinstance(schema, bool)
            or schema <= 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_catalog_schema_or_limit_invalid"
            )
        if identity != _IDENTITIES[dataset_id]:
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_catalog_identity_mismatch"
            )
        try:
            fingerprint = dataset_contract_fingerprint(row)
        except (TypeError, ValueError) as exc:
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_catalog_contract_invalid"
            ) from exc
        material = {
            "dataset_contract_fingerprint": fingerprint,
            "fields": list(fields),
            "identity": list(identity),
            "time_field": "trade_date",
            "timezone": "Asia/Shanghai",
            "semantics": "raw_industry_flow_context_only",
        }
        return cls(
            expected_catalog_version,
            catalog.catalog_version,
            dataset_id,
            schema,
            fields,
            _strings(
                row.get("default_order", ["trade_date:asc"]),
                "ashare_industry_flow_catalog_order_invalid",
            ),
            identity,
            fingerprint,
            _sha256(material),
            16,
            limit * 16,
            limit,
        )


@dataclass(frozen=True)
class IndustryFlowProfileSet:
    expected_catalog_version: str
    observed_catalog_version: str
    by_dataset: Mapping[str, IndustryFlowDatasetProfile]
    consumer_profile_set_sha256: str

    def __post_init__(self) -> None:
        profiles = dict(self.by_dataset)
        if set(profiles) != set(INDUSTRY_FLOW_DATASET_IDS):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_profile_set_invalid"
            )
        object.__setattr__(self, "by_dataset", MappingProxyType(profiles))


@dataclass(frozen=True)
class IndustryFlowFact:
    dataset_id: str
    trade_date: str
    values: Mapping[str, Any]
    receipt_id: str
    source_lineage_sha256: str
    source_row_sha256: str
    envelope_proof_sha256: str
    candidate_eligible: bool = False
    execution_eligible: bool = False
    risk_authority: bool = False
    position_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.dataset_id not in INDUSTRY_FLOW_DATASET_IDS or any(
            (
                self.candidate_eligible,
                self.execution_eligible,
                self.risk_authority,
                self.position_authority,
                self.real_trading_enabled,
            )
        ):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_authority_invalid"
            )
        if set(_REQUIRED[self.dataset_id]).difference(self.values):
            raise AshareIndustryFlowContractError("ashare_industry_flow_fields_invalid")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class IndustryFlowSnapshotBatch:
    profile: IndustryFlowDatasetProfile
    facts: tuple[IndustryFlowFact, ...]
    page_count: int
    row_count: int
    first_semantic_sha256: str
    replay_semantic_sha256: str
    same_observation: bool
    contract_ready_only: bool = True

    def __post_init__(self) -> None:
        if (
            not self.facts
            or self.row_count != len(self.facts)
            or not self.same_observation
            or not self.contract_ready_only
            or self.first_semantic_sha256 != self.replay_semantic_sha256
        ):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_snapshot_invalid"
            )


def _map(
    profile: IndustryFlowDatasetProfile, run: PagedQueryRun, decision: datetime
) -> tuple[IndustryFlowFact, ...]:
    run.verify_integrity(identity_fields=profile.identity_fields)
    envelope = run.envelope
    meta = envelope.metadata
    if envelope.dataset_id != profile.dataset_id:
        raise AshareIndustryFlowContractError(
            "ashare_industry_flow_query_binding_mismatch"
        )
    if meta.state.strip().lower() != "ready" or meta.degraded is not False:
        raise AshareIndustryFlowContractError("ashare_industry_flow_metadata_not_ready")
    if not _fresh(meta.freshness):
        raise AshareIndustryFlowContractError("ashare_industry_flow_metadata_not_fresh")
    if not _valid_quality(meta.quality):
        raise AshareIndustryFlowContractError(
            "ashare_industry_flow_metadata_quality_invalid"
        )
    if not _complete_lineage(meta.lineage):
        raise AshareIndustryFlowContractError(
            "ashare_industry_flow_metadata_lineage_incomplete"
        )
    if not all(
        isinstance(x, str) and x.strip() == x and x
        for x in (meta.receipt_id, meta.data_through, meta.observed_at)
    ):
        raise AshareIndustryFlowContractError(
            "ashare_industry_flow_metadata_proof_incomplete"
        )
    through = _parse_aware_iso(
        meta.data_through, "ashare_industry_flow_data_through_invalid"
    )
    observed = _parse_aware_iso(
        meta.observed_at, "ashare_industry_flow_observed_at_invalid"
    )
    if through > observed or observed > decision:
        raise AshareIndustryFlowContractError("ashare_industry_flow_time_order_invalid")
    assert meta.lineage is not None
    lineage = _sha256(meta.lineage)
    proof = _sha256(
        {
            "dataset_id": envelope.dataset_id,
            "receipt_id": meta.receipt_id,
            "data_through": meta.data_through,
            "observed_at": meta.observed_at,
            "lineage": meta.lineage,
        }
    )
    facts = []
    for row in envelope.data:
        if set(_REQUIRED[profile.dataset_id]).difference(row):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_row_required_fields_missing"
            )
        facts.append(
            IndustryFlowFact(
                profile.dataset_id,
                _trade_date(row.get("trade_date"), observed),
                {field: row[field] for field in profile.default_fields if field in row},
                str(meta.receipt_id),
                lineage,
                _sha256(row),
                proof,
            )
        )
    if not facts:
        raise AshareIndustryFlowContractError(
            "ashare_industry_flow_query_returned_no_rows"
        )
    return tuple(facts)


def _record(
    audit: AshareIndustryFlowEvidenceAuditLedger,
    reason: str,
    dataset: str,
    profile: IndustryFlowDatasetProfile | None,
    decision: datetime,
    observed: str,
) -> None:
    expected = profile.expected_catalog_version if profile else "unconfigured"
    audit.append(
        AshareEvidenceAuditRecord(
            reason,
            dataset,
            expected,
            observed,
            expected != observed,
            profile.dataset_contract_fingerprint if profile else None,
            profile.consumer_profile_sha256 if profile else None,
            decision,
            _sha256({"dataset": dataset, "reason": reason}),
        )
    )


class AshareIndustryFlowEvidencePort(Protocol):
    def freeze_industry_flow_profiles(
        self, *, audit_ledger: AshareIndustryFlowEvidenceAuditLedger
    ) -> IndustryFlowProfileSet: ...
    def load_industry_flow_snapshot(
        self,
        *,
        profile: IndustryFlowDatasetProfile,
        decision_time: datetime,
        audit_ledger: AshareIndustryFlowEvidenceAuditLedger,
    ) -> IndustryFlowSnapshotBatch: ...


class TradingDatasAshareIndustryFlowEvidencePort:
    def __init__(self, client: SharedSignalsV1Client) -> None:
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        self._client = client

    def freeze_industry_flow_profiles(
        self, *, audit_ledger: AshareIndustryFlowEvidenceAuditLedger
    ) -> IndustryFlowProfileSet:
        now = datetime.now().astimezone()
        observed = "unobserved"
        try:
            if self._client.config.catalog_version_policy != "evidence_only":
                raise AshareIndustryFlowContractError(
                    "ashare_industry_flow_catalog_policy_invalid"
                )
            catalog = self._client.get_catalog()
            observed = catalog.catalog_version
            rows = {}
            for row in catalog.data:
                dataset = row.get("dataset_id")
                if dataset in INDUSTRY_FLOW_DATASET_IDS:
                    if dataset in rows:
                        raise AshareIndustryFlowContractError(
                            "ashare_industry_flow_catalog_duplicate"
                        )
                    rows[dataset] = row
            if set(rows) != set(INDUSTRY_FLOW_DATASET_IDS) or not set(
                INDUSTRY_FLOW_DATASET_IDS
            ).issubset(self._client.config.dataset_ids):
                raise AshareIndustryFlowContractError(
                    "ashare_industry_flow_catalog_missing"
                )
            profiles = {
                dataset: IndustryFlowDatasetProfile.from_catalog_row(
                    catalog,
                    rows[dataset],
                    expected_catalog_version=self._client.config.expected_catalog_version,
                )
                for dataset in INDUSTRY_FLOW_DATASET_IDS
            }
            return IndustryFlowProfileSet(
                self._client.config.expected_catalog_version,
                observed,
                profiles,
                _sha256({d: p.consumer_profile_sha256 for d, p in profiles.items()}),
            )
        except AshareIndustryFlowContractError as exc:
            _record(audit_ledger, exc.reason_code, "catalog", None, now, observed)
            raise
        except SharedSignalsV1Error as exc:
            _record(
                audit_ledger,
                "ashare_industry_flow_catalog_failed",
                "catalog",
                None,
                now,
                observed,
            )
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_catalog_failed"
            ) from exc

    def load_industry_flow_snapshot(
        self,
        *,
        profile: IndustryFlowDatasetProfile,
        decision_time: datetime,
        audit_ledger: AshareIndustryFlowEvidenceAuditLedger,
    ) -> IndustryFlowSnapshotBatch:
        if not isinstance(profile, IndustryFlowDatasetProfile):
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_dataset_not_allowlisted"
            )
        observed = "unobserved"
        try:
            decision = _aware(
                decision_time, "ashare_industry_flow_decision_time_invalid"
            )
            catalog = self._client.get_catalog()
            observed = catalog.catalog_version
            rows = [
                row
                for row in catalog.data
                if row.get("dataset_id") == profile.dataset_id
            ]
            if len(rows) != 1:
                raise AshareIndustryFlowContractError(
                    "ashare_industry_flow_catalog_missing"
                )
            current = IndustryFlowDatasetProfile.from_catalog_row(
                catalog,
                rows[0],
                expected_catalog_version=profile.expected_catalog_version,
            )
            if (
                current.dataset_contract_fingerprint
                != profile.dataset_contract_fingerprint
                or current.consumer_profile_sha256 != profile.consumer_profile_sha256
            ):
                raise AshareIndustryFlowContractError(
                    "ashare_industry_flow_catalog_drift"
                )
            request = QueryRequest(
                dataset_id=profile.dataset_id,
                schema_major=profile.schema_major,
                fields=profile.default_fields,
                as_of=decision.isoformat(),
                order=profile.default_order,
                limit=profile.page_limit,
            )
            first = collect_query_pages(
                client=self._client,
                request=request,
                identity_fields=profile.identity_fields,
                max_pages=profile.max_pages,
                max_rows=profile.max_rows,
            )
            replay = collect_query_pages(
                client=self._client,
                request=request,
                identity_fields=profile.identity_fields,
                max_pages=profile.max_pages,
                max_rows=profile.max_rows,
            )
            if (
                first.semantic_sha256 != replay.semantic_sha256
                or first.semantic_trace_sha256 != replay.semantic_trace_sha256
            ):
                raise AshareIndustryFlowContractError(
                    "ashare_industry_flow_replay_drift"
                )
            facts = _map(profile, first, decision)
            replay_facts = _map(profile, replay, decision)
            if [fact.source_row_sha256 for fact in facts] != [
                fact.source_row_sha256 for fact in replay_facts
            ]:
                raise AshareIndustryFlowContractError(
                    "ashare_industry_flow_replay_drift"
                )
            return IndustryFlowSnapshotBatch(
                profile,
                facts,
                first.page_count,
                len(facts),
                first.semantic_sha256,
                replay.semantic_sha256,
                True,
            )
        except AshareIndustryFlowContractError as exc:
            _record(
                audit_ledger,
                exc.reason_code,
                profile.dataset_id,
                profile,
                decision_time,
                observed,
            )
            raise
        except (PaginationContractError, SharedSignalsV1Error) as exc:
            _record(
                audit_ledger,
                "ashare_industry_flow_query_failed",
                profile.dataset_id,
                profile,
                decision_time,
                observed,
            )
            raise AshareIndustryFlowContractError(
                "ashare_industry_flow_query_failed"
            ) from exc


def load_industry_flow_snapshots(
    *,
    port: AshareIndustryFlowEvidencePort,
    profiles: IndustryFlowProfileSet,
    decision_time: datetime,
    audit_ledger: AshareIndustryFlowEvidenceAuditLedger,
) -> tuple[IndustryFlowSnapshotBatch, ...]:
    if not isinstance(profiles, IndustryFlowProfileSet):
        raise AshareIndustryFlowContractError(
            "ashare_industry_flow_profile_set_invalid"
        )
    return tuple(
        port.load_industry_flow_snapshot(
            profile=profiles.by_dataset[dataset],
            decision_time=decision_time,
            audit_ledger=audit_ledger,
        )
        for dataset in INDUSTRY_FLOW_DATASET_IDS
    )
