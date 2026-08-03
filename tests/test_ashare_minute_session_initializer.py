from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path
from typing import Any

import pytest

import Ashare.minute_session_initializer as initializer_module
from Ashare.minute_session_initializer import (
    MinuteSessionInitializerError,
    _scaled_minute_profile,
    initialize_minute_session,
)
from shared.data.sharedsignals_v1 import HTTPResponse, SharedSignalsV1Error


CATALOG_VERSION = "v1-minute-session-fixture"
REPO_ROOT = Path(__file__).resolve().parents[1]


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
    fields: list[str],
    *,
    max_page_size: int = 500,
    identity_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "schema_major": 2,
        "default_fields": list(fields),
        "default_order": [f"{fields[0]}:asc"],
        "identity_fields": list(
            fields[:2] if identity_fields is None else identity_fields
        ),
        "fields": [_field(name) for name in fields],
        "filter_operators": {
            name: ["eq", "in", "gte", "lte", "between"] for name in fields
        },
        "limits": {
            "max_page_size": max_page_size,
            "max_lookback_days": 36500,
        },
        "availability": {"activation_states": ["active"]},
    }


def _catalog_rows(
    *,
    daily_max_page_size: int = 500,
    daily_identity_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        _catalog_row(
            "cn.market.trade_calendar",
            ["exchange", "cal_date", "is_open", "pretrade_date"],
        ),
        _catalog_row(
            "cn.equity.daily",
            ["ts_code", "trade_date", "close"],
            max_page_size=daily_max_page_size,
            identity_fields=(
                daily_identity_fields
                if daily_identity_fields is not None
                else ["ts_code", "trade_date"]
            ),
        ),
        _catalog_row(
            "cn.dataset.rt_min",
            ["ts_code", "time", "open", "high", "low", "close", "vol", "amount"],
        ),
    ]


def _metadata(dataset_id: str, *, degraded: bool = False) -> dict[str, Any]:
    return {
        "state": "partial" if degraded else "ready",
        "degraded": degraded,
        "freshness": {
            "state": "unknown" if degraded else "fresh",
            "stale": degraded,
        },
        "quality": {
            "state": "degraded" if degraded else "valid",
            "valid": not degraded,
        },
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "provider": "fixture",
            "transport_service": "fixture",
        },
        "receipt_id": f"receipt:{dataset_id}",
        "data_through": "2026-07-28T16:31:00+08:00",
        "observed_at": "2026-07-28T16:31:01+08:00",
        "reasons": ["fixture_degraded"] if degraded else [],
    }


