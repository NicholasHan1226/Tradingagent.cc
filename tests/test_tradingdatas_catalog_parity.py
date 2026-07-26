from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from shared.data.sharedsignals_v1 import HTTPResponse


CATALOG_VERSION = "fixture-catalog-v1"
READY_ID = "fixture.market.ready"
IMPAIRED_ID = "fixture.market.impaired"
PAUSED_ID = "fixture.market.paused"
AS_OF = "2026-07-22T15:30:00+08:00"

NINE_ACTIVE_DATASETS = (
    (
        "cn.dataset.adj_factor",
        "impaired",
        1,
        ("ts_code", "trade_date", "adj_factor"),
        ("ts_code", "trade_date"),
        "trade_date",
    ),
    (
        "cn.dataset.index_classify",
        "ready",
        2,
        (
            "index_code",
            "industry_name",
            "parent_code",
            "level",
            "industry_code",
            "is_pub",
            "src",
        ),
        ("index_code",),
        None,
    ),
    (
        "cn.dataset.stk_auction",
        "impaired",
        1,
        (
            "ts_code",
            "trade_date",
            "vol",
            "price",
            "amount",
            "pre_close",
            "turnover_rate",
            "volume_ratio",
            "float_share",
        ),
        ("ts_code", "trade_date"),
        "trade_date",
    ),
    (
        "cn.dataset.stk_limit",
        "impaired",
        1,
        ("trade_date", "ts_code", "pre_close", "up_limit", "down_limit"),
        ("ts_code", "trade_date"),
        "trade_date",
    ),
    (
        "cn.dataset.suspend_d",
        "impaired",
        1,
        ("ts_code", "trade_date", "suspend_timing", "suspend_type"),
        ("ts_code", "trade_date"),
        "trade_date",
    ),
    (
        "cn.dataset.sw_daily",
        "ready",
        2,
        (
            "ts_code",
            "trade_date",
            "name",
            "open",
            "low",
            "high",
            "close",
            "change",
            "pct_change",
            "vol",
            "amount",
            "pe",
            "pb",
            "float_mv",
            "total_mv",
        ),
        ("ts_code", "trade_date"),
        "trade_date",
    ),
    (
        "cn.equity.daily",
        "ready",
        2,
        (
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
        ("ts_code", "trade_date"),
        "trade_date",
    ),
    (
        "cn.equity.security_master",
        "ready",
        2,
        (
            "ts_code",
            "symbol",
            "name",
            "area",
            "industry",
            "cnspell",
            "market",
            "list_date",
            "act_name",
            "act_ent_type",
        ),
        ("ts_code",),
        None,
    ),
    (
        "cn.market.trade_calendar",
        "ready",
        2,
        ("exchange", "cal_date", "is_open", "pretrade_date"),
        ("exchange", "cal_date"),
        "cal_date",
    ),
)


def _api():
    return importlib.import_module("shared.runtime_test.tradingdatas_catalog_parity")


def _dataset_specs() -> list[dict[str, Any]]:
    return [
        {
            "dataset_id": READY_ID,
            "expected_health": "ready",
            "schema_major": 2,
            "catalog_default_fields": ["symbol", "trade_date", "close"],
            "catalog_limits": {"default": 2, "max": 10},
            "filters": {"trade_date": {"eq": "20260722"}},
            "query_limit": 2,
            "minimum_row_count": 1,
            "identity_fields": ["symbol", "trade_date"],
            "observation": {
                "mode": "current_observation",
                "query_as_of_mode": "decision_as_of",
                "row_event_time_field": "trade_date",
                "row_event_time_format": "yyyymmdd",
                "row_event_timezone": "Asia/Shanghai",
                "row_event_time_semantic": "session",
            },
            "max_pages": 3,
            "max_rows": 10,
        },
        {
            "dataset_id": IMPAIRED_ID,
            "expected_health": "impaired",
            "schema_major": 2,
            "catalog_default_fields": ["sector_code", "trade_date", "close"],
            "catalog_limits": {"default": 2, "max": 10},
            "filters": {"trade_date": {"eq": "20260722"}},
            "query_limit": 2,
            "minimum_row_count": 0,
            "identity_fields": ["sector_code", "trade_date"],
            "observation": {
                "mode": "current_observation",
                "query_as_of_mode": "omit",
                "row_event_time_field": "trade_date",
                "row_event_time_format": "yyyymmdd",
                "row_event_timezone": "Asia/Shanghai",
                "row_event_time_semantic": "session",
            },
            "max_pages": 2,
            "max_rows": 4,
        },
    ]


def _manifest() -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "base_url": "https://tradingdatas.fixture.invalid",
        "catalog_version": CATALOG_VERSION,
        "access_policy_id": "ta-catalog-parity-fixture-v1",
        "transport_id": "urllib-bearer-v1",
        "timeout_seconds": 3,
        "as_of": AS_OF,
        "expected_counts": {"total": 3, "active": 2, "paused": 1},
        "datasets": _dataset_specs(),
    }


