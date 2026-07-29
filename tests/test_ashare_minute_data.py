from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime
from typing import Any

import pytest

from Ashare.minute_data import (
    MinuteDataContractError,
    MinuteDatasetProfile,
    MinuteEvidenceAuditLedger,
    MinuteEvidenceUse,
    MinuteReferenceFact,
    MinuteTimestampSemantics,
    TradingDatasMinuteMarketDataPort,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG = "fixture-minute-catalog-v1"
DATASET = "fixture.cn.equity.five_minute"
FIELDS = [
    "ts_code",
    "bar_time",
    "freq",
    "open",
    "high",
    "low",
    "close",
    "vol",
    "amount",
    "pre_close",
    "suspended",
]


def _catalog_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "dataset_id": DATASET,
        "schema_major": 1,
        "default_fields": list(FIELDS),
        "default_order": ["ts_code:asc", "bar_time:asc"],
        "fields": [
            {
                "name": value,
                "selectable": True,
                "filterable": True,
                "sortable": True,
                "operators": ["eq", "in", "gte", "lte", "between"],
            }
            for value in FIELDS
        ],
        "filter_operators": {
            value: ["eq", "in", "gte", "lte", "between"] for value in FIELDS
        },
        "limits": {"max_page_size": 2, "max_lookback_days": 30},
        "availability": {"activation_states": ["active"]},
    }
    row.update(overrides)
    return row


def _catalog_payload(row: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG,
        "request_id": "catalog-request",
        "data": [copy.deepcopy(row or _catalog_row())],
    }


def test_profile_accepts_catalog_filter_only_field() -> None:
    row = _catalog_row()
    row["default_fields"] = [
        field_name for field_name in row["default_fields"] if field_name != "freq"
    ]
    transport = _Transport(catalog_row=row)
    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="http://127.0.0.1:18082",
            expected_catalog_version=CATALOG,
            dataset_ids=frozenset({DATASET}),
            access_policy_id="minute-test",
            max_limit=2,
        ),
        transport=transport,
    )

    profile = MinuteDatasetProfile.from_catalog(
        client.get_catalog(),
        expected_catalog_version=CATALOG,
        dataset_id=DATASET,
        identity_fields=("ts_code", "bar_time"),
        symbol_field="ts_code",
        timestamp_field="bar_time",
        open_field="open",
        high_field="high",
        low_field="low",
        close_field="close",
        volume_field="vol",
        amount_field="amount",
        previous_close_field="pre_close",
        suspension_field="suspended",
        frequency_field=None,
        frequency_value=None,
        timestamp_format="%Y%m%d %H:%M:%S",
        timestamp_semantics=MinuteTimestampSemantics.BAR_END,
        volume_multiplier_to_shares=1.0,
        amount_multiplier_to_cny=1.0,
        price_adjustment="raw_unadjusted",
        max_pages=2,
        max_rows=4,
        page_limit=2,
    )

    assert dict(profile.filter_operators)["freq"] == (
        "eq",
        "in",
        "gte",
        "lte",
        "between",
    )
    assert "freq" not in profile.default_fields


def _metadata(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid"},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "providers": ["fixture"],
            "transport_service": "fixture",
        },
        "receipt_id": "receipt-minute-1",
        "data_through": "2026-07-27T09:40:00+08:00",
        "observed_at": "2026-07-27T09:40:20+08:00",
        "reasons": [],
    }
    value.update(overrides)
    return value


def _row(
    symbol: str,
    bar_time: str,
    *,
    open_price: float = 10.0,
    high: float = 10.2,
    low: float = 9.9,
    close: float = 10.1,
    volume: int = 10_000,
    suspended: bool = False,
) -> dict[str, Any]:
    return {
        "ts_code": symbol,
        "bar_time": bar_time,
        "freq": "5min",
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "vol": volume,
        "amount": volume * close,
        "pre_close": 10.0,
        "suspended": suspended,
    }


def _query_payload(
    *,
    request_id: str,
    rows: list[dict[str, Any]],
    next_cursor: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG,
        "request_id": request_id,
        "dataset_id": DATASET,
        "data": copy.deepcopy(rows),
        "next_cursor": next_cursor,
        "metadata": copy.deepcopy(metadata or _metadata()),
    }


