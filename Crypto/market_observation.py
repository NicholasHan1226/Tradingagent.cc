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


def _wire_iso(value: datetime) -> str:
    """Use the formal current-query RFC3339 offset spelling on the wire."""

    return value.astimezone(timezone.utc).isoformat()


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
    identity_sha256: str
    market_data_sha256: str
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
            "identity_sha256": self.identity_sha256,
            "market_data_sha256": self.market_data_sha256,
            "semantic_sha256": self.semantic_sha256,
            "pagination_trace_sha256": self.pagination_trace_sha256,
        }

    def to_market_data_payload(self) -> dict[str, object]:
        """The row-level replay identity, deliberately excluding mutable receipts."""

        return {
            "symbol": self.symbol,
            "dataset_id": self.dataset_id,
            "row_count": self.row_count,
            "identity_sha256": self.identity_sha256,
            "market_data_sha256": self.market_data_sha256,
        }


@dataclass(frozen=True)
class CryptoMarketObservation:
    catalog_version: str
    window: CryptoObservationWindow
    sources: tuple[CryptoObservationSource, ...]
    market_data_sha256: str
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
        expected_market_data = _canonical_sha256(self.to_market_data_payload())
        if self.market_data_sha256 != expected_market_data:
            raise CryptoMarketObservationError(
                "crypto_observation_market_data_digest_invalid"
            )
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
            "market_data_sha256": self.market_data_sha256,
            "authority": self.authority,
            "execution_eligible": self.execution_eligible,
            "capital_write_eligible": self.capital_write_eligible,
            "model_authority": self.model_authority,
        }
        if include_digest:
            payload["observation_sha256"] = self.observation_sha256
        return payload

    def to_market_data_payload(self) -> dict[str, object]:
        """Return row/identity evidence suitable for current replay checks.

        Current-query receipts may advance between otherwise identical reads. Their
        receipt and observation timestamps remain part of the full evidence digest,
        but are not mistaken for market-row drift.
        """

        return {
            "contract": OBSERVATION_CONTRACT,
            "catalog_version": self.catalog_version,
            "window_end": _iso(self.window.window_end),
            "sources": [source.to_market_data_payload() for source in self.sources],
        }


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
        identity_sha256=run.identity_sha256,
        market_data_sha256=run.ordered_rows_sha256,
        semantic_sha256=run.semantic_sha256,
        pagination_trace_sha256=run.pagination_trace_sha256,
    )


def _collect_market_observation_rows_with_catalog(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    expected_catalog_version: str,
    window: CryptoObservationWindow,
) -> tuple[CryptoMarketObservation, dict[str, list[dict[str, Any]]]]:
    """Collect the cohort and also return the validated raw bar rows.

    The rows never enter any observation digest field; they are returned so
    the accumulator runtime can persist them as an immutable bars sidecar
    whose digests re-derive exactly the per-source ``identity_sha256`` and
    ``market_data_sha256`` already bound in the observation evidence.
    """

    if (
        catalog.api_version != "v1"
        or catalog.catalog_version != expected_catalog_version
    ):
        raise CryptoMarketObservationError("crypto_observation_catalog_version_drift")
    sources: list[CryptoObservationSource] = []
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
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
                            _wire_iso(window.first_open_time),
                            _wire_iso(window.last_open_time),
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
        rows_by_symbol[symbol] = [dict(row) for row in run.envelope.data]
    sources_tuple = tuple(sources)
    market_data_payload = {
        "contract": OBSERVATION_CONTRACT,
        "catalog_version": expected_catalog_version,
        "window_end": _iso(window.window_end),
        "sources": [source.to_market_data_payload() for source in sources_tuple],
    }
    market_data_sha256 = _canonical_sha256(market_data_payload)
    payload = {
        "contract": OBSERVATION_CONTRACT,
        "catalog_version": expected_catalog_version,
        "window_end": _iso(window.window_end),
        "observation_cutoff": _iso(window.observation_cutoff),
        "sources": [source.to_payload() for source in sources_tuple],
        "market_data_sha256": market_data_sha256,
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }
    return CryptoMarketObservation(
        catalog_version=expected_catalog_version,
        window=window,
        sources=sources_tuple,
        market_data_sha256=market_data_sha256,
        observation_sha256=_canonical_sha256(payload),
    ), rows_by_symbol


