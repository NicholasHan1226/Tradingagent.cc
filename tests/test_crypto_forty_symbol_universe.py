"""Versioned forty-symbol universe tests for the Crypto observation lane.

These tests prove the new universe/profile/store/factor-set family is distinct
from the frozen ten-symbol chain: the ten-symbol constants and append-only
store are never mutated, and the forty-symbol family fails closed against
ten-symbol payloads (and vice versa).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from Crypto.factor_research import (
    FORTY_SYMBOL_FACTOR_SET_ID,
    FORTY_SYMBOL_FACTOR_SET_VERSION,
    TEN_SYMBOL_FACTOR_SET_ID,
    TEN_SYMBOL_FACTOR_SET_VERSION,
)
from Crypto.market_observation import (
    FIVE_MINUTES,
    FORTY_SYMBOL_BARS_SIDECAR_CONTRACT,
    OBSERVATION_SYMBOLS,
    OBSERVATION_SYMBOLS_V40,
    TEN_SYMBOL_BARS_SIDECAR_CONTRACT,
    CryptoMarketObservationError,
    CryptoObservationWindow,
    _collect_market_observation_rows_with_catalog,
    build_ten_symbol_bars_sidecar,
    observation_from_ten_symbol_bars_sidecar,
)
from Crypto.ten_symbol_factor_research import (
    FORTY_SYMBOL_FACTOR_RESEARCH_CONFIG,
    FORTY_SYMBOL_SEGMENTED_LEARNING_CONSUMER_PROFILE_ID,
    SEGMENTED_LEARNING_CONSUMER_PROFILE_ID,
    TEN_SYMBOL_FACTOR_RESEARCH_CONFIG,
    _segmented_learning_consumer_profile,
)
from Crypto.ten_symbol_observation_profile import (
    FORTY_SYMBOL_PROFILE_CONTRACT,
    TEN_SYMBOL_PROFILE_CONTRACT,
    build_forty_symbol_observation_profile,
    load_forty_symbol_observation_profile_payload,
    load_ten_symbol_observation_profile_payload,
)
from Crypto.ten_symbol_observation_store import (
    FORTY_SYMBOL_CONTRACTS,
    TEN_SYMBOL_CONTRACTS,
    TEN_SYMBOL_EVENT_CONTRACT,
    FORTY_SYMBOL_EVENT_CONTRACT,
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from tests.test_crypto_ten_symbol_support import (
    CATALOG_VERSION,
    bar_dataset,
    catalog_row,
    query_metadata,
)


FORTY_CATALOG_VERSION = "fixture-crypto-forty-symbol-observation-v1"
FORTY_WINDOW_END = datetime(2026, 8, 10, 1, 5, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _forty_catalog_payload() -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": FORTY_CATALOG_VERSION,
        "request_id": "fixture-forty-symbol-catalog",
        "data": [catalog_row(symbol) for symbol in OBSERVATION_SYMBOLS_V40],
    }


def _forty_generated_rows(
    symbol: str,
    first_open: datetime,
    *,
    count: int = 13,
) -> list[dict[str, Any]]:
    base = Decimal("100") + Decimal(OBSERVATION_SYMBOLS_V40.index(symbol) * 10)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        open_time = first_open + index * FIVE_MINUTES
        price = base + Decimal(index)
        rows.append(
            {
                "symbol": symbol,
                "open_time": _iso(open_time),
                "close_time": _iso(open_time + FIVE_MINUTES - timedelta(milliseconds=1)),
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


class FortySymbolFixtureTransport:
    def __init__(self, *, observed_at: datetime | None = None) -> None:
        self.observed_at = observed_at

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        if kwargs["method"] == "GET":
            return HTTPResponse(200, _forty_catalog_payload())
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        dataset_id = body["dataset_id"]
        symbol = str(body["filters"]["symbol"]["eq"])
        between = body["filters"]["open_time"]["between"]
        first_open = datetime.fromisoformat(str(between[0]).replace("Z", "+00:00"))
        last_open = datetime.fromisoformat(str(between[1]).replace("Z", "+00:00"))
        rows = _forty_generated_rows(symbol, first_open, count=13)
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
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": FORTY_CATALOG_VERSION,
                "request_id": f"fixture-query-{dataset_id}",
                "dataset_id": dataset_id,
                "data": rows[: int(body["limit"])],
                "next_cursor": None,
                "metadata": metadata,
            },
        )


def _forty_client() -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="http://127.0.0.1:18083",
            expected_catalog_version=FORTY_CATALOG_VERSION,
            dataset_ids=frozenset(bar_dataset(s) for s in OBSERVATION_SYMBOLS_V40),
            access_policy_id="fixture-forty-symbol-observation",
            catalog_version_policy="strict",
            timeout_seconds=1.0,
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=FortySymbolFixtureTransport(),
    )


def test_observation_symbols_v40_is_frozen_and_superset_of_ten() -> None:
    assert len(OBSERVATION_SYMBOLS_V40) == 40
    assert len(set(OBSERVATION_SYMBOLS_V40)) == 40
    assert OBSERVATION_SYMBOLS_V40[:2] == ("BTCUSDT", "ETHUSDT")
    assert set(OBSERVATION_SYMBOLS_V40[:10]) == set(OBSERVATION_SYMBOLS)
    # The ten-symbol frozen constant is untouched.
    assert len(OBSERVATION_SYMBOLS) == 10


def test_forty_symbol_profile_roundtrip_and_distinct_contract() -> None:
    catalog = _forty_client().get_catalog()
    profile = build_forty_symbol_observation_profile(
        catalog,
        expected_catalog_version=FORTY_CATALOG_VERSION,
    )
    assert len(profile.datasets) == 40
    assert tuple(dataset.symbol for dataset in profile.datasets) == OBSERVATION_SYMBOLS_V40
    assert profile.profile_contract == FORTY_SYMBOL_PROFILE_CONTRACT

    loaded = load_forty_symbol_observation_profile_payload(profile.to_payload())
    assert loaded.profile_sha256 == profile.profile_sha256
    assert loaded.consumer_profile_sha256 == profile.consumer_profile_sha256

    # A ten-symbol profile payload is not a forty-symbol profile.
    with pytest.raises(Exception):
        load_forty_symbol_observation_profile_payload(
            {
                **profile.to_payload(),
                "contract": TEN_SYMBOL_PROFILE_CONTRACT,
            }
        )


def test_profile_families_reject_each_other() -> None:
    catalog = _forty_client().get_catalog()
    forty = build_forty_symbol_observation_profile(
        catalog,
        expected_catalog_version=FORTY_CATALOG_VERSION,
    )
    # Loading a forty profile with the ten-symbol loader must fail closed.
    with pytest.raises(Exception):
        load_ten_symbol_observation_profile_payload(forty.to_payload())


def test_forty_symbol_store_contracts_fail_closed_cross_family(
    tmp_path: Any,
) -> None:
    forty_root = tmp_path / "forty"
    ten_root = tmp_path / "ten"
    forty_store = CryptoTenSymbolObservationStore(
        forty_root, contracts=FORTY_SYMBOL_CONTRACTS
    )
    ten_store = CryptoTenSymbolObservationStore(
        ten_root, contracts=TEN_SYMBOL_CONTRACTS
    )
    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="event_contract_invalid",
    ):
        forty_store.append_event(
            {"contract": TEN_SYMBOL_EVENT_CONTRACT, "event_type": "data_reject"}
        )
    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="event_contract_invalid",
    ):
        ten_store.append_event(
            {"contract": FORTY_SYMBOL_EVENT_CONTRACT, "event_type": "data_reject"}
        )


def test_forty_symbol_factor_research_config_is_v3() -> None:
    config = FORTY_SYMBOL_FACTOR_RESEARCH_CONFIG
    assert config.feature_set_id == FORTY_SYMBOL_FACTOR_SET_ID
    assert config.feature_set_version == FORTY_SYMBOL_FACTOR_SET_VERSION
    assert config.consumer_profile_id == (
        FORTY_SYMBOL_SEGMENTED_LEARNING_CONSUMER_PROFILE_ID
    )
    assert config.projection_namespace == "forty_symbol_factor_research"
    assert config.symbols == OBSERVATION_SYMBOLS_V40

    consumer = _segmented_learning_consumer_profile(config)
    assert consumer["symbols"] == list(OBSERVATION_SYMBOLS_V40)
    assert consumer["feature_set_id"] == FORTY_SYMBOL_FACTOR_SET_ID
    assert consumer["feature_set_version"] == FORTY_SYMBOL_FACTOR_SET_VERSION

    # The ten-symbol v2 config stays frozen.
    ten = _segmented_learning_consumer_profile(TEN_SYMBOL_FACTOR_RESEARCH_CONFIG)
    assert ten["symbols"] == list(OBSERVATION_SYMBOLS)
    assert ten["feature_set_id"] == TEN_SYMBOL_FACTOR_SET_ID
    assert ten["feature_set_version"] == TEN_SYMBOL_FACTOR_SET_VERSION
    assert ten["consumer_profile_id"] == SEGMENTED_LEARNING_CONSUMER_PROFILE_ID


def test_forty_symbol_bars_sidecar_roundtrip() -> None:
    client = _forty_client()
    catalog = client.get_catalog()
    window = CryptoObservationWindow(
        window_end=FORTY_WINDOW_END,
        observation_cutoff=FORTY_WINDOW_END + timedelta(seconds=55),
    )
    observation, rows_by_symbol = _collect_market_observation_rows_with_catalog(
        client,
        catalog=catalog,
        expected_catalog_version=FORTY_CATALOG_VERSION,
        window=window,
        symbols=OBSERVATION_SYMBOLS_V40,
    )
    assert len(observation.sources) == 40
    sidecar = build_ten_symbol_bars_sidecar(
        window=window,
        profile_sha256="a" * 64,
        observation=observation,
        rows_by_symbol=rows_by_symbol,
        bars_sidecar_contract=FORTY_SYMBOL_BARS_SIDECAR_CONTRACT,
    )
    assert sidecar["contract"] == FORTY_SYMBOL_BARS_SIDECAR_CONTRACT
    rebuilt, rebuilt_rows = observation_from_ten_symbol_bars_sidecar(
        sidecar,
        symbols=OBSERVATION_SYMBOLS_V40,
        bars_sidecar_contract=FORTY_SYMBOL_BARS_SIDECAR_CONTRACT,
    )
    assert rebuilt.observation_sha256 == observation.observation_sha256
    assert set(rebuilt_rows) == set(OBSERVATION_SYMBOLS_V40)

    # A forty-symbol sidecar is not a ten-symbol sidecar.
    with pytest.raises(CryptoMarketObservationError):
        observation_from_ten_symbol_bars_sidecar(
            sidecar,
            symbols=OBSERVATION_SYMBOLS,
            bars_sidecar_contract=TEN_SYMBOL_BARS_SIDECAR_CONTRACT,
        )
