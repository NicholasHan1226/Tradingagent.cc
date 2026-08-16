"""Frozen consumer profile for the ten-symbol observation accumulator.

The profile binds each of the ten 5-minute bar datasets to its canonical
TradingDatas catalog contract fingerprint plus one uniform consumer query
shape.  It deliberately does not reuse ``CryptoFiveMinuteDataProfile`` so the
BTC/ETH capital path stays untouched.  Any contract, field, identity, filter
or digest drift fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from Crypto.market_observation import (
    BAR_COUNT,
    BAR_FIELDS,
    OBSERVATION_SYMBOLS,
    OBSERVATION_SYMBOLS_V40,
    _catalog_row,
    _verify_catalog,
)
from shared.data.sharedsignals_v1 import CatalogEnvelope
from shared.governance.evidence_readiness import dataset_contract_fingerprint


TEN_SYMBOL_PROFILE_CONTRACT = "tradingagent.crypto.ten_symbol_observation_profile.v1"
FORTY_SYMBOL_PROFILE_CONTRACT = "tradingagent.crypto.forty_symbol_observation_profile.v1"
QUERY_ORDER = ("symbol:asc", "open_time:asc")
IDENTITY_FIELDS = ("symbol", "open_time")
FILTER_BINDINGS = (
    {"role": "symbol", "field": "symbol", "operator": "eq"},
    {"role": "open_time_window", "field": "open_time", "operator": "between"},
)
PAGE_LIMIT = BAR_COUNT
MAX_PAGES = 1
MAX_ROWS = BAR_COUNT


class CryptoTenSymbolProfileError(ValueError):
    """A fail-closed profile reason safe to surface without a payload."""


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
        raise CryptoTenSymbolProfileError("ten_symbol_profile_not_canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bar_dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


def consumer_profile_payload(
    profile_contract: str = TEN_SYMBOL_PROFILE_CONTRACT,
) -> dict[str, Any]:
    """The uniform bounded consumer query shape shared by every dataset."""

    return {
        "contract": profile_contract,
        "bar_count": BAR_COUNT,
        "selected_fields": list(BAR_FIELDS),
        "query_order": list(QUERY_ORDER),
        "identity_fields": list(IDENTITY_FIELDS),
        "filter_bindings": [dict(binding) for binding in FILTER_BINDINGS],
        "page_limit": PAGE_LIMIT,
        "max_pages": MAX_PAGES,
        "max_rows": MAX_ROWS,
    }


def consumer_profile_sha256(
    profile_contract: str = TEN_SYMBOL_PROFILE_CONTRACT,
) -> str:
    return _canonical_sha256(consumer_profile_payload(profile_contract))


@dataclass(frozen=True)
class TenSymbolDatasetContract:
    symbol: str
    dataset_id: str
    catalog_contract_sha256: str
    symbols: tuple[str, ...] = field(
        default=OBSERVATION_SYMBOLS,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if self.symbol not in self.symbols:
            raise CryptoTenSymbolProfileError("ten_symbol_profile_symbol_invalid")
        if self.dataset_id != _bar_dataset_id(self.symbol):
            raise CryptoTenSymbolProfileError("ten_symbol_profile_dataset_invalid")
        if not _is_sha256(self.catalog_contract_sha256):
            raise CryptoTenSymbolProfileError("ten_symbol_profile_contract_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "dataset_id": self.dataset_id,
            "catalog_contract_sha256": self.catalog_contract_sha256,
        }


@dataclass(frozen=True)
class CryptoTenSymbolObservationProfile:
    catalog_version: str
    datasets: tuple[TenSymbolDatasetContract, ...]
    consumer_profile_sha256: str
    profile_sha256: str
    symbols: tuple[str, ...] = field(
        default=OBSERVATION_SYMBOLS,
        compare=False,
        hash=False,
    )
    profile_contract: str = field(
        default=TEN_SYMBOL_PROFILE_CONTRACT,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_version, str) or not self.catalog_version:
            raise CryptoTenSymbolProfileError("ten_symbol_profile_catalog_invalid")
        if tuple(dataset.symbol for dataset in self.datasets) != self.symbols:
            raise CryptoTenSymbolProfileError("ten_symbol_profile_datasets_invalid")
        if self.consumer_profile_sha256 != consumer_profile_sha256(
            self.profile_contract
        ):
            raise CryptoTenSymbolProfileError("ten_symbol_profile_sha256_mismatch")
        if self.profile_sha256 != self._compute_profile_sha256():
            raise CryptoTenSymbolProfileError("ten_symbol_profile_sha256_mismatch")

    def _compute_profile_sha256(self) -> str:
        return _canonical_sha256(self.to_payload(include_digest=False))

    def to_payload(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract": self.profile_contract,
            "catalog_version": self.catalog_version,
            "consumer": consumer_profile_payload(self.profile_contract),
            "consumer_profile_sha256": self.consumer_profile_sha256,
            "datasets": [dataset.to_payload() for dataset in self.datasets],
        }
        if include_digest:
            payload["profile_sha256"] = self.profile_sha256
        return payload

    @classmethod
    def from_catalog(
        cls,
        catalog: CatalogEnvelope,
        *,
        expected_catalog_version: str,
        symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
        profile_contract: str = TEN_SYMBOL_PROFILE_CONTRACT,
    ) -> "CryptoTenSymbolObservationProfile":
        if not isinstance(catalog, CatalogEnvelope):
            raise CryptoTenSymbolProfileError("ten_symbol_profile_catalog_invalid")
        if (
            catalog.api_version != "v1"
            or catalog.catalog_version != expected_catalog_version
        ):
            raise CryptoTenSymbolProfileError(
                "ten_symbol_profile_catalog_version_drift"
            )
        datasets: list[TenSymbolDatasetContract] = []
        for symbol in symbols:
            dataset_id = _bar_dataset_id(symbol)
            # Reuse the market-observation hard catalog gates unchanged.
            _verify_catalog(catalog, dataset_id)
            row = _catalog_row(catalog, dataset_id)
            try:
                contract_sha256 = dataset_contract_fingerprint(row)
            except ValueError as exc:
                raise CryptoTenSymbolProfileError(
                    "ten_symbol_profile_contract_invalid"
                ) from exc
            datasets.append(
                TenSymbolDatasetContract(
                    symbol=symbol,
                    dataset_id=dataset_id,
                    catalog_contract_sha256=contract_sha256,
                    symbols=symbols,
                )
            )
        consumer_sha256 = consumer_profile_sha256(profile_contract)
        material = {
            "contract": profile_contract,
            "catalog_version": expected_catalog_version,
            "consumer": consumer_profile_payload(profile_contract),
            "consumer_profile_sha256": consumer_sha256,
            "datasets": [dataset.to_payload() for dataset in datasets],
        }
        return cls(
            catalog_version=expected_catalog_version,
            datasets=tuple(datasets),
            consumer_profile_sha256=consumer_sha256,
            profile_sha256=_canonical_sha256(material),
            symbols=symbols,
            profile_contract=profile_contract,
        )

    def verify_catalog(self, catalog: CatalogEnvelope) -> None:
        if not isinstance(catalog, CatalogEnvelope) or catalog.api_version != "v1":
            raise CryptoTenSymbolProfileError("ten_symbol_profile_catalog_invalid")
        for expected in self.datasets:
            _verify_catalog(catalog, expected.dataset_id)
            row = _catalog_row(catalog, expected.dataset_id)
            try:
                observed_sha256 = dataset_contract_fingerprint(row)
            except ValueError as exc:
                raise CryptoTenSymbolProfileError(
                    "ten_symbol_profile_contract_invalid"
                ) from exc
            if observed_sha256 != expected.catalog_contract_sha256:
                raise CryptoTenSymbolProfileError(
                    "ten_symbol_profile_contract_drift"
                )


def load_ten_symbol_observation_profile_payload(
    payload: Mapping[str, Any],
    *,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    profile_contract: str = TEN_SYMBOL_PROFILE_CONTRACT,
) -> CryptoTenSymbolObservationProfile:
    if not isinstance(payload, Mapping):
        raise CryptoTenSymbolProfileError("ten_symbol_profile_payload_invalid")
    if set(payload) != {
        "contract",
        "catalog_version",
        "consumer",
        "consumer_profile_sha256",
        "datasets",
        "profile_sha256",
    } or payload.get("contract") != profile_contract:
        raise CryptoTenSymbolProfileError("ten_symbol_profile_payload_invalid")
    if payload.get("consumer") != consumer_profile_payload(profile_contract):
        raise CryptoTenSymbolProfileError("ten_symbol_profile_consumer_drift")
    raw_datasets = payload.get("datasets")
    if not isinstance(raw_datasets, list):
        raise CryptoTenSymbolProfileError("ten_symbol_profile_payload_invalid")
    datasets: list[TenSymbolDatasetContract] = []
    for item in raw_datasets:
        if not isinstance(item, Mapping) or set(item) != {
            "symbol",
            "dataset_id",
            "catalog_contract_sha256",
        }:
            raise CryptoTenSymbolProfileError("ten_symbol_profile_payload_invalid")
        try:
            datasets.append(
                TenSymbolDatasetContract(
                    symbol=item["symbol"],
                    dataset_id=item["dataset_id"],
                    catalog_contract_sha256=item["catalog_contract_sha256"],
                    symbols=symbols,
                )
            )
        except (TypeError, KeyError) as exc:
            raise CryptoTenSymbolProfileError(
                "ten_symbol_profile_payload_invalid"
            ) from exc
    catalog_version = payload.get("catalog_version")
    if not isinstance(catalog_version, str) or not catalog_version:
        raise CryptoTenSymbolProfileError("ten_symbol_profile_payload_invalid")
    return CryptoTenSymbolObservationProfile(
        catalog_version=catalog_version,
        datasets=tuple(datasets),
        consumer_profile_sha256=str(payload.get("consumer_profile_sha256")),
        profile_sha256=str(payload.get("profile_sha256")),
        symbols=symbols,
        profile_contract=profile_contract,
    )


def build_forty_symbol_observation_profile(
    catalog: CatalogEnvelope,
    *,
    expected_catalog_version: str,
) -> CryptoTenSymbolObservationProfile:
    """Build the versioned forty-symbol observation profile from one catalog."""

    return CryptoTenSymbolObservationProfile.from_catalog(
        catalog,
        expected_catalog_version=expected_catalog_version,
        symbols=OBSERVATION_SYMBOLS_V40,
        profile_contract=FORTY_SYMBOL_PROFILE_CONTRACT,
    )


def load_forty_symbol_observation_profile_payload(
    payload: Mapping[str, Any],
) -> CryptoTenSymbolObservationProfile:
    """Load and verify a versioned forty-symbol observation profile payload."""

    return load_ten_symbol_observation_profile_payload(
        payload,
        symbols=OBSERVATION_SYMBOLS_V40,
        profile_contract=FORTY_SYMBOL_PROFILE_CONTRACT,
    )


__all__ = [
    "FILTER_BINDINGS",
    "IDENTITY_FIELDS",
    "MAX_PAGES",
    "MAX_ROWS",
    "PAGE_LIMIT",
    "QUERY_ORDER",
    "FORTY_SYMBOL_PROFILE_CONTRACT",
    "TEN_SYMBOL_PROFILE_CONTRACT",
    "CryptoTenSymbolObservationProfile",
    "CryptoTenSymbolProfileError",
    "TenSymbolDatasetContract",
    "build_forty_symbol_observation_profile",
    "consumer_profile_payload",
    "consumer_profile_sha256",
    "load_forty_symbol_observation_profile_payload",
    "load_ten_symbol_observation_profile_payload",
]
