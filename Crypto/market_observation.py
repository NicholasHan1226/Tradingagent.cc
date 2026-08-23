"""Read-only health observation for the TradingDatas Crypto 5-minute cohort.

This module deliberately sits outside the BTC/ETH delayed-paper capital path.
It expands *data* observation to the currently collected ten-symbol cohort,
but has no capital, order, model, ledger, timer, or promotion authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
import time
from typing import Any, Callable, Mapping
import urllib.error

from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    HTTPStatusError,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_pagination import PagedQueryRun, collect_query_pages
from shared.governance.evidence_readiness import dataset_contract_fingerprint


OBSERVATION_CONTRACT = "tradingagent.crypto.market_observation.v1"
FIVE_MINUTES = timedelta(minutes=5)
BAR_COUNT = 13
# Headroom kept against the caller's invocation budget before spending a
# same-invocation shape retry, so the remaining symbols and the auxiliary
# spread leg always keep room to finish.
BAR_SHAPE_RETRY_RESERVE_SECONDS = 15.0
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

# Frozen 40-symbol research universe.  This is a *new* versioned universe, not
# an in-place widening of ``OBSERVATION_SYMBOLS``: the ten-symbol append-only
# store and the v2 factor projection both require ``len(sources) ==
# len(OBSERVATION_SYMBOLS)`` and order equality, so the ten-symbol chain stays
# read-only and the forty-symbol chain gets its own family below.
OBSERVATION_SYMBOLS_V40 = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "BCHUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "NEARUSDT",
    "SUIUSDT",
    "APTUSDT",
    "UNIUSDT",
    "ATOMUSDT",
    "XLMUSDT",
    "HBARUSDT",
    "ETCUSDT",
    "FILUSDT",
    "INJUSDT",
    "ARBUSDT",
    "OPUSDT",
    "AAVEUSDT",
    "GRTUSDT",
    "TIAUSDT",
    "SEIUSDT",
    "ONDOUSDT",
    "LDOUSDT",
    "CRVUSDT",
    "ENAUSDT",
    "WLDUSDT",
    "STRKUSDT",
    "JUPUSDT",
    "PYTHUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "POLUSDT",
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


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


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
    symbols: tuple[str, ...] = field(
        default=OBSERVATION_SYMBOLS,
        compare=False,
        hash=False,
    )

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
        if tuple(source.symbol for source in self.sources) != self.symbols:
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


def _fetch_and_shape_validate(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    symbol: str,
    dataset_id: str,
    window: CryptoObservationWindow,
) -> tuple[PagedQueryRun, CryptoObservationSource]:
    """Run the exact bounded bar query once and gate its envelope shape."""

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
    source = _validate_run(run, symbol=symbol, dataset_id=dataset_id, window=window)
    return run, source


def _collect_symbol_bar_run(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    symbol: str,
    dataset_id: str,
    window: CryptoObservationWindow,
    shape_retry_delays: tuple[float, ...],
    retry_sleep: Callable[[float], None],
    budget_remaining: Callable[[], float] | None,
) -> tuple[PagedQueryRun, CryptoObservationSource]:
    """Fetch one symbol's bars, retrying only the mid-write shape transient.

    A ``crypto_observation_query_shape_invalid`` reject means the paged read
    raced an in-progress collection write (typically a short row count).
    When enabled, the identical bounded query is re-issued after fixed
    delays while the caller's invocation budget still affords it; every
    validation gate is unchanged and exhausting the retries (or running out
    of budget) re-raises the original error, so the fail-closed contract is
    preserved.  Every other reason propagates on its first attempt.
    """

    for attempt in range(1 + len(shape_retry_delays)):
        try:
            return _fetch_and_shape_validate(
                client,
                catalog=catalog,
                symbol=symbol,
                dataset_id=dataset_id,
                window=window,
            )
        except CryptoMarketObservationError as exc:
            if (
                str(exc) != "crypto_observation_query_shape_invalid"
                or attempt == len(shape_retry_delays)
            ):
                raise
            delay = shape_retry_delays[attempt]
            if budget_remaining is not None and (
                budget_remaining() <= delay + BAR_SHAPE_RETRY_RESERVE_SECONDS
            ):
                raise
            retry_sleep(delay)


def _collect_market_observation_rows_with_catalog(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    expected_catalog_version: str,
    window: CryptoObservationWindow,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    shape_retry_delays: tuple[float, ...] = (),
    retry_sleep: Callable[[float], None] = time.sleep,
    budget_remaining: Callable[[], float] | None = None,
) -> tuple[CryptoMarketObservation, dict[str, list[dict[str, Any]]]]:
    """Collect the cohort and also return the validated raw bar rows.

    The rows never enter any observation digest field; they are returned so
    the accumulator runtime can persist them as an immutable bars sidecar
    whose digests re-derive exactly the per-source ``identity_sha256`` and
    ``market_data_sha256`` already bound in the observation evidence.

    The bounded mid-write shape retry is opt-in per family: by default every
    symbol is a single attempt, so frozen call sites keep their established
    behavior byte-for-byte.
    """

    if (
        catalog.api_version != "v1"
        or catalog.catalog_version != expected_catalog_version
    ):
        raise CryptoMarketObservationError("crypto_observation_catalog_version_drift")
    sources: list[CryptoObservationSource] = []
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        dataset_id = _bar_dataset_id(symbol)
        _verify_catalog(catalog, dataset_id)
        run, source = _collect_symbol_bar_run(
            client,
            catalog=catalog,
            symbol=symbol,
            dataset_id=dataset_id,
            window=window,
            shape_retry_delays=shape_retry_delays,
            retry_sleep=retry_sleep,
            budget_remaining=budget_remaining,
        )
        sources.append(source)
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
        symbols=symbols,
    ), rows_by_symbol


def _collect_market_observation_with_catalog(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    expected_catalog_version: str,
    window: CryptoObservationWindow,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
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
        symbols=symbols,
    )
    return observation


TEN_SYMBOL_BARS_SIDECAR_CONTRACT = "tradingagent.crypto.ten_symbol_observation_bars.v1"
FORTY_SYMBOL_BARS_SIDECAR_CONTRACT = "tradingagent.crypto.forty_symbol_observation_bars.v1"


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
    bars_sidecar_contract: str = TEN_SYMBOL_BARS_SIDECAR_CONTRACT,
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
        "contract": bars_sidecar_contract,
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
    *,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    bars_sidecar_contract: str = TEN_SYMBOL_BARS_SIDECAR_CONTRACT,
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
    } or payload.get("contract") != bars_sidecar_contract:
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
    if not isinstance(raw_sources, list) or len(raw_sources) != len(symbols):
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
        symbol = symbols[index]
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
        symbols=symbols,
    )
    return observation, rows_by_symbol


# ---------------------------------------------------------------------------
# Book-ticker spread sampling (auxiliary, degradation-tolerant evidence)
# ---------------------------------------------------------------------------
#
# The ``.book_ticker`` datasets are current-snapshot reads: the upstream row
# carries no provider event timestamp, so the collection receipt's
# ``observed_at`` is the only time authority.  Spread evidence is deliberately
# auxiliary: every upstream/contract failure degrades to a per-symbol or
# leg-wide recorded status and never fails the bar observation it rides on.
# Raw snapshot rows stay out of every observation digest, mirroring the bars
# sidecar precedent; the event anchors only the derived status block and the
# sidecar digest.

BOOK_TICKER_FIELDS = ("symbol", "bid_price", "bid_qty", "ask_price", "ask_qty")
BOOK_TICKER_ROW_COUNT = 1
TEN_SYMBOL_SPREAD_CONTRACT = "tradingagent.crypto.ten_symbol_observation_spread.v1"
TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT = (
    "tradingagent.crypto.ten_symbol_observation_spreads.v1"
)
FORTY_SYMBOL_SPREAD_CONTRACT = "tradingagent.crypto.forty_symbol_observation_spread.v1"
FORTY_SYMBOL_SPREADS_SIDECAR_CONTRACT = (
    "tradingagent.crypto.forty_symbol_observation_spreads.v1"
)


def _book_ticker_dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.book_ticker"


def _spread_catalog_row(catalog: CatalogEnvelope, dataset_id: str) -> Mapping[str, Any]:
    matches = [row for row in catalog.data if row.get("dataset_id") == dataset_id]
    if len(matches) != 1:
        raise CryptoMarketObservationError("crypto_spread_catalog_row_missing")
    return matches[0]


def _verify_book_ticker_catalog(catalog: CatalogEnvelope, dataset_id: str) -> None:
    row = _spread_catalog_row(catalog, dataset_id)
    if row.get("schema_major") != 1:
        raise CryptoMarketObservationError("crypto_spread_schema_major_drift")
    if tuple(row.get("default_fields") or ()) != BOOK_TICKER_FIELDS:
        raise CryptoMarketObservationError("crypto_spread_fields_drift")
    if tuple(row.get("identity_fields") or ()) != ("symbol",):
        raise CryptoMarketObservationError("crypto_spread_identity_drift")
    if row.get("point_in_time") != "current_snapshot":
        raise CryptoMarketObservationError("crypto_spread_point_in_time_drift")
    availability = row.get("availability")
    queryability = row.get("queryability")
    if not (
        isinstance(availability, Mapping)
        and "active" in availability.get("entitlement_states", [])
        and "active" in availability.get("activation_states", [])
        and isinstance(queryability, Mapping)
        and queryability.get("queryable") is True
    ):
        raise CryptoMarketObservationError("crypto_spread_dataset_not_queryable")
    filters = row.get("filter_operators")
    if not (isinstance(filters, Mapping) and "eq" in filters.get("symbol", [])):
        raise CryptoMarketObservationError("crypto_spread_filters_drift")
    limits = row.get("limits")
    if not isinstance(limits, Mapping) or limits.get("max_page_size", 0) < (
        BOOK_TICKER_ROW_COUNT
    ):
        raise CryptoMarketObservationError("crypto_spread_page_limit_invalid")


def _spread_contract_fingerprint(
    catalog: CatalogEnvelope, dataset_id: str
) -> str:
    try:
        return dataset_contract_fingerprint(_spread_catalog_row(catalog, dataset_id))
    except ValueError as exc:
        raise CryptoMarketObservationError(
            "crypto_spread_contract_fingerprint_invalid"
        ) from exc


def _validated_spread_row(value: object, *, symbol: str) -> dict[str, Any]:
    reason = "crypto_spread_row_shape_invalid"
    if not isinstance(value, Mapping) or set(value) != set(BOOK_TICKER_FIELDS):
        raise CryptoMarketObservationError(reason)
    row = dict(value)
    if row.get("symbol") != symbol:
        raise CryptoMarketObservationError(reason)
    bid_price = _decimal(
        row.get("bid_price"), "crypto_spread_quote_invalid", positive=True
    )
    ask_price = _decimal(
        row.get("ask_price"), "crypto_spread_quote_invalid", positive=True
    )
    _decimal(row.get("bid_qty"), "crypto_spread_quote_invalid", positive=True)
    _decimal(row.get("ask_qty"), "crypto_spread_quote_invalid", positive=True)
    if ask_price < bid_price:
        raise CryptoMarketObservationError("crypto_spread_quote_invalid")
    return row


def _validate_spread_run(
    run: PagedQueryRun,
    *,
    symbol: str,
    dataset_id: str,
    window: CryptoObservationWindow,
    catalog_contract_sha256: str,
) -> dict[str, Any]:
    envelope = run.envelope
    metadata = envelope.metadata
    if (
        envelope.dataset_id != dataset_id
        or envelope.api_version != "v1"
        or run.page_count != 1
        or run.row_count != BOOK_TICKER_ROW_COUNT
        or envelope.next_cursor is not None
    ):
        raise CryptoMarketObservationError("crypto_spread_query_shape_invalid")
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
        raise CryptoMarketObservationError("crypto_spread_metadata_invalid")
    observed_at = _parse_utc(
        metadata.observed_at, "crypto_spread_observed_at_invalid"
    )
    if metadata.data_through is not None:
        _parse_utc(metadata.data_through, "crypto_spread_observed_at_invalid")
    # The receipt observation instant is the snapshot's only time authority;
    # it is held to the same slot watermark discipline as the bar evidence.
    if not window.window_end <= observed_at <= window.observation_cutoff:
        raise CryptoMarketObservationError("crypto_spread_watermark_invalid")
    row = _validated_spread_row(envelope.data[0], symbol=symbol)
    return {
        "symbol": symbol,
        "dataset_id": dataset_id,
        "status": "sampled",
        "receipt_id": metadata.receipt_id,
        "observed_at": _iso(observed_at),
        "freshness_state": str(metadata.freshness.get("state")),
        "quality_state": str(metadata.quality.get("state")),
        "catalog_contract_sha256": catalog_contract_sha256,
        "identity_sha256": run.identity_sha256,
        "market_data_sha256": run.ordered_rows_sha256,
        "row": row,
    }


def _rejected_spread_entry(
    *,
    symbol: str,
    dataset_id: str,
    reason_code: str,
    catalog_contract_sha256: str | None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "dataset_id": dataset_id,
        "status": "rejected",
        "reason_code": reason_code,
        "catalog_contract_sha256": catalog_contract_sha256,
    }


def _collect_book_ticker_entry(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    symbol: str,
    window: CryptoObservationWindow,
) -> dict[str, Any]:
    """Sample one symbol's book ticker; upstream faults become a rejection."""

    dataset_id = _book_ticker_dataset_id(symbol)
    try:
        _verify_book_ticker_catalog(catalog, dataset_id)
        fingerprint = _spread_contract_fingerprint(catalog, dataset_id)
    except CryptoMarketObservationError as exc:
        return _rejected_spread_entry(
            symbol=symbol,
            dataset_id=dataset_id,
            reason_code=str(exc),
            catalog_contract_sha256=None,
        )
    try:
        run = collect_query_pages(
            client=client,
            request=QueryRequest(
                dataset_id=dataset_id,
                schema_major=1,
                fields=BOOK_TICKER_FIELDS,
                filters={"symbol": {"eq": symbol}},
                # A current-snapshot read carries no as_of; the receipt's
                # observed_at is the time authority, like the bar current read.
                order=("symbol:asc",),
                limit=BOOK_TICKER_ROW_COUNT,
            ),
            identity_fields=("symbol",),
            max_pages=1,
            max_rows=BOOK_TICKER_ROW_COUNT,
        )
        return _validate_spread_run(
            run,
            symbol=symbol,
            dataset_id=dataset_id,
            window=window,
            catalog_contract_sha256=fingerprint,
        )
    except CryptoMarketObservationError as exc:
        reason_code = str(exc)
    except (urllib.error.HTTPError, HTTPStatusError):
        reason_code = "crypto_spread_query_http_error"
    except SharedSignalsV1Error:
        reason_code = "crypto_spread_query_contract_invalid"
    except (
        TimeoutError,
        ConnectionError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        reason_code = "crypto_spread_query_transport_failed"
    return _rejected_spread_entry(
        symbol=symbol,
        dataset_id=dataset_id,
        reason_code=reason_code,
        catalog_contract_sha256=fingerprint,
    )


def collect_book_ticker_spread_entries(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    expected_catalog_version: str,
    window: CryptoObservationWindow,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
) -> list[dict[str, Any]]:
    """Sample all book tickers for one universe against one observed catalog.

    Every per-symbol failure is captured as a rejected entry so one symbol's
    drift, staleness or transport fault never withholds the other snapshots.
    Leg-wide faults (for example the catalog read itself) still propagate to
    the caller, which records a leg-wide ``unavailable`` status instead.
    """

    if (
        catalog.api_version != "v1"
        or catalog.catalog_version != expected_catalog_version
    ):
        raise CryptoMarketObservationError("crypto_spread_catalog_version_drift")
    return [
        _collect_book_ticker_entry(client, catalog=catalog, symbol=symbol, window=window)
        for symbol in symbols
    ]


def _validate_spread_entries(
    value: object,
    *,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
) -> list[dict[str, Any]]:
    reason = "crypto_observation_spreads_sidecar_invalid"
    if not isinstance(value, list) or len(value) != len(symbols):
        raise CryptoMarketObservationError(reason)
    sampled_keys = {
        "symbol",
        "dataset_id",
        "status",
        "receipt_id",
        "observed_at",
        "freshness_state",
        "quality_state",
        "catalog_contract_sha256",
        "identity_sha256",
        "market_data_sha256",
        "row",
    }
    rejected_keys = {
        "symbol",
        "dataset_id",
        "status",
        "reason_code",
        "catalog_contract_sha256",
    }
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        symbol = symbols[index]
        if not isinstance(item, Mapping):
            raise CryptoMarketObservationError(reason)
        entry = dict(item)
        if entry.get("symbol") != symbol or entry.get(
            "dataset_id"
        ) != _book_ticker_dataset_id(symbol):
            raise CryptoMarketObservationError(reason)
        fingerprint = entry.get("catalog_contract_sha256")
        status = entry.get("status")
        if status == "sampled":
            if set(entry) != sampled_keys:
                raise CryptoMarketObservationError(reason)
            if (
                not isinstance(entry.get("receipt_id"), str)
                or not entry["receipt_id"]
                or not isinstance(entry.get("freshness_state"), str)
                or not entry["freshness_state"]
                or not isinstance(entry.get("quality_state"), str)
                or not entry["quality_state"]
                or not _is_sha256(fingerprint)
                or not _is_sha256(entry.get("identity_sha256"))
                or not _is_sha256(entry.get("market_data_sha256"))
            ):
                raise CryptoMarketObservationError(reason)
            _parse_utc(entry.get("observed_at"), reason)
            row = _validated_spread_row(entry.get("row"), symbol=symbol)
            if (
                _wire_rows_sha256([{"symbol": symbol}]) != entry["identity_sha256"]
                or _wire_rows_sha256([row]) != entry["market_data_sha256"]
            ):
                raise CryptoMarketObservationError(reason)
            entries.append({**entry, "row": row})
        elif status == "rejected":
            if set(entry) != rejected_keys:
                raise CryptoMarketObservationError(reason)
            if (
                not isinstance(entry.get("reason_code"), str)
                or not entry["reason_code"]
                or (fingerprint is not None and not _is_sha256(fingerprint))
            ):
                raise CryptoMarketObservationError(reason)
            entries.append(entry)
        else:
            raise CryptoMarketObservationError(reason)
    return entries


def _spread_entries_sha256(entries: list[dict[str, Any]]) -> str:
    return _canonical_sha256(entries)


def build_spread_event_block(
    *,
    entries: list[dict[str, Any]],
    catalog_version: str | None,
    spread_sha256: str | None,
    spread_contract: str = TEN_SYMBOL_SPREAD_CONTRACT,
) -> dict[str, Any]:
    """Derive the event-facing spread status block from validated entries."""

    sampled = [entry for entry in entries if entry["status"] == "sampled"]
    rejected_reasons = {
        entry["symbol"]: entry["reason_code"]
        for entry in entries
        if entry["status"] == "rejected"
    }
    if not rejected_reasons:
        status = "completed"
    elif sampled:
        status = "degraded"
    else:
        status = "unavailable"
    return {
        "contract": spread_contract,
        "status": status,
        "reason_code": None,
        "sampled_symbol_count": len(sampled),
        "rejected_symbol_count": len(rejected_reasons),
        "rejected_reasons": rejected_reasons,
        "spread_sha256": spread_sha256,
        "catalog_version": catalog_version,
    }


def unavailable_spread_event_block(
    reason_code: str,
    *,
    spread_contract: str = TEN_SYMBOL_SPREAD_CONTRACT,
) -> dict[str, Any]:
    """The leg-wide degradation block when no per-symbol evidence exists."""

    if not isinstance(reason_code, str) or not reason_code:
        raise CryptoMarketObservationError("crypto_spread_reason_invalid")
    return {
        "contract": spread_contract,
        "status": "unavailable",
        "reason_code": reason_code,
        "sampled_symbol_count": 0,
        "rejected_symbol_count": 0,
        "rejected_reasons": {},
        "spread_sha256": None,
        "catalog_version": None,
    }


def build_ten_symbol_spreads_sidecar(
    *,
    window: CryptoObservationWindow,
    profile_sha256: str,
    catalog_version: str,
    entries: list[dict[str, Any]],
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    spreads_sidecar_contract: str = TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
) -> dict[str, Any]:
    """Assemble the immutable per-slot spreads sidecar payload.

    The payload mirrors the bars sidecar precedent: raw snapshot rows plus
    the per-symbol receipt/digest metadata, so a detached consumer can
    independently re-derive every per-symbol digest and the top-level
    ``spread_sha256`` and compare them against the store event's spread
    block.  Spread rows never enter any observation digest.
    """

    if not isinstance(window, CryptoObservationWindow):
        raise CryptoMarketObservationError("crypto_observation_spreads_sidecar_invalid")
    if not _is_sha256(profile_sha256):
        raise CryptoMarketObservationError("crypto_observation_spreads_sidecar_invalid")
    if not isinstance(catalog_version, str) or not catalog_version:
        raise CryptoMarketObservationError("crypto_observation_spreads_sidecar_invalid")
    normalized = _validate_spread_entries(entries, symbols=symbols)
    return {
        "contract": spreads_sidecar_contract,
        "window_end": _iso(window.window_end),
        "observation_cutoff": _iso(window.observation_cutoff),
        "catalog_version": catalog_version,
        "profile_sha256": profile_sha256,
        "entries": normalized,
        "spread_sha256": _spread_entries_sha256(normalized),
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def validate_ten_symbol_spreads_sidecar(
    payload: Mapping[str, Any],
    *,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    spreads_sidecar_contract: str = TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
) -> list[dict[str, Any]]:
    """Re-derive and verify one slot's spreads sidecar, returning its entries.

    Every sampled entry's row digests are recomputed from the persisted row,
    and the top-level ``spread_sha256`` claim must reproduce exactly.  Any
    drift fails closed; a corrupt sidecar is local evidence corruption and
    is never an upstream data rejection.
    """

    reason = "crypto_observation_spreads_sidecar_invalid"
    if not isinstance(payload, Mapping):
        raise CryptoMarketObservationError(reason)
    if set(payload) != {
        "contract",
        "window_end",
        "observation_cutoff",
        "catalog_version",
        "profile_sha256",
        "entries",
        "spread_sha256",
        "authority",
        "execution_eligible",
        "capital_write_eligible",
        "model_authority",
    } or payload.get("contract") != spreads_sidecar_contract:
        raise CryptoMarketObservationError(reason)
    if (
        payload.get("authority") != "none"
        or payload.get("execution_eligible") is not False
        or payload.get("capital_write_eligible") is not False
        or payload.get("model_authority") is not False
    ):
        raise CryptoMarketObservationError("crypto_observation_authority_invalid")
    window = CryptoObservationWindow(
        window_end=_parse_utc(payload.get("window_end"), reason),
        observation_cutoff=_parse_utc(payload.get("observation_cutoff"), reason),
    )
    if not _is_sha256(payload.get("profile_sha256")):
        raise CryptoMarketObservationError(reason)
    catalog_version = payload.get("catalog_version")
    if not isinstance(catalog_version, str) or not catalog_version:
        raise CryptoMarketObservationError(reason)
    entries = _validate_spread_entries(payload.get("entries"), symbols=symbols)
    for entry in entries:
        if entry["status"] != "sampled":
            continue
        observed_at = _parse_utc(entry.get("observed_at"), reason)
        if not window.window_end <= observed_at <= window.observation_cutoff:
            raise CryptoMarketObservationError(reason)
    if payload.get("spread_sha256") != _spread_entries_sha256(entries):
        raise CryptoMarketObservationError(reason)
    return entries


def collect_market_observation(
    client: SharedSignalsV1Client,
    *,
    expected_catalog_version: str,
    window: CryptoObservationWindow,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
) -> CryptoMarketObservation:
    """Return a zero-authority read-only observation for one universe.

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
        symbols=symbols,
    )


__all__ = [
    "BAR_COUNT",
    "BAR_FIELDS",
    "BAR_SHAPE_RETRY_RESERVE_SECONDS",
    "BOOK_TICKER_FIELDS",
    "BOOK_TICKER_ROW_COUNT",
    "FORTY_SYMBOL_BARS_SIDECAR_CONTRACT",
    "FORTY_SYMBOL_SPREAD_CONTRACT",
    "FORTY_SYMBOL_SPREADS_SIDECAR_CONTRACT",
    "OBSERVATION_CONTRACT",
    "OBSERVATION_SYMBOLS",
    "OBSERVATION_SYMBOLS_V40",
    "TEN_SYMBOL_BARS_SIDECAR_CONTRACT",
    "TEN_SYMBOL_SPREAD_CONTRACT",
    "TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT",
    "CryptoMarketObservation",
    "CryptoMarketObservationError",
    "CryptoObservationSource",
    "CryptoObservationWindow",
    "build_spread_event_block",
    "build_ten_symbol_bars_sidecar",
    "build_ten_symbol_spreads_sidecar",
    "collect_book_ticker_spread_entries",
    "collect_market_observation",
    "observation_from_ten_symbol_bars_sidecar",
    "unavailable_spread_event_block",
    "validate_ten_symbol_spreads_sidecar",
]