def _catalog_row(
    dataset_id: str,
    *,
    activation_state: str,
    schema_major: int = 2,
    default_fields: list[str] | None = None,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    defaults = {
        READY_ID: ["symbol", "trade_date", "close"],
        IMPAIRED_ID: ["sector_code", "trade_date", "close"],
        PAUSED_ID: ["unused"],
    }
    return {
        "dataset_id": dataset_id,
        "schema_major": schema_major,
        "default_fields": copy.deepcopy(default_fields or defaults[dataset_id]),
        "limits": copy.deepcopy(limits or {"default": 2, "max": 10}),
        "availability": {"activation_states": [activation_state]},
    }


def _catalog_payload(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "catalog-request",
        "data": rows
        or [
            _catalog_row(READY_ID, activation_state="active"),
            _catalog_row(IMPAIRED_ID, activation_state="active"),
            _catalog_row(PAUSED_ID, activation_state="paused"),
        ],
    }


def _metadata(*, health: str) -> dict[str, Any]:
    if health == "ready":
        state = "ready"
        degraded = False
        quality_state = "valid"
        quality_valid = True
        reasons: list[str] = []
    else:
        state = "partial"
        degraded = True
        quality_state = "partial"
        quality_valid = True
        reasons = ["provider-secret-context-must-not-leak"]
    return {
        "state": state,
        "degraded": degraded,
        "freshness": {"state": "fresh", "fresh": True},
        "quality": {"state": quality_state, "valid": quality_valid},
        "lineage": {
            "state": "complete",
            "complete": True,
            "provider_neutral": True,
            "provider": "fixture-provider",
            "transport_service": "fixture-transport",
        },
        "receipt_id": f"receipt-{health}",
        "data_through": "2026-07-22T15:00:00+08:00",
        "observed_at": "2026-07-22T15:01:00+08:00",
        "reasons": reasons,
    }


class FixtureTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.catalog = _catalog_payload()
        self.query_status = 200
        self.metadata_overrides: dict[tuple[str, int], dict[str, Any]] = {}
        self.query_rows_overrides: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.next_cursor_overrides: dict[tuple[str, int], str | None] = {}
        self.cursor_cycle = False
        self.ready_empty = False
        self._starts: dict[str, int] = {}
        self._query_calls: dict[str, int] = {}

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": copy.deepcopy(headers),
                "json_body": copy.deepcopy(json_body),
                "timeout_seconds": timeout_seconds,
            }
        )
        if method == "GET" and url.endswith("/v1/catalog"):
            return HTTPResponse(200, copy.deepcopy(self.catalog))
        if method != "POST" or not url.endswith("/v1/query"):
            raise AssertionError(f"unexpected route: {method} {url}")
        if self.query_status != 200:
            return HTTPResponse(self.query_status, {})
        assert json_body is not None
        dataset_id = str(json_body["dataset_id"])
        self._query_calls[dataset_id] = self._query_calls.get(dataset_id, 0) + 1
        call_number = self._query_calls[dataset_id]
        cursor = json_body.get("cursor")
        if cursor is None:
            run = self._starts.get(dataset_id, 0) + 1
            self._starts[dataset_id] = run
        else:
            run = self._starts[dataset_id]

        if dataset_id == READY_ID:
            if self.ready_empty:
                rows = []
                next_cursor = None
            elif cursor is None:
                rows = [{"symbol": "000001.SZ", "trade_date": "20260722", "close": 10}]
                next_cursor = f"opaque-cursor-run-{run}"
            else:
                rows = [{"symbol": "600000.SH", "trade_date": "20260722", "close": 20}]
                next_cursor = cursor if self.cursor_cycle else None
            health = "ready"
        elif dataset_id == IMPAIRED_ID:
            rows = []
            next_cursor = None
            health = "impaired"
        else:
            raise AssertionError(f"paused dataset queried: {dataset_id}")
        rows = copy.deepcopy(
            self.query_rows_overrides.get((dataset_id, call_number), rows)
        )
        next_cursor = self.next_cursor_overrides.get(
            (dataset_id, call_number), next_cursor
        )
        metadata = copy.deepcopy(
            self.metadata_overrides.get(
                (dataset_id, call_number), _metadata(health=health)
            )
        )
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"query-{dataset_id}-{call_number}",
                "dataset_id": dataset_id,
                "data": rows,
                "next_cursor": next_cursor,
                "metadata": metadata,
            },
        )


