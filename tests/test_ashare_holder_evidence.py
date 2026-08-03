from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from Ashare.holder_evidence import (
    HOLDER_DATASET_IDS,
    AshareHolderContractError,
    AshareHolderEvidenceAuditLedger,
    TradingDatasAshareHolderEvidencePort,
    load_holder_snapshots,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)

CATALOG = "fixture-ashare-holder-catalog-v1"
DECISION_TIME = datetime.fromisoformat("2026-08-03T17:00:00+08:00")

_FIELDS = {
    "cn.dataset.stk_holdernumber": [
        "ts_code",
        "ann_date",
        "end_date",
        "holder_num",
    ],
    "cn.dataset.stk_holdertrade": [
        "ts_code",
        "ann_date",
        "holder_name",
        "holder_type",
        "in_de",
        "change_vol",
        "change_ratio",
        "after_share",
        "after_ratio",
        "avg_price",
        "total_share",
    ],
}
_IDENTITIES = {
    "cn.dataset.stk_holdernumber": ["ts_code", "ann_date", "end_date"],
    "cn.dataset.stk_holdertrade": [
        "ts_code",
        "ann_date",
        "holder_name",
        "holder_type",
        "in_de",
        "change_vol",
        "change_ratio",
        "after_share",
        "after_ratio",
        "avg_price",
        "total_share",
    ],
}


def _catalog_row(
    dataset_id: str, *, identity_fields: list[str] | None = None
) -> dict[str, Any]:
    fields = _FIELDS[dataset_id]
    return {
        "dataset_id": dataset_id,
        "schema_major": 1 if dataset_id.endswith("holdernumber") else 2,
        "default_fields": fields,
        "default_order": ["ann_date:asc", "ts_code:asc"],
        "filter_operators": {field: ["eq", "in", "gte", "lte"] for field in fields},
        "identity_fields": identity_fields or _IDENTITIES[dataset_id],
        "limits": {"max_page_size": 100},
        "availability": {"activation_states": ["active"]},
    }


def _metadata(**overrides: Any) -> dict[str, Any]:
    value = {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid"},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "providers": ["fixture-provider"],
            "transport_service": "fixture-v1",
        },
        "receipt_id": "receipt-holder-1",
        "data_through": "2026-08-03T16:30:00+08:00",
        "observed_at": "2026-08-03T16:35:00+08:00",
        "reasons": [],
    }
    value.update(overrides)
    return value


def _rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "cn.dataset.stk_holdernumber": [
            {
                "ts_code": "600000.SH",
                "ann_date": "20260803",
                "end_date": "20260731",
                "holder_num": 100,
            },
        ],
        "cn.dataset.stk_holdertrade": [
            {
                "ts_code": "600000.SH",
                "ann_date": "20260803",
                "holder_name": "fixture holder",
                "holder_type": "individual",
                "in_de": "IN",
                "change_vol": 1000,
                "change_ratio": 0.1,
                "after_share": 2000,
                "after_ratio": 0.2,
                "avg_price": 10.0,
                "total_share": 10000,
            },
        ],
    }


class _Transport:
    def __init__(
        self,
        *,
        rows: dict[str, list[dict[str, Any]]] | None = None,
        metadata: dict[str, Any] | None = None,
        catalog_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = rows or _rows()
        self.metadata = metadata or _metadata()
        self.catalog_rows = catalog_rows or [
            _catalog_row(dataset_id) for dataset_id in HOLDER_DATASET_IDS
        ]
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG,
                    "request_id": "catalog-1",
                    "data": copy.deepcopy(self.catalog_rows),
                },
            )
        body = kwargs["json_body"]
        assert body is not None
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG,
                "request_id": f"query-{len(self.calls)}",
                "dataset_id": body["dataset_id"],
                "data": copy.deepcopy(self.rows[body["dataset_id"]]),
                "next_cursor": None,
                "metadata": copy.deepcopy(self.metadata),
            },
        )


def _port(transport: _Transport) -> TradingDatasAshareHolderEvidencePort:
    return TradingDatasAshareHolderEvidencePort(
        SharedSignalsV1Client(
            SharedSignalsV1Config(
                base_url="https://holder-evidence.fixture.invalid",
                expected_catalog_version=CATALOG,
                dataset_ids=frozenset(HOLDER_DATASET_IDS),
                access_policy_id="ashare-holder-fixture",
                catalog_version_policy="evidence_only",
                max_limit=100,
                cache_ttl_seconds=0,
            ),
            transport=transport,
        )
    )


