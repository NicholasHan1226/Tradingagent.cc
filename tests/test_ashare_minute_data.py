from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
import threading
import time

import pytest

import Ashare.minute_data as minute_data_module
from Ashare.minute_data import (
    MinuteDataContractError,
    MinuteDatasetProfile,
    MinuteEvidenceAuditLedger,
    MinuteEvidenceUse,
    MinuteReferenceFact,
    MinuteTimestampSemantics,
    TradingDatasMinuteMarketDataPort,
)
from Ashare.rt_min_daily_pit import RtMinExactSlotProofEnvelope
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    HTTPStatusError,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_transport import TradingDatasAuthenticationError
from shared.governance.evidence_readiness import dataset_contract_fingerprint


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
        "identity_fields": ["ts_code", "bar_time"],
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


def _catalog_payload(
    row: dict[str, Any] | None = None,
    *,
    catalog_version: str = CATALOG,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": catalog_version,
        "request_id": "catalog-request",
        "data": copy.deepcopy(rows if rows is not None else [row or _catalog_row()]),
    }


def test_profile_accepts_only_canonical_default_field_filters() -> None:
    row = _catalog_row()
    row["default_fields"] = [
        field_name for field_name in row["default_fields"] if field_name != "freq"
    ]
    row["filter_operators"].pop("freq")
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

    assert "freq" not in profile.default_fields
    assert "freq" not in dict(profile.filter_operators)


def test_dataset_fingerprint_is_canonical_while_global_version_is_evidence() -> None:
    row = _catalog_row()
    transport = _Transport(
        catalog_versions=(
            "catalog-at-profile-build",
            "catalog-with-unrelated-addition",
        ),
        query_catalog_version="catalog-with-unrelated-addition",
        catalog_rows_by_read=(
            [
                row,
                {
                    "dataset_id": "unrelated.fixture.dataset",
                    "schema_major": 1,
                    "default_fields": ["id"],
                    "default_order": ["id:asc"],
                    "identity_fields": ["id"],
                    "filter_operators": {"id": ["eq"]},
                    "limits": {"max_page_size": 1},
                    "availability": {"activation_states": ["active"]},
                },
            ],
        ),
    )
    client = _client(transport)
    profile = _profile(
        client,
        expected_catalog_version="previous-global-catalog-version",
        expected_dataset_contract_fingerprint=dataset_contract_fingerprint(row),
    )

    assert profile.dataset_contract_fingerprint == dataset_contract_fingerprint(row)
    assert profile.expected_catalog_version == "previous-global-catalog-version"
    assert profile.observed_catalog_version == "catalog-at-profile-build"
    assert profile.catalog_version_drift is True
    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters={},
        decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
        trading_dates=frozenset({date(2026, 7, 27)}),
        audit_ledger=MinuteEvidenceAuditLedger(),
    )
    assert snapshot.row_count == 2
    assert snapshot.observed_catalog_version == "catalog-with-unrelated-addition"
    assert snapshot.catalog_version_drift is True


def test_minute_port_proof_opt_in_is_forwarded_and_validator_blocks() -> None:
    transport = _Transport()
    client = _client(transport)
    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()

    with pytest.raises(MinuteDataContractError, match="minute_exact_slot_receipt_proof_failed"):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
            include_receipt_proofs=True,
            envelope_validator=lambda _envelope: (_ for _ in ()).throw(
                ValueError("proof rejected")
            ),
        )
    query_calls = [call for call in transport.calls if call["method"] == "POST"]
    assert query_calls
    assert all(call["json_body"]["include_receipt_proofs"] is True for call in query_calls)


