from __future__ import annotations

import copy
import inspect
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from Ashare.event_evidence import (
    FIXED_CATALOG_ROUTE,
    FIXED_QUERY_ROUTE,
    OPTIONAL_DATASET_IDS,
    PAUSED_DATASET_IDS,
    PRIMARY_DATASET_IDS,
    AshareEvidenceAuditLedger,
    AshareEvidenceAuditRecord,
    AshareEvidenceContractError,
    TradingDatasAshareEvidencePort,
    bind_shadow_decision,
    build_llm_shadow_request,
    build_sentiment_snapshot,
)
from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.llm.evidence_artifact import EvidenceArtifactError
from shared.review.decision_ledger import ExposureDisposition, InMemoryDecisionLedger


CATALOG = "fixture-ashare-event-catalog-v1"
DECISION_TIME = datetime.fromisoformat("2026-07-31T10:30:00+08:00")
GENERIC_FIELDS = [
    "event_id",
    "ts_code",
    "event_time",
    "entity",
    "title",
    "content",
    "url",
    "source",
]


def _catalog_row(
    dataset_id: str,
    *,
    active: bool = True,
    fields: list[str] | None = None,
    max_page_size: int = 2,
    identity_fields: list[str] | None = None,
) -> dict[str, Any]:
    names = fields or GENERIC_FIELDS
    identities = identity_fields or _fixture_identity_fields(dataset_id, names)
    return {
        "dataset_id": dataset_id,
        "schema_major": 1,
        "default_fields": list(names),
        "default_order": [f"{names[0]}:asc"],
        "filter_operators": {
            name: ["eq", "in", "gte", "lte", "between"] for name in names
        },
        "limits": {"max_page_size": max_page_size},
        "identity_fields": identities,
        "availability": {"activation_states": ["active" if active else "paused"]},
    }


def _fixture_identity_fields(dataset_id: str, fields: list[str]) -> list[str]:
    if dataset_id in PAUSED_DATASET_IDS:
        return ["event_id"]
    candidates = {
        "cn.dataset.anns_d": (("event_id",), ("ann_date", "ts_code", "url")),
        "cn.dataset.cctv_news": (("event_id",), ("date", "title")),
        "cn.dataset.irm_qa_sh": (("event_id",), ("ts_code", "pub_time", "q")),
        "cn.dataset.irm_qa_sz": (("event_id",), ("ts_code", "pub_time", "q")),
        "cn.dataset.research_report": (
            ("event_id",),
            ("trade_date", "url"),
            ("trade_date", "ts_code", "inst_csname", "title"),
        ),
        "cn.dataset.disclosure_date": (
            ("event_id",),
            ("ts_code", "end_date", "ann_date"),
        ),
        "cn.dataset.report_rc": (
            ("event_id",),
            ("ts_code", "report_date", "report_title", "org_name"),
        ),
        "cn.dataset.broker_recommend": (("event_id",), ("month", "broker", "ts_code")),
        "cn.dataset.stk_surv": (("event_id",), ("ts_code", "surv_date", "rece_org")),
    }[dataset_id]
    return list(
        next(candidate for candidate in candidates if set(candidate) <= set(fields))
    )


def _catalog_rows(
    *,
    include_optional: bool = True,
    include_paused: bool = True,
) -> list[dict[str, Any]]:
    dataset_ids = list(PRIMARY_DATASET_IDS)
    if include_optional:
        dataset_ids.extend(OPTIONAL_DATASET_IDS)
    rows = [_catalog_row(dataset_id) for dataset_id in dataset_ids]
    if include_paused:
        rows.extend(_catalog_row(dataset_id) for dataset_id in PAUSED_DATASET_IDS)
    return rows


def _metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
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
        "receipt_id": "receipt-event-1",
        "data_through": "2026-07-31T10:20:00+08:00",
        "observed_at": "2026-07-31T10:25:00+08:00",
        "reasons": [],
    }
    metadata.update(overrides)
    return metadata


def _row(
    event_id: str = "event-1",
    *,
    symbol: str = "600000.SH",
    event_time: str = "2026-07-31T09:15:00+08:00",
    title: str = "公司公告签署重大合同",
    content: str = "合同金额增长，交付仍取决于客户验收。",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "ts_code": symbol,
        "event_time": event_time,
        "entity": "浦发银行",
        "title": title,
        "content": content,
        "url": "https://fixture.invalid/event-1",
        "source": "fixture-disclosure",
    }