def _nine_active_manifest() -> dict[str, Any]:
    datasets: list[dict[str, Any]] = []
    for (
        dataset_id,
        health,
        schema_major,
        fields,
        identities,
        event_field,
    ) in NINE_ACTIVE_DATASETS:
        filters = (
            {event_field: {"eq": "20260722"}}
            if event_field in {"trade_date", "cal_date"}
            else {}
        )
        observation: dict[str, Any] = {
            "mode": "current_observation",
            "query_as_of_mode": "decision_as_of" if health == "ready" else "omit",
        }
        if event_field is not None:
            observation.update(
                {
                    "row_event_time_field": event_field,
                    "row_event_time_format": "yyyymmdd",
                    "row_event_timezone": "Asia/Shanghai",
                    "row_event_time_semantic": "session",
                }
            )
        datasets.append(
            {
                "dataset_id": dataset_id,
                "expected_health": health,
                "schema_major": schema_major,
                "catalog_default_fields": list(fields),
                "catalog_limits": {
                    "max_lookback_days": 36500,
                    "max_page_size": 500,
                },
                "filters": filters,
                "query_limit": 500,
                "minimum_row_count": 1 if health == "ready" else 0,
                "identity_fields": list(identities),
                "observation": observation,
                "max_pages": 20,
                "max_rows": 10000,
            }
        )
    return {
        "manifest_version": 1,
        "base_url": "https://tradingdatas.fixture.invalid",
        "catalog_version": CATALOG_VERSION,
        "access_policy_id": "ta-nine-active-fixture-v1",
        "transport_id": "urllib-bearer-v1",
        "timeout_seconds": 3,
        "as_of": AS_OF,
        "expected_counts": {"total": 190, "active": 9, "paused": 181},
        "datasets": datasets,
    }


class NineDatasetFixtureTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.specs = {item[0]: item for item in NINE_ACTIVE_DATASETS}
        active_rows = [
            _catalog_row(
                dataset_id,
                activation_state="active",
                schema_major=schema_major,
                default_fields=list(fields),
                limits={"max_lookback_days": 36500, "max_page_size": 500},
            )
            for dataset_id, _, schema_major, fields, _, _ in NINE_ACTIVE_DATASETS
        ]
        paused_rows = [
            _catalog_row(
                f"fixture.paused.{index:03d}",
                activation_state="paused",
                schema_major=1,
                default_fields=["identity"],
                limits={"max_lookback_days": 36500, "max_page_size": 500},
            )
            for index in range(181)
        ]
        self.catalog = _catalog_payload(active_rows + paused_rows)
        self._query_calls: dict[str, int] = {}

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": copy.deepcopy(headers),
                "json_body": copy.deepcopy(json_body),
                "timeout_seconds": timeout_seconds,
            }
        )
        if method == "GET" and url.endswith("/v1/catalog"):
            return HTTPResponse(200, copy.deepcopy(self.catalog))
        if method != "POST" or not url.endswith("/v1/query"):
            raise AssertionError(f"unexpected route: {method} {url}")
        assert json_body is not None
        dataset_id = str(json_body["dataset_id"])
        _, health, _, fields, identities, event_field = self.specs[dataset_id]
        self._query_calls[dataset_id] = self._query_calls.get(dataset_id, 0) + 1
        row: dict[str, Any] = {field: 1 for field in fields}
        for identity in identities:
            row[identity] = f"identity-{identity}"
        if event_field is not None:
            row[event_field] = "20260722"
        metadata = _metadata(health=health)
        metadata["receipt_id"] = f"receipt-{dataset_id}"
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"query-{dataset_id}-{self._query_calls[dataset_id]}",
                "dataset_id": dataset_id,
                "data": [row] if health == "ready" else [],
                "next_cursor": None,
                "metadata": metadata,
            },
        )


def _load_config(tmp_path: Path, payload: dict[str, Any] | None = None):
    path = tmp_path / "catalog-parity.json"
    path.write_text(json.dumps(payload or _manifest()), encoding="utf-8")
    return _api().load_catalog_parity_manifest(path.resolve())


