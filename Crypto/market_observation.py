"""Read-only health observation for the TradingDatas Crypto 5-minute cohort.

This module deliberately sits outside the BTC/ETH delayed-paper capital path.
It expands *data* observation to the currently collected ten-symbol cohort,
but has no capital, order, model, ledger, timer, or promotion authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping

from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
)
from shared.data.tradingdatas_pagination import PagedQueryRun, collect_query_pages


OBSERVATION_CONTRACT = "tradingagent.crypto.market_observation.v1"
FIVE_MINUTES = timedelta(minutes=5)
BAR_COUNT = 13
BAR_FIELDS = (
    "symbol",
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
)
OBSERVATION_SYMBOLS = (
    "ADAUSDT",
    "AVAXUSDT",
    "BNBUSDT",
    "BTCUSDT",
    "DOGEUSDT",
    "ETHUSDT",
    "LINKUSDT",
    "SOLUSDT",
    "TRXUSDT",
    "XRPUSDT",
)


class CryptoMarketObservationError(ValueError):
    """A fail-closed reason safe to surface without a payload or token."""


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CryptoMarketObservationError("crypto_observation_not_canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime, reason: str, *, aligned: bool = False) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CryptoMarketObservationError(reason)
    normalized = value.astimezone(timezone.utc)
    if aligned and (
        normalized.second != 0
        or normalized.microsecond != 0
        or normalized.minute % 5 != 0
    ):
        raise CryptoMarketObservationError(reason)
    return normalized


def _parse_utc(value: object, reason: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CryptoMarketObservationError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoMarketObservationError(reason) from exc
    return _utc(parsed, reason)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: object, reason: str, *, positive: bool) -> Decimal:
    if not isinstance(value, str):
        raise CryptoMarketObservationError(reason)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CryptoMarketObservationError(reason) from exc
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        raise CryptoMarketObservationError(reason)
    return parsed


def _complete_lineage(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    providers = value.get("providers")
    return bool(
        value.get("complete") is True
        and value.get("provider_neutral") is True
        and isinstance(providers, list)
        and providers
        and all(isinstance(item, str) and item.strip() for item in providers)
        and isinstance(value.get("transport_service"), str)
        and value.get("transport_service", "").strip()
    )


def _bar_dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


def _catalog_row(catalog: CatalogEnvelope, dataset_id: str) -> Mapping[str, Any]:
    matches = [row for row in catalog.data if row.get("dataset_id") == dataset_id]
    if len(matches) != 1:
        raise CryptoMarketObservationError("crypto_observation_catalog_row_missing")
    return matches[0]


def _verify_catalog(catalog: CatalogEnvelope, dataset_id: str) -> None:
    row = _catalog_row(catalog, dataset_id)
    if row.get("schema_major") != 1:
        raise CryptoMarketObservationError("crypto_observation_schema_major_drift")
    if tuple(row.get("default_fields") or ()) != BAR_FIELDS:
        raise CryptoMarketObservationError("crypto_observation_fields_drift")
    if tuple(row.get("identity_fields") or ()) != ("symbol", "open_time"):
        raise CryptoMarketObservationError("crypto_observation_identity_drift")
    availability = row.get("availability")
    queryability = row.get("queryability")
    if not (
        isinstance(availability, Mapping)
        and "active" in availability.get("entitlement_states", [])
        and "active" in availability.get("activation_states", [])
        and isinstance(queryability, Mapping)
        and queryability.get("queryable") is True
    ):
        raise CryptoMarketObservationError("crypto_observation_dataset_not_queryable")
    filters = row.get("filter_operators")
    if not (
        isinstance(filters, Mapping)
        and "eq" in filters.get("symbol", [])
        and "between" in filters.get("open_time", [])
    ):
        raise CryptoMarketObservationError("crypto_observation_filters_drift")
    limits = row.get("limits")
    if not isinstance(limits, Mapping) or limits.get("max_page_size", 0) < BAR_COUNT:
        raise CryptoMarketObservationError("crypto_observation_page_limit_invalid")


@dataclass(frozen=True)
class CryptoObservationWindow:
    """One exact, already-closed 13-bar UTC observation window."""

    window_end: datetime
    observation_cutoff: datetime

    def __post_init__(self) -> None:
        end = _utc(
            self.window_end, "crypto_observation_window_end_invalid", aligned=True
        )
        cutoff = _utc(self.observation_cutoff, "crypto_observation_cutoff_invalid")
        if cutoff < end:
            raise CryptoMarketObservationError(
                "crypto_observation_cutoff_precedes_window"
            )
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "observation_cutoff", cutoff)

    @property
    def first_open_time(self) -> datetime:
        return self.window_end - BAR_COUNT * FIVE_MINUTES

    @property
    def last_open_time(self) -> datetime:
        return self.window_end - FIVE_MINUTES


@dataclass(frozen=True)
class CryptoObservationSource:
    symbol: str
    dataset_id: str
    row_count: int
    page_count: int
    receipt_id: str
    data_through: datetime
    observed_at: datetime
    semantic_sha256: str
    pagination_trace_sha256: str

    def to_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "dataset_id": self.dataset_id,
            "row_count": self.row_count,
            "page_count": self.page_count,
            "receipt_id": self.receipt_id,
            "data_through": _iso(self.data_through),
            "observed_at": _iso(self.observed_at),
            "semantic_sha256": self.semantic_sha256,
            "pagination_trace_sha256": self.pagination_trace_sha256,
        }


@dataclass(frozen=True)
class CryptoMarketObservation:
    catalog_version: str
    window: CryptoObservationWindow
    sources: tuple[CryptoObservationSource, ...]
    observation_sha256: str
    authority: str = "none"
    execution_eligible: bool = False
    capital_write_eligible: bool = False
    model_authority: bool = False

    def __post_init__(self) -> None:
        if self.authority != "none" or any(
            value is not False
            for value in (
                self.execution_eligible,
                self.capital_write_eligible,
                self.model_authority,
            )
        ):
            raise CryptoMarketObservationError("crypto_observation_authority_invalid")
        if tuple(source.symbol for source in self.sources) != OBSERVATION_SYMBOLS:
            raise CryptoMarketObservationError("crypto_observation_sources_incomplete")
        expected = _canonical_sha256(self.to_payload(include_digest=False))
        if self.observation_sha256 != expected:
            raise CryptoMarketObservationError("crypto_observation_digest_invalid")

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract": OBSERVATION_CONTRACT,
            "catalog_version": self.catalog_version,
            "window_end": _iso(self.window.window_end),
            "observation_cutoff": _iso(self.window.observation_cutoff),
            "sources": [source.to_payload() for source in self.sources],
            "authority": self.authority,
            "execution_eligible": self.execution_eligible,
            "capital_write_eligible": self.capital_write_eligible,
            "model_authority": self.model_authority,
        }
        if include_digest:
            payload["observation_sha256"] = self.observation_sha256
        return payload


def _validate_run(
    run: PagedQueryRun,
    *,
    symbol: str,
    dataset_id: str,
    window: CryptoObservationWindow,
) -> CryptoObservationSource:
    envelope = run.envelope
    metadata = envelope.metadata
    if (
        envelope.dataset_id != dataset_id
        or envelope.api_version != "v1"
        or run.page_count != 1
        or run.row_count != BAR_COUNT
        or envelope.next_cursor is not None
    ):
        raise CryptoMarketObservationError("crypto_observation_query_shape_invalid")
    if (
        metadata.state.lower() != "ready"
        or metadata.degraded is not False
        or metadata.freshness.get("state") != "fresh"
        or metadata.freshness.get("stale") is not False
        or metadata.quality.get("state") != "valid"
        or not _complete_lineage(metadata.lineage)
        or not isinstance(metadata.receipt_id, str)
        or not metadata.receipt_id
    ):
        raise CryptoMarketObservationError("crypto_observation_metadata_invalid")
    data_through = _parse_utc(
        metadata.data_through, "crypto_observation_data_through_invalid"
    )
    observed_at = _parse_utc(
        metadata.observed_at, "crypto_observation_observed_at_invalid"
    )
    if data_through > observed_at or observed_at > window.observation_cutoff:
        raise CryptoMarketObservationError("crypto_observation_watermark_invalid")

    expected_open = window.first_open_time
    for row in envelope.data:
        if set(row) != set(BAR_FIELDS) or row.get("symbol") != symbol:
            raise CryptoMarketObservationError("crypto_observation_row_shape_invalid")
        open_time = _parse_utc(
            row.get("open_time"), "crypto_observation_open_time_invalid"
        )
        close_time = _parse_utc(
            row.get("close_time"), "crypto_observation_close_time_invalid"
        )
        if (
            open_time != expected_open
            or close_time != open_time + FIVE_MINUTES - timedelta(milliseconds=1)
        ):
            raise CryptoMarketObservationError(
                "crypto_observation_bar_continuity_invalid"
            )
        open_price = _decimal(
            row.get("open"), "crypto_observation_ohlc_invalid", positive=True
        )
        high = _decimal(
            row.get("high"), "crypto_observation_ohlc_invalid", positive=True
        )
        low = _decimal(row.get("low"), "crypto_observation_ohlc_invalid", positive=True)
        close = _decimal(
            row.get("close"), "crypto_observation_ohlc_invalid", positive=True
        )
        _decimal(row.get("volume"), "crypto_observation_volume_invalid", positive=False)
        _decimal(
            row.get("quote_volume"), "crypto_observation_volume_invalid", positive=False
        )
        if not isinstance(row.get("trade_count"), int) or row["trade_count"] < 0:
            raise CryptoMarketObservationError("crypto_observation_trade_count_invalid")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise CryptoMarketObservationError("crypto_observation_ohlc_invalid")
        expected_open += FIVE_MINUTES
    if data_through < window.last_open_time + FIVE_MINUTES - timedelta(milliseconds=1):
        raise CryptoMarketObservationError("crypto_observation_data_through_early")
    return CryptoObservationSource(
        symbol=symbol,
        dataset_id=dataset_id,
        row_count=run.row_count,
        page_count=run.page_count,
        receipt_id=metadata.receipt_id,
        data_through=data_through,
        observed_at=observed_at,
        semantic_sha256=run.semantic_sha256,
        pagination_trace_sha256=run.pagination_trace_sha256,
    )


def collect_market_observation(
    client: SharedSignalsV1Client,
    *,
    expected_catalog_version: str,
    window: CryptoObservationWindow,
) -> CryptoMarketObservation:
    """Return a zero-authority ten-symbol read-only observation.

    The function intentionally does not replay, persist, schedule, or invoke
    any capital/model path. A caller may compare repeated reports separately.
    """

    if not isinstance(client, SharedSignalsV1Client):
        raise TypeError("client must be a SharedSignalsV1Client")
    catalog = client.get_catalog()
    if (
        catalog.api_version != "v1"
        or catalog.catalog_version != expected_catalog_version
    ):
        raise CryptoMarketObservationError("crypto_observation_catalog_version_drift")
    sources: list[CryptoObservationSource] = []
    for symbol in OBSERVATION_SYMBOLS:
        dataset_id = _bar_dataset_id(symbol)
        _verify_catalog(catalog, dataset_id)
        run = collect_query_pages(
            client=client,
            request=QueryRequest(
                dataset_id=dataset_id,
                schema_major=1,
                fields=BAR_FIELDS,
                filters={
                    "symbol": {"eq": symbol},
                    "open_time": {
                        "between": [
                            _iso(window.first_open_time),
                            _iso(window.last_open_time),
                        ]
                    },
                },
                # This is a current health observation, not a historical/PIT
                # read.  The formal ten-symbol 18083 contract supports the
                # exact bounded current window with as_of omitted.
                order=("symbol:asc", "open_time:asc"),
                limit=BAR_COUNT,
            ),
            identity_fields=("symbol", "open_time"),
            max_pages=1,
            max_rows=BAR_COUNT,
        )
        sources.append(
            _validate_run(run, symbol=symbol, dataset_id=dataset_id, window=window)
        )
    payload = {
        "contract": OBSERVATION_CONTRACT,
        "catalog_version": expected_catalog_version,
        "window_end": _iso(window.window_end),
        "observation_cutoff": _iso(window.observation_cutoff),
        "sources": [source.to_payload() for source in sources],
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }
    return CryptoMarketObservation(
        catalog_version=expected_catalog_version,
        window=window,
        sources=tuple(sources),
        observation_sha256=_canonical_sha256(payload),
    )


__all__ = [
    "BAR_COUNT",
    "BAR_FIELDS",
    "OBSERVATION_CONTRACT",
    "OBSERVATION_SYMBOLS",
    "CryptoMarketObservation",
    "CryptoMarketObservationError",
    "CryptoObservationSource",
    "CryptoObservationWindow",
    "collect_market_observation",
]