def test_minute_port_fanouts_large_symbol_filter_into_replayed_v1_shards() -> None:
    symbols = tuple(f"{index + 1:06d}.SZ" for index in range(101))
    catalog_row = _catalog_row()
    catalog_row["limits"] = {"max_page_size": 100, "max_lookback_days": 30}

    class ShardTransport(_Transport):
        def __init__(self) -> None:
            super().__init__(catalog_row=catalog_row)
            self.query_bodies: list[dict[str, Any]] = []

        def __call__(self, **kwargs: Any) -> HTTPResponse:
            if kwargs["method"] == "GET":
                return super().__call__(**kwargs)
            body = kwargs["json_body"]
            assert body is not None
            self.query_bodies.append(copy.deepcopy(body))
            requested = tuple(body["filters"]["ts_code"]["in"])
            rows = [_row(symbol, "20260727 09:40:00") for symbol in requested]
            return HTTPResponse(
                200,
                _query_payload(
                    request_id=f"shard-query-{len(self.query_bodies)}",
                    rows=rows,
                    next_cursor=None,
                ),
            )

    transport = ShardTransport()
    client = _client(transport, max_limit=100)
    profile = _profile(
        client,
        max_pages=2,
        max_rows=101,
        page_limit=100,
    )
    references = {
        symbol: MinuteReferenceFact(
            symbol=symbol,
            trade_date=date(2026, 7, 27),
            previous_close_cny=10.0,
            suspended=False,
            evidence_sha256="a" * 64,
        )
        for symbol in symbols
    }

    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters={
            "ts_code": {"in": list(symbols)},
            "bar_time": {"eq": "20260727 09:40:00"},
        },
        decision_time=datetime.fromisoformat("2026-07-27T09:45:25+08:00"),
        trading_dates=frozenset({date(2026, 7, 27)}),
        audit_ledger=MinuteEvidenceAuditLedger(),
        reference_facts=references,
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )

    assert snapshot.row_count == 101
    assert snapshot.page_count == 2
    assert len(transport.query_bodies) == 4
    assert all(
        len(body["filters"]["ts_code"]["in"]) <= 100
        for body in transport.query_bodies
    )


def test_minute_port_uses_single_flight_client_per_parallel_worker() -> None:
    symbols = tuple(f"{index + 1:06d}.SZ" for index in range(101))
    catalog_row = _catalog_row(
        limits={"max_page_size": 100, "max_lookback_days": 30}
    )

    class SingleFlightTransport(_Transport):
        def __init__(self) -> None:
            super().__init__(catalog_row=catalog_row)
            self._active = threading.Lock()
            self.concurrent_rejections = 0

        def __call__(self, **kwargs: Any) -> HTTPResponse:
            if not self._active.acquire(blocking=False):
                self.concurrent_rejections += 1
                raise TradingDatasAuthenticationError(
                    "fixture transport rejects concurrent requests"
                )
            try:
                time.sleep(0.01)
                if kwargs["method"] == "GET":
                    return super().__call__(**kwargs)
                body = kwargs["json_body"]
                assert body is not None
                requested = tuple(body["filters"]["ts_code"]["in"])
                rows = [_row(symbol, "20260727 09:40:00") for symbol in requested]
                self.query_count += 1
                return HTTPResponse(
                    200,
                    _query_payload(
                        request_id=f"shard-query-{self.query_count}",
                        rows=rows,
                        next_cursor=None,
                    ),
                )
            finally:
                self._active.release()

    primary_transport = SingleFlightTransport()
    primary_transport.catalog_row = catalog_row
    client = _client(primary_transport, max_limit=100)
    profile = _profile(client, max_pages=2, max_rows=101, page_limit=100)
    worker_transports: list[SingleFlightTransport] = []

    def shard_client_factory() -> SharedSignalsV1Client:
        transport = SingleFlightTransport()
        transport.catalog_row = catalog_row
        worker_transports.append(transport)
        return _client(transport, max_limit=100)

    references = {
        symbol: MinuteReferenceFact(
            symbol=symbol,
            trade_date=date(2026, 7, 27),
            previous_close_cny=10.0,
            suspended=False,
            evidence_sha256="a" * 64,
        )
        for symbol in symbols
    }

    snapshot = TradingDatasMinuteMarketDataPort(
        client,
        shard_client_factory=shard_client_factory,
    ).load_snapshot(
        profile=profile,
        filters={
            "ts_code": {"in": list(symbols)},
            "bar_time": {"eq": "20260727 09:40:00"},
        },
        decision_time=datetime.fromisoformat("2026-07-27T09:45:25+08:00"),
        trading_dates=frozenset({date(2026, 7, 27)}),
        audit_ledger=MinuteEvidenceAuditLedger(),
        reference_facts=references,
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )

    assert snapshot.row_count == 101
    assert len(worker_transports) >= 1
    assert sum(transport.query_count for transport in worker_transports) == 4
    assert primary_transport.concurrent_rejections == 0
    assert all(transport.concurrent_rejections == 0 for transport in worker_transports)


