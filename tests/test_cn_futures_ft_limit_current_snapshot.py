"""Failure-first contracts for the bounded ``ft_limit`` current snapshot."""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from collections.abc import Callable

import pytest

from CNFutures.ft_limit_current_snapshot import (
    FutLimitCurrentSnapshotConsumerError,
    load_ft_limit_current_snapshot,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "fixture-ft-limit-v1"
DATASET_ID = "cn.dataset.ft_limit"
TRADE_DATE = "20260803"
RECEIPT_ID = "receipt:a6b9755a6aef1da93f708b32c72e6487e2ed04a84dae9c3bc268a313e4e5c036"
RAW_FIELDS = ("trade_date", "ts_code", "exchange", "up_limit", "down_limit", "m_ratio")
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
                "logical_type": "text" if field in {"trade_date", "ts_code", "exchange"} else "float",
                "nullable": field not in {"trade_date", "ts_code"},
                "selectable": True,
                "filterable": field == "trade_date",
                "sortable": field in {"trade_date", "ts_code"},
                "operators": ["eq"] if field == "trade_date" else [],
            }
            for field in RAW_FIELDS
        ],
        "filter_operators": {"trade_date": ["eq"]},
        "limits": {"max_page_size": 100, "max_lookback_days": 36500},
        "availability": {
            "entitlement_states": ["active"],
            "activation_states": ["active"],
        },
        "queryability": {"queryable": True, "reasons": []},
    }


def _row(index: int) -> dict[str, object]:
    if index < 8:
        return {
            "trade_date": TRADE_DATE,
            "ts_code": f"M{2401 + index}.DCE",
            "exchange": "DCE",
            "up_limit": 3000.0 + index,
            "down_limit": 2500.0 + index,
            "m_ratio": 12.0,
        }
    return {
        "trade_date": TRADE_DATE,
        "ts_code": f"RB{2600 + index:04d}.SHFE",
        "exchange": "SHFE",
        "up_limit": 4000.0,
        "down_limit": 3500.0,
        "m_ratio": 13.0,
    }


def _rows() -> list[dict[str, object]]:
    return [_row(index) for index in range(868)]


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "state": "stale",
        "degraded": True,
        "freshness": {"state": "stale", "stale": True},
        "quality": {"state": "degraded_invalid", "valid": False},
        "lineage": copy.deepcopy(LINEAGE),
        "receipt_id": RECEIPT_ID,
        "data_through": "2026-08-03T00:00:00+08:00",
        "observed_at": "2026-08-03T18:03:44.284177+00:00",
        "reasons": ["freshness_sla_exceeded"],
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
        assert body["limit"] == 100
        assert "as_of" not in body
        cursor = body.get("cursor")
        if cursor is None:
            self.run_index += 1
            page_index = 0
        else:
            assert isinstance(cursor, str)
            page_index = int(cursor.rsplit(":", 1)[1])
        assert 0 <= page_index < 9

        rows = copy.deepcopy(self.rows)
        if self.run_index == 1 and self.replay_mutator is not None:
            self.replay_mutator(rows)
        start = page_index * 100
        page = rows[start : start + 100]
        terminal = page_index == 8 and self.terminal
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"fixture-query-{self.run_index}-{page_index}",
                "dataset_id": DATASET_ID,
                "data": page,
                "next_cursor": None if terminal else f"fixture-cursor:{page_index + 1}",
                "metadata": copy.deepcopy(self.metadata),
            },
        )


