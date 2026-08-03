from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable

import pytest

from CNFutures.fut_settle_market_rules import (
    FutSettleMarketRuleConsumerError,
    load_fut_settle_raw_market_rules,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG_VERSION = "fixture-fut-settle-v2"
DATASET_ID = "cn.dataset.fut_settle"
TRADE_DATE = "20260803"
RECEIPT_ID = "receipt:fixture-fut-settle"
RAW_FIELDS = (
    "trade_date",
    "ts_code",
    "settle",
    "trading_fee_rate",
    "trading_fee",
    "delivery_fee",
    "b_hedging_margin_rate",
    "s_hedging_margin_rate",
    "long_margin_rate",
    "short_margin_rate",
)
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
    filter_operators = {"trade_date": ["eq"]}
    return {
        "dataset_id": DATASET_ID,
        "schema_major": 2,
        "default_fields": list(RAW_FIELDS),
        "default_order": ["trade_date:asc", "ts_code:asc"],
        "identity_fields": ["trade_date", "ts_code"],
        "fields": [
            {
                "name": field,
                "logical_type": "text",
                "nullable": False,
                "selectable": True,
                "filterable": field in filter_operators,
                "sortable": field in {"trade_date", "ts_code"},
                "operators": filter_operators.get(field, []),
            }
            for field in RAW_FIELDS
        ],
        "filter_operators": filter_operators,
        "limits": {"max_page_size": 500, "max_lookback_days": 36500},
        "availability": {
            "entitlement_states": ["active"],
            "activation_states": ["active"],
        },
        "queryability": {"queryable": True, "reasons": []},
    }


def _row(ts_code: str, *, trade_date: str = TRADE_DATE) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "ts_code": ts_code,
        "settle": 3000.0,
        "trading_fee_rate": 0.0001,
        "trading_fee": 1.5,
        "delivery_fee": 0.0,
        "b_hedging_margin_rate": 0.08,
        "s_hedging_margin_rate": 0.08,
        "long_margin_rate": 0.10,
        "short_margin_rate": 0.10,
    }


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid", "valid": True},
        "lineage": copy.deepcopy(LINEAGE),
        "receipt_id": RECEIPT_ID,
        "data_through": "2026-08-03T18:49:11+00:00",
        "observed_at": "2026-08-03T18:49:13+00:00",
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
    ) -> None:
        self.rows = copy.deepcopy(
            rows if rows is not None else [_row("M2609.DCE"), _row("RB2610.SHFE")]
        )
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
        assert body["schema_major"] == 2
        assert body["fields"] == list(RAW_FIELDS)
        assert body["filters"] == {"trade_date": {"eq": TRADE_DATE}}
        assert body["order"] == ["trade_date:asc", "ts_code:asc"]
        assert "as_of" not in body
        cursor = body.get("cursor")
        if cursor is None:
            self.run_index += 1
        rows = copy.deepcopy(self.rows)
        if self.run_index == 1 and self.replay_mutator is not None:
            self.replay_mutator(rows)
        offset = 0 if cursor is None else int(str(cursor).rsplit(":", 1)[1])
        page = rows[offset : offset + 1]
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
            access_policy_id="fixture-cnfutures-fut-settle",
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _load(transport: FixtureTransport) -> object:
    return load_fut_settle_raw_market_rules(
        client=_client(transport),
        trade_date=TRADE_DATE,
        expected_catalog_version=CATALOG_VERSION,
        expected_receipt_id=RECEIPT_ID,
        expected_lineage_sha256=_sha256(LINEAGE),
    )


def test_maps_only_dce_m_receipt_bound_raw_market_rule_facts() -> None:
    transport = FixtureTransport()

    result = _load(transport)

    assert result.dataset_id == DATASET_ID
    assert result.schema_major == 2
    assert result.trade_date == TRADE_DATE
    assert result.page_count == 2
    assert result.row_count == 2
    assert result.terminal_pagination is True
    assert result.replay_verified is True
    assert len(result.semantic_sha256) == 64
    assert len(result.pagination_trace_sha256) == 64
    assert result.as_of is None
    assert result.pit_authority is False
    assert result.execution_eligible is False
    assert [fact.ts_code for fact in result.facts] == ["M2609.DCE"]
    assert result.facts[0].raw_values == {
        "settle": 3000.0,
        "trading_fee_rate": 0.0001,
        "trading_fee": 1.5,
        "delivery_fee": 0.0,
        "b_hedging_margin_rate": 0.08,
        "s_hedging_margin_rate": 0.08,
        "long_margin_rate": 0.10,
        "short_margin_rate": 0.10,
    }
    assert len(transport.calls) == 5


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
    with pytest.raises(FutSettleMarketRuleConsumerError, match=reason):
        _load(FixtureTransport(metadata=metadata))


@pytest.mark.parametrize(
    ("rows", "reason"),
    (
        ([_row("M2609.DCE"), _row("M2609.DCE")], "pagination_duplicate_row_identity"),
        ([_row("M2609.DCE", trade_date="20260804")], "trade_date_partition_drift"),
        ([{"trade_date": TRADE_DATE, "ts_code": "M2609.DCE"}], "raw_field_missing"),
    ),
)
def test_rejects_identity_partition_and_raw_fact_gaps(
    rows: list[dict[str, object]], reason: str
) -> None:
    with pytest.raises(FutSettleMarketRuleConsumerError, match=reason):
        _load(FixtureTransport(rows=rows))


def test_rejects_catalog_identity_and_schema_drift() -> None:
    catalog = _catalog_row()
    catalog["schema_major"] = 1

    with pytest.raises(FutSettleMarketRuleConsumerError, match="catalog_schema_invalid"):
        _load(FixtureTransport(catalog_row=catalog))


def test_rejects_receipt_lineage_and_replay_drift() -> None:
    with pytest.raises(FutSettleMarketRuleConsumerError, match="receipt_mismatch"):
        load_fut_settle_raw_market_rules(
            client=_client(FixtureTransport()),
            trade_date=TRADE_DATE,
            expected_catalog_version=CATALOG_VERSION,
            expected_receipt_id="receipt:other",
            expected_lineage_sha256=_sha256(LINEAGE),
        )

    with pytest.raises(FutSettleMarketRuleConsumerError, match="replay_drift"):
        _load(
            FixtureTransport(
                replay_mutator=lambda rows: rows[0].update({"settle": 3001.0})
            )
        )