class _Transport:
    def __init__(
        self,
        *,
        catalog_rows: list[dict[str, Any]] | None = None,
        rows_by_dataset: dict[str, list[dict[str, Any]]] | None = None,
        metadata: dict[str, Any] | None = None,
        query_status: int = 200,
        replay_drift: bool = False,
        catalog_responses: list[tuple[str, list[dict[str, Any]]]] | None = None,
        query_catalog_versions: list[str] | None = None,
    ) -> None:
        self.catalog_rows = catalog_rows or _catalog_rows()
        self.rows_by_dataset = rows_by_dataset or {
            dataset_id: [_row(event_id=f"{dataset_id}-1")]
            for dataset_id in (*PRIMARY_DATASET_IDS, *OPTIONAL_DATASET_IDS)
        }
        self.metadata = metadata or _metadata()
        self.query_status = query_status
        self.replay_drift = replay_drift
        self.catalog_responses = catalog_responses
        self.query_catalog_versions = query_catalog_versions or []
        self.calls: list[dict[str, Any]] = []
        self._traversal_by_dataset: dict[str, int] = {}
        self._catalog_call_count = 0
        self._query_call_count = 0
        self._current_catalog_version = CATALOG

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            if self.catalog_responses:
                index = min(
                    self._catalog_call_count,
                    len(self.catalog_responses) - 1,
                )
                catalog_version, catalog_rows = self.catalog_responses[index]
            else:
                catalog_version, catalog_rows = CATALOG, self.catalog_rows
            self._catalog_call_count += 1
            self._current_catalog_version = catalog_version
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": catalog_version,
                    "request_id": f"catalog-{len(self.calls)}",
                    "data": copy.deepcopy(catalog_rows),
                },
            )
        if self.query_status != 200:
            return HTTPResponse(self.query_status, {"error": "fixture-query-failure"})
        body = kwargs["json_body"]
        assert body is not None
        dataset_id = body["dataset_id"]
        rows = copy.deepcopy(self.rows_by_dataset.get(dataset_id, []))
        cursor = body.get("cursor")
        if cursor is None:
            traversal = self._traversal_by_dataset.get(dataset_id, 0)
            self._traversal_by_dataset[dataset_id] = traversal + 1
            index = 0
        else:
            traversal_text, index_text = cursor.split(":", 1)
            traversal = int(traversal_text)
            index = int(index_text)
        limit = int(body["limit"])
        page = rows[index : index + limit]
        if self.replay_drift and traversal == 1 and page:
            page[0]["title"] = f"{page[0]['title']} replay-drift"
        next_index = index + len(page)
        next_cursor = f"{traversal}:{next_index}" if next_index < len(rows) else None
        query_catalog_version = (
            self.query_catalog_versions[
                min(self._query_call_count, len(self.query_catalog_versions) - 1)
            ]
            if self.query_catalog_versions
            else self._current_catalog_version
        )
        self._query_call_count += 1
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": query_catalog_version,
                "request_id": f"query-{len(self.calls)}",
                "dataset_id": dataset_id,
                "data": page,
                "next_cursor": next_cursor,
                "metadata": copy.deepcopy(self.metadata),
            },
        )


class _ManualCatalogClient(SharedSignalsV1Client):
    """Fixture-only client for adapter tests below the generic catalog parser."""

    def __init__(
        self,
        transport: _Transport,
        catalogs: list[CatalogEnvelope],
    ) -> None:
        super().__init__(_client(transport).config, transport=transport)
        self._catalogs = list(catalogs)

    def get_catalog(self) -> CatalogEnvelope:
        if not self._catalogs:
            raise AssertionError("fixture catalog exhausted")
        catalog = self._catalogs.pop(0)
        self._observed_catalog_version = catalog.catalog_version
        return catalog


def _catalog_envelope(
    catalog_version: str,
    rows: list[dict[str, Any]],
) -> CatalogEnvelope:
    return CatalogEnvelope(
        api_version="v1",
        catalog_version=catalog_version,
        request_id=f"fixture-{catalog_version}",
        data=tuple(rows),
    )


def _client(
    transport: _Transport,
    *,
    configured_ids: frozenset[str] | None = None,
) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://event-evidence.fixture.invalid",
            expected_catalog_version=CATALOG,
            dataset_ids=configured_ids
            or frozenset((*PRIMARY_DATASET_IDS, *OPTIONAL_DATASET_IDS)),
            access_policy_id="ashare-event-fixture",
            catalog_version_policy="evidence_only",
            max_limit=100,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _profile_and_port(
    transport: _Transport | None = None,
) -> tuple[TradingDatasAshareEvidencePort, Any, AshareEvidenceAuditLedger, _Transport]:
    actual_transport = transport or _Transport()
    port = TradingDatasAshareEvidencePort(_client(actual_transport))
    audit = AshareEvidenceAuditLedger()
    profiles = port.freeze_profiles(audit_ledger=audit)
    return port, profiles.by_dataset["cn.dataset.anns_d"], audit, actual_transport


def test_profile_set_comes_only_from_catalog_and_never_uses_paused_fallbacks() -> None:
    transport = _Transport()
    port = TradingDatasAshareEvidencePort(_client(transport))
    audit = AshareEvidenceAuditLedger()

    profiles = port.freeze_profiles(audit_ledger=audit)

    assert set(profiles.by_dataset) == set(PRIMARY_DATASET_IDS) | set(
        OPTIONAL_DATASET_IDS
    )
    assert not set(PAUSED_DATASET_IDS).intersection(profiles.by_dataset)
    assert profiles.missing_optional == ()
    assert profiles.catalog_route == FIXED_CATALOG_ROUTE == "GET /v1/catalog"
    assert all(call["method"] == "GET" for call in transport.calls)
    assert audit.records() == ()