def _identityless_impaired_manifest(*, max_rows: int) -> dict[str, Any]:
    payload = _manifest()
    impaired = payload["datasets"][1]
    impaired["query_limit"] = 500
    impaired["minimum_row_count"] = 0
    impaired["identity_fields"] = []
    impaired["max_pages"] = 1
    impaired["max_rows"] = max_rows
    return payload


def test_catalog_discovery_accounts_for_ready_and_impaired_active_sets(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "pass"
    assert receipt["blocking"] is False
    assert receipt["transport_contract_pass"] is True
    assert receipt["ready_set_pass"] is True
    assert receipt["impaired_set_accounted"] is True
    assert receipt["catalog"]["counts"] == {"total": 3, "active": 2, "paused": 1}
    assert [item["dataset_id"] for item in receipt["datasets"]] == [
        READY_ID,
        IMPAIRED_ID,
    ]
    ready, impaired = receipt["datasets"]
    assert ready["expected_health"] == "ready"
    assert ready["evidence_action"] == "accept"
    assert ready["effective_weight"] == 1.0
    assert ready["parity_data_accepted"] is True
    assert ready["research_snapshot_eligible"] is False
    assert ready["same_observation_match"] is True
    assert ready["page_count"] == 2
    assert impaired["expected_health"] == "impaired"
    assert impaired["evidence_action"] == "reject"
    assert impaired["effective_weight"] == 0.0
    assert impaired["research_snapshot_eligible"] is False
    assert impaired["same_observation_match"] is True
    assert [call["url"].rsplit("/", 2)[-2:] for call in transport.calls] == [
        ["v1", "catalog"],
        ["v1", "query"],
        ["v1", "query"],
        ["v1", "query"],
        ["v1", "query"],
        ["v1", "query"],
        ["v1", "query"],
    ]
    ready_payloads = [
        call["json_body"]
        for call in transport.calls
        if call["method"] == "POST" and call["json_body"]["dataset_id"] == READY_ID
    ]
    impaired_payloads = [
        call["json_body"]
        for call in transport.calls
        if call["method"] == "POST" and call["json_body"]["dataset_id"] == IMPAIRED_ID
    ]
    assert all(payload["schema_major"] == 2 for payload in ready_payloads)
    assert all(
        payload["fields"] == ["symbol", "trade_date", "close"]
        for payload in ready_payloads
    )
    assert all(payload["as_of"] == AS_OF for payload in ready_payloads)
    assert all("as_of" not in payload for payload in impaired_payloads)
    assert all("order" not in payload for payload in ready_payloads + impaired_payloads)

    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "tradingdatas.fixture.invalid" not in encoded
    assert "ta-catalog-parity-fixture-v1" not in encoded
    assert "opaque-cursor" not in encoded
    assert "provider-secret-context" not in encoded
    assert "symbol" not in encoded
    assert receipt["receipt_sha256"] == api.receipt_sha256(receipt)


def test_frozen_nine_active_shape_accounts_for_five_ready_and_four_impaired(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(tmp_path, _nine_active_manifest())
    transport = NineDatasetFixtureTransport()

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "pass"
    assert receipt["catalog"]["counts"] == {
        "total": 190,
        "active": 9,
        "paused": 181,
    }
    assert receipt["ready_set_pass"] is True
    assert receipt["impaired_set_accounted"] is True
    assert {item["dataset_id"] for item in receipt["datasets"]} == {
        item[0] for item in NINE_ACTIVE_DATASETS
    }
    assert sum(item["parity_data_accepted"] for item in receipt["datasets"]) == 5
    assert sum(item["effective_weight"] == 0.0 for item in receipt["datasets"]) == 4
    assert all(
        item["research_snapshot_eligible"] is False for item in receipt["datasets"]
    )
    query_calls = [call for call in transport.calls if call["method"] == "POST"]
    assert len(query_calls) == 18
    assert all(call["url"].endswith("/v1/query") for call in query_calls)
    assert all("order" not in call["json_body"] for call in query_calls)
    assert all(call["json_body"]["schema_major"] in {1, 2} for call in query_calls)


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_rejection_blocks_without_fallback(
    tmp_path: Path, status_code: int
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    transport.query_status = status_code

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["blocking"] is True
    assert receipt["transport_contract_pass"] is False
    assert receipt["reason_codes"] == ["query_contract_or_transport_failure"]
    assert [
        (call["method"], call["url"].rsplit("/", 1)[-1]) for call in transport.calls
    ] == [
        ("GET", "catalog"),
        ("POST", "query"),
    ]


def test_manifest_active_set_must_exactly_match_catalog_discovery(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    transport.catalog = _catalog_payload(
        [
            _catalog_row(READY_ID, activation_state="active"),
            _catalog_row(IMPAIRED_ID, activation_state="active"),
            _catalog_row(PAUSED_ID, activation_state="active"),
        ]
    )

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["reason_codes"] == ["catalog_contract_failure"]
    assert len(transport.calls) == 1


@pytest.mark.parametrize("drift", ["schema_major", "default_fields", "limits"])
def test_active_catalog_contract_drift_blocks_before_query(
    tmp_path: Path,
    drift: str,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    ready = _catalog_row(READY_ID, activation_state="active")
    if drift == "schema_major":
        ready["schema_major"] = 3
    elif drift == "default_fields":
        ready["default_fields"] = ["symbol"]
    else:
        ready["limits"] = {"default": 1, "max": 1}
    transport.catalog = _catalog_payload(
        [
            ready,
            _catalog_row(IMPAIRED_ID, activation_state="active"),
            _catalog_row(PAUSED_ID, activation_state="paused"),
        ]
    )

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["reason_codes"] == ["catalog_contract_failure"]
    assert len(transport.calls) == 1


def test_ready_dataset_becoming_degraded_is_not_reclassified_as_accounted_impaired(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    for call_number in (1, 2, 3, 4):
        transport.metadata_overrides[(READY_ID, call_number)] = _metadata(
            health="impaired"
        )

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["ready_set_pass"] is False
    assert receipt["impaired_set_accounted"] is True
    ready = receipt["datasets"][0]
    assert ready["expected_health"] == "ready"
    assert ready["evidence_action"] == "reject"
    assert ready["reason_codes"] == ["ready_dataset_rejected"]


def test_ready_dataset_below_explicit_minimum_row_count_is_rejected(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    transport.ready_empty = True

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["transport_contract_pass"] is True
    assert receipt["ready_set_pass"] is False
    ready = receipt["datasets"][0]
    assert ready["row_count"] == 0
    assert ready["minimum_row_count"] == 1
    assert ready["parity_data_accepted"] is False
    assert ready["research_snapshot_eligible"] is False
    assert ready["reason_codes"] == ["minimum_row_count_not_met"]


def test_impaired_dataset_becoming_ready_blocks_unexpected_accept(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    for call_number in (1, 2):
        transport.metadata_overrides[(IMPAIRED_ID, call_number)] = _metadata(
            health="ready"
        )

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["ready_set_pass"] is True
    assert receipt["impaired_set_accounted"] is False
    impaired = receipt["datasets"][1]
    assert impaired["evidence_action"] == "accept"
    assert impaired["reason_codes"] == ["impaired_dataset_unexpectedly_accepted"]


def test_null_source_proof_is_honestly_accounted_for_declared_impaired(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    for call_number in (1, 2):
        metadata = _metadata(health="impaired")
        metadata["lineage"] = None
        metadata["receipt_id"] = None
        metadata["data_through"] = None
        metadata["observed_at"] = None
        transport.metadata_overrides[(IMPAIRED_ID, call_number)] = metadata

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "pass"
    assert receipt["transport_contract_pass"] is True
    assert receipt["impaired_set_accounted"] is True
    assert receipt["datasets"][1]["source_proof_complete"] is False
    assert receipt["datasets"][1]["source_proof_sha256"] is None
    assert receipt["datasets"][1]["reason_codes"] == []
    assert receipt["datasets"][1]["accounting_reason_codes"] == [
        "source_proof_unavailable"
    ]


def test_source_proof_requires_provider_and_transport_lineage(tmp_path: Path) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    for call_number in (1, 2):
        metadata = _metadata(health="impaired")
        del metadata["lineage"]["transport_service"]
        transport.metadata_overrides[(IMPAIRED_ID, call_number)] = metadata

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "pass"
    assert receipt["transport_contract_pass"] is True
    assert receipt["impaired_set_accounted"] is True
    assert receipt["datasets"][1]["source_proof_complete"] is False
    assert receipt["datasets"][1]["accounting_reason_codes"] == [
        "source_proof_unavailable"
    ]


def test_ready_dataset_still_requires_complete_source_proof(tmp_path: Path) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    for call_number in (1, 2, 3, 4):
        metadata = _metadata(health="ready")
        metadata["lineage"] = None
        metadata["receipt_id"] = None
        metadata["data_through"] = None
        metadata["observed_at"] = None
        metadata["degraded"] = True
        transport.metadata_overrides[(READY_ID, call_number)] = metadata

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["transport_contract_pass"] is False
    assert receipt["ready_set_pass"] is False
    assert receipt["datasets"][0]["reason_codes"] == ["source_proof_incomplete"]


@pytest.mark.parametrize(
    ("lineage_key", "malformed_value"),
    [("provider", " "), ("transport_service", "\t")],
)
def test_ready_source_proof_rejects_whitespace_lineage_identity(
    tmp_path: Path,
    lineage_key: str,
    malformed_value: str,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    for call_number in (1, 2, 3, 4):
        metadata = _metadata(health="ready")
        metadata["lineage"][lineage_key] = malformed_value
        transport.metadata_overrides[(READY_ID, call_number)] = metadata

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["ready_set_pass"] is False
    assert receipt["datasets"][0]["source_proof_complete"] is False
    assert receipt["datasets"][0]["reason_codes"] == ["source_proof_incomplete"]


def test_cli_preserves_manifest_path_for_loader_symlink_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_manifest()), encoding="utf-8")
    alias = tmp_path / "manifest-link.json"
    alias.symlink_to(target)
    seen: list[Path] = []

    def rejecting_loader(path: Path):
        seen.append(path)
        raise api.CatalogParityConfigurationError("symlink rejected")

    monkeypatch.setattr(api, "load_catalog_parity_manifest", rejecting_loader)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")

    rc = api.main(
        [
            "--manifest",
            str(alias),
            "--token-file",
            "/run/secrets/tradingagent/tradingdatas-read.token",
            "--output",
            str((tmp_path / "receipt.json").resolve()),
        ]
    )

    assert rc == 64
    assert seen == [alias]


def test_cli_preserves_relative_output_for_writer_absolute_path_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    config = _load_config(tmp_path)
    relative_output = Path("relative-receipt.json")
    seen: list[Path] = []

    monkeypatch.setattr(api, "load_catalog_parity_manifest", lambda path: config)
    monkeypatch.setattr(
        api, "build_runtime_transport", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        api,
        "run_tradingdatas_catalog_parity",
        lambda *args, **kwargs: {"status": "pass", "blocking": False},
    )

    def rejecting_writer(path: Path, receipt: dict[str, Any]) -> None:
        seen.append(path)
        raise api.CatalogParityConfigurationError("relative output rejected")

    monkeypatch.setattr(api, "write_catalog_parity_receipt", rejecting_writer)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")

    rc = api.main(
        [
            "--manifest",
            str((tmp_path / "catalog-parity.json").resolve()),
            "--token-file",
            "/run/secrets/tradingagent/tradingdatas-read.token",
            "--output",
            str(relative_output),
        ]
    )

    assert rc == 64
    assert seen == [relative_output]


def test_same_observation_metadata_drift_blocks(tmp_path: Path) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    drifted = _metadata(health="impaired")
    drifted["observed_at"] = "2026-07-22T15:02:00+08:00"
    transport.metadata_overrides[(IMPAIRED_ID, 2)] = drifted

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["transport_contract_pass"] is False
    assert receipt["impaired_set_accounted"] is False
    assert receipt["datasets"][1]["reason_codes"] == [
        "same_observation_semantic_mismatch"
    ]


def test_cursor_cycle_blocks_without_leaking_cursor(tmp_path: Path) -> None:
    api = _api()
    config = _load_config(tmp_path)
    transport = FixtureTransport()
    transport.cursor_cycle = True

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["transport_contract_pass"] is False
    assert "pagination_cursor_cycle" in receipt["reason_codes"]
    assert "opaque-cursor" not in json.dumps(receipt, sort_keys=True)


def test_manifest_counts_are_explicit_and_drive_dataset_cardinality(
    tmp_path: Path,
) -> None:
    api = _api()
    payload = _manifest()
    payload["expected_counts"]["total"] = 4
    payload["expected_counts"]["active"] = 3

    with pytest.raises(api.CatalogParityConfigurationError, match="active count"):
        _load_config(tmp_path, payload)


def test_identityless_impaired_empty_page_accepts_zero_frozen_row_cap(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(
        tmp_path,
        _identityless_impaired_manifest(max_rows=0),
    )
    transport = FixtureTransport()

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "pass"
    assert receipt["transport_contract_pass"] is True
    assert receipt["impaired_set_accounted"] is True
    impaired = receipt["datasets"][1]
    assert impaired["row_count"] == 0
    assert impaired["max_rows"] == 0
    assert impaired["identity_authority_available"] is False
    assert impaired["identity_sha256"] is None
    assert impaired["evidence_action"] == "reject"
    assert impaired["evidence_eligible"] is False
    assert impaired["effective_weight"] == 0.0
    assert impaired["parity_data_accepted"] is False
    assert impaired["research_snapshot_eligible"] is False
    impaired_queries = [
        call["json_body"]
        for call in transport.calls
        if call["method"] == "POST" and call["json_body"]["dataset_id"] == IMPAIRED_ID
    ]
    assert len(impaired_queries) == 2
    assert all(query["limit"] == 500 for query in impaired_queries)
    assert all("as_of" not in query for query in impaired_queries)
    assert all("order" not in query for query in impaired_queries)


def test_identityless_duplicate_provider_rows_make_no_identity_claim(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(
        tmp_path,
        _identityless_impaired_manifest(max_rows=2),
    )
    transport = FixtureTransport()
    provider_row = {"sector_code": "provider-native", "close": 1}
    for call_number in (1, 2):
        transport.query_rows_overrides[(IMPAIRED_ID, call_number)] = [
            provider_row,
            provider_row,
        ]

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "pass"
    impaired = receipt["datasets"][1]
    assert impaired["row_count"] == 2
    assert impaired["same_observation_match"] is True
    assert impaired["identity_authority_available"] is False
    assert impaired["identity_sha256"] is None
    assert impaired["evidence_action"] == "reject"
    assert impaired["evidence_eligible"] is False
    assert impaired["effective_weight"] == 0.0
    assert impaired["parity_data_accepted"] is False
    assert impaired["research_snapshot_eligible"] is False
    assert impaired["accounting_reason_codes"] == ["identity_authority_unavailable"]
    assert len(impaired["run_semantic_sha256s"]) == 2
    assert len(impaired["run_semantic_trace_sha256s"]) == 2
    assert len(set(impaired["run_semantic_sha256s"])) == 1
    assert len(set(impaired["run_semantic_trace_sha256s"])) == 1


@pytest.mark.parametrize(
    ("invalid_profile", "error_pattern"),
    [
        ("ready_identityless", "impaired"),
        ("identityless_multi_page", "max_pages"),
        ("identityless_positive_minimum", "minimum_row_count"),
        ("keyed_zero_row_cap", "max_rows"),
    ],
)
def test_identityless_manifest_mode_is_narrow_and_keyed_profiles_stay_strict(
    tmp_path: Path,
    invalid_profile: str,
    error_pattern: str,
) -> None:
    api = _api()
    payload = _manifest()
    if invalid_profile == "ready_identityless":
        dataset = payload["datasets"][0]
        dataset["identity_fields"] = []
        dataset["minimum_row_count"] = 0
        dataset["max_pages"] = 1
        dataset["max_rows"] = 1
    elif invalid_profile == "identityless_multi_page":
        dataset = payload["datasets"][1]
        dataset["identity_fields"] = []
        dataset["max_pages"] = 2
    elif invalid_profile == "identityless_positive_minimum":
        dataset = payload["datasets"][1]
        dataset["identity_fields"] = []
        dataset["max_pages"] = 1
        dataset["minimum_row_count"] = 1
    else:
        payload["datasets"][0]["max_rows"] = 0

    with pytest.raises(api.CatalogParityConfigurationError, match=error_pattern):
        _load_config(tmp_path, payload)


def test_keyed_manifest_allows_query_limit_above_frozen_row_cap(
    tmp_path: Path,
) -> None:
    payload = _manifest()
    ready = payload["datasets"][0]
    ready["query_limit"] = 500
    ready["max_rows"] = 1

    config = _load_config(tmp_path, payload)

    assert config.datasets[0].identity_fields == ("symbol", "trade_date")
    assert config.datasets[0].query_limit == 500
    assert config.datasets[0].max_rows == 1


def test_identityless_nonterminal_cursor_fails_closed_and_is_redacted(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(
        tmp_path,
        _identityless_impaired_manifest(max_rows=0),
    )
    transport = FixtureTransport()
    transport.next_cursor_overrides[(IMPAIRED_ID, 1)] = "identityless-opaque-cursor"

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["transport_contract_pass"] is False
    assert "pagination_page_budget_exceeded" in receipt["reason_codes"]
    assert "identityless-opaque-cursor" not in json.dumps(receipt, sort_keys=True)


def test_identityless_row_budget_is_checked_after_the_complete_response(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(
        tmp_path,
        _identityless_impaired_manifest(max_rows=0),
    )
    transport = FixtureTransport()
    transport.query_rows_overrides[(IMPAIRED_ID, 1)] = [{"unexpected": "row"}]

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["transport_contract_pass"] is False
    assert receipt["reason_codes"] == ["pagination_row_budget_exceeded"]


def test_identityless_ordered_provider_row_drift_fails_same_observation(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(
        tmp_path,
        _identityless_impaired_manifest(max_rows=1),
    )
    transport = FixtureTransport()
    transport.query_rows_overrides[(IMPAIRED_ID, 1)] = [{"provider_value": 1}]
    transport.query_rows_overrides[(IMPAIRED_ID, 2)] = [{"provider_value": 2}]

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["transport_contract_pass"] is False
    impaired = receipt["datasets"][1]
    assert impaired["same_observation_match"] is False
    assert impaired["identity_authority_available"] is False
    assert impaired["identity_sha256"] is None
    assert impaired["evidence_action"] == "reject"
    assert impaired["evidence_eligible"] is False
    assert impaired["effective_weight"] == 0.0
    assert impaired["reason_codes"] == ["same_observation_semantic_mismatch"]


def test_identityless_metadata_becoming_ready_still_has_no_acceptance_or_weight(
    tmp_path: Path,
) -> None:
    api = _api()
    config = _load_config(
        tmp_path,
        _identityless_impaired_manifest(max_rows=0),
    )
    transport = FixtureTransport()
    for call_number in (1, 2):
        transport.metadata_overrides[(IMPAIRED_ID, call_number)] = _metadata(
            health="ready"
        )

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["impaired_set_accounted"] is False
    impaired = receipt["datasets"][1]
    assert impaired["effective_state"] == "ready"
    assert impaired["evidence_action"] == "reject"
    assert impaired["evidence_eligible"] is False
    assert impaired["effective_weight"] == 0.0
    assert impaired["parity_data_accepted"] is False
    assert impaired["research_snapshot_eligible"] is False
    assert impaired["reason_codes"] == ["impaired_dataset_unexpectedly_accepted"]


def test_identityless_metadata_drift_fails_same_observation(tmp_path: Path) -> None:
    api = _api()
    config = _load_config(
        tmp_path,
        _identityless_impaired_manifest(max_rows=0),
    )
    transport = FixtureTransport()
    drifted = _metadata(health="impaired")
    drifted["observed_at"] = "2026-07-22T15:02:00+08:00"
    transport.metadata_overrides[(IMPAIRED_ID, 2)] = drifted

    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "fail"
    impaired = receipt["datasets"][1]
    assert impaired["same_observation_match"] is False
    assert impaired["reason_codes"] == ["same_observation_semantic_mismatch"]


def test_null_as_of_is_omitted_from_wire_when_every_dataset_omits_decision_as_of(
    tmp_path: Path,
) -> None:
    api = _api()
    payload = _identityless_impaired_manifest(max_rows=0)
    payload["as_of"] = None
    for dataset in payload["datasets"]:
        dataset["observation"]["query_as_of_mode"] = "omit"
    config = _load_config(tmp_path, payload)
    transport = FixtureTransport()

    assert config.as_of is None
    assert config.to_payload()["as_of"] is None
    receipt = api.run_tradingdatas_catalog_parity(config, transport=transport)

    assert receipt["status"] == "pass"
    assert receipt["as_of"] is None
    assert all(item["query_as_of"] is None for item in receipt["datasets"])
    query_payloads = [
        call["json_body"] for call in transport.calls if call["method"] == "POST"
    ]
    assert query_payloads
    assert all("as_of" not in query for query in query_payloads)
    assert all("order" not in query for query in query_payloads)
    assert receipt["receipt_sha256"] == api.receipt_sha256(receipt)


def test_null_as_of_rejects_any_decision_as_of_dataset(tmp_path: Path) -> None:
    api = _api()
    payload = _manifest()
    payload["as_of"] = None

    with pytest.raises(
        api.CatalogParityConfigurationError,
        match="decision_as_of",
    ):
        _load_config(tmp_path, payload)


def test_nonnull_as_of_rejects_profile_where_every_dataset_omits_it(
    tmp_path: Path,
) -> None:
    api = _api()
    payload = _manifest()
    for dataset in payload["datasets"]:
        dataset["observation"]["query_as_of_mode"] = "omit"

    with pytest.raises(
        api.CatalogParityConfigurationError,
        match="as_of must be null",
    ):
        _load_config(tmp_path, payload)
