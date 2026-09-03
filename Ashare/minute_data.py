"""Strict five-minute TradingDatas evidence contract for the A-share lane.

The module is deliberately provider-neutral and mock-first.  A dataset profile
must be derived from one frozen ``GET /v1/catalog`` response.  Rows are then
read only through ``POST /v1/query`` and are bound to the response-envelope
receipt, lineage, freshness and observation timestamps.

No production dataset ID, transport, SQLite path, legacy route, fallback, or
trading authority exists here.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
import hashlib
import json
import math
import threading
from time import monotonic as _monotonic
from time import sleep as _sleep
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    CatalogContractError,
    ContractViolation,
    HTTPStatusError,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Error,
    TransportNotConfigured,
)
from shared.data.tradingdatas_pagination import (
    PagedQueryRun,
    PaginationContractError,
    collect_query_pages,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    TradingDatasAuthenticationError,
)
from shared.governance.evidence_readiness import (
    dataset_contract_fingerprint,
    load_evidence_readiness_contract,
)
from shared.universe.policy import is_mainboard_tradable


SHANGHAI = ZoneInfo("Asia/Shanghai")
FIVE_MINUTES = timedelta(minutes=5)
MAX_MINUTE_DATA_LATENCY = timedelta(seconds=30)
FIXED_CATALOG_ROUTE = "GET /v1/catalog"
FIXED_QUERY_ROUTE = "POST /v1/query"
MAX_MINUTE_FANOUT_WORKERS = 4
# TradingDatas can briefly return a retryable response while its provider
# collector holds the SQLite authority lock. Keep this bounded so a stale
# observation cannot be promoted by waiting indefinitely.
MINUTE_QUERY_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0, 20.0)
# Aggregate wall-clock budget for one sharded snapshot load (first read plus
# replay across every shard).  Healthy loads finish in seconds; on a degraded
# server the per-shard retry chains used to multiply past the systemd unit
# budget and get killed mid-round (#297), so shards now stop starting new
# read phases once this budget is gone and degrade to typed fanout failures.
MINUTE_SNAPSHOT_LOAD_BUDGET_SECONDS = 180.0
# Active read-model writes (receipt commits, bar appends) legitimately land
# between the two bounded reads of a same-observation pair during trading
# hours (#589): rt_min receipts commit continuously, so a single naive pair
# is near-deterministically mismatched intraday. Re-collect the pair until
# both reads agree; an unconverged shard degrades through the typed fanout
# failure lane instead of failing the whole snapshot. The accepted pair
# satisfies the identical same-observation contract - fail-closed semantics
# unchanged.
MINUTE_STABLE_PAIR_ATTEMPTS = 4
MINUTE_STABLE_PAIR_RETRY_DELAYS_SECONDS = (0.5, 1.5, 3.0)

_SHA256_HEX = frozenset("0123456789abcdef")


class MinuteDataContractError(ValueError):
    """Fail-closed minute-data contract failure with a stable reason code."""

    def __init__(
        self,
        reason_code: str,
        *,
        failure_stage: str = "unknown",
        failure_class: str = "unknown",
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.failure_stage = failure_stage
        self.failure_class = failure_class


class MinuteSnapshotLoadBudgetExhausted(MinuteDataContractError):
    """Raised when one snapshot load exceeds its wall-clock read budget."""


_FAILURE_CLASSES = frozenset(
    {
        "CatalogContractError",
        "ContractViolation",
        "HTTPStatusError",
        "MinuteSnapshotLoadBudgetExhausted",
        "PaginationContractError",
        "RuntimeGateConfigurationError",
        "SharedSignalsV1Error",
        "TradingDatasAuthenticationError",
        "TransportNotConfigured",
        "OSError",
        "unknown",
    }
)


def _bounded_failure_class(error: BaseException) -> str:
    known = (
        TradingDatasAuthenticationError,
        HTTPStatusError,
        PaginationContractError,
        CatalogContractError,
        RuntimeGateConfigurationError,
        TransportNotConfigured,
        OSError,
        ContractViolation,
        SharedSignalsV1Error,
    )
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in known:
            if isinstance(current, candidate):
                name = candidate.__name__
                return name if name in _FAILURE_CLASSES else "unknown"
        current = current.__cause__ or current.__context__
    return "unknown"


def _request_failure_stage(error: BaseException, *, phase: str) -> str:
    if isinstance(error, TradingDatasAuthenticationError):
        return "auth"
    if isinstance(error, RuntimeGateConfigurationError):
        return "configuration"
    if isinstance(error, (TransportNotConfigured, OSError)):
        return "transport"
    if isinstance(error, HTTPStatusError):
        return f"{phase}_request"
    if isinstance(error, (CatalogContractError, ContractViolation)):
        return f"{phase}_contract"
    return f"{phase}_contract" if phase in {"catalog", "query"} else "unknown"


def _marked_request_failure(error: BaseException, *, phase: str) -> MinuteDataContractError:
    stage = _request_failure_stage(error, phase=phase)
    return MinuteDataContractError(
        "minute_tradingdatas_request_failed",
        failure_stage=stage,
        failure_class=_bounded_failure_class(error),
    )


def _is_retryable_minute_query_error(error: BaseException) -> bool:
    """Return whether one failed TD query may be retried safely.

    Authentication, contract, pagination, and freshness failures remain
    fail-closed. Only transport timeouts and the API's explicitly retryable
    capacity/lock statuses are transient here.
    """

    if isinstance(error, (TimeoutError, OSError)):
        return True
    if isinstance(error, HTTPStatusError):
        message = str(error)
        return message.endswith("HTTP 429") or message.endswith("HTTP 503")
    return False


def _collect_minute_query_pages_with_retry(
    *,
    client: SharedSignalsV1Client,
    request: QueryRequest,
    identity_fields: tuple[str, ...],
    max_pages: int,
    max_rows: int,
    deadline: float | None = None,
) -> PagedQueryRun:
    """Read one query with bounded retry for transient TD API failures."""

    delays = (0.0, *MINUTE_QUERY_RETRY_DELAYS_SECONDS)
    for attempt, delay in enumerate(delays):
        _wait_for_minute_read_retry(delay, deadline=deadline)
        try:
            run = collect_query_pages(
                client=client,
                request=request,
                identity_fields=identity_fields,
                max_pages=max_pages,
                max_rows=max_rows,
            )
            _minute_read_budget_remaining(deadline)
            return run
        except (SharedSignalsV1Error, OSError) as exc:
            if attempt == len(delays) - 1 or not _is_retryable_minute_query_error(exc):
                raise
    raise AssertionError("minute query retry loop exhausted without a result")


def _minute_read_budget_remaining(deadline: float | None) -> float:
    remaining = math.inf if deadline is None else deadline - _monotonic()
    if remaining <= 0:
        raise MinuteSnapshotLoadBudgetExhausted(
            "minute_snapshot_load_budget_exhausted",
            failure_stage="query_request",
            failure_class="MinuteSnapshotLoadBudgetExhausted",
        )
    return remaining


def _wait_for_minute_read_retry(delay: float, *, deadline: float | None) -> None:
    remaining = _minute_read_budget_remaining(deadline)
    if delay >= remaining:
        raise MinuteSnapshotLoadBudgetExhausted(
            "minute_snapshot_load_budget_exhausted",
            failure_stage="query_request",
            failure_class="MinuteSnapshotLoadBudgetExhausted",
        )
    if delay:
        _sleep(delay)
        _minute_read_budget_remaining(deadline)


def _minute_deadline_client(
    client: SharedSignalsV1Client, *, deadline: Callable[[], float],
) -> SharedSignalsV1Client:
    """Keep the client contract/transport, capping every page's wire timeout.

    The shared client has no per-call timeout parameter. A worker-local copy
    avoids changing its caller's config, cache or single-flight transport.
    Authentication and envelope validation remain with their existing owners.
    """

    bounded = copy(client)
    bounded._cache = dict(client._cache)
    bounded._query_cache_index = dict(client._query_cache_index)
    transport = client._transport
    if transport is not None:
        def send(**kwargs: Any) -> Any:
            kwargs["timeout_seconds"] = min(
                kwargs["timeout_seconds"], _minute_read_budget_remaining(deadline())
            )
            return transport(**kwargs)

        bounded._transport = send
    return bounded


def _collect_stable_minute_pair(
    *,
    client: SharedSignalsV1Client,
    request: QueryRequest,
    identity_fields: tuple[str, ...],
    max_pages: int,
    max_rows: int,
    deadline: float | None = None,
) -> tuple[PagedQueryRun, PagedQueryRun]:
    """Collect a read pair whose two bounded reads agree semantically.

    Active read-model writes land between the two reads of a pair during
    trading hours (#589), so a naive first+replay collection is
    near-deterministically mismatched intraday even though each read is
    individually consistent. Re-collecting the pair bounds the wait for a
    stable observation; an unconverged read surfaces through the typed
    budget-exhausted lane and degrades like any other failed shard. The
    accepted pair satisfies exactly the same same-observation contract as
    before - fail-closed semantics unchanged.
    """

    delays = (0.0, *MINUTE_STABLE_PAIR_RETRY_DELAYS_SECONDS)
    for attempt, delay in enumerate(delays[:MINUTE_STABLE_PAIR_ATTEMPTS]):
        _wait_for_minute_read_retry(delay, deadline=deadline)
        first = _collect_minute_query_pages_with_retry(
            client=client,
            request=request,
            identity_fields=identity_fields,
            max_pages=max_pages,
            max_rows=max_rows,
            deadline=deadline,
        )
        replay = _collect_minute_query_pages_with_retry(
            client=client,
            request=request,
            identity_fields=identity_fields,
            max_pages=max_pages,
            max_rows=max_rows,
            deadline=deadline,
        )
        if (
            first.semantic_sha256 == replay.semantic_sha256
            and first.semantic_trace_sha256 == replay.semantic_trace_sha256
        ):
            return first, replay
    raise MinuteSnapshotLoadBudgetExhausted(
        "minute_stable_pair_unconverged",
        failure_stage="query_request",
        failure_class="MinuteSnapshotLoadBudgetExhausted",
    )


def _delayed_paper_latency_limit() -> timedelta:
    """Read the one-cadence delayed-observation bound from governance.

    A delayed observation remains a non-execution tier.  The market adapter
    consumes the frozen shared policy rather than maintaining a second 12m
    latency budget locally.
    """

    try:
        policy = load_evidence_readiness_contract().freshness("delayed_observation")
    except ValueError as exc:
        raise MinuteDataContractError(
            "minute_delayed_readiness_contract_invalid"
        ) from exc
    if (
        policy.wall_clock_freshness_required is not True
        or policy.maximum_lag_seconds is not None
        or policy.maximum_bar_cadence_multiple != 1
        or policy.maximum_jitter_seconds < 0
        or policy.same_event_execution_allowed is not False
    ):
        raise MinuteDataContractError("minute_delayed_readiness_contract_invalid")
    return FIVE_MINUTES * policy.maximum_bar_cadence_multiple + timedelta(
        seconds=policy.maximum_jitter_seconds
    )


MAX_DELAYED_PAPER_LATENCY = _delayed_paper_latency_limit()


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
        raise MinuteDataContractError("minute_payload_not_canonical") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MinuteDataContractError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MinuteDataContractError(reason)
    return value


def _parse_aware_iso(value: object, reason: str) -> datetime:
    raw = _text(value, reason)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MinuteDataContractError(reason) from exc
    return _aware(parsed, reason)


def _parse_proof_timestamp(value: object, reason: str) -> datetime:
    raw = _text(value, reason)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise MinuteDataContractError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def _finite(value: object, reason: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (float(value) <= 0 if positive else float(value) < 0)
    ):
        raise MinuteDataContractError(reason)
    return float(value)


def _strings(value: object, reason: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise MinuteDataContractError(reason)
    result: list[str] = []
    for item in value:
        normalized = _text(item, reason)
        if normalized in result:
            raise MinuteDataContractError(reason)
        result.append(normalized)
    if nonempty and not result:
        raise MinuteDataContractError(reason)
    return tuple(result)


def _active_catalog_row(row: Mapping[str, Any]) -> bool:
    availability = row.get("availability")
    return bool(
        isinstance(availability, Mapping)
        and availability.get("activation_states") == ["active"]
    )


def _fresh(metadata_freshness: Mapping[str, Any]) -> bool:
    state = metadata_freshness.get("state")
    return (
        isinstance(state, str)
        and state.strip().lower() == "fresh"
        and metadata_freshness.get("stale") is False
    )


def _valid_quality(metadata_quality: Mapping[str, Any]) -> bool:
    state = metadata_quality.get("state")
    return isinstance(state, str) and state.strip().lower() == "valid"


def _complete_lineage(lineage: Mapping[str, Any] | None) -> bool:
    providers = lineage.get("providers") if isinstance(lineage, Mapping) else None
    return bool(
        isinstance(lineage, Mapping)
        and lineage.get("complete") is True
        and lineage.get("provider_neutral") is True
        and isinstance(providers, list)
        and bool(providers)
        and all(
            isinstance(provider, str)
            and bool(provider)
            and provider == provider.strip()
            for provider in providers
        )
        and len(providers) == len(set(providers))
        and isinstance(lineage.get("transport_service"), str)
        and bool(lineage.get("transport_service"))
    )


def _session_for_bar(bar_start: datetime, bar_end: datetime) -> str:
    local_start = bar_start.astimezone(SHANGHAI)
    local_end = bar_end.astimezone(SHANGHAI)
    if local_start.date() != local_end.date():
        raise MinuteDataContractError("minute_bar_crosses_trade_date")
    start_value = local_start.time()
    end_value = local_end.time()
    if time(9, 30) <= start_value and end_value <= time(11, 30):
        return "continuous_auction_am"
    if time(13, 0) <= start_value and end_value <= time(15, 0):
        return "continuous_auction_pm"
    raise MinuteDataContractError("minute_bar_outside_trading_session")


class MinuteTimestampSemantics(str, Enum):
    BAR_END = "bar_end"
    BAR_START = "bar_start"


class MinuteEvidenceUse(str, Enum):
    """Explicit latency tier; delayed evidence can never masquerade as live."""

    LOW_LATENCY_EXECUTION = "low_latency_execution"
    DELAYED_PAPER = "delayed_paper"
    HISTORICAL_DISPLAY = "historical_display"


@dataclass(frozen=True)
class MinuteDatasetProfile:
    """TA-owned interpretation of one exact active catalog contract."""

    expected_catalog_version: str
    observed_catalog_version: str
    dataset_id: str
    schema_major: int
    default_fields: tuple[str, ...]
    default_order: tuple[str, ...]
    filter_operators: tuple[tuple[str, tuple[str, ...]], ...]
    dataset_contract_fingerprint: str
    consumer_profile_sha256: str
    identity_fields: tuple[str, ...]
    symbol_field: str
    timestamp_field: str
    open_field: str
    high_field: str
    low_field: str
    close_field: str
    volume_field: str
    amount_field: str
    previous_close_field: str | None
    suspension_field: str | None
    frequency_field: str | None
    frequency_value: str | None
    timestamp_format: str
    timestamp_semantics: MinuteTimestampSemantics
    volume_multiplier_to_shares: float
    amount_multiplier_to_cny: float
    price_adjustment: str
    max_pages: int
    max_rows: int
    page_limit: int
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE

    def __post_init__(self) -> None:
        for field_name in (
            "expected_catalog_version",
            "observed_catalog_version",
            "dataset_id",
            "symbol_field",
            "timestamp_field",
            "open_field",
            "high_field",
            "low_field",
            "close_field",
            "volume_field",
            "amount_field",
            "timestamp_format",
        ):
            _text(getattr(self, field_name), f"minute_profile_{field_name}_invalid")
        if (self.previous_close_field is None) != (self.suspension_field is None):
            raise MinuteDataContractError("minute_reference_field_contract_incomplete")
        for field_name in ("previous_close_field", "suspension_field"):
            value = getattr(self, field_name)
            if value is not None:
                _text(value, f"minute_profile_{field_name}_invalid")
        if self.catalog_route != FIXED_CATALOG_ROUTE:
            raise MinuteDataContractError("minute_catalog_route_invalid")
        if self.query_route != FIXED_QUERY_ROUTE:
            raise MinuteDataContractError("minute_query_route_invalid")
        if (
            isinstance(self.schema_major, bool)
            or not isinstance(self.schema_major, int)
            or self.schema_major <= 0
        ):
            raise MinuteDataContractError("minute_schema_major_invalid")
        if self.timestamp_semantics not in MinuteTimestampSemantics:
            raise MinuteDataContractError("minute_timestamp_semantics_invalid")
        for field_name in (
            "volume_multiplier_to_shares",
            "amount_multiplier_to_cny",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise MinuteDataContractError(f"minute_{field_name}_invalid")
        if self.price_adjustment != "raw_unadjusted":
            raise MinuteDataContractError(
                "minute_execution_prices_must_be_raw_unadjusted"
            )
        for field_name in (
            "dataset_contract_fingerprint",
            "consumer_profile_sha256",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in _SHA256_HEX for character in value)
            ):
                raise MinuteDataContractError(f"minute_{field_name}_invalid")
        if (self.frequency_field is None) != (self.frequency_value is None):
            raise MinuteDataContractError("minute_frequency_contract_incomplete")
        if self.frequency_field is not None:
            _text(self.frequency_field, "minute_frequency_field_invalid")
            if str(self.frequency_value).strip().lower() not in {
                "5min",
                "5m",
                "5",
            }:
                raise MinuteDataContractError("minute_frequency_must_be_five_minutes")
        for name, value in (
            ("max_pages", self.max_pages),
            ("max_rows", self.max_rows),
            ("page_limit", self.page_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MinuteDataContractError(f"minute_{name}_invalid")
        required = {
            self.symbol_field,
            self.timestamp_field,
            self.open_field,
            self.high_field,
            self.low_field,
            self.close_field,
            self.volume_field,
            self.amount_field,
        }
        if self.previous_close_field is not None:
            required.add(self.previous_close_field)
        if self.suspension_field is not None:
            required.add(self.suspension_field)
        if self.frequency_field is not None:
            required.add(self.frequency_field)
        if not required.issubset(set(self.default_fields)):
            raise MinuteDataContractError("minute_profile_required_fields_missing")
        if not {self.symbol_field, self.timestamp_field}.issubset(
            set(self.identity_fields)
        ):
            raise MinuteDataContractError("minute_profile_identity_incomplete")
        if not set(self.identity_fields).issubset(set(self.default_fields)):
            raise MinuteDataContractError("minute_profile_identity_field_missing")
        filter_fields: set[str] = set()
        for field_name, operators in self.filter_operators:
            if field_name in filter_fields:
                raise MinuteDataContractError("minute_filter_operators_invalid")
            filter_fields.add(field_name)
            _text(field_name, "minute_filter_operators_invalid")
            _strings(operators, "minute_filter_operators_invalid")

    @property
    def catalog_version(self) -> str:
        """Compatibility projection for envelope/audit evidence.

        Global catalog version is never a dataset contract pin.  Consumers must
        report the separately retained expected and observed values instead.
        """

        return self.observed_catalog_version

    @property
    def catalog_version_drift(self) -> bool:
        return self.expected_catalog_version != self.observed_catalog_version

    @classmethod
    def from_catalog(
        cls,
        catalog: CatalogEnvelope,
        *,
        expected_catalog_version: str,
        expected_dataset_contract_fingerprint: str | None = None,
        dataset_id: str,
        identity_fields: tuple[str, ...],
        symbol_field: str,
        timestamp_field: str,
        open_field: str,
        high_field: str,
        low_field: str,
        close_field: str,
        volume_field: str,
        amount_field: str,
        previous_close_field: str | None,
        suspension_field: str | None,
        timestamp_format: str,
        timestamp_semantics: MinuteTimestampSemantics,
        volume_multiplier_to_shares: float,
        amount_multiplier_to_cny: float,
        price_adjustment: str,
        max_pages: int,
        max_rows: int,
        page_limit: int,
        frequency_field: str | None = None,
        frequency_value: str | None = None,
    ) -> "MinuteDatasetProfile":
        """Freeze a profile from one exact formal catalog row.

        The caller supplies only TA domain interpretation.  Dataset ID,
        schema, selectable fields, default order and page-size authority come
        from the returned catalog and are never inferred from a provider name.
        """

        if not isinstance(catalog, CatalogEnvelope):
            raise MinuteDataContractError("minute_catalog_envelope_required")
        _text(expected_catalog_version, "minute_expected_catalog_version_invalid")
        matches = [row for row in catalog.data if row.get("dataset_id") == dataset_id]
        if len(matches) != 1:
            raise MinuteDataContractError("minute_dataset_catalog_row_missing")
        row = matches[0]
        if not _active_catalog_row(row):
            raise MinuteDataContractError("minute_dataset_not_active")
        schema_major = row.get("schema_major")
        if (
            isinstance(schema_major, bool)
            or not isinstance(schema_major, int)
            or schema_major <= 0
        ):
            raise MinuteDataContractError("minute_catalog_schema_major_invalid")
        default_fields = _strings(
            row.get("default_fields"),
            "minute_catalog_default_fields_invalid",
        )
        default_order = _strings(
            row.get("default_order", []),
            "minute_catalog_default_order_invalid",
            nonempty=False,
        )
        declared_identity = _strings(
            identity_fields,
            "minute_profile_identity_fields_invalid",
        )
        catalog_identity = _strings(
            row.get("identity_fields"),
            "minute_catalog_identity_fields_invalid",
        )
        if declared_identity != catalog_identity:
            raise MinuteDataContractError("minute_catalog_identity_mismatch")
        limits = row.get("limits")
        if not isinstance(limits, Mapping):
            raise MinuteDataContractError("minute_catalog_limits_invalid")
        server_page_size = limits.get("max_page_size")
        if (
            isinstance(server_page_size, bool)
            or not isinstance(server_page_size, int)
            or server_page_size <= 0
            or page_limit > server_page_size
        ):
            raise MinuteDataContractError("minute_page_limit_exceeds_catalog")
        server_max_in_values = limits.get("max_in_values")
        if (
            isinstance(server_max_in_values, bool)
            or not isinstance(server_max_in_values, int)
            or server_max_in_values <= 0
            or page_limit > server_max_in_values
        ):
            raise MinuteDataContractError(
                "minute_page_limit_exceeds_catalog_in_values"
            )
        raw_filter_operators = row.get("filter_operators")
        if not isinstance(raw_filter_operators, Mapping):
            raise MinuteDataContractError("minute_catalog_filter_operators_invalid")
        filter_operators: list[tuple[str, tuple[str, ...]]] = []
        for raw_field_name in sorted(raw_filter_operators):
            field_name = _text(
                raw_field_name, "minute_catalog_filter_operators_invalid"
            )
            filter_operators.append(
                (
                    field_name,
                    _strings(
                        raw_filter_operators[raw_field_name],
                        "minute_catalog_filter_operators_invalid",
                    ),
                )
            )
        try:
            contract_fingerprint = dataset_contract_fingerprint(row)
        except (TypeError, ValueError) as exc:
            raise MinuteDataContractError("minute_dataset_contract_invalid") from exc
        if expected_dataset_contract_fingerprint is not None:
            expected_fingerprint = _text(
                expected_dataset_contract_fingerprint,
                "minute_dataset_contract_fingerprint_invalid",
            )
            if expected_fingerprint != contract_fingerprint:
                raise MinuteDataContractError("minute_dataset_contract_drift")
        profile_material = {
            "dataset_contract_fingerprint": contract_fingerprint,
            "selected_fields": list(default_fields),
            "filter_operators": {
                field_name: list(operators)
                for field_name, operators in filter_operators
            },
            "default_order": list(default_order),
            "identity_fields": list(declared_identity),
            "field_mapping": {
                "symbol": symbol_field,
                "timestamp": timestamp_field,
                "open": open_field,
                "high": high_field,
                "low": low_field,
                "close": close_field,
                "volume": volume_field,
                "amount": amount_field,
                "previous_close": previous_close_field,
                "suspension": suspension_field,
                "frequency": frequency_field,
            },
            "frequency_value": frequency_value,
            "timestamp_format": timestamp_format,
            "timestamp_semantics": timestamp_semantics.value,
            "volume_multiplier_to_shares": volume_multiplier_to_shares,
            "amount_multiplier_to_cny": amount_multiplier_to_cny,
            "price_adjustment": price_adjustment,
            "page_budgets": {
                "max_pages": max_pages,
                "max_rows": max_rows,
                "page_limit": page_limit,
            },
        }
        return cls(
            expected_catalog_version=expected_catalog_version,
            observed_catalog_version=catalog.catalog_version,
            dataset_id=dataset_id,
            schema_major=schema_major,
            default_fields=default_fields,
            default_order=default_order,
            filter_operators=tuple(filter_operators),
            dataset_contract_fingerprint=contract_fingerprint,
            consumer_profile_sha256=_sha256(profile_material),
            identity_fields=declared_identity,
            symbol_field=symbol_field,
            timestamp_field=timestamp_field,
            open_field=open_field,
            high_field=high_field,
            low_field=low_field,
            close_field=close_field,
            volume_field=volume_field,
            amount_field=amount_field,
            previous_close_field=previous_close_field,
            suspension_field=suspension_field,
            frequency_field=frequency_field,
            frequency_value=frequency_value,
            timestamp_format=timestamp_format,
            timestamp_semantics=timestamp_semantics,
            volume_multiplier_to_shares=volume_multiplier_to_shares,
            amount_multiplier_to_cny=amount_multiplier_to_cny,
            price_adjustment=price_adjustment,
            max_pages=max_pages,
            max_rows=max_rows,
            page_limit=page_limit,
        )


@dataclass(frozen=True)
class MinuteBarEvidence:
    """One accepted, completed five-minute bar and its envelope proof."""

    symbol: str
    bar_start: datetime
    bar_end: datetime
    open_cny: float
    high_cny: float
    low_cny: float
    close_cny: float
    volume_shares: float
    amount_cny: float
    previous_close_cny: float
    suspended: bool
    market_session: str
    dataset_id: str
    catalog_version: str
    receipt_id: str
    data_through: datetime
    observed_at: datetime
    available_at: datetime
    decision_time: datetime
    source_lineage_sha256: str
    envelope_proof_sha256: str
    source_row_sha256: str
    reference_evidence_sha256: str
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION

    def __post_init__(self) -> None:
        if not is_mainboard_tradable(self.symbol):
            raise MinuteDataContractError("minute_symbol_not_mainboard_tradable")
        for field_name in (
            "dataset_id",
            "catalog_version",
            "receipt_id",
            "market_session",
        ):
            _text(getattr(self, field_name), f"minute_{field_name}_invalid")
        for field_name in (
            "source_lineage_sha256",
            "envelope_proof_sha256",
            "source_row_sha256",
            "reference_evidence_sha256",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in _SHA256_HEX for character in value)
            ):
                raise MinuteDataContractError(f"minute_{field_name}_invalid")
        bar_start = _aware(self.bar_start, "minute_bar_start_timezone_required")
        bar_end = _aware(self.bar_end, "minute_bar_end_timezone_required")
        data_through = _aware(
            self.data_through, "minute_data_through_timezone_required"
        )
        observed = _aware(self.observed_at, "minute_observed_at_timezone_required")
        available = _aware(self.available_at, "minute_available_at_timezone_required")
        decision = _aware(self.decision_time, "minute_decision_time_timezone_required")
        if bar_end - bar_start != FIVE_MINUTES:
            raise MinuteDataContractError("minute_bar_duration_invalid")
        expected_session = _session_for_bar(bar_start, bar_end)
        if self.market_session != expected_session:
            raise MinuteDataContractError("minute_market_session_mismatch")
        if bar_end.astimezone(SHANGHAI).weekday() >= 5:
            raise MinuteDataContractError("minute_weekend_bar_forbidden")
        if not (bar_end <= data_through <= observed <= available <= decision):
            raise MinuteDataContractError("minute_evidence_time_order_invalid")
        if not isinstance(self.evidence_use, MinuteEvidenceUse):
            raise MinuteDataContractError("minute_evidence_use_invalid")
        maximum_latency = None
        if self.evidence_use is MinuteEvidenceUse.LOW_LATENCY_EXECUTION:
            maximum_latency = MAX_MINUTE_DATA_LATENCY
        elif self.evidence_use is MinuteEvidenceUse.DELAYED_PAPER:
            maximum_latency = MAX_DELAYED_PAPER_LATENCY
        if maximum_latency is not None and (
            available - bar_end > maximum_latency
            or decision - bar_end > maximum_latency
        ):
            raise MinuteDataContractError("minute_evidence_latency_exceeded")
        if self.suspended is not False:
            raise MinuteDataContractError("minute_suspended_instrument")
        opening = _finite(self.open_cny, "minute_open_invalid", positive=True)
        high = _finite(self.high_cny, "minute_high_invalid", positive=True)
        low = _finite(self.low_cny, "minute_low_invalid", positive=True)
        close = _finite(self.close_cny, "minute_close_invalid", positive=True)
        _finite(
            self.previous_close_cny,
            "minute_previous_close_invalid",
            positive=True,
        )
        volume = _finite(self.volume_shares, "minute_volume_invalid")
        _finite(self.amount_cny, "minute_amount_invalid")
        if high < max(opening, close, low) or low > min(opening, close, high):
            raise MinuteDataContractError("minute_ohlc_relationship_invalid")
        if volume <= 0:
            raise MinuteDataContractError("minute_zero_volume_not_tradable")

    @property
    def identity(self) -> tuple[str, datetime]:
        return self.symbol, self.bar_end

    @property
    def execution_latency_eligible(self) -> bool:
        return bool(
            self.evidence_use is MinuteEvidenceUse.LOW_LATENCY_EXECUTION
            and self.available_at - self.bar_end <= MAX_MINUTE_DATA_LATENCY
        )

    @property
    def delayed_paper_eligible(self) -> bool:
        return bool(
            self.evidence_use is MinuteEvidenceUse.DELAYED_PAPER
            and self.available_at - self.bar_end <= MAX_DELAYED_PAPER_LATENCY
        )

    def canonical_payload(self) -> dict[str, Any]:
        def stamp(value: datetime) -> str:
            return value.astimezone(timezone.utc).isoformat()

        return {
            "symbol": self.symbol,
            "bar_start": stamp(self.bar_start),
            "bar_end": stamp(self.bar_end),
            "open_cny": self.open_cny,
            "high_cny": self.high_cny,
            "low_cny": self.low_cny,
            "close_cny": self.close_cny,
            "volume_shares": self.volume_shares,
            "amount_cny": self.amount_cny,
            "previous_close_cny": self.previous_close_cny,
            "suspended": self.suspended,
            "market_session": self.market_session,
            "dataset_id": self.dataset_id,
            "catalog_version": self.catalog_version,
            "receipt_id": self.receipt_id,
            "data_through": stamp(self.data_through),
            "observed_at": stamp(self.observed_at),
            "available_at": stamp(self.available_at),
            "decision_time": stamp(self.decision_time),
            "source_lineage_sha256": self.source_lineage_sha256,
            "envelope_proof_sha256": self.envelope_proof_sha256,
            "source_row_sha256": self.source_row_sha256,
            "reference_evidence_sha256": self.reference_evidence_sha256,
            "evidence_use": self.evidence_use.value,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_payload())


@dataclass(frozen=True)
class MinuteReferenceFact:
    """TA-owned daily/reference evidence required by provider-native minute rows."""

    symbol: str
    trade_date: date
    previous_close_cny: float
    suspended: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not is_mainboard_tradable(self.symbol):
            raise MinuteDataContractError(
                "minute_reference_symbol_not_mainboard_tradable"
            )
        if not isinstance(self.trade_date, date) or isinstance(
            self.trade_date, datetime
        ):
            raise MinuteDataContractError("minute_reference_trade_date_invalid")
        _finite(
            self.previous_close_cny,
            "minute_reference_previous_close_invalid",
            positive=True,
        )
        if type(self.suspended) is not bool:
            raise MinuteDataContractError("minute_reference_suspension_invalid")
        if (
            not isinstance(self.evidence_sha256, str)
            or len(self.evidence_sha256) != 64
            or any(character not in _SHA256_HEX for character in self.evidence_sha256)
        ):
            raise MinuteDataContractError("minute_reference_evidence_sha256_invalid")


@dataclass(frozen=True)
class MinuteValidatedProofSummary:
    """Immutable, bounded summary of a validated exact-slot proof cohort."""

    dataset_id: str
    provider: str
    execution_id: str
    config_hash: str
    data_through: str
    receipt_ids: tuple[str, ...]
    content_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("dataset_id", "provider", "execution_id", "data_through"):
            _text(getattr(self, field_name), f"minute_snapshot_proof_{field_name}_invalid")
        for field_name in ("config_hash", "content_sha256"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in _SHA256_HEX for character in value)
            ):
                raise MinuteDataContractError(
                    f"minute_snapshot_proof_{field_name}_invalid"
                )
        if (
            not isinstance(self.receipt_ids, tuple)
            or not self.receipt_ids
            or any(
                not isinstance(receipt_id, str) or not receipt_id.strip()
                for receipt_id in self.receipt_ids
            )
            or len(self.receipt_ids) != len(set(self.receipt_ids))
        ):
            raise MinuteDataContractError("minute_snapshot_proof_receipts_invalid")


@dataclass(frozen=True)
class MinuteBarSnapshot:
    """One replay-proven, bounded set of accepted minute bars."""

    profile: MinuteDatasetProfile
    bars: tuple[MinuteBarEvidence, ...]
    page_count: int
    row_count: int
    pagination_trace_sha256: str
    first_semantic_sha256: str
    replay_semantic_sha256: str
    same_observation: bool
    validated_proof_summary: MinuteValidatedProofSummary | None = None
    fanout_failures: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, MinuteDatasetProfile):
            raise MinuteDataContractError("minute_snapshot_profile_invalid")
        if not self.bars:
            raise MinuteDataContractError("minute_snapshot_empty")
        if self.row_count != len(self.bars):
            raise MinuteDataContractError("minute_snapshot_row_count_mismatch")
        if not (1 <= self.page_count <= self.profile.max_pages):
            raise MinuteDataContractError("minute_snapshot_page_count_invalid")
        identities: dict[tuple[str, datetime], str] = {}
        observed_catalog_version: str | None = None
        for bar in self.bars:
            if not isinstance(bar, MinuteBarEvidence):
                raise MinuteDataContractError("minute_snapshot_bar_invalid")
            if bar.dataset_id != self.profile.dataset_id:
                raise MinuteDataContractError("minute_snapshot_binding_mismatch")
            if observed_catalog_version is None:
                observed_catalog_version = bar.catalog_version
            elif bar.catalog_version != observed_catalog_version:
                raise MinuteDataContractError("minute_query_catalog_version_drift")
            previous = identities.get(bar.identity)
            if previous is not None:
                reason = (
                    "minute_duplicate_bar"
                    if previous == bar.sha256
                    else "minute_conflicting_bar"
                )
                raise MinuteDataContractError(reason)
            identities[bar.identity] = bar.sha256
        for field_name in (
            "pagination_trace_sha256",
            "first_semantic_sha256",
            "replay_semantic_sha256",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in _SHA256_HEX for character in value)
            ):
                raise MinuteDataContractError(f"minute_{field_name}_invalid")
        if self.same_observation is not True:
            raise MinuteDataContractError("minute_same_observation_mismatch")
        if self.first_semantic_sha256 != self.replay_semantic_sha256:
            raise MinuteDataContractError("minute_same_observation_mismatch")
        if self.validated_proof_summary is not None and not isinstance(
            self.validated_proof_summary, MinuteValidatedProofSummary
        ):
            raise MinuteDataContractError("minute_snapshot_proof_summary_invalid")
        if not isinstance(self.fanout_failures, tuple):
            raise MinuteDataContractError("minute_snapshot_fanout_failures_invalid")
        for failure in self.fanout_failures:
            if not isinstance(failure, Mapping):
                raise MinuteDataContractError(
                    "minute_snapshot_fanout_failures_invalid"
                )
            if (
                not isinstance(failure.get("shard_index"), int)
                or isinstance(failure.get("shard_index"), bool)
                or failure.get("shard_index") < 0
                or not isinstance(failure.get("symbol_count"), int)
                or isinstance(failure.get("symbol_count"), bool)
                or failure.get("symbol_count") <= 0
                or not isinstance(failure.get("reason_code"), str)
                or not failure.get("reason_code")
            ):
                raise MinuteDataContractError(
                    "minute_snapshot_fanout_failures_invalid"
                )

    @property
    def sha256(self) -> str:
        return _sha256(
            {
                "profile": {
                    "expected_catalog_version": self.profile.expected_catalog_version,
                    "dataset_id": self.profile.dataset_id,
                    "schema_major": self.profile.schema_major,
                },
                "observed_catalog_version": self.observed_catalog_version,
                "bars": [bar.sha256 for bar in self.bars],
                "page_count": self.page_count,
                "row_count": self.row_count,
                "pagination_trace_sha256": self.pagination_trace_sha256,
                "semantic_sha256": self.first_semantic_sha256,
                "same_observation": True,
                "fanout_failures": list(self.fanout_failures),
            }
        )

    @property
    def observed_catalog_version(self) -> str:
        """Actual catalog/query version for this accepted snapshot."""

        return self.bars[0].catalog_version

    @property
    def catalog_version_drift(self) -> bool:
        return self.profile.expected_catalog_version != self.observed_catalog_version


@dataclass(frozen=True)
class MinuteEvidenceAuditRecord:
    """Audit-only failure record; never eligible for features or execution."""

    reason_code: str
    dataset_id: str
    catalog_version: str
    decision_time: datetime
    rejected_payload_sha256: str
    feature_eligible: bool = False
    candidate_eligible: bool = False
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        _text(self.reason_code, "minute_audit_reason_invalid")
        _text(self.dataset_id, "minute_audit_dataset_invalid")
        _text(self.catalog_version, "minute_audit_catalog_invalid")
        _aware(self.decision_time, "minute_audit_decision_time_invalid")
        if len(self.rejected_payload_sha256) != 64 or any(
            c not in _SHA256_HEX for c in self.rejected_payload_sha256
        ):
            raise MinuteDataContractError("minute_audit_payload_hash_invalid")
        if any(
            (
                self.feature_eligible,
                self.candidate_eligible,
                self.execution_eligible,
            )
        ):
            raise MinuteDataContractError("minute_rejected_evidence_must_be_audit_only")


@dataclass(frozen=True)
class MinuteRowRejection:
    """One row-level quality rejection retained without blocking valid rows."""

    symbol: str
    reason_code: str
    dataset_id: str
    catalog_version: str
    rejected_payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.symbol, "minute_row_rejection_symbol_invalid")
        _text(self.reason_code, "minute_row_rejection_reason_invalid")
        _text(self.dataset_id, "minute_row_rejection_dataset_invalid")
        _text(self.catalog_version, "minute_row_rejection_catalog_invalid")
        if len(self.rejected_payload_sha256) != 64 or any(
            c not in _SHA256_HEX for c in self.rejected_payload_sha256
        ):
            raise MinuteDataContractError("minute_row_rejection_payload_hash_invalid")


class MinuteEvidenceAuditLedger:
    """Idempotent process-local collector for rejected minute evidence."""

    def __init__(self) -> None:
        self._records: dict[str, MinuteEvidenceAuditRecord] = {}
        self._row_rejections: dict[str, MinuteRowRejection] = {}

    def append(self, record: MinuteEvidenceAuditRecord) -> bool:
        if not isinstance(record, MinuteEvidenceAuditRecord):
            raise MinuteDataContractError("minute_audit_record_invalid")
        identity = _sha256(
            {
                "reason": record.reason_code,
                "dataset_id": record.dataset_id,
                "catalog_version": record.catalog_version,
                "decision_time": record.decision_time.astimezone(
                    timezone.utc
                ).isoformat(),
                "payload": record.rejected_payload_sha256,
            }
        )
        previous = self._records.get(identity)
        if previous is None:
            self._records[identity] = record
            return True
        if previous == record:
            return False
        raise MinuteDataContractError("minute_audit_identity_conflict")

    def records(self) -> tuple[MinuteEvidenceAuditRecord, ...]:
        return tuple(self._records.values())

    def append_row_rejection(self, record: MinuteRowRejection) -> bool:
        if not isinstance(record, MinuteRowRejection):
            raise MinuteDataContractError("minute_row_rejection_invalid")
        identity = _sha256(
            {
                "symbol": record.symbol,
                "reason": record.reason_code,
                "dataset_id": record.dataset_id,
                "catalog_version": record.catalog_version,
                "payload": record.rejected_payload_sha256,
            }
        )
        previous = self._row_rejections.get(identity)
        if previous is None:
            self._row_rejections[identity] = record
            return True
        if previous == record:
            return False
        raise MinuteDataContractError("minute_row_rejection_identity_conflict")

    def row_rejections(self) -> tuple[MinuteRowRejection, ...]:
        return tuple(self._row_rejections.values())


class MinuteMarketDataPort(Protocol):
    """Internal TA role port; no transport or provider is implied."""

    def load_snapshot(
        self,
        *,
        profile: MinuteDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        trading_dates: frozenset[date],
        audit_ledger: MinuteEvidenceAuditLedger,
        reference_facts: Mapping[str, MinuteReferenceFact] | None = None,
        evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
        allow_symbol_rejections: bool = False,
    ) -> MinuteBarSnapshot: ...


def _provider_timestamp(
    raw: object,
    *,
    profile: MinuteDatasetProfile,
) -> tuple[datetime, datetime]:
    value = _text(raw, "minute_row_timestamp_invalid")
    try:
        parsed = datetime.strptime(value, profile.timestamp_format)
    except ValueError as exc:
        raise MinuteDataContractError("minute_row_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    else:
        parsed = parsed.astimezone(SHANGHAI)
    if profile.timestamp_semantics is MinuteTimestampSemantics.BAR_END:
        return parsed - FIVE_MINUTES, parsed
    return parsed, parsed + FIVE_MINUTES


_ROW_LEVEL_REJECTION_CODES = frozenset(
    {
        "minute_row_symbol_missing",
        "minute_symbol_not_mainboard_tradable",
        "minute_row_timestamp_invalid",
        "minute_trade_date_not_calendar_eligible",
        "minute_row_frequency_mismatch",
        "minute_reference_fact_missing",
        "minute_reference_symbol_mismatch",
        "minute_reference_trade_date_mismatch",
        "minute_row_suspension_invalid",
        "minute_suspended_instrument",
        "minute_open_invalid",
        "minute_high_invalid",
        "minute_low_invalid",
        "minute_close_invalid",
        "minute_volume_invalid",
        "minute_amount_invalid",
        "minute_previous_close_invalid",
        "minute_ohlc_relationship_invalid",
        "minute_zero_volume_not_tradable",
        "minute_bar_duration_invalid",
        "minute_market_session_mismatch",
        "minute_weekend_bar_forbidden",
    }
)


def _map_run(
    *,
    profile: MinuteDatasetProfile,
    run: PagedQueryRun,
    decision_time: datetime,
    trading_dates: frozenset[date],
    reference_facts: Mapping[str, MinuteReferenceFact] | None,
    evidence_use: MinuteEvidenceUse,
    row_proof_metadata: tuple[Mapping[str, Any], ...] | None = None,
    row_indices: tuple[int, ...] | None = None,
    verify_integrity: bool = True,
) -> tuple[MinuteBarEvidence, ...]:
    # Row-level quarantine validates the whole run once before mapping each
    # row.  Repeating this envelope-wide hash/identity check for every row
    # turns a bounded shard into quadratic work at scale.
    if verify_integrity:
        run.verify_integrity(identity_fields=profile.identity_fields)
    envelope = run.envelope
    metadata = envelope.metadata
    proof_bound = row_proof_metadata is not None
    if envelope.dataset_id != profile.dataset_id:
        raise MinuteDataContractError("minute_query_binding_mismatch")
    historical_display = evidence_use is MinuteEvidenceUse.HISTORICAL_DISPLAY
    state = metadata.state.strip().lower()
    freshness_only_degraded = (
        state == "stale"
        and metadata.degraded is True
        and bool(metadata.reasons)
        and set(metadata.reasons) <= {"freshness_sla_exceeded"}
    )
    delayed_paper_freshness_tolerated = (
        evidence_use is MinuteEvidenceUse.DELAYED_PAPER
        and freshness_only_degraded
    )
    if not proof_bound:
        if historical_display:
            if not (
                (state == "ready" and metadata.degraded is False)
                or freshness_only_degraded
            ):
                raise MinuteDataContractError("minute_metadata_not_displayable")
        else:
            if not (
                (state == "ready" and metadata.degraded is False)
                or delayed_paper_freshness_tolerated
            ):
                raise MinuteDataContractError("minute_metadata_not_ready")
            if not delayed_paper_freshness_tolerated and not _fresh(metadata.freshness):
                raise MinuteDataContractError("minute_metadata_not_fresh")
    quality = metadata.quality
    quality_evidence = quality.get("evidence")
    freshness_only_quality = (
        (historical_display or delayed_paper_freshness_tolerated)
        and freshness_only_degraded
        and quality.get("state") == "degraded"
        and quality.get("valid") is False
        and isinstance(quality_evidence, list)
        and bool(quality_evidence)
        and all(isinstance(item, str) for item in quality_evidence)
        and set(quality_evidence) <= {"freshness_sla_exceeded"}
    )
    if not proof_bound and not _valid_quality(quality) and not freshness_only_quality:
        raise MinuteDataContractError("minute_metadata_quality_invalid")
    if not _complete_lineage(metadata.lineage):
        raise MinuteDataContractError("minute_metadata_lineage_incomplete")
    decision = _aware(decision_time, "minute_decision_time_timezone_required")
    if not proof_bound:
        if not all(
            isinstance(value, str) and bool(value)
            for value in (
                metadata.receipt_id,
                metadata.data_through,
                metadata.observed_at,
            )
        ):
            raise MinuteDataContractError("minute_metadata_proof_incomplete")
        data_through = _parse_aware_iso(
            metadata.data_through, "minute_data_through_invalid"
        )
        observed = _parse_aware_iso(metadata.observed_at, "minute_observed_at_invalid")
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
    bars: list[MinuteBarEvidence] = []
    seen: dict[tuple[str, datetime], str] = {}
    if row_proof_metadata is not None and len(row_proof_metadata) != len(envelope.data):
        raise MinuteDataContractError("minute_exact_slot_receipt_proof_failed")
    if row_proof_metadata:
        data_through = _parse_proof_timestamp(
            row_proof_metadata[0].get("data_through"),
            "minute_exact_slot_receipt_proof_failed",
        )
        observed = _parse_proof_timestamp(
            row_proof_metadata[0].get("finished_at"),
            "minute_exact_slot_receipt_proof_failed",
        )
    indices = (
        tuple(range(len(envelope.data)))
        if row_indices is None
        else row_indices
    )
    if any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0
        or index >= len(envelope.data)
        for index in indices
    ):
        raise MinuteDataContractError("minute_row_indices_invalid")
    for index in indices:
        row = envelope.data[index]
        symbol = _text(
            row.get(profile.symbol_field), "minute_row_symbol_missing"
        ).upper()
        bar_start, bar_end = _provider_timestamp(
            row.get(profile.timestamp_field),
            profile=profile,
        )
        if bar_end.astimezone(SHANGHAI).date() not in trading_dates:
            raise MinuteDataContractError("minute_trade_date_not_calendar_eligible")
        if profile.frequency_field is not None:
            actual_frequency = str(row.get(profile.frequency_field) or "").lower()
            if actual_frequency != str(profile.frequency_value).lower():
                raise MinuteDataContractError("minute_row_frequency_mismatch")
        source_row_sha = _sha256(row)
        row_proof = (
            row_proof_metadata[index] if row_proof_metadata is not None else None
        )
        if row_proof is not None:
            receipt_id = _text(
                row_proof.get("receipt_id"),
                "minute_exact_slot_receipt_proof_failed",
            )
            proof_data_through = _parse_proof_timestamp(
                row_proof.get("data_through"),
                "minute_exact_slot_receipt_proof_failed",
            )
            proof_finished_at = _parse_proof_timestamp(
                row_proof.get("finished_at"),
                "minute_exact_slot_receipt_proof_failed",
            )
            if proof_finished_at > decision or proof_data_through > decision:
                raise MinuteDataContractError(
                    "minute_exact_slot_receipt_proof_failed"
                )
            proof_lineage = _sha256(
                {
                    "dataset_id": envelope.dataset_id,
                    "provider": row_proof.get("provider"),
                    "source": row_proof.get("source"),
                    "execution_id": row_proof.get("execution_id"),
                    "config_hash": row_proof.get("config_hash"),
                    "receipt_id": receipt_id,
                    "data_through": row_proof.get("data_through"),
                    "finished_at": row_proof.get("finished_at"),
                    "receipt_proof_sha256": row_proof.get("receipt_proof_sha256"),
                }
            )
            row_data_through = proof_data_through
            row_observed_at = proof_finished_at
            row_envelope_proof = _sha256(
                {
                    "dataset_id": envelope.dataset_id,
                    "row_identity_sha256": row_proof.get("row_identity_sha256"),
                    "receipt_proof_sha256": row_proof.get("receipt_proof_sha256"),
                    "receipt_id": receipt_id,
                }
            )
            row_lineage = proof_lineage
        else:
            receipt_id = str(metadata.receipt_id)
            row_data_through = data_through
            row_observed_at = observed
            row_envelope_proof = envelope_proof_sha
            row_lineage = lineage_sha
        if profile.previous_close_field is None:
            reference = (
                reference_facts.get(symbol)
                if isinstance(reference_facts, Mapping)
                else None
            )
            if not isinstance(reference, MinuteReferenceFact):
                raise MinuteDataContractError("minute_reference_fact_missing")
            if reference.symbol != symbol:
                raise MinuteDataContractError("minute_reference_symbol_mismatch")
            if reference.trade_date != bar_end.astimezone(SHANGHAI).date():
                raise MinuteDataContractError("minute_reference_trade_date_mismatch")
            previous_close = reference.previous_close_cny
            suspended = reference.suspended
            reference_evidence_sha = reference.evidence_sha256
        else:
            assert profile.suspension_field is not None
            previous_close = row.get(profile.previous_close_field)
            suspended = row.get(profile.suspension_field)
            if type(suspended) is not bool:
                raise MinuteDataContractError("minute_row_suspension_invalid")
            reference_evidence_sha = source_row_sha
        evidence = MinuteBarEvidence(
            symbol=symbol,
            bar_start=bar_start,
            bar_end=bar_end,
            open_cny=_finite(
                row.get(profile.open_field), "minute_open_invalid", positive=True
            ),
            high_cny=_finite(
                row.get(profile.high_field), "minute_high_invalid", positive=True
            ),
            low_cny=_finite(
                row.get(profile.low_field), "minute_low_invalid", positive=True
            ),
            close_cny=_finite(
                row.get(profile.close_field), "minute_close_invalid", positive=True
            ),
            volume_shares=(
                _finite(row.get(profile.volume_field), "minute_volume_invalid")
                * profile.volume_multiplier_to_shares
            ),
            amount_cny=(
                _finite(row.get(profile.amount_field), "minute_amount_invalid")
                * profile.amount_multiplier_to_cny
            ),
            previous_close_cny=_finite(
                previous_close,
                "minute_previous_close_invalid",
                positive=True,
            ),
            suspended=suspended,
            market_session=_session_for_bar(bar_start, bar_end),
            dataset_id=envelope.dataset_id,
            catalog_version=envelope.catalog_version,
            receipt_id=receipt_id,
            data_through=row_data_through,
            observed_at=row_observed_at,
            available_at=row_observed_at,
            decision_time=decision,
            source_lineage_sha256=row_lineage,
            envelope_proof_sha256=row_envelope_proof,
            source_row_sha256=source_row_sha,
            reference_evidence_sha256=reference_evidence_sha,
            evidence_use=evidence_use,
        )
        previous = seen.get(evidence.identity)
        if previous is not None:
            reason = (
                "minute_duplicate_bar"
                if previous == evidence.sha256
                else "minute_conflicting_bar"
            )
            raise MinuteDataContractError(reason)
        seen[evidence.identity] = evidence.sha256
        bars.append(evidence)
    if not bars:
        raise MinuteDataContractError("minute_query_returned_no_bars")
    return tuple(bars)


def _map_run_with_symbol_rejections(
    *,
    profile: MinuteDatasetProfile,
    run: PagedQueryRun,
    decision_time: datetime,
    trading_dates: frozenset[date],
    reference_facts: Mapping[str, MinuteReferenceFact] | None,
    evidence_use: MinuteEvidenceUse,
    audit_ledger: MinuteEvidenceAuditLedger,
    row_proof_metadata: tuple[Mapping[str, Any], ...] | None = None,
) -> tuple[MinuteBarEvidence, ...]:
    """Map valid rows while quarantining only explicitly row-local failures."""

    run.verify_integrity(identity_fields=profile.identity_fields)
    rows = run.envelope.data
    bars: list[MinuteBarEvidence] = []
    for index, row in enumerate(rows):
        try:
            mapped = _map_run(
                profile=profile,
                run=run,
                decision_time=decision_time,
                trading_dates=trading_dates,
                reference_facts=reference_facts,
                evidence_use=evidence_use,
                row_proof_metadata=row_proof_metadata,
                row_indices=(index,),
                verify_integrity=False,
            )
        except MinuteDataContractError as exc:
            if exc.reason_code not in _ROW_LEVEL_REJECTION_CODES:
                raise
            raw_symbol = row.get(profile.symbol_field)
            symbol = (
                raw_symbol.strip().upper()
                if isinstance(raw_symbol, str) and raw_symbol.strip()
                else "<invalid>"
            )
            audit_ledger.append_row_rejection(
                MinuteRowRejection(
                    symbol=symbol,
                    reason_code=exc.reason_code,
                    dataset_id=run.envelope.dataset_id,
                    catalog_version=run.envelope.catalog_version,
                    rejected_payload_sha256=_sha256(
                        {
                            "row": row,
                            "row_index": index,
                            "semantic_sha256": run.semantic_sha256,
                        }
                    ),
                )
            )
            continue
        bars.extend(mapped)
    if not bars:
        raise MinuteDataContractError("minute_query_returned_no_bars")
    return tuple(bars)


def snapshot_from_runs(
    *,
    profile: MinuteDatasetProfile,
    first: PagedQueryRun,
    replay: PagedQueryRun,
    decision_time: datetime,
    trading_dates: frozenset[date],
    audit_ledger: MinuteEvidenceAuditLedger,
    reference_facts: Mapping[str, MinuteReferenceFact] | None = None,
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
    envelope_validator: Callable[[Any], object] | None = None,
    allow_symbol_rejections: bool = False,
) -> MinuteBarSnapshot:
    """Map two bounded reads and require identical same-observation semantics."""

    rejected_payload = {
        "dataset_id": profile.dataset_id,
        "expected_catalog_version": profile.expected_catalog_version,
        "observed_catalog_version": getattr(
            getattr(first, "envelope", None), "catalog_version", None
        ),
        "first_semantic_sha256": getattr(first, "semantic_sha256", None),
        "replay_semantic_sha256": getattr(replay, "semantic_sha256", None),
    }
    observed_catalog_version = "unobserved"
    try:
        observed_catalog_version = _text(
            first.envelope.catalog_version,
            "minute_query_catalog_version_invalid",
        )
        if replay.envelope.catalog_version != observed_catalog_version:
            raise MinuteDataContractError("minute_query_catalog_version_drift")
        if (
            first.semantic_sha256 != replay.semantic_sha256
            or first.semantic_trace_sha256 != replay.semantic_trace_sha256
        ):
            raise MinuteDataContractError("minute_same_observation_mismatch")
        row_proof_metadata: tuple[Mapping[str, Any], ...] | None = None
        if envelope_validator is not None:
            try:
                first_proof_envelope = envelope_validator(first.envelope)
                replay_proof_envelope = envelope_validator(replay.envelope)
            except (SharedSignalsV1Error, ValueError) as exc:
                raise MinuteDataContractError(
                    "minute_exact_slot_receipt_proof_failed"
                ) from exc
            first_proofs = getattr(first_proof_envelope, "row_receipt_proofs", None)
            replay_proofs = getattr(replay_proof_envelope, "row_receipt_proofs", None)
            first_content = getattr(first_proof_envelope, "content_sha256", None)
            replay_content = getattr(replay_proof_envelope, "content_sha256", None)
            if (
                not isinstance(first_proofs, tuple)
                or not isinstance(replay_proofs, tuple)
                or len(first_proofs) != len(first.envelope.data)
                or len(replay_proofs) != len(replay.envelope.data)
                or not isinstance(first_content, str)
                or len(first_content) != 64
                or not isinstance(replay_content, str)
                or len(replay_content) != 64
                or first_content != replay_content
                or _sha256(list(first_proofs)) != _sha256(list(replay_proofs))
                or any(not isinstance(item, Mapping) for item in first_proofs)
                or any(not isinstance(item, Mapping) for item in replay_proofs)
            ):
                raise MinuteDataContractError(
                    "minute_exact_slot_receipt_proof_failed"
                )
            row_proof_metadata = tuple(first_proofs)
        if allow_symbol_rejections:
            bars = _map_run_with_symbol_rejections(
                profile=profile,
                run=first,
                decision_time=decision_time,
                trading_dates=trading_dates,
                reference_facts=reference_facts,
                evidence_use=evidence_use,
                audit_ledger=audit_ledger,
                row_proof_metadata=row_proof_metadata,
            )
            replay_bars = _map_run_with_symbol_rejections(
                profile=profile,
                run=replay,
                decision_time=decision_time,
                trading_dates=trading_dates,
                reference_facts=reference_facts,
                evidence_use=evidence_use,
                audit_ledger=audit_ledger,
                row_proof_metadata=(
                    tuple(getattr(replay_proof_envelope, "row_receipt_proofs"))
                    if envelope_validator is not None
                    else None
                ),
            )
        else:
            bars = _map_run(
                profile=profile,
                run=first,
                decision_time=decision_time,
                trading_dates=trading_dates,
                reference_facts=reference_facts,
                evidence_use=evidence_use,
                row_proof_metadata=row_proof_metadata,
            )
            replay_bars = _map_run(
                profile=profile,
                run=replay,
                decision_time=decision_time,
                trading_dates=trading_dates,
                reference_facts=reference_facts,
                evidence_use=evidence_use,
                row_proof_metadata=(
                    tuple(getattr(replay_proof_envelope, "row_receipt_proofs"))
                    if envelope_validator is not None
                    else None
                ),
            )
        if [bar.sha256 for bar in bars] != [bar.sha256 for bar in replay_bars]:
            raise MinuteDataContractError("minute_same_observation_mismatch")
        proof_summary = None
        if envelope_validator is not None:
            proof_summary = MinuteValidatedProofSummary(
                dataset_id=_text(
                    getattr(first_proof_envelope, "dataset_id", None),
                    "minute_snapshot_proof_dataset_id_invalid",
                ),
                provider=_text(
                    getattr(first_proof_envelope, "provider", None),
                    "minute_snapshot_proof_provider_invalid",
                ),
                execution_id=_text(
                    getattr(first_proof_envelope, "execution_id", None),
                    "minute_snapshot_proof_execution_id_invalid",
                ),
                config_hash=_text(
                    getattr(first_proof_envelope, "config_hash", None),
                    "minute_snapshot_proof_config_hash_invalid",
                ),
                data_through=_text(
                    getattr(first_proof_envelope, "data_through", None),
                    "minute_snapshot_proof_data_through_invalid",
                ),
                receipt_ids=tuple(
                    dict.fromkeys(getattr(first_proof_envelope, "receipt_ids", ()))
                ),
                content_sha256=_text(
                    getattr(first_proof_envelope, "content_sha256", None),
                    "minute_snapshot_proof_content_sha256_invalid",
                ),
            )
        return MinuteBarSnapshot(
            profile=profile,
            bars=bars,
            page_count=first.page_count,
            row_count=len(bars),
            pagination_trace_sha256=first.pagination_trace_sha256,
            first_semantic_sha256=first.semantic_sha256,
            replay_semantic_sha256=replay.semantic_sha256,
            same_observation=True,
            validated_proof_summary=proof_summary,
        )
    except MinuteDataContractError as exc:
        audit_ledger.append(
            MinuteEvidenceAuditRecord(
                reason_code=exc.reason_code,
                dataset_id=profile.dataset_id,
                catalog_version=observed_catalog_version,
                decision_time=_aware(
                    decision_time, "minute_decision_time_timezone_required"
                ),
                rejected_payload_sha256=_sha256(rejected_payload),
            )
        )
        raise


def _minute_symbol_shards(
    *,
    filters: Mapping[str, Any],
    symbol_field: str,
    max_symbols_per_shard: int,
) -> tuple[tuple[str, ...], ...] | None:
    """Return bounded symbol shards for the V1 ``max_in_values`` contract."""

    if (
        isinstance(max_symbols_per_shard, bool)
        or not isinstance(max_symbols_per_shard, int)
        or max_symbols_per_shard <= 0
    ):
        raise MinuteDataContractError("minute_symbol_shard_limit_invalid")

    condition = filters.get(symbol_field)
    if not isinstance(condition, Mapping) or "in" not in condition:
        return None
    raw_symbols = condition["in"]
    if not isinstance(raw_symbols, (list, tuple)):
        return None
    symbols = tuple(raw_symbols)
    if any(
        not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip()
        for symbol in symbols
    ):
        raise MinuteDataContractError("minute_symbol_filter_invalid")
    if len(symbols) <= max_symbols_per_shard:
        return None
    if len(symbols) != len(set(symbols)):
        raise MinuteDataContractError("minute_symbol_filter_duplicate")
    return tuple(
        symbols[index : index + max_symbols_per_shard]
        for index in range(0, len(symbols), max_symbols_per_shard)
    )


class TradingDatasMinuteMarketDataPort:
    """Injected-client adapter for the fixed TradingDatas V1 data plane."""

    def __init__(
        self,
        client: SharedSignalsV1Client,
        *,
        shard_client_factory: Callable[[], SharedSignalsV1Client] | None = None,
    ) -> None:
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        if shard_client_factory is not None and not callable(shard_client_factory):
            raise TypeError("shard_client_factory must be callable")
        self._client = client
        self._shard_client_factory = shard_client_factory

    def _load_sharded_snapshot(
        self,
        *,
        profile: MinuteDatasetProfile,
        filters: Mapping[str, Any],
        shards: tuple[tuple[str, ...], ...],
        decision_time: datetime,
        trading_dates: frozenset[date],
        reference_facts: Mapping[str, MinuteReferenceFact] | None,
        evidence_use: MinuteEvidenceUse,
        envelope_validator: Callable[[Any], object] | None,
        audit_ledger: MinuteEvidenceAuditLedger,
        allow_symbol_rejections: bool,
        load_budget_seconds: float | None = None,
    ) -> MinuteBarSnapshot:
        """Read bounded shards in parallel and retain successful subsets."""

        if envelope_validator is not None:
            raise MinuteDataContractError("minute_fanout_receipt_proof_unsupported")
        if load_budget_seconds is None:
            load_budget_seconds = MINUTE_SNAPSHOT_LOAD_BUDGET_SECONDS
        if not math.isfinite(load_budget_seconds):
            raise MinuteDataContractError("minute_snapshot_load_budget_invalid")
        load_deadline = _monotonic() + load_budget_seconds
        worker_count = min(MAX_MINUTE_FANOUT_WORKERS, len(shards))
        scheduling_lock = threading.Lock()
        started_shards = 0

        # The production bearer transport is deliberately single-flight per
        # client. Keep the bounded fanout parallel, but give each executor
        # worker its own client/transport instead of sharing one transport
        # across threads. A client is cached on the worker so its observed
        # catalog version remains bound for both the first read and replay.
        worker_state = threading.local()

        def query_shard(
            index: int,
            symbols: tuple[str, ...],
        ) -> tuple[
            int,
            PagedQueryRun | None,
            PagedQueryRun | None,
            dict[str, object] | None,
        ]:
            nonlocal started_shards
            shard_filters = dict(filters)
            shard_filters[profile.symbol_field] = {"in": list(symbols)}
            page_limit = min(profile.page_limit, len(symbols))
            shard_pages = max(1, (len(symbols) + page_limit - 1) // page_limit)
            request = QueryRequest(
                dataset_id=profile.dataset_id,
                schema_major=profile.schema_major,
                fields=profile.default_fields,
                filters=shard_filters,
                order=profile.default_order or None,
                limit=page_limit,
                include_receipt_proofs=False,
            )
            phase = "query"
            try:
                _minute_read_budget_remaining(load_deadline)
                shard_client = getattr(worker_state, "client", None)
                source = shard_client
                if source is None:
                    source = (
                        self._client if self._shard_client_factory is None
                        else self._shard_client_factory()
                    )
                    if not isinstance(source, SharedSignalsV1Client):
                        raise MinuteDataContractError("minute_shard_client_factory_invalid")
                # Reserve time for queued waves instead of letting the first
                # retry chains consume the entire load budget. Keep at least
                # the existing client timeout as a read quantum: hard fair
                # shares alone can discard otherwise healthy catalog/pairs.
                with scheduling_lock:
                    now = _monotonic()
                    remaining_shards = len(shards) - started_shards
                    started_shards += 1
                    fair_budget = max(0.0, load_deadline - now) * (
                        worker_count / max(worker_count, remaining_shards)
                    )
                    worker_state.deadline = min(
                        load_deadline,
                        now + max(source.config.timeout_seconds, fair_budget),
                    )
                _minute_read_budget_remaining(worker_state.deadline)
                if shard_client is None:
                    shard_client = _minute_deadline_client(
                        source, deadline=lambda: worker_state.deadline,
                    )
                    if self._shard_client_factory is not None:
                        phase = "catalog"
                        try:
                            shard_client.get_catalog()
                        except SharedSignalsV1Error as exc:
                            if not _is_retryable_minute_query_error(exc):
                                raise _marked_request_failure(exc, phase="catalog") from exc
                            raise
                    worker_state.client = shard_client
                phase = "query"
                first, replay = _collect_stable_minute_pair(
                    client=shard_client,
                    request=request,
                    identity_fields=profile.identity_fields,
                    max_pages=shard_pages,
                    max_rows=len(symbols),
                    deadline=worker_state.deadline,
                )
                return index, first, replay, None
            except PaginationContractError:
                raise
            except MinuteSnapshotLoadBudgetExhausted as exc:
                # A spent load budget degrades the shard exactly like a
                # failed request: partial snapshots keep their successful
                # subsets and an all-exhausted load surfaces this typed
                # reason instead of burning the unit's systemd budget.
                return index, None, None, {
                    "shard_index": index,
                    "symbol_count": len(symbols),
                    "reason_code": exc.reason_code,
                    "failure_stage": f"{phase}_request",
                    "failure_class": exc.failure_class,
                }
            except (SharedSignalsV1Error, OSError) as exc:
                if not _is_retryable_minute_query_error(exc):
                    raise
                marked = _marked_request_failure(exc, phase=phase)
                return index, None, None, {
                    "shard_index": index,
                    "symbol_count": len(symbols),
                    "reason_code": marked.reason_code,
                    "failure_stage": marked.failure_stage,
                    "failure_class": marked.failure_class,
                }

        results: dict[
            int,
            tuple[
                PagedQueryRun | None,
                PagedQueryRun | None,
                dict[str, object] | None,
            ],
        ] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(query_shard, index, symbols): index
                for index, symbols in enumerate(shards)
            }
            for future in as_completed(futures):
                index, first, replay, failure = future.result()
                results[index] = (first, replay, failure)

        first_runs: list[PagedQueryRun] = []
        replay_runs: list[PagedQueryRun] = []
        fanout_failures: list[Mapping[str, object]] = []
        for index in range(len(shards)):
            first, replay, failure = results[index]
            if failure is not None:
                fanout_failures.append(failure)
                continue
            if first is None or replay is None:
                raise MinuteDataContractError("minute_fanout_replay_invalid")
            first_runs.append(first)
            replay_runs.append(replay)

        if len(first_runs) != len(replay_runs) or not first_runs:
            failure = fanout_failures[0] if fanout_failures else {}
            raise MinuteDataContractError(
                str(failure.get("reason_code", "minute_fanout_replay_invalid"))
            )
        versions = {
            run.envelope.catalog_version
            for run in (*first_runs, *replay_runs)
        }
        if len(versions) != 1:
            raise MinuteDataContractError("minute_query_catalog_version_drift")

        bars: list[MinuteBarEvidence] = []
        first_semantics: list[str] = []
        replay_semantics: list[str] = []
        first_traces: list[str] = []
        replay_traces: list[str] = []
        page_count = 0
        for first, replay in zip(first_runs, replay_runs, strict=True):
            if (
                first.semantic_sha256 != replay.semantic_sha256
                or first.semantic_trace_sha256 != replay.semantic_trace_sha256
            ):
                raise MinuteDataContractError("minute_same_observation_mismatch")
            try:
                if allow_symbol_rejections:
                    first_bars = _map_run_with_symbol_rejections(
                        profile=profile,
                        run=first,
                        decision_time=decision_time,
                        trading_dates=trading_dates,
                        reference_facts=reference_facts,
                        evidence_use=evidence_use,
                        audit_ledger=audit_ledger,
                    )
                    replay_bars = _map_run_with_symbol_rejections(
                        profile=profile,
                        run=replay,
                        decision_time=decision_time,
                        trading_dates=trading_dates,
                        reference_facts=reference_facts,
                        evidence_use=evidence_use,
                        audit_ledger=audit_ledger,
                    )
                else:
                    first_bars = _map_run(
                        profile=profile,
                        run=first,
                        decision_time=decision_time,
                        trading_dates=trading_dates,
                        reference_facts=reference_facts,
                        evidence_use=evidence_use,
                    )
                    replay_bars = _map_run(
                        profile=profile,
                        run=replay,
                        decision_time=decision_time,
                        trading_dates=trading_dates,
                        reference_facts=reference_facts,
                        evidence_use=evidence_use,
                    )
            except MinuteDataContractError as exc:
                if not (
                    allow_symbol_rejections
                    and exc.reason_code == "minute_query_returned_no_bars"
                ):
                    raise
                continue
            if [bar.sha256 for bar in first_bars] != [bar.sha256 for bar in replay_bars]:
                raise MinuteDataContractError("minute_same_observation_mismatch")
            bars.extend(first_bars)
            first_semantics.append(first.semantic_sha256)
            replay_semantics.append(replay.semantic_sha256)
            first_traces.append(first.pagination_trace_sha256)
            replay_traces.append(replay.pagination_trace_sha256)
            page_count += first.page_count

        aggregate_semantic = _sha256(first_semantics)
        return MinuteBarSnapshot(
            profile=profile,
            bars=tuple(sorted(bars, key=lambda bar: bar.identity)),
            page_count=page_count,
            row_count=len(bars),
            pagination_trace_sha256=_sha256(
                {
                    "first": first_traces,
                    "replay": replay_traces,
                }
            ),
            first_semantic_sha256=aggregate_semantic,
            replay_semantic_sha256=_sha256(replay_semantics),
            same_observation=True,
            fanout_failures=tuple(fanout_failures),
        )

    def load_snapshot(
        self,
        *,
        profile: MinuteDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        trading_dates: frozenset[date],
        audit_ledger: MinuteEvidenceAuditLedger,
        reference_facts: Mapping[str, MinuteReferenceFact] | None = None,
        evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
        include_receipt_proofs: bool = False,
        envelope_validator: Callable[[Any], object] | None = None,
        allow_symbol_rejections: bool = False,
        load_budget_seconds: float | None = None,
    ) -> MinuteBarSnapshot:
        audit_count_before = len(audit_ledger.records())
        runtime_catalog_version = "unobserved"
        try:
            if self._client.config.catalog_version_policy != "evidence_only":
                raise MinuteDataContractError("minute_catalog_version_policy_invalid")
            try:
                catalog = self._client.get_catalog()
            except SharedSignalsV1Error as exc:
                raise _marked_request_failure(exc, phase="catalog") from exc
            runtime_catalog_version = catalog.catalog_version
            matches = [
                item
                for item in catalog.data
                if item.get("dataset_id") == profile.dataset_id
            ]
            if len(matches) != 1:
                raise MinuteDataContractError("minute_dataset_catalog_row_missing")
            row = matches[0]
            if not _active_catalog_row(row):
                raise MinuteDataContractError("minute_dataset_not_active")
            try:
                current_fingerprint = dataset_contract_fingerprint(row)
            except (TypeError, ValueError) as exc:
                raise MinuteDataContractError(
                    "minute_dataset_contract_invalid"
                ) from exc
            if current_fingerprint != profile.dataset_contract_fingerprint:
                raise MinuteDataContractError("minute_dataset_contract_drift")
            filter_contract = dict(profile.filter_operators)
            for field_name, condition in filters.items():
                if (
                    field_name not in filter_contract
                    or not isinstance(condition, Mapping)
                    or not condition
                    or any(
                        operator not in filter_contract[field_name]
                        for operator in condition
                    )
                ):
                    raise MinuteDataContractError(
                        "minute_query_filter_not_catalog_authorized"
                    )
            symbol_shards = _minute_symbol_shards(
                filters=filters,
                symbol_field=profile.symbol_field,
                max_symbols_per_shard=profile.page_limit,
            )
            if symbol_shards is not None:
                return self._load_sharded_snapshot(
                    profile=profile,
                    filters=filters,
                    shards=symbol_shards,
                    decision_time=decision_time,
                    trading_dates=trading_dates,
                    reference_facts=reference_facts,
                    evidence_use=evidence_use,
                    envelope_validator=envelope_validator,
                    audit_ledger=audit_ledger,
                    allow_symbol_rejections=allow_symbol_rejections,
                    load_budget_seconds=load_budget_seconds,
                )
            request = QueryRequest(
                dataset_id=profile.dataset_id,
                schema_major=profile.schema_major,
                fields=profile.default_fields,
                filters=filters,
                order=profile.default_order or None,
                limit=profile.page_limit,
                include_receipt_proofs=include_receipt_proofs,
            )
            try:
                first, replay = _collect_stable_minute_pair(
                    client=self._client,
                    request=request,
                    identity_fields=profile.identity_fields,
                    max_pages=profile.max_pages,
                    max_rows=profile.max_rows,
                )
            except PaginationContractError:
                raise
            except SharedSignalsV1Error as exc:
                raise _marked_request_failure(exc, phase="query") from exc
            return snapshot_from_runs(
                profile=profile,
                first=first,
                replay=replay,
                decision_time=decision_time,
                trading_dates=trading_dates,
                audit_ledger=audit_ledger,
                reference_facts=reference_facts,
                evidence_use=evidence_use,
                envelope_validator=envelope_validator,
                allow_symbol_rejections=allow_symbol_rejections,
            )
        except MinuteDataContractError as exc:
            if len(audit_ledger.records()) == audit_count_before:
                audit_ledger.append(
                    MinuteEvidenceAuditRecord(
                        reason_code=exc.reason_code,
                        dataset_id=profile.dataset_id,
                        catalog_version=runtime_catalog_version,
                        decision_time=_aware(
                            decision_time,
                            "minute_decision_time_timezone_required",
                        ),
                        rejected_payload_sha256=_sha256(
                            {
                                "failure_class": "minute_contract",
                                "reason_code": exc.reason_code,
                                "dataset_id": profile.dataset_id,
                            }
                        ),
                    )
                )
            raise
        except PaginationContractError as exc:
            reason = str(exc) or "minute_pagination_contract_failed"
            audit_ledger.append(
                MinuteEvidenceAuditRecord(
                    reason_code=reason,
                    dataset_id=profile.dataset_id,
                    catalog_version=runtime_catalog_version,
                    decision_time=_aware(
                        decision_time, "minute_decision_time_timezone_required"
                    ),
                    rejected_payload_sha256=_sha256(
                        {
                            "failure_class": "pagination",
                            "reason_code": reason,
                            "dataset_id": profile.dataset_id,
                        }
                    ),
                )
            )
            raise MinuteDataContractError(
                reason,
                failure_stage="pagination",
                failure_class="PaginationContractError",
            ) from exc
        except (SharedSignalsV1Error, OSError) as exc:
            reason = "minute_tradingdatas_request_failed"
            audit_ledger.append(
                MinuteEvidenceAuditRecord(
                    reason_code=reason,
                    dataset_id=profile.dataset_id,
                    catalog_version=runtime_catalog_version,
                    decision_time=_aware(
                        decision_time, "minute_decision_time_timezone_required"
                    ),
                    rejected_payload_sha256=_sha256(
                        {
                            "failure_class": _bounded_failure_class(exc),
                            "dataset_id": profile.dataset_id,
                        }
                    ),
                )
            )
            raise MinuteDataContractError(
                reason,
                failure_stage="unknown",
                failure_class=_bounded_failure_class(exc),
            ) from exc


__all__ = [
    "FIXED_CATALOG_ROUTE",
    "FIXED_QUERY_ROUTE",
    "FIVE_MINUTES",
    "MAX_DELAYED_PAPER_LATENCY",
    "MAX_MINUTE_DATA_LATENCY",
    "MinuteBarEvidence",
    "MinuteBarSnapshot",
    "MinuteDataContractError",
    "MinuteDatasetProfile",
    "MinuteEvidenceAuditLedger",
    "MinuteEvidenceAuditRecord",
    "MinuteRowRejection",
    "MinuteEvidenceUse",
    "MinuteMarketDataPort",
    "MinuteReferenceFact",
    "MinuteValidatedProofSummary",
    "MinuteTimestampSemantics",
    "TradingDatasMinuteMarketDataPort",
    "snapshot_from_runs",
]
