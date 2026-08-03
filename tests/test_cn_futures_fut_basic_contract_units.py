"""Failure-first contract tests for the bounded ``fut_basic`` M reader."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable

import pytest

from CNFutures.fut_basic_contract_units import (
    FutBasicContractUnitConsumerError,
    load_fut_basic_raw_contract_units,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "fixture-fut-basic-v1"
DATASET_ID = "cn.dataset.fut_basic"
RECEIPT_ID = "receipt:fixture-fut-basic"
LINEAGE = {
    "complete": True,
    "provider_neutral": True,
    "providers": ["fixture-tushare"],
    "transport_service": "fixture-transport",
}
QUERY_FIELDS = (
    "ts_code",
    "exchange",
    "fut_code",
    "multiplier",
    "trade_unit",
    "per_unit",
    "quote_unit",
    "quote_unit_desc",
)


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
        "default_fields": list(QUERY_FIELDS),
        "default_order": ["ts_code:asc"],
        "identity_fields": ["ts_code"],
        "fields": [
            {
                "name": field,
                "logical_type": "text",
                "nullable": True,
                "selectable": True,
                "filterable": field == "fut_code",
                "sortable": field == "ts_code",
                "operators": ["eq"] if field == "fut_code" else [],
            }
            for field in QUERY_FIELDS
        ],
        "filter_operators": {"fut_code": ["eq"]},
        "limits": {"max_page_size": 100, "max_lookback_days": 36500},
        "availability": {
            "entitlement_states": ["active"],
            "activation_states": ["active"],
        },
        "queryability": {"queryable": True, "reasons": []},
    }


def _row(index: int) -> dict[str, object]:
    return {
        "ts_code": f"M{2601 + index:04d}.DCE",
        "exchange": "DCE",
        "fut_code": "M",
        "multiplier": 10.0,
        "trade_unit": "10吨/手",
        "per_unit": 10.0,
        "quote_unit": "元/吨",
        "quote_unit_desc": "元/吨",
    }


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "state": "partial",
        "degraded": True,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid", "valid": True},
        "lineage": copy.deepcopy(LINEAGE),
        "receipt_id": RECEIPT_ID,
        "data_through": "2026-08-03T17:21:23+00:00",
        "observed_at": "2026-08-03T17:21:38+00:00",
        "reasons": ["response_completeness_unverified"],
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
    ) -> None:
        self.rows = copy.deepcopy(rows if rows is not None else [_row(index) for index in range(207)])
        self.catalog_row = copy.deepcopy(catalog_row or _catalog_row())
        self.metadata = copy.deepcopy(metadata or _metadata())
        self.replay_mutator = replay_mutator
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
        assert body["fields"] == list(QUERY_FIELDS)
        assert body["filters"] == {"fut_code": {"eq": "M"}}
        assert body["order"] == ["ts_code:asc"]
        assert "as_of" not in body
        cursor = body.get("cursor")
        if cursor is None:
            self.run_index += 1
        rows = copy.deepcopy(self.rows)
        if self.run_index == 1 and self.replay_mutator is not None:
            self.replay_mutator(rows)
        offset = 0 if cursor is None else int(str(cursor).rsplit(":", 1)[1])
        page = rows[offset : offset + 100]
        next_cursor = (
            f"fixture-cursor:{offset + len(page)}"
            if offset + len(page) < len(rows)
            else None
        )
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"fixture-query-{self.run_index}-{offset}",
                "dataset_id": DATASET_ID,
                "data": page,
                "next_cursor": next_cursor,
                "metadata": copy.deepcopy(self.metadata),
            },
        )


def _client(transport: FixtureTransport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://fixture.cnfutures.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset({DATASET_ID}),
            access_policy_id="fixture-cnfutures-fut-basic",
            max_limit=100,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _load(transport: FixtureTransport) -> object:
    return load_fut_basic_raw_contract_units(
        client=_client(transport),
        expected_catalog_version=CATALOG_VERSION,
        expected_receipt_id=RECEIPT_ID,
        expected_lineage_sha256=_sha256(LINEAGE),
    )


def test_maps_the_exact_dce_m_cohort_as_partial_raw_contract_facts() -> None:
    transport = FixtureTransport()

    result = _load(transport)

    assert result.dataset_id == DATASET_ID
    assert result.schema_major == 1
    assert result.row_count == 207
    assert result.page_count == 3
    assert result.terminal_pagination is True
    assert result.replay_verified is True
    assert result.coverage_complete is False
    assert result.coverage_reason == "response_completeness_unverified"
    assert result.state == "partial"
    assert result.degraded is True
    assert result.as_of is None
    assert result.pit_authority is False
    assert result.runtime_eligible is False
    assert result.execution_eligible is False
    assert result.trading_eligible is False
    assert len(result.semantic_sha256) == 64
    assert len(result.pagination_trace_sha256) == 64
    assert len(result.facts) == 207
    assert len({fact.ts_code for fact in result.facts}) == 207
    assert result.facts[0].ts_code == "M2601.DCE"
    assert result.facts[0].raw_values == {
        "multiplier": 10.0,
        "trade_unit": "10吨/手",
        "per_unit": 10.0,
        "quote_unit": "元/吨",
        "quote_unit_desc": "元/吨",
    }
    assert len(transport.calls) == 7


@pytest.mark.parametrize(
    ("metadata", "reason"),
    (
        (_metadata(reasons=["some_other_degraded_reason"]), "metadata_degraded_reason_invalid"),
        (_metadata(state="ready"), "metadata_state_invalid"),
        (_metadata(degraded=False), "metadata_degraded_invalid"),
        (_metadata(lineage={"complete": False, "provider_neutral": True}), "lineage_incomplete"),
    ),
)
def test_rejects_any_degraded_state_except_the_explicit_coverage_debt(
    metadata: dict[str, object], reason: str
) -> None:
    with pytest.raises(FutBasicContractUnitConsumerError, match=reason):
        _load(FixtureTransport(metadata=metadata))


@pytest.mark.parametrize(
    ("rows", "reason"),
    (
        ([_row(index) for index in range(206)], "row_count_invalid"),
        ([_row(index) for index in range(206)] + [_row(0)], "pagination_duplicate_row_identity"),
        ([{key: value for key, value in _row(index).items() if key != "ts_code"} for index in range(207)], "pagination_row_identity_missing"),
        ([{key: value for key, value in _row(index).items() if key != "quote_unit"} for index in range(207)], "raw_field_missing"),
        ([{**_row(index), "exchange": "SHFE"} for index in range(207)], "row_exchange_invalid"),
        ([{**_row(index), "fut_code": "RB"} for index in range(207)], "row_fut_code_invalid"),
        ([{**_row(0), "ts_code": "RB2601.SHFE"}] + [_row(index) for index in range(1, 207)], "row_ts_code_invalid"),
        ([{**_row(0), "ts_code": "M-2601.DCE"}] + [_row(index) for index in range(1, 207)], "row_ts_code_invalid"),
    ),
)
def test_rejects_count_identity_unit_and_dce_m_scope_drift(
    rows: list[dict[str, object]], reason: str
) -> None:
    with pytest.raises(FutBasicContractUnitConsumerError, match=reason):
        _load(FixtureTransport(rows=rows))


def test_rejects_catalog_receipt_lineage_and_replay_drift() -> None:
    catalog = _catalog_row()
    catalog["identity_fields"] = ["symbol"]
    with pytest.raises(FutBasicContractUnitConsumerError, match="catalog_identity_invalid"):
        _load(FixtureTransport(catalog_row=catalog))

    with pytest.raises(FutBasicContractUnitConsumerError, match="receipt_mismatch"):
        load_fut_basic_raw_contract_units(
            client=_client(FixtureTransport()),
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id="receipt:other",
            expected_lineage_sha256=_sha256(LINEAGE),
        )

    with pytest.raises(FutBasicContractUnitConsumerError, match="lineage_mismatch"):
        load_fut_basic_raw_contract_units(
            client=_client(FixtureTransport()),
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id=RECEIPT_ID,
            expected_lineage_sha256="0" * 64,
        )

    with pytest.raises(FutBasicContractUnitConsumerError, match="replay_drift"):
        _load(
            FixtureTransport(
                replay_mutator=lambda rows: rows[0].update({"trade_unit": "changed"})
            )
        )
