"""Fixture-first A-share event and sentiment evidence port.

This module is a non-production, provider-neutral consumer of the frozen
TradingDatas V1 data plane.  It accepts an explicitly injected client, freezes
dataset profiles from ``GET /v1/catalog``, and reads rows only through
``POST /v1/query``.  It has no default transport, credential, network call,
persistence path, runtime hook, candidate authority, or trading authority.

Provider rows remain untrusted until catalog identity, bounded pagination,
same-observation replay, metadata quality, lineage, receipt and point-in-time
ordering all validate.  Rejected evidence enters only the process-local audit
ledger.  Accepted evidence can produce deterministic shadow observations and
an optional offline LLM evidence request, but never an order, position, risk
decision, trained sample, calibrated probability, or promotion decision.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

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
from shared.llm.evidence_artifact import EvidenceArtifact
from shared.llm.schema import LLMEvidenceRequest
from shared.review.decision_ledger import (
    DecisionExposureRecord,
    ExposureDisposition,
    InMemoryDecisionLedger,
)
from shared.universe.policy import classify_instrument


FIXED_CATALOG_ROUTE = "GET /v1/catalog"
FIXED_QUERY_ROUTE = "POST /v1/query"

PRIMARY_DATASET_IDS = (
    "cn.dataset.anns_d",
    "cn.dataset.cctv_news",
    "cn.dataset.irm_qa_sh",
    "cn.dataset.irm_qa_sz",
    "cn.dataset.research_report",
)
OPTIONAL_DATASET_IDS = (
    "cn.dataset.disclosure_date",
    "cn.dataset.report_rc",
    "cn.dataset.broker_recommend",
    "cn.dataset.stk_surv",
)
PAUSED_DATASET_IDS = ("cn.dataset.major_news", "cn.dataset.news")

SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256_HEX = frozenset("0123456789abcdef")


class AshareEvidenceContractError(ValueError):
    """Fail-closed evidence boundary failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AshareEvidenceContractError(
            "ashare_evidence_payload_not_canonical"
        ) from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise AshareEvidenceContractError(reason)
    return value


def _optional_text(value: object, reason: str) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, reason)


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise AshareEvidenceContractError(reason)
    return value


def _parse_aware_iso(value: object, reason: str) -> datetime:
    raw = _text(value, reason)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AshareEvidenceContractError(reason) from exc
    return _aware(parsed, reason)


def _sha256_text(value: object, reason: str) -> str:
    text = _text(value, reason)
    if len(text) != 64 or any(character not in _SHA256_HEX for character in text):
        raise AshareEvidenceContractError(reason)
    return text


def _strings(
    value: object,
    reason: str,
    *,
    nonempty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise AshareEvidenceContractError(reason)
    result: list[str] = []
    for item in value:
        text = _text(item, reason)
        if text in result:
            raise AshareEvidenceContractError(reason)
        result.append(text)
    if nonempty and not result:
        raise AshareEvidenceContractError(reason)
    return tuple(result)


def _active_catalog_row(row: Mapping[str, Any]) -> bool:
    availability = row.get("availability")
    return bool(
        isinstance(availability, Mapping)
        and availability.get("activation_states") == ["active"]
    )


def _fresh(value: Mapping[str, Any]) -> bool:
    state = value.get("state")
    return (
        isinstance(state, str)
        and state.strip().lower() == "fresh"
        and value.get("stale") is False
    )


def _valid_quality(value: Mapping[str, Any]) -> bool:
    state = value.get("state")
    return isinstance(state, str) and state.strip().lower() == "valid"


def _complete_lineage(value: Mapping[str, Any] | None) -> bool:
    providers = value.get("providers") if isinstance(value, Mapping) else None
    return bool(
        isinstance(value, Mapping)
        and value.get("complete") is True
        and value.get("provider_neutral") is True
        and isinstance(providers, list)
        and bool(providers)
        and all(
            isinstance(provider, str)
            and bool(provider)
            and provider == provider.strip()
            for provider in providers
        )
        and len(providers) == len(set(providers))
        and isinstance(value.get("transport_service"), str)
        and bool(value.get("transport_service"))
        and value.get("transport_service") == value.get("transport_service").strip()
    )


def _mainboard_symbol_allowlist(
    value: tuple[str, ...] | None,
) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, tuple) or not value:
        raise AshareEvidenceContractError("ashare_evidence_allowed_symbols_invalid")
    normalized: list[str] = []
    for item in value:
        symbol = _text(
            item,
            "ashare_evidence_allowed_symbols_invalid",
        ).upper()
        eligibility = classify_instrument(
            symbol,
            instrument_type="common_stock",
        )
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != symbol
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_allowed_symbol_outside_mainboard_scope"
            )
        normalized.append(symbol)
    if len(normalized) != len(set(normalized)):
        raise AshareEvidenceContractError("ashare_evidence_allowed_symbols_duplicate")
    return frozenset(normalized)


def _catalog_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": row.get("dataset_id"),
        "schema_major": row.get("schema_major"),
        "default_fields": row.get("default_fields"),
        "default_order": row.get("default_order"),
        "filter_operators": row.get("filter_operators"),
        "limits": row.get("limits"),
        "availability": row.get("availability"),
    }


@dataclass(frozen=True)
class _DatasetSpec:
    identity_candidates: tuple[tuple[str, ...], ...]
    event_time_candidates: tuple[str, ...]
    symbol_candidates: tuple[str, ...] = ("ts_code", "symbol")
    entity_candidates: tuple[str, ...] = ("entity", "name")
    title_candidates: tuple[str, ...] = ("title",)
    content_candidates: tuple[str, ...] = ("content",)
    url_candidates: tuple[str, ...] = ("url",)
    source_candidates: tuple[str, ...] = ("source",)
    default_entity: str | None = None
    naive_datetime_timezone: str | None = None


