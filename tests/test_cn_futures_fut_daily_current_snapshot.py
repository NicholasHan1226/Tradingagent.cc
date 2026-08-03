"""Failure-first contract tests for the bounded ``fut_daily`` M reader."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from CNFutures.fut_daily_current_snapshot import (
    FutDailyCurrentSnapshotConsumerError,
    _validate_metadata,
    load_fut_daily_current_snapshot,
)
from CNFutures.fut_mapping_current_snapshot import (
    FutMappingCurrentSnapshot,
    FutMappingRawCurrentSnapshotFact,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "fixture-fut-daily-v1"
DATASET_ID = "cn.dataset.fut_daily"
TRADE_DATE = "20260803"
RECEIPT_ID = "receipt:fixture-fut-daily"
MAPPING_RECEIPT_ID = "receipt:fixture-fut-mapping"
RAW_FIELDS = ("trade_date", "ts_code", "open", "high", "low", "close", "settle", "vol", "oi")
DATA_THROUGH = "2026-08-03T20:20:57+00:00"
OBSERVED_AT = "2026-08-03T20:20:58+00:00"
DECISION_TIME = datetime.fromisoformat("2026-08-03T20:21:00+00:00")
LINEAGE = {
    "complete": True,
    "provider_neutral": True,
    "providers": ["fixture-tushare"],
    "transport_service": "fixture-transport",
}
MAPPING_LINEAGE = {
    "complete": True,
    "provider_neutral": True,
    "providers": ["fixture-tushare"],
    "transport_service": "fixture-mapping-transport",
}


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _catalog_row() -> dict[str, object]:
    return {
        "dataset_id": DATASET_ID,
        "schema_major": 1,
        "default_fields": list(RAW_FIELDS),
        "default_order": ["trade_date:asc", "ts_code:asc"],
        "identity_fields": ["trade_date", "ts_code"],
        "fields": [
            {
                "name": field,
                "logical_type": "text",
                "nullable": False,
                "selectable": True,
                "filterable": field == "trade_date",
                "sortable": field in {"trade_date", "ts_code"},
                "operators": ["eq"] if field == "trade_date" else [],
            }
            for field in RAW_FIELDS
        ],
        "filter_operators": {"trade_date": ["eq"]},
        "limits": {"max_page_size": 500, "max_lookback_days": 36500},
        "availability": {
            "entitlement_states": ["active"],
            "activation_states": ["active"],
        },
        "queryability": {"queryable": True, "reasons": []},
    }


def _row(index: int) -> dict[str, object]:
    if index == 0:
        ts_code = "M2609.DCE"
    else:
        ts_code = f"RB{2600 + index:04d}.SHFE"
    return {
        "trade_date": TRADE_DATE,
        "ts_code": ts_code,
        "open": 3000 + index,
        "high": 3010 + index,
        "low": 2990 + index,
        "close": 3005 + index,
        "settle": 3004 + index,
        "vol": 1000 + index,
        "oi": 2000 + index,
    }


def _rows() -> list[dict[str, object]]:
    return [_row(0), _row(1)]


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid", "valid": True},
        "lineage": copy.deepcopy(LINEAGE),
        "receipt_id": RECEIPT_ID,
        "data_through": DATA_THROUGH,
        "observed_at": OBSERVED_AT,
        "reasons": [],
    }
    value.update(overrides)
    return value


def _metadata_object(**overrides: object) -> SimpleNamespace:
    return SimpleNamespace(**_metadata(**overrides))


def _mapping_snapshot() -> FutMappingCurrentSnapshot:
    fact = FutMappingRawCurrentSnapshotFact(
        trade_date=TRADE_DATE,
        ts_code="M.DCE",
        receipt_id=MAPPING_RECEIPT_ID,
        lineage_sha256=_sha256(MAPPING_LINEAGE),
        raw_values={"mapping_ts_code": "M2609.DCE"},
    )
    return FutMappingCurrentSnapshot(
        dataset_id="cn.dataset.fut_mapping",
        schema_major=1,
        catalog_version="fixture-fut-mapping-v1",
        trade_date=TRADE_DATE,
        receipt_id=MAPPING_RECEIPT_ID,
        lineage_sha256=_sha256(MAPPING_LINEAGE),
        page_count=1,
        row_count=202,
        terminal_pagination=True,
        replay_verified=True,
        semantic_sha256="1" * 64,
        pagination_trace_sha256="2" * 64,
        facts=(fact,),
    )


class FixtureTransport:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        catalog_row: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        replay_mutator: Callable[[list[dict[str, object]]], None] | None = None,
        terminal: bool = True,
    ) -> None:
        self.rows = copy.deepcopy(rows if rows is not None else _rows())
        self.catalog_row = copy.deepcopy(catalog_row or _catalog_row())
        self.metadata = copy.deepcopy(metadata or _metadata())
        self.replay_mutator = replay_mutator
        self.terminal = terminal
        self.calls: list[dict[str, object]] = []
        self.run_index = -1

    def __call__(self, **kwargs: object) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "fixture-catalog",
                    "data": [copy.deepcopy(self.catalog_row)],
                },
            )
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        assert body["dataset_id"] == DATASET_ID
        assert body["schema_major"] == 1
        assert body["fields"] == list(RAW_FIELDS)
        assert body["filters"] == {"trade_date": {"eq": TRADE_DATE}}
        assert body["order"] == ["trade_date:asc", "ts_code:asc"]
        assert "as_of" not in body
        if self.terminal:
            assert body.get("cursor") is None
        self.run_index += 1
        rows = copy.deepcopy(self.rows)
        if self.run_index == 1 and self.replay_mutator is not None:
            self.replay_mutator(rows)
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"fixture-query-{self.run_index}",
                "dataset_id": DATASET_ID,
                "data": rows,
                "next_cursor": None if self.terminal else "fixture-cursor:2",
                "metadata": copy.deepcopy(self.metadata),
            },
        )


def _client(transport: FixtureTransport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://fixture.cnfutures.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset({DATASET_ID}),
            access_policy_id="fixture-cnfutures-fut-daily",
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _load(
    transport: FixtureTransport,
    *,
    mapping: FutMappingCurrentSnapshot | None = None,
    decision_time: datetime | None = DECISION_TIME,
) -> object:
    return load_fut_daily_current_snapshot(
        client=_client(transport),
        trade_date=TRADE_DATE,
        expected_catalog_version=CATALOG_VERSION,
        expected_receipt_id=RECEIPT_ID,
        expected_lineage_sha256=_sha256(LINEAGE),
        mapping_snapshot=mapping or _mapping_snapshot(),
        decision_time=decision_time,
    )


def test_maps_the_mapping_selected_daily_row_as_raw_current_partition_fact() -> None:
    transport = FixtureTransport()

    result = _load(transport)

    assert result.dataset_id == DATASET_ID
    assert result.schema_major == 1
    assert result.trade_date == TRADE_DATE
    assert result.receipt_id == RECEIPT_ID
    assert result.lineage_sha256 == _sha256(LINEAGE)
    assert result.data_through == datetime.fromisoformat(DATA_THROUGH)
    assert result.observed_at == datetime.fromisoformat(OBSERVED_AT)
    assert result.decision_time == DECISION_TIME
    assert result.mapping_ts_code == "M2609.DCE"
    assert result.mapping_receipt_id == MAPPING_RECEIPT_ID
    assert result.mapping_lineage_sha256 == _sha256(MAPPING_LINEAGE)
    assert result.page_count == 1
    assert result.row_count == 2
    assert result.terminal_pagination is True
    assert result.replay_verified is True
    assert result.as_of is None
    assert result.stable is False
    assert result.pit_authority is False
    assert result.session_authority is False
    assert result.simulation_ready is False
    assert result.runtime_eligible is False
    assert result.execution_eligible is False
    assert result.trading_eligible is False
    assert [fact.ts_code for fact in result.facts] == ["M2609.DCE"]
    assert result.facts[0].receipt_id == RECEIPT_ID
    assert result.facts[0].lineage_sha256 == _sha256(LINEAGE)
    assert result.facts[0].raw_values == {
        "open": 3000,
        "high": 3010,
        "low": 2990,
        "close": 3005,
        "settle": 3004,
        "vol": 1000,
        "oi": 2000,
    }
    assert len(transport.calls) == 3


@pytest.mark.parametrize(
    ("metadata", "reason"),
    (
        (_metadata(state="partial"), "metadata_not_ready"),
        (_metadata(degraded=True), "metadata_degraded"),
        (_metadata(freshness={"state": "stale", "stale": True}), "metadata_not_fresh"),
        (_metadata(quality={"state": "degraded", "valid": False}), "metadata_invalid"),
        (_metadata(lineage={"complete": False, "provider_neutral": True}), "lineage_incomplete"),
    ),
)
def test_rejects_non_consumable_metadata(
    metadata: dict[str, object], reason: str
) -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(metadata=metadata))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("data_through", None, "metadata_data_through_missing"),
        ("observed_at", None, "metadata_observed_at_missing"),
        ("data_through", "not-a-timestamp", "metadata_data_through_invalid"),
        ("observed_at", "not-a-timestamp", "metadata_observed_at_invalid"),
        ("data_through", "2026-08-03T20:20:57", "metadata_data_through_timezone_invalid"),
        ("observed_at", "2026-08-03T20:20:58", "metadata_observed_at_timezone_invalid"),
    ),
)
def test_rejects_missing_malformed_or_naive_metadata_timestamps(
    field: str, value: object, reason: str
) -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match=reason):
        _validate_metadata(
            metadata=_metadata_object(**{field: value}),
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256=_sha256(LINEAGE),
            decision_time=DECISION_TIME,
        )


def test_rejects_missing_or_naive_decision_time() -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="decision_time_invalid"):
        _load(FixtureTransport(), decision_time=None)

    with pytest.raises(
        FutDailyCurrentSnapshotConsumerError, match="decision_time_timezone_invalid"
    ):
        _load(
            FixtureTransport(),
            decision_time=datetime.fromisoformat("2026-08-03T20:21:00"),
        )


def test_rejects_metadata_time_order_after_data_through_or_decision_time() -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="metadata_time_order_invalid"):
        _validate_metadata(
            metadata=_metadata_object(
                data_through="2026-08-03T20:21:00+00:00",
                observed_at=OBSERVED_AT,
            ),
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256=_sha256(LINEAGE),
            decision_time=DECISION_TIME,
        )

    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="metadata_time_order_invalid"):
        _validate_metadata(
            metadata=_metadata_object(),
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256=_sha256(LINEAGE),
            decision_time=datetime.fromisoformat(DATA_THROUGH),
        )


@pytest.mark.parametrize(
    ("rows", "reason"),
    (
        ([], "mapping_selected_daily_row_missing_or_nonunique"),
        (_rows() + [_row(0)], "pagination_duplicate_row_identity"),
        ([{key: value for key, value in row.items() if key != "trade_date"} for row in _rows()], "pagination_row_identity_missing"),
        ([{**row, "trade_date": "20260804"} for row in _rows()], "trade_date_partition_drift"),
        ([{key: value for key, value in row.items() if key != "settle"} for row in _rows()], "raw_field_missing"),
    ),
)
def test_rejects_identity_partition_and_selected_row_gaps(
    rows: list[dict[str, object]], reason: str
) -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(rows=rows))


def test_rejects_nonterminal_snapshot_page() -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="pagination_duplicate_row_identity"):
        _load(FixtureTransport(terminal=False))


def test_rejects_catalog_receipt_lineage_and_replay_drift() -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="catalog_version_mismatch"):
        load_fut_daily_current_snapshot(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version="fixture-fut-daily-other",
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256=_sha256(LINEAGE),
            mapping_snapshot=_mapping_snapshot(),
            decision_time=DECISION_TIME,
        )

    catalog = _catalog_row()
    catalog["schema_major"] = 2
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="catalog_schema_invalid"):
        _load(FixtureTransport(catalog_row=catalog))

    catalog = _catalog_row()
    catalog["identity_fields"] = ["ts_code"]
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="catalog_identity_invalid"):
        _load(FixtureTransport(catalog_row=catalog))

    catalog = _catalog_row()
    catalog["default_order"] = ["ts_code:asc"]
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="catalog_order_invalid"):
        _load(FixtureTransport(catalog_row=catalog))

    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="receipt_mismatch"):
        load_fut_daily_current_snapshot(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id="receipt:other",
            expected_lineage_sha256=_sha256(LINEAGE),
            mapping_snapshot=_mapping_snapshot(),
            decision_time=DECISION_TIME,
        )

    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="lineage_mismatch"):
        load_fut_daily_current_snapshot(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256="0" * 64,
            mapping_snapshot=_mapping_snapshot(),
            decision_time=DECISION_TIME,
        )

    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="replay_drift"):
        _load(FixtureTransport(replay_mutator=lambda rows: rows[0].update({"close": 0})))


@pytest.mark.parametrize(
    ("mapping", "reason"),
    (
        (lambda: replace(_mapping_snapshot(), trade_date="20260804"), "mapping_trade_date_mismatch"),
        (
            lambda: replace(
                _mapping_snapshot(),
                facts=(replace(_mapping_snapshot().facts[0], receipt_id="receipt:other"),),
            ),
            "mapping_fact_receipt_mismatch",
        ),
        (
            lambda: replace(
                _mapping_snapshot(),
                facts=(replace(_mapping_snapshot().facts[0], lineage_sha256="0" * 64),),
            ),
            "mapping_fact_lineage_mismatch",
        ),
        (
            lambda: replace(
                _mapping_snapshot(),
                facts=(replace(_mapping_snapshot().facts[0], trade_date="20260804"),),
            ),
            "mapping_fact_trade_date_mismatch",
        ),
    ),
)
def test_rejects_mapping_identity_receipt_lineage_and_day_drift(
    mapping: Callable[[], FutMappingCurrentSnapshot], reason: str
) -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(), mapping=mapping())


def test_rejects_mapping_dataset_drift() -> None:
    mapping = _mapping_snapshot()
    object.__setattr__(mapping, "dataset_id", "cn.dataset.fut_daily")

    with pytest.raises(
        FutDailyCurrentSnapshotConsumerError, match="mapping_snapshot_authority_invalid"
    ):
        _load(FixtureTransport(), mapping=mapping)


def test_rejects_authority_lift() -> None:
    with pytest.raises(FutDailyCurrentSnapshotConsumerError, match="snapshot_authority_invalid"):
        replace(_load(FixtureTransport()), stable=True)
