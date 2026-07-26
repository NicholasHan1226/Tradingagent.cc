from __future__ import annotations

import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import shared.runtime.ashare_observation as ashare_observation_module
from shared.data.research_snapshot_store import (
    FileResearchSnapshotStore,
    ResearchSnapshotStoreConflict,
)
from shared.data.sharedsignals_v1 import HTTPResponse
from shared.runtime.ashare_observation_ledger import (
    OBSERVED_REASON_CODE,
    FileAshareObservationMembershipLedger,
)
from shared.runtime.ashare_observation import (
    AshareObservationBlocked,
    AshareObservationConfig,
    AshareObservationConfigurationError,
    run_ashare_observation,
)


CATALOG_VERSION = "catalog-ashare-observation-fixture-v1"
AS_OF = "2026-07-22T15:10:00+08:00"
DATASETS = {
    "trade_calendar": "fixture.cn.market.trade-calendar.v1",
    "security_master": "fixture.cn.equity.security-master.v1",
    "daily_bars": "fixture.cn.equity.daily.v1",
    "industry_context": "fixture.cn.industry.breadth.v1",
}
ROOT = Path(__file__).resolve().parents[1]


def _observation_receipt_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in root.glob("observation-*.json")
        if not path.name.startswith("observation-intent-")
        and not path.name.startswith("observation-complete-")
    )


def _manifest() -> dict[str, Any]:
    return {
        "manifest_version": 2,
        "profile_id": "ashare-phase1-current-observation-v1",
        "base_url": "https://tradingdatas.fixture.invalid",
        "catalog_version": CATALOG_VERSION,
        "access_policy_id": "ta-ashare-observation-read-v1",
        "transport_id": "http-json-v1",
        "timeout_seconds": 3,
        "as_of": AS_OF,
        "expected_probe_roles": [
            "trade_calendar",
            "security_master",
            "daily_bars",
            "industry_context",
        ],
        "datasets": [
            {
                "probe_role": "trade_calendar",
                "dataset_id": DATASETS["trade_calendar"],
                "schema_major": 2,
                "requirement_role": "required_execution",
                "fields": ["market", "cal_date", "is_open"],
                "filters": {"market": {"eq": "SSE"}},
                "limit": 100,
                "minimum_row_count": 1,
                "identity_fields": ["market", "cal_date"],
                "observation_mode": "current_observation",
                "query_as_of_mode": "decision_as_of",
                "max_pages": 2,
                "max_rows": 100,
                "row_event_time_field": "cal_date",
                "row_event_time_format": "yyyymmdd",
                "row_event_timezone": "Asia/Shanghai",
                "row_event_time_semantic": "scheduled",
            },
            {
                "probe_role": "security_master",
                "dataset_id": DATASETS["security_master"],
                "schema_major": 2,
                "requirement_role": "required_execution",
                "fields": ["ts_code", "name", "list_status", "list_date"],
                "filters": {"list_status": {"eq": "L"}},
                "limit": 100,
                "minimum_row_count": 1,
                "identity_fields": ["ts_code"],
                "observation_mode": "current_observation",
                "query_as_of_mode": "omit",
                "max_pages": 2,
                "max_rows": 100,
            },
            {
                "probe_role": "daily_bars",
                "dataset_id": DATASETS["daily_bars"],
                "schema_major": 2,
                "requirement_role": "required_execution",
                "fields": ["ts_code", "trade_date", "close", "vol", "amount"],
                "filters": {"trade_date": {"eq": "20260722"}},
                "limit": 10,
                "minimum_row_count": 1,
                "identity_fields": ["ts_code", "trade_date"],
                "observation_mode": "current_observation",
                "query_as_of_mode": "decision_as_of",
                "max_pages": 3,
                "max_rows": 20,
                "row_event_time_field": "trade_date",
                "row_event_time_format": "yyyymmdd",
                "row_event_timezone": "Asia/Shanghai",
                "row_event_time_semantic": "session",
            },
            {
                "probe_role": "industry_context",
                "dataset_id": DATASETS["industry_context"],
                "schema_major": 2,
                "requirement_role": "optional_context",
                "fields": ["industry_code", "trade_date", "breadth"],
                "filters": {"trade_date": {"eq": "20260722"}},
                "limit": 100,
                "minimum_row_count": 1,
                "identity_fields": ["industry_code", "trade_date"],
                "observation_mode": "current_observation",
                "query_as_of_mode": "decision_as_of",
                "max_pages": 2,
                "max_rows": 100,
                "row_event_time_field": "trade_date",
                "row_event_time_format": "yyyymmdd",
                "row_event_timezone": "Asia/Shanghai",
                "row_event_time_semantic": "session",
            },
        ],
    }