def test_minute_port_retains_successful_shards_when_one_request_fails() -> None:
    symbols = tuple(f"{index + 1:06d}.SZ" for index in range(101))
    catalog_row = _catalog_row()
    catalog_row["limits"] = {"max_page_size": 100, "max_lookback_days": 30}

    class PartialShardTransport(_Transport):
        def __call__(self, **kwargs: Any) -> HTTPResponse:
            if kwargs["method"] == "GET":
                return super().__call__(**kwargs)
            body = kwargs["json_body"]
            assert body is not None
            requested = tuple(body["filters"]["ts_code"]["in"])
            if requested and requested[0] == "000101.SZ":
                return HTTPResponse(
                    503,
                    {"error": {"code": "service_unavailable"}},
                )
            rows = [_row(symbol, "20260727 09:40:00") for symbol in requested]
            return HTTPResponse(
                200,
                _query_payload(
                    request_id=f"partial-shard-{len(self.calls)}",
                    rows=rows,
                    next_cursor=None,
                ),
            )

    transport = PartialShardTransport(catalog_row=catalog_row)
    client = _client(transport, max_limit=100)
    profile = _profile(client, max_pages=2, max_rows=101, page_limit=100)
    audit = MinuteEvidenceAuditLedger()

    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters={
            "ts_code": {"in": list(symbols)},
            "bar_time": {"eq": "20260727 09:40:00"},
        },
        decision_time=datetime.fromisoformat("2026-07-27T09:45:25+08:00"),
        trading_dates=frozenset({date(2026, 7, 27)}),
        audit_ledger=audit,
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )

    assert snapshot.row_count == 100
    assert snapshot.same_observation is True
    assert snapshot.fanout_failures == (
        {
            "shard_index": 1,
            "symbol_count": 1,
            "reason_code": "minute_tradingdatas_request_failed",
            "failure_stage": "query_request",
            "failure_class": "HTTPStatusError",
        },
    )
    assert audit.records() == ()


def test_minute_port_isolates_raw_transport_error_to_one_shard() -> None:
    symbols = tuple(f"{index + 1:06d}.SZ" for index in range(101))
    catalog_row = _catalog_row()
    catalog_row["limits"] = {"max_page_size": 100, "max_lookback_days": 30}

    class RawTransportError(_Transport):
        def __call__(self, **kwargs: Any) -> HTTPResponse:
            if kwargs["method"] == "GET":
                return super().__call__(**kwargs)
            body = kwargs["json_body"]
            assert body is not None
            requested = tuple(body["filters"]["ts_code"]["in"])
            if requested and requested[0] == "000101.SZ":
                raise OSError("connection reset by peer")
            rows = [_row(symbol, "20260727 09:40:00") for symbol in requested]
            return HTTPResponse(
                200,
                _query_payload(
                    request_id=f"raw-error-shard-{len(self.calls)}",
                    rows=rows,
                    next_cursor=None,
                ),
            )

    client = _client(RawTransportError(catalog_row=catalog_row), max_limit=100)
    profile = _profile(client, max_pages=2, max_rows=101, page_limit=100)
    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters={
            "ts_code": {"in": list(symbols)},
            "bar_time": {"eq": "20260727 09:40:00"},
        },
        decision_time=datetime.fromisoformat("2026-07-27T09:45:25+08:00"),
        trading_dates=frozenset({date(2026, 7, 27)}),
        audit_ledger=MinuteEvidenceAuditLedger(),
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )

    assert snapshot.row_count == 100
    assert snapshot.fanout_failures == (
        {
            "shard_index": 1,
            "symbol_count": 1,
            "reason_code": "minute_tradingdatas_request_failed",
            "failure_stage": "transport",
            "failure_class": "OSError",
        },
    )


