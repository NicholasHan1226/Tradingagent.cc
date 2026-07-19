#!/usr/bin/env python3
"""Provider-neutral SharedSignals V1 client contract for TradingAgent.

The client deliberately has no default transport.  Runtime code must inject an
explicit HTTP transport and an explicit catalog/dataset configuration.  This
keeps tests offline and prevents silent fallback to legacy endpoints or local
storage.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


CATALOG_PATH = "/v1/catalog"
QUERY_PATH = "/v1/query"
QUERY_RESPONSE_SCHEMA_ID = "sharedsignals.query_result.v1"

_DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class SharedSignalsV1Error(RuntimeError):
    """Base error for the fail-closed V1 boundary."""


class TransportNotConfigured(SharedSignalsV1Error):
    """Raised when a caller tries to use the client without a transport."""


class HTTPStatusError(SharedSignalsV1Error):
    """Raised when the injected transport returns a non-success status."""


class ContractViolation(SharedSignalsV1Error):
    """Raised when a V1 request or response violates the frozen fixture contract."""


class CatalogContractError(ContractViolation):
    """Raised when the catalog does not match explicit TradingAgent config."""


@dataclass(frozen=True)
class HTTPResponse:
    """Decoded HTTP response returned by an explicitly injected transport."""

    status_code: int
    json_body: Mapping[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise TypeError("status_code must be an integer")
        if not isinstance(self.json_body, Mapping):
            raise TypeError("json_body must be a mapping")


class HTTPTransport(Protocol):
    """Minimal transport port; implementations live outside this contract."""

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse: ...


def _native_nonempty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ContractViolation(f"{field_name} must not contain outer whitespace")
    return value


def _dataset_id(value: Any, *, field_name: str = "dataset_id") -> str:
    dataset_id = _native_nonempty_string(value, field_name=field_name)
    if not _DATASET_ID_RE.fullmatch(dataset_id):
        raise ContractViolation(f"{field_name} has an invalid canonical format")
    return dataset_id


def _parse_aware_iso_timestamp(value: Any, *, field_name: str) -> tuple[str, datetime]:
    text = _native_nonempty_string(value, field_name=field_name)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractViolation(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractViolation(f"{field_name} must include a timezone offset")
    return text, parsed


def _aware_iso_timestamp(value: Any, *, field_name: str) -> str:
    return _parse_aware_iso_timestamp(value, field_name=field_name)[0]


def _copy_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractViolation(f"{field_name} must be a mapping")
    copied: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ContractViolation(f"{field_name} keys must be non-empty strings")
        copied[key] = copy.deepcopy(item)
    return copied


def _canonical_json(value: Any, *, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            f"{field_name} must contain canonical JSON values"
        ) from exc


@dataclass(frozen=True)
class SharedSignalsV1Config:
    """Explicit runtime contract; no endpoint or dataset discovery fallback."""

    base_url: str
    expected_catalog_version: str
    dataset_ids: frozenset[str]
    access_policy_id: str
    timeout_seconds: float = 10.0
    max_limit: int = 10_000
    cache_ttl_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ValueError("base_url must be explicitly configured")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not include a query or fragment")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

        if (
            not isinstance(self.expected_catalog_version, str)
            or not self.expected_catalog_version.strip()
        ):
            raise ValueError("expected_catalog_version must be explicitly configured")
        if self.expected_catalog_version != self.expected_catalog_version.strip():
            raise ValueError(
                "expected_catalog_version must not contain outer whitespace"
            )

        try:
            normalized_dataset_ids = frozenset(
                _dataset_id(item, field_name="dataset_ids item")
                for item in self.dataset_ids
            )
        except TypeError as exc:
            raise ValueError("dataset_ids must be a non-empty collection") from exc
        if not normalized_dataset_ids:
            raise ValueError("dataset_ids must be explicitly configured")
        object.__setattr__(self, "dataset_ids", normalized_dataset_ids)

        if (
            not isinstance(self.access_policy_id, str)
            or not self.access_policy_id.strip()
        ):
            raise ValueError("access_policy_id must be explicitly configured")
        if self.access_policy_id != self.access_policy_id.strip():
            raise ValueError("access_policy_id must not contain outer whitespace")

        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(self.max_limit, bool)
            or not isinstance(self.max_limit, int)
            or self.max_limit <= 0
        ):
            raise ValueError("max_limit must be a positive integer")
        if (
            isinstance(self.cache_ttl_seconds, bool)
            or not isinstance(self.cache_ttl_seconds, (int, float))
            or self.cache_ttl_seconds < 0
        ):
            raise ValueError("cache_ttl_seconds must be non-negative")


@dataclass(frozen=True)
class QueryRequest:
    """Provider-neutral query request accepted by ``POST /v1/query``."""

    dataset_id: str
    schema_major: int
    fields: tuple[str, ...] = ()
    filters: Mapping[str, Any] = field(default_factory=dict)
    as_of: str | None = None
    order: tuple[str, ...] | None = None
    limit: int = 1_000
    cursor: str | None = None
    _filters_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _dataset_id(self.dataset_id))
        if (
            isinstance(self.schema_major, bool)
            or not isinstance(self.schema_major, int)
            or self.schema_major <= 0
        ):
            raise ContractViolation("schema_major must be a positive integer")

        if isinstance(self.fields, str):
            raise ContractViolation("fields must be a sequence of field names")
        normalized_fields: list[str] = []
        for item in self.fields:
            field_name = _native_nonempty_string(item, field_name="fields item")
            if field_name in normalized_fields:
                raise ContractViolation("fields must not contain duplicates")
            normalized_fields.append(field_name)
        object.__setattr__(self, "fields", tuple(normalized_fields))

        filters = _copy_mapping(self.filters, field_name="filters")
        filters_json = _canonical_json(filters, field_name="filters")
        object.__setattr__(self, "filters", json.loads(filters_json))
        object.__setattr__(self, "_filters_json", filters_json)

        if self.as_of is not None:
            object.__setattr__(
                self,
                "as_of",
                _aware_iso_timestamp(self.as_of, field_name="as_of"),
            )
        if self.order is not None:
            if isinstance(self.order, str):
                raise ContractViolation("order must be a sequence of order terms")
            normalized_order: list[str] = []
            for item in self.order:
                order_term = _native_nonempty_string(item, field_name="order item")
                if order_term in normalized_order:
                    raise ContractViolation("order must not contain duplicates")
                normalized_order.append(order_term)
            if not normalized_order:
                raise ContractViolation("order must not be empty when provided")
            object.__setattr__(self, "order", tuple(normalized_order))
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit <= 0
        ):
            raise ContractViolation("limit must be a positive integer")
        if self.cursor is not None:
            object.__setattr__(
                self,
                "cursor",
                _native_nonempty_string(self.cursor, field_name="cursor"),
            )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "schema_major": self.schema_major,
            "fields": list(self.fields),
            "filters": json.loads(self._filters_json),
            "as_of": self.as_of,
            "limit": self.limit,
            "cursor": self.cursor,
        }
        if self.order is not None:
            payload["order"] = list(self.order)
        return payload

    @property
    def sha256(self) -> str:
        payload = _canonical_json(self.to_payload(), field_name="query")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, init=False)
class CatalogEnvelope:
    api_version: str
    catalog_version: str
    request_id: str
    _data_json: str = field(repr=False)

    def __init__(
        self,
        *,
        api_version: str,
        catalog_version: str,
        request_id: str,
        data: tuple[dict[str, Any], ...],
    ) -> None:
        object.__setattr__(self, "api_version", api_version)
        object.__setattr__(self, "catalog_version", catalog_version)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(
            self,
            "_data_json",
            _canonical_json(list(data), field_name="catalog envelope data"),
        )

    @property
    def data(self) -> tuple[dict[str, Any], ...]:
        decoded = json.loads(self._data_json)
        return tuple(decoded)

    @property
    def dataset_ids(self) -> frozenset[str]:
        return frozenset(row["dataset_id"] for row in self.data)


@dataclass(frozen=True, init=False)
class QueryMetadata:
    state: str
    degraded: bool
    receipt_id: str | None
    data_through: str | None
    observed_at: str | None
    reasons: tuple[str, ...]
    _freshness_json: str = field(repr=False)
    _quality_json: str = field(repr=False)
    _lineage_json: str | None = field(repr=False)

    def __init__(
        self,
        *,
        state: str,
        degraded: bool,
        freshness: dict[str, Any],
        quality: dict[str, Any],
        lineage: dict[str, Any] | None,
        receipt_id: str | None,
        data_through: str | None,
        observed_at: str | None,
        reasons: tuple[str, ...],
    ) -> None:
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "degraded", degraded)
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "data_through", data_through)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "reasons", tuple(reasons))
        object.__setattr__(
            self,
            "_freshness_json",
            _canonical_json(freshness, field_name="query metadata freshness"),
        )
        object.__setattr__(
            self,
            "_quality_json",
            _canonical_json(quality, field_name="query metadata quality"),
        )
        object.__setattr__(
            self,
            "_lineage_json",
            (
                None
                if lineage is None
                else _canonical_json(lineage, field_name="query metadata lineage")
            ),
        )

    @property
    def freshness(self) -> dict[str, Any]:
        return json.loads(self._freshness_json)

    @property
    def quality(self) -> dict[str, Any]:
        return json.loads(self._quality_json)

    @property
    def lineage(self) -> dict[str, Any] | None:
        return None if self._lineage_json is None else json.loads(self._lineage_json)


@dataclass(frozen=True, init=False)
class QueryEnvelope:
    api_version: str
    catalog_version: str
    request_id: str
    dataset_id: str
    next_cursor: str | None
    metadata: QueryMetadata
    _data_json: str = field(repr=False)

    def __init__(
        self,
        *,
        api_version: str,
        catalog_version: str,
        request_id: str,
        dataset_id: str,
        data: tuple[dict[str, Any], ...],
        next_cursor: str | None,
        metadata: QueryMetadata,
    ) -> None:
        object.__setattr__(self, "api_version", api_version)
        object.__setattr__(self, "catalog_version", catalog_version)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "dataset_id", dataset_id)
        object.__setattr__(self, "next_cursor", next_cursor)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(
            self,
            "_data_json",
            _canonical_json(list(data), field_name="query envelope data"),
        )

    @property
    def data(self) -> tuple[dict[str, Any], ...]:
        decoded = json.loads(self._data_json)
        return tuple(decoded)


@dataclass(frozen=True, order=True)
class CacheKey:
    """Complete cache identity, including source receipt and access policy."""

    query_sha256: str
    catalog_version: str
    schema_id: str
    receipt_id: str
    access_policy_id: str


def _parse_rows(value: Any, *, field_name: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ContractViolation(f"{field_name} must be a list")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ContractViolation(f"{field_name}[{index}] must be a mapping")
        row = _copy_mapping(raw, field_name=f"{field_name}[{index}]")
        _canonical_json(row, field_name=f"{field_name}[{index}]")
        rows.append(row)
    return tuple(rows)


def parse_catalog_envelope(payload: Mapping[str, Any]) -> CatalogEnvelope:
    root = _copy_mapping(payload, field_name="catalog response")
    api_version = _native_nonempty_string(
        root.get("api_version"), field_name="api_version"
    )
    if api_version != "v1":
        raise CatalogContractError("api_version must be v1")
    catalog_version = _native_nonempty_string(
        root.get("catalog_version"), field_name="catalog_version"
    )
    request_id = _native_nonempty_string(
        root.get("request_id"), field_name="request_id"
    )
    data = _parse_rows(root.get("data"), field_name="data")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(data):
        dataset = _dataset_id(
            row.get("dataset_id"), field_name=f"data[{index}].dataset_id"
        )
        if dataset in seen:
            raise CatalogContractError("catalog contains duplicate dataset_id")
        seen.add(dataset)
        copied = dict(row)
        copied["dataset_id"] = dataset
        normalized.append(copied)
    return CatalogEnvelope(
        api_version=api_version,
        catalog_version=catalog_version,
        request_id=request_id,
        data=tuple(normalized),
    )


def parse_query_envelope(payload: Mapping[str, Any]) -> QueryEnvelope:
    """Strictly parse a query response without assigning trading authority."""

    root = _copy_mapping(payload, field_name="query response")
    api_version = _native_nonempty_string(
        root.get("api_version"), field_name="api_version"
    )
    if api_version != "v1":
        raise ContractViolation("api_version must be v1")
    catalog_version = _native_nonempty_string(
        root.get("catalog_version"), field_name="catalog_version"
    )
    request_id = _native_nonempty_string(
        root.get("request_id"), field_name="request_id"
    )
    dataset = _dataset_id(root.get("dataset_id"))
    data = _parse_rows(root.get("data"), field_name="data")
    raw_next_cursor = root.get("next_cursor")
    next_cursor = (
        None
        if raw_next_cursor is None
        else _native_nonempty_string(raw_next_cursor, field_name="next_cursor")
    )

    metadata = _copy_mapping(root.get("metadata"), field_name="metadata")
    state = _native_nonempty_string(metadata.get("state"), field_name="metadata.state")
    degraded = metadata.get("degraded")
    if type(degraded) is not bool:
        raise ContractViolation("metadata.degraded must be a boolean")
    freshness = _copy_mapping(
        metadata.get("freshness"), field_name="metadata.freshness"
    )
    quality = _copy_mapping(metadata.get("quality"), field_name="metadata.quality")
    for required_field in (
        "lineage",
        "receipt_id",
        "data_through",
        "observed_at",
    ):
        if required_field not in metadata:
            raise ContractViolation(f"metadata.{required_field} is required")

    raw_lineage = metadata.get("lineage")
    lineage = (
        None
        if raw_lineage is None
        else _copy_mapping(raw_lineage, field_name="metadata.lineage")
    )
    for field_name, structured in (
        ("metadata.freshness", freshness),
        ("metadata.quality", quality),
    ):
        if not structured:
            raise ContractViolation(f"{field_name} must not be empty")
        _canonical_json(structured, field_name=field_name)
    if lineage is not None:
        if not lineage:
            raise ContractViolation("metadata.lineage must not be empty")
        _canonical_json(lineage, field_name="metadata.lineage")

    raw_receipt_id = metadata.get("receipt_id")
    receipt_id = (
        None
        if raw_receipt_id is None
        else _native_nonempty_string(
            raw_receipt_id,
            field_name="metadata.receipt_id",
        )
    )
    raw_data_through = metadata.get("data_through")
    data_through: str | None = None
    data_through_instant: datetime | None = None
    if raw_data_through is not None:
        data_through, data_through_instant = _parse_aware_iso_timestamp(
            raw_data_through,
            field_name="metadata.data_through",
        )
    raw_observed_at = metadata.get("observed_at")
    observed_at: str | None = None
    observed_at_instant: datetime | None = None
    if raw_observed_at is not None:
        observed_at, observed_at_instant = _parse_aware_iso_timestamp(
            raw_observed_at,
            field_name="metadata.observed_at",
        )
    if (
        data_through_instant is not None
        and observed_at_instant is not None
        and data_through_instant > observed_at_instant
    ):
        raise ContractViolation("metadata.data_through must not be after observed_at")

    normalized_state = state.strip().lower()
    proof_may_be_null = degraded or normalized_state not in {
        "ready",
        "healthy",
        "ok",
        "available",
    }
    if not proof_may_be_null:
        for field_name, value in (
            ("metadata.lineage", lineage),
            ("metadata.receipt_id", receipt_id),
            ("metadata.data_through", data_through),
            ("metadata.observed_at", observed_at),
        ):
            if value is None:
                raise ContractViolation(f"{field_name} must not be null for ready data")
    raw_reasons = metadata.get("reasons")
    if not isinstance(raw_reasons, list):
        raise ContractViolation("metadata.reasons must be a list")
    reasons = tuple(
        _native_nonempty_string(item, field_name="metadata.reasons item")
        for item in raw_reasons
    )

    return QueryEnvelope(
        api_version=api_version,
        catalog_version=catalog_version,
        request_id=request_id,
        dataset_id=dataset,
        data=data,
        next_cursor=next_cursor,
        metadata=QueryMetadata(
            state=state,
            degraded=degraded,
            freshness=freshness,
            quality=quality,
            lineage=lineage,
            receipt_id=receipt_id,
            data_through=data_through,
            observed_at=observed_at,
            reasons=reasons,
        ),
    )


class SharedSignalsV1Client:
    """Strict typed client for the two provider-neutral V1 endpoints."""

    def __init__(
        self,
        config: SharedSignalsV1Config,
        *,
        transport: HTTPTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._transport = transport
        self._clock = clock
        self._cache: dict[CacheKey, tuple[float, QueryEnvelope]] = {}
        self._query_cache_index: dict[tuple[str, str, str, str], CacheKey] = {}

    @property
    def cache_keys(self) -> tuple[CacheKey, ...]:
        self._evict_expired()
        return tuple(sorted(self._cache))

    def _headers(self, *, json_request: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Access-Policy": self.config.access_policy_id,
        }
        if json_request:
            headers["Content-Type"] = "application/json"
        return headers

    def _send(
        self,
        *,
        method: str,
        path: str,
        json_body: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        if self._transport is None:
            raise TransportNotConfigured(
                "SharedSignals V1 transport must be explicitly injected"
            )
        response = self._transport(
            method=method,
            url=f"{self.config.base_url}{path}",
            headers=self._headers(json_request=json_body is not None),
            json_body=json_body,
            timeout_seconds=float(self.config.timeout_seconds),
        )
        if response.status_code != 200:
            raise HTTPStatusError(
                f"SharedSignals V1 {path} returned HTTP {response.status_code}"
            )
        if not isinstance(response.json_body, Mapping):
            raise ContractViolation("HTTP response body must be a mapping")
        return response.json_body

    def get_catalog(self) -> CatalogEnvelope:
        payload = self._send(method="GET", path=CATALOG_PATH, json_body=None)
        catalog = parse_catalog_envelope(payload)
        if catalog.catalog_version != self.config.expected_catalog_version:
            raise CatalogContractError(
                "catalog_version does not match explicit TradingAgent config"
            )
        missing = self.config.dataset_ids.difference(catalog.dataset_ids)
        if missing:
            raise CatalogContractError(
                "catalog is missing configured dataset IDs: "
                + ", ".join(sorted(missing))
            )
        return catalog

    def _lookup_key(self, request: QueryRequest) -> tuple[str, str, str, str]:
        return (
            request.sha256,
            self.config.expected_catalog_version,
            QUERY_RESPONSE_SCHEMA_ID,
            self.config.access_policy_id,
        )

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [key for key, (deadline, _) in self._cache.items() if deadline <= now]
        for key in expired:
            del self._cache[key]
        if expired:
            live_keys = set(self._cache)
            self._query_cache_index = {
                lookup: key
                for lookup, key in self._query_cache_index.items()
                if key in live_keys
            }

    def _cached(self, request: QueryRequest) -> QueryEnvelope | None:
        self._evict_expired()
        cache_key = self._query_cache_index.get(self._lookup_key(request))
        if cache_key is None:
            return None
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        return copy.deepcopy(entry[1])

    def _store(self, request: QueryRequest, envelope: QueryEnvelope) -> None:
        if self.config.cache_ttl_seconds <= 0:
            return
        if envelope.metadata.degraded or envelope.metadata.state.lower() != "ready":
            return
        if any(
            value is None
            for value in (
                envelope.metadata.lineage,
                envelope.metadata.receipt_id,
                envelope.metadata.data_through,
                envelope.metadata.observed_at,
            )
        ):
            return
        receipt_id = envelope.metadata.receipt_id
        assert receipt_id is not None
        cache_key = CacheKey(
            query_sha256=request.sha256,
            catalog_version=envelope.catalog_version,
            schema_id=QUERY_RESPONSE_SCHEMA_ID,
            receipt_id=receipt_id,
            access_policy_id=self.config.access_policy_id,
        )
        self._cache[cache_key] = (
            self._clock() + float(self.config.cache_ttl_seconds),
            copy.deepcopy(envelope),
        )
        self._query_cache_index[self._lookup_key(request)] = cache_key

    def query(self, request: QueryRequest) -> QueryEnvelope:
        if not isinstance(request, QueryRequest):
            raise ContractViolation("query request must be a QueryRequest")
        if request.dataset_id not in self.config.dataset_ids:
            raise ContractViolation(
                "dataset_id is not present in explicit client config"
            )
        if request.limit > self.config.max_limit:
            raise ContractViolation("limit exceeds configured max_limit")

        cached = self._cached(request)
        if cached is not None:
            return cached

        payload = self._send(
            method="POST",
            path=QUERY_PATH,
            json_body=request.to_payload(),
        )
        envelope = parse_query_envelope(payload)
        if envelope.catalog_version != self.config.expected_catalog_version:
            raise ContractViolation(
                "catalog_version does not match explicit TradingAgent config"
            )
        if envelope.dataset_id != request.dataset_id:
            raise ContractViolation("dataset_id does not match the query request")
        if request.as_of is not None and envelope.metadata.data_through is not None:
            _, as_of_instant = _parse_aware_iso_timestamp(
                request.as_of, field_name="as_of"
            )
            _, data_through_instant = _parse_aware_iso_timestamp(
                envelope.metadata.data_through,
                field_name="metadata.data_through",
            )
            if data_through_instant > as_of_instant:
                raise ContractViolation(
                    "metadata.data_through must not be after the requested as_of"
                )

        # Cache only after the complete response has passed strict validation.
        self._store(request, envelope)
        return envelope


__all__ = [
    "CATALOG_PATH",
    "QUERY_PATH",
    "QUERY_RESPONSE_SCHEMA_ID",
    "CacheKey",
    "CatalogContractError",
    "CatalogEnvelope",
    "ContractViolation",
    "HTTPResponse",
    "HTTPStatusError",
    "HTTPTransport",
    "QueryEnvelope",
    "QueryMetadata",
    "QueryRequest",
    "SharedSignalsV1Client",
    "SharedSignalsV1Config",
    "SharedSignalsV1Error",
    "TransportNotConfigured",
    "parse_catalog_envelope",
    "parse_query_envelope",
]