def _write_manifest(tmp_path: Path, payload: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "outside-repo" / "ashare-observation.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(payload or _manifest(), ensure_ascii=False),
        encoding="utf-8",
    )
    return path.resolve()


def _token_path(tmp_path: Path) -> Path:
    path = tmp_path / "outside-repo" / "tradingdatas-read.token"
    path.write_text("fixture-token-never-read-by-test-transport\n", encoding="utf-8")
    path.chmod(0o600)
    return path.resolve()


def _query_payload(
    dataset_id: str,
    *,
    request_id: str,
    rows: list[dict[str, Any]],
    next_cursor: str | None = None,
    data_through: str = "2026-07-22T15:00:00+08:00",
    observed_at: str = "2026-07-22T15:05:00+08:00",
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": request_id,
        "dataset_id": dataset_id,
        "data": copy.deepcopy(rows),
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
            "data_through": data_through,
            "observed_at": observed_at,
            "reasons": [],
        },
    }


class ObservationTransport:
    def __init__(
        self,
        *,
        change_second_daily_run: bool = False,
        change_snapshot_daily_run: bool = False,
        daily_trade_date: str = "20260722",
        data_through: str = "2026-07-22T15:00:00+08:00",
        observed_at: str = "2026-07-22T15:05:00+08:00",
        calendar_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._daily_first_page_count = 0
        self._change_second_daily_run = change_second_daily_run
        self._change_snapshot_daily_run = change_snapshot_daily_run
        self._daily_trade_date = daily_trade_date
        self._data_through = data_through
        self._observed_at = observed_at
        self._calendar_rows = calendar_rows or [
            {"market": "SSE", "cal_date": "20260722", "is_open": 1}
        ]

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
            return HTTPResponse(
                status_code=200,
                json_body={
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": f"catalog-{len(self.calls)}",
                    "data": [
                        {"dataset_id": dataset_id} for dataset_id in DATASETS.values()
                    ],
                },
            )
        assert method == "POST" and url.endswith("/v1/query")
        assert json_body is not None
        dataset_id = str(json_body["dataset_id"])
        cursor = json_body.get("cursor")
        request_id = f"query-{dataset_id}-{len(self.calls)}"
        if dataset_id == DATASETS["trade_calendar"]:
            return HTTPResponse(
                status_code=200,
                json_body=_query_payload(
                    dataset_id,
                    request_id=request_id,
                    rows=self._calendar_rows,
                    data_through=self._data_through,
                    observed_at=self._observed_at,
                ),
            )
        if dataset_id == DATASETS["industry_context"]:
            return HTTPResponse(
                status_code=200,
                json_body=_query_payload(
                    dataset_id,
                    request_id=request_id,
                    rows=[
                        {
                            "industry_code": "801010.SI",
                            "trade_date": self._daily_trade_date,
                            "breadth": 0.58,
                        }
                    ],
                    data_through=self._data_through,
                    observed_at=self._observed_at,
                ),
            )
        if dataset_id == DATASETS["security_master"]:
            rows = [
                {
                    "ts_code": symbol,
                    "name": name,
                    "list_status": status,
                    "list_date": list_date,
                }
                for symbol, name, status, list_date in (
                    ("600000.SH", "浦发银行", "L", "19991110"),
                    ("000001.SZ", "平安银行", "L", "19910403"),
                    ("300001.SZ", "特锐德", "L", "20091030"),
                    ("688001.SH", "华兴源创", "L", "20190722"),
                    ("430001.BJ", "世纪瑞尔", "L", "20211115"),
                    ("600001.SH", "*ST测试", "L", "19991201"),
                    ("600002.SH", "新股测试", "L", "20260710"),
                    ("600003.SH", "停牌测试", "L", "20000101"),
                    ("600004.SH", "缺日线测试", "L", "20000101"),
                )
            ]
            return HTTPResponse(
                status_code=200,
                json_body=_query_payload(
                    dataset_id,
                    request_id=request_id,
                    rows=rows,
                    data_through=self._data_through,
                    observed_at=self._observed_at,
                ),
            )
        assert dataset_id == DATASETS["daily_bars"]
        if cursor is None:
            self._daily_first_page_count += 1
            close = 10.0
            if self._change_second_daily_run and self._daily_first_page_count == 2:
                close = 10.1
            if self._change_snapshot_daily_run and self._daily_first_page_count == 3:
                close = 10.2
            return HTTPResponse(
                status_code=200,
                json_body=_query_payload(
                    dataset_id,
                    request_id=request_id,
                    rows=[
                        {
                            "ts_code": "600000.SH",
                            "trade_date": self._daily_trade_date,
                            "close": close,
                            "vol": 1000.0,
                            "amount": 10_000_000.0,
                        },
                        {
                            "ts_code": "300001.SZ",
                            "trade_date": self._daily_trade_date,
                            "close": 20.0,
                            "vol": 1000.0,
                            "amount": 20_000_000.0,
                        },
                        {
                            "ts_code": "600001.SH",
                            "trade_date": self._daily_trade_date,
                            "close": 5.0,
                            "vol": 1000.0,
                            "amount": 5_000_000.0,
                        },
                    ],
                    next_cursor="daily-page-2",
                    data_through=self._data_through,
                    observed_at=self._observed_at,
                ),
            )
        assert cursor == "daily-page-2"
        return HTTPResponse(
            status_code=200,
            json_body=_query_payload(
                dataset_id,
                request_id=request_id,
                rows=[
                    {
                        "ts_code": "688001.SH",
                        "trade_date": self._daily_trade_date,
                        "close": 30.0,
                        "vol": 1000.0,
                        "amount": 30_000_000.0,
                    },
                    {
                        "ts_code": "430001.BJ",
                        "trade_date": self._daily_trade_date,
                        "close": 8.0,
                        "vol": 1000.0,
                        "amount": 8_000_000.0,
                    },
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": self._daily_trade_date,
                        "close": 12.0,
                        "vol": 1000.0,
                        "amount": 12_000_000.0,
                    },
                    {
                        "ts_code": "600002.SH",
                        "trade_date": self._daily_trade_date,
                        "close": 9.0,
                        "vol": 1000.0,
                        "amount": 9_000_000.0,
                    },
                    {
                        "ts_code": "600003.SH",
                        "trade_date": self._daily_trade_date,
                        "close": 7.0,
                        "vol": 0.0,
                        "amount": 0.0,
                    },
                ],
                data_through=self._data_through,
                observed_at=self._observed_at,
            ),
        )


