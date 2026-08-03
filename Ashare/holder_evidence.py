"""Receipt-bound, caller-invoked A-share holder facts.

This is a deliberately narrow TradingAgent consumer for the active TD holder
datasets.  It has no default client, scheduler, persistence, UI, provider
fallback, interpretation, candidate, risk, position, or order path.  A caller
must inject the generic TD V1 client and an immutable symbol cohort; each read
is catalog-bound, receipt-bound, bounded, and replayed before raw facts are
returned.
"""

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
from shared.universe.policy import classify_instrument

HOLDER_DATASET_IDS = (
    "cn.dataset.stk_holdernumber",
    "cn.dataset.stk_holdertrade",
)

_IDENTITIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "cn.dataset.stk_holdernumber": ("ts_code", "ann_date", "end_date"),
        "cn.dataset.stk_holdertrade": (
            "ts_code",
            "ann_date",
            "holder_name",
            "holder_type",
            "in_de",
            "change_vol",
            "change_ratio",
            "after_share",
            "after_ratio",
            "avg_price",
            "total_share",
        ),
    }
)
_REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "cn.dataset.stk_holdernumber": (
            "ts_code",
            "ann_date",
            "end_date",
            "holder_num",
        ),
        "cn.dataset.stk_holdertrade": _IDENTITIES["cn.dataset.stk_holdertrade"],
    }
)