def test_unrelated_catalog_duplicate_does_not_block_event_profile_freeze() -> None:
    unrelated = _catalog_row(
        "cn.dataset.unrelated",
        fields=["id"],
        identity_fields=["id"],
    )
    transport = _Transport()
    client = _ManualCatalogClient(
        transport,
        [_catalog_envelope(CATALOG, [*_catalog_rows(), unrelated, unrelated])],
    )

    profiles = TradingDatasAshareEvidencePort(client).freeze_profiles(
        audit_ledger=AshareEvidenceAuditLedger()
    )

    assert set(PRIMARY_DATASET_IDS).issubset(profiles.by_dataset)


def test_audit_catalog_drift_is_derived_and_rejects_forged_value() -> None:
    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_audit_catalog_drift_mismatch",
    ):
        AshareEvidenceAuditRecord(
            reason_code="fixture_rejection",
            dataset_id="catalog",
            expected_catalog_version="expected-v1",
            observed_catalog_version="observed-v2",
            catalog_version_drift=False,
            dataset_contract_fingerprint=None,
            consumer_profile_sha256=None,
            decision_time=DECISION_TIME,
            rejected_payload_sha256="0" * 64,
        )


def test_missing_optional_profiles_are_explicit_degradation_not_fallback() -> None:
    transport = _Transport(
        catalog_rows=_catalog_rows(include_optional=False, include_paused=True)
    )
    port = TradingDatasAshareEvidencePort(
        _client(transport, configured_ids=frozenset(PRIMARY_DATASET_IDS))
    )

    profiles = port.freeze_profiles(audit_ledger=AshareEvidenceAuditLedger())

    assert set(profiles.by_dataset) == set(PRIMARY_DATASET_IDS)
    assert profiles.missing_optional == tuple(sorted(OPTIONAL_DATASET_IDS))
    assert profiles.complete_optional_coverage is False
    assert profiles.candidate_eligible is False


def test_first_profiles_accept_catalog_validated_provider_native_aliases() -> None:
    native_fields = {
        "cn.dataset.anns_d": [
            "ts_code",
            "ann_date",
            "title",
            "url",
            "rec_time",
            "name",
        ],
        "cn.dataset.cctv_news": ["date", "title", "content"],
        "cn.dataset.irm_qa_sh": ["ts_code", "trade_date", "pub_time", "q", "a", "name"],
        "cn.dataset.irm_qa_sz": ["ts_code", "trade_date", "pub_time", "q", "a", "name"],
        "cn.dataset.research_report": [
            "ts_code",
            "trade_date",
            "title",
            "inst_csname",
            "name",
        ],
        "cn.dataset.disclosure_date": [
            "ts_code",
            "end_date",
            "ann_date",
            "actual_date",
            "name",
        ],
        "cn.dataset.report_rc": [
            "ts_code",
            "report_date",
            "report_title",
            "org_name",
            "name",
        ],
        "cn.dataset.broker_recommend": ["month", "broker", "ts_code", "name"],
        "cn.dataset.stk_surv": [
            "ts_code",
            "surv_date",
            "rece_org",
            "content",
            "name",
        ],
    }
    rows = [
        _catalog_row(dataset_id, fields=fields)
        for dataset_id, fields in native_fields.items()
    ]
    transport = _Transport(catalog_rows=rows)
    port = TradingDatasAshareEvidencePort(_client(transport))

    profiles = port.freeze_profiles(audit_ledger=AshareEvidenceAuditLedger())

    assert set(profiles.by_dataset) == set(native_fields)
    assert profiles.by_dataset["cn.dataset.cctv_news"].symbol_field is None
    assert profiles.by_dataset["cn.dataset.cctv_news"].default_entity == "CN-MACRO"
    assert profiles.by_dataset["cn.dataset.irm_qa_sh"].title_field == "q"
    assert profiles.by_dataset["cn.dataset.irm_qa_sz"].content_field == "a"
    assert (
        profiles.by_dataset["cn.dataset.research_report"].source_field == "inst_csname"
    )
    assert profiles.by_dataset["cn.dataset.report_rc"].title_field == "report_title"
    assert profiles.by_dataset["cn.dataset.broker_recommend"].source_field == "broker"