def _config(tmp_path: Path) -> AshareObservationConfig:
    return AshareObservationConfig(
        manifest_path=_write_manifest(tmp_path),
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "research-snapshots").resolve(),
        marketgraph_mode="mg_off",
        real_trading_enabled=False,
    )


def test_probe_precedes_immutable_current_observation_and_mainboard_projection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    factory_calls: list[tuple[str, Path, str]] = []

    def factory(transport_id: str, *, token_file: Path, base_url: str):
        factory_calls.append((transport_id, token_file, base_url))
        return transport

    result = run_ashare_observation(config, transport_factory=factory)

    assert result.status == "pass"
    assert result.mode == "observation_only"
    assert result.marketgraph_mode == "mg_off"
    assert result.real_trading_enabled is False
    assert result.historical_pit_eligible is False
    assert result.execution_authority is False
    assert result.idempotent_replay is False
    assert result.observation_session == "20260722"
    assert (
        result.observation_universe_semantics
        == "mainboard_observation_universe_not_order_eligible"
    )
    assert result.observation_universe_count == 2
    assert result.observation_universe_sha256 == result.tradable_universe_sha256
    assert len(result.observation_ledger_sha256) == 64
    assert result.tradable_symbols == ("000001.SZ", "600000.SH")
    assert result.tradable_universe_count == 2
    assert len(result.tradable_universe_sha256) == 64
    assert result.excluded_individual_count == 7
    assert result.excluded_reason_counts == {
        "beijing_security_not_in_phase_scope": 1,
        "chinext_individual_permission_unavailable": 1,
        "daily_bar_missing_or_unavailable": 1,
        "new_listing_excluded": 1,
        "risk_warning_security_excluded": 1,
        "star_individual_permission_unavailable": 1,
        "suspended_or_nonpositive_bar_excluded": 1,
    }
    assert result.context_probe_roles == ("industry_context",)
    assert result.probe_same_as_of_match is True
    assert len(result.probe_receipt_sha256) == 64
    assert len(result.observation_receipt_sha256) == 64
    assert len(result.observation_transaction_complete_sha256) == 64
    assert factory_calls == [
        (
            "http-json-v1",
            config.token_file,
            "https://tradingdatas.fixture.invalid",
        )
    ]

    persisted = FileResearchSnapshotStore(config.snapshot_root).load_bound_decision(
        profile_id="ashare-phase1-current-observation-v1",
        decision_as_of=AS_OF,
        catalog_version=CATALOG_VERSION,
    )
    assert persisted is not None
    assert persisted.snapshot_sha256 == result.snapshot_sha256
    assert persisted.historical_pit_eligible is False
    assert persisted.execution_eligible is True
    daily = next(
        item for item in persisted.datasets if item.dataset_id == DATASETS["daily_bars"]
    )
    assert daily.page_count == 2
    assert daily.row_count == 8
    assert {row["ts_code"] for row in daily.decoded_rows()} == {
        "600000.SH",
        "300001.SZ",
        "688001.SH",
        "430001.BJ",
        "000001.SZ",
        "600001.SH",
        "600002.SH",
        "600003.SH",
    }
    probe_paths = tuple(config.snapshot_root.glob("integration-*.json"))
    assert len(probe_paths) == 1
    assert probe_paths[0].stat().st_mode & 0o777 == 0o600
    observation_paths = _observation_receipt_paths(config.snapshot_root)
    assert len(observation_paths) == 1
    assert observation_paths[0].stat().st_mode & 0o777 == 0o600
    observation_receipt = json.loads(observation_paths[0].read_text(encoding="utf-8"))
    assert observation_receipt["snapshot_sha256"] == result.snapshot_sha256
    assert observation_receipt["probe_receipt_sha256"] == result.probe_receipt_sha256
    assert observation_receipt["receipt_sha256"] == result.observation_receipt_sha256
    assert len(tuple(config.snapshot_root.glob("observation-intent-*.json"))) == 1
    complete_paths = tuple(config.snapshot_root.glob("observation-complete-*.json"))
    assert len(complete_paths) == 1
    complete = json.loads(complete_paths[0].read_text(encoding="utf-8"))
    assert (
        complete["content_sha256"]
        == result.observation_transaction_complete_sha256
    )

    membership = FileAshareObservationMembershipLedger(
        config.snapshot_root / "observation-membership"
    ).load_bound_session(observation_session="20260722")
    assert membership is not None
    assert membership.content_sha256 == result.observation_ledger_sha256
    assert membership.universe_sha256 == result.observation_universe_sha256
    assert tuple(
        (item.symbol, item.disposition, item.reason_code) for item in membership.records
    ) == (
        ("000001.SZ", "observed", OBSERVED_REASON_CODE),
        (
            "300001.SZ",
            "excluded",
            "chinext_individual_permission_unavailable",
        ),
        ("430001.BJ", "excluded", "beijing_security_not_in_phase_scope"),
        ("600000.SH", "observed", OBSERVED_REASON_CODE),
        ("600001.SH", "excluded", "risk_warning_security_excluded"),
        ("600002.SH", "excluded", "new_listing_excluded"),
        (
            "600003.SH",
            "excluded",
            "suspended_or_nonpositive_bar_excluded",
        ),
        (
            "600004.SH",
            "excluded",
            "daily_bar_missing_or_unavailable",
        ),
        ("688001.SH", "excluded", "star_individual_permission_unavailable"),
    )

    first_call_count = len(transport.calls)
    replay = run_ashare_observation(config, transport_factory=factory)
    assert replay.idempotent_replay is True
    assert replay.snapshot_sha256 == result.snapshot_sha256
    assert replay.observation_ledger_sha256 == result.observation_ledger_sha256
    assert replay.tradable_symbols == result.tradable_symbols
    assert len(transport.calls) == first_call_count
    assert len(factory_calls) == 1