def _client(transport: FixtureTransport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://fixture.cnfutures.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset({DATASET_ID}),
            access_policy_id="fixture-cnfutures-ft-limit",
            max_limit=100,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _load(transport: FixtureTransport) -> object:
    return load_ft_limit_current_snapshot(
        client=_client(transport),
        trade_date=TRADE_DATE,
        expected_catalog_version=CATALOG_VERSION,
        expected_receipt_id=RECEIPT_ID,
        expected_lineage_sha256=_sha256(LINEAGE),
    )


def test_maps_only_the_eight_dce_m_raw_price_limit_facts() -> None:
    transport = FixtureTransport()

    result = _load(transport)

    assert result.dataset_id == DATASET_ID
    assert result.schema_major == 1
    assert result.trade_date == TRADE_DATE
    assert result.page_count == 9
    assert result.row_count == 868
    assert result.terminal_pagination is True
    assert result.replay_verified is True
    assert result.state == "stale"
    assert result.degraded is True
    assert result.reason == "freshness_sla_exceeded"
    assert result.as_of is None
    assert result.stable is False
    assert result.pit_authority is False
    assert result.numeric_tick_authority is False
    assert result.session_authority is False
    assert result.rollover_authority is False
    assert result.simulation_ready is False
    assert result.runtime_eligible is False
    assert result.execution_eligible is False
    assert result.trading_eligible is False
    assert [fact.ts_code for fact in result.facts] == [f"M{2401 + index}.DCE" for index in range(8)]
    assert all(fact.exchange == "DCE" for fact in result.facts)
    assert all(
        set(fact.raw_values) == {"up_limit", "down_limit", "m_ratio"}
        and fact.receipt_id == RECEIPT_ID
        and fact.lineage_sha256 == _sha256(LINEAGE)
        for fact in result.facts
    )
    assert len(transport.calls) == 19


@pytest.mark.parametrize(
    ("catalog_update", "reason"),
    (
        ({"schema_major": 2}, "catalog_schema_invalid"),
        ({"identity_fields": ["ts_code"]}, "catalog_identity_invalid"),
        ({"default_order": ["ts_code:asc"]}, "catalog_order_invalid"),
    ),
)
def test_rejects_catalog_contract_drift(
    catalog_update: dict[str, object], reason: str
) -> None:
    catalog = _catalog_row()
    catalog.update(catalog_update)

    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(catalog_row=catalog))


@pytest.mark.parametrize(
    ("rows", "reason"),
    (
        (_rows()[:-1], "row_count_invalid"),
        (_rows()[:-1] + [_row(0)], "pagination_duplicate_row_identity"),
        ([{key: value for key, value in row.items() if key != "trade_date"} for row in _rows()], "pagination_row_identity_missing"),
        ([{**row, "trade_date": "20260804"} for row in _rows()], "trade_date_partition_drift"),
        ([{key: value for key, value in row.items() if key != "up_limit"} for row in _rows()[:8]] + _rows()[8:], "raw_field_missing"),
        ([{**_row(0), "exchange": "SHFE"}] + _rows()[1:], "dce_m_rows_missing_or_nonunique"),
        (
            [*_rows()[:8], {**_row(0), "ts_code": "M2409.DCE"}, *_rows()[9:]],
            "dce_m_rows_missing_or_nonunique",
        ),
    ),
)
def test_rejects_partition_identity_and_dce_m_fact_gaps(
    rows: list[dict[str, object]], reason: str
) -> None:
    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(rows=rows))


def test_rejects_nonterminal_pagination_and_replay_drift() -> None:
    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match="pagination_row_budget_exceeded"):
        _load(FixtureTransport(terminal=False))

    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match="replay_drift"):
        _load(
            FixtureTransport(
                replay_mutator=lambda rows: rows[0].update({"up_limit": 9999.0})
            )
        )


def test_rejects_receipt_lineage_and_stale_metadata_drift() -> None:
    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match="receipt_mismatch"):
        load_ft_limit_current_snapshot(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id="receipt:other",
            expected_lineage_sha256=_sha256(LINEAGE),
        )
    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match="lineage_mismatch"):
        load_ft_limit_current_snapshot(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256="0" * 64,
        )
    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match="metadata_stale_contract_invalid"):
        _load(FixtureTransport(metadata=_metadata(state="ready", degraded=False)))


@pytest.mark.parametrize(
    "field_name",
    (
        "stable",
        "pit_authority",
        "numeric_tick_authority",
        "session_authority",
        "rollover_authority",
        "simulation_ready",
        "runtime_eligible",
        "execution_eligible",
        "trading_eligible",
    ),
)
def test_rejects_every_attempted_snapshot_authority_lift(field_name: str) -> None:
    result = _load(FixtureTransport())
    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match="snapshot_authority_invalid"):
        replace(result, **{field_name: True})


def test_rejects_attempted_raw_fact_authority_lift() -> None:
    result = _load(FixtureTransport())
    with pytest.raises(FutLimitCurrentSnapshotConsumerError, match="raw_fact_authority_invalid"):
        replace(result.facts[0], numeric_tick_authority=True)
