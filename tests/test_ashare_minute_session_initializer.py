from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from Ashare.minute_session_initializer import (
    MinuteSessionInitializerError,
    _scaled_minute_profile,
    initialize_minute_session,
)
from shared.data.sharedsignals_v1 import HTTPResponse


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


def _catalog_row(dataset_id: str, fields: list[str]) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "schema_major": 2,
        "default_fields": list(fields),
        "default_order": [f"{fields[0]}:asc"],
        "fields": [_field(name) for name in fields],
        "filter_operators": {
            name: ["eq", "in", "gte", "lte", "between"] for name in fields
        },
        "limits": {"max_page_size": 500, "max_lookback_days": 36500},
        "availability": {"activation_states": ["active"]},
    }


def _catalog_rows() -> list[dict[str, Any]]:
    return [
        _catalog_row(
            "cn.market.trade_calendar",
            ["exchange", "cal_date", "is_open", "pretrade_date"],
        ),
        _catalog_row(
            "cn.equity.daily",
            ["ts_code", "trade_date", "close"],
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
    ) -> None:
        self.open_day = open_day
        self.daily_degraded = daily_degraded
        self.omit_symbol = omit_symbol
        self.daily_rows = daily_rows
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
                    "request_id": f"catalog-{len(self.calls)}",
                    "data": _catalog_rows(),
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
            if self.omit_symbol is not None:
                rows = [row for row in rows if row["ts_code"] != self.omit_symbol]
            degraded = self.daily_degraded
        else:
            raise AssertionError(f"unexpected query {dataset_id}")
        return HTTPResponse(
            status_code=200,
            json_body={
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"query-{len(self.calls)}",
                "dataset_id": dataset_id,
                "data": rows,
                "next_cursor": None,
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
                "catalog_version": "v1-old",
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
    assert manifest["catalog_version"] == CATALOG_VERSION
    assert manifest["profile"]["max_pages"] == 1
    assert manifest["profile"]["max_rows"] == 2
    assert manifest["profile"]["page_limit"] == 2
    references = json.loads((day / "reference-facts.json").read_text())
    assert [row["symbol"] for row in references] == ["000001.SZ", "600000.SH"]
    assert [row["previous_close_cny"] for row in references] == [11.11, 13.14]
    assert all(row["suspended"] is False for row in references)
    assert all(len(row["evidence_sha256"]) == 64 for row in references)
    assert not (day / "state-bundle.json").exists()
    daily_requests = [
        call["json_body"]
        for call in transport.calls
        if isinstance(call["json_body"], dict)
        and call["json_body"].get("dataset_id") == "cn.equity.daily"
    ]
    assert len(daily_requests) == 2
    assert daily_requests[0]["filters"] == {
        "trade_date": {"eq": "20260728"},
        "ts_code": {"in": ["000001.SZ", "600000.SH"]},
    }


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


def test_explicit_universe_source_rejects_ineligible_security(
    tmp_path: Path,
) -> None:
    _template(tmp_path)
    universe, _ = _large_universe(2)
    universe[0]["risk_warning"] = True
    source = tmp_path / "reviewed-universe-ineligible.json"
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

    after = {
        path.name: path.read_bytes() for path in (tmp_path / "20260729").iterdir()
    }
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
            transport_factory=_factory(
                FixtureTransport(omit_symbol="600000.SH")
            ),
        )

    assert not (tmp_path / "20260729").exists()


def test_session_units_are_preopen_simulation_only_and_sandboxed() -> None:
    service = (
        REPO_ROOT
        / "deploy/systemd/tradingagent-ashare-minute-session.service"
    ).read_text(encoding="utf-8")
    timer = (
        REPO_ROOT
        / "deploy/systemd/tradingagent-ashare-minute-session.timer"
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

    assert "OnCalendar=Mon..Fri *-*-* 09:20:00" in timer
    assert "Persistent=false" in timer
    assert "Unit=tradingagent-ashare-minute-session.service" in timer