class AshareHolderContractError(ValueError):
    """A stable, audit-safe reason for rejecting a holder fact read."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


# Reuse the existing process-local, audit-only ledger rather than create a
# second persistence mechanism.  Holder rejections remain non-actionable.
AshareHolderEvidenceAuditLedger = AshareEvidenceAuditLedger


def _strings(value: object, reason: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise AshareHolderContractError(reason)
    result: list[str] = []
    for item in value:
        text = _text(item, reason)
        if text in result:
            raise AshareHolderContractError(reason)
        result.append(text)
    if nonempty and not result:
        raise AshareHolderContractError(reason)
    return tuple(result)


def _parse_announcement_date(value: object, *, observed_at: datetime) -> str:
    raw = _text(value, "ashare_holder_time_missing")
    try:
        parsed = date.fromisoformat(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    except ValueError:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise AshareHolderContractError("ashare_holder_time_invalid") from exc
    if parsed > observed_at.astimezone(SHANGHAI).date():
        raise AshareHolderContractError("ashare_holder_time_after_availability")
    return raw


def _allowlist(value: tuple[str, ...]) -> frozenset[str]:
    if not isinstance(value, tuple) or not value:
        raise AshareHolderContractError("ashare_holder_allowed_symbols_invalid")
    normalized: list[str] = []
    for raw in value:
        symbol = _text(raw, "ashare_holder_allowed_symbols_invalid").upper()
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != symbol
        ):
            raise AshareHolderContractError(
                "ashare_holder_symbol_outside_mainboard_scope"
            )
        normalized.append(symbol)
    if len(normalized) != len(set(normalized)):
        raise AshareHolderContractError("ashare_holder_allowed_symbols_duplicate")
    return frozenset(normalized)


@dataclass(frozen=True)
class HolderDatasetProfile:
    """A TA-owned, exact catalog binding for one raw holder dataset."""

    expected_catalog_version: str
    observed_catalog_version: str
    dataset_id: str
    schema_major: int
    default_fields: tuple[str, ...]
    default_order: tuple[str, ...]
    identity_fields: tuple[str, ...]
    filter_operators: tuple[tuple[str, tuple[str, ...]], ...]
    dataset_contract_fingerprint: str
    consumer_profile_sha256: str
    max_pages: int
    max_rows: int
    page_limit: int
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE

    def __post_init__(self) -> None:
        if self.dataset_id not in HOLDER_DATASET_IDS:
            raise AshareHolderContractError("ashare_holder_dataset_not_allowlisted")
        if (
            self.catalog_route != FIXED_CATALOG_ROUTE
            or self.query_route != FIXED_QUERY_ROUTE
        ):
            raise AshareHolderContractError("ashare_holder_route_invalid")
        if (
            not isinstance(self.schema_major, int)
            or isinstance(self.schema_major, bool)
            or self.schema_major <= 0
        ):
            raise AshareHolderContractError("ashare_holder_schema_major_invalid")
        if self.identity_fields != _IDENTITIES[self.dataset_id]:
            raise AshareHolderContractError("ashare_holder_catalog_identity_mismatch")
        fields = set(self.default_fields)
        if not set(_REQUIRED_FIELDS[self.dataset_id]).issubset(fields):
            raise AshareHolderContractError(
                "ashare_holder_catalog_required_fields_missing"
            )
        if (
            self.max_pages <= 0
            or self.max_rows <= 0
            or self.page_limit <= 0
            or self.page_limit > self.max_rows
        ):
            raise AshareHolderContractError("ashare_holder_page_budget_invalid")
        if (
            len(self.dataset_contract_fingerprint) != 64
            or len(self.consumer_profile_sha256) != 64
        ):
            raise AshareHolderContractError("ashare_holder_profile_sha256_invalid")

    @classmethod
    def from_catalog_row(
        cls,
        catalog: CatalogEnvelope,
        row: Mapping[str, Any],
        *,
        expected_catalog_version: str,
    ) -> HolderDatasetProfile:
        dataset_id = _text(
            row.get("dataset_id"), "ashare_holder_catalog_dataset_id_invalid"
        )
        if dataset_id not in HOLDER_DATASET_IDS:
            raise AshareHolderContractError("ashare_holder_dataset_not_allowlisted")
        if not _active_catalog_row(row):
            raise AshareHolderContractError("ashare_holder_dataset_not_active")
        schema_major = row.get("schema_major")
        if (
            not isinstance(schema_major, int)
            or isinstance(schema_major, bool)
            or schema_major <= 0
        ):
            raise AshareHolderContractError(
                "ashare_holder_catalog_schema_major_invalid"
            )
        default_fields = _strings(
            row.get("default_fields"), "ashare_holder_catalog_default_fields_invalid"
        )
        default_order = _strings(
            row.get("default_order", []),
            "ashare_holder_catalog_default_order_invalid",
            nonempty=False,
        )
        identity_fields = _strings(
            row.get("identity_fields"), "ashare_holder_catalog_identity_invalid"
        )
        if identity_fields != _IDENTITIES[dataset_id]:
            raise AshareHolderContractError("ashare_holder_catalog_identity_mismatch")
        raw_operators = row.get("filter_operators")
        if not isinstance(raw_operators, Mapping):
            raise AshareHolderContractError(
                "ashare_holder_catalog_filter_operators_invalid"
            )
        operators = tuple(
            (
                name,
                _strings(
                    raw_operators[name],
                    "ashare_holder_catalog_filter_operators_invalid",
                ),
            )
            for name in sorted(raw_operators)
            if isinstance(name, str)
        )
        if len(operators) != len(raw_operators) or "in" not in dict(operators).get(
            "ts_code", ()
        ):
            raise AshareHolderContractError("ashare_holder_symbol_filter_unsupported")
        limits = row.get("limits")
        page_limit = (
            limits.get("max_page_size") if isinstance(limits, Mapping) else None
        )
        if (
            not isinstance(page_limit, int)
            or isinstance(page_limit, bool)
            or page_limit <= 0
        ):
            raise AshareHolderContractError("ashare_holder_catalog_page_limit_invalid")
        try:
            fingerprint = dataset_contract_fingerprint(row)
        except (TypeError, ValueError) as exc:
            raise AshareHolderContractError(
                "ashare_holder_dataset_contract_invalid"
            ) from exc
        material = {
            "dataset_contract_fingerprint": fingerprint,
            "default_fields": list(default_fields),
            "default_order": list(default_order),
            "identity_fields": list(identity_fields),
            "filter_operators": list(operators),
            "semantics": "raw_receipt_bound_holder_facts_only_no_ownership_inference",
        }
        return cls(
            expected_catalog_version=expected_catalog_version,
            observed_catalog_version=catalog.catalog_version,
            dataset_id=dataset_id,
            schema_major=schema_major,
            default_fields=default_fields,
            default_order=default_order,
            identity_fields=identity_fields,
            filter_operators=operators,
            dataset_contract_fingerprint=fingerprint,
            consumer_profile_sha256=_sha256(material),
            max_pages=16,
            max_rows=page_limit * 16,
            page_limit=page_limit,
        )


@dataclass(frozen=True)
class HolderProfileSet:
    expected_catalog_version: str
    observed_catalog_version: str
    by_dataset: Mapping[str, HolderDatasetProfile]
    consumer_profile_set_sha256: str
    catalog_route: str = FIXED_CATALOG_ROUTE

    def __post_init__(self) -> None:
        profiles = dict(self.by_dataset)
        if set(profiles) != set(HOLDER_DATASET_IDS) or any(
            dataset_id != profile.dataset_id
            or profile.expected_catalog_version != self.expected_catalog_version
            or profile.observed_catalog_version != self.observed_catalog_version
            for dataset_id, profile in profiles.items()
        ):
            raise AshareHolderContractError("ashare_holder_profile_set_binding_invalid")
        if self.catalog_route != FIXED_CATALOG_ROUTE:
            raise AshareHolderContractError("ashare_holder_route_invalid")
        object.__setattr__(self, "by_dataset", MappingProxyType(profiles))


@dataclass(frozen=True)
class HolderEvidenceFact:
    """One raw source row, never an ownership, recommendation, or trade signal."""

    dataset_id: str
    symbol: str
    announcement_date: str
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
        if self.dataset_id not in HOLDER_DATASET_IDS or any(
            (
                self.candidate_eligible,
                self.execution_eligible,
                self.risk_authority,
                self.position_authority,
                self.real_trading_enabled,
            )
        ):
            raise AshareHolderContractError("ashare_holder_fact_authority_invalid")
        if not self.values or set(_REQUIRED_FIELDS[self.dataset_id]).difference(
            self.values
        ):
            raise AshareHolderContractError("ashare_holder_fact_fields_invalid")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class HolderEvidenceSnapshotBatch:
    profile: HolderDatasetProfile
    facts: tuple[HolderEvidenceFact, ...]
    page_count: int
    row_count: int
    pagination_trace_sha256: str
    first_semantic_sha256: str
    replay_semantic_sha256: str
    same_observation: bool
    contract_ready_only: bool = True
    query_route: str = FIXED_QUERY_ROUTE

    def __post_init__(self) -> None:
        if (
            not self.facts
            or self.row_count != len(self.facts)
            or not self.same_observation
            or not self.contract_ready_only
            or self.query_route != FIXED_QUERY_ROUTE
        ):
            raise AshareHolderContractError("ashare_holder_snapshot_invalid")
        if self.first_semantic_sha256 != self.replay_semantic_sha256:
            raise AshareHolderContractError("ashare_holder_same_observation_mismatch")


def _validate_metadata(
    run: PagedQueryRun, profile: HolderDatasetProfile, decision_time: datetime
) -> tuple[datetime, str, str]:
    run.verify_integrity(identity_fields=profile.identity_fields)
    envelope = run.envelope
    metadata = envelope.metadata
    if envelope.dataset_id != profile.dataset_id:
        raise AshareHolderContractError("ashare_holder_query_binding_mismatch")
    if metadata.state.strip().lower() != "ready" or metadata.degraded is not False:
        raise AshareHolderContractError("ashare_holder_metadata_not_ready")
    if not _fresh(metadata.freshness):
        raise AshareHolderContractError("ashare_holder_metadata_not_fresh")
    if not _valid_quality(metadata.quality):
        raise AshareHolderContractError("ashare_holder_metadata_quality_invalid")
    if not _complete_lineage(metadata.lineage):
        raise AshareHolderContractError("ashare_holder_metadata_lineage_incomplete")
    if not all(
        isinstance(item, str) and item and item == item.strip()
        for item in (metadata.receipt_id, metadata.data_through, metadata.observed_at)
    ):
        raise AshareHolderContractError("ashare_holder_metadata_proof_incomplete")
    data_through = _parse_aware_iso(
        metadata.data_through, "ashare_holder_data_through_invalid"
    )
    observed_at = _parse_aware_iso(
        metadata.observed_at, "ashare_holder_observed_at_invalid"
    )
    decision = _aware(decision_time, "ashare_holder_decision_time_timezone_required")
    if data_through > observed_at:
        raise AshareHolderContractError("ashare_holder_metadata_time_order_invalid")
    if observed_at > decision:
        raise AshareHolderContractError("ashare_holder_available_after_decision")
    assert metadata.lineage is not None
    return (
        observed_at,
        _sha256(metadata.lineage),
        _sha256(
            {
                "dataset_id": envelope.dataset_id,
                "catalog_version": envelope.catalog_version,
                "receipt_id": metadata.receipt_id,
                "data_through": metadata.data_through,
                "observed_at": metadata.observed_at,
                "freshness": metadata.freshness,
                "quality": metadata.quality,
                "lineage": metadata.lineage,
            }
        ),
    )


def _map_run(
    *,
    profile: HolderDatasetProfile,
    run: PagedQueryRun,
    decision_time: datetime,
    allowed_symbols: frozenset[str],
) -> tuple[HolderEvidenceFact, ...]:
    observed_at, lineage_sha, envelope_sha = _validate_metadata(
        run, profile, decision_time
    )
    facts: list[HolderEvidenceFact] = []
    for row in run.envelope.data:
        symbol = _text(row.get("ts_code"), "ashare_holder_symbol_missing").upper()
        if symbol not in allowed_symbols:
            raise AshareHolderContractError("ashare_holder_symbol_mismatch")
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != symbol
        ):
            raise AshareHolderContractError(
                "ashare_holder_symbol_outside_mainboard_scope"
            )
        announcement_date = _parse_announcement_date(
            row.get("ann_date"), observed_at=observed_at
        )
        if set(_REQUIRED_FIELDS[profile.dataset_id]).difference(row):
            raise AshareHolderContractError("ashare_holder_row_required_fields_missing")
        facts.append(
            HolderEvidenceFact(
                dataset_id=profile.dataset_id,
                symbol=symbol,
                announcement_date=announcement_date,
                values={
                    field: row[field]
                    for field in profile.default_fields
                    if field in row
                },
                receipt_id=str(run.envelope.metadata.receipt_id),
                source_lineage_sha256=lineage_sha,
                source_row_sha256=_sha256(row),
                envelope_proof_sha256=envelope_sha,
            )
        )
    if not facts:
        raise AshareHolderContractError("ashare_holder_query_returned_no_rows")
    return tuple(facts)


def _snapshot_from_runs(
    *,
    profile: HolderDatasetProfile,
    first: PagedQueryRun,
    replay: PagedQueryRun,
    decision_time: datetime,
    allowed_symbols: tuple[str, ...],
) -> HolderEvidenceSnapshotBatch:
    if first.envelope.catalog_version != replay.envelope.catalog_version:
        raise AshareHolderContractError("ashare_holder_query_catalog_version_drift")
    if (
        first.semantic_sha256 != replay.semantic_sha256
        or first.semantic_trace_sha256 != replay.semantic_trace_sha256
    ):
        raise AshareHolderContractError("ashare_holder_same_observation_mismatch")
    allowlist = _allowlist(allowed_symbols)
    facts = _map_run(
        profile=profile,
        run=first,
        decision_time=decision_time,
        allowed_symbols=allowlist,
    )
    replay_facts = _map_run(
        profile=profile,
        run=replay,
        decision_time=decision_time,
        allowed_symbols=allowlist,
    )
    if [fact.source_row_sha256 for fact in facts] != [
        fact.source_row_sha256 for fact in replay_facts
    ]:
        raise AshareHolderContractError("ashare_holder_same_observation_mismatch")
    return HolderEvidenceSnapshotBatch(
        profile=profile,
        facts=facts,
        page_count=first.page_count,
        row_count=len(facts),
        pagination_trace_sha256=first.pagination_trace_sha256,
        first_semantic_sha256=first.semantic_sha256,
        replay_semantic_sha256=replay.semantic_sha256,
        same_observation=True,
    )


def _record_failure(
    *,
    audit_ledger: AshareHolderEvidenceAuditLedger,
    reason: str,
    dataset_id: str,
    profile: HolderDatasetProfile | None,
    decision_time: datetime,
    observed_catalog_version: str,
) -> None:
    expected_catalog_version = (
        profile.expected_catalog_version if profile else "unconfigured"
    )
    audit_ledger.append(
        AshareEvidenceAuditRecord(
            reason_code=reason,
            dataset_id=dataset_id,
            expected_catalog_version=expected_catalog_version,
            observed_catalog_version=observed_catalog_version,
            catalog_version_drift=(
                expected_catalog_version != observed_catalog_version
            ),
            dataset_contract_fingerprint=(
                profile.dataset_contract_fingerprint if profile else None
            ),
            consumer_profile_sha256=(
                profile.consumer_profile_sha256 if profile else None
            ),
            decision_time=_aware(
                decision_time, "ashare_holder_decision_time_timezone_required"
            ),
            rejected_payload_sha256=_sha256(
                {"dataset_id": dataset_id, "reason": reason}
            ),
        )
    )


class AshareHolderEvidencePort(Protocol):
    def freeze_holder_profiles(
        self, *, audit_ledger: AshareHolderEvidenceAuditLedger
    ) -> HolderProfileSet: ...
    def load_holder_snapshot(
        self,
        *,
        profile: HolderDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        audit_ledger: AshareHolderEvidenceAuditLedger,
        allowed_symbols: tuple[str, ...],
    ) -> HolderEvidenceSnapshotBatch: ...


class TradingDatasAshareHolderEvidencePort:
    """Injected generic TD V1 adapter; no client is constructed implicitly."""

    def __init__(self, client: SharedSignalsV1Client) -> None:
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        self._client = client

    def freeze_holder_profiles(
        self, *, audit_ledger: AshareHolderEvidenceAuditLedger
    ) -> HolderProfileSet:
        decision = datetime.now().astimezone()
        observed = "unobserved"
        try:
            if self._client.config.catalog_version_policy != "evidence_only":
                raise AshareHolderContractError(
                    "ashare_holder_catalog_version_policy_invalid"
                )
            catalog = self._client.get_catalog()
            observed = catalog.catalog_version
            rows: dict[str, Mapping[str, Any]] = {}
            for row in catalog.data:
                dataset_id = row.get("dataset_id")
                if dataset_id in HOLDER_DATASET_IDS:
                    if dataset_id in rows:
                        raise AshareHolderContractError(
                            "ashare_holder_dataset_catalog_row_duplicate"
                        )
                    rows[dataset_id] = row
            if set(rows) != set(HOLDER_DATASET_IDS) or not set(
                HOLDER_DATASET_IDS
            ).issubset(self._client.config.dataset_ids):
                raise AshareHolderContractError(
                    "ashare_holder_dataset_catalog_row_missing"
                )
            profiles = {
                dataset_id: HolderDatasetProfile.from_catalog_row(
                    catalog,
                    rows[dataset_id],
                    expected_catalog_version=self._client.config.expected_catalog_version,
                )
                for dataset_id in HOLDER_DATASET_IDS
            }
            return HolderProfileSet(
                expected_catalog_version=self._client.config.expected_catalog_version,
                observed_catalog_version=observed,
                by_dataset=profiles,
                consumer_profile_set_sha256=_sha256(
                    {
                        dataset_id: profile.consumer_profile_sha256
                        for dataset_id, profile in profiles.items()
                    }
                ),
            )
        except AshareHolderContractError as exc:
            _record_failure(
                audit_ledger=audit_ledger,
                reason=exc.reason_code,
                dataset_id="catalog",
                profile=None,
                decision_time=decision,
                observed_catalog_version=observed,
            )
            raise
        except SharedSignalsV1Error as exc:
            _record_failure(
                audit_ledger=audit_ledger,
                reason="ashare_holder_catalog_failed",
                dataset_id="catalog",
                profile=None,
                decision_time=decision,
                observed_catalog_version=observed,
            )
            raise AshareHolderContractError("ashare_holder_catalog_failed") from exc

    def load_holder_snapshot(
        self,
        *,
        profile: HolderDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        audit_ledger: AshareHolderEvidenceAuditLedger,
        allowed_symbols: tuple[str, ...],
    ) -> HolderEvidenceSnapshotBatch:
        if not isinstance(profile, HolderDatasetProfile):
            raise AshareHolderContractError("ashare_holder_dataset_not_allowlisted")
        observed = "unobserved"
        try:
            if self._client.config.catalog_version_policy != "evidence_only":
                raise AshareHolderContractError(
                    "ashare_holder_catalog_version_policy_invalid"
                )
            catalog = self._client.get_catalog()
            observed = catalog.catalog_version
            matches = [
                row
                for row in catalog.data
                if row.get("dataset_id") == profile.dataset_id
            ]
            if len(matches) != 1:
                raise AshareHolderContractError(
                    "ashare_holder_dataset_catalog_row_missing"
                )
            current = HolderDatasetProfile.from_catalog_row(
                catalog,
                matches[0],
                expected_catalog_version=profile.expected_catalog_version,
            )
            if (
                current.dataset_contract_fingerprint
                != profile.dataset_contract_fingerprint
                or current.consumer_profile_sha256 != profile.consumer_profile_sha256
            ):
                raise AshareHolderContractError("ashare_holder_dataset_contract_drift")
            symbols = _allowlist(allowed_symbols)
            if filters != {"ts_code": {"in": sorted(symbols)}}:
                raise AshareHolderContractError("ashare_holder_filters_unsupported")
            request = QueryRequest(
                dataset_id=profile.dataset_id,
                schema_major=profile.schema_major,
                fields=profile.default_fields,
                filters=dict(filters),
                as_of=_aware(
                    decision_time, "ashare_holder_decision_time_timezone_required"
                ).isoformat(),
                order=profile.default_order or None,
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
            return _snapshot_from_runs(
                profile=profile,
                first=first,
                replay=replay,
                decision_time=decision_time,
                allowed_symbols=tuple(sorted(symbols)),
            )
        except AshareHolderContractError as exc:
            _record_failure(
                audit_ledger=audit_ledger,
                reason=exc.reason_code,
                dataset_id=profile.dataset_id,
                profile=profile,
                decision_time=decision_time,
                observed_catalog_version=observed,
            )
            raise
        except (PaginationContractError, SharedSignalsV1Error) as exc:
            _record_failure(
                audit_ledger=audit_ledger,
                reason="ashare_holder_query_failed",
                dataset_id=profile.dataset_id,
                profile=profile,
                decision_time=decision_time,
                observed_catalog_version=observed,
            )
            raise AshareHolderContractError("ashare_holder_query_failed") from exc


def load_holder_snapshots(
    *,
    port: AshareHolderEvidencePort,
    profiles: HolderProfileSet,
    decision_time: datetime,
    audit_ledger: AshareHolderEvidenceAuditLedger,
    allowed_symbols: tuple[str, ...],
) -> tuple[HolderEvidenceSnapshotBatch, ...]:
    """Explicitly read both holder sources for one caller-provided frozen cohort."""

    if not isinstance(profiles, HolderProfileSet):
        raise AshareHolderContractError("ashare_holder_profile_set_invalid")
    symbols = tuple(sorted(_allowlist(allowed_symbols)))
    filters = {"ts_code": {"in": list(symbols)}}
    return tuple(
        port.load_holder_snapshot(
            profile=profiles.by_dataset[dataset_id],
            filters=filters,
            decision_time=decision_time,
            audit_ledger=audit_ledger,
            allowed_symbols=symbols,
        )
        for dataset_id in HOLDER_DATASET_IDS
    )