class FixtureTransport:
    def __init__(
        self,
        *,
        open_day: bool = True,
        daily_degraded: bool = False,
        omit_symbol: str | None = None,
        daily_rows: list[dict[str, Any]] | None = None,
        daily_max_page_size: int = 500,
        daily_response_page_size: int | None = None,
        daily_http_status: int | None = None,
        daily_timeout: bool = False,
        daily_catalog_drift: bool = False,
        daily_catalog_identity_fields: list[str] | None = None,
        daily_duplicate: bool = False,
        daily_replay_change: bool = False,
        daily_wrong_trade_date: bool = False,
        catalog_http_status: int | None = None,
    ) -> None:
        self.open_day = open_day
        self.daily_degraded = daily_degraded
        self.omit_symbol = omit_symbol
        self.daily_rows = daily_rows
        self.daily_max_page_size = daily_max_page_size
        self.daily_response_page_size = daily_response_page_size
        self.daily_http_status = daily_http_status
        self.daily_timeout = daily_timeout
        self.daily_catalog_drift = daily_catalog_drift
        self.daily_catalog_identity_fields = daily_catalog_identity_fields
        self.daily_duplicate = daily_duplicate
        self.daily_replay_change = daily_replay_change
        self.daily_wrong_trade_date = daily_wrong_trade_date
        self.catalog_http_status = catalog_http_status
        self.daily_base_query_count = 0
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
            if self.catalog_http_status is not None:
                return HTTPResponse(
                    status_code=self.catalog_http_status,
                    json_body={"error": "fixture catalog http failure"},
                )
            return HTTPResponse(
                status_code=200,
                json_body={
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": f"catalog-{len(self.calls)}",
                    "data": _catalog_rows(
                        daily_max_page_size=self.daily_max_page_size,
                        daily_identity_fields=self.daily_catalog_identity_fields,
                    ),
                },
            )
        assert json_body is not None
        dataset_id = json_body["dataset_id"]
        if dataset_id == "cn.market.trade_calendar":
            rows = [
                {
                    "exchange": "SSE",
                    "cal_date": "20260729",
                    "is_open": 1 if self.open_day else 0,
                    "pretrade_date": "20260728",
                }
            ]
            degraded = False
        elif dataset_id == "cn.equity.daily":
            if self.daily_http_status is not None:
                return HTTPResponse(
                    status_code=self.daily_http_status,
                    json_body={"error": "fixture daily http failure"},
                )
            if self.daily_timeout:
                raise TimeoutError("fixture daily timeout")
            rows = (
                copy.deepcopy(self.daily_rows)
                if self.daily_rows is not None
                else [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260728",
                        "close": 11.11,
                    },
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20260728",
                        "close": 13.14,
                    },
                ]
            )
            requested_symbols = set(json_body["filters"]["ts_code"]["in"])
            rows = [row for row in rows if row["ts_code"] in requested_symbols]
            if self.omit_symbol is not None:
                rows = [row for row in rows if row["ts_code"] != self.omit_symbol]
            if self.daily_wrong_trade_date and rows:
                rows[0]["trade_date"] = "20260725"
            cursor = json_body.get("cursor")
            if cursor is None:
                self.daily_base_query_count += 1
                offset = 0
            else:
                offset = int(str(cursor).removeprefix("daily-offset-"))
            if self.daily_replay_change and self.daily_base_query_count == 2 and rows:
                rows[0]["close"] += 0.01
            page_size = self.daily_response_page_size or len(rows)
            page_rows = rows[offset : offset + page_size]
            if self.daily_duplicate and offset > 0 and page_rows:
                page_rows[0] = copy.deepcopy(rows[0])
            next_offset = offset + page_size
            next_cursor = (
                f"daily-offset-{next_offset}" if next_offset < len(rows) else None
            )
            degraded = self.daily_degraded
        else:
            raise AssertionError(f"unexpected query {dataset_id}")
        return HTTPResponse(
            status_code=200,
            json_body={
                "api_version": "v1",
                "catalog_version": (
                    "v1-minute-session-drift"
                    if dataset_id == "cn.equity.daily" and self.daily_catalog_drift
                    else CATALOG_VERSION
                ),
                "request_id": f"query-{len(self.calls)}",
                "dataset_id": dataset_id,
                "data": page_rows if dataset_id == "cn.equity.daily" else rows,
                "next_cursor": (
                    next_cursor if dataset_id == "cn.equity.daily" else None
                ),
                "metadata": _metadata(dataset_id, degraded=degraded),
            },
        )


def _template(root: Path) -> Path:
    day = root / "20260728"
    day.mkdir(parents=True)
    (day / "minute-manifest.json").write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:18082",
                "expected_catalog_version": "v1-old",
                "dataset_id": "cn.dataset.rt_min",
                "access_policy_id": "tradingagent-read-v1",
                "transport_id": "http-json-v1",
                "timeout_seconds": 20,
                "filters": {},
                "profile": {
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
                    "max_rows": 30,
                    "page_limit": 30,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    universe = [
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "industry": "银行",
            "research_theme": "mainboard_opportunity_scan",
            "list_date": "1991-04-03",
            "risk_warning": False,
            "delisting_risk": False,
            "context_only": False,
        },
        {
            "symbol": "600000.SH",
            "name": "浦发银行",
            "industry": "银行",
            "research_theme": "mainboard_opportunity_scan",
            "list_date": "1999-11-10",
            "risk_warning": False,
            "delisting_risk": False,
            "context_only": False,
        },
    ]
    (day / "universe.json").write_text(
        json.dumps(universe) + "\n",
        encoding="utf-8",
    )
    (day / "reference-facts.json").write_text("[]\n", encoding="utf-8")
    return day