def test_primary_profiles_match_real_tradingdatas_dataset_ids_and_fields() -> None:
    native_fields = {
        "cn.dataset.anns_d": [
            "ann_date",
            "ts_code",
            "name",
            "title",
            "url",
        ],
        "cn.dataset.cctv_news": ["date", "title", "content"],
        "cn.dataset.irm_qa_sh": [
            "ts_code",
            "name",
            "trade_date",
            "q",
            "a",
            "pub_time",
        ],
        "cn.dataset.irm_qa_sz": [
            "ts_code",
            "name",
            "trade_date",
            "q",
            "a",
            "pub_time",
            "industry",
        ],
        "cn.dataset.research_report": [
            "trade_date",
            "abstr",
            "title",
            "report_type",
            "author",
            "name",
            "ts_code",
            "inst_csname",
            "ind_name",
            "url",
        ],
    }
    transport = _Transport(
        catalog_rows=[
            _catalog_row(dataset_id, fields=fields)
            for dataset_id, fields in native_fields.items()
        ]
    )
    profiles = TradingDatasAshareEvidencePort(
        _client(transport, configured_ids=frozenset(PRIMARY_DATASET_IDS))
    ).freeze_profiles(audit_ledger=AshareEvidenceAuditLedger())

    assert set(PRIMARY_DATASET_IDS) == set(native_fields)
    assert all(
        dataset_id.startswith("cn.dataset.") for dataset_id in profiles.by_dataset
    )
    assert profiles.by_dataset["cn.dataset.anns_d"].identity_fields == (
        "ann_date",
        "ts_code",
        "url",
    )
    assert profiles.by_dataset["cn.dataset.cctv_news"].event_time_field == "date"
    assert profiles.by_dataset["cn.dataset.irm_qa_sh"].event_time_field == "pub_time"
    assert profiles.by_dataset["cn.dataset.irm_qa_sz"].content_field == "a"
    assert profiles.by_dataset["cn.dataset.research_report"].identity_fields == (
        "trade_date",
        "url",
    )


def test_anns_d_formal_identity_including_title_is_catalog_bound() -> None:
    rows = _catalog_rows()
    target_index = next(
        index
        for index, row in enumerate(rows)
        if row["dataset_id"] == "cn.dataset.anns_d"
    )
    rows[target_index] = _catalog_row(
        "cn.dataset.anns_d",
        fields=["ann_date", "ts_code", "name", "title", "url"],
        identity_fields=["ann_date", "ts_code", "title", "url"],
    )
    transport = _Transport(catalog_rows=rows)

    profiles = TradingDatasAshareEvidencePort(
        _client(transport, configured_ids=frozenset(PRIMARY_DATASET_IDS))
    ).freeze_profiles(audit_ledger=AshareEvidenceAuditLedger())

    assert profiles.by_dataset["cn.dataset.anns_d"].identity_fields == (
        "ann_date",
        "ts_code",
        "title",
        "url",
    )


@pytest.mark.parametrize(
    ("dataset_id", "fields", "row", "expected_event_time", "expected_source"),
    [
        (
            "cn.dataset.anns_d",
            ["ann_date", "ts_code", "name", "title", "url"],
            {
                "ann_date": "20260731",
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "title": "重大合同公告",
                "url": "https://fixture.invalid/anns-d.pdf",
            },
            "20260731",
            "cn.dataset.anns_d",
        ),
        (
            "cn.dataset.cctv_news",
            ["date", "title", "content"],
            {
                "date": "20260731",
                "title": "新闻联播",
                "content": "宏观政策与产业信息。",
            },
            "20260731",
            "cn.dataset.cctv_news",
        ),
        (
            "cn.dataset.irm_qa_sh",
            ["ts_code", "name", "trade_date", "q", "a", "pub_time"],
            {
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "trade_date": "20260731",
                "q": "项目进展如何？",
                "a": "项目按计划推进。",
                "pub_time": "2026-07-31 10:00:00",
            },
            "2026-07-31T10:00:00+08:00",
            "cn.dataset.irm_qa_sh",
        ),
        (
            "cn.dataset.research_report",
            [
                "trade_date",
                "abstr",
                "title",
                "report_type",
                "author",
                "name",
                "ts_code",
                "inst_csname",
                "ind_name",
                "url",
            ],
            {
                "trade_date": "20260731",
                "abstr": "盈利预测保持稳定。",
                "title": "公司研究报告",
                "report_type": "个股研报",
                "author": "研究员",
                "name": "浦发银行",
                "ts_code": "600000.SH",
                "inst_csname": "示例证券",
                "ind_name": "银行",
                "url": "https://fixture.invalid/report.pdf",
            },
            "20260731",
            "示例证券",
        ),
    ],
)
def test_provider_native_primary_rows_are_shadow_mapped_without_authority(
    dataset_id: str,
    fields: list[str],
    row: dict[str, Any],
    expected_event_time: str,
    expected_source: str,
) -> None:
    catalog_rows = []
    rows_by_dataset = {}
    for primary_id in PRIMARY_DATASET_IDS:
        selected_fields = fields if primary_id == dataset_id else GENERIC_FIELDS
        catalog_rows.append(_catalog_row(primary_id, fields=selected_fields))
        rows_by_dataset[primary_id] = (
            [row] if primary_id == dataset_id else [_row(f"{primary_id}-1")]
        )
    transport = _Transport(
        catalog_rows=catalog_rows,
        rows_by_dataset=rows_by_dataset,
    )
    port = TradingDatasAshareEvidencePort(
        _client(transport, configured_ids=frozenset(PRIMARY_DATASET_IDS))
    )
    audit = AshareEvidenceAuditLedger()
    profile = port.freeze_profiles(audit_ledger=audit).by_dataset[dataset_id]

    event = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).events[0]

    assert event.dataset_id == dataset_id
    assert event.event_time == expected_event_time
    assert event.source == expected_source
    assert event.candidate_eligible is False
    assert event.execution_eligible is False
    assert event.training_eligible is False
    assert event.real_trading_enabled is False