class _Transport:
    def __init__(
        self,
        *,
        first_rows: list[dict[str, Any]] | None = None,
        second_rows: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        catalog_row: dict[str, Any] | None = None,
        replay_change: bool = False,
        cursor_cycle: bool = False,
        query_status: int = 200,
    ) -> None:
        self.first_rows = first_rows or [_row("600000.SH", "20260727 09:40:00")]
        self.second_rows = second_rows or [_row("000001.SZ", "20260727 09:40:00")]
        self.metadata = metadata or _metadata()
        self.catalog_row = catalog_row or _catalog_row()
        self.replay_change = replay_change
        self.cursor_cycle = cursor_cycle
        self.query_status = query_status
        self.query_count = 0
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            return HTTPResponse(200, _catalog_payload(self.catalog_row))
        body = kwargs["json_body"]
        assert body is not None
        if self.query_status != 200:
            return HTTPResponse(self.query_status, {"error": "fixture-auth-failure"})
        cursor = body.get("cursor")
        self.query_count += 1
        run = (self.query_count - 1) // 2
        if cursor is None:
            rows = copy.deepcopy(self.first_rows)
            if self.replay_change and run == 1:
                rows[0]["close"] = 10.15
            return HTTPResponse(
                200,
                _query_payload(
                    request_id=f"query-{self.query_count}",
                    rows=rows,
                    next_cursor="cursor-1",
                    metadata=self.metadata,
                ),
            )
        assert cursor == "cursor-1"
        return HTTPResponse(
            200,
            _query_payload(
                request_id=f"query-{self.query_count}",
                rows=self.second_rows,
                next_cursor="cursor-1" if self.cursor_cycle else None,
                metadata=self.metadata,
            ),
        )


def _client(transport: _Transport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://minute.fixture.invalid",
            expected_catalog_version=CATALOG,
            dataset_ids=frozenset({DATASET}),
            access_policy_id="fixture-read",
            max_limit=2,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _profile(
    client: SharedSignalsV1Client,
    **overrides: Any,
) -> MinuteDatasetProfile:
    values: dict[str, Any] = {
        "expected_catalog_version": CATALOG,
        "dataset_id": DATASET,
        "identity_fields": ("ts_code", "bar_time"),
        "symbol_field": "ts_code",
        "timestamp_field": "bar_time",
        "open_field": "open",
        "high_field": "high",
        "low_field": "low",
        "close_field": "close",
        "volume_field": "vol",
        "amount_field": "amount",
        "previous_close_field": "pre_close",
        "suspension_field": "suspended",
        "frequency_field": "freq",
        "frequency_value": "5min",
        "timestamp_format": "%Y%m%d %H:%M:%S",
        "timestamp_semantics": MinuteTimestampSemantics.BAR_END,
        "volume_multiplier_to_shares": 1.0,
        "amount_multiplier_to_cny": 1.0,
        "price_adjustment": "raw_unadjusted",
        "max_pages": 2,
        "max_rows": 4,
        "page_limit": 2,
    }
    values.update(overrides)
    return MinuteDatasetProfile.from_catalog(client.get_catalog(), **values)


def _load(
    transport: _Transport,
    *,
    decision_time: str = "2026-07-27T09:40:25+08:00",
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
) -> tuple[Any, MinuteEvidenceAuditLedger]:
    client = _client(transport)
    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()
    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters={"bar_time": {"gte": "20260727 09:35:00"}},
        decision_time=datetime.fromisoformat(decision_time),
        trading_dates=frozenset({date(2026, 7, 27)}),
        audit_ledger=audit,
        evidence_use=evidence_use,
    )
    return snapshot, audit


def test_profile_is_frozen_from_active_catalog_and_query_uses_only_fixed_routes() -> (
    None
):
    transport = _Transport()
    snapshot, audit = _load(transport)

    assert snapshot.row_count == 2
    assert snapshot.page_count == 2
    assert snapshot.same_observation is True
    assert [bar.symbol for bar in snapshot.bars] == ["600000.SH", "000001.SZ"]
    assert audit.records() == ()
    assert {call["url"].split(".invalid")[-1] for call in transport.calls} == {
        "/v1/catalog",
        "/v1/query",
    }
    query_bodies = [
        call["json_body"] for call in transport.calls if call["method"] == "POST"
    ]
    assert all(body["schema_major"] == 1 for body in query_bodies)
    assert all("as_of" not in body for body in query_bodies)
    assert all("/tushare" not in call["url"] for call in transport.calls)
    assert all("source_status" not in call["url"] for call in transport.calls)


