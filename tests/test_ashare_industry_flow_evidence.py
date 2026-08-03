from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

import pytest

from Ashare.industry_flow_evidence import (
    INDUSTRY_FLOW_DATASET_IDS,
    AshareIndustryFlowContractError,
    AshareIndustryFlowEvidenceAuditLedger,
    TradingDatasAshareIndustryFlowEvidencePort,
    load_industry_flow_snapshots,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)

CATALOG = "fixture-industry-flow-v1"
DECISION = datetime.fromisoformat("2026-08-03T18:00:00+08:00")
FIELDS = {
    "cn.dataset.moneyflow_ind_dc": (
        "trade_date",
        "content_type",
        "ts_code",
        "name",
        "net_amount",
        "rank",
    ),
    "cn.dataset.moneyflow_ind_ths": ("trade_date", "ts_code", "industry", "net_amount"),
}
IDENTITIES = {
    "cn.dataset.moneyflow_ind_dc": ("trade_date", "content_type", "ts_code"),
    "cn.dataset.moneyflow_ind_ths": ("trade_date", "ts_code"),
}


def _catalog_row(
    dataset_id: str, *, identity: tuple[str, ...] | None = None
) -> dict[str, Any]:
    fields = FIELDS[dataset_id]
    return {
        "dataset_id": dataset_id,
        "schema_major": 2,
        "default_fields": list(fields),
        "default_order": ["trade_date:asc", "ts_code:asc"],
        "identity_fields": list(identity or IDENTITIES[dataset_id]),
        "filter_operators": {field: ["eq", "in"] for field in fields},
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
            "providers": ["fixture"],
            "transport_service": "fixture-v1",
        },
        "receipt_id": "receipt-industry-1",
        "data_through": "2026-08-03T17:00:00+08:00",
        "observed_at": "2026-08-03T17:05:00+08:00",
        "reasons": [],
    }
    value.update(overrides)
    return value


class _Transport:
    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        rows: dict[str, list[dict[str, Any]]] | None = None,
        catalog_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.metadata = metadata or _metadata()
        self.rows = rows or {
            "cn.dataset.moneyflow_ind_dc": [
                {
                    "trade_date": "20260803",
                    "content_type": "industry",
                    "ts_code": "DC001",
                    "name": "fixture",
                    "net_amount": 1.0,
                    "rank": 1,
                }
            ],
            "cn.dataset.moneyflow_ind_ths": [
                {
                    "trade_date": "20260803",
                    "ts_code": "THS001",
                    "industry": "fixture",
                    "net_amount": 2.0,
                }
            ],
        }
        self.catalog_rows = catalog_rows or [
            _catalog_row(dataset_id) for dataset_id in INDUSTRY_FLOW_DATASET_IDS
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
                    "request_id": "catalog",
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
                "request_id": f"q{len(self.calls)}",
                "dataset_id": body["dataset_id"],
                "data": copy.deepcopy(self.rows[body["dataset_id"]]),
                "next_cursor": None,
                "metadata": copy.deepcopy(self.metadata),
            },
        )


def _port(transport: _Transport) -> TradingDatasAshareIndustryFlowEvidencePort:
    return TradingDatasAshareIndustryFlowEvidencePort(
        SharedSignalsV1Client(
            SharedSignalsV1Config(
                base_url="https://fixture.invalid",
                expected_catalog_version=CATALOG,
                dataset_ids=frozenset(INDUSTRY_FLOW_DATASET_IDS),
                access_policy_id="ashare-industry-fixture",
                catalog_version_policy="evidence_only",
                max_limit=100,
                cache_ttl_seconds=0,
            ),
            transport=transport,
        )
    )


def _load(transport: _Transport):
    audit = AshareIndustryFlowEvidenceAuditLedger()
    port = _port(transport)
    profiles = port.freeze_industry_flow_profiles(audit_ledger=audit)
    return load_industry_flow_snapshots(
        port=port, profiles=profiles, decision_time=DECISION, audit_ledger=audit
    ), audit


def test_explicit_context_profiles_are_caller_invoked_raw_facts_and_replayed() -> None:
    transport = _Transport()
    batches, audit = _load(transport)
    assert (
        tuple(batch.profile.dataset_id for batch in batches)
        == INDUSTRY_FLOW_DATASET_IDS
    )
    assert all(
        batch.same_observation and batch.contract_ready_only for batch in batches
    )
    assert all(
        not fact.candidate_eligible and not fact.execution_eligible
        for batch in batches
        for fact in batch.facts
    )
    assert audit.records() == ()
    assert len([call for call in transport.calls if call["method"] == "POST"]) == 4


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata(degraded=True), "ashare_industry_flow_metadata_not_ready"),
        (
            _metadata(freshness={"state": "stale", "stale": True}),
            "ashare_industry_flow_metadata_not_fresh",
        ),
        (_metadata(lineage=None), "ashare_industry_flow_query_failed"),
    ],
)
def test_industry_flow_envelope_proof_gaps_fail_closed(
    metadata: dict[str, Any], reason: str
) -> None:
    with pytest.raises(AshareIndustryFlowContractError, match=reason):
        _load(_Transport(metadata=metadata))


def test_industry_flow_catalog_identity_and_time_mismatch_fail_closed() -> None:
    with pytest.raises(
        AshareIndustryFlowContractError,
        match="ashare_industry_flow_catalog_identity_mismatch",
    ):
        _port(
            _Transport(
                catalog_rows=[
                    _catalog_row("cn.dataset.moneyflow_ind_dc"),
                    _catalog_row("cn.dataset.moneyflow_ind_ths", identity=("ts_code",)),
                ]
            )
        ).freeze_industry_flow_profiles(
            audit_ledger=AshareIndustryFlowEvidenceAuditLedger()
        )
    rows = _Transport().rows
    rows["cn.dataset.moneyflow_ind_dc"][0]["trade_date"] = "20260804"
    with pytest.raises(
        AshareIndustryFlowContractError,
        match="ashare_industry_flow_time_after_availability",
    ):
        _load(_Transport(rows=rows))
