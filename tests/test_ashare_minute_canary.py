from __future__ import annotations

import copy
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any

import pytest

from Ashare import minute_canary as minute_canary_module
from Ashare.minute_canary import (
    MinuteCanaryConfig,
    MinuteCanaryConfigurationError,
    load_minute_canary_config,
    load_reference_facts,
    run_minute_canary,
)
from Ashare.minute_data import MinuteDataContractError, MinuteReferenceFact
from shared.data.sharedsignals_v1 import HTTPResponse, SharedSignalsV1Client


CATALOG = "fixture-rt-min-v1"
DATASET = "fixture.cn.dataset.rt_min"


class _Transport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(kwargs)
        if kwargs["method"] == "GET":
            fields = [
                "ts_code",
                "time",
                "open",
                "high",
                "low",
                "close",
                "vol",
                "amount",
            ]
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG,
                    "request_id": "catalog-1",
                    "data": [
                        {
                            "dataset_id": DATASET,
                            "schema_major": 1,
                            "default_fields": fields,
                            "default_order": ["ts_code:asc", "time:asc"],
                            "identity_fields": ["ts_code", "time"],
                            "fields": [
                                {
                                    "name": field,
                                    "selectable": True,
                                    "filterable": True,
                                    "sortable": True,
                                    "operators": [
                                        "eq",
                                        "in",
                                        "gte",
                                        "lte",
                                        "between",
                                    ],
                                }
                                for field in fields
                            ],
                            "filter_operators": {
                                field: ["eq", "in", "gte", "lte", "between"]
                                for field in fields
                            },
                            "limits": {
                                "max_page_size": 10,
                                "max_lookback_days": 1,
                            },
                            "availability": {"activation_states": ["active"]},
                        }
                    ],
                },
            )
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG,
                "request_id": f"query-{len(self.calls)}",
                "dataset_id": DATASET,
                "data": [
                    {
                        "ts_code": "600000.SH",
                        "time": "2026-07-28 09:35:00",
                        "open": 10.0,
                        "high": 10.2,
                        "low": 9.9,
                        "close": 10.1,
                        "vol": 10_000,
                        "amount": 101_000,
                    }
                ],
                "next_cursor": None,
                "metadata": {
                    "state": "ready",
                    "runtime_state": "success",
                    "degraded": False,
                    "freshness": {"state": "fresh", "stale": False},
                    "quality": {"state": "valid"},
                    "lineage": {
                        "complete": True,
                        "provider_neutral": True,
                        "providers": ["fixture"],
                        "transport_service": "quicksync",
                    },
                    "receipt_id": "receipt-rt-min-1",
                    "data_through": "2026-07-28T09:35:00+08:00",
                    "observed_at": "2026-07-28T09:35:20+08:00",
                    "reasons": [],
                },
            },
        )


def _config() -> MinuteCanaryConfig:
    profile: dict[str, Any] = {
        "identity_fields": ["ts_code", "time"],
        "symbol_field": "ts_code",
        "timestamp_field": "time",
        "open_field": "open",
        "high_field": "high",
        "low_field": "low",
        "close_field": "close",
        "volume_field": "vol",
        "amount_field": "amount",
        "previous_close_field": None,
        "suspension_field": None,
        "frequency_field": None,
        "frequency_value": None,
        "timestamp_format": "%Y-%m-%d %H:%M:%S",
        "timestamp_semantics": "bar_end",
        "volume_multiplier_to_shares": 1,
        "amount_multiplier_to_cny": 1,
        "price_adjustment": "raw_unadjusted",
        "max_pages": 1,
        "max_rows": 10,
        "page_limit": 10,
    }
    bootstrap = MinuteCanaryConfig(
        base_url="https://tradingdatas.fixture.invalid",
        expected_catalog_version=CATALOG,
        dataset_id=DATASET,
        access_policy_id="fixture-ta-read",
        transport_id="http-json-v1",
        timeout_seconds=5,
        filters={"ts_code": {"in": ["600000.SH"]}},
        profile=profile,
    )
    client = SharedSignalsV1Client(bootstrap.client_config(), transport=_Transport())
    bound = bootstrap.build_profile(client, require_declared_bindings=False)
    profile.update(
        {
            "dataset_contract_fingerprint": bound.dataset_contract_fingerprint,
            "consumer_profile_sha256": bound.consumer_profile_sha256,
        }
    )
    return MinuteCanaryConfig(
        base_url=bootstrap.base_url,
        expected_catalog_version=bootstrap.expected_catalog_version,
        dataset_id=bootstrap.dataset_id,
        access_policy_id=bootstrap.access_policy_id,
        transport_id=bootstrap.transport_id,
        timeout_seconds=bootstrap.timeout_seconds,
        filters=bootstrap.filters,
        profile=profile,
    )