def test_same_observation_probe_failure_writes_no_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport(change_second_daily_run=True)

    with pytest.raises(AshareObservationBlocked, match="integration_probe_blocked"):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )

    assert not config.snapshot_root.exists()


def test_post_probe_snapshot_drift_writes_no_observation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport(change_snapshot_daily_run=True)

    with pytest.raises(
        AshareObservationBlocked,
        match="snapshot_read_drifted_after_probe",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )

    assert not config.snapshot_root.exists()


def test_prior_day_daily_rows_cannot_enter_current_tradable_projection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport(daily_trade_date="20260721")

    with pytest.raises(
        AshareObservationBlocked,
        match="daily_bar_trade_date_mismatch",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )
    assert not config.snapshot_root.exists()


def test_manifest_cannot_bind_a_session_older_than_latest_open_calendar_day(
    tmp_path: Path,
) -> None:
    payload = _manifest()
    payload["datasets"][2]["filters"] = {"trade_date": {"eq": "20260721"}}
    payload["datasets"][3]["filters"] = {"trade_date": {"eq": "20260721"}}
    config = AshareObservationConfig(
        manifest_path=_write_manifest(tmp_path, payload),
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "snapshots").resolve(),
    )

    with pytest.raises(
        AshareObservationBlocked,
        match="daily_bars_not_latest_completed_session",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(
                daily_trade_date="20260721"
            ),
        )

    assert not config.snapshot_root.exists()