_GENERIC_IDENTITY = (("event_id",),)
_DATASET_SPECS: Mapping[str, _DatasetSpec] = MappingProxyType(
    {
        "cn.dataset.anns_d": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY + (("ann_date", "ts_code", "url"),),
            event_time_candidates=("event_time", "ann_date", "rec_time"),
            naive_datetime_timezone="Asia/Shanghai",
        ),
        "cn.dataset.cctv_news": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY + (("date", "title"),),
            event_time_candidates=("event_time", "date"),
            symbol_candidates=("ts_code", "symbol"),
            default_entity="CN-MACRO",
        ),
        "cn.dataset.irm_qa_sh": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY + (("ts_code", "pub_time", "q"),),
            event_time_candidates=("event_time", "pub_time", "trade_date"),
            title_candidates=("title", "q", "question"),
            content_candidates=("content", "a", "answer"),
            naive_datetime_timezone="Asia/Shanghai",
        ),
        "cn.dataset.irm_qa_sz": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY + (("ts_code", "pub_time", "q"),),
            event_time_candidates=("event_time", "pub_time", "trade_date"),
            title_candidates=("title", "q", "question"),
            content_candidates=("content", "a", "answer"),
            naive_datetime_timezone="Asia/Shanghai",
        ),
        "cn.dataset.research_report": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY
            + (
                ("trade_date", "url"),
                ("trade_date", "ts_code", "inst_csname", "title"),
            ),
            event_time_candidates=("event_time", "trade_date"),
            source_candidates=("source", "inst_csname"),
        ),
        "cn.dataset.disclosure_date": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY
            + (("ts_code", "end_date", "ann_date"),),
            event_time_candidates=(
                "event_time",
                "actual_date",
                "ann_date",
                "pre_date",
            ),
            title_candidates=("title", "name"),
        ),
        "cn.dataset.report_rc": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY
            + (("ts_code", "report_date", "report_title", "org_name"),),
            event_time_candidates=("event_time", "report_date"),
            title_candidates=("title", "report_title"),
            source_candidates=("source", "org_name"),
        ),
        "cn.dataset.broker_recommend": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY + (("month", "broker", "ts_code"),),
            event_time_candidates=("event_time", "month"),
            title_candidates=("title", "name"),
            source_candidates=("source", "broker"),
        ),
        "cn.dataset.stk_surv": _DatasetSpec(
            identity_candidates=_GENERIC_IDENTITY
            + (("ts_code", "surv_date", "rece_org"),),
            event_time_candidates=("event_time", "surv_date"),
            content_candidates=("content",),
            source_candidates=("source", "rece_org"),
        ),
    }
)


def _first_field(
    fields: frozenset[str],
    candidates: tuple[str, ...],
) -> str | None:
    return next((candidate for candidate in candidates if candidate in fields), None)


def _identity_fields(
    fields: frozenset[str],
    candidates: tuple[tuple[str, ...], ...],
) -> tuple[str, ...] | None:
    return next(
        (candidate for candidate in candidates if set(candidate).issubset(fields)),
        None,
    )