def test_read_only_canary_uses_catalog_query_and_same_observation() -> None:
    transport = _Transport()

    def factory(*args: Any, **kwargs: Any) -> _Transport:
        assert args == ("http-json-v1",)
        assert kwargs["base_url"] == "https://tradingdatas.fixture.invalid"
        assert kwargs["token_file"] == Path("/run/secrets/fixture.token")
        return transport

    receipt = run_minute_canary(
        _config(),
        token_file=Path("/run/secrets/fixture.token"),
        decision_time=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
        trading_date=date(2026, 7, 28),
        reference_facts={
            "600000.SH": MinuteReferenceFact(
                symbol="600000.SH",
                trade_date=date(2026, 7, 28),
                previous_close_cny=9.98,
                suspended=False,
                evidence_sha256="a" * 64,
            )
        },
        transport_factory=factory,
    )

    assert receipt["status"] == "pass"
    assert receipt["expected_catalog_version"] == CATALOG
    assert receipt["observed_catalog_version"] == CATALOG
    assert receipt["catalog_version_drift"] is False
    assert len(receipt["dataset_contract_fingerprint"]) == 64
    assert len(receipt["consumer_profile_sha256"]) == 64
    assert "catalog_version" not in receipt
    assert "catalog_contract_sha256" not in receipt
    assert receipt["authority_tier"] == "observation_only"
    assert receipt["real_trading_enabled"] is False
    assert receipt["row_count"] == 1
    assert receipt["same_observation"] is True
    assert [call["method"] for call in transport.calls] == [
        "GET",
        "GET",
        "POST",
        "POST",
    ]
    assert all("/v1/" in call["url"] for call in transport.calls)
    assert all("/tushare" not in call["url"] for call in transport.calls)


def test_canary_uses_explicit_evidence_only_catalog_policy() -> None:
    assert _config().client_config().catalog_version_policy == "evidence_only"


def test_canary_fails_closed_without_reference_fact() -> None:
    transport = _Transport()

    with pytest.raises(MinuteDataContractError, match="minute_reference_fact_missing"):
        run_minute_canary(
            _config(),
            token_file=Path("/run/secrets/fixture.token"),
            decision_time=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
            trading_date=date(2026, 7, 28),
            reference_facts={},
            transport_factory=lambda *args, **kwargs: transport,
        )


class _ExactSlotTransport(_Transport):
    """Return a later bar only when the caller fails to pin the exact slot."""

    def __init__(self) -> None:
        super().__init__()
        self.query_bodies: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        if kwargs["method"] == "GET":
            return super().__call__(**kwargs)
        body = copy.deepcopy(kwargs["json_body"])
        self.query_bodies.append(body)
        rows = [
            {
                "ts_code": "600000.SH",
                "time": "2026-08-10 13:10:00",
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "vol": 10_000,
                "amount": 101_000,
            },
            {
                "ts_code": "000333.SZ",
                "time": "2026-08-10 13:10:00",
                "open": 20.0,
                "high": 20.2,
                "low": 19.9,
                "close": 20.1,
                "vol": 20_000,
                "amount": 402_000,
            },
        ]
        later_rows = [
            {
                **row,
                "time": "2026-08-10 13:15:00",
            }
            for row in rows
        ]
        available_rows = rows + later_rows
        assert body["filters"] == {
            "time": {"eq": "2026-08-10 13:10:00"},
            "ts_code": {"in": ["000333.SZ", "600000.SH"]},
        }
        assert any(row["time"] == "2026-08-10 13:15:00" for row in available_rows)
        selected_rows = [
            row
            for row in available_rows
            if row["time"] == body["filters"]["time"]["eq"]
        ]
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG,
                "request_id": f"query-exact-{len(self.query_bodies)}",
                "dataset_id": DATASET,
                "data": selected_rows,
                "next_cursor": None,
                "metadata": {
                    "state": "ready",
                    "runtime_state": "success",
                    "degraded": False,
                    "freshness": {"state": "fresh", "stale": False},
                    "quality": {"state": "valid"},
                    "lineage": {
                        "complete": True,
                        "provider_neutral": True,
                        "providers": ["fixture"],
                        "transport_service": "quicksync",
                    },
                    "receipt_id": "receipt-exact-slot",
                    "data_through": "2026-08-10T13:10:00+08:00",
                    "observed_at": "2026-08-10T13:10:20+08:00",
                    "reasons": [],
                },
            },
        )