@pytest.mark.parametrize(
    ("catalog_row", "reason"),
    [
        (
            _catalog_row(availability={"activation_states": ["paused"]}),
            "minute_dataset_not_active",
        ),
        (
            _catalog_row(default_fields=[field for field in FIELDS if field != "vol"]),
            "minute_profile_required_fields_missing",
        ),
        (
            _catalog_row(limits={"max_page_size": 1, "max_lookback_days": 30}),
            "minute_page_limit_exceeds_catalog",
        ),
    ],
)
def test_profile_fails_closed_on_catalog_contract_gaps(
    catalog_row: dict[str, Any], reason: str
) -> None:
    client = _client(_Transport(catalog_row=catalog_row))
    with pytest.raises(MinuteDataContractError, match=reason):
        _profile(client)


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata(state="stale", degraded=True), "minute_metadata_not_ready"),
        (
            _metadata(freshness={"state": "stale", "stale": True}),
            "minute_metadata_not_fresh",
        ),
        (_metadata(quality={"state": "degraded"}), "minute_metadata_quality_invalid"),
        (
            _metadata(lineage={"complete": False}),
            "minute_metadata_lineage_incomplete",
        ),
        (
            _metadata(
                lineage={
                    "complete": True,
                    "provider_neutral": True,
                    "provider": "fixture",
                    "transport_service": "fixture",
                }
            ),
            "minute_metadata_lineage_incomplete",
        ),
    ],
)
def test_metadata_gate_rejects_and_records_audit_only(
    metadata: dict[str, Any], reason: str
) -> None:
    client = _client(_Transport(metadata=metadata))
    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match=reason):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
        )
    assert [record.reason_code for record in audit.records()] == [reason]
    assert audit.records()[0].feature_eligible is False
    assert audit.records()[0].execution_eligible is False


@pytest.mark.parametrize(
    ("row", "decision_time", "trading_dates", "reason"),
    [
        (
            _row("300001.SZ", "20260727 09:35:00"),
            "2026-07-27T09:40:25+08:00",
            frozenset({date(2026, 7, 27)}),
            "minute_symbol_not_mainboard_tradable",
        ),
        (
            _row("600000.SH", "20260727 12:00:00"),
            "2026-07-27T12:00:25+08:00",
            frozenset({date(2026, 7, 27)}),
            "minute_bar_outside_trading_session",
        ),
        (
            _row("600000.SH", "20260726 09:35:00"),
            "2026-07-26T09:35:25+08:00",
            frozenset({date(2026, 7, 26)}),
            "minute_weekend_bar_forbidden",
        ),
        (
            _row("600000.SH", "20260727 09:35:00", high=9.9),
            "2026-07-27T09:40:25+08:00",
            frozenset({date(2026, 7, 27)}),
            "minute_ohlc_relationship_invalid",
        ),
        (
            _row("600000.SH", "20260727 09:35:00", volume=0),
            "2026-07-27T09:40:25+08:00",
            frozenset({date(2026, 7, 27)}),
            "minute_zero_volume_not_tradable",
        ),
        (
            _row("600000.SH", "20260727 09:35:00", suspended=True),
            "2026-07-27T09:40:25+08:00",
            frozenset({date(2026, 7, 27)}),
            "minute_suspended_instrument",
        ),
        (
            _row("600000.SH", "20260727 09:35:00"),
            "2026-07-27T09:40:25+08:00",
            frozenset(),
            "minute_trade_date_not_calendar_eligible",
        ),
    ],
)
def test_row_gate_rejects_market_time_quality_and_universe_failures(
    row: dict[str, Any],
    decision_time: str,
    trading_dates: frozenset[date],
    reason: str,
) -> None:
    metadata = _metadata(
        data_through=row["bar_time"][:8] + "T" + row["bar_time"][9:] + "+08:00",
        observed_at=row["bar_time"][:8] + "T" + row["bar_time"][9:] + ".020000+08:00",
    )
    # Convert YYYYMMDD to ISO for the strict envelope timestamp.
    day = row["bar_time"][:8]
    iso_prefix = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    metadata["data_through"] = iso_prefix + "T" + row["bar_time"][9:] + "+08:00"
    metadata["observed_at"] = iso_prefix + "T" + row["bar_time"][9:] + ".020000+08:00"
    client = _client(
        _Transport(
            first_rows=[row],
            second_rows=[_row("000001.SZ", row["bar_time"])],
            metadata=metadata,
        )
    )
    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match=reason):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat(decision_time),
            trading_dates=trading_dates,
            audit_ledger=audit,
        )
    assert audit.records()[0].reason_code == reason