def _large_universe(count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    universe: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    for offset in range(count):
        symbol = f"{600000 + offset:06d}.SH"
        universe.append(
            {
                "symbol": symbol,
                "name": f"主板样本{offset:03d}",
                "industry": "主板扫描",
                "research_theme": "mainboard_opportunity_scan",
                "list_date": "2020-01-01",
                "risk_warning": False,
                "delisting_risk": False,
                "context_only": False,
            }
        )
        daily.append(
            {
                "ts_code": symbol,
                "trade_date": "20260728",
                "close": 10.0 + offset / 100,
            }
        )
    return universe, daily


def _factory(transport: FixtureTransport):
    def build(
        transport_id: str,
        *,
        token_file: Path,
        base_url: str,
    ) -> FixtureTransport:
        assert transport_id == "http-json-v1"
        assert token_file == Path("/run/private/token")
        assert base_url == "http://127.0.0.1:18082"
        return transport

    return build


def _now() -> datetime:
    return datetime.fromisoformat("2026-07-29T09:20:00+08:00")


def _daily_requests(transport: FixtureTransport) -> list[dict[str, Any]]:
    return [
        call["json_body"]
        for call in transport.calls
        if isinstance(call["json_body"], dict)
        and call["json_body"].get("dataset_id") == "cn.equity.daily"
    ]


def test_initializer_writes_three_inputs_without_state_bundle(tmp_path: Path) -> None:
    _template(tmp_path)
    transport = FixtureTransport()

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        transport_factory=_factory(transport),
    )

    assert result["status"] == "pass"
    assert result["symbol_count"] == 2
    assert result["previous_session"] == "2026-07-28"
    assert result["reused"] is False
    day = tmp_path / "20260729"
    assert sorted(path.name for path in day.iterdir()) == [
        "minute-manifest.json",
        "reference-facts.json",
        "universe.json",
    ]
    manifest = json.loads((day / "minute-manifest.json").read_text())
    assert manifest["expected_catalog_version"] == "v1-old"
    assert manifest["observed_catalog_version"] == CATALOG_VERSION
    assert manifest["catalog_version_drift"] is True
    assert len(manifest["profile"]["dataset_contract_fingerprint"]) == 64
    assert len(manifest["profile"]["consumer_profile_sha256"]) == 64
    assert manifest["profile"]["max_pages"] == 1
    assert manifest["profile"]["max_rows"] == 2
    assert manifest["profile"]["page_limit"] == 2
    references = json.loads((day / "reference-facts.json").read_text())
    assert [row["symbol"] for row in references] == ["000001.SZ", "600000.SH"]
    assert [row["previous_close_cny"] for row in references] == [11.11, 13.14]
    assert all(row["suspended"] is False for row in references)
    assert all(len(row["evidence_sha256"]) == 64 for row in references)
    assert not (day / "state-bundle.json").exists()
    daily_requests = _daily_requests(transport)
    assert len(daily_requests) == 2
    assert daily_requests[0]["filters"] == {
        "trade_date": {"eq": "20260728"},
        "ts_code": {"in": ["000001.SZ", "600000.SH"]},
    }


def test_initializer_atomically_publishes_a_named_copilot_tracking_universe(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    output = tmp_path / "trading-copilot" / "tracking-universe.json"
    output.parent.mkdir()
    transport = FixtureTransport()

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        tracking_universe_output=output,
        transport_factory=_factory(transport),
    )

    assert result["tracking_universe_published"] is True
    assert result["tracking_universe_symbol_count"] == 2
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "contractId": "tradingagent.trading_copilot_tracking_universe.v1",
        "generatedAt": "2026-07-29T09:20:00+08:00",
        "items": [
            {"symbol": "000001.SZ", "name": "平安银行"},
            {"symbol": "600000.SH", "name": "浦发银行"},
        ],
    }