def test_canary_selects_exact_historical_slot_and_reference_universe() -> None:
    transport = _ExactSlotTransport()
    references = {
        symbol: MinuteReferenceFact(
            symbol=symbol,
            trade_date=date(2026, 8, 10),
            previous_close_cny=9.98 if symbol == "600000.SH" else 19.98,
            suspended=False,
            evidence_sha256="a" * 64,
        )
        for symbol in ("600000.SH", "000333.SZ")
    }

    receipt = run_minute_canary(
        _config(),
        token_file=Path("/run/secrets/fixture.token"),
        decision_time=datetime.fromisoformat("2026-08-10T13:10:25+08:00"),
        trading_date=date(2026, 8, 10),
        reference_facts=references,
        bar_end="2026-08-10 13:10:00",
        transport_factory=lambda *args, **kwargs: transport,
    )

    assert receipt["bar_end"] == "2026-08-10T13:10:00+08:00"
    assert receipt["reference_symbols"] == ["000333.SZ", "600000.SH"]
    assert receipt["receipt_id"] == "receipt-exact-slot"
    assert receipt["receipt_ids"] == ["receipt-exact-slot"]
    assert receipt["data_through"] == "2026-08-10T13:10:00+08:00"
    assert receipt["source_lineage_sha256"] == receipt["bars"][0][
        "source_lineage_sha256"
    ]
    assert receipt["replay"]["same_observation"] is True
    assert (
        receipt["replay"]["first_semantic_sha256"]
        == receipt["replay"]["replay_semantic_sha256"]
    )
    assert {bar["symbol"] for bar in receipt["bars"]} == set(references)
    assert all(bar["bar_end"] == receipt["bar_end"] for bar in receipt["bars"])
    assert all(bar["receipt_id"] == "receipt-exact-slot" for bar in receipt["bars"])
    assert len(transport.query_bodies) == 2