@pytest.mark.parametrize(
    "catalog_identity,consumer_identity,reason",
    [
        ([], ("ts_code", "bar_time"), "minute_catalog_identity_fields_invalid"),
        (
            ["bar_time", "ts_code"],
            ("ts_code", "bar_time"),
            "minute_catalog_identity_mismatch",
        ),
    ],
)
def test_catalog_identity_must_exactly_match_consumer_identity(
    catalog_identity: list[str],
    consumer_identity: tuple[str, ...],
    reason: str,
) -> None:
    client = _client(
        _Transport(catalog_row=_catalog_row(identity_fields=catalog_identity))
    )
    with pytest.raises(MinuteDataContractError, match=reason):
        _profile(client, identity_fields=consumer_identity)


@pytest.mark.parametrize(
    "catalog_rows",
    [
        [],
        [_catalog_row(), _catalog_row()],
    ],
)
def test_runtime_catalog_missing_or_duplicate_target_row_fails_before_query(
    catalog_rows: list[dict[str, Any]],
) -> None:
    transport = _Transport(
        catalog_rows_by_read=([_catalog_row()], catalog_rows),
    )
    client = _client(transport)
    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()

    with pytest.raises(
        MinuteDataContractError, match="minute_tradingdatas_request_failed"
    ):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
        )
    assert transport.query_count == 0
    assert audit.records()[0].reason_code == "minute_tradingdatas_request_failed"


def test_runtime_target_contract_or_query_catalog_mutation_fails_closed() -> None:
    changed = _catalog_row(default_order=["bar_time:asc", "ts_code:asc"])
    transport = _Transport(catalog_rows_by_read=([_catalog_row()], [changed]))
    client = _client(transport)
    profile = _profile(client)
    audit = MinuteEvidenceAuditLedger()

    with pytest.raises(MinuteDataContractError, match="minute_dataset_contract_drift"):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=audit,
        )
    assert transport.query_count == 0

    mutation = _Transport(query_catalog_version="catalog-after-query-mutation")
    mutation_client = _client(mutation)
    mutation_profile = _profile(mutation_client)
    with pytest.raises(
        MinuteDataContractError, match="minute_tradingdatas_request_failed"
    ):
        TradingDatasMinuteMarketDataPort(mutation_client).load_snapshot(
            profile=mutation_profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=MinuteEvidenceAuditLedger(),
        )
    assert mutation.query_count == 1


