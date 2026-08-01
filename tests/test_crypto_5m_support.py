from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from Crypto.five_minute_data import (
    CryptoBarFieldMap,
    CryptoDatasetQueryProfile,
    CryptoFiveMinuteDataProfile,
    CryptoFiveMinuteWindowRequest,
    CryptoInstrumentRuleFieldMap,
    CryptoQueryFilterBinding,
    CryptoSymbolDatasetBinding,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "fixture-crypto-binance-candidate-v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
BAR_DATASETS = {
    "BTCUSDT": "crypto.spot.binance.btcusdt.5m",
    "ETHUSDT": "crypto.spot.binance.ethusdt.5m",
}
RULE_DATASETS = {
    "BTCUSDT": "crypto.spot.binance.btcusdt.rules",
    "ETHUSDT": "crypto.spot.binance.ethusdt.rules",
}
ALL_DATASETS = frozenset((*BAR_DATASETS.values(), *RULE_DATASETS.values()))
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
RULE_FIELDS = (
    "symbol",
    "status",
    "base_asset",
    "quote_asset",
    "price_filter_tick_size",
    "lot_size_step_size",
    "lot_size_min_qty",
    "min_notional",
)
WINDOW_END = datetime(2026, 7, 19, 1, 5, tzinfo=timezone.utc)
OBSERVATION_CUTOFF = WINDOW_END + timedelta(seconds=30)


def iso(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    timespec = "milliseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def offset_iso(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    timespec = "milliseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec)


def catalog_row(
    dataset_id: str,
    fields: tuple[str, ...],
    order: tuple[str, ...],
    filter_operators: dict[str, list[str]],
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "schema_major": 1,
        "default_fields": list(fields),
        "default_order": list(order),
        "identity_fields": (
            ["symbol", "open_time"] if "open_time" in fields else ["symbol"]
        ),
        "fields": [
            {
                "name": field,
                "logical_type": ("integer" if field == "trade_count" else "text"),
                "nullable": False,
                "selectable": True,
                "filterable": field in filter_operators,
                "sortable": any(term.split(":", 1)[0] == field for term in order),
                "operators": filter_operators.get(field, []),
            }
            for field in fields
        ],
        "filter_operators": copy.deepcopy(filter_operators),
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
    common_text_ops = ["eq", "in"]
    timestamp_ops = ["eq", "in", "gte", "lte", "between"]
    generated: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        generated.append(
            catalog_row(
                BAR_DATASETS[symbol],
                BAR_FIELDS,
                ("symbol:asc", "open_time:asc"),
                {
                    "symbol": common_text_ops,
                    "open_time": timestamp_ops,
                    "close_time": timestamp_ops,
                },
            )
        )
        generated.append(
            catalog_row(
                RULE_DATASETS[symbol],
                RULE_FIELDS,
                ("symbol:asc",),
                {
                    "symbol": common_text_ops,
                    "status": common_text_ops,
                },
            )
        )
    generated.sort(key=lambda row: str(row["dataset_id"]))
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "fixture-catalog-request",
        "data": copy.deepcopy(rows if rows is not None else generated),
    }


def metadata(
    *,
    dataset_id: str,
    data_through: datetime,
    observed_at: datetime = WINDOW_END + timedelta(seconds=20),
    state: str = "ready",
    degraded: bool = False,
    freshness: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "degraded": degraded,
        "freshness": copy.deepcopy(
            freshness if freshness is not None else {"state": "fresh", "stale": False}
        ),
        "quality": copy.deepcopy(
            quality if quality is not None else {"state": "valid"}
        ),
        "lineage": copy.deepcopy(
            lineage
            if lineage is not None
            else {
                "complete": True,
                "provider_neutral": True,
                "providers": ["binance_spot_fixture"],
                "transport_service": "fixture_catalog_query_transport",
            }
        ),
        "receipt_id": f"fixture-receipt-{dataset_id}",
        "data_through": iso(data_through),
        "observed_at": iso(observed_at),
        "reasons": [],
    }


def bars_for_symbol(
    symbol: str,
    *,
    execution_open: Decimal | None = None,
) -> list[dict[str, Any]]:
    if symbol not in SYMBOLS:
        raise ValueError("unsupported fixture symbol")
    start = WINDOW_END - timedelta(minutes=65)
    base, increment, tick = {
        "BTCUSDT": (Decimal("50000.00"), Decimal("50.00"), Decimal("0.01")),
        "ETHUSDT": (Decimal("3000.00"), Decimal("5.00"), Decimal("0.01")),
    }[symbol]
    rows: list[dict[str, Any]] = []
    for index in range(13):
        open_time = start + timedelta(minutes=5 * index)
        open_price = (
            execution_open
            if index == 12 and execution_open is not None
            else base + increment * index
        )
        close = open_price + increment
        volume = Decimal("10") + index
        rows.append(
            {
                "symbol": symbol,
                "open_time": iso(open_time),
                # Binance kline closeTime is the inclusive last millisecond.
                "close_time": iso(
                    open_time + timedelta(minutes=5) - timedelta(milliseconds=1)
                ),
                "open": format(open_price, "f"),
                "high": format(close + tick * 10, "f"),
                "low": format(open_price - tick * 10, "f"),
                "close": format(close, "f"),
                "volume": format(volume, "f"),
                "quote_volume": format(volume * close, "f"),
                "trade_count": 100 + index,
            }
        )
    return rows


def bar_rows(
    *,
    execution_open_overrides: dict[str, Decimal] | None = None,
) -> list[dict[str, Any]]:
    return [
        row
        for symbol in SYMBOLS
        for row in bars_for_symbol(
            symbol,
            execution_open=(
                None
                if execution_open_overrides is None
                else execution_open_overrides.get(symbol)
            ),
        )
    ]


def rule_for_symbol(symbol: str) -> dict[str, Any]:
    base_asset, step, minimum = {
        "BTCUSDT": ("BTC", "0.000001", "0.00001"),
        "ETHUSDT": ("ETH", "0.0001", "0.001"),
    }[symbol]
    return {
        "symbol": symbol,
        "status": "TRADING",
        "base_asset": base_asset,
        "quote_asset": "USDT",
        "price_filter_tick_size": "0.01",
        "lot_size_step_size": step,
        "lot_size_min_qty": minimum,
        "min_notional": "10",
    }


class FixtureTradingDatasTransport:
    def __init__(
        self,
        *,
        bars: list[dict[str, Any]] | None = None,
        rules: list[dict[str, Any]] | None = None,
        metadata_by_dataset: dict[str, dict[str, Any]] | None = None,
        catalog_rows: list[dict[str, Any]] | None = None,
        replay_mutator: Callable[[str, list[dict[str, Any]]], None] | None = None,
        page_size_override: int | None = None,
        force_cursor_cycle: bool = False,
        status_code: int = 200,
    ) -> None:
        combined_bars = copy.deepcopy(bars if bars is not None else bar_rows())
        combined_rules = copy.deepcopy(
            rules
            if rules is not None
            else [rule_for_symbol(symbol) for symbol in SYMBOLS]
        )
        self.rows_by_dataset: dict[str, list[dict[str, Any]]] = {}
        for symbol in SYMBOLS:
            self.rows_by_dataset[BAR_DATASETS[symbol]] = [
                row for row in combined_bars if row.get("symbol") == symbol
            ]
            self.rows_by_dataset[RULE_DATASETS[symbol]] = [
                row for row in combined_rules if row.get("symbol") == symbol
            ]
        defaults: dict[str, dict[str, Any]] = {}
        for symbol in SYMBOLS:
            defaults[BAR_DATASETS[symbol]] = metadata(
                dataset_id=BAR_DATASETS[symbol],
                data_through=WINDOW_END - timedelta(milliseconds=1),
            )
            defaults[RULE_DATASETS[symbol]] = metadata(
                dataset_id=RULE_DATASETS[symbol],
                data_through=WINDOW_END + timedelta(seconds=5),
                observed_at=WINDOW_END + timedelta(seconds=10),
            )
        self.metadata_by_dataset = defaults
        if metadata_by_dataset:
            self.metadata_by_dataset.update(copy.deepcopy(metadata_by_dataset))
        self.catalog_rows = copy.deepcopy(catalog_rows)
        self.replay_mutator = replay_mutator
        self.page_size_override = page_size_override
        self.force_cursor_cycle = force_cursor_cycle
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []
        self._run_index = {dataset_id: -1 for dataset_id in ALL_DATASETS}

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            return HTTPResponse(200, catalog_payload(rows=self.catalog_rows))
        if self.status_code != 200:
            return HTTPResponse(self.status_code, {"error": "fixture failure"})
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        dataset_id = body["dataset_id"]
        if dataset_id not in self.rows_by_dataset:
            return HTTPResponse(404, {"error": "unknown fixture dataset"})
        cursor = body.get("cursor")
        if cursor is None:
            self._run_index[dataset_id] += 1
        run_index = self._run_index[dataset_id]
        rows = copy.deepcopy(self.rows_by_dataset[dataset_id])
        if run_index == 1 and self.replay_mutator is not None:
            self.replay_mutator(dataset_id, rows)
        filters = body.get("filters")
        if isinstance(filters, dict):
            open_time_filter = filters.get("open_time")
            if (
                isinstance(open_time_filter, dict)
                and isinstance(open_time_filter.get("between"), list)
                and len(open_time_filter["between"]) == 2
            ):
                lower = datetime.fromisoformat(
                    str(open_time_filter["between"][0]).replace("Z", "+00:00")
                )
                upper = datetime.fromisoformat(
                    str(open_time_filter["between"][1]).replace("Z", "+00:00")
                )
                rows = [
                    row
                    for row in rows
                    if lower
                    <= datetime.fromisoformat(
                        str(row["open_time"]).replace("Z", "+00:00")
                    )
                    <= upper
                ]
        if body.get("order") == ["symbol:asc", "open_time:desc"]:
            rows.reverse()
        offset = int(str(cursor).rsplit(":", 1)[-1]) if cursor else 0
        limit = (
            int(self.page_size_override)
            if self.page_size_override is not None
            else int(body["limit"])
        )
        page = rows[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            f"fixture-cursor:{next_offset}" if next_offset < len(rows) else None
        )
        if self.force_cursor_cycle and cursor is not None:
            next_cursor = str(cursor)
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": (f"fixture-query-{dataset_id}-{run_index}-{offset}"),
                "dataset_id": dataset_id,
                "data": page,
                "next_cursor": next_cursor,
                "metadata": copy.deepcopy(self.metadata_by_dataset[dataset_id]),
            },
        )


