"""Fixture-first, catalog-bound A-share moneyflow shadow evidence.

This module consumes only an injected TradingDatas V1 client through the
fixed ``GET /v1/catalog`` and ``POST /v1/query`` routes.  Each source variant
is independent: a rejected source is recorded in a process-local audit ledger,
is never substituted by the other variant, and cannot affect the minute
baseline.  Accepted records remain zero-notional counterfactual evidence.
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
from shared.universe.policy import classify_instrument


FIXED_CATALOG_ROUTE = "GET /v1/catalog"
FIXED_QUERY_ROUTE = "POST /v1/query"
MONEYFLOW_DATASET_IDS = ("cn.dataset.moneyflow", "cn.dataset.moneyflow_ths")
SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256_HEX = frozenset("0123456789abcdef")
_SYMBOL_FIELDS = ("ts_code", "symbol")
_TIME_FIELDS = ("event_time", "trade_date", "date")
_NET_FLOW_FIELDS = (
    "net_mf_amount",
    "net_flow_amount",
    "net_amount",
    "net_inflow_amount",
)


class AshareMoneyflowEvidenceError(ValueError):
    """Fail-closed moneyflow evidence failure with a stable reason code."""

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
        raise AshareMoneyflowEvidenceError(
            "ashare_moneyflow_payload_not_canonical"
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
        raise AshareMoneyflowEvidenceError(reason)
    return value


def _sha256_text(value: object, reason: str) -> str:
    text = _text(value, reason)
    if len(text) != 64 or any(character not in _SHA256_HEX for character in text):
        raise AshareMoneyflowEvidenceError(reason)
    return text


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise AshareMoneyflowEvidenceError(reason)
    return value


def _parse_aware(value: object, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, reason).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AshareMoneyflowEvidenceError(reason) from exc
    return _aware(parsed, reason)


def _finite(value: object, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AshareMoneyflowEvidenceError(reason)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise AshareMoneyflowEvidenceError(reason) from exc
    if not math.isfinite(result):
        raise AshareMoneyflowEvidenceError(reason)
    return result


def _strings(value: object, reason: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise AshareMoneyflowEvidenceError(reason)
    result = tuple(_text(item, reason) for item in value)
    if len(set(result)) != len(result) or (nonempty and not result):
        raise AshareMoneyflowEvidenceError(reason)
    return result


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


def _active_catalog_row(row: Mapping[str, Any]) -> bool:
    availability = row.get("availability")
    return bool(
        isinstance(availability, Mapping)
        and availability.get("activation_states") == ["active"]
    )


def _first_field(fields: frozenset[str], candidates: tuple[str, ...]) -> str | None:
    return next((candidate for candidate in candidates if candidate in fields), None)


def _identity_fields(
    default_order: tuple[str, ...],
    fields: frozenset[str],
    *,
    symbol_field: str,
    source_time_field: str,
) -> tuple[str, ...]:
    """Derive pagination identity only from the catalog primary-key projection."""

    if not default_order:
        raise AshareMoneyflowEvidenceError(
            "ashare_moneyflow_catalog_default_order_missing"
        )
    identity: list[str] = []
    for term in default_order:
        if term.count(":") != 1:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_catalog_default_order_invalid"
            )
        field_name, direction = term.split(":", 1)
        if (
            not field_name
            or field_name not in fields
            or direction not in {"asc", "desc"}
            or field_name in identity
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_catalog_default_order_invalid"
            )
        identity.append(field_name)
    if symbol_field not in identity or source_time_field not in identity:
        raise AshareMoneyflowEvidenceError(
            "ashare_moneyflow_catalog_default_order_identity_incomplete"
        )
    return tuple(identity)


def _mainboard_allowlist(value: tuple[str, ...] | None) -> frozenset[str] | None:
    if value is None:
        return None
    normalized: list[str] = []
    for item in value:
        symbol = _text(item, "ashare_moneyflow_allowed_symbols_invalid").upper()
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != symbol
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_allowed_symbol_outside_mainboard_scope"
            )
        normalized.append(symbol)
    if not normalized or len(set(normalized)) != len(normalized):
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_allowed_symbols_invalid")
    return frozenset(normalized)


def _complete_lineage(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    providers = value.get("providers")
    transport_service = value.get("transport_service")
    if not isinstance(providers, list) or not providers:
        return False
    normalized_providers: list[str] = []
    for provider in providers:
        if (
            not isinstance(provider, str)
            or not provider
            or provider != provider.strip()
        ):
            return False
        normalized_providers.append(provider)
    return bool(
        value.get("complete") is True
        and value.get("provider_neutral") is True
        and len(normalized_providers) == len(set(normalized_providers))
        and isinstance(transport_service, str)
        and transport_service
        and transport_service == transport_service.strip()
    )


def _parse_source_time(
    value: object,
    *,
    available_at: datetime,
) -> tuple[str, str]:
    text = _text(value, "ashare_moneyflow_source_time_missing")
    if len(text) == 8 and text.isdigit():
        try:
            parsed_date = datetime.strptime(text, "%Y%m%d").date()
        except ValueError as exc:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_source_time_invalid"
            ) from exc
        if parsed_date > available_at.astimezone(SHANGHAI).date():
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_source_time_after_availability"
            )
        return text, "date"
    if len(text) == 10:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            parsed_date = None
        if parsed_date is not None:
            if parsed_date > available_at.astimezone(SHANGHAI).date():
                raise AshareMoneyflowEvidenceError(
                    "ashare_moneyflow_source_time_after_availability"
                )
            return text, "date"
    parsed = _parse_aware(text, "ashare_moneyflow_source_time_invalid")
    if parsed > available_at:
        raise AshareMoneyflowEvidenceError(
            "ashare_moneyflow_source_time_after_availability"
        )
    return text, "instant"


@dataclass(frozen=True)
class MoneyflowDatasetProfile:
    """One exact active catalog interpretation for one source variant."""

    catalog_version: str
    dataset_id: str
    schema_major: int
    default_fields: tuple[str, ...]
    default_order: tuple[str, ...]
    filter_operators: tuple[tuple[str, tuple[str, ...]], ...]
    catalog_contract_sha256: str
    identity_fields: tuple[str, ...]
    symbol_field: str
    source_time_field: str
    net_flow_amount_field: str
    max_pages: int
    max_rows: int
    page_limit: int
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE

    def __post_init__(self) -> None:
        _text(self.catalog_version, "ashare_moneyflow_profile_catalog_invalid")
        if self.dataset_id not in MONEYFLOW_DATASET_IDS:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_profile_dataset_invalid"
            )
        if (
            isinstance(self.schema_major, bool)
            or not isinstance(self.schema_major, int)
            or self.schema_major <= 0
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_profile_schema_invalid"
            )
        _sha256_text(
            self.catalog_contract_sha256,
            "ashare_moneyflow_profile_contract_sha_invalid",
        )
        fields = frozenset(self.default_fields)
        expected_identity = _identity_fields(
            self.default_order,
            fields,
            symbol_field=self.symbol_field,
            source_time_field=self.source_time_field,
        )
        if (
            not fields
            or self.identity_fields != expected_identity
            or any(
                field not in fields
                for field in (
                    self.symbol_field,
                    self.source_time_field,
                    self.net_flow_amount_field,
                )
            )
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_profile_fields_invalid"
            )
        if (
            self.catalog_route != FIXED_CATALOG_ROUTE
            or self.query_route != FIXED_QUERY_ROUTE
        ):
            raise AshareMoneyflowEvidenceError("ashare_moneyflow_profile_route_invalid")
        for field_name in ("max_pages", "max_rows", "page_limit"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AshareMoneyflowEvidenceError(
                    f"ashare_moneyflow_profile_{field_name}_invalid"
                )
        if self.page_limit > self.max_rows:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_profile_page_limit_invalid"
            )

    @classmethod
    def from_catalog_row(
        cls,
        catalog: CatalogEnvelope,
        row: Mapping[str, Any],
    ) -> "MoneyflowDatasetProfile":
        dataset_id = _text(
            row.get("dataset_id"), "ashare_moneyflow_catalog_dataset_id_invalid"
        )
        if dataset_id not in MONEYFLOW_DATASET_IDS:
            raise AshareMoneyflowEvidenceError("ashare_moneyflow_dataset_not_allowed")
        if not _active_catalog_row(row):
            raise AshareMoneyflowEvidenceError("ashare_moneyflow_dataset_not_active")
        schema_major = row.get("schema_major")
        if (
            isinstance(schema_major, bool)
            or not isinstance(schema_major, int)
            or schema_major <= 0
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_catalog_schema_invalid"
            )
        default_fields = _strings(
            row.get("default_fields"), "ashare_moneyflow_catalog_fields_invalid"
        )
        default_order = _strings(
            row.get("default_order", []),
            "ashare_moneyflow_catalog_order_invalid",
            nonempty=False,
        )
        fields = frozenset(default_fields)
        symbol_field = _first_field(fields, _SYMBOL_FIELDS)
        source_time_field = _first_field(fields, _TIME_FIELDS)
        net_flow_amount_field = _first_field(fields, _NET_FLOW_FIELDS)
        if (
            symbol_field is None
            or source_time_field is None
            or net_flow_amount_field is None
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_catalog_semantic_fields_missing"
            )
        identity_fields = _identity_fields(
            default_order,
            fields,
            symbol_field=symbol_field,
            source_time_field=source_time_field,
        )
        raw_operators = row.get("filter_operators")
        if not isinstance(raw_operators, Mapping):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_catalog_filter_operators_invalid"
            )
        filter_operators = tuple(
            (
                _text(name, "ashare_moneyflow_catalog_filter_operators_invalid"),
                _strings(
                    raw_operators[name],
                    "ashare_moneyflow_catalog_filter_operators_invalid",
                ),
            )
            for name in sorted(raw_operators)
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
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_catalog_page_limit_invalid"
            )
        max_pages = 16
        return cls(
            catalog_version=catalog.catalog_version,
            dataset_id=dataset_id,
            schema_major=schema_major,
            default_fields=default_fields,
            default_order=default_order,
            filter_operators=filter_operators,
            catalog_contract_sha256=_sha256(_catalog_contract(row)),
            identity_fields=identity_fields,
            symbol_field=symbol_field,
            source_time_field=source_time_field,
            net_flow_amount_field=net_flow_amount_field,
            max_pages=max_pages,
            max_rows=max_page_size * max_pages,
            page_limit=max_page_size,
        )


@dataclass(frozen=True)
class MoneyflowProfileSet:
    catalog_version: str
    by_dataset: Mapping[str, MoneyflowDatasetProfile]
    catalog_contract_sha256: str
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE
    counterfactual_only: bool = True
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    execution_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        _text(self.catalog_version, "ashare_moneyflow_profile_set_catalog_invalid")
        _sha256_text(
            self.catalog_contract_sha256,
            "ashare_moneyflow_profile_set_sha_invalid",
        )
        profiles = dict(self.by_dataset)
        if not profiles or any(
            not isinstance(profile, MoneyflowDatasetProfile)
            or dataset_id not in MONEYFLOW_DATASET_IDS
            or dataset_id != profile.dataset_id
            or profile.catalog_version != self.catalog_version
            for dataset_id, profile in profiles.items()
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_profile_set_binding_invalid"
            )
        if (
            self.catalog_route != FIXED_CATALOG_ROUTE
            or self.query_route != FIXED_QUERY_ROUTE
            or self.counterfactual_only is not True
            or any(
                (
                    self.candidate_eligible,
                    self.execution_eligible,
                    self.training_eligible,
                    self.promotion_eligible,
                    self.execution_authority,
                    self.real_trading_enabled,
                )
            )
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_profile_set_authority_invalid"
            )
        object.__setattr__(self, "by_dataset", MappingProxyType(profiles))


@dataclass(frozen=True)
class AshareMoneyflowAuditRecord:
    """Process-local audit-only rejection; it is never a scoring input."""

    reason_code: str
    dataset_id: str
    catalog_version: str
    decision_time: datetime
    rejected_payload_sha256: str
    pit_feature_eligible: bool = False
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    execution_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        for value, reason in (
            (self.reason_code, "ashare_moneyflow_audit_reason_invalid"),
            (self.dataset_id, "ashare_moneyflow_audit_dataset_invalid"),
            (self.catalog_version, "ashare_moneyflow_audit_catalog_invalid"),
        ):
            _text(value, reason)
        _aware(self.decision_time, "ashare_moneyflow_audit_decision_time_invalid")
        _sha256_text(
            self.rejected_payload_sha256,
            "ashare_moneyflow_audit_payload_sha_invalid",
        )
        if self.pit_feature_eligible or any(
            (
                self.candidate_eligible,
                self.execution_eligible,
                self.training_eligible,
                self.promotion_eligible,
                self.execution_authority,
                self.real_trading_enabled,
            )
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_audit_must_be_non_authoritative"
            )


class AshareMoneyflowAuditLedger:
    """Idempotent in-memory audit ledger with no persistence side effect."""

    def __init__(self) -> None:
        self._records: dict[str, AshareMoneyflowAuditRecord] = {}

    def append(self, record: AshareMoneyflowAuditRecord) -> bool:
        if not isinstance(record, AshareMoneyflowAuditRecord):
            raise AshareMoneyflowEvidenceError("ashare_moneyflow_audit_record_invalid")
        identity = _sha256(
            {
                "reason_code": record.reason_code,
                "dataset_id": record.dataset_id,
                "catalog_version": record.catalog_version,
                "decision_time": record.decision_time.astimezone(
                    timezone.utc
                ).isoformat(),
                "payload_sha": record.rejected_payload_sha256,
            }
        )
        previous = self._records.get(identity)
        if previous is None:
            self._records[identity] = record
            return True
        if previous == record:
            return False
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_audit_identity_conflict")

    def records(self) -> tuple[AshareMoneyflowAuditRecord, ...]:
        return tuple(self._records.values())


@dataclass(frozen=True)
class MoneyflowShadowFeature:
    """One PIT-bounded, non-authoritative moneyflow feature."""

    dataset_id: str
    catalog_version: str
    symbol: str
    source_time: str
    source_time_precision: str
    net_flow_amount_cny: float
    available_at: datetime
    decision_time: datetime
    receipt_id: str
    lineage_sha256: str
    source_row_sha256: str
    envelope_proof_sha256: str
    evidence_ref: str
    score_semantics: str = "raw_moneyflow_feature_not_probability"
    calibrated_probability: None = None
    counterfactual_only: bool = True
    pit_feature_eligible: bool = True
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    execution_authority: bool = False
    risk_authority: bool = False
    position_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.dataset_id not in MONEYFLOW_DATASET_IDS:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_feature_dataset_invalid"
            )
        for value, reason in (
            (self.catalog_version, "ashare_moneyflow_feature_catalog_invalid"),
            (self.symbol, "ashare_moneyflow_feature_symbol_invalid"),
            (self.source_time, "ashare_moneyflow_feature_source_time_invalid"),
            (self.source_time_precision, "ashare_moneyflow_feature_precision_invalid"),
            (self.receipt_id, "ashare_moneyflow_feature_receipt_invalid"),
            (self.evidence_ref, "ashare_moneyflow_feature_reference_invalid"),
        ):
            _text(value, reason)
        eligibility = classify_instrument(self.symbol, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != self.symbol
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_symbol_outside_mainboard_scope"
            )
        available = _aware(
            self.available_at, "ashare_moneyflow_feature_available_at_invalid"
        )
        decision = _aware(
            self.decision_time, "ashare_moneyflow_feature_decision_time_invalid"
        )
        if available > decision:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_available_after_decision"
            )
        _finite(self.net_flow_amount_cny, "ashare_moneyflow_feature_amount_invalid")
        for value in (
            self.lineage_sha256,
            self.source_row_sha256,
            self.envelope_proof_sha256,
        ):
            _sha256_text(value, "ashare_moneyflow_feature_proof_invalid")
        expected = (
            f"td-v1:{self.dataset_id}:{self.receipt_id}:{self.source_row_sha256[:16]}"
        )
        if self.evidence_ref != expected:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_feature_reference_binding_invalid"
            )
        if (
            self.score_semantics != "raw_moneyflow_feature_not_probability"
            or self.calibrated_probability is not None
            or self.counterfactual_only is not True
            or self.pit_feature_eligible is not True
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
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_feature_authority_invalid"
            )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "catalog_version": self.catalog_version,
            "symbol": self.symbol,
            "source_time": self.source_time,
            "source_time_precision": self.source_time_precision,
            "net_flow_amount_cny": self.net_flow_amount_cny,
            "available_at": self.available_at.isoformat(),
            "decision_time": self.decision_time.isoformat(),
            "receipt_id": self.receipt_id,
            "lineage_sha256": self.lineage_sha256,
            "source_row_sha256": self.source_row_sha256,
            "envelope_proof_sha256": self.envelope_proof_sha256,
            "evidence_ref": self.evidence_ref,
            "score_semantics": self.score_semantics,
            "calibrated_probability": self.calibrated_probability,
            "counterfactual_only": self.counterfactual_only,
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

    def to_minute_auxiliary(
        self,
        *,
        expires_at: datetime,
        normalization_scale_cny: float,
    ) -> Any:
        """Explicitly project this record into the flow counterfactual sleeve."""

        from .minute_loop import MinuteAuxiliaryEvidence

        scale = _finite(
            normalization_scale_cny,
            "ashare_moneyflow_normalization_scale_invalid",
        )
        expires = _aware(expires_at, "ashare_moneyflow_auxiliary_expiry_invalid")
        if scale <= 0 or expires < self.decision_time:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_auxiliary_parameters_invalid"
            )
        return MinuteAuxiliaryEvidence(
            symbol=self.symbol,
            evidence_type="flow",
            normalized_score=math.tanh(self.net_flow_amount_cny / scale),
            event_time=self.available_at,
            available_at=self.available_at,
            decision_time=self.decision_time,
            expires_at=expires,
            evidence_sha256=self.sha256,
            execution_authority=False,
        )


@dataclass(frozen=True)
class MoneyflowShadowSnapshot:
    profile: MoneyflowDatasetProfile
    features: tuple[MoneyflowShadowFeature, ...]
    page_count: int
    row_count: int
    pagination_trace_sha256: str
    first_semantic_sha256: str
    replay_semantic_sha256: str
    same_observation: bool
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE
    counterfactual_only: bool = True
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    execution_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile, MoneyflowDatasetProfile)
            or not self.features
            or self.row_count != len(self.features)
            or not 1 <= self.page_count <= self.profile.max_pages
        ):
            raise AshareMoneyflowEvidenceError("ashare_moneyflow_snapshot_invalid")
        for value in (
            self.pagination_trace_sha256,
            self.first_semantic_sha256,
            self.replay_semantic_sha256,
        ):
            _sha256_text(value, "ashare_moneyflow_snapshot_proof_invalid")
        if (
            self.first_semantic_sha256 != self.replay_semantic_sha256
            or self.same_observation is not True
            or self.catalog_route != FIXED_CATALOG_ROUTE
            or self.query_route != FIXED_QUERY_ROUTE
            or self.counterfactual_only is not True
            or any(
                (
                    self.candidate_eligible,
                    self.execution_eligible,
                    self.training_eligible,
                    self.promotion_eligible,
                    self.execution_authority,
                    self.real_trading_enabled,
                )
            )
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_snapshot_authority_or_replay_invalid"
            )


def _validate_filters(
    *, profile: MoneyflowDatasetProfile, filters: Mapping[str, Any]
) -> None:
    allowed = dict(profile.filter_operators)
    for raw_name, raw_expression in filters.items():
        field_name = _text(raw_name, "ashare_moneyflow_filter_field_invalid")
        operators = allowed.get(field_name)
        if operators is None:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_filter_not_catalog_allowed"
            )
        if not isinstance(raw_expression, Mapping) or len(raw_expression) != 1:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_filter_expression_invalid"
            )
        operator = _text(
            next(iter(raw_expression)), "ashare_moneyflow_filter_operator_invalid"
        )
        if operator not in operators:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_filter_operator_not_catalog_allowed"
            )


def _map_run(
    *,
    profile: MoneyflowDatasetProfile,
    run: PagedQueryRun,
    decision_time: datetime,
    allowed_symbols: frozenset[str] | None,
) -> tuple[MoneyflowShadowFeature, ...]:
    run.verify_integrity(identity_fields=profile.identity_fields)
    envelope = run.envelope
    metadata = envelope.metadata
    if (
        envelope.dataset_id != profile.dataset_id
        or envelope.catalog_version != profile.catalog_version
    ):
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_query_binding_mismatch")
    if metadata.state.strip().lower() != "ready" or metadata.degraded is not False:
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_metadata_not_ready")
    freshness = metadata.freshness
    if not (
        isinstance(freshness, Mapping)
        and freshness.get("state") == "fresh"
        and freshness.get("stale") is False
    ):
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_metadata_not_fresh")
    quality = metadata.quality
    if not isinstance(quality, Mapping) or quality.get("state") != "valid":
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_metadata_quality_invalid")
    if not _complete_lineage(metadata.lineage):
        raise AshareMoneyflowEvidenceError(
            "ashare_moneyflow_metadata_lineage_incomplete"
        )
    if metadata.reasons or not all(
        isinstance(value, str) and value and value == value.strip()
        for value in (metadata.receipt_id, metadata.data_through, metadata.observed_at)
    ):
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_metadata_proof_incomplete")
    decision = _aware(decision_time, "ashare_moneyflow_decision_time_timezone_required")
    data_through = _parse_aware(
        metadata.data_through, "ashare_moneyflow_data_through_invalid"
    )
    observed = _parse_aware(
        metadata.observed_at, "ashare_moneyflow_observed_at_invalid"
    )
    if data_through > observed:
        raise AshareMoneyflowEvidenceError(
            "ashare_moneyflow_metadata_time_order_invalid"
        )
    if observed > decision:
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_available_after_decision")
    assert metadata.lineage is not None
    lineage_sha256 = _sha256(metadata.lineage)
    envelope_proof_sha256 = _sha256(
        {
            "dataset_id": envelope.dataset_id,
            "catalog_version": envelope.catalog_version,
            "receipt_id": metadata.receipt_id,
            "lineage_sha256": lineage_sha256,
            "data_through": metadata.data_through,
            "observed_at": metadata.observed_at,
            "semantic_sha256": run.semantic_sha256,
        }
    )
    features: list[MoneyflowShadowFeature] = []
    for row in envelope.data:
        if not isinstance(row, Mapping):
            raise AshareMoneyflowEvidenceError("ashare_moneyflow_row_invalid")
        symbol = _text(
            row.get(profile.symbol_field), "ashare_moneyflow_row_symbol_missing"
        ).upper()
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != symbol
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_symbol_outside_mainboard_scope"
            )
        if allowed_symbols is not None and symbol not in allowed_symbols:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_symbol_not_in_allowed_universe"
            )
        source_time, precision = _parse_source_time(
            row.get(profile.source_time_field), available_at=observed
        )
        amount = _finite(
            row.get(profile.net_flow_amount_field),
            "ashare_moneyflow_row_net_amount_invalid",
        )
        row_sha256 = _sha256(row)
        features.append(
            MoneyflowShadowFeature(
                dataset_id=profile.dataset_id,
                catalog_version=profile.catalog_version,
                symbol=symbol,
                source_time=source_time,
                source_time_precision=precision,
                net_flow_amount_cny=amount,
                available_at=observed,
                decision_time=decision,
                receipt_id=metadata.receipt_id,
                lineage_sha256=lineage_sha256,
                source_row_sha256=row_sha256,
                envelope_proof_sha256=envelope_proof_sha256,
                evidence_ref=(
                    f"td-v1:{profile.dataset_id}:{metadata.receipt_id}:{row_sha256[:16]}"
                ),
            )
        )
    if not features:
        raise AshareMoneyflowEvidenceError("ashare_moneyflow_query_returned_no_rows")
    return tuple(features)


def _snapshot_from_runs(
    *,
    profile: MoneyflowDatasetProfile,
    first: PagedQueryRun,
    replay: PagedQueryRun,
    decision_time: datetime,
    audit_ledger: AshareMoneyflowAuditLedger,
    allowed_symbols: tuple[str, ...] | None,
) -> MoneyflowShadowSnapshot:
    rejected_payload = {
        "dataset_id": profile.dataset_id,
        "catalog_version": profile.catalog_version,
        "first_semantic_sha256": getattr(first, "semantic_sha256", None),
        "replay_semantic_sha256": getattr(replay, "semantic_sha256", None),
    }
    try:
        allowlist = _mainboard_allowlist(allowed_symbols)
        if (
            first.semantic_sha256 != replay.semantic_sha256
            or first.semantic_trace_sha256 != replay.semantic_trace_sha256
        ):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_same_observation_mismatch"
            )
        features = _map_run(
            profile=profile,
            run=first,
            decision_time=decision_time,
            allowed_symbols=allowlist,
        )
        replay_features = _map_run(
            profile=profile,
            run=replay,
            decision_time=decision_time,
            allowed_symbols=allowlist,
        )
        if [feature.sha256 for feature in features] != [
            feature.sha256 for feature in replay_features
        ]:
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_same_observation_mismatch"
            )
        return MoneyflowShadowSnapshot(
            profile=profile,
            features=features,
            page_count=first.page_count,
            row_count=len(features),
            pagination_trace_sha256=first.pagination_trace_sha256,
            first_semantic_sha256=first.semantic_sha256,
            replay_semantic_sha256=replay.semantic_sha256,
            same_observation=True,
        )
    except AshareMoneyflowEvidenceError as exc:
        audit_ledger.append(
            AshareMoneyflowAuditRecord(
                reason_code=exc.reason_code,
                dataset_id=profile.dataset_id,
                catalog_version=profile.catalog_version,
                decision_time=_aware(
                    decision_time,
                    "ashare_moneyflow_decision_time_timezone_required",
                ),
                rejected_payload_sha256=_sha256(rejected_payload),
            )
        )
        raise


class AshareMoneyflowEvidencePort(Protocol):
    def freeze_profiles(
        self, *, audit_ledger: AshareMoneyflowAuditLedger
    ) -> MoneyflowProfileSet: ...

    def load_shadow_snapshot(
        self,
        *,
        profile: MoneyflowDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        audit_ledger: AshareMoneyflowAuditLedger,
        allowed_symbols: tuple[str, ...] | None = None,
    ) -> MoneyflowShadowSnapshot: ...


class TradingDatasAshareMoneyflowPort:
    """Injected-client consumer for two independent moneyflow variants."""

    def __init__(self, client: SharedSignalsV1Client) -> None:
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        if not client.config.dataset_ids <= frozenset(MONEYFLOW_DATASET_IDS):
            raise AshareMoneyflowEvidenceError(
                "ashare_moneyflow_client_dataset_scope_invalid"
            )
        self._client = client

    def freeze_profiles(
        self, *, audit_ledger: AshareMoneyflowAuditLedger
    ) -> MoneyflowProfileSet:
        if not isinstance(audit_ledger, AshareMoneyflowAuditLedger):
            raise TypeError("audit_ledger must be AshareMoneyflowAuditLedger")
        decision_time = datetime.now(timezone.utc)
        try:
            catalog = self._client.get_catalog()
            rows: dict[str, Mapping[str, Any]] = {}
            for row in catalog.data:
                if not isinstance(row, Mapping):
                    raise AshareMoneyflowEvidenceError(
                        "ashare_moneyflow_catalog_row_invalid"
                    )
                dataset_id = row.get("dataset_id")
                if dataset_id not in self._client.config.dataset_ids:
                    continue
                if dataset_id in rows:
                    raise AshareMoneyflowEvidenceError(
                        "ashare_moneyflow_catalog_dataset_duplicate"
                    )
                rows[dataset_id] = row
            profiles = {
                dataset_id: MoneyflowDatasetProfile.from_catalog_row(catalog, row)
                for dataset_id, row in rows.items()
                if _active_catalog_row(row)
            }
            if not profiles:
                raise AshareMoneyflowEvidenceError(
                    "ashare_moneyflow_no_active_catalog_profile"
                )
            return MoneyflowProfileSet(
                catalog_version=catalog.catalog_version,
                by_dataset=profiles,
                catalog_contract_sha256=_sha256(
                    {
                        "catalog_version": catalog.catalog_version,
                        "profiles": {
                            dataset_id: profile.catalog_contract_sha256
                            for dataset_id, profile in sorted(profiles.items())
                        },
                    }
                ),
            )
        except AshareMoneyflowEvidenceError as exc:
            reason = exc.reason_code
        except SharedSignalsV1Error:
            reason = "ashare_moneyflow_catalog_failed"
        audit_ledger.append(
            AshareMoneyflowAuditRecord(
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
        raise AshareMoneyflowEvidenceError(reason)

    def load_shadow_snapshot(
        self,
        *,
        profile: MoneyflowDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        audit_ledger: AshareMoneyflowAuditLedger,
        allowed_symbols: tuple[str, ...] | None = None,
    ) -> MoneyflowShadowSnapshot:
        if not isinstance(profile, MoneyflowDatasetProfile):
            raise TypeError("profile must be MoneyflowDatasetProfile")
        if not isinstance(filters, Mapping):
            raise AshareMoneyflowEvidenceError("ashare_moneyflow_filters_invalid")
        if not isinstance(audit_ledger, AshareMoneyflowAuditLedger):
            raise TypeError("audit_ledger must be AshareMoneyflowAuditLedger")
        decision = _aware(
            decision_time, "ashare_moneyflow_decision_time_timezone_required"
        )
        audits_before = len(audit_ledger.records())
        try:
            normalized_allowed = _mainboard_allowlist(allowed_symbols)
            catalog = self._client.get_catalog()
            if catalog.catalog_version != profile.catalog_version:
                raise AshareMoneyflowEvidenceError(
                    "ashare_moneyflow_catalog_version_drift"
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
                raise AshareMoneyflowEvidenceError(
                    "ashare_moneyflow_dataset_not_active"
                )
            if _sha256(_catalog_contract(row)) != profile.catalog_contract_sha256:
                raise AshareMoneyflowEvidenceError(
                    "ashare_moneyflow_catalog_contract_drift"
                )
            _validate_filters(profile=profile, filters=filters)
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
            return _snapshot_from_runs(
                profile=profile,
                first=first,
                replay=replay,
                decision_time=decision,
                audit_ledger=audit_ledger,
                allowed_symbols=(
                    tuple(sorted(normalized_allowed))
                    if normalized_allowed is not None
                    else None
                ),
            )
        except AshareMoneyflowEvidenceError as exc:
            reason = exc.reason_code
            source_reason = exc.reason_code
        except PaginationContractError as exc:
            reason = "ashare_moneyflow_pagination_failed"
            source_reason = str(exc)
        except SharedSignalsV1Error as exc:
            reason = "ashare_moneyflow_query_failed"
            source_reason = type(exc).__name__
        if len(audit_ledger.records()) == audits_before:
            audit_ledger.append(
                AshareMoneyflowAuditRecord(
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
        raise AshareMoneyflowEvidenceError(reason)


__all__ = [
    "FIXED_CATALOG_ROUTE",
    "FIXED_QUERY_ROUTE",
    "MONEYFLOW_DATASET_IDS",
    "AshareMoneyflowAuditLedger",
    "AshareMoneyflowAuditRecord",
    "AshareMoneyflowEvidenceError",
    "AshareMoneyflowEvidencePort",
    "MoneyflowDatasetProfile",
    "MoneyflowProfileSet",
    "MoneyflowShadowFeature",
    "MoneyflowShadowSnapshot",
    "TradingDatasAshareMoneyflowPort",
]
