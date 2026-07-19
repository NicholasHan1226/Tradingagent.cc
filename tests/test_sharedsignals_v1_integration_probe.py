from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from shared.data.sharedsignals_v1 import HTTPResponse
from shared.runtime_test.sharedsignals_v1_integration_probe import (
    IntegrationProbeConfigurationError,
    load_probe_manifest,
    main,
    run_sharedsignals_integration_probe,
    write_probe_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG_VERSION = "catalog-integration-fixture-v1"
ACCESS_POLICY_ID = "ta-integration-read-v1"
AS_OF = "2026-07-17T09:25:00+08:00"
DATASETS = {
    "trade_calendar": "cn.market.trade-calendar.v1",
    "daily_bars": "cn.equity.daily.mainboard.v1",
}


def _manifest() -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "profile_id": "ashare-mainboard-integration-v1",
        "base_url": "https://sharedsignals.fixture.invalid",
        "catalog_version": CATALOG_VERSION,
        "access_policy_id": ACCESS_POLICY_ID,
        "transport_id": "fixture-v1",
        "timeout_seconds": 3,
        "as_of": AS_OF,
        "expected_probe_roles": ["trade_calendar", "daily_bars"],
        "datasets": [
            {
                "probe_role": "trade_calendar",
                "dataset_id": DATASETS["trade_calendar"],
                "schema_major": 1,
                "requirement_role": "required_execution",
                "fields": [
                    "market",
                    "trade_date",
                    "is_open",
                    "event_time",
                    "available_time",
                    "revision_id",
                    "receipt_id",
                ],
                "filters": {"market": "CN"},
                "limit": 100,
                "minimum_row_count": 1,
                "row_event_time_field": "event_time",
                "row_available_time_field": "available_time",
                "row_revision_id_field": "revision_id",
                "row_receipt_id_field": "receipt_id",
            },
            {
                "probe_role": "daily_bars",
                "dataset_id": DATASETS["daily_bars"],
                "schema_major": 2,
                "requirement_role": "required_execution",
                "fields": [
                    "symbol",
                    "close",
                    "event_time",
                    "available_time",
                    "revision_id",
                    "receipt_id",
                ],
                "filters": {"board": "mainboard"},
                "limit": 500,
                "minimum_row_count": 1,
                "row_event_time_field": "event_time",
                "row_available_time_field": "available_time",
                "row_revision_id_field": "revision_id",
                "row_receipt_id_field": "receipt_id",
            },
        ],
    }


def _row(dataset_id: str) -> dict[str, Any]:
    common = {
        "event_time": "2026-07-16T15:00:00+08:00",
        "available_time": "2026-07-16T15:01:00+08:00",
        "revision_id": "revision-1",
        "receipt_id": f"row-{dataset_id}",
    }
    if dataset_id == DATASETS["trade_calendar"]:
        return {
            **common,
            "market": "CN",
            "trade_date": "2026-07-16",
            "is_open": True,
        }
    return {**common, "symbol": "600000.SH", "close": 10.5}


def _ready_query_payload(
    dataset_id: str,
    *,
    request_id: str,
    row: dict[str, Any] | None = None,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": request_id,
        "dataset_id": dataset_id,
        "data": [copy.deepcopy(row or _row(dataset_id))],
        "next_cursor": next_cursor,
        "metadata": {
            "state": "ready",
            "degraded": False,
            "freshness": {"state": "fresh", "fresh": True},
            "quality": {"state": "valid", "valid": True},
            "lineage": {
                "state": "complete",
                "complete": True,
                "provider_neutral": True,
            },
            "receipt_id": f"receipt-{dataset_id}",
            "data_through": "2026-07-16T15:01:00+08:00",
            "observed_at": "2026-07-16T15:01:01+08:00",
            "reasons": [],
        },
    }


class DoubleRunTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.query_counts: dict[str, int] = {}
        self.overrides: dict[tuple[str, int], dict[str, Any]] = {}
        self.error: Exception | None = None

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
                "headers": dict(headers),
                "json_body": copy.deepcopy(json_body),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        if method == "GET" and url.endswith("/v1/catalog"):
            return HTTPResponse(
                status_code=200,
                json_body={
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "catalog-request-1",
                    "data": [
                        {"dataset_id": dataset_id} for dataset_id in DATASETS.values()
                    ],
                },
            )
        if method == "POST" and url.endswith("/v1/query"):
            assert json_body is not None
            dataset_id = str(json_body["dataset_id"])
            run_number = self.query_counts.get(dataset_id, 0) + 1
            self.query_counts[dataset_id] = run_number
            payload = self.overrides.get(
                (dataset_id, run_number),
                _ready_query_payload(
                    dataset_id,
                    request_id=f"query-{dataset_id}-{run_number}",
                ),
            )
            return HTTPResponse(status_code=200, json_body=payload)
        raise AssertionError(f"unexpected request: {method} {url}")


def _load_config(tmp_path: Path, payload: dict[str, Any] | None = None):
    manifest_path = tmp_path / "probe-manifest.json"
    manifest_path.write_text(
        json.dumps(payload or _manifest(), ensure_ascii=False),
        encoding="utf-8",
    )
    return load_probe_manifest(manifest_path.resolve())