def client(transport: FixtureTradingDatasTransport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://fixture.crypto.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=ALL_DATASETS,
            access_policy_id="fixture-crypto-5m",
            max_limit=13,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def profile(
    tradingdatas_client: SharedSignalsV1Client,
) -> CryptoFiveMinuteDataProfile:
    catalog = tradingdatas_client.get_catalog()
    bindings: list[CryptoSymbolDatasetBinding] = []
    for symbol in SYMBOLS:
        bars = CryptoDatasetQueryProfile.from_catalog(
            catalog,
            expected_catalog_version=CATALOG_VERSION,
            dataset_id=BAR_DATASETS[symbol],
            expected_schema_major=1,
            selected_fields=BAR_FIELDS,
            query_order=("symbol:asc", "open_time:desc"),
            identity_fields=("symbol", "open_time"),
            filter_bindings=(
                CryptoQueryFilterBinding("symbol", "symbol", "eq"),
                CryptoQueryFilterBinding("open_time_window", "open_time", "between"),
            ),
            page_limit=13,
            max_pages=4,
            max_rows=30,
        )
        rules = CryptoDatasetQueryProfile.from_catalog(
            catalog,
            expected_catalog_version=CATALOG_VERSION,
            dataset_id=RULE_DATASETS[symbol],
            expected_schema_major=1,
            selected_fields=RULE_FIELDS,
            query_order=("symbol:asc",),
            identity_fields=("symbol",),
            filter_bindings=(
                CryptoQueryFilterBinding("symbol", "symbol", "eq"),
                CryptoQueryFilterBinding("active_status", "status", "eq"),
            ),
            page_limit=1,
            max_pages=1,
            max_rows=1,
        )
        bindings.append(
            CryptoSymbolDatasetBinding(
                symbol=symbol,
                bars=bars,
                instrument_rules=rules,
            )
        )
    return CryptoFiveMinuteDataProfile(
        mode="fixture_mock",
        catalog_version=CATALOG_VERSION,
        symbols=tuple(bindings),
        bar_fields=CryptoBarFieldMap(
            symbol="symbol",
            open_time="open_time",
            close_time="close_time",
            open="open",
            high="high",
            low="low",
            close="close",
            volume="volume",
            quote_volume="quote_volume",
            trade_count="trade_count",
        ),
        rule_fields=CryptoInstrumentRuleFieldMap(
            symbol="symbol",
            status="status",
            base_asset="base_asset",
            quote_asset="quote_asset",
            price_tick="price_filter_tick_size",
            quantity_step="lot_size_step_size",
            min_quantity="lot_size_min_qty",
            min_notional="min_notional",
        ),
        bar_close_time_semantics="inclusive_last_millisecond",
        bar_closed_semantics="dataset_contract_discards_open_bars",
        active_rule_status="TRADING",
        max_bar_observation_lag_seconds=600,
        max_rule_observation_lag_seconds=86400,
    )


def window_request() -> CryptoFiveMinuteWindowRequest:
    return CryptoFiveMinuteWindowRequest(
        window_end=WINDOW_END,
        observation_cutoff=OBSERVATION_CUTOFF,
    )