def test_event_snapshot_maps_provider_row_and_binds_envelope_availability() -> None:
    port, profile, audit, transport = _profile_and_port()

    snapshot = port.load_event_snapshot(
        profile=profile,
        filters={"ts_code": {"eq": "600000.SH"}},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    )

    assert snapshot.same_observation is True
    assert snapshot.query_route == FIXED_QUERY_ROUTE == "POST /v1/query"
    assert len(snapshot.events) == 1
    event = snapshot.events[0]
    assert event.symbol == "600000.SH"
    assert event.event_time == "2026-07-31T09:15:00+08:00"
    assert event.event_time_precision == "instant"
    assert event.available_at.isoformat() == "2026-07-31T10:25:00+08:00"
    assert event.available_at_source == "query_envelope.metadata.observed_at"
    assert event.receipt_id == "receipt-event-1"
    assert event.source_lineage_sha256
    assert event.evidence_ref.startswith("td-v1:")
    assert event.execution_eligible is False
    assert event.training_eligible is False
    assert event.promotion_eligible is False
    assert event.real_trading_enabled is False
    assert [call["method"] for call in transport.calls] == [
        "GET",
        "GET",
        "POST",
        "POST",
    ]


def test_unrelated_global_catalog_drift_passes_with_target_fingerprint_binding() -> (
    None
):
    initial_rows = _catalog_rows()
    current_rows = copy.deepcopy(initial_rows)
    current_rows.append(
        _catalog_row(
            "cn.dataset.unrelated",
            fields=["id"],
            identity_fields=["id"],
        )
    )
    transport = _Transport(
        catalog_responses=[
            (CATALOG, initial_rows),
            ("fixture-global-catalog-v2", current_rows),
        ]
    )
    port, profile, audit, _ = _profile_and_port(transport)

    snapshot = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    )

    assert snapshot.expected_catalog_version == CATALOG
    assert snapshot.observed_catalog_version == "fixture-global-catalog-v2"
    assert snapshot.catalog_version_drift is True
    assert snapshot.dataset_contract_fingerprint == profile.dataset_contract_fingerprint
    assert snapshot.consumer_profile_sha256 == profile.consumer_profile_sha256
    assert audit.records() == ()


def test_catalog_to_query_version_drift_fails_closed_with_full_audit_evidence() -> None:
    rows = _catalog_rows()
    transport = _Transport(
        catalog_responses=[(CATALOG, rows), ("fixture-global-catalog-v2", rows)],
        query_catalog_versions=["fixture-query-catalog-v3"],
    )
    port, profile, audit, _ = _profile_and_port(transport)

    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_query_failed",
    ):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    record = audit.records()[-1]
    assert record.expected_catalog_version == CATALOG
    assert record.observed_catalog_version == "fixture-global-catalog-v2"
    assert record.catalog_version_drift is True
    assert record.dataset_contract_fingerprint == profile.dataset_contract_fingerprint
    assert record.consumer_profile_sha256 == profile.consumer_profile_sha256


@pytest.mark.parametrize(
    "field_name",
    (
        "dataset_id",
        "schema_major",
        "default_fields",
        "filter_operators",
        "default_order",
        "limits",
        "identity_fields",
    ),
)
def test_target_catalog_contract_seven_field_drift_fails_before_query(
    field_name: str,
) -> None:
    initial_rows = _catalog_rows()
    current_rows = copy.deepcopy(initial_rows)
    target = next(
        row for row in current_rows if row["dataset_id"] == "cn.dataset.anns_d"
    )
    replacements: dict[str, Any] = {
        "dataset_id": "cn.dataset.anns_d.drifted",
        "schema_major": 2,
        "default_fields": [*target["default_fields"], "contract_drift"],
        "filter_operators": {
            **target["filter_operators"],
            "event_id": ["eq"],
        },
        "default_order": ["event_id:desc"],
        "limits": {"max_page_size": 1},
        "identity_fields": ["ts_code", "event_id"],
    }
    target[field_name] = replacements[field_name]
    transport = _Transport(
        catalog_responses=[
            (CATALOG, initial_rows),
            ("fixture-global-catalog-v2", current_rows),
        ]
    )
    port, profile, audit, _ = _profile_and_port(transport)

    with pytest.raises(AshareEvidenceContractError):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert not [call for call in transport.calls if call["method"] == "POST"]
    assert audit.records()[-1].dataset_contract_fingerprint == (
        profile.dataset_contract_fingerprint
    )


def test_catalog_identity_order_mismatch_fails_before_any_query() -> None:
    rows = _catalog_rows()
    target = next(row for row in rows if row["dataset_id"] == "cn.dataset.anns_d")
    target["identity_fields"] = ["ts_code", "event_id"]
    transport = _Transport(catalog_rows=rows)

    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_catalog_identity_mismatch",
    ):
        TradingDatasAshareEvidencePort(_client(transport)).freeze_profiles(
            audit_ledger=AshareEvidenceAuditLedger()
        )

    assert not [call for call in transport.calls if call["method"] == "POST"]