def test_each_canonical_target_contract_field_drift_fails_before_query() -> None:
    changed_rows = (
        _catalog_row(dataset_id="fixture.cn.equity.other"),
        _catalog_row(schema_major=2),
        _catalog_row(default_fields=list(reversed(FIELDS))),
        _catalog_row(
            filter_operators={
                **_catalog_row()["filter_operators"],
                "close": ["eq"],
            }
        ),
        _catalog_row(default_order=["bar_time:asc", "ts_code:asc"]),
        _catalog_row(limits={"max_page_size": 2, "max_lookback_days": 31}),
        _catalog_row(identity_fields=["bar_time", "ts_code"]),
    )
    for changed in changed_rows:
        transport = _Transport(catalog_rows_by_read=([_catalog_row()], [changed]))
        client = _client(transport)
        profile = _profile(client)
        with pytest.raises(MinuteDataContractError):
            TradingDatasMinuteMarketDataPort(client).load_snapshot(
                profile=profile,
                filters={},
                decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
                trading_dates=frozenset({date(2026, 7, 27)}),
                audit_ledger=MinuteEvidenceAuditLedger(),
            )
        assert transport.query_count == 0


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
    catalog_version: str = CATALOG,
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": catalog_version,
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
        catalog_versions: tuple[str, ...] = (CATALOG,),
        catalog_rows_by_read: tuple[list[dict[str, Any]], ...] | None = None,
        query_catalog_version: str | None = None,
    ) -> None:
        self.first_rows = first_rows or [_row("600000.SH", "20260727 09:40:00")]
        self.second_rows = second_rows or [_row("000001.SZ", "20260727 09:40:00")]
        self.metadata = metadata or _metadata()
        self.catalog_row = catalog_row or _catalog_row()
        self.replay_change = replay_change
        self.cursor_cycle = cursor_cycle
        self.query_status = query_status
        self.catalog_versions = catalog_versions
        self.catalog_rows_by_read = catalog_rows_by_read
        self.query_catalog_version = query_catalog_version or CATALOG
        self.catalog_count = 0
        self.query_count = 0
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            read_index = self.catalog_count
            index = min(read_index, len(self.catalog_versions) - 1)
            self.catalog_count += 1
            rows = (
                self.catalog_rows_by_read[
                    min(read_index, len(self.catalog_rows_by_read) - 1)
                ]
                if self.catalog_rows_by_read is not None
                else [self.catalog_row]
            )
            return HTTPResponse(
                200,
                _catalog_payload(
                    catalog_version=self.catalog_versions[index], rows=rows
                ),
            )
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
                    catalog_version=self.query_catalog_version,
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
                catalog_version=self.query_catalog_version,
            ),
        )


def _client(
    transport: _Transport,
    *,
    max_limit: int = 2,
) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://minute.fixture.invalid",
            expected_catalog_version=CATALOG,
            dataset_ids=frozenset({DATASET}),
            access_policy_id="fixture-read",
            catalog_version_policy="evidence_only",
            max_limit=max_limit,
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
    include_receipt_proofs: bool = False,
    envelope_validator: Any = None,
    profile_overrides: dict[str, Any] | None = None,
    filter_time: str = "20260727 09:35:00",
    trading_date: date = date(2026, 7, 27),
) -> tuple[Any, MinuteEvidenceAuditLedger]:
    client = _client(
        transport,
        max_limit=(profile_overrides or {}).get("page_limit", 2),
    )
    profile = _profile(client, **(profile_overrides or {}))
    audit = MinuteEvidenceAuditLedger()
    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters={"bar_time": {"gte": filter_time}},
        decision_time=datetime.fromisoformat(decision_time),
        trading_dates=frozenset({trading_date}),
        audit_ledger=audit,
        evidence_use=evidence_use,
        include_receipt_proofs=include_receipt_proofs,
        envelope_validator=envelope_validator,
    )
    return snapshot, audit