def test_latency_future_and_replay_mismatch_fail_closed() -> None:
    late = _metadata(observed_at="2026-07-27T09:40:31+08:00")
    client = _client(_Transport(metadata=late))
    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="latency"):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:32+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
        )
    assert audit.records()[0].reason_code == "minute_evidence_latency_exceeded"


def test_delayed_paper_accepts_ten_minute_lag_without_execution_authority() -> None:
    delayed = _metadata(
        observed_at="2026-07-27T09:50:00+08:00",
        data_through="2026-07-27T09:50:00+08:00",
    )
    snapshot, audit = _load(
        _Transport(metadata=delayed),
        decision_time="2026-07-27T09:50:01+08:00",
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )
    assert audit.records() == ()
    assert all(
        bar.evidence_use is MinuteEvidenceUse.DELAYED_PAPER for bar in snapshot.bars
    )
    assert all(bar.delayed_paper_eligible is True for bar in snapshot.bars)
    assert all(bar.execution_latency_eligible is False for bar in snapshot.bars)

    too_late = _metadata(
        observed_at="2026-07-27T09:52:01+08:00",
        data_through="2026-07-27T09:52:01+08:00",
    )
    client = _client(_Transport(metadata=too_late))
    profile = _profile(client)
    late_audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="latency"):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:52:02+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=late_audit,
            evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
        )
    assert late_audit.records()[0].reason_code == "minute_evidence_latency_exceeded"

    changed_client = _client(_Transport(replay_change=True))
    changed_profile = _profile(changed_client)
    changed_audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="same_observation"):
        TradingDatasMinuteMarketDataPort(changed_client).load_snapshot(
            profile=changed_profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=changed_audit,
        )


def test_cursor_cycle_and_duplicate_cross_page_identity_are_rejected() -> None:
    cycle_client = _client(_Transport(cursor_cycle=True))
    cycle_profile = _profile(cycle_client)
    cycle_audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="cursor_cycle"):
        TradingDatasMinuteMarketDataPort(cycle_client).load_snapshot(
            profile=cycle_profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=cycle_audit,
        )
    assert cycle_audit.records()[0].reason_code == "pagination_cursor_cycle"

    duplicate = _row("600000.SH", "20260727 09:35:00")
    duplicate_client = _client(
        _Transport(first_rows=[duplicate], second_rows=[copy.deepcopy(duplicate)])
    )
    duplicate_profile = _profile(duplicate_client)
    duplicate_audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="duplicate_row_identity"):
        TradingDatasMinuteMarketDataPort(duplicate_client).load_snapshot(
            profile=duplicate_profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=duplicate_audit,
        )
    assert (
        duplicate_audit.records()[0].reason_code == "pagination_duplicate_row_identity"
    )


def test_auth_failure_is_audited_and_has_no_fallback() -> None:
    profile_client = _client(_Transport())
    profile = _profile(profile_client)
    failing_transport = _Transport(query_status=401)
    audit = MinuteEvidenceAuditLedger()
    with pytest.raises(
        MinuteDataContractError, match="minute_tradingdatas_request_failed"
    ):
        TradingDatasMinuteMarketDataPort(_client(failing_transport)).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
        )
    assert audit.records()[0].reason_code == "minute_tradingdatas_request_failed"
    assert {call["url"].split(".invalid")[-1] for call in failing_transport.calls} == {
        "/v1/catalog",
        "/v1/query",
    }


def test_units_price_adjustment_and_filters_are_explicit_fail_closed_contracts() -> (
    None
):
    client = _client(_Transport())
    with pytest.raises(MinuteDataContractError, match="raw_unadjusted"):
        _profile(client, price_adjustment="forward_adjusted")
    with pytest.raises(MinuteDataContractError, match="multiplier"):
        _profile(client, volume_multiplier_to_shares=0)

    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="filter_not_catalog"):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={"provider_private_filter": {"eq": "x"}},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
        )


