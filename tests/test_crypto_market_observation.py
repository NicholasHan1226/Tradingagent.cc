from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from Crypto.market_observation import (
    BAR_FIELDS,
    OBSERVATION_SYMBOLS,
    CryptoMarketObservationError,
    CryptoObservationWindow,
    collect_market_observation,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "crypto-observation-fixture-v1"
WINDOW_END = datetime(2026, 8, 2, 5, 5, tzinfo=timezone.utc)
CUTOFF = WINDOW_END + timedelta(seconds=55)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dataset(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


def _row(symbol: str, offset: int) -> dict[str, Any]:
    open_time = WINDOW_END - timedelta(minutes=65) + timedelta(minutes=offset * 5)
    base = Decimal("100") + Decimal(offset)
    return {
        "symbol": symbol,
        "open_time": _iso(open_time),
        "close_time": _iso(
            open_time + timedelta(minutes=5) - timedelta(milliseconds=1)
        ),
        "open": str(base),
        "high": str(base + 2),
        "low": str(base - 1),
        "close": str(base + 1),
        "volume": "10",
        "quote_volume": "1000",
        "trade_count": 10 + offset,
    }


def _catalog_row(symbol: str) -> dict[str, Any]:
    return {
        "dataset_id": _dataset(symbol),
        "schema_major": 1,
        "default_fields": list(BAR_FIELDS),
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


def _metadata(symbol: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid"},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "providers": ["binance_spot"],
            "transport_service": "quicksync",
        },
        "receipt_id": f"receipt-{symbol}",
        "data_through": _iso(WINDOW_END - timedelta(milliseconds=1)),
        "observed_at": _iso(WINDOW_END + timedelta(seconds=20)),
        "reasons": [],
    }


class _Transport:
    def __init__(self) -> None:
        self.rows = {
            _dataset(symbol): [_row(symbol, item) for item in range(13)]
            for symbol in OBSERVATION_SYMBOLS
        }
        self.metadata = {
            dataset: _metadata(symbol)
            for symbol, dataset in (
                (_symbol, _dataset(_symbol)) for _symbol in OBSERVATION_SYMBOLS
            )
        }
        self.catalog_rows = [_catalog_row(symbol) for symbol in OBSERVATION_SYMBOLS]
        self.calls: list[dict[str, Any]] = []
        self.forced_cursor_dataset: str | None = None

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "catalog",
                    "data": self.catalog_rows,
                },
            )
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        dataset = body["dataset_id"]
        payload = {
            "api_version": "v1",
            "catalog_version": CATALOG_VERSION,
            "request_id": f"request-{dataset}",
            "dataset_id": dataset,
            "data": self.rows[dataset],
            "next_cursor": None,
            "metadata": self.metadata[dataset],
        }
        if self.forced_cursor_dataset == dataset:
            payload["next_cursor"] = "not-terminal"
        return HTTPResponse(200, payload)


def _client(transport: _Transport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="http://127.0.0.1:18083",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset(_dataset(symbol) for symbol in OBSERVATION_SYMBOLS),
            access_policy_id="fixture-observation",
            catalog_version_policy="strict",
            timeout_seconds=1.0,
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _window() -> CryptoObservationWindow:
    return CryptoObservationWindow(window_end=WINDOW_END, observation_cutoff=CUTOFF)


def test_collects_the_exact_ten_symbol_closed_bar_cohort_without_authority() -> None:
    transport = _Transport()
    report = collect_market_observation(
        _client(transport), expected_catalog_version=CATALOG_VERSION, window=_window()
    )

    assert tuple(source.symbol for source in report.sources) == OBSERVATION_SYMBOLS
    assert all(
        source.row_count == 13 and source.page_count == 1 for source in report.sources
    )
    assert report.authority == "none"
    assert report.execution_eligible is False
    assert report.capital_write_eligible is False
    assert report.model_authority is False
    assert len(report.observation_sha256) == 64
    queries = [
        item["json_body"] for item in transport.calls if item["method"] == "POST"
    ]
    assert len(queries) == 10
    assert all(query["limit"] == 13 and "cursor" not in query for query in queries)
    assert all(query["fields"] == list(BAR_FIELDS) for query in queries)


def test_rejects_a_gap_even_when_metadata_is_ready() -> None:
    transport = _Transport()
    rows = transport.rows[_dataset("BTCUSDT")]
    rows[8]["close_time"] = rows[8]["open_time"]
    with pytest.raises(CryptoMarketObservationError, match="bar_continuity"):
        collect_market_observation(
            _client(transport),
            expected_catalog_version=CATALOG_VERSION,
            window=_window(),
        )


def test_rejects_cursor_truncation_and_stale_metadata() -> None:
    transport = _Transport()
    transport.forced_cursor_dataset = _dataset("ETHUSDT")
    with pytest.raises(Exception, match="pagination"):
        collect_market_observation(
            _client(transport),
            expected_catalog_version=CATALOG_VERSION,
            window=_window(),
        )

    transport = _Transport()
    transport.metadata[_dataset("SOLUSDT")]["freshness"] = {
        "state": "stale",
        "stale": True,
    }
    with pytest.raises(CryptoMarketObservationError, match="metadata"):
        collect_market_observation(
            _client(transport),
            expected_catalog_version=CATALOG_VERSION,
            window=_window(),
        )


def test_window_rejects_unaligned_bar_end() -> None:
    with pytest.raises(CryptoMarketObservationError, match="window_end"):
        CryptoObservationWindow(
            window_end=WINDOW_END + timedelta(seconds=1), observation_cutoff=CUTOFF
        )