@pytest.mark.parametrize("mode", ("missing", "duplicate"))
def test_target_catalog_row_missing_or_duplicate_fails_before_query(mode: str) -> None:
    initial_rows = _catalog_rows()
    current_rows = copy.deepcopy(initial_rows)
    if mode == "missing":
        current_rows = [
            row for row in current_rows if row["dataset_id"] != "cn.dataset.anns_d"
        ]
    else:
        current_rows.append(
            copy.deepcopy(
                next(
                    row
                    for row in current_rows
                    if row["dataset_id"] == "cn.dataset.anns_d"
                )
            )
        )
    transport = _Transport()
    client = _ManualCatalogClient(
        transport,
        [
            _catalog_envelope(CATALOG, initial_rows),
            _catalog_envelope("fixture-global-catalog-v2", current_rows),
        ],
    )
    port = TradingDatasAshareEvidencePort(client)
    audit = AshareEvidenceAuditLedger()
    profile = port.freeze_profiles(audit_ledger=audit).by_dataset["cn.dataset.anns_d"]

    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_dataset_catalog_row_missing",
    ):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert not [call for call in transport.calls if call["method"] == "POST"]


def test_pagination_is_bounded_and_replayed_with_exact_identity() -> None:
    rows = [_row("event-1"), _row("event-2", title="业绩预增")]
    transport = _Transport(rows_by_dataset={"cn.dataset.anns_d": rows})
    port, profile, audit, _ = _profile_and_port(transport)

    snapshot = port.load_event_snapshot(
        profile=replace(profile, page_limit=1, max_pages=2, max_rows=2),
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    )

    assert snapshot.page_count == 2
    assert snapshot.row_count == 2
    assert len(snapshot.events) == 2
    post_calls = [call for call in transport.calls if call["method"] == "POST"]
    assert len(post_calls) == 4
    assert all(call["url"].endswith("/v1/query") for call in post_calls)


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata(state="failed"), "ashare_evidence_metadata_not_ready"),
        (_metadata(degraded=True), "ashare_evidence_metadata_not_ready"),
        (
            _metadata(freshness={"state": "stale", "stale": True}),
            "ashare_evidence_metadata_not_fresh",
        ),
        (
            _metadata(quality={"state": "invalid"}),
            "ashare_evidence_metadata_quality_invalid",
        ),
        (
            _metadata(lineage={"complete": False}),
            "ashare_evidence_metadata_lineage_incomplete",
        ),
    ],
)
def test_impaired_metadata_fails_closed_to_audit_only(
    metadata: dict[str, Any],
    reason: str,
) -> None:
    port, profile, audit, _ = _profile_and_port(_Transport(metadata=metadata))

    with pytest.raises(AshareEvidenceContractError, match=reason):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert [record.reason_code for record in audit.records()] == [reason]
    assert all(record.candidate_eligible is False for record in audit.records())
    assert all(record.execution_eligible is False for record in audit.records())


def test_query_http_failure_has_one_attempt_and_is_audit_only() -> None:
    transport = _Transport(query_status=429)
    port, profile, audit, _ = _profile_and_port(transport)

    with pytest.raises(
        AshareEvidenceContractError, match="ashare_evidence_query_failed"
    ):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    post_calls = [call for call in transport.calls if call["method"] == "POST"]
    assert len(post_calls) == 1
    assert audit.records()[0].reason_code == "ashare_evidence_query_failed"


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata(receipt_id=None),
        _metadata(lineage=None),
        _metadata(observed_at="2026-07-31T10:25:00"),
    ],
)
def test_missing_receipt_lineage_or_aware_availability_fails_closed(
    metadata: dict[str, Any],
) -> None:
    transport = _Transport(metadata=metadata)
    port, profile, audit, _ = _profile_and_port(transport)

    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_query_failed",
    ):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert audit.records()[0].reason_code == "ashare_evidence_query_failed"
    assert len([call for call in transport.calls if call["method"] == "POST"]) == 1


def test_replay_drift_and_duplicate_identity_fail_closed() -> None:
    replay_transport = _Transport(replay_drift=True)
    port, profile, audit, _ = _profile_and_port(replay_transport)
    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_same_observation_mismatch",
    ):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    duplicate_transport = _Transport(
        rows_by_dataset={"cn.dataset.anns_d": [_row("duplicate"), _row("duplicate")]}
    )
    port, profile, audit, _ = _profile_and_port(duplicate_transport)
    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_pagination_failed",
    ):
        port.load_event_snapshot(
            profile=replace(profile, page_limit=1),
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            _row(event_time="2026-07-31T10:26:00+08:00"),
            "ashare_evidence_event_time_after_availability",
        ),
        (
            _row(symbol="300001.SZ"),
            "ashare_evidence_symbol_outside_mainboard_scope",
        ),
        (
            _row(symbol="688001.SH"),
            "ashare_evidence_symbol_outside_mainboard_scope",
        ),
    ],
)
def test_time_and_symbol_scope_attacks_fail_closed(
    row: dict[str, Any],
    reason: str,
) -> None:
    transport = _Transport(rows_by_dataset={"cn.dataset.anns_d": [row]})
    port, profile, audit, _ = _profile_and_port(transport)

    with pytest.raises(AshareEvidenceContractError, match=reason):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert audit.records()[0].reason_code == reason