def test_exact_proof_mapping_uses_selected_row_receipts_not_latest_runtime_metadata() -> None:
    latest = _metadata(
        state="failed",
        degraded=True,
        freshness={"state": "stale", "stale": True},
        quality={"state": "degraded", "valid": False},
        receipt_id="latest-failed",
        data_through="2026-08-13T13:50:00+08:00",
        observed_at="2026-08-13T13:55:30+08:00",
    )
    symbols = tuple(
        [f"{600000 + index:06d}.SH" for index in range(15)]
        + [f"{1 + index:06d}.SZ" for index in range(15)]
    )
    rows = [_row(symbol, "20260813 09:40:00") for symbol in symbols]
    proofs = tuple(
        {
            "page_index": index,
            "row_identity_sha256": "a" * 64,
            "provider": "tushare",
            "execution_id": "exec-0940",
            "config_hash": "b" * 64,
            "receipt_id": f"receipt-0940-{index // 6}",
            "dataset_id": DATASET,
            "status": "success",
            "data_through": "2026-08-13 09:40:00",
            "finished_at": "2026-08-13T09:45:00+08:00",
            "receipt_proof_sha256": "c" * 64,
        }
        for index in range(30)
    )

    validated = RtMinExactSlotProofEnvelope(
        contract_id="tradingagent.ashare.rt_min.exact_slot_proof.v1",
        dataset_id=DATASET,
        requested_slot="2026-08-13 09:40:00",
        decision_as_of="2026-08-13T13:55:30+08:00",
        requested_symbols=symbols,
        accepted_symbols=symbols,
        quality_status="usable",
        rows=tuple(rows),
        row_receipt_proofs=proofs,
        receipt_ids=tuple(proof["receipt_id"] for proof in proofs),
        provider="tushare",
        execution_id="exec-0940",
        config_hash="b" * 64,
        data_through="2026-08-13 09:40:00",
        receipt_lineage=True,
        historical_pit_eligible=False,
        learning_eligible=False,
        promotion_eligible=False,
        execution_authority=False,
        real_trading_enabled=False,
        content_sha256="d" * 64,
    )

    def validator(_envelope: Any) -> RtMinExactSlotProofEnvelope:
        return validated

    catalog_row = _catalog_row(
        limits={"max_page_size": 30, "max_lookback_days": 30}
    )
    snapshot, audit = _load(
        _Transport(
            first_rows=rows[:15],
            second_rows=rows[15:],
            metadata=latest,
            catalog_row=catalog_row,
        ),
        decision_time="2026-08-13T13:50:00+08:00",
        include_receipt_proofs=True,
        envelope_validator=validator,
        evidence_use=MinuteEvidenceUse.HISTORICAL_DISPLAY,
        profile_overrides={"page_limit": 30, "max_rows": 60},
        filter_time="20260813 09:35:00",
        trading_date=date(2026, 8, 13),
    )

    assert audit.records() == ()
    assert len(snapshot.bars) == 30
    assert [bar.receipt_id for bar in snapshot.bars] == [
        f"receipt-0940-{index // 6}" for index in range(30)
    ]
    assert all(
        bar.data_through == datetime.fromisoformat("2026-08-13T09:40:00+08:00")
        for bar in snapshot.bars
    )
    assert all(
        bar.observed_at == datetime.fromisoformat("2026-08-13T09:45:00+08:00")
        for bar in snapshot.bars
    )
    assert all(bar.receipt_id != "latest-failed" for bar in snapshot.bars)

    with pytest.raises(MinuteDataContractError, match="minute_metadata_not_ready"):
        _load(
            _Transport(
                first_rows=rows[:15],
                second_rows=rows[15:],
                metadata=latest,
                catalog_row=catalog_row,
            ),
            decision_time="2026-08-13T13:50:00+08:00",
            profile_overrides={"page_limit": 30, "max_rows": 60},
            filter_time="20260813 09:35:00",
            trading_date=date(2026, 8, 13),
        )


def test_exact_proof_mapping_rejects_first_replay_validator_drift() -> None:
    proofs = tuple(
        {
            "page_index": index,
            "receipt_id": f"receipt-{index}",
            "data_through": "2026-07-27T09:40:00+08:00",
            "finished_at": "2026-07-27T09:40:20+08:00",
        }
        for index in range(2)
    )
    calls = 0

    def validator(_envelope: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            row_receipt_proofs=proofs,
            content_sha256=("a" if calls == 1 else "b") * 64,
        )

    with pytest.raises(MinuteDataContractError, match="exact_slot_receipt_proof_failed"):
        _load(_Transport(), include_receipt_proofs=True, envelope_validator=validator)


