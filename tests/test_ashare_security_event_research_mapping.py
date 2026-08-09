from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from Ashare.event_evidence import (
    EventEvidenceSnapshot,
    EventEvidenceSnapshotBatch,
    EvidenceDatasetProfile,
)
from Ashare.security_event_research_mapping import (
    SecurityEventResearchMappingError,
    build_security_event_research_mapping,
)
from shared.data.research_snapshot import ResearchDataSnapshot, ResearchDatasetSnapshot


DECISION_TIME = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
CATALOG_VERSION = "catalog-security-v1"


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _research_snapshot() -> ResearchDataSnapshot:
    rows = [
        {
            "ts_code": "600000.SH",
            "name": "浦发银行",
            "list_status": "L",
            "list_date": "19991110",
        },
        {
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "list_status": "L",
            "list_date": "19910403",
        },
    ]
    master = ResearchDatasetSnapshot(
        dataset_id="cn.equity.security_master",
        role="required_execution",
        api_version="v1",
        catalog_version=CATALOG_VERSION,
        request_id="request-security",
        receipt_id="receipt:security-master",
        evidence_state="ready",
        evidence_action="accept",
        eligible=True,
        weight=1.0,
        reasons=(),
        source_proof_complete=True,
        lineage_sha256="1" * 64,
        source_proof_sha256="2" * 64,
        data_through="2026-08-09T11:00:00+00:00",
        observed_at="2026-08-09T11:05:00+00:00",
        next_cursor=None,
        row_count=2,
        observation_mode="current_observation",
        historical_pit_eligible=False,
        query_as_of_mode="omit",
        minimum_row_count=1,
        max_pages=20,
        max_rows=10_000,
        identity_fields=("ts_code",),
        row_event_time_field=None,
        row_event_time_format=None,
        row_event_timezone=None,
        row_event_time_semantic=None,
        identity_sha256="3" * 64,
        row_observation_sha256="4" * 64,
        max_row_observed_at="2026-08-09T11:05:00+00:00",
        max_row_event_value=None,
        page_count=1,
        pagination_trace_sha256="5" * 64,
        pagination_semantic_sha256="6" * 64,
        page_request_set_sha256="7" * 64,
        page_response_set_sha256="8" * 64,
        cursor_chain_sha256="9" * 64,
        response_sha256="a" * 64,
        _rows_json=json.dumps(rows, ensure_ascii=False),
    )
    profile_sha = "b" * 64
    payload = {
        "profile_id": "ashare-security-current-observation-v1",
        "profile_contract_sha256": profile_sha,
        "catalog_version": CATALOG_VERSION,
        "decision_as_of": "2026-08-09T11:10:00+00:00",
        "datasets": [
            {
                "dataset_id": master.dataset_id,
                "role": master.role,
                "response_sha256": master.response_sha256,
            }
        ],
        "blocking_reasons": [],
    }
    return ResearchDataSnapshot(
        profile_id=payload["profile_id"],
        profile_contract_sha256=profile_sha,
        catalog_version=CATALOG_VERSION,
        decision_as_of=payload["decision_as_of"],
        datasets=(master,),
        execution_eligible=True,
        historical_pit_eligible=False,
        blocking_reasons=(),
        snapshot_sha256=_sha(payload),
    )


def _profile(dataset_id: str) -> EvidenceDatasetProfile:
    if dataset_id == "cn.dataset.anns_d":
        fields = ("ann_date", "ts_code", "title", "url")
        identity = fields
        event_time = "ann_date"
        symbol_field = "ts_code"
        entity_field = None
        title_field = "title"
        content_field = None
        url_field = "url"
        source_field = None
        default_entity = None
    elif dataset_id == "cn.dataset.cctv_news":
        fields = ("date", "title", "content")
        identity = ("date", "title")
        event_time = "date"
        symbol_field = None
        entity_field = None
        title_field = "title"
        content_field = "content"
        url_field = None
        source_field = None
        default_entity = "CN-MACRO"
    else:
        fields = ("trade_date", "ts_code", "title", "inst_csname", "url")
        identity = ("trade_date", "url")
        event_time = "trade_date"
        symbol_field = "ts_code"
        entity_field = None
        title_field = "title"
        content_field = None
        url_field = "url"
        source_field = "inst_csname"
        default_entity = None
    return EvidenceDatasetProfile(
        expected_catalog_version="v1-0e8833239222d9ae",
        observed_catalog_version="v1-0e8833239222d9ae",
        dataset_id=dataset_id,
        schema_major=2,
        default_fields=fields,
        default_order=(f"{event_time}:asc",),
        filter_operators=((event_time, ("eq", "gte", "lte")),),
        dataset_contract_fingerprint=_sha([dataset_id, "contract"]),
        consumer_profile_sha256=_sha([dataset_id, "consumer"]),
        identity_fields=identity,
        event_time_field=event_time,
        symbol_field=symbol_field,
        entity_field=entity_field,
        title_field=title_field,
        content_field=content_field,
        url_field=url_field,
        source_field=source_field,
        default_entity=default_entity,
        optional_dataset=False,
        max_pages=4,
        max_rows=500,
        page_limit=100,
    )