def test_initializer_rejects_a_symlinked_copilot_tracking_universe_output(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    output = tmp_path / "tracking-universe.json"
    output.symlink_to(tmp_path / "outside.json")

    with pytest.raises(
        MinuteSessionInitializerError,
        match="tracking_universe_output_invalid",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            tracking_universe_output=output,
            transport_factory=_factory(FixtureTransport()),
        )


def test_initializer_bootstraps_an_empty_root_from_explicit_reviewed_inputs(
    tmp_path: Path,
) -> None:
    template = _template(tmp_path / "reviewed-inputs")
    bootstrap_manifest = template / "minute-manifest.json"
    universe = template / "universe.json"
    state_root = tmp_path / "runtime"
    transport = FixtureTransport()

    result = initialize_minute_session(
        state_root=state_root,
        token_file=Path("/run/private/token"),
        now=_now(),
        universe_source=universe,
        bootstrap_manifest=bootstrap_manifest,
        transport_factory=_factory(transport),
    )

    assert result["status"] == "pass"
    assert result["bootstrap"] is True
    assert (state_root / "20260729" / "minute-manifest.json").is_file()


def test_initializer_rejects_bootstrap_without_an_explicit_universe(
    tmp_path: Path,
) -> None:
    template = _template(tmp_path / "reviewed-inputs")

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_bootstrap_universe_required",
    ):
        initialize_minute_session(
            state_root=tmp_path / "runtime",
            token_file=Path("/run/private/token"),
            now=_now(),
            bootstrap_manifest=template / "minute-manifest.json",
            transport_factory=_factory(FixtureTransport()),
        )


def test_initializer_rejects_bootstrap_after_a_state_root_is_initialized(
    tmp_path: Path,
) -> None:
    template = _template(tmp_path)
    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_bootstrap_not_permitted_after_initialization",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            bootstrap_manifest=template / "minute-manifest.json",
            universe_source=template / "universe.json",
            transport_factory=_factory(FixtureTransport()),
        )


def test_initializer_promotes_explicit_reviewed_universe_to_500(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, daily = _large_universe(500)
    source = tmp_path / "reviewed-universe-500.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        universe_source=source,
        transport_factory=_factory(FixtureTransport(daily_rows=daily)),
    )

    assert result["status"] == "pass"
    assert result["symbol_count"] == 500
    assert result["profile_max_pages"] == 1
    assert result["profile_max_rows"] == 500
    assert result["profile_page_limit"] == 500
    day = tmp_path / "20260729"
    manifest = json.loads((day / "minute-manifest.json").read_text())
    published_universe = json.loads((day / "universe.json").read_text())
    assert manifest["profile"]["max_pages"] == 1
    assert manifest["profile"]["max_rows"] == 500
    assert manifest["profile"]["page_limit"] == 500
    assert manifest["universe_sha256"] == result["universe_sha256"]
    assert published_universe == universe


@pytest.mark.parametrize(
    ("symbol_count", "expected_batch_size", "expected_request_count"),
    [(1, 1, 2), (10, 10, 2), (500, 100, 10)],
)
def test_initializer_bounds_daily_batches_by_v1_in_filter_limit(
    tmp_path: Path,
    symbol_count: int,
    expected_batch_size: int,
    expected_request_count: int,
) -> None:
    _template(tmp_path)
    universe, daily = _large_universe(symbol_count)
    source = tmp_path / f"reviewed-universe-{symbol_count}.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")
    transport = FixtureTransport(daily_rows=daily)

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        universe_source=source,
        transport_factory=_factory(transport),
    )

    daily_requests = _daily_requests(transport)
    assert result["symbol_count"] == symbol_count
    assert len(daily_requests) == expected_request_count
    assert all(request["limit"] == expected_batch_size for request in daily_requests)
    assert all(
        len(request["filters"]["ts_code"]["in"]) == expected_batch_size
        for request in daily_requests
    )


def test_initializer_uses_five_catalog_sized_batches_for_500_symbols(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, daily = _large_universe(500)
    source = tmp_path / "reviewed-universe-500.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")
    transport = FixtureTransport(
        daily_rows=daily,
        daily_max_page_size=100,
    )

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        universe_source=source,
        transport_factory=_factory(transport),
    )

    daily_requests = _daily_requests(transport)
    assert result["symbol_count"] == 500
    assert len(daily_requests) == 10
    assert all(request["limit"] == 100 for request in daily_requests)
    assert all(
        len(request["filters"]["ts_code"]["in"]) == 100 for request in daily_requests
    )