@dataclass(frozen=True)
class EvidenceDatasetProfile:
    """A TA-owned interpretation frozen from one exact active catalog row."""

    catalog_version: str
    dataset_id: str
    schema_major: int
    default_fields: tuple[str, ...]
    default_order: tuple[str, ...]
    filter_operators: tuple[tuple[str, tuple[str, ...]], ...]
    catalog_contract_sha256: str
    identity_fields: tuple[str, ...]
    event_time_field: str
    symbol_field: str | None
    entity_field: str | None
    title_field: str | None
    content_field: str | None
    url_field: str | None
    source_field: str | None
    default_entity: str | None
    optional_dataset: bool
    max_pages: int
    max_rows: int
    page_limit: int
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE

    def __post_init__(self) -> None:
        for field_name in (
            "catalog_version",
            "dataset_id",
            "event_time_field",
        ):
            _text(
                getattr(self, field_name),
                f"ashare_evidence_profile_{field_name}_invalid",
            )
        if self.dataset_id not in _DATASET_SPECS:
            raise AshareEvidenceContractError("ashare_evidence_dataset_not_allowlisted")
        if self.dataset_id in PAUSED_DATASET_IDS:
            raise AshareEvidenceContractError(
                "ashare_evidence_paused_dataset_forbidden"
            )
        if self.catalog_route != FIXED_CATALOG_ROUTE:
            raise AshareEvidenceContractError("ashare_evidence_catalog_route_invalid")
        if self.query_route != FIXED_QUERY_ROUTE:
            raise AshareEvidenceContractError("ashare_evidence_query_route_invalid")
        if (
            isinstance(self.schema_major, bool)
            or not isinstance(self.schema_major, int)
            or self.schema_major <= 0
        ):
            raise AshareEvidenceContractError("ashare_evidence_schema_major_invalid")
        if type(self.optional_dataset) is not bool:
            raise AshareEvidenceContractError("ashare_evidence_optional_flag_invalid")
        _sha256_text(
            self.catalog_contract_sha256,
            "ashare_evidence_catalog_contract_sha256_invalid",
        )
        fields = set(self.default_fields)
        if not set(self.identity_fields).issubset(fields):
            raise AshareEvidenceContractError("ashare_evidence_identity_fields_missing")
        if self.event_time_field not in fields:
            raise AshareEvidenceContractError(
                "ashare_evidence_event_time_field_missing"
            )
        for field_name in (
            "symbol_field",
            "entity_field",
            "title_field",
            "content_field",
            "url_field",
            "source_field",
        ):
            value = getattr(self, field_name)
            if value is not None and value not in fields:
                raise AshareEvidenceContractError(
                    f"ashare_evidence_{field_name}_missing"
                )
        if self.title_field is None and self.content_field is None:
            raise AshareEvidenceContractError("ashare_evidence_text_fields_missing")
        if self.symbol_field is None and self.entity_field is None:
            _text(
                self.default_entity,
                "ashare_evidence_entity_contract_missing",
            )
        for field_name in ("max_pages", "max_rows", "page_limit"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AshareEvidenceContractError(
                    f"ashare_evidence_{field_name}_invalid"
                )
        if self.page_limit > self.max_rows:
            raise AshareEvidenceContractError(
                "ashare_evidence_page_limit_above_row_budget"
            )

    @classmethod
    def from_catalog_row(
        cls,
        catalog: CatalogEnvelope,
        row: Mapping[str, Any],
    ) -> "EvidenceDatasetProfile":
        dataset_id = _text(
            row.get("dataset_id"),
            "ashare_evidence_catalog_dataset_id_invalid",
        )
        spec = _DATASET_SPECS.get(dataset_id)
        if spec is None or dataset_id in PAUSED_DATASET_IDS:
            raise AshareEvidenceContractError("ashare_evidence_dataset_not_allowlisted")
        if not _active_catalog_row(row):
            raise AshareEvidenceContractError("ashare_evidence_dataset_not_active")
        schema_major = row.get("schema_major")
        if (
            isinstance(schema_major, bool)
            or not isinstance(schema_major, int)
            or schema_major <= 0
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_catalog_schema_major_invalid"
            )
        default_fields = _strings(
            row.get("default_fields"),
            "ashare_evidence_catalog_default_fields_invalid",
        )
        default_order = _strings(
            row.get("default_order", []),
            "ashare_evidence_catalog_default_order_invalid",
            nonempty=False,
        )
        fields = frozenset(default_fields)
        identity_fields = _identity_fields(fields, spec.identity_candidates)
        event_time_field = _first_field(fields, spec.event_time_candidates)
        if identity_fields is None:
            raise AshareEvidenceContractError(
                "ashare_evidence_catalog_identity_missing"
            )
        if event_time_field is None:
            raise AshareEvidenceContractError(
                "ashare_evidence_catalog_event_time_missing"
            )
        raw_filter_operators = row.get("filter_operators")
        if not isinstance(raw_filter_operators, Mapping):
            raise AshareEvidenceContractError(
                "ashare_evidence_catalog_filter_operators_invalid"
            )
        filter_operators: list[tuple[str, tuple[str, ...]]] = []
        for raw_field_name in sorted(raw_filter_operators):
            field_name = _text(
                raw_field_name,
                "ashare_evidence_catalog_filter_operators_invalid",
            )
            filter_operators.append(
                (
                    field_name,
                    _strings(
                        raw_filter_operators[raw_field_name],
                        "ashare_evidence_catalog_filter_operators_invalid",
                    ),
                )
            )
        limits = row.get("limits")
        max_page_size = (
            limits.get("max_page_size") if isinstance(limits, Mapping) else None
        )
        if (
            isinstance(max_page_size, bool)
            or not isinstance(max_page_size, int)
            or max_page_size <= 0
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_catalog_page_limit_invalid"
            )
        max_pages = 16
        return cls(
            catalog_version=catalog.catalog_version,
            dataset_id=dataset_id,
            schema_major=schema_major,
            default_fields=default_fields,
            default_order=default_order,
            filter_operators=tuple(filter_operators),
            catalog_contract_sha256=_sha256(_catalog_contract(row)),
            identity_fields=identity_fields,
            event_time_field=event_time_field,
            symbol_field=_first_field(fields, spec.symbol_candidates),
            entity_field=_first_field(fields, spec.entity_candidates),
            title_field=_first_field(fields, spec.title_candidates),
            content_field=_first_field(fields, spec.content_candidates),
            url_field=_first_field(fields, spec.url_candidates),
            source_field=_first_field(fields, spec.source_candidates),
            default_entity=spec.default_entity,
            optional_dataset=dataset_id in OPTIONAL_DATASET_IDS,
            max_pages=max_pages,
            max_rows=max_page_size * max_pages,
            page_limit=max_page_size,
        )


@dataclass(frozen=True)
class EvidenceProfileSet:
    catalog_version: str
    by_dataset: Mapping[str, EvidenceDatasetProfile]
    missing_optional: tuple[str, ...]
    catalog_contract_sha256: str
    catalog_route: str = FIXED_CATALOG_ROUTE
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        _text(
            self.catalog_version,
            "ashare_evidence_profile_set_catalog_invalid",
        )
        _sha256_text(
            self.catalog_contract_sha256,
            "ashare_evidence_profile_set_sha256_invalid",
        )
        profiles = dict(self.by_dataset)
        if set(PRIMARY_DATASET_IDS).difference(profiles):
            raise AshareEvidenceContractError("ashare_evidence_primary_profile_missing")
        if set(profiles).intersection(PAUSED_DATASET_IDS):
            raise AshareEvidenceContractError(
                "ashare_evidence_paused_dataset_forbidden"
            )
        if any(
            dataset_id != profile.dataset_id
            or profile.catalog_version != self.catalog_version
            for dataset_id, profile in profiles.items()
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_profile_set_binding_invalid"
            )
        expected_missing = tuple(sorted(set(OPTIONAL_DATASET_IDS).difference(profiles)))
        if self.missing_optional != expected_missing:
            raise AshareEvidenceContractError(
                "ashare_evidence_optional_profile_state_invalid"
            )
        if self.catalog_route != FIXED_CATALOG_ROUTE or any(
            (
                self.candidate_eligible,
                self.execution_eligible,
                self.training_eligible,
                self.promotion_eligible,
                self.real_trading_enabled,
            )
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_profile_set_authority_invalid"
            )
        object.__setattr__(self, "by_dataset", MappingProxyType(profiles))

    @property
    def complete_optional_coverage(self) -> bool:
        return not self.missing_optional


@dataclass(frozen=True)
class AshareEvidenceAuditRecord:
    """Audit-only rejection record; never usable for a model or decision."""

    reason_code: str
    dataset_id: str
    catalog_version: str
    decision_time: datetime
    rejected_payload_sha256: str
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        _text(self.reason_code, "ashare_evidence_audit_reason_invalid")
        _text(self.dataset_id, "ashare_evidence_audit_dataset_invalid")
        _text(self.catalog_version, "ashare_evidence_audit_catalog_invalid")
        _aware(
            self.decision_time,
            "ashare_evidence_audit_decision_time_invalid",
        )
        _sha256_text(
            self.rejected_payload_sha256,
            "ashare_evidence_audit_payload_sha256_invalid",
        )
        if any(
            (
                self.candidate_eligible,
                self.execution_eligible,
                self.training_eligible,
                self.promotion_eligible,
                self.real_trading_enabled,
            )
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_rejection_must_be_audit_only"
            )


class AshareEvidenceAuditLedger:
    """Idempotent process-local collector for rejected evidence."""

    def __init__(self) -> None:
        self._records: dict[str, AshareEvidenceAuditRecord] = {}

    def append(self, record: AshareEvidenceAuditRecord) -> bool:
        if not isinstance(record, AshareEvidenceAuditRecord):
            raise AshareEvidenceContractError("ashare_evidence_audit_record_invalid")
        identity = _sha256(
            {
                "reason_code": record.reason_code,
                "dataset_id": record.dataset_id,
                "catalog_version": record.catalog_version,
                "decision_time": record.decision_time.astimezone(
                    timezone.utc
                ).isoformat(),
                "rejected_payload_sha256": record.rejected_payload_sha256,
            }
        )
        previous = self._records.get(identity)
        if previous is None:
            self._records[identity] = record
            return True
        if previous == record:
            return False
        raise AshareEvidenceContractError("ashare_evidence_audit_identity_conflict")

    def records(self) -> tuple[AshareEvidenceAuditRecord, ...]:
        return tuple(self._records.values())


@dataclass(frozen=True)
class EventEvidenceSnapshot:
    """One catalog- and envelope-bound A-share event observation."""

    dataset_id: str
    catalog_version: str
    event_time: str
    event_time_precision: str
    available_at: datetime
    available_at_source: str
    entity: str
    symbol: str | None
    title: str | None
    content: str | None
    url: str | None
    source: str
    receipt_id: str
    source_lineage_sha256: str
    source_row_sha256: str
    envelope_proof_sha256: str
    evidence_ref: str
    evidence_confidence: float
    event_time_instant_proven: bool
    historical_known_time_proven: bool
    pit_feature_eligible: bool
    confidence_semantics: str = "deterministic_evidence_completeness_not_probability"
    calibrated_probability: None = None
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    execution_authority: bool = False
    risk_authority: bool = False
    position_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "dataset_id",
            "catalog_version",
            "event_time",
            "event_time_precision",
            "available_at_source",
            "entity",
            "source",
            "receipt_id",
            "evidence_ref",
            "confidence_semantics",
        ):
            _text(
                getattr(self, field_name),
                f"ashare_evidence_event_{field_name}_invalid",
            )
        if (
            self.dataset_id not in _DATASET_SPECS
            or self.dataset_id in PAUSED_DATASET_IDS
        ):
            raise AshareEvidenceContractError("ashare_evidence_event_dataset_invalid")
        _aware(
            self.available_at,
            "ashare_evidence_event_available_at_invalid",
        )
        for field_name in (
            "source_lineage_sha256",
            "source_row_sha256",
            "envelope_proof_sha256",
        ):
            _sha256_text(
                getattr(self, field_name),
                f"ashare_evidence_event_{field_name}_invalid",
            )
        parsed_event_time, parsed_precision, parsed_known_time = _event_time(
            self.event_time,
            available_at=self.available_at,
        )
        if (
            parsed_event_time != self.event_time
            or parsed_precision != self.event_time_precision
            or parsed_known_time is not self.event_time_instant_proven
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_event_time_precision_invalid"
            )
        if self.historical_known_time_proven is not False:
            raise AshareEvidenceContractError(
                "ashare_evidence_historical_known_time_unproven"
            )
        if self.available_at_source != "query_envelope.metadata.observed_at":
            raise AshareEvidenceContractError(
                "ashare_evidence_available_at_source_invalid"
            )
        expected_ref_prefix = f"td-v1:{self.dataset_id}:{self.receipt_id}:"
        if (
            not self.evidence_ref.startswith(expected_ref_prefix)
            or self.evidence_ref[len(expected_ref_prefix) :]
            != self.source_row_sha256[:16]
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_reference_binding_invalid"
            )
        if self.symbol is not None:
            eligibility = classify_instrument(
                self.symbol,
                instrument_type="common_stock",
            )
            if (
                not eligibility.order_identity_allowed
                or eligibility.normalized_symbol != self.symbol
            ):
                raise AshareEvidenceContractError(
                    "ashare_evidence_symbol_outside_mainboard_scope"
                )
        if self.title is None and self.content is None:
            raise AshareEvidenceContractError("ashare_evidence_event_text_missing")
        for field_name in ("title", "content", "url"):
            value = getattr(self, field_name)
            if value is not None:
                _text(
                    value,
                    f"ashare_evidence_event_{field_name}_invalid",
                )
        if (
            isinstance(self.evidence_confidence, bool)
            or not isinstance(self.evidence_confidence, (int, float))
            or not math.isfinite(float(self.evidence_confidence))
            or not 0 <= float(self.evidence_confidence) <= 1
        ):
            raise AshareEvidenceContractError("ashare_evidence_confidence_invalid")
        if self.pit_feature_eligible is not False:
            raise AshareEvidenceContractError(
                "ashare_evidence_pit_feature_state_invalid"
            )
        if self.calibrated_probability is not None or any(
            (
                self.candidate_eligible,
                self.execution_eligible,
                self.training_eligible,
                self.promotion_eligible,
                self.execution_authority,
                self.risk_authority,
                self.position_authority,
                self.real_trading_enabled,
            )
        ):
            raise AshareEvidenceContractError("ashare_evidence_event_authority_invalid")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "catalog_version": self.catalog_version,
            "event_time": self.event_time,
            "event_time_precision": self.event_time_precision,
            "available_at": self.available_at.isoformat(),
            "available_at_source": self.available_at_source,
            "entity": self.entity,
            "symbol": self.symbol,
            "title": self.title,
            "content": self.content,
            "url": self.url,
            "source": self.source,
            "receipt_id": self.receipt_id,
            "source_lineage_sha256": self.source_lineage_sha256,
            "source_row_sha256": self.source_row_sha256,
            "envelope_proof_sha256": self.envelope_proof_sha256,
            "evidence_ref": self.evidence_ref,
            "evidence_confidence": self.evidence_confidence,
            "confidence_semantics": self.confidence_semantics,
            "calibrated_probability": self.calibrated_probability,
            "event_time_instant_proven": self.event_time_instant_proven,
            "historical_known_time_proven": self.historical_known_time_proven,
            "pit_feature_eligible": self.pit_feature_eligible,
            "candidate_eligible": self.candidate_eligible,
            "execution_eligible": self.execution_eligible,
            "training_eligible": self.training_eligible,
            "promotion_eligible": self.promotion_eligible,
            "execution_authority": self.execution_authority,
            "risk_authority": self.risk_authority,
            "position_authority": self.position_authority,
            "real_trading_enabled": self.real_trading_enabled,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_payload())