def test_pre_close_as_of_cannot_bind_a_daily_observation(tmp_path: Path) -> None:
    payload = _manifest()
    payload["as_of"] = "2026-07-22T14:59:59+08:00"
    config = AshareObservationConfig(
        manifest_path=_write_manifest(tmp_path, payload),
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "snapshots").resolve(),
    )

    with pytest.raises(
        AshareObservationBlocked, match="post_close_observation_required"
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(),
        )
    assert not config.snapshot_root.exists()


def test_weekend_current_observation_binds_latest_completed_open_session(
    tmp_path: Path,
) -> None:
    payload = _manifest()
    payload["as_of"] = "2026-07-26T19:00:00+08:00"
    payload["datasets"][2]["filters"] = {"trade_date": {"eq": "20260724"}}
    payload["datasets"][3]["filters"] = {"trade_date": {"eq": "20260724"}}
    config = AshareObservationConfig(
        manifest_path=_write_manifest(tmp_path, payload),
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "snapshots").resolve(),
    )
    transport = ObservationTransport(
        daily_trade_date="20260724",
        data_through="2026-07-24T00:00:00+08:00",
        observed_at="2026-07-26T18:36:07+08:00",
        calendar_rows=[
            {"market": "SSE", "cal_date": "20260723", "is_open": 1},
            {"market": "SSE", "cal_date": "20260724", "is_open": 1},
            {"market": "SSE", "cal_date": "20260725", "is_open": 0},
            {"market": "SSE", "cal_date": "20260726", "is_open": 0},
        ],
    )

    result = run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: transport,
    )

    assert result.status == "pass"
    assert result.observation_session == "20260724"
    assert result.historical_pit_eligible is False
    assert result.execution_authority is False


@pytest.mark.parametrize(
    "observed_at",
    (
        "not-an-instant",
        "2026-07-22T14:59:59+08:00",
        "2026-07-22T15:11:00+08:00",
    ),
)
def test_invalid_or_time_inconsistent_observation_proof_is_blocked_by_probe(
    tmp_path: Path,
    observed_at: str,
) -> None:
    config = _config(tmp_path)

    with pytest.raises(AshareObservationBlocked, match="integration_probe_blocked"):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(
                observed_at=observed_at
            ),
        )

    assert not config.snapshot_root.exists()


