from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from shared.data.sharedsignals_v1 import HTTPResponse
from shared.runtime.ashare_observation_manifest import (
    AshareObservationManifestBlocked,
    AshareObservationManifestBuildConfig,
    build_ashare_observation_manifest,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import load_probe_manifest


CATALOG_VERSION = "v1-dynamic-fixture"
DECISION_AS_OF = datetime.fromisoformat("2026-07-26T18:00:00+08:00")


def _field(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "selectable": True,
        "filterable": True,
        "sortable": True,
        "operators": ["eq", "in", "gte", "lte", "between"],
    }


def _catalog_row(
    dataset_id: str,
    *,
    fields: list[str],
    schema_major: int = 2,
    activation_state: str = "active",
    runtime_state: str = "success",
    degraded: bool = False,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "schema_major": schema_major,
        "default_fields": list(fields),
        "default_order": [f"{fields[0]}:asc"],
        "fields": [_field(name) for name in fields],
        "filter_operators": {
            name: ["eq", "in", "gte", "lte", "between"] for name in fields
        },
        "limits": {"max_page_size": 500, "max_lookback_days": 36500},
        "availability": {"activation_states": [activation_state]},
        "runtime": {
            "state": runtime_state,
            "degraded": degraded,
            "receipt_id": "catalog-runtime-receipt",
            "data_through": "20260724",
            "observed_at": "2026-07-27T09:00:00Z",
            "reasons": [],
        },
    }


def _catalog_rows() -> list[dict[str, Any]]:
    return [
        _catalog_row(
            "cn.market.trade_calendar",
            fields=["exchange", "cal_date", "is_open", "pretrade_date"],
        ),
        _catalog_row(
            "cn.equity.security_master",
            fields=[
                "ts_code",
                "symbol",
                "name",
                "market",
                "list_status",
                "list_date",
            ],
        ),
        _catalog_row(
            "cn.equity.daily",
            fields=[
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "vol",
                "amount",
            ],
        ),
        _catalog_row(
            "cn.dataset.partial_context",
            fields=["ts_code", "trade_date"],
            schema_major=1,
            runtime_state="partial",
            degraded=True,
        ),
        _catalog_row(
            "cn.dataset.paused",
            fields=["ts_code"],
            schema_major=1,
            activation_state="paused",
            runtime_state="paused",
            degraded=True,
        ),
    ]


def _metadata(dataset_id: str, *, degraded: bool = False) -> dict[str, Any]:
    state = "partial" if degraded else "ready"
    return {
        "state": state,
        "degraded": degraded,
        "freshness": {"state": "unknown" if degraded else "fresh"},
        "quality": {"state": "degraded" if degraded else "valid"},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "provider": "fixture-provider",
            "transport_service": "fixture-transport",
        },
        "receipt_id": f"receipt:{dataset_id}",
        "data_through": (
            "2026-07-24T00:00:00+08:00"
            if dataset_id == "cn.equity.daily"
            else "2026-07-26T09:00:00+08:00"
        ),
        "observed_at": "2026-07-26T10:00:00+08:00",
        "reasons": ["fixture_partial"] if degraded else [],
    }