def _batch(
    dataset_id: str,
    *,
    symbol: str | None,
    receipt_suffix: str,
) -> EventEvidenceSnapshotBatch:
    profile = _profile(dataset_id)
    source_row_sha256 = _sha([dataset_id, symbol])
    receipt_id = f"receipt:{receipt_suffix}"
    event = EventEvidenceSnapshot(
        dataset_id=dataset_id,
        catalog_version=profile.catalog_version,
        event_time="20260809",
        event_time_precision="date",
        as_of=DECISION_TIME - timedelta(minutes=5),
        data_through=DECISION_TIME - timedelta(minutes=20),
        available_at=DECISION_TIME - timedelta(minutes=10),
        available_at_source="query_envelope.metadata.observed_at",
        entity=symbol or "CN-MACRO",
        symbol=symbol,
        title=f"{dataset_id} evidence",
        content="macro context" if symbol is None else None,
        url=None if symbol is None else f"https://example.invalid/{receipt_suffix}",
        source="fixture",
        receipt_id=receipt_id,
        source_lineage_sha256=_sha([dataset_id, "lineage"]),
        source_row_sha256=source_row_sha256,
        envelope_proof_sha256=_sha([dataset_id, "proof"]),
        evidence_ref=(
            f"td-v1:{dataset_id}:{receipt_id}:{source_row_sha256[:16]}"
        ),
        evidence_confidence=0.5,
        event_time_instant_proven=False,
        historical_known_time_proven=False,
        pit_feature_eligible=False,
    )
    return EventEvidenceSnapshotBatch(
        profile=profile,
        events=(event,),
        page_count=1,
        row_count=1,
        pagination_trace_sha256=_sha([dataset_id, "pages"]),
        first_semantic_sha256=_sha([dataset_id, "semantic"]),
        replay_semantic_sha256=_sha([dataset_id, "semantic"]),
        same_observation=True,
    )


def _batches() -> tuple[EventEvidenceSnapshotBatch, ...]:
    return (
        _batch("cn.dataset.anns_d", symbol="600000.SH", receipt_suffix="5fb53248"),
        _batch("cn.dataset.cctv_news", symbol=None, receipt_suffix="2427a219"),
        _batch(
            "cn.dataset.research_report",
            symbol="000001.SZ",
            receipt_suffix="34a2d088",
        ),
    )


def _mapping(**overrides: object) -> dict[str, object]:
    values = {
        "research_snapshot": _research_snapshot(),
        "event_batches": _batches(),
        "symbols": ("600000.SH", "000001.SZ"),
        "blocked_dataset_reasons": {
            "cn.dataset.irm_qa_sh": "ashare_evidence_metadata_not_ready",
            "cn.dataset.irm_qa_sz": "ashare_evidence_metadata_not_ready",
        },
        "decision_time": DECISION_TIME,
    }
    values.update(overrides)
    return build_security_event_research_mapping(**values)  # type: ignore[arg-type]


def test_mapping_binds_security_event_and_context_receipts() -> None:
    mapping = _mapping()

    assert mapping["historicalPitEligible"] is False
    assert mapping["coverage"] == {
        "symbolCount": 2,
        "acceptedEventCount": 3,
        "acceptedDatasetIds": [
            "cn.dataset.anns_d",
            "cn.dataset.cctv_news",
            "cn.dataset.research_report",
        ],
        "blockedDatasetIds": ["cn.dataset.irm_qa_sh", "cn.dataset.irm_qa_sz"],
        "blockedDatasetReasons": {
            "cn.dataset.irm_qa_sh": "ashare_evidence_metadata_not_ready",
            "cn.dataset.irm_qa_sz": "ashare_evidence_metadata_not_ready",
        },
    }
    by_symbol = {item["symbol"]: item for item in mapping["securities"]}
    assert by_symbol["600000.SH"]["events"][0]["receipt_id"] == "receipt:5fb53248"
    assert by_symbol["000001.SZ"]["events"][0]["receipt_id"] == "receipt:34a2d088"
    assert mapping["contextEvents"][0]["receipt_id"] == "receipt:2427a219"
    security_source = by_symbol["600000.SH"]["securityMasterSource"]
    assert security_source["receiptId"] == "receipt:security-master"
    assert security_source["profileContractSha256"] == "b" * 64
    assert all(
        source["sameObservation"] is True
        and source["firstSemanticSha256"] == source["replaySemanticSha256"]
        for source in mapping["sourceCoverage"]
    )
    assert mapping["mappingSha256"]
    assert mapping["executionAuthority"] is False
    assert mapping["realTradingEnabled"] is False


def test_mapping_requires_every_primary_dataset_to_be_accepted_or_blocked() -> None:
    with pytest.raises(
        SecurityEventResearchMappingError,
        match="ashare_research_mapping_coverage_invalid",
    ):
        _mapping(blocked_dataset_reasons={"cn.dataset.irm_qa_sh": "not_ready"})


def test_mapping_rejects_event_outside_the_frozen_universe() -> None:
    batches = list(_batches())
    batches[0] = _batch(
        "cn.dataset.anns_d", symbol="600001.SH", receipt_suffix="foreign"
    )
    with pytest.raises(
        SecurityEventResearchMappingError,
        match="ashare_research_mapping_event_symbol_outside_universe",
    ):
        _mapping(event_batches=tuple(batches))


def test_mapping_rejects_security_master_without_receipt() -> None:
    snapshot = _research_snapshot()
    master = replace(snapshot.datasets[0], receipt_id=None)
    snapshot = replace(snapshot, datasets=(master,))
    with pytest.raises(
        SecurityEventResearchMappingError,
        match="ashare_research_mapping_security_receipt_invalid",
    ):
        _mapping(research_snapshot=snapshot)