@dataclass(frozen=True)
class EventEvidenceSnapshotBatch:
    profile: EvidenceDatasetProfile
    events: tuple[EventEvidenceSnapshot, ...]
    page_count: int
    row_count: int
    pagination_trace_sha256: str
    first_semantic_sha256: str
    replay_semantic_sha256: str
    same_observation: bool
    query_route: str = FIXED_QUERY_ROUTE
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.profile, EvidenceDatasetProfile):
            raise AshareEvidenceContractError(
                "ashare_evidence_snapshot_profile_invalid"
            )
        if not self.events or self.row_count != len(self.events):
            raise AshareEvidenceContractError(
                "ashare_evidence_snapshot_row_count_invalid"
            )
        if not 1 <= self.page_count <= self.profile.max_pages:
            raise AshareEvidenceContractError(
                "ashare_evidence_snapshot_page_count_invalid"
            )
        for field_name in (
            "pagination_trace_sha256",
            "first_semantic_sha256",
            "replay_semantic_sha256",
        ):
            _sha256_text(
                getattr(self, field_name),
                f"ashare_evidence_snapshot_{field_name}_invalid",
            )
        if (
            self.same_observation is not True
            or self.first_semantic_sha256 != self.replay_semantic_sha256
            or self.query_route != FIXED_QUERY_ROUTE
            or any(
                (
                    self.candidate_eligible,
                    self.execution_eligible,
                    self.training_eligible,
                    self.promotion_eligible,
                    self.real_trading_enabled,
                )
            )
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_snapshot_authority_or_replay_invalid"
            )