class FixtureTransport:
    def __init__(
        self,
        *,
        catalog_rows: list[dict[str, Any]] | None = None,
        daily_degraded: bool = False,
    ) -> None:
        self.catalog_rows = copy.deepcopy(catalog_rows or _catalog_rows())
        self.daily_degraded = daily_degraded
        self.calls: list[dict[str, Any]] = []

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
        if method == "GET":
            return HTTPResponse(
                status_code=200,
                json_body={
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "catalog-request",
                    "data": copy.deepcopy(self.catalog_rows),
                },
            )
        assert json_body is not None
        dataset_id = json_body["dataset_id"]
        if dataset_id == "cn.market.trade_calendar":
            rows = [
                {
                    "exchange": "SSE",
                    "cal_date": "20260723",
                    "is_open": 1,
                    "pretrade_date": "20260722",
                },
                {
                    "exchange": "SSE",
                    "cal_date": "20260724",
                    "is_open": 1,
                    "pretrade_date": "20260723",
                },
                {
                    "exchange": "SSE",
                    "cal_date": "20260725",
                    "is_open": 0,
                    "pretrade_date": "20260724",
                },
                {
                    "exchange": "SSE",
                    "cal_date": "20260726",
                    "is_open": 0,
                    "pretrade_date": "20260724",
                },
                {
                    "exchange": "SSE",
                    "cal_date": "20260727",
                    "is_open": 1,
                    "pretrade_date": "20260724",
                },
            ]
        elif dataset_id == "cn.equity.security_master":
            rows = [
                {
                    "ts_code": "600000.SH",
                    "name": "浦发银行",
                    "list_status": "L",
                    "list_date": "19991110",
                }
            ]
        elif dataset_id == "cn.equity.daily":
            rows = [
                {
                    "ts_code": "600000.SH",
                    "trade_date": "20260724",
                    "close": 10.0,
                    "vol": 100.0,
                    "amount": 1000.0,
                }
            ]
        else:  # pragma: no cover - the builder must never query non-core datasets
            raise AssertionError(f"unexpected dataset query: {dataset_id}")
        return HTTPResponse(
            status_code=200,
            json_body={
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"query-{dataset_id}",
                "dataset_id": dataset_id,
                "data": rows,
                "next_cursor": None,
                "metadata": _metadata(
                    dataset_id,
                    degraded=(
                        self.daily_degraded
                        and dataset_id == "cn.equity.daily"
                    ),
                ),
            },
        )


def _config(tmp_path: Path) -> AshareObservationManifestBuildConfig:
    return AshareObservationManifestBuildConfig(
        base_url="https://tradingdatas.fixture.invalid",
        access_policy_id="ta-ashare-observation-read-v1",
        transport_id="http-json-v1",
        timeout_seconds=3,
        manifest_root=(tmp_path / "manifests").resolve(),
        decision_as_of=DECISION_AS_OF,
        real_trading_enabled=False,
    )


def test_dynamic_catalog_builds_core_manifest_and_accounts_for_all_active_rows(
    tmp_path: Path,
) -> None:
    transport = FixtureTransport()

    result = build_ashare_observation_manifest(
        _config(tmp_path),
        transport=transport,
    )

    assert result.observation_session == "20260724"
    assert result.catalog_version == CATALOG_VERSION
    assert result.catalog_counts == {"total": 5, "active": 4, "paused": 1}
    assert result.reused is False
    assert result.current_manifest_path.is_file()
    assert result.archive_manifest_path.is_file()
    assert result.catalog_snapshot_path.is_file()
    assert result.current_manifest_path.read_bytes() == (
        result.archive_manifest_path.read_bytes()
    )

    manifest = load_probe_manifest(result.current_manifest_path)
    assert manifest.catalog_version == CATALOG_VERSION
    assert manifest.as_of == DECISION_AS_OF.isoformat()
    assert manifest.expected_probe_roles == (
        "trade_calendar",
        "security_master",
        "daily_bars",
    )
    assert {item.dataset_id for item in manifest.datasets} == {
        "cn.market.trade_calendar",
        "cn.equity.security_master",
        "cn.equity.daily",
    }
    daily = next(item for item in manifest.datasets if item.probe_role == "daily_bars")
    assert daily.filters == {"trade_date": {"eq": "20260724"}}
    master = next(
        item for item in manifest.datasets if item.probe_role == "security_master"
    )
    assert master.filters == {"list_status": {"eq": "L"}}

    catalog_snapshot = json.loads(
        result.catalog_snapshot_path.read_text(encoding="utf-8")
    )
    assert catalog_snapshot["counts"] == {"total": 5, "active": 4, "paused": 1}
    assert len(catalog_snapshot["active_catalog_rows"]) == 4
    assert "cn.dataset.partial_context" in {
        row["dataset_id"] for row in catalog_snapshot["active_catalog_rows"]
    }
    queried = [
        call["json_body"]["dataset_id"]
        for call in transport.calls
        if call["method"] == "POST"
    ]
    assert queried == [
        "cn.market.trade_calendar",
        "cn.equity.security_master",
        "cn.equity.daily",
    ]
    assert all(
        forbidden not in call["url"]
        for call in transport.calls
        for forbidden in ("/tushare", "/source_status", ":8082")
    )