def test_daily_data_through_must_match_the_observed_session(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with pytest.raises(
        AshareObservationBlocked,
        match="post_close_daily_data_through_required",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(
                data_through="2026-07-21T23:59:59+08:00"
            ),
        )

    assert not config.snapshot_root.exists()


def test_replay_rejects_missing_observation_membership_ledger(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: transport,
    )
    ledger_root = config.snapshot_root / "observation-membership"
    for path in ledger_root.iterdir():
        path.unlink()
    ledger_root.rmdir()
    call_count = len(transport.calls)

    with pytest.raises(
        AshareObservationBlocked,
        match="observation_membership_ledger_missing",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )
    assert len(transport.calls) == call_count


def test_replay_rejects_probe_receipt_not_bound_to_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    first = run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: transport,
    )
    probe_path = next(config.snapshot_root.glob("integration-*.json"))
    payload = json.loads(probe_path.read_text(encoding="utf-8"))
    payload["datasets"][0]["identity_sha256"] = "0" * 64
    unsigned = dict(payload)
    unsigned.pop("receipt_sha256")
    payload["receipt_sha256"] = (
        __import__("hashlib")
        .sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        .hexdigest()
    )
    probe_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    probe_path.chmod(0o600)

    with pytest.raises(
        AshareObservationBlocked,
        match="replay_probe_snapshot_binding_invalid",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )
    assert first.snapshot_sha256


def test_replay_never_recreates_missing_observation_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: transport,
    )
    observation_path = _observation_receipt_paths(config.snapshot_root)[0]
    observation_path.unlink()
    call_count = len(transport.calls)

    with pytest.raises(
        AshareObservationBlocked,
        match="observation_receipt_invalid",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )
    assert not observation_path.exists()
    assert len(transport.calls) == call_count


def test_legacy_three_artifact_state_without_intent_is_never_upgraded(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: transport,
    )
    next(config.snapshot_root.glob("observation-intent-*.json")).unlink()
    next(config.snapshot_root.glob("observation-complete-*.json")).unlink()
    membership_root = config.snapshot_root / "observation-membership"
    for path in membership_root.iterdir():
        if path.is_file():
            path.unlink()
    call_count = len(transport.calls)

    with pytest.raises(
        AshareObservationBlocked,
        match="observation_transaction_intent_missing",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy_state_must_not_create_transport")
            ),
        )

    assert len(transport.calls) == call_count
    assert not tuple(membership_root.glob("session-*.json"))


def test_probe_only_state_without_intent_is_never_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    original_compare_and_swap = FileResearchSnapshotStore.compare_and_swap

    def fail_after_probe(*_args: object, **_kwargs: object) -> None:
        raise ResearchSnapshotStoreConflict("injected_after_probe")

    monkeypatch.setattr(
        FileResearchSnapshotStore,
        "compare_and_swap",
        fail_after_probe,
    )
    with pytest.raises(
        AshareObservationBlocked,
        match="research_snapshot_store_commit_failed",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )
    next(config.snapshot_root.glob("observation-intent-*.json")).unlink()
    monkeypatch.setattr(
        FileResearchSnapshotStore,
        "compare_and_swap",
        original_compare_and_swap,
    )

    with pytest.raises(
        AshareObservationBlocked,
        match="observation_transaction_legacy_state_forbidden",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )

    assert len(tuple(config.snapshot_root.glob("integration-*.json"))) == 1
    assert (
        FileResearchSnapshotStore(config.snapshot_root).load_bound_decision(
            profile_id="ashare-phase1-current-observation-v1",
            decision_as_of=AS_OF,
            catalog_version=CATALOG_VERSION,
        )
        is None
    )


def test_complete_marker_link_publish_window_recovers_exactly(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: transport,
    )
    complete = next(config.snapshot_root.glob("observation-complete-*.json"))
    temporary = complete.parent / f".{complete.name}.injected.tmp"
    os.link(complete, temporary)
    assert complete.stat().st_nlink == 2
    call_count = len(transport.calls)

    replay = run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("marker_recovery_must_not_create_transport")
        ),
    )

    assert replay.idempotent_replay is True
    assert len(transport.calls) == call_count
    assert not temporary.exists()
    assert complete.stat().st_nlink == 1


