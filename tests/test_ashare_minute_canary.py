from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any

import pytest

from Ashare.minute_canary import (
    MinuteCanaryConfig,
    MinuteCanaryConfigurationError,
    load_minute_canary_config,
    load_reference_facts,
    run_minute_canary,
)
from Ashare.minute_data import MinuteDataContractError, MinuteReferenceFact
from shared.data.sharedsignals_v1 import HTTPResponse


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
                        "provider": "fixture",
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
    return MinuteCanaryConfig(
        base_url="https://tradingdatas.fixture.invalid",
        catalog_version=CATALOG,
        dataset_id=DATASET,
        access_policy_id="fixture-ta-read",
        transport_id="http-json-v1",
        timeout_seconds=5,
        filters={"ts_code": {"in": ["600000.SH"]}},
        profile={
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
        },
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


def test_external_manifests_are_strict_and_secret_free(tmp_path: Path) -> None:
    manifest = tmp_path / "minute.json"
    manifest.write_text(
        json.dumps(
            {
                "base_url": _config().base_url,
                "catalog_version": CATALOG,
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

    assert load_minute_canary_config(manifest).dataset_id == DATASET
    assert load_reference_facts(references)["600000.SH"].previous_close_cny == 9.98

    references.write_text("[]", encoding="utf-8")
    with pytest.raises(
        MinuteCanaryConfigurationError, match="minute_reference_manifest_invalid"
    ):
        load_reference_facts(references)