def test_exact_proof_mapping_rejects_selected_proof_after_decision() -> None:
    proofs = tuple(
        {
            "page_index": index,
            "receipt_id": f"receipt-{index}",
            "data_through": "2026-07-27T09:40:00+08:00",
            "finished_at": "2026-07-27T09:45:01+08:00",
        }
        for index in range(2)
    )

    def validator(_envelope: Any) -> Any:
        return SimpleNamespace(row_receipt_proofs=proofs, content_sha256="a" * 64)

    with pytest.raises(MinuteDataContractError, match="exact_slot_receipt_proof_failed"):
        _load(
            _Transport(),
            decision_time="2026-07-27T09:45:00+08:00",
            include_receipt_proofs=True,
            envelope_validator=validator,
        )


def test_catalog_http_failure_has_catalog_request_phase_and_bounded_class() -> None:
    good_client = _client(_Transport())
    profile = _profile(good_client)

    class CatalogFailureTransport(_Transport):
        def __call__(self, **kwargs: Any) -> HTTPResponse:
            if kwargs["method"] == "GET":
                return HTTPResponse(503, {"error": "redacted"})
            return super().__call__(**kwargs)

    client = _client(CatalogFailureTransport())
    with pytest.raises(MinuteDataContractError) as caught:
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=MinuteEvidenceAuditLedger(),
        )
    assert caught.value.reason_code == "minute_tradingdatas_request_failed"
    assert caught.value.failure_stage == "catalog_request"
    assert caught.value.failure_class == "HTTPStatusError"


def test_query_http_failure_has_query_request_phase_not_catalog_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(minute_data_module, "_sleep", lambda _seconds: None)
    good_client = _client(_Transport())
    profile = _profile(good_client)
    client = _client(_Transport(query_status=503))
    with pytest.raises(MinuteDataContractError) as caught:
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:40:25+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=MinuteEvidenceAuditLedger(),
        )
    assert caught.value.reason_code == "minute_tradingdatas_request_failed"
    assert caught.value.failure_stage == "query_request"
    assert caught.value.failure_class == "HTTPStatusError"