def test_partial_commit_after_snapshot_recovers_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    original_write = ashare_observation_module._write_immutable_observation_receipt

    def fail_after_snapshot(*_args: object, **_kwargs: object) -> None:
        raise AshareObservationBlocked("injected_after_snapshot")

    monkeypatch.setattr(
        ashare_observation_module,
        "_write_immutable_observation_receipt",
        fail_after_snapshot,
    )
    with pytest.raises(AshareObservationBlocked, match="injected_after_snapshot"):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )

    assert (
        FileResearchSnapshotStore(config.snapshot_root).load_bound_decision(
            profile_id="ashare-phase1-current-observation-v1",
            decision_as_of=AS_OF,
            catalog_version=CATALOG_VERSION,
        )
        is not None
    )
    assert len(tuple(config.snapshot_root.glob("observation-intent-*.json"))) == 1
    assert not tuple(config.snapshot_root.glob("observation-complete-*.json"))
    assert not _observation_receipt_paths(config.snapshot_root)
    call_count = len(transport.calls)

    monkeypatch.setattr(
        ashare_observation_module,
        "_write_immutable_observation_receipt",
        original_write,
    )
    recovered = run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery_must_not_create_transport")
        ),
    )

    assert recovered.idempotent_replay is False
    assert len(transport.calls) == call_count
    assert len(tuple(config.snapshot_root.glob("observation-complete-*.json"))) == 1
    assert (
        FileAshareObservationMembershipLedger(
            config.snapshot_root / "observation-membership"
        ).load_bound_session(observation_session="20260722")
        is not None
    )


def test_partial_commit_after_receipt_recovers_membership_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    transport = ObservationTransport()
    original_persist = ashare_observation_module._persist_observation_membership

    def fail_after_receipt(*_args: object, **_kwargs: object) -> None:
        raise AshareObservationBlocked("injected_after_receipt")

    monkeypatch.setattr(
        ashare_observation_module,
        "_persist_observation_membership",
        fail_after_receipt,
    )
    with pytest.raises(AshareObservationBlocked, match="injected_after_receipt"):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: transport,
        )

    assert len(_observation_receipt_paths(config.snapshot_root)) == 1
    assert not tuple(config.snapshot_root.glob("observation-complete-*.json"))
    call_count = len(transport.calls)

    monkeypatch.setattr(
        ashare_observation_module,
        "_persist_observation_membership",
        original_persist,
    )
    recovered = run_ashare_observation(
        config,
        transport_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery_must_not_create_transport")
        ),
    )

    assert recovered.idempotent_replay is False
    assert len(transport.calls) == call_count
    assert len(tuple(config.snapshot_root.glob("observation-complete-*.json"))) == 1


def test_industry_and_index_roles_must_remain_optional_context(tmp_path: Path) -> None:
    payload = _manifest()
    context = next(
        item for item in payload["datasets"] if item["probe_role"] == "industry_context"
    )
    context["requirement_role"] = "required_execution"
    manifest = _write_manifest(tmp_path, payload)
    config = AshareObservationConfig(
        manifest_path=manifest,
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "snapshots").resolve(),
    )

    with pytest.raises(
        AshareObservationBlocked,
        match="context_probe_role_must_be_optional_context",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(),
        )
    assert not config.snapshot_root.exists()


def test_optional_context_rejects_nonaggregate_role(tmp_path: Path) -> None:
    payload = _manifest()
    context = next(
        item for item in payload["datasets"] if item["probe_role"] == "industry_context"
    )
    context["probe_role"] = "individual_candidates_industry"
    payload["expected_probe_roles"] = [
        "individual_candidates_industry" if role == "industry_context" else role
        for role in payload["expected_probe_roles"]
    ]
    config = AshareObservationConfig(
        manifest_path=_write_manifest(tmp_path, payload),
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "snapshots").resolve(),
    )

    with pytest.raises(
        AshareObservationBlocked,
        match="optional_context_role_not_aggregate",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(),
        )


def test_security_master_is_required_for_tradable_projection(tmp_path: Path) -> None:
    payload = _manifest()
    payload["datasets"] = [
        item for item in payload["datasets"] if item["probe_role"] != "security_master"
    ]
    payload["expected_probe_roles"] = [
        role for role in payload["expected_probe_roles"] if role != "security_master"
    ]
    config = AshareObservationConfig(
        manifest_path=_write_manifest(tmp_path, payload),
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "snapshots").resolve(),
    )

    with pytest.raises(
        AshareObservationBlocked,
        match="security_master_probe_role_required",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(),
        )


