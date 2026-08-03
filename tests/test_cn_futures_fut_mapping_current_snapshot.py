"""Failure-first contract tests for the bounded ``fut_mapping`` M reader."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable

import pytest

from CNFutures.fut_mapping_current_snapshot import (
    FutMappingCurrentSnapshotConsumerError,
    load_fut_mapping_current_snapshot,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "fixture-fut-mapping-v1"
DATASET_ID = "cn.dataset.fut_mapping"
TRADE_DATE = "20260803"
RECEIPT_ID = "receipt:fixture-fut-mapping"
RAW_FIELDS = ("trade_date", "ts_code", "mapping_ts_code")
LINEAGE = {
    "complete": True,
    "provider_neutral": True,
    "providers": ["fixture-tushare"],
    "transport_service": "fixture-transport",
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
        return {
            "trade_date": TRADE_DATE,
            "ts_code": "M.DCE",
            "mapping_ts_code": "M2609.DCE",
        }
    return {
        "trade_date": TRADE_DATE,
        "ts_code": f"RB{2600 + index:04d}.SHFE",
        "mapping_ts_code": f"RB{2600 + index:04d}.SHFE",
    }


def _rows() -> list[dict[str, object]]:
    return [_row(index) for index in range(202)]


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid", "valid": True},
        "lineage": copy.deepcopy(LINEAGE),
        "receipt_id": RECEIPT_ID,
        "data_through": "2026-08-03T20:20:57+00:00",
        "observed_at": "2026-08-03T20:20:58+00:00",
        "reasons": [],
    }
    value.update(overrides)
    return value


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
                "next_cursor": None if self.terminal else "fixture-cursor:202",
                "metadata": copy.deepcopy(self.metadata),
            },
        )


def _client(transport: FixtureTransport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://fixture.cnfutures.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset({DATASET_ID}),
            access_policy_id="fixture-cnfutures-fut-mapping",
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _load(transport: FixtureTransport) -> object:
    return load_fut_mapping_current_snapshot(
        client=_client(transport),
        trade_date=TRADE_DATE,
        expected_catalog_version=CATALOG_VERSION,
        expected_receipt_id=RECEIPT_ID,
        expected_lineage_sha256=_sha256(LINEAGE),
    )


def test_maps_the_one_m_dce_current_snapshot_row_as_raw_fact() -> None:
    transport = FixtureTransport()

    result = _load(transport)

    assert result.dataset_id == DATASET_ID
    assert result.schema_major == 1
    assert result.trade_date == TRADE_DATE
    assert result.page_count == 1
    assert result.row_count == 202
    assert result.terminal_pagination is True
    assert result.replay_verified is True
    assert len(result.semantic_sha256) == 64
    assert len(result.pagination_trace_sha256) == 64
    assert result.as_of is None
    assert result.stable is False
    assert result.pit_rollover_authority is False
    assert result.simulation_ready is False
    assert result.runtime_eligible is False
    assert result.execution_eligible is False
    assert result.trading_eligible is False
    assert [fact.ts_code for fact in result.facts] == ["M.DCE"]
    assert result.facts[0].raw_values == {"mapping_ts_code": "M2609.DCE"}
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
    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(metadata=metadata))


@pytest.mark.parametrize(
    ("rows", "reason"),
    (
        (_rows()[:-1], "row_count_invalid"),
        (_rows()[:-1] + [_row(0)], "pagination_duplicate_row_identity"),
        ([{key: value for key, value in row.items() if key != "trade_date"} for row in _rows()], "pagination_row_identity_missing"),
        ([{**row, "trade_date": "20260804"} for row in _rows()], "trade_date_partition_drift"),
        ([{key: value for key, value in row.items() if key != "mapping_ts_code"} for row in _rows()], "raw_field_missing"),
        ([{**_row(0), "ts_code": "M2609.DCE"}] + _rows()[1:], "m_dce_row_missing"),
    ),
)
def test_rejects_count_identity_partition_and_m_row_gaps(
    rows: list[dict[str, object]], reason: str
) -> None:
    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(rows=rows))


def test_rejects_nonterminal_snapshot_page() -> None:
    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match="pagination_row_budget_exceeded"):
        _load(FixtureTransport(terminal=False))


def test_rejects_catalog_identity_order_schema_receipt_lineage_and_replay_drift() -> None:
    catalog = _catalog_row()
    catalog["schema_major"] = 2
    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match="catalog_schema_invalid"):
        _load(FixtureTransport(catalog_row=catalog))

    catalog = _catalog_row()
    catalog["identity_fields"] = ["ts_code"]
    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match="catalog_identity_invalid"):
        _load(FixtureTransport(catalog_row=catalog))

    catalog = _catalog_row()
    catalog["default_order"] = ["ts_code:asc"]
    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match="catalog_order_invalid"):
        _load(FixtureTransport(catalog_row=catalog))

    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match="receipt_mismatch"):
        load_fut_mapping_current_snapshot(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id="receipt:other",
            expected_lineage_sha256=_sha256(LINEAGE),
        )

    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match="lineage_mismatch"):
        load_fut_mapping_current_snapshot(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256="0" * 64,
        )

    with pytest.raises(FutMappingCurrentSnapshotConsumerError, match="replay_drift"):
        _load(
            FixtureTransport(
                replay_mutator=lambda rows: rows[0].update({"mapping_ts_code": "M2701.DCE"})
            )
        )