def test_provider_native_quicksync_shape_binds_daily_reference_evidence() -> None:
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
    catalog_row = _catalog_row(
        default_fields=fields,
        default_order=["ts_code:asc", "time:asc"],
        fields=[
            {
                "name": field,
                "selectable": True,
                "filterable": True,
                "sortable": True,
                "operators": ["eq", "in", "gte", "lte", "between"],
            }
            for field in fields
        ],
        filter_operators={
            field: ["eq", "in", "gte", "lte", "between"] for field in fields
        },
    )
    first = {
        "ts_code": "600000.SH",
        "time": "2026-07-27 09:40:00",
        "open": 10.0,
        "high": 10.2,
        "low": 9.9,
        "close": 10.1,
        "vol": 10_000,
        "amount": 101_000,
    }
    second = {
        "ts_code": "000001.SZ",
        "time": "2026-07-27 09:40:00",
        "open": 11.0,
        "high": 11.2,
        "low": 10.9,
        "close": 11.1,
        "vol": 12_000,
        "amount": 133_200,
    }
    client = _client(
        _Transport(
            first_rows=[first],
            second_rows=[second],
            catalog_row=catalog_row,
        )
    )
    profile = _profile(
        client,
        identity_fields=("ts_code", "time"),
        timestamp_field="time",
        previous_close_field=None,
        suspension_field=None,
        frequency_field=None,
        frequency_value=None,
        timestamp_format="%Y-%m-%d %H:%M:%S",
    )
    audit = MinuteEvidenceAuditLedger()
    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters={"time": {"gte": "2026-07-27 09:35:00"}},
        decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
        trading_dates=frozenset({date(2026, 7, 27)}),
        audit_ledger=audit,
        reference_facts={
            "600000.SH": MinuteReferenceFact(
                symbol="600000.SH",
                trade_date=date(2026, 7, 27),
                previous_close_cny=9.98,
                suspended=False,
                evidence_sha256="a" * 64,
            ),
            "000001.SZ": MinuteReferenceFact(
                symbol="000001.SZ",
                trade_date=date(2026, 7, 27),
                previous_close_cny=10.95,
                suspended=False,
                evidence_sha256="b" * 64,
            ),
        },
    )

    assert [bar.symbol for bar in snapshot.bars] == ["600000.SH", "000001.SZ"]
    assert [bar.previous_close_cny for bar in snapshot.bars] == [9.98, 10.95]
    assert [bar.reference_evidence_sha256 for bar in snapshot.bars] == [
        "a" * 64,
        "b" * 64,
    ]
    assert (
        replace(snapshot.bars[0], reference_evidence_sha256="c" * 64).sha256
        != snapshot.bars[0].sha256
    )
    assert audit.records() == ()


def test_provider_native_quicksync_shape_requires_matching_reference_fact() -> None:
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
    catalog_row = _catalog_row(
        default_fields=fields,
        default_order=["ts_code:asc", "time:asc"],
        fields=[
            {
                "name": field,
                "selectable": True,
                "filterable": True,
                "sortable": True,
                "operators": ["eq", "in", "gte", "lte", "between"],
            }
            for field in fields
        ],
        filter_operators={
            field: ["eq", "in", "gte", "lte", "between"] for field in fields
        },
    )
    rows = [
        {
            "ts_code": symbol,
            "time": "2026-07-27 09:40:00",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "vol": 10_000,
            "amount": 101_000,
        }
        for symbol in ("600000.SH", "000001.SZ")
    ]
    client = _client(
        _Transport(
            first_rows=[rows[0]],
            second_rows=[rows[1]],
            catalog_row=catalog_row,
        )
    )
    profile = _profile(
        client,
        identity_fields=("ts_code", "time"),
        timestamp_field="time",
        previous_close_field=None,
        suspension_field=None,
        frequency_field=None,
        frequency_value=None,
        timestamp_format="%Y-%m-%d %H:%M:%S",
    )
    audit = MinuteEvidenceAuditLedger()

    with pytest.raises(MinuteDataContractError, match="minute_reference_fact_missing"):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
            reference_facts={
                "600000.SH": MinuteReferenceFact(
                    symbol="600000.SH",
                    trade_date=date(2026, 7, 27),
                    previous_close_cny=9.98,
                    suspended=False,
                    evidence_sha256="a" * 64,
                ),
            },
        )

    assert audit.records()[0].reason_code == "minute_reference_fact_missing"