def _receipt_sha256(receipt: dict[str, Any]) -> str:
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_healthy_multidataset_double_run_emits_content_addressed_receipt(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    assert receipt["status"] == "pass"
    assert receipt["blocking"] is False
    assert receipt["authority"] == "non_authority"
    assert receipt["production_verified"] is False
    assert receipt["real_trading_enabled"] is False
    assert receipt["same_as_of_match"] is True
    assert receipt["receipt_sha256"] == _receipt_sha256(receipt)
    assert [dataset["probe_role"] for dataset in receipt["datasets"]] == [
        "trade_calendar",
        "daily_bars",
    ]
    assert all(dataset["same_as_of_match"] for dataset in receipt["datasets"])
    assert all(dataset["pagination_complete"] for dataset in receipt["datasets"])
    assert [call["url"].rsplit("/", 2)[-2:] for call in transport.calls] == [
        ["v1", "catalog"],
        ["v1", "query"],
        ["v1", "query"],
        ["v1", "query"],
        ["v1", "query"],
    ]
    for call in transport.calls[1:]:
        payload = call["json_body"]
        assert payload is not None
        assert payload["as_of"] == AS_OF
        assert payload["cursor"] is None
        assert "order" not in payload

    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "sharedsignals.fixture.invalid" not in serialized
    assert ACCESS_POLICY_ID not in serialized


def test_non_null_cursor_blocks_without_following_unfrozen_pagination(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()
    for run_number in (1, 2):
        transport.overrides[(DATASETS["daily_bars"], run_number)] = (
            _ready_query_payload(
                DATASETS["daily_bars"],
                request_id=f"paged-{run_number}",
                next_cursor="opaque-secret-cursor",
            )
        )

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    assert receipt["status"] == "fail"
    assert receipt["blocking"] is True
    assert "pagination_contract_unfrozen" in receipt["reason_codes"]
    assert transport.query_counts[DATASETS["daily_bars"]] == 2
    assert "opaque-secret-cursor" not in json.dumps(receipt, sort_keys=True)


@pytest.mark.parametrize(
    ("state", "degraded"),
    [
        ("degraded", True),
        ("stale", False),
        ("unobserved", False),
        ("paused", False),
        ("failed", False),
        ("empty", False),
    ],
)
def test_impaired_dataset_with_null_proof_fails_closed_without_forgery(
    tmp_path: Path,
    state: str,
    degraded: bool,
) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()
    for run_number in (1, 2):
        payload = _ready_query_payload(
            DATASETS["daily_bars"],
            request_id=f"impaired-{run_number}",
        )
        payload["data"] = []
        payload["metadata"].update(
            {
                "state": state,
                "degraded": degraded,
                "lineage": None,
                "receipt_id": None,
                "data_through": None,
                "observed_at": None,
                "reasons": [f"fixture_{state}"],
            }
        )
        transport.overrides[(DATASETS["daily_bars"], run_number)] = payload

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    dataset = next(
        item for item in receipt["datasets"] if item["probe_role"] == "daily_bars"
    )
    assert receipt["blocking"] is True
    assert dataset["receipt_id"] is None
    assert dataset["data_through"] is None
    assert dataset["observed_at"] is None
    assert dataset["evidence_action"] == "reject"
    assert dataset["source_proof_complete"] is False


def test_same_as_of_semantic_drift_blocks_even_when_transport_succeeds(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()
    changed = _row(DATASETS["daily_bars"])
    changed["close"] = 11.0
    transport.overrides[(DATASETS["daily_bars"], 2)] = _ready_query_payload(
        DATASETS["daily_bars"],
        request_id="query-changed",
        row=changed,
    )

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    dataset = next(
        item for item in receipt["datasets"] if item["probe_role"] == "daily_bars"
    )
    assert receipt["blocking"] is True
    assert receipt["same_as_of_match"] is False
    assert dataset["same_as_of_match"] is False
    assert "same_as_of_semantic_mismatch" in dataset["reason_codes"]


def test_missing_requested_field_and_future_pit_data_fail_closed(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()
    bad_row = _row(DATASETS["daily_bars"])
    bad_row.pop("close")
    bad_row["available_time"] = "2026-07-17T09:26:00+08:00"
    for run_number in (1, 2):
        transport.overrides[(DATASETS["daily_bars"], run_number)] = (
            _ready_query_payload(
                DATASETS["daily_bars"],
                request_id=f"bad-row-{run_number}",
                row=bad_row,
            )
        )

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    assert receipt["blocking"] is True
    assert "requested_field_missing" in receipt["reason_codes"]
    assert "research_snapshot_contract_failure" in receipt["reason_codes"]


def test_undeclared_response_field_blocks_without_emitting_untrusted_name(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()
    untrusted_field = "symbol-secret-sk-0123456789abcdef0123456789abcdef"
    row = _row(DATASETS["daily_bars"])
    row[untrusted_field] = "must-not-enter-receipt"
    for run_number in (1, 2):
        transport.overrides[(DATASETS["daily_bars"], run_number)] = (
            _ready_query_payload(
                DATASETS["daily_bars"],
                request_id=f"extra-field-{run_number}",
                row=row,
            )
        )

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    dataset = next(
        item for item in receipt["datasets"] if item["probe_role"] == "daily_bars"
    )
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert receipt["blocking"] is True
    assert "undeclared_field_present" in receipt["reason_codes"]
    assert dataset["unexpected_field_count"] == 1
    assert len(dataset["unexpected_fields_sha256"]) == 64
    assert untrusted_field not in serialized
    assert "must-not-enter-receipt" not in serialized


def test_transport_exception_is_redacted_and_never_falls_back(tmp_path: Path) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()
    transport.error = RuntimeError(
        "https://user:sk-secret@sharedsignals.invalid/private?token=sk-secret"
    )

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert receipt["blocking"] is True
    assert receipt["reason_codes"] == ["catalog_contract_or_transport_failure"]
    assert receipt["error_type"] == "RuntimeError"
    assert "sk-secret" not in serialized
    assert "sharedsignals.invalid" not in serialized
    assert len(transport.calls) == 1


def test_provider_reason_text_is_hashed_not_emitted(tmp_path: Path) -> None:
    config = _load_config(tmp_path)
    transport = DoubleRunTransport()
    sentinel = "provider-debug-secret-sk-0123456789abcdef0123456789abcdef"
    for run_number in (1, 2):
        payload = _ready_query_payload(
            DATASETS["daily_bars"],
            request_id=f"reason-{run_number}",
        )
        payload["metadata"]["reasons"] = ["dataset_failed", sentinel]
        transport.overrides[(DATASETS["daily_bars"], run_number)] = payload

    receipt = run_sharedsignals_integration_probe(config, transport=transport)

    dataset = next(
        item for item in receipt["datasets"] if item["probe_role"] == "daily_bars"
    )
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert receipt["blocking"] is False
    assert dataset["reason_codes"] == []
    assert len(dataset["evidence_reasons_sha256"]) == 64
    assert sentinel not in serialized


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("as_of"),
        lambda payload: payload.update({"expected_probe_roles": ["daily_bars"]}),
        lambda payload: payload.update({"api_key": "sk-must-not-be-in-manifest"}),
        lambda payload: payload.update(
            {"base_url": "https://user:password@sharedsignals.invalid"}
        ),
        lambda payload: payload["datasets"][0].update({"limit": 10_001}),
        lambda payload: payload["datasets"][0]["fields"].remove("event_time"),
        lambda payload: payload["datasets"][0]["filters"].update(
            {"opaque_value": "sk-0123456789abcdef0123456789abcdef"}
        ),
    ],
)
def test_manifest_is_explicit_complete_and_secret_free(
    tmp_path: Path,
    mutation,
) -> None:
    payload = _manifest()
    mutation(payload)
    manifest_path = tmp_path / "invalid-manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IntegrationProbeConfigurationError):
        load_probe_manifest(manifest_path.resolve())


def test_receipt_write_is_atomic_private_and_reproducible(tmp_path: Path) -> None:
    config = _load_config(tmp_path)
    receipt = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )
    output_path = (tmp_path / "receipts" / "integration.json").resolve()

    write_probe_receipt(output_path, receipt)
    first = output_path.read_bytes()
    write_probe_receipt(output_path, receipt)

    assert output_path.read_bytes() == first
    assert output_path.stat().st_mode & 0o077 == 0
    assert json.loads(first)["receipt_sha256"] == receipt["receipt_sha256"]


def test_identical_probe_traces_have_the_same_receipt_hash(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)

    first = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )
    second = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )

    assert first == second
    assert first["receipt_sha256"] == second["receipt_sha256"]