def test_full_market_snapshot_can_be_scoped_to_frozen_mainboard_allowlist() -> None:
    transport = _Transport(
        rows_by_dataset={
            "cn.dataset.anns_d": [
                _row("mainboard", symbol="600000.SH"),
                _row(
                    "chinext",
                    symbol="300001.SZ",
                    event_time="2026-07-31T10:26:00+08:00",
                ),
                _row("star", symbol="688001.SH"),
                _row("missing", symbol=""),
            ]
        }
    )
    port, profile, audit, _ = _profile_and_port(transport)

    snapshot = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
        allowed_symbols=("600000.SH",),
    )

    assert snapshot.row_count == 1
    assert [event.symbol for event in snapshot.events] == ["600000.SH"]
    assert snapshot.same_observation is True
    assert snapshot.candidate_eligible is False
    assert snapshot.execution_eligible is False
    assert snapshot.training_eligible is False
    assert snapshot.real_trading_enabled is False
    assert audit.records() == ()


@pytest.mark.parametrize(
    ("allowed_symbols", "reason"),
    [
        ((), "ashare_evidence_allowed_symbols_invalid"),
        (
            ("300001.SZ",),
            "ashare_evidence_allowed_symbol_outside_mainboard_scope",
        ),
        (
            ("600000.SH", "600000.SH"),
            "ashare_evidence_allowed_symbols_duplicate",
        ),
    ],
)
def test_invalid_event_symbol_allowlist_fails_closed(
    allowed_symbols: tuple[str, ...],
    reason: str,
) -> None:
    port, profile, audit, transport = _profile_and_port()
    calls_before = len(transport.calls)

    with pytest.raises(AshareEvidenceContractError, match=reason):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
            allowed_symbols=allowed_symbols,
        )

    assert audit.records()[0].reason_code == reason
    assert len(transport.calls) == calls_before


def test_allowlist_with_no_matching_events_remains_fail_closed() -> None:
    port, profile, audit, _ = _profile_and_port()

    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_query_returned_no_rows",
    ):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
            allowed_symbols=("600519.SH",),
        )

    assert audit.records()[0].reason_code == "ashare_evidence_query_returned_no_rows"


def test_future_observed_at_and_date_only_event_do_not_fake_pit() -> None:
    future = _Transport(metadata=_metadata(observed_at="2026-07-31T10:31:00+08:00"))
    port, profile, audit, _ = _profile_and_port(future)
    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_available_after_decision",
    ):
        port.load_event_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    date_only = _Transport(
        rows_by_dataset={"cn.dataset.anns_d": [_row(event_time="20260731")]}
    )
    port, profile, audit, _ = _profile_and_port(date_only)
    snapshot = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    )
    assert snapshot.events[0].event_time_precision == "date"
    assert snapshot.events[0].event_time_instant_proven is False
    assert snapshot.events[0].historical_known_time_proven is False
    assert snapshot.events[0].pit_feature_eligible is False


def test_event_instant_does_not_claim_historical_known_time_or_pit_authority() -> None:
    port, profile, audit, _ = _profile_and_port()

    event = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).events[0]

    assert event.event_time_instant_proven is True
    assert event.historical_known_time_proven is False
    assert event.pit_feature_eligible is False


def test_deterministic_sentiment_deweights_missing_evidence_and_is_not_probability() -> (
    None
):
    port, profile, audit, _ = _profile_and_port()
    base = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).events[0]
    events = tuple(
        replace(
            base,
            dataset_id=dataset_id,
            title=f"{dataset_id} 业绩增长",
            content="订单增长，风险可控。",
            evidence_ref=(
                f"td-v1:{dataset_id}:{base.receipt_id}:{base.source_row_sha256[:16]}"
            ),
        )
        for dataset_id in PRIMARY_DATASET_IDS
    )

    full = build_sentiment_snapshot(
        events=events,
        decision_time=DECISION_TIME,
    )
    partial = build_sentiment_snapshot(
        events=events[:1],
        decision_time=DECISION_TIME,
    )

    assert full.coverage_weight == 1.0
    assert partial.coverage_weight == pytest.approx(1 / (len(PRIMARY_DATASET_IDS) - 1))
    assert abs(partial.shadow_score) < abs(full.shadow_score)
    assert partial.missing_dataset_ids
    assert partial.not_applicable_dataset_ids == ("cn.dataset.irm_qa_sz",)
    assert partial.score_semantics == "deterministic_shadow_score_not_probability"
    assert partial.calibrated_probability is None
    assert partial.candidate_eligible is False
    assert partial.execution_eligible is False
    assert partial.counterfactual_only is True