def test_daily_amount_is_required_for_forward_liquidity_history(
    tmp_path: Path,
) -> None:
    payload = _manifest()
    daily = next(
        item for item in payload["datasets"] if item["probe_role"] == "daily_bars"
    )
    daily["fields"].remove("amount")
    config = AshareObservationConfig(
        manifest_path=_write_manifest(tmp_path, payload),
        token_file=_token_path(tmp_path),
        snapshot_root=(tmp_path / "state" / "snapshots").resolve(),
    )

    with pytest.raises(
        AshareObservationBlocked,
        match="daily_bars_scope_contract_invalid",
    ):
        run_ashare_observation(
            config,
            transport_factory=lambda *_args, **_kwargs: ObservationTransport(),
        )
    assert not config.snapshot_root.exists()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"manifest_path": Path("manifest.json")}, "manifest_path_must_be_absolute"),
        ({"token_file": Path("token")}, "token_file_must_be_absolute"),
        (
            {"manifest_path": ROOT / "manifest.json"},
            "manifest_path_must_be_repository_external",
        ),
        (
            {"token_file": ROOT / "token"},
            "token_file_must_be_repository_external",
        ),
        ({"snapshot_root": Path("state")}, "snapshot_root_must_be_absolute"),
        ({"marketgraph_mode": "mg_on"}, "marketgraph_mode_must_be_mg_off"),
        ({"real_trading_enabled": True}, "real_trading_must_be_disabled"),
    ],
)
def test_runtime_configuration_is_explicit_and_simulation_only(
    tmp_path: Path,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    values: dict[str, Any] = {
        "manifest_path": _write_manifest(tmp_path),
        "token_file": _token_path(tmp_path),
        "snapshot_root": (tmp_path / "state" / "snapshots").resolve(),
        "marketgraph_mode": "mg_off",
        "real_trading_enabled": False,
    }
    values.update(overrides)

    with pytest.raises(AshareObservationConfigurationError, match=reason):
        AshareObservationConfig(**values)


def test_observation_module_has_no_trading_or_storage_fallback_imports() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "shared"
        / "runtime"
        / "ashare_observation.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_prefixes = (
        "shared.accounting",
        "shared.capital",
        "shared.execution",
        "shared.portfolio",
        "shared.review",
        "Ashare.sim",
    )
    assert not any(name.startswith(forbidden_prefixes) for name in imported), imported
    assert "sqlite" not in source_path.read_text(encoding="utf-8").lower()
    assert "shared.runtime.stage_ports" not in imported


def test_runner_cli_matches_dedicated_worker_unit_contract() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_ashare_observation.py"),
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    for argument in (
        "--manifest",
        "--token-file",
        "--state-root",
        "--runtime-root",
        "--log-root",
    ):
        assert argument in completed.stdout


def test_runner_cli_fails_closed_when_real_trading_is_not_exactly_false(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_ashare_observation.py"),
            "--manifest",
            str((tmp_path / "manifest.json").resolve()),
            "--token-file",
            str((tmp_path / "token").resolve()),
            "--state-root",
            str((tmp_path / "state").resolve()),
            "--runtime-root",
            str((tmp_path / "run").resolve()),
            "--log-root",
            str((tmp_path / "log").resolve()),
            "--json",
        ],
        cwd=ROOT,
        env={**os.environ, "REAL_TRADING_ENABLED": "true"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 64
    payload = json.loads(completed.stdout)
    assert payload == {
        "blocking": True,
        "real_trading_enabled": False,
        "reason_code": "real_trading_environment_must_equal_false",
        "status": "fail",
    }
    assert "token" not in completed.stderr.lower()


@pytest.mark.parametrize(
    "variable",
    (
        "TRADINGDATAS_API_TOKEN",
        "TRADINGDATAS_BEARER_TOKEN",
        "TRADINGDATAS_TOKEN",
    ),
)
def test_runner_cli_rejects_plaintext_token_environment(
    tmp_path: Path,
    variable: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_ashare_observation.py"),
            "--manifest",
            str((tmp_path / "manifest.json").resolve()),
            "--token-file",
            str((tmp_path / "token").resolve()),
            "--state-root",
            str((tmp_path / "state").resolve()),
            "--runtime-root",
            str((tmp_path / "run").resolve()),
            "--log-root",
            str((tmp_path / "log").resolve()),
            "--json",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "REAL_TRADING_ENABLED": "false",
            variable: "fixture-secret-must-not-be-consumed",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 64
    payload = json.loads(completed.stdout)
    assert payload["reason_code"] == "plaintext_token_environment_forbidden"
    assert "fixture-secret" not in completed.stdout
    assert "fixture-secret" not in completed.stderr