def test_initializer_follows_bounded_daily_pagination_for_each_replay(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, daily = _large_universe(10)
    source = tmp_path / "reviewed-universe-10.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")
    transport = FixtureTransport(
        daily_rows=daily,
        daily_max_page_size=10,
        daily_response_page_size=4,
    )

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        universe_source=source,
        transport_factory=_factory(transport),
    )

    daily_requests = _daily_requests(transport)
    assert result["symbol_count"] == 10
    assert len(daily_requests) == 6
    assert [request.get("cursor") for request in daily_requests] == [
        None,
        "daily-offset-4",
        "daily-offset-8",
        None,
        "daily-offset-4",
        "daily-offset-8",
    ]
    assert all(request["limit"] == 10 for request in daily_requests)


def test_scaled_profile_uses_catalog_page_budget() -> None:
    profile = _scaled_minute_profile(
        {"max_pages": 1, "max_rows": 30, "page_limit": 30},
        symbol_count=500,
        catalog_page_size=200,
    )

    assert profile == {"max_pages": 3, "max_rows": 500, "page_limit": 200}


def test_explicit_universe_source_must_be_absolute(tmp_path: Path) -> None:
    _template(tmp_path)

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_universe_source_invalid",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            universe_source=Path("relative-universe.json"),
            transport_factory=_factory(FixtureTransport()),
        )


def test_explicit_universe_source_keeps_risk_security_observation_only(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, daily = _large_universe(2)
    universe[0]["risk_warning"] = True
    universe[1]["delisting_risk"] = True
    source = tmp_path / "reviewed-universe-ineligible.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        universe_source=source,
        transport_factory=_factory(FixtureTransport(daily_rows=daily)),
    )
    initialized = json.loads(
        (tmp_path / "20260729" / "universe.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "pass"
    assert result["symbol_count"] == 2
    assert initialized[0]["risk_warning"] is True
    assert initialized[1]["delisting_risk"] is True


def test_explicit_universe_source_still_rejects_recent_listing(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, _ = _large_universe(2)
    universe[0]["list_date"] = "2026-07-01"
    source = tmp_path / "reviewed-universe-recent-listing.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_universe_ineligible",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            universe_source=source,
            transport_factory=_factory(FixtureTransport()),
        )


def test_cli_uses_reviewed_universe_source_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "reviewed-universe-500.json"
    source.write_text("[]\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_initialize(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "pass", "real_trading_enabled": False}

    monkeypatch.setenv("ASHARE_MINUTE_UNIVERSE_SOURCE", str(source))
    monkeypatch.setattr(
        initializer_module,
        "initialize_minute_session",
        fake_initialize,
    )

    assert (
        initializer_module.main(
            [
                "--state-root",
                str(tmp_path / "state"),
                "--token-file",
                "/run/private/token",
                "--now",
                "2026-07-29T09:20:00+08:00",
            ]
        )
        == 0
    )
    assert captured["universe_source"] == source


def test_cli_uses_copilot_tracking_universe_output_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "tracking-universe.json"
    captured: dict[str, Any] = {}

    def fake_initialize(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "pass", "real_trading_enabled": False}

    monkeypatch.setenv("TRADING_COPILOT_TRACKING_UNIVERSE_PATH", str(output))
    monkeypatch.setattr(initializer_module, "initialize_minute_session", fake_initialize)

    assert (
        initializer_module.main(
            [
                "--state-root",
                str(tmp_path / "state"),
                "--token-file",
                "/run/private/token",
                "--now",
                "2026-07-29T09:20:00+08:00",
            ]
        )
        == 0
    )
    assert captured["tracking_universe_output"] == output


def test_cli_logs_only_a_structured_initializer_failure_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_initialize(**kwargs: Any) -> dict[str, object]:
        raise MinuteSessionInitializerError("minute_session_template_missing")

    monkeypatch.setattr(initializer_module, "initialize_minute_session", fail_initialize)

    assert (
        initializer_module.main(
            [
                "--state-root",
                str(tmp_path / "state"),
                "--token-file",
                "/run/private/token",
                "--now",
                "2026-07-29T09:20:00+08:00",
            ]
        )
        == 2
    )
    assert capsys.readouterr().err == (
        "minute session initializer failed closed: minute_session_template_missing\n"
    )


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            initializer_module.MinuteCanaryConfigurationError("private detail"),
            "minute_session_canary_configuration_invalid",
        ),
        (
            initializer_module.RuntimeGateConfigurationError("private detail"),
            "minute_session_transport_configuration_invalid",
        ),
        (
            initializer_module.SharedSignalsV1Error("private detail"),
            "minute_session_dependency_failed",
        ),
        (OSError("private detail"), "minute_session_dependency_failed"),
        (ValueError("private detail"), "minute_session_input_invalid"),
    ],
)
def test_cli_classifies_non_initializer_failures_without_echoing_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_code: str,
) -> None:
    def fail_initialize(**kwargs: Any) -> dict[str, object]:
        raise error

    monkeypatch.setattr(initializer_module, "initialize_minute_session", fail_initialize)

    assert (
        initializer_module.main(
            [
                "--state-root",
                str(tmp_path / "state"),
                "--token-file",
                "/run/private/token",
                "--now",
                "2026-07-29T09:20:00+08:00",
            ]
        )
        == 2
    )
    assert capsys.readouterr().err == (
        f"minute session initializer failed closed: {expected_code}\n"
    )


