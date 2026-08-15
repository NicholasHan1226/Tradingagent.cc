from __future__ import annotations

import copy
from typing import Any, Callable

import pytest

from Crypto.market_observation import OBSERVATION_SYMBOLS, CryptoMarketObservationError
from Crypto.ten_symbol_observation_profile import (
    TEN_SYMBOL_PROFILE_CONTRACT,
    CryptoTenSymbolObservationProfile,
    CryptoTenSymbolProfileError,
    load_ten_symbol_observation_profile_payload,
)
from shared.data.sharedsignals_v1 import parse_catalog_envelope
from shared.governance.evidence_readiness import dataset_contract_fingerprint
from tests.test_crypto_ten_symbol_support import (
    CATALOG_VERSION,
    bar_dataset,
    catalog_payload,
    catalog_row,
)


def _catalog(rows: list[dict[str, Any]] | None = None) -> Any:
    return parse_catalog_envelope(catalog_payload(rows=rows))


def _profile() -> CryptoTenSymbolObservationProfile:
    return CryptoTenSymbolObservationProfile.from_catalog(
        _catalog(),
        expected_catalog_version=CATALOG_VERSION,
    )


def _catalog_with_row_mutation(
    mutate: Callable[[dict[str, Any]], None],
    *,
    symbol: str = "BTCUSDT",
) -> Any:
    rows = [catalog_row(item) for item in OBSERVATION_SYMBOLS]
    target = next(row for row in rows if row["dataset_id"] == bar_dataset(symbol))
    mutate(target)
    return _catalog(rows)


def test_profile_binds_ten_dataset_contracts_and_stable_shas() -> None:
    profile = _profile()

    assert tuple(dataset.symbol for dataset in profile.datasets) == OBSERVATION_SYMBOLS
    assert tuple(dataset.dataset_id for dataset in profile.datasets) == tuple(
        bar_dataset(symbol) for symbol in OBSERVATION_SYMBOLS
    )
    catalog = _catalog()
    for dataset in profile.datasets:
        row = next(
            item for item in catalog.data if item["dataset_id"] == dataset.dataset_id
        )
        assert dataset.catalog_contract_sha256 == dataset_contract_fingerprint(row)
    assert profile.catalog_version == CATALOG_VERSION
    assert len(profile.consumer_profile_sha256) == 64
    assert len(profile.profile_sha256) == 64
    assert profile.profile_sha256 == _profile().profile_sha256


def test_profile_verify_catalog_passes_on_unchanged_catalog() -> None:
    _profile().verify_catalog(_catalog())


def test_profile_payload_round_trip() -> None:
    profile = _profile()
    payload = profile.to_payload()

    assert payload["contract"] == TEN_SYMBOL_PROFILE_CONTRACT
    rebuilt = load_ten_symbol_observation_profile_payload(payload)
    assert rebuilt == profile


def test_profile_payload_tamper_fails_closed() -> None:
    payload = _profile().to_payload()
    payload["datasets"][0]["catalog_contract_sha256"] = "0" * 64

    with pytest.raises(
        CryptoTenSymbolProfileError,
        match="ten_symbol_profile_sha256_mismatch",
    ):
        load_ten_symbol_observation_profile_payload(payload)


def test_profile_payload_missing_dataset_fails_closed() -> None:
    payload = _profile().to_payload()
    payload["datasets"] = payload["datasets"][:-1]
    payload["profile_sha256"] = "0" * 64

    with pytest.raises(CryptoTenSymbolProfileError):
        load_ten_symbol_observation_profile_payload(payload)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda row: row.update({"schema_major": 2}),
            "crypto_observation_schema_major_drift",
        ),
        (
            lambda row: row.update({"default_fields": ["symbol", "open_time"]}),
            "crypto_observation_fields_drift",
        ),
        (
            lambda row: row.update({"identity_fields": ["symbol"]}),
            "crypto_observation_identity_drift",
        ),
        (
            lambda row: row["filter_operators"].update({"open_time": ["eq"]}),
            "crypto_observation_filters_drift",
        ),
        (
            lambda row: row.update({"queryability": {"queryable": False}}),
            "crypto_observation_dataset_not_queryable",
        ),
        (
            lambda row: row["limits"].update({"max_page_size": 12}),
            "crypto_observation_page_limit_invalid",
        ),
    ],
)
def test_profile_catalog_hard_gate_drift_fails_closed(
    mutate: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    catalog = _catalog_with_row_mutation(mutate)

    with pytest.raises(CryptoMarketObservationError, match=reason):
        CryptoTenSymbolObservationProfile.from_catalog(
            catalog,
            expected_catalog_version=CATALOG_VERSION,
        )


def test_profile_verify_detects_contract_fingerprint_drift() -> None:
    profile = _profile()
    drifted = _catalog_with_row_mutation(
        lambda row: row["limits"].update({"max_lookback_days": 36501})
    )

    with pytest.raises(
        CryptoTenSymbolProfileError,
        match="ten_symbol_profile_contract_drift",
    ):
        profile.verify_catalog(drifted)


def test_profile_verify_detects_catalog_version_drift() -> None:
    profile = _profile()
    catalog = _catalog()
    mutated = copy.deepcopy(catalog)
    object.__setattr__(mutated, "catalog_version", "fixture-other-version")

    with pytest.raises(
        CryptoTenSymbolProfileError,
        match="ten_symbol_profile_catalog_version_drift",
    ):
        profile.verify_catalog(mutated)


def test_profile_requires_the_exact_ten_symbol_cohort() -> None:
    rows = [catalog_row(symbol) for symbol in OBSERVATION_SYMBOLS[:-1]]
    catalog = _catalog(rows)

    with pytest.raises(
        CryptoMarketObservationError,
        match="crypto_observation_catalog_row_missing",
    ):
        CryptoTenSymbolObservationProfile.from_catalog(
            catalog,
            expected_catalog_version=CATALOG_VERSION,
        )