def _load(transport: _Transport):
    audit = AshareHolderEvidenceAuditLedger()
    port = _port(transport)
    profiles = port.freeze_holder_profiles(audit_ledger=audit)
    return load_holder_snapshots(
        port=port,
        profiles=profiles,
        decision_time=DECISION_TIME,
        audit_ledger=audit,
        allowed_symbols=("600000.SH",),
    ), audit


def test_explicit_holder_profiles_read_only_facts_and_caller_invoked_bounded_read() -> (
    None
):
    transport = _Transport()
    batches, audit = _load(transport)

    assert tuple(batch.profile.dataset_id for batch in batches) == HOLDER_DATASET_IDS
    assert all(
        batch.same_observation and batch.contract_ready_only for batch in batches
    )
    assert all(fact.symbol == "600000.SH" for batch in batches for fact in batch.facts)
    assert all(
        not fact.candidate_eligible and not fact.execution_eligible
        for batch in batches
        for fact in batch.facts
    )
    assert audit.records() == ()
    queries = [
        call["json_body"] for call in transport.calls if call["method"] == "POST"
    ]
    assert len(queries) == 4
    assert all(
        query["filters"] == {"ts_code": {"in": ["600000.SH"]}} for query in queries
    )


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata(degraded=True), "ashare_holder_metadata_not_ready"),
        (
            _metadata(freshness={"state": "stale", "stale": True}),
            "ashare_holder_metadata_not_fresh",
        ),
        # The fixed V1 parser rejects absent proof fields before this adapter;
        # the adapter converts that parser rejection into one fail-closed reason.
        (_metadata(receipt_id=None), "ashare_holder_query_failed"),
        (_metadata(lineage=None), "ashare_holder_query_failed"),
        (_metadata(data_through=None), "ashare_holder_query_failed"),
        (_metadata(observed_at=None), "ashare_holder_query_failed"),
    ],
)
def test_holder_envelope_proof_gaps_fail_closed(
    metadata: dict[str, Any], reason: str
) -> None:
    with pytest.raises(AshareHolderContractError, match=reason):
        _load(_Transport(metadata=metadata))


@pytest.mark.parametrize(
    ("dataset_id", "rows", "reason"),
    [
        (
            "cn.dataset.stk_holdernumber",
            {
                "cn.dataset.stk_holdernumber": [
                    {
                        **_rows()["cn.dataset.stk_holdernumber"][0],
                        "ts_code": "000001.SZ",
                    }
                ],
                "cn.dataset.stk_holdertrade": _rows()["cn.dataset.stk_holdertrade"],
            },
            "ashare_holder_symbol_mismatch",
        ),
        (
            "cn.dataset.stk_holdernumber",
            {
                "cn.dataset.stk_holdernumber": [
                    {
                        **_rows()["cn.dataset.stk_holdernumber"][0],
                        "ann_date": "20260804",
                    }
                ],
                "cn.dataset.stk_holdertrade": _rows()["cn.dataset.stk_holdertrade"],
            },
            "ashare_holder_time_after_availability",
        ),
    ],
)
def test_holder_symbol_and_time_mismatch_fail_closed(
    dataset_id: str, rows: dict[str, list[dict[str, Any]]], reason: str
) -> None:
    del dataset_id
    with pytest.raises(AshareHolderContractError, match=reason):
        _load(_Transport(rows=rows))


def test_holder_wrong_dataset_or_unsupported_identity_semantics_fail_closed() -> None:
    transport = _Transport(
        catalog_rows=[
            _catalog_row("cn.dataset.stk_holdernumber"),
            _catalog_row(
                "cn.dataset.stk_holdertrade", identity_fields=["ts_code", "ann_date"]
            ),
        ]
    )
    with pytest.raises(
        AshareHolderContractError, match="ashare_holder_catalog_identity_mismatch"
    ):
        _port(transport).freeze_holder_profiles(
            audit_ledger=AshareHolderEvidenceAuditLedger()
        )

    clean_port = _port(_Transport())
    profiles = clean_port.freeze_holder_profiles(
        audit_ledger=AshareHolderEvidenceAuditLedger()
    )
    with pytest.raises(
        AshareHolderContractError, match="ashare_holder_dataset_not_allowlisted"
    ):
        replace(
            profiles.by_dataset["cn.dataset.stk_holdernumber"],
            dataset_id="cn.dataset.unrelated",
        )
    with pytest.raises(
        AshareHolderContractError, match="ashare_holder_dataset_not_allowlisted"
    ):
        clean_port.load_holder_snapshot(
            profile=object(),
            filters={"ts_code": {"in": ["600000.SH"]}},
            decision_time=DECISION_TIME,
            audit_ledger=AshareHolderEvidenceAuditLedger(),
            allowed_symbols=("600000.SH",),
        )