def test_initializer_exact_replay_is_idempotent(tmp_path: Path) -> None:
    _template(tmp_path)

    first = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        transport_factory=_factory(FixtureTransport()),
    )
    before = {
        path.name: path.read_bytes() for path in (tmp_path / "20260729").iterdir()
    }
    second = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        transport_factory=_factory(FixtureTransport()),
    )

    after = {path.name: path.read_bytes() for path in (tmp_path / "20260729").iterdir()}
    assert first["reused"] is False
    assert second["reused"] is True
    assert before == after


def test_closed_day_is_noop_without_target_directory(tmp_path: Path) -> None:
    _template(tmp_path)

    result = initialize_minute_session(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_now(),
        transport_factory=_factory(FixtureTransport(open_day=False)),
    )

    assert result["reason"] == "market_closed"
    assert not (tmp_path / "20260729").exists()


def test_degraded_daily_fails_closed(tmp_path: Path) -> None:
    _template(tmp_path)

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_dataset_rejected:cn.equity.daily",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(FixtureTransport(daily_degraded=True)),
        )

    assert not (tmp_path / "20260729").exists()


def test_daily_catalog_identity_mismatch_fails_before_any_query(tmp_path: Path) -> None:
    _template(tmp_path)
    transport = FixtureTransport(daily_catalog_identity_fields=["ts_code"])

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_identity_invalid:cn.equity.daily",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(transport),
        )

    assert [call["method"] for call in transport.calls] == ["GET"]
    assert not (tmp_path / "20260729").exists()


def test_incomplete_daily_universe_fails_closed(tmp_path: Path) -> None:
    _template(tmp_path)

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_daily_universe_incomplete",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(FixtureTransport(omit_symbol="600000.SH")),
        )

    assert not (tmp_path / "20260729").exists()


def test_duplicate_daily_identity_fails_closed_without_publishing(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, daily = _large_universe(10)
    source = tmp_path / "reviewed-universe-10.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")

    with pytest.raises(SharedSignalsV1Error, match="duplicate_row_identity"):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            universe_source=source,
            transport_factory=_factory(
                FixtureTransport(
                    daily_rows=daily,
                    daily_max_page_size=10,
                    daily_response_page_size=4,
                    daily_duplicate=True,
                )
            ),
        )

    assert not (tmp_path / "20260729").exists()


def test_wrong_daily_trade_date_fails_exact_identity_check(tmp_path: Path) -> None:
    _template(tmp_path)
    transport = FixtureTransport(daily_wrong_trade_date=True)

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_daily_identity_mismatch",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(transport),
        )

    assert len(_daily_requests(transport)) == 2
    assert not (tmp_path / "20260729").exists()