def _collect_market_observation_with_catalog(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    expected_catalog_version: str,
    window: CryptoObservationWindow,
) -> CryptoMarketObservation:
    """Collect the cohort against an already-observed catalog envelope.

    The accumulator runtime fetches the catalog once per cycle so the frozen
    profile fingerprints and the query loop share a single catalog read.
    The validated bar rows are deliberately dropped here; callers that need
    them for the immutable bars sidecar use the row-returning variant.
    """

    observation, _ = _collect_market_observation_rows_with_catalog(
        client,
        catalog=catalog,
        expected_catalog_version=expected_catalog_version,
        window=window,
    )
    return observation


TEN_SYMBOL_BARS_SIDECAR_CONTRACT = "tradingagent.crypto.ten_symbol_observation_bars.v1"


def _wire_rows_sha256(value: object) -> str:
    """Recompute digests with the exact pagination-layer canonicalization."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CryptoMarketObservationError(
            "crypto_observation_bars_sidecar_invalid"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _recomputed_identity_sha256(rows: list[Mapping[str, Any]]) -> str:
    identities: list[dict[str, Any]] = []
    for row in rows:
        identity = {
            "symbol": row.get("symbol"),
            "open_time": row.get("open_time"),
        }
        if identity["symbol"] is None or identity["open_time"] is None:
            raise CryptoMarketObservationError(
                "crypto_observation_bars_sidecar_invalid"
            )
        identities.append(identity)
    return _wire_rows_sha256(identities)


def _recomputed_market_data_sha256(rows: list[Mapping[str, Any]]) -> str:
    return _wire_rows_sha256([dict(row) for row in rows])


def _validated_sidecar_rows(value: object, *, symbol: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != BAR_COUNT:
        raise CryptoMarketObservationError("crypto_observation_bars_sidecar_invalid")
    rows: list[dict[str, Any]] = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or set(item) != set(BAR_FIELDS)
            or item.get("symbol") != symbol
        ):
            raise CryptoMarketObservationError(
                "crypto_observation_bars_sidecar_invalid"
            )
        rows.append(dict(item))
    return rows


def _verify_sidecar_rows(
    source: CryptoObservationSource,
    rows: list[dict[str, Any]],
) -> None:
    if (
        source.row_count != len(rows)
        or source.page_count != 1
        or _recomputed_identity_sha256(rows) != source.identity_sha256
        or _recomputed_market_data_sha256(rows) != source.market_data_sha256
    ):
        raise CryptoMarketObservationError("crypto_observation_bars_sidecar_invalid")


def build_ten_symbol_bars_sidecar(
    *,
    window: CryptoObservationWindow,
    profile_sha256: str,
    observation: CryptoMarketObservation,
    rows_by_symbol: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the immutable per-slot bars sidecar payload.

    The payload carries each source's validated raw rows next to the digest
    claims already bound in the observation evidence, so a detached consumer
    can independently re-derive ``identity_sha256``/``market_data_sha256``
    from the rows and compare them against the append-only store event.
    """

    if not isinstance(observation, CryptoMarketObservation):
        raise CryptoMarketObservationError("crypto_observation_bars_sidecar_invalid")
    if (
        observation.window.window_end != window.window_end
        or observation.window.observation_cutoff != window.observation_cutoff
    ):
        raise CryptoMarketObservationError("crypto_observation_bars_sidecar_invalid")
    if (
        not isinstance(profile_sha256, str)
        or len(profile_sha256) != 64
        or any(character not in "0123456789abcdef" for character in profile_sha256)
    ):
        raise CryptoMarketObservationError("crypto_observation_bars_sidecar_invalid")
    sources: list[dict[str, Any]] = []
    for source in observation.sources:
        rows = _validated_sidecar_rows(rows_by_symbol.get(source.symbol), symbol=source.symbol)
        _verify_sidecar_rows(source, rows)
        sources.append({**source.to_payload(), "rows": rows})
    return {
        "contract": TEN_SYMBOL_BARS_SIDECAR_CONTRACT,
        "window_end": _iso(window.window_end),
        "observation_cutoff": _iso(window.observation_cutoff),
        "catalog_version": observation.catalog_version,
        "profile_sha256": profile_sha256,
        "observation_sha256": observation.observation_sha256,
        "market_data_sha256": observation.market_data_sha256,
        "sources": sources,
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def observation_from_ten_symbol_bars_sidecar(
    payload: Mapping[str, Any],
) -> tuple[CryptoMarketObservation, dict[str, list[dict[str, Any]]]]:
    """Re-derive and verify one slot observation from its bars sidecar.

    Every per-source row digest is recomputed from the persisted rows, and
    the reconstructed observation must reproduce both the observation-level
    ``market_data_sha256`` and ``observation_sha256`` claims exactly.  Any
    drift fails closed.
    """

    reason = "crypto_observation_bars_sidecar_invalid"
    if not isinstance(payload, Mapping):
        raise CryptoMarketObservationError(reason)
    if set(payload) != {
        "contract",
        "window_end",
        "observation_cutoff",
        "catalog_version",
        "profile_sha256",
        "observation_sha256",
        "market_data_sha256",
        "sources",
        "authority",
        "execution_eligible",
        "capital_write_eligible",
        "model_authority",
    } or payload.get("contract") != TEN_SYMBOL_BARS_SIDECAR_CONTRACT:
        raise CryptoMarketObservationError(reason)
    if (
        payload.get("authority") != "none"
        or payload.get("execution_eligible") is not False
        or payload.get("capital_write_eligible") is not False
        or payload.get("model_authority") is not False
    ):
        raise CryptoMarketObservationError("crypto_observation_authority_invalid")
    window_end = _parse_utc(payload.get("window_end"), reason)
    observation_cutoff = _parse_utc(payload.get("observation_cutoff"), reason)
    window = CryptoObservationWindow(
        window_end=window_end,
        observation_cutoff=observation_cutoff,
    )
    profile_sha256 = payload.get("profile_sha256")
    catalog_version = payload.get("catalog_version")
    if (
        not isinstance(profile_sha256, str)
        or len(profile_sha256) != 64
        or any(character not in "0123456789abcdef" for character in profile_sha256)
        or not isinstance(catalog_version, str)
        or not catalog_version
    ):
        raise CryptoMarketObservationError(reason)
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != len(
        OBSERVATION_SYMBOLS
    ):
        raise CryptoMarketObservationError(reason)
    expected_keys = {
        "symbol",
        "dataset_id",
        "row_count",
        "page_count",
        "receipt_id",
        "data_through",
        "observed_at",
        "identity_sha256",
        "market_data_sha256",
        "semantic_sha256",
        "pagination_trace_sha256",
        "rows",
    }
    sources: list[CryptoObservationSource] = []
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for index, raw in enumerate(raw_sources):
        symbol = OBSERVATION_SYMBOLS[index]
        if not isinstance(raw, Mapping) or set(raw) != expected_keys:
            raise CryptoMarketObservationError(reason)
        if raw.get("symbol") != symbol or raw.get("dataset_id") != _bar_dataset_id(
            symbol
        ):
            raise CryptoMarketObservationError(reason)
        source = CryptoObservationSource(
            symbol=symbol,
            dataset_id=str(raw["dataset_id"]),
            row_count=raw.get("row_count"),
            page_count=raw.get("page_count"),
            receipt_id=raw.get("receipt_id"),
            data_through=_parse_utc(raw.get("data_through"), reason),
            observed_at=_parse_utc(raw.get("observed_at"), reason),
            identity_sha256=raw.get("identity_sha256"),
            market_data_sha256=raw.get("market_data_sha256"),
            semantic_sha256=raw.get("semantic_sha256"),
            pagination_trace_sha256=raw.get("pagination_trace_sha256"),
        )
        rows = _validated_sidecar_rows(raw.get("rows"), symbol=symbol)
        _verify_sidecar_rows(source, rows)
        sources.append(source)
        rows_by_symbol[symbol] = rows
    observation = CryptoMarketObservation(
        catalog_version=catalog_version,
        window=window,
        sources=tuple(sources),
        market_data_sha256=payload.get("market_data_sha256"),
        observation_sha256=payload.get("observation_sha256"),
    )
    return observation, rows_by_symbol


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
    return _collect_market_observation_with_catalog(
        client,
        catalog=catalog,
        expected_catalog_version=expected_catalog_version,
        window=window,
    )


__all__ = [
    "BAR_COUNT",
    "BAR_FIELDS",
    "OBSERVATION_CONTRACT",
    "OBSERVATION_SYMBOLS",
    "TEN_SYMBOL_BARS_SIDECAR_CONTRACT",
    "CryptoMarketObservation",
    "CryptoMarketObservationError",
    "CryptoObservationSource",
    "CryptoObservationWindow",
    "build_ten_symbol_bars_sidecar",
    "collect_market_observation",
    "observation_from_ten_symbol_bars_sidecar",
]