def test_new_request_ids_change_exact_receipt_but_not_semantic_snapshot(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    baseline = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )
    retried_transport = DoubleRunTransport()
    for dataset_id in DATASETS.values():
        for run_number in (1, 2):
            retried_transport.overrides[(dataset_id, run_number)] = (
                _ready_query_payload(
                    dataset_id,
                    request_id=f"retry-{dataset_id}-{run_number}",
                )
            )

    retried = run_sharedsignals_integration_probe(
        config,
        transport=retried_transport,
    )

    assert retried["blocking"] is False
    assert retried["semantic_snapshot_sha256"] == baseline["semantic_snapshot_sha256"]
    assert retried["receipt_sha256"] != baseline["receipt_sha256"]


def test_cli_uses_explicit_manifest_and_writes_machine_receipt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    payload = _manifest()
    payload["transport_id"] = "http-json-v1"
    manifest_path = tmp_path / "cli-manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    output_path = (tmp_path / "cli-receipt.json").resolve()
    transport = DoubleRunTransport()
    monkeypatch.setattr(
        "shared.runtime_test.sharedsignals_v1_integration_probe.build_runtime_transport",
        lambda _transport_id: transport,
    )

    exit_code = main(
        [
            "--manifest",
            str(manifest_path.resolve()),
            "--output",
            str(output_path),
            "--json",
        ]
    )

    assert exit_code == 0
    stdout_receipt = json.loads(capsys.readouterr().out)
    file_receipt = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout_receipt == file_receipt
    assert file_receipt["blocking"] is False


def test_checked_in_example_manifest_is_parseable_and_non_live() -> None:
    config = load_probe_manifest(
        (
            ROOT
            / "docs"
            / "examples"
            / "sharedsignals_v1_integration_probe.example.json"
        ).resolve()
    )

    assert config.base_url.endswith(".invalid")
    assert config.expected_probe_roles == (
        "trade_calendar",
        "equity_master",
        "daily_bars",
        "industry_context",
    )
    assert config.datasets[-1].requirement_role == "optional_context"
