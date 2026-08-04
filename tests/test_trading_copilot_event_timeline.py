from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import Ashare.event_evidence as event_evidence
from Ashare.event_evidence import (
    AshareEvidenceContractError,
    EventEvidenceSnapshot,
    EventEvidenceSnapshotBatch,
    EvidenceDatasetProfile,
    load_event_evidence_batch_artifact,
    write_event_evidence_batch_artifact,
)
from Ashare.trading_copilot_observation_worker import TradingCopilotObservationError
from Ashare.trading_copilot_event_timeline import (
    build_event_timeline_batch,
    publish_retained_event_timeline,
)


def _batch() -> EventEvidenceSnapshotBatch:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    profile = EvidenceDatasetProfile(
        expected_catalog_version="catalog-v1",
        observed_catalog_version="catalog-v1",
        dataset_id="cn.dataset.anns_d",
        schema_major=2,
        default_fields=("ann_date", "ts_code", "title", "url"),
        default_order=("ann_date:asc",),
        filter_operators=(("ts_code", ("in",)),),
        dataset_contract_fingerprint="a" * 64,
        consumer_profile_sha256="b" * 64,
        identity_fields=("ann_date", "ts_code", "title", "url"),
        event_time_field="ann_date",
        symbol_field="ts_code",
        entity_field=None,
        title_field="title",
        content_field=None,
        url_field="url",
        source_field=None,
        default_entity=None,
        optional_dataset=False,
        max_pages=2,
        max_rows=20,
        page_limit=10,
    )
    receipt_id = "receipt:" + "c" * 64
    source_row_sha256 = "e" * 64
    event = EventEvidenceSnapshot(
        dataset_id="cn.dataset.anns_d",
        catalog_version="catalog-v1",
        event_time="20260804",
        event_time_precision="date",
        as_of=now,
        data_through=now - timedelta(hours=1),
        available_at=now - timedelta(minutes=30),
        available_at_source="query_envelope.metadata.observed_at",
        entity="600000.SH",
        symbol="600000.SH",
        title="fixture announcement",
        content=None,
        url="https://example.invalid/announcement",
        source="fixture",
        receipt_id=receipt_id,
        source_lineage_sha256="d" * 64,
        source_row_sha256=source_row_sha256,
        envelope_proof_sha256="f" * 64,
        evidence_ref=f"td-v1:cn.dataset.anns_d:{receipt_id}:{source_row_sha256[:16]}",
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
        pagination_trace_sha256="1" * 64,
        first_semantic_sha256="2" * 64,
        replay_semantic_sha256="2" * 64,
        same_observation=True,
    )


def test_retained_batch_is_receipt_bound_and_consumable_by_existing_timeline(
    tmp_path,
) -> None:
    artifact = tmp_path / "events.json"
    batch = _batch()

    write_event_evidence_batch_artifact(batch=batch, path=artifact)
    retained = load_event_evidence_batch_artifact(artifact)

    with pytest.raises(AshareEvidenceContractError, match="path_invalid"):
        write_event_evidence_batch_artifact(batch=batch, path=artifact)

    timeline = build_event_timeline_batch(
        symbols=("600000.SH",),
        events=retained.events,
        blocked_dataset_reasons={},
        generated_at=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 8, 4, 13, 1, tzinfo=timezone.utc),
    )
    assert timeline["items"][0]["coverage"]["acceptedReceiptIds"] == [
        "receipt:" + "c" * 64
    ]
    result = publish_retained_event_timeline(
        artifact_paths=(artifact,),
        symbols=("600000.SH",),
        blocked_dataset_reasons={},
        generated_at=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 8, 4, 13, 1, tzinfo=timezone.utc),
        output_root=(tmp_path / "timeline").resolve(),
    )
    assert result["symbolCount"] == 1
    assert (tmp_path / "timeline" / "600000.SH.receipt.json").is_file()

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["events"][0]["receipt_id"] = "receipt:" + "0" * 64
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AshareEvidenceContractError, match="evidence"):
        load_event_evidence_batch_artifact(artifact)


def test_retained_batch_does_not_replace_a_competing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "events.json"
    incumbent = b"already-retained-by-a-competing-writer\n"
    original_link = event_evidence.os.link

    def create_incumbent_then_link(source: str, destination: str) -> None:
        Path(destination).write_bytes(incumbent)
        original_link(source, destination)

    monkeypatch.setattr(event_evidence.os, "link", create_incumbent_then_link)

    with pytest.raises(AshareEvidenceContractError, match="path_invalid"):
        write_event_evidence_batch_artifact(batch=_batch(), path=artifact)

    assert artifact.read_bytes() == incumbent
    assert not tuple(tmp_path.glob(".events.json.*"))


def test_retained_timeline_rejects_foreign_batch(tmp_path: Path) -> None:
    artifact = tmp_path / "events.json"
    write_event_evidence_batch_artifact(batch=_batch(), path=artifact)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["profile"]["dataset_id"] = "cn.dataset.cctv_news"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TradingCopilotObservationError, match="retained_artifact_invalid"):
        publish_retained_event_timeline(
            artifact_paths=(artifact,),
            symbols=("600000.SH",),
            blocked_dataset_reasons={},
            generated_at=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
            valid_until=datetime(2026, 8, 4, 13, 1, tzinfo=timezone.utc),
            output_root=(tmp_path / "timeline").resolve(),
        )