def test_sentiment_snapshot_binds_existing_decision_ledger_as_shadow_only() -> None:
    port, profile, audit, _ = _profile_and_port()
    event = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).events[0]
    sentiment = build_sentiment_snapshot(
        events=(event,),
        decision_time=DECISION_TIME,
    )
    ledger = InMemoryDecisionLedger()

    record = bind_shadow_decision(
        ledger=ledger,
        sentiment=sentiment,
        decision_id="event-shadow-1",
        decision_time=DECISION_TIME,
    )

    assert record.disposition is ExposureDisposition.SHADOW_ONLY
    assert record.symbol == "600000.SH"
    assert record.requested_notional_cny == 0
    assert record.filled_quantity == 0
    assert record.capital_layer == "simulated"
    assert record.account_type == "simulated"
    assert record.real_trading_enabled is False
    assert ledger.records() == (record,)


def test_llm_projection_reuses_offline_evidence_artifact_without_invocation() -> None:
    port, profile, audit, transport = _profile_and_port()
    event = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).events[0]
    call_count = len(transport.calls)

    request = build_llm_shadow_request(
        event=event,
        request_id="ashare-event-shadow-1",
        document_cutoff=DECISION_TIME,
    )

    assert len(transport.calls) == call_count
    assert request.task_type == "event_evidence_extraction"
    assert request.route == "bulk_extraction"
    assert len(request.artifacts) == 1
    assert request.artifacts[0].available_at == event.available_at.isoformat()
    assert request.artifacts[0].source_authority_receipt is None
    assert request.payload["event_type"] == "provider_neutral_event_evidence"
    assert request.payload["research_scores"]["event"]["state"] == "shadow_only"
    assert "execution_authority" not in request.payload
    with pytest.raises(
        EvidenceArtifactError,
        match="external_source_authority_receipt_required",
    ):
        request.validate_for_transport(
            "deepseek-chat",
            source_authority_verifier=lambda **_: True,
        )


def test_date_only_event_is_not_projected_as_an_instant_for_llm() -> None:
    transport = _Transport(
        rows_by_dataset={"cn.dataset.anns_d": [_row(event_time="20260731")]}
    )
    port, profile, audit, _ = _profile_and_port(transport)
    event = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).events[0]

    with pytest.raises(
        AshareEvidenceContractError,
        match="ashare_evidence_llm_event_time_not_instant",
    ):
        build_llm_shadow_request(
            event=event,
            request_id="ashare-event-shadow-date-only",
            document_cutoff=DECISION_TIME,
        )


def test_module_has_no_legacy_route_storage_runtime_or_authority_escape() -> None:
    import Ashare.event_evidence as module

    source = inspect.getsource(module)
    forbidden = (
        "/tushare",
        "/source_status",
        "sqlite3",
        "SharedSignalsReader",
        "sharedsignals.db",
        "broker_order",
        "REAL_TRADING_ENABLED=true",
        "execution_eligible=True",
        "training_eligible=True",
        "promotion_eligible=True",
    )
    assert all(value not in source for value in forbidden)
    assert source.count('FIXED_CATALOG_ROUTE = "GET /v1/catalog"') == 1
    assert source.count('FIXED_QUERY_ROUTE = "POST /v1/query"') == 1


def test_authority_flags_remain_false_across_event_sentiment_and_audit() -> None:
    port, profile, audit, _ = _profile_and_port()
    event = port.load_event_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).events[0]
    sentiment = build_sentiment_snapshot(
        events=(event,),
        decision_time=DECISION_TIME,
    )
    values = [event.canonical_payload(), sentiment.canonical_payload()]
    forbidden_true = {
        "candidate_eligible",
        "execution_eligible",
        "training_eligible",
        "promotion_eligible",
        "execution_authority",
        "risk_authority",
        "position_authority",
        "real_trading_enabled",
    }
    for payload in values:
        assert all(payload[key] is False for key in forbidden_true)
        assert "calibrated_probability" in payload
        assert payload["calibrated_probability"] is None


@pytest.mark.parametrize(
    ("filters", "reason"),
    [
        (
            {"provider_private": {"eq": "x"}},
            "ashare_evidence_filter_not_catalog_allowed",
        ),
        (
            {"ts_code": {"contains": "600000"}},
            "ashare_evidence_filter_operator_not_catalog_allowed",
        ),
        (
            {"ts_code": "600000.SH"},
            "ashare_evidence_filter_expression_invalid",
        ),
    ],
)
def test_query_filters_are_frozen_by_catalog(
    filters: dict[str, Any],
    reason: str,
) -> None:
    port, profile, audit, transport = _profile_and_port()
    calls_before = len(transport.calls)

    with pytest.raises(AshareEvidenceContractError, match=reason):
        port.load_event_snapshot(
            profile=profile,
            filters=filters,
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert not [
        call for call in transport.calls[calls_before:] if call["method"] == "POST"
    ]
    assert audit.records()[0].reason_code == reason