def test_daily_pagination_page_budget_fails_closed_without_query_storm(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, daily = _large_universe(10)
    source = tmp_path / "reviewed-universe-10.json"
    source.write_text(json.dumps(universe) + "\n", encoding="utf-8")
    transport = FixtureTransport(
        daily_rows=daily,
        daily_max_page_size=10,
        daily_response_page_size=1,
    )

    with pytest.raises(SharedSignalsV1Error, match="page_budget_exceeded"):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            universe_source=source,
            transport_factory=_factory(transport),
        )

    assert len(_daily_requests(transport)) == 5
    assert not (tmp_path / "20260729").exists()


def test_daily_replay_change_fails_closed_without_publishing(tmp_path: Path) -> None:
    _template(tmp_path)
    transport = FixtureTransport(daily_replay_change=True)

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_replay_mismatch:cn.equity.daily",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(transport),
        )

    assert len(_daily_requests(transport)) == 2
    assert not (tmp_path / "20260729").exists()


@pytest.mark.parametrize("status_code", [413, 429])
def test_daily_http_error_fails_closed_without_retry_or_publish(
    tmp_path: Path,
    status_code: int,
) -> None:
    _template(tmp_path)
    transport = FixtureTransport(daily_http_status=status_code)

    with pytest.raises(SharedSignalsV1Error, match=f"HTTP {status_code}"):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(transport),
        )

    assert len(_daily_requests(transport)) == 1
    assert not (tmp_path / "20260729").exists()


def test_catalog_429_fails_closed_without_query_or_retry(tmp_path: Path) -> None:
    _template(tmp_path)
    transport = FixtureTransport(catalog_http_status=429)

    with pytest.raises(
        MinuteSessionInitializerError,
        match="minute_session_catalog_http_failed",
    ):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(transport),
        )

    assert len(transport.calls) == 1
    assert _daily_requests(transport) == []
    assert not (tmp_path / "20260729").exists()


def test_daily_timeout_fails_closed_without_retry_or_publish(tmp_path: Path) -> None:
    _template(tmp_path)
    transport = FixtureTransport(daily_timeout=True)

    with pytest.raises(TimeoutError, match="fixture daily timeout"):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(transport),
        )

    assert len(_daily_requests(transport)) == 1
    assert not (tmp_path / "20260729").exists()


def test_daily_catalog_drift_fails_closed_without_retry_or_publish(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    transport = FixtureTransport(daily_catalog_drift=True)

    with pytest.raises(SharedSignalsV1Error, match="catalog_version"):
        initialize_minute_session(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_now(),
            transport_factory=_factory(transport),
        )

    assert len(_daily_requests(transport)) == 1
    assert not (tmp_path / "20260729").exists()


def test_session_units_are_preopen_simulation_only_and_sandboxed() -> None:
    service = (
        REPO_ROOT / "deploy/systemd/tradingagent-ashare-minute-session.service"
    ).read_text(encoding="utf-8")
    timer = (
        REPO_ROOT / "deploy/systemd/tradingagent-ashare-minute-session.timer"
    ).read_text(encoding="utf-8")

    for required in (
        "Type=oneshot",
        "User=tradingagent",
        "Group=tradingagent",
        "Environment=REAL_TRADING_ENABLED=false",
        "ConditionPathExists=/run/secrets/tradingagent/tradingdatas-read.token",
        "-m Ashare.minute_session_initializer",
        "ProtectSystem=strict",
        "NoNewPrivileges=true",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "ReadWritePaths=/var/lib/tradingagent/ashare-minute-paper",
    ):
        assert required in service
    for forbidden in (
        "broker",
        "qmt",
        "ptrade",
        "/tushare",
        "/source_status",
        ":8082",
        "sqlite",
        "deepseek",
        "openai",
    ):
        assert forbidden not in service.lower()

    assert "OnCalendar=Mon..Fri *-*-* 09:18:00" in timer
    assert "Persistent=false" in timer
    assert "Unit=tradingagent-ashare-minute-session.service" in timer