def test_degraded_core_dataset_blocks_without_publishing_manifest(
    tmp_path: Path,
) -> None:
    transport = FixtureTransport(daily_degraded=True)

    with pytest.raises(
        AshareObservationManifestBlocked,
        match="core_dataset_evidence_rejected:cn.equity.daily",
    ):
        build_ashare_observation_manifest(
            _config(tmp_path),
            transport=transport,
        )

    assert not (_config(tmp_path).manifest_root / "current.json").exists()


def test_paused_core_dataset_blocks_before_any_query(tmp_path: Path) -> None:
    rows = _catalog_rows()
    rows[2]["availability"] = {"activation_states": ["paused"]}
    transport = FixtureTransport(catalog_rows=rows)

    with pytest.raises(
        AshareObservationManifestBlocked,
        match="core_dataset_not_active:cn.equity.daily",
    ):
        build_ashare_observation_manifest(
            _config(tmp_path),
            transport=transport,
        )

    assert [call["method"] for call in transport.calls] == ["GET"]


def test_same_session_is_idempotent_but_catalog_drift_fails_closed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first_transport = FixtureTransport()
    first = build_ashare_observation_manifest(config, transport=first_transport)
    second = build_ashare_observation_manifest(
        config,
        transport=FixtureTransport(),
    )

    assert second.reused is True
    assert second.manifest_sha256 == first.manifest_sha256
    assert second.current_manifest_path.read_bytes() == (
        first.current_manifest_path.read_bytes()
    )

    drifted_rows = _catalog_rows()
    drifted_rows[3]["default_fields"].append("new_field")
    drifted_rows[3]["fields"].append(_field("new_field"))
    drifted_rows[3]["filter_operators"]["new_field"] = ["eq"]
    current_before = first.current_manifest_path.read_bytes()
    with pytest.raises(
        AshareObservationManifestBlocked,
        match="same_session_catalog_contract_changed",
    ):
        build_ashare_observation_manifest(
            config,
            transport=FixtureTransport(catalog_rows=drifted_rows),
        )
    assert first.current_manifest_path.read_bytes() == current_before


def test_pre_close_decision_time_is_rejected(tmp_path: Path) -> None:
    config = AshareObservationManifestBuildConfig(
        **{
            **_config(tmp_path).__dict__,
            "decision_as_of": datetime.fromisoformat(
                "2026-07-26T14:59:59+08:00"
            ),
        }
    )

    with pytest.raises(
        AshareObservationManifestBlocked,
        match="post_close_manifest_required",
    ):
        build_ashare_observation_manifest(
            config,
            transport=FixtureTransport(),
        )


def test_existing_current_with_broad_mode_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = build_ashare_observation_manifest(
        config,
        transport=FixtureTransport(),
    )
    os.chmod(first.current_manifest_path, 0o644)

    with pytest.raises(
        AshareObservationManifestBlocked,
        match="current_manifest_invalid",
    ):
        build_ashare_observation_manifest(
            config,
            transport=FixtureTransport(),
        )


def test_existing_immutable_artifact_symlink_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = build_ashare_observation_manifest(
        config,
        transport=FixtureTransport(),
    )
    first.catalog_snapshot_path.unlink()
    first.catalog_snapshot_path.symlink_to(first.archive_manifest_path)

    with pytest.raises(
        AshareObservationManifestBlocked,
        match="immutable_artifact_conflict",
    ):
        build_ashare_observation_manifest(
            config,
            transport=FixtureTransport(),
        )