def test_canary_cli_passes_exact_bar_end_to_existing_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config()
    manifest = tmp_path / "minute.json"
    manifest.write_text(
        json.dumps(
            {
                "base_url": config.base_url,
                "expected_catalog_version": config.expected_catalog_version,
                "dataset_id": config.dataset_id,
                "access_policy_id": config.access_policy_id,
                "transport_id": config.transport_id,
                "timeout_seconds": config.timeout_seconds,
                "filters": dict(config.filters),
                "profile": dict(config.profile),
            }
        ),
        encoding="utf-8",
    )
    references = tmp_path / "references.json"
    references.write_text(
        json.dumps(
            [
                {
                    "symbol": "600000.SH",
                    "trade_date": "2026-08-10",
                    "previous_close_cny": 9.98,
                    "suspended": False,
                    "evidence_sha256": "a" * 64,
                },
                {
                    "symbol": "000333.SZ",
                    "trade_date": "2026-08-10",
                    "previous_close_cny": 19.98,
                    "suspended": False,
                    "evidence_sha256": "a" * 64,
                },
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "receipt.json"
    transport = _ExactSlotTransport()
    real_run = run_minute_canary

    def injected_run(config: MinuteCanaryConfig, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["bar_end"] == "2026-08-10 13:10:00"
        return real_run(
            config,
            **kwargs,
            transport_factory=lambda *args, **inner_kwargs: transport,
        )

    monkeypatch.setattr(minute_canary_module, "run_minute_canary", injected_run)
    status = minute_canary_module.main(
        [
            "--manifest",
            str(manifest),
            "--reference-facts",
            str(references),
            "--token-file",
            "/run/secrets/fixture.token",
            "--decision-time",
            "2026-08-10T13:10:25+08:00",
            "--trading-date",
            "2026-08-10",
            "--bar-end",
            "2026-08-10 13:10:00",
            "--output",
            str(output),
            "--json",
        ]
    )

    assert status == 0
    assert json.loads(output.read_text(encoding="utf-8"))["bar_end"] == (
        "2026-08-10T13:10:00+08:00"
    )
    assert len(transport.query_bodies) == 2
    assert "PASS" not in capsys.readouterr().out


def test_canary_rejects_exact_bar_end_from_another_trading_date() -> None:
    transport = _ExactSlotTransport()
    reference = MinuteReferenceFact(
        symbol="600000.SH",
        trade_date=date(2026, 8, 10),
        previous_close_cny=9.98,
        suspended=False,
        evidence_sha256="a" * 64,
    )

    with pytest.raises(
        MinuteCanaryConfigurationError, match="bar_end_trade_date_mismatch"
    ):
        run_minute_canary(
            _config(),
            token_file=Path("/run/secrets/fixture.token"),
            decision_time=datetime.fromisoformat("2026-08-10T13:10:25+08:00"),
            trading_date=date(2026, 8, 10),
            reference_facts={"600000.SH": reference},
            bar_end="2026-08-11 13:10:00",
            transport_factory=lambda *args, **kwargs: transport,
        )

    assert transport.query_bodies == []


def test_external_manifests_are_strict_and_secret_free(tmp_path: Path) -> None:
    manifest = tmp_path / "minute.json"
    manifest.write_text(
        json.dumps(
            {
                "base_url": _config().base_url,
                "expected_catalog_version": CATALOG,
                "dataset_id": DATASET,
                "access_policy_id": "fixture-ta-read",
                "transport_id": "http-json-v1",
                "timeout_seconds": 5,
                "filters": {"ts_code": {"in": ["600000.SH"]}},
                "profile": dict(_config().profile),
            }
        ),
        encoding="utf-8",
    )
    references = tmp_path / "references.json"
    references.write_text(
        json.dumps(
            [
                {
                    "symbol": "600000.SH",
                    "trade_date": "2026-07-28",
                    "previous_close_cny": 9.98,
                    "suspended": False,
                    "evidence_sha256": "a" * 64,
                }
            ]
        ),
        encoding="utf-8",
    )

    loaded_manifest = load_minute_canary_config(manifest)
    assert loaded_manifest.dataset_id == DATASET
    assert loaded_manifest.expected_catalog_version == CATALOG
    assert load_reference_facts(references)["600000.SH"].previous_close_cny == 9.98

    references.write_text("[]", encoding="utf-8")
    with pytest.raises(
        MinuteCanaryConfigurationError, match="minute_reference_manifest_invalid"
    ):
        load_reference_facts(references)


def test_external_manifest_accepts_legacy_catalog_version_only(tmp_path: Path) -> None:
    manifest = tmp_path / "legacy-minute.json"
    legacy_catalog_version = "v1-1e4560099e58a89e"
    manifest.write_text(
        json.dumps(
            {
                "base_url": _config().base_url,
                "catalog_version": legacy_catalog_version,
                "dataset_id": DATASET,
                "access_policy_id": "fixture-ta-read",
                "transport_id": "http-json-v1",
                "timeout_seconds": 5,
                "filters": {"ts_code": {"in": ["600000.SH"]}},
                "profile": dict(_config().profile),
            }
        ),
        encoding="utf-8",
    )

    assert (
        load_minute_canary_config(manifest).expected_catalog_version
        == legacy_catalog_version
    )


def test_external_manifest_rejects_conflicting_catalog_version_keys(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "conflicting-minute.json"
    manifest.write_text(
        json.dumps(
            {
                "base_url": _config().base_url,
                "expected_catalog_version": CATALOG,
                "catalog_version": "v1-legacy-conflict",
                "dataset_id": DATASET,
                "access_policy_id": "fixture-ta-read",
                "transport_id": "http-json-v1",
                "timeout_seconds": 5,
                "filters": {"ts_code": {"in": ["600000.SH"]}},
                "profile": dict(_config().profile),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        MinuteCanaryConfigurationError, match="catalog_version_compatibility_mismatch"
    ):
        load_minute_canary_config(manifest)


def test_external_manifest_rejects_missing_catalog_version_keys(tmp_path: Path) -> None:
    manifest = tmp_path / "missing-catalog-version-minute.json"
    manifest.write_text(
        json.dumps(
            {
                "base_url": _config().base_url,
                "dataset_id": DATASET,
                "access_policy_id": "fixture-ta-read",
                "transport_id": "http-json-v1",
                "timeout_seconds": 5,
                "filters": {"ts_code": {"in": ["600000.SH"]}},
                "profile": dict(_config().profile),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        MinuteCanaryConfigurationError, match="expected_catalog_version_invalid"
    ):
        load_minute_canary_config(manifest)