def _event_time(
    raw: object,
    *,
    available_at: datetime,
) -> tuple[str, str, bool]:
    text = _text(raw, "ashare_evidence_event_time_missing")
    if len(text) == 8 and text.isdigit():
        try:
            parsed_date = datetime.strptime(text, "%Y%m%d").date()
        except ValueError as exc:
            raise AshareEvidenceContractError(
                "ashare_evidence_event_time_invalid"
            ) from exc
        if parsed_date > available_at.astimezone(SHANGHAI).date():
            raise AshareEvidenceContractError(
                "ashare_evidence_event_time_after_availability"
            )
        return text, "date", False
    if len(text) == 10:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            parsed_date = None
        if parsed_date is not None:
            if parsed_date > available_at.astimezone(SHANGHAI).date():
                raise AshareEvidenceContractError(
                    "ashare_evidence_event_time_after_availability"
                )
            return text, "date", False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AshareEvidenceContractError("ashare_evidence_event_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AshareEvidenceContractError("ashare_evidence_event_time_timezone_missing")
    if parsed > available_at:
        raise AshareEvidenceContractError(
            "ashare_evidence_event_time_after_availability"
        )
    return text, "instant", True


def _normalize_provider_event_time(
    raw: object,
    *,
    profile: EvidenceDatasetProfile,
) -> object:
    spec = _DATASET_SPECS[profile.dataset_id]
    if spec.naive_datetime_timezone is None or not isinstance(raw, str):
        return raw
    text = raw.strip()
    if len(text) <= 10:
        return raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return raw
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return raw
    return parsed.replace(tzinfo=ZoneInfo(spec.naive_datetime_timezone)).isoformat()


def _map_run(
    *,
    profile: EvidenceDatasetProfile,
    run: PagedQueryRun,
    decision_time: datetime,
    allowed_symbols: frozenset[str] | None = None,
) -> tuple[EventEvidenceSnapshot, ...]:
    run.verify_integrity(identity_fields=profile.identity_fields)
    envelope = run.envelope
    metadata = envelope.metadata
    if (
        envelope.dataset_id != profile.dataset_id
        or envelope.catalog_version != profile.catalog_version
    ):
        raise AshareEvidenceContractError("ashare_evidence_query_binding_mismatch")
    if metadata.state.strip().lower() != "ready" or metadata.degraded is not False:
        raise AshareEvidenceContractError("ashare_evidence_metadata_not_ready")
    if not _fresh(metadata.freshness):
        raise AshareEvidenceContractError("ashare_evidence_metadata_not_fresh")
    if not _valid_quality(metadata.quality):
        raise AshareEvidenceContractError("ashare_evidence_metadata_quality_invalid")
    if not _complete_lineage(metadata.lineage):
        raise AshareEvidenceContractError("ashare_evidence_metadata_lineage_incomplete")
    if not all(
        isinstance(value, str) and bool(value) and value == value.strip()
        for value in (
            metadata.receipt_id,
            metadata.data_through,
            metadata.observed_at,
        )
    ):
        raise AshareEvidenceContractError("ashare_evidence_metadata_proof_incomplete")
    decision = _aware(
        decision_time,
        "ashare_evidence_decision_time_timezone_required",
    )
    data_through = _parse_aware_iso(
        metadata.data_through,
        "ashare_evidence_data_through_invalid",
    )
    observed = _parse_aware_iso(
        metadata.observed_at,
        "ashare_evidence_observed_at_invalid",
    )
    if data_through > observed:
        raise AshareEvidenceContractError("ashare_evidence_metadata_time_order_invalid")
    if observed > decision:
        raise AshareEvidenceContractError("ashare_evidence_available_after_decision")
    assert metadata.lineage is not None
    lineage_sha = _sha256(metadata.lineage)
    envelope_proof_sha = _sha256(
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
    )
    events: list[EventEvidenceSnapshot] = []
    if allowed_symbols is not None and profile.symbol_field is None:
        raise AshareEvidenceContractError(
            "ashare_evidence_allowed_symbols_require_symbol_field"
        )
    for row in envelope.data:
        symbol: str | None = None
        if profile.symbol_field is not None:
            raw_symbol = row.get(profile.symbol_field)
            if raw_symbol not in (None, ""):
                raw_symbol_text = _text(
                    raw_symbol,
                    "ashare_evidence_symbol_invalid",
                ).upper()
                if (
                    allowed_symbols is not None
                    and raw_symbol_text not in allowed_symbols
                ):
                    continue
                eligibility = classify_instrument(
                    raw_symbol_text,
                    instrument_type="common_stock",
                )
                if not eligibility.order_identity_allowed:
                    raise AshareEvidenceContractError(
                        "ashare_evidence_symbol_outside_mainboard_scope"
                    )
                symbol = eligibility.normalized_symbol
            elif allowed_symbols is not None:
                continue
        event_time, precision, known_time_proven = _event_time(
            _normalize_provider_event_time(
                row.get(profile.event_time_field),
                profile=profile,
            ),
            available_at=observed,
        )
        raw_entity = (
            row.get(profile.entity_field) if profile.entity_field is not None else None
        )
        entity = (
            _optional_text(
                raw_entity,
                "ashare_evidence_entity_invalid",
            )
            or profile.default_entity
            or symbol
        )
        entity = _text(entity, "ashare_evidence_entity_missing")
        title = (
            _optional_text(
                row.get(profile.title_field),
                "ashare_evidence_title_invalid",
            )
            if profile.title_field is not None
            else None
        )
        content = (
            _optional_text(
                row.get(profile.content_field),
                "ashare_evidence_content_invalid",
            )
            if profile.content_field is not None
            else None
        )
        if title is None and content is None:
            raise AshareEvidenceContractError("ashare_evidence_event_text_missing")
        url = (
            _optional_text(
                row.get(profile.url_field),
                "ashare_evidence_url_invalid",
            )
            if profile.url_field is not None
            else None
        )
        source = (
            _optional_text(
                row.get(profile.source_field),
                "ashare_evidence_source_invalid",
            )
            if profile.source_field is not None
            else None
        ) or profile.dataset_id
        row_sha = _sha256(row)
        confidence = 1.0
        if content is None:
            confidence -= 0.15
        if title is None:
            confidence -= 0.1
        if url is None:
            confidence -= 0.05
        event = EventEvidenceSnapshot(
            dataset_id=profile.dataset_id,
            catalog_version=profile.catalog_version,
            event_time=event_time,
            event_time_precision=precision,
            available_at=observed,
            available_at_source="query_envelope.metadata.observed_at",
            entity=entity,
            symbol=symbol,
            title=title,
            content=content,
            url=url,
            source=source,
            receipt_id=str(metadata.receipt_id),
            source_lineage_sha256=lineage_sha,
            source_row_sha256=row_sha,
            envelope_proof_sha256=envelope_proof_sha,
            evidence_ref=(
                f"td-v1:{profile.dataset_id}:{metadata.receipt_id}:{row_sha[:16]}"
            ),
            evidence_confidence=round(confidence, 6),
            event_time_instant_proven=known_time_proven,
            historical_known_time_proven=False,
            pit_feature_eligible=False,
        )
        events.append(event)
    if not events:
        raise AshareEvidenceContractError("ashare_evidence_query_returned_no_rows")
    return tuple(events)


def snapshot_from_runs(
    *,
    profile: EvidenceDatasetProfile,
    first: PagedQueryRun,
    replay: PagedQueryRun,
    decision_time: datetime,
    audit_ledger: AshareEvidenceAuditLedger,
    allowed_symbols: tuple[str, ...] | None = None,
) -> EventEvidenceSnapshotBatch:
    rejected_payload = {
        "dataset_id": profile.dataset_id,
        "catalog_version": profile.catalog_version,
        "first_semantic_sha256": getattr(first, "semantic_sha256", None),
        "replay_semantic_sha256": getattr(replay, "semantic_sha256", None),
    }
    try:
        allowlist = _mainboard_symbol_allowlist(allowed_symbols)
        if (
            first.semantic_sha256 != replay.semantic_sha256
            or first.semantic_trace_sha256 != replay.semantic_trace_sha256
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_same_observation_mismatch"
            )
        events = _map_run(
            profile=profile,
            run=first,
            decision_time=decision_time,
            allowed_symbols=allowlist,
        )
        replay_events = _map_run(
            profile=profile,
            run=replay,
            decision_time=decision_time,
            allowed_symbols=allowlist,
        )
        if [event.sha256 for event in events] != [
            event.sha256 for event in replay_events
        ]:
            raise AshareEvidenceContractError(
                "ashare_evidence_same_observation_mismatch"
            )
        return EventEvidenceSnapshotBatch(
            profile=profile,
            events=events,
            page_count=first.page_count,
            row_count=len(events),
            pagination_trace_sha256=first.pagination_trace_sha256,
            first_semantic_sha256=first.semantic_sha256,
            replay_semantic_sha256=replay.semantic_sha256,
            same_observation=True,
        )
    except AshareEvidenceContractError as exc:
        audit_ledger.append(
            AshareEvidenceAuditRecord(
                reason_code=exc.reason_code,
                dataset_id=profile.dataset_id,
                catalog_version=profile.catalog_version,
                decision_time=_aware(
                    decision_time,
                    "ashare_evidence_decision_time_timezone_required",
                ),
                rejected_payload_sha256=_sha256(rejected_payload),
            )
        )
        raise


class AshareEventEvidencePort(Protocol):
    def freeze_profiles(
        self,
        *,
        audit_ledger: AshareEvidenceAuditLedger,
    ) -> EvidenceProfileSet: ...

    def load_event_snapshot(
        self,
        *,
        profile: EvidenceDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        audit_ledger: AshareEvidenceAuditLedger,
        allowed_symbols: tuple[str, ...] | None = None,
    ) -> EventEvidenceSnapshotBatch: ...


class TradingDatasAshareEvidencePort:
    """Injected-client adapter for the two fixed TradingDatas V1 routes."""

    def __init__(self, client: SharedSignalsV1Client) -> None:
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        self._client = client

    def freeze_profiles(
        self,
        *,
        audit_ledger: AshareEvidenceAuditLedger,
    ) -> EvidenceProfileSet:
        if not isinstance(audit_ledger, AshareEvidenceAuditLedger):
            raise TypeError("audit_ledger must be AshareEvidenceAuditLedger")
        decision_time = datetime.now(timezone.utc)
        try:
            catalog = self._client.get_catalog()
            rows = {
                row.get("dataset_id"): row
                for row in catalog.data
                if row.get("dataset_id") not in PAUSED_DATASET_IDS
            }
            missing_primary = set(PRIMARY_DATASET_IDS).difference(rows)
            if missing_primary:
                raise AshareEvidenceContractError(
                    "ashare_evidence_primary_profile_missing"
                )
            profiles: dict[str, EvidenceDatasetProfile] = {}
            for dataset_id in (*PRIMARY_DATASET_IDS, *OPTIONAL_DATASET_IDS):
                row = rows.get(dataset_id)
                if row is None or dataset_id not in self._client.config.dataset_ids:
                    continue
                profiles[dataset_id] = EvidenceDatasetProfile.from_catalog_row(
                    catalog,
                    row,
                )
            return EvidenceProfileSet(
                catalog_version=catalog.catalog_version,
                by_dataset=profiles,
                missing_optional=tuple(
                    sorted(set(OPTIONAL_DATASET_IDS).difference(profiles))
                ),
                catalog_contract_sha256=_sha256(
                    {
                        "catalog_version": catalog.catalog_version,
                        "profiles": {
                            dataset_id: profile.catalog_contract_sha256
                            for dataset_id, profile in sorted(profiles.items())
                        },
                        "paused_fallbacks": list(PAUSED_DATASET_IDS),
                    }
                ),
            )
        except AshareEvidenceContractError as exc:
            reason = exc.reason_code
        except SharedSignalsV1Error:
            reason = "ashare_evidence_catalog_failed"
        audit_ledger.append(
            AshareEvidenceAuditRecord(
                reason_code=reason,
                dataset_id="catalog",
                catalog_version=self._client.config.expected_catalog_version,
                decision_time=decision_time,
                rejected_payload_sha256=_sha256(
                    {
                        "configured_dataset_ids": sorted(
                            self._client.config.dataset_ids
                        ),
                        "catalog_route": FIXED_CATALOG_ROUTE,
                    }
                ),
            )
        )
        raise AshareEvidenceContractError(reason)

    def load_event_snapshot(
        self,
        *,
        profile: EvidenceDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        audit_ledger: AshareEvidenceAuditLedger,
        allowed_symbols: tuple[str, ...] | None = None,
    ) -> EventEvidenceSnapshotBatch:
        if not isinstance(profile, EvidenceDatasetProfile):
            raise TypeError("profile must be EvidenceDatasetProfile")
        if not isinstance(filters, Mapping):
            raise AshareEvidenceContractError("ashare_evidence_filters_invalid")
        if not isinstance(audit_ledger, AshareEvidenceAuditLedger):
            raise TypeError("audit_ledger must be AshareEvidenceAuditLedger")
        decision = _aware(
            decision_time,
            "ashare_evidence_decision_time_timezone_required",
        )
        audit_count_before = len(audit_ledger.records())
        try:
            normalized_allowed_symbols = _mainboard_symbol_allowlist(allowed_symbols)
            catalog = self._client.get_catalog()
            if catalog.catalog_version != profile.catalog_version:
                raise AshareEvidenceContractError(
                    "ashare_evidence_catalog_version_drift"
                )
            row = next(
                (
                    item
                    for item in catalog.data
                    if item.get("dataset_id") == profile.dataset_id
                ),
                None,
            )
            if row is None or not _active_catalog_row(row):
                raise AshareEvidenceContractError("ashare_evidence_dataset_not_active")
            if _sha256(_catalog_contract(row)) != profile.catalog_contract_sha256:
                raise AshareEvidenceContractError(
                    "ashare_evidence_catalog_contract_drift"
                )
            _validate_query_filters(profile=profile, filters=filters)
            request = QueryRequest(
                dataset_id=profile.dataset_id,
                schema_major=profile.schema_major,
                fields=profile.default_fields,
                filters=dict(filters),
                as_of=decision.isoformat(),
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
            return snapshot_from_runs(
                profile=profile,
                first=first,
                replay=replay,
                decision_time=decision,
                audit_ledger=audit_ledger,
                allowed_symbols=(
                    tuple(sorted(normalized_allowed_symbols))
                    if normalized_allowed_symbols is not None
                    else None
                ),
            )
        except AshareEvidenceContractError as exc:
            reason = exc.reason_code
            source_reason = exc.reason_code
        except PaginationContractError as exc:
            reason = "ashare_evidence_pagination_failed"
            source_reason = str(exc)
        except SharedSignalsV1Error as exc:
            reason = "ashare_evidence_query_failed"
            source_reason = type(exc).__name__
        if len(audit_ledger.records()) == audit_count_before:
            audit_ledger.append(
                AshareEvidenceAuditRecord(
                    reason_code=reason,
                    dataset_id=profile.dataset_id,
                    catalog_version=profile.catalog_version,
                    decision_time=decision,
                    rejected_payload_sha256=_sha256(
                        {
                            "dataset_id": profile.dataset_id,
                            "catalog_version": profile.catalog_version,
                            "filters_sha256": _sha256(dict(filters)),
                            "source_reason": source_reason,
                        }
                    ),
                )
            )
        raise AshareEvidenceContractError(reason)


def _validate_query_filters(
    *,
    profile: EvidenceDatasetProfile,
    filters: Mapping[str, Any],
) -> None:
    allowed = dict(profile.filter_operators)
    for raw_field_name, raw_expression in filters.items():
        field_name = _text(
            raw_field_name,
            "ashare_evidence_filter_field_invalid",
        )
        operators = allowed.get(field_name)
        if operators is None:
            raise AshareEvidenceContractError(
                "ashare_evidence_filter_not_catalog_allowed"
            )
        if not isinstance(raw_expression, Mapping) or len(raw_expression) != 1:
            raise AshareEvidenceContractError(
                "ashare_evidence_filter_expression_invalid"
            )
        raw_operator = next(iter(raw_expression))
        operator = _text(
            raw_operator,
            "ashare_evidence_filter_operator_invalid",
        )
        if operator not in operators:
            raise AshareEvidenceContractError(
                "ashare_evidence_filter_operator_not_catalog_allowed"
            )


_POSITIVE_TERMS = (
    "上调",
    "中标",
    "业绩增长",
    "回购",
    "增持",
    "改善",
    "盈利",
    "签署",
    "订单增长",
)
_NEGATIVE_TERMS = (
    "下调",
    "亏损",
    "处罚",
    "违约",
    "调查",
    "减持",
    "终止",
    "重大风险",
)


@dataclass(frozen=True)
class SentimentEvidenceSnapshot:
    """Deterministic, uncalibrated shadow aggregation of accepted events."""

    decision_time: datetime
    entity: str
    symbol: str | None
    evidence_refs: tuple[str, ...]
    covered_dataset_ids: tuple[str, ...]
    missing_dataset_ids: tuple[str, ...]
    raw_shadow_score: float
    coverage_weight: float
    shadow_score: float
    baseline_score: float
    event_counterfactual_delta: float
    score_semantics: str = "deterministic_shadow_score_not_probability"
    calibrated_probability: None = None
    counterfactual_only: bool = True
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    execution_authority: bool = False
    risk_authority: bool = False
    position_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        _aware(
            self.decision_time,
            "ashare_evidence_sentiment_decision_time_invalid",
        )
        _text(self.entity, "ashare_evidence_sentiment_entity_invalid")
        _strings(
            self.evidence_refs,
            "ashare_evidence_sentiment_refs_invalid",
        )
        for field_name in (
            "raw_shadow_score",
            "coverage_weight",
            "shadow_score",
            "baseline_score",
            "event_counterfactual_delta",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise AshareEvidenceContractError(
                    f"ashare_evidence_sentiment_{field_name}_invalid"
                )
        if not 0 < self.coverage_weight <= 1:
            raise AshareEvidenceContractError(
                "ashare_evidence_sentiment_coverage_invalid"
            )
        if (
            self.score_semantics != "deterministic_shadow_score_not_probability"
            or self.calibrated_probability is not None
            or self.counterfactual_only is not True
            or any(
                (
                    self.candidate_eligible,
                    self.execution_eligible,
                    self.training_eligible,
                    self.promotion_eligible,
                    self.execution_authority,
                    self.risk_authority,
                    self.position_authority,
                    self.real_trading_enabled,
                )
            )
        ):
            raise AshareEvidenceContractError(
                "ashare_evidence_sentiment_authority_invalid"
            )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "decision_time": self.decision_time.isoformat(),
            "entity": self.entity,
            "symbol": self.symbol,
            "evidence_refs": list(self.evidence_refs),
            "covered_dataset_ids": list(self.covered_dataset_ids),
            "missing_dataset_ids": list(self.missing_dataset_ids),
            "raw_shadow_score": self.raw_shadow_score,
            "coverage_weight": self.coverage_weight,
            "shadow_score": self.shadow_score,
            "baseline_score": self.baseline_score,
            "event_counterfactual_delta": self.event_counterfactual_delta,
            "score_semantics": self.score_semantics,
            "calibrated_probability": self.calibrated_probability,
            "counterfactual_only": self.counterfactual_only,
            "candidate_eligible": self.candidate_eligible,
            "execution_eligible": self.execution_eligible,
            "training_eligible": self.training_eligible,
            "promotion_eligible": self.promotion_eligible,
            "execution_authority": self.execution_authority,
            "risk_authority": self.risk_authority,
            "position_authority": self.position_authority,
            "real_trading_enabled": self.real_trading_enabled,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_payload())


def build_sentiment_snapshot(
    *,
    events: tuple[EventEvidenceSnapshot, ...],
    decision_time: datetime,
) -> SentimentEvidenceSnapshot:
    if not isinstance(events, tuple) or not events:
        raise AshareEvidenceContractError("ashare_evidence_sentiment_events_required")
    decision = _aware(
        decision_time,
        "ashare_evidence_sentiment_decision_time_invalid",
    )
    if any(
        not isinstance(event, EventEvidenceSnapshot) or event.available_at > decision
        for event in events
    ):
        raise AshareEvidenceContractError("ashare_evidence_sentiment_event_invalid")
    symbols = {event.symbol for event in events if event.symbol is not None}
    entities = {event.entity for event in events}
    if len(symbols) > 1 or (not symbols and len(entities) > 1):
        raise AshareEvidenceContractError("ashare_evidence_sentiment_entity_conflict")
    refs = tuple(event.evidence_ref for event in events)
    if len(refs) != len(set(refs)):
        raise AshareEvidenceContractError(
            "ashare_evidence_sentiment_duplicate_evidence"
        )
    covered = tuple(
        sorted(
            set(PRIMARY_DATASET_IDS).intersection(event.dataset_id for event in events)
        )
    )
    if not covered:
        raise AshareEvidenceContractError(
            "ashare_evidence_sentiment_primary_evidence_missing"
        )
    missing = tuple(sorted(set(PRIMARY_DATASET_IDS).difference(covered)))
    event_scores: list[float] = []
    for event in events:
        text = "\n".join(
            part for part in (event.title, event.content) if part is not None
        )
        positive = sum(text.count(term) for term in _POSITIVE_TERMS)
        negative = sum(text.count(term) for term in _NEGATIVE_TERMS)
        event_scores.append(max(-1.0, min(1.0, (positive - negative) / 4.0)))
    raw_score = sum(event_scores) / len(event_scores)
    coverage_weight = len(covered) / len(PRIMARY_DATASET_IDS)
    shadow_score = raw_score * coverage_weight
    symbol = next(iter(symbols)) if symbols else None
    entity = next(
        event.entity for event in events if symbol is None or event.symbol == symbol
    )
    return SentimentEvidenceSnapshot(
        decision_time=decision,
        entity=entity,
        symbol=symbol,
        evidence_refs=refs,
        covered_dataset_ids=covered,
        missing_dataset_ids=missing,
        raw_shadow_score=round(raw_score, 6),
        coverage_weight=round(coverage_weight, 6),
        shadow_score=round(shadow_score, 6),
        baseline_score=0.0,
        event_counterfactual_delta=round(shadow_score, 6),
    )


def bind_shadow_decision(
    *,
    ledger: InMemoryDecisionLedger,
    sentiment: SentimentEvidenceSnapshot,
    decision_id: str,
    decision_time: datetime,
) -> DecisionExposureRecord:
    """Bind accepted evidence to the existing Decision Ledger vocabulary."""

    if not isinstance(ledger, InMemoryDecisionLedger):
        raise TypeError("ledger must be InMemoryDecisionLedger")
    if not isinstance(sentiment, SentimentEvidenceSnapshot):
        raise TypeError("sentiment must be SentimentEvidenceSnapshot")
    decision = _aware(
        decision_time,
        "ashare_evidence_decision_time_timezone_required",
    )
    if decision != sentiment.decision_time:
        raise AshareEvidenceContractError(
            "ashare_evidence_decision_time_binding_mismatch"
        )
    record = DecisionExposureRecord(
        decision_id=_text(
            decision_id,
            "ashare_evidence_decision_id_invalid",
        ),
        decision_cluster_id=f"ashare-event:{sentiment.sha256[:24]}",
        decision_time=decision,
        symbol=sentiment.symbol or f"CONTEXT:{sentiment.entity}",
        model_id="ashare-deterministic-event-shadow",
        model_version="v1",
        manifest_sha256=sentiment.sha256,
        action="observe_event_evidence",
        disposition=ExposureDisposition.SHADOW_ONLY,
        requested_notional_cny=0.0,
        filled_quantity=0,
        filled_notional_cny=0.0,
        actual_cost_cny=0.0,
        simulated_fill_id=None,
        rejection_reason=None,
        nonfill_reason=None,
    )
    ledger.append(record)
    return record


def build_llm_shadow_request(
    *,
    event: EventEvidenceSnapshot,
    request_id: str,
    document_cutoff: datetime,
) -> LLMEvidenceRequest:
    """Project one instant-proven event into the existing offline LLM schema.

    This creates only an immutable request object.  It does not instantiate a
    gateway, adapter, provider transport or evidence journal, and therefore
    cannot invoke DeepSeek or any other model.  The artifact intentionally has
    no external source-authority receipt; a future formal handoff must provide
    and verify that receipt before the shared gateway can transmit it.
    """

    if not isinstance(event, EventEvidenceSnapshot):
        raise TypeError("event must be EventEvidenceSnapshot")
    cutoff = _aware(
        document_cutoff,
        "ashare_evidence_llm_cutoff_invalid",
    )
    if cutoff < event.available_at:
        raise AshareEvidenceContractError(
            "ashare_evidence_llm_cutoff_before_availability"
        )
    if (
        event.event_time_precision != "instant"
        or event.event_time_instant_proven is not True
    ):
        raise AshareEvidenceContractError("ashare_evidence_llm_event_time_not_instant")
    document = "\n\n".join(
        part for part in (event.title, event.content) if part is not None
    )
    artifact = EvidenceArtifact.create(
        document_text=document,
        published_at=_parse_aware_iso(
            event.event_time,
            "ashare_evidence_llm_event_time_invalid",
        ).isoformat(),
        available_at=event.available_at.isoformat(),
        span_start=0,
        span_end=len(document),
        entity_resolution_version="ashare-event-evidence.v1",
    )
    return LLMEvidenceRequest.create(
        request_id=_text(
            request_id,
            "ashare_evidence_llm_request_id_invalid",
        ),
        task_type="event_evidence_extraction",
        route="bulk_extraction",
        prompt_template_id="general-evidence-review",
        prompt_version="bull-bear.v1",
        document_cutoff=cutoff.isoformat(),
        evidence_refs=(artifact.artifact_id,),
        artifacts=(artifact,),
        payload={
            "entity_id": event.entity,
            "symbol": event.symbol,
            "event_type": "provider_neutral_event_evidence",
            "research_scores": {
                "event": {
                    "score": 0.0,
                    "confidence": event.evidence_confidence,
                    "state": "shadow_only",
                    "available_at": event.available_at.isoformat(),
                    "source_class": event.dataset_id,
                }
            },
        },
    )


__all__ = [
    "FIXED_CATALOG_ROUTE",
    "FIXED_QUERY_ROUTE",
    "OPTIONAL_DATASET_IDS",
    "PAUSED_DATASET_IDS",
    "PRIMARY_DATASET_IDS",
    "AshareEventEvidencePort",
    "AshareEvidenceAuditLedger",
    "AshareEvidenceAuditRecord",
    "AshareEvidenceContractError",
    "EventEvidenceSnapshot",
    "EventEvidenceSnapshotBatch",
    "EvidenceDatasetProfile",
    "EvidenceProfileSet",
    "SentimentEvidenceSnapshot",
    "TradingDatasAshareEvidencePort",
    "bind_shadow_decision",
    "build_llm_shadow_request",
    "build_sentiment_snapshot",
    "snapshot_from_runs",
]
