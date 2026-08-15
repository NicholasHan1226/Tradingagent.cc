"""Shared offline fixtures for the ten-symbol observation accumulator tests.

The fixture transport emulates the formal TradingDatas loopback contract for
the ten 5-minute bar datasets without any network access.  Rows are generated
deterministically from the requested ``open_time between`` window so arbitrary
historical and current slots can be exercised.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from Crypto.market_observation import (
    BAR_FIELDS,
    FIVE_MINUTES,
    OBSERVATION_SYMBOLS,
    CryptoMarketObservation,
    CryptoObservationWindow,
    _collect_market_observation_rows_with_catalog,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "fixture-crypto-ten-symbol-observation-v1"
WINDOW_END = datetime(2026, 8, 10, 1, 5, tzinfo=timezone.utc)
CUTOFF = WINDOW_END + timedelta(seconds=55)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bar_dataset(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


ALL_DATASETS = frozenset(bar_dataset(symbol) for symbol in OBSERVATION_SYMBOLS)


def catalog_row(symbol: str) -> dict[str, Any]:
    return {
        "dataset_id": bar_dataset(symbol),
        "schema_major": 1,
        "default_fields": list(BAR_FIELDS),
        "default_order": ["symbol:asc", "open_time:asc"],
        "identity_fields": ["symbol", "open_time"],
        "fields": [],
        "filter_operators": {
            "symbol": ["eq", "in"],
            "open_time": ["eq", "between", "gte", "lte"],
        },
        "limits": {"max_page_size": 500, "max_lookback_days": 36500},
        "availability": {
            "entitlement_states": ["active"],
            "activation_states": ["active"],
        },
        "queryability": {"queryable": True, "reasons": []},
    }


def catalog_payload(
    *,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "fixture-ten-symbol-catalog",
        "data": copy.deepcopy(
            rows if rows is not None else [catalog_row(s) for s in OBSERVATION_SYMBOLS]
        ),
    }


def generated_rows(
    symbol: str,
    first_open: datetime,
    *,
    count: int = 13,
) -> list[dict[str, Any]]:
    base = Decimal("100") + Decimal(OBSERVATION_SYMBOLS.index(symbol) * 10)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        open_time = first_open + index * FIVE_MINUTES
        price = base + Decimal(index)
        rows.append(
            {
                "symbol": symbol,
                "open_time": iso(open_time),
                "close_time": iso(
                    open_time + FIVE_MINUTES - timedelta(milliseconds=1)
                ),
                "open": format(price, "f"),
                "high": format(price + 2, "f"),
                "low": format(price - 1, "f"),
                "close": format(price + 1, "f"),
                "volume": "10",
                "quote_volume": "1000",
                "trade_count": 10 + index,
            }
        )
    return rows


def query_metadata(
    dataset_id: str,
    *,
    data_through: datetime,
    observed_at: datetime,
) -> dict[str, Any]:
    return {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid"},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "providers": ["binance_spot_fixture"],
            "transport_service": "fixture_ten_symbol_transport",
        },
        "receipt_id": f"fixture-receipt-{dataset_id}",
        "data_through": iso(data_through),
        "observed_at": iso(observed_at),
        "reasons": [],
    }


class TenSymbolFixtureTransport:
    """Offline loopback fixture for one catalog plus bounded window queries."""

    def __init__(
        self,
        *,
        catalog_rows: list[dict[str, Any]] | None = None,
        observed_at: datetime | None = None,
        row_count: int = 13,
        status_code: int = 200,
        metadata_mutator: Callable[[str, datetime, dict[str, Any]], None] | None = None,
    ) -> None:
        self.catalog_rows = copy.deepcopy(catalog_rows)
        # A current read reports *now* as observed_at; tests set this to the
        # invocation time so historical windows fail the watermark gate exactly
        # like the formal current-query contract.
        self.observed_at = observed_at
        self.row_count = row_count
        self.status_code = status_code
        self.metadata_mutator = metadata_mutator
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            return HTTPResponse(200, catalog_payload(rows=self.catalog_rows))
        if self.status_code != 200:
            return HTTPResponse(self.status_code, {"error": "fixture failure"})
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        dataset_id = body["dataset_id"]
        if dataset_id not in ALL_DATASETS:
            return HTTPResponse(404, {"error": "unknown fixture dataset"})
        between = body["filters"]["open_time"]["between"]
        first_open = datetime.fromisoformat(str(between[0]).replace("Z", "+00:00"))
        last_open = datetime.fromisoformat(str(between[1]).replace("Z", "+00:00"))
        symbol = str(body["filters"]["symbol"]["eq"])
        rows = generated_rows(symbol, first_open, count=self.row_count)
        window_end = last_open + FIVE_MINUTES
        observed_at = (
            self.observed_at
            if self.observed_at is not None
            else window_end + timedelta(seconds=20)
        )
        metadata = query_metadata(
            dataset_id,
            data_through=window_end - timedelta(milliseconds=1),
            observed_at=observed_at,
        )
        if self.metadata_mutator is not None:
            self.metadata_mutator(dataset_id, window_end, metadata)
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"fixture-query-{dataset_id}",
                "dataset_id": dataset_id,
                "data": rows[: int(body["limit"])],
                "next_cursor": None,
                "metadata": metadata,
            },
        )


def client(transport: TenSymbolFixtureTransport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="http://127.0.0.1:18083",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=ALL_DATASETS,
            access_policy_id="fixture-ten-symbol-observation",
            catalog_version_policy="strict",
            timeout_seconds=1.0,
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def collect_fixture_observation(
    window_end: datetime,
    *,
    observed_at: datetime | None = None,
) -> tuple[CryptoMarketObservation, dict[str, list[dict[str, Any]]]]:
    """Collect one fully validated fixture observation plus its raw bar rows."""

    transport = TenSymbolFixtureTransport(observed_at=observed_at)
    fixture_client = client(transport)
    catalog = fixture_client.get_catalog()
    window = CryptoObservationWindow(
        window_end=window_end,
        observation_cutoff=window_end + timedelta(seconds=55),
    )
    return _collect_market_observation_rows_with_catalog(
        fixture_client,
        catalog=catalog,
        expected_catalog_version=CATALOG_VERSION,
        window=window,
    )