def test_query_retries_transient_api_failure_but_preserves_replay_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(minute_data_module, "_sleep", lambda _seconds: None)

    class TransientQueryTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self.failures_remaining = 2

        def __call__(self, **kwargs: Any) -> HTTPResponse:
            if kwargs["method"] == "POST" and self.failures_remaining:
                self.failures_remaining -= 1
                return HTTPResponse(503, {"error": "temporary lock contention"})
            return super().__call__(**kwargs)

    snapshot, audit = _load(TransientQueryTransport())

    assert snapshot.row_count == 2
    assert snapshot.same_observation is True
    assert audit.records() == ()


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
            _catalog_row(
                default_fields=[field for field in FIELDS if field != "vol"],
                filter_operators={
                    field: ["eq", "in", "gte", "lte", "between"]
                    for field in FIELDS
                    if field != "vol"
                },
            ),
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
            "2026-07-27T09:35:25+08:00",
            frozenset({date(2026, 7, 27)}),
            "minute_ohlc_relationship_invalid",
        ),
        (
            _row("600000.SH", "20260727 09:35:00", volume=0),
            "2026-07-27T09:35:25+08:00",
            frozenset({date(2026, 7, 27)}),
            "minute_zero_volume_not_tradable",
        ),
        (
            _row("600000.SH", "20260727 09:35:00", suspended=True),
            "2026-07-27T09:35:25+08:00",
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


def test_historical_display_accepts_old_receipt_without_delayed_authority() -> None:
    stale = _metadata(
        state="stale",
        degraded=True,
        freshness={"state": "stale", "stale": True},
        quality={
            "state": "degraded",
            "valid": False,
            "evidence": ["freshness_sla_exceeded"],
        },
        reasons=["freshness_sla_exceeded"],
    )
    snapshot, audit = _load(
        _Transport(metadata=stale),
        decision_time="2026-08-02T09:00:00+08:00",
        evidence_use=MinuteEvidenceUse.HISTORICAL_DISPLAY,
    )

    assert audit.records() == ()
    assert all(
        bar.evidence_use is MinuteEvidenceUse.HISTORICAL_DISPLAY
        for bar in snapshot.bars
    )
    assert all(bar.delayed_paper_eligible is False for bar in snapshot.bars)
    assert all(bar.execution_latency_eligible is False for bar in snapshot.bars)


def test_delayed_paper_accepts_freshness_only_stale_metadata_with_coverage() -> None:
    """The delayed-paper consumer reads an exact completed bar at bar_end plus
    one cadence, so metadata whose only degradation is freshness_sla_exceeded
    is expected; coverage and latency bounds still gate the read downstream."""

    stale = _metadata(
        state="stale",
        degraded=True,
        freshness={"state": "stale", "stale": True},
        quality={
            "state": "degraded",
            "valid": False,
            "evidence": ["freshness_sla_exceeded"],
        },
        reasons=["freshness_sla_exceeded"],
    )
    snapshot, audit = _load(
        _Transport(metadata=stale),
        decision_time="2026-07-27T09:45:30+08:00",
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )

    assert audit.records() == ()
    assert all(
        bar.evidence_use is MinuteEvidenceUse.DELAYED_PAPER
        for bar in snapshot.bars
    )


def test_historical_display_rejects_nonfreshness_degradation() -> None:
    degraded = _metadata(
        state="stale",
        degraded=True,
        freshness={"state": "stale", "stale": True},
        quality={
            "state": "degraded",
            "valid": False,
            "evidence": ["quality_threshold_failed"],
        },
        reasons=["quality_threshold_failed"],
    )

    with pytest.raises(MinuteDataContractError, match="not_displayable"):
        _load(
            _Transport(metadata=degraded),
            decision_time="2026-08-02T09:00:00+08:00",
            evidence_use=MinuteEvidenceUse.HISTORICAL_DISPLAY,
        )


def test_delayed_paper_allows_one_cadence_plus_shared_jitter_only() -> None:
    delayed = _metadata(
        observed_at="2026-07-27T09:47:00+08:00",
        data_through="2026-07-27T09:47:00+08:00",
    )
    snapshot, audit = _load(
        _Transport(metadata=delayed),
        decision_time="2026-07-27T09:47:00+08:00",
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )
    assert audit.records() == ()
    assert all(
        bar.evidence_use is MinuteEvidenceUse.DELAYED_PAPER for bar in snapshot.bars
    )
    assert all(bar.delayed_paper_eligible is True for bar in snapshot.bars)
    assert all(bar.execution_latency_eligible is False for bar in snapshot.bars)

    too_late = _metadata(
        observed_at="2026-07-27T09:47:00+08:00",
        data_through="2026-07-27T09:47:00+08:00",
    )
    client = _client(_Transport(metadata=too_late))
    profile = _profile(client)
    late_audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="latency"):
        TradingDatasMinuteMarketDataPort(client).load_snapshot(
            profile=profile,
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:47:01+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=late_audit,
            evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
        )
    assert late_audit.records()[0].reason_code == "minute_evidence_latency_exceeded"

    stale_decision_client = _client(_Transport(metadata=delayed))
    stale_decision_audit = MinuteEvidenceAuditLedger()
    with pytest.raises(MinuteDataContractError, match="latency"):
        TradingDatasMinuteMarketDataPort(stale_decision_client).load_snapshot(
            profile=_profile(stale_decision_client),
            filters={},
            decision_time=datetime.fromisoformat("2026-07-27T09:50:00+08:00"),
            trading_dates=frozenset({date(2026, 7, 27)}),
            audit_ledger=stale_decision_audit,
            evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
        )
    assert stale_decision_audit.records()[0].reason_code == (
        "minute_evidence_latency_exceeded"
    )

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
        identity_fields=["ts_code", "time"],
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
        identity_fields=["ts_code", "time"],
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
