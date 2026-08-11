from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from Ashare.event_evidence import (
    AshareEvidenceContractError,
    EvidenceDatasetProfile,
    EventEvidenceSnapshot,
    EventEvidenceSnapshotBatch,
    PRIMARY_DATASET_IDS,
)
from Ashare.minute_data import (
    MinuteDataContractError,
    MinuteBarEvidence,
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    MinuteEvidenceUse,
    MinuteReferenceFact,
    MinuteTimestampSemantics,
)
from Ashare.minute_canary import MinuteCanaryConfig
from Ashare.trading_copilot_observation_worker import (
    TradingCopilotObservationError,
    _pinned_snapshot_plan,
    build_projection_batch,
    build_offline_projection_batch,
    build_td_projection_batch,
    company_facts_from_verified_observation,
    load_event_bundle,
    load_company_facts,
    load_current_event_snapshots,
    retain_same_observation_inputs,
)
import Ashare.trading_copilot_observation_worker as observation_worker
from Ashare.trading_copilot_projection import BATCH_INPUT_CONTRACT, publish_projection_batch
from Ashare.trading_copilot_event_timeline import build_event_timeline_batch, publish_event_timeline_batch
from shared.data.sharedsignals_v1 import SharedSignalsV1Client


def test_same_observation_retention_entrypoint_exists() -> None:
    """The worker exposes an explicit opt-in retention boundary."""

    assert callable(retain_same_observation_inputs)


def test_same_observation_retention_binds_snapshot_and_typed_event_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ashare_security_event_research_mapping import _research_snapshot

    snapshot = _research_snapshot()
    decision = datetime.fromisoformat(snapshot.decision_as_of)
    artifact_paths = []
    batches = {}
    for dataset_id in ("cn.dataset.anns_d", "cn.dataset.cctv_news", "cn.dataset.research_report"):
        path = (tmp_path / f"{dataset_id}.json").resolve()
        path.write_bytes(dataset_id.encode())
        artifact_paths.append(path)
        batches[path] = SimpleNamespace(
            profile=SimpleNamespace(dataset_id=dataset_id),
            observed_catalog_version=snapshot.catalog_version,
            events=(SimpleNamespace(as_of=decision, receipt_id=f"receipt:{dataset_id}"),),
            row_count=1,
            page_count=1,
            same_observation=True,
        )
    monkeypatch.setattr(
        observation_worker,
        "load_event_evidence_batch_artifact",
        lambda path: batches[Path(path)],
    )
    calls = []

    class Store:
        def __init__(self, root: Path) -> None:
            self.root = root

        def compare_and_swap(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(observation_worker, "FileResearchSnapshotStore", Store)
    result = retain_same_observation_inputs(
        research_snapshot=snapshot,
        event_artifact_paths=artifact_paths,
        blocked_dataset_reasons={
            "cn.dataset.irm_qa_sh": "ashare_evidence_metadata_not_ready",
            "cn.dataset.irm_qa_sz": "ashare_evidence_metadata_not_ready",
        },
        decision_time=decision,
        store_root=(tmp_path / "snapshots").resolve(),
    )
    assert calls == [{"snapshot": snapshot, "expected_snapshot_sha256": None}]
    assert result["snapshotSha256"] == snapshot.snapshot_sha256
    assert result["snapshotPath"].endswith(f"snapshot-{snapshot.snapshot_sha256}.json")
    assert [item["datasetId"] for item in result["eventArtifacts"]] == [
        "cn.dataset.anns_d", "cn.dataset.cctv_news", "cn.dataset.research_report"
    ]
    assert result["blockedDatasetReasons"] == {
        "cn.dataset.irm_qa_sh": "ashare_evidence_metadata_not_ready",
        "cn.dataset.irm_qa_sz": "ashare_evidence_metadata_not_ready",
    }
    assert all(value is False for key, value in result["authority"].items() if key.endswith("Authority") or key.endswith("Eligible") or key.endswith("Enabled"))


def test_same_observation_retention_fails_closed_for_missing_security_master(
    tmp_path: Path,
) -> None:
    from tests.test_ashare_security_event_research_mapping import _research_snapshot

    snapshot = _research_snapshot()
    missing = replace(snapshot, datasets=())
    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_security_master_binding_invalid",
    ):
        retain_same_observation_inputs(
            research_snapshot=missing,
            event_artifact_paths=(),
            blocked_dataset_reasons={},
            decision_time=datetime.fromisoformat(snapshot.decision_as_of),
            store_root=(tmp_path / "snapshots").resolve(),
        )


def test_same_observation_retention_fails_closed_for_cross_window_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ashare_security_event_research_mapping import _research_snapshot

    snapshot = _research_snapshot()
    path = (tmp_path / "anns.json").resolve()
    path.write_bytes(b"artifact")
    monkeypatch.setattr(
        observation_worker,
        "load_event_evidence_batch_artifact",
        lambda _: SimpleNamespace(
            profile=SimpleNamespace(dataset_id="cn.dataset.anns_d"),
            observed_catalog_version=snapshot.catalog_version,
            events=(SimpleNamespace(as_of=datetime.fromisoformat(snapshot.decision_as_of).replace(hour=10), receipt_id="receipt:cross"),),
            row_count=1,
            page_count=1,
            same_observation=True,
        ),
    )
    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_research_snapshot_observation_identity_mismatch",
    ):
        retain_same_observation_inputs(
            research_snapshot=snapshot,
            event_artifact_paths=(path,),
            blocked_dataset_reasons={
                "cn.dataset.irm_qa_sh": "ashare_evidence_metadata_not_ready",
                "cn.dataset.irm_qa_sz": "ashare_evidence_metadata_not_ready",
                "cn.dataset.cctv_news": "missing",
                "cn.dataset.research_report": "missing",
            },
            decision_time=datetime.fromisoformat(snapshot.decision_as_of),
            store_root=(tmp_path / "snapshots").resolve(),
        )


def test_same_observation_retention_maps_immutable_store_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ashare_security_event_research_mapping import _research_snapshot

    class ConflictStore:
        def __init__(self, root: Path) -> None:
            pass

        def compare_and_swap(self, **kwargs) -> None:
            raise observation_worker.ResearchSnapshotStoreConflict("conflict-detail")

    monkeypatch.setattr(observation_worker, "FileResearchSnapshotStore", ConflictStore)
    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_research_snapshot_retention_failed",
    ):
        retain_same_observation_inputs(
            research_snapshot=_research_snapshot(),
            event_artifact_paths=(),
            blocked_dataset_reasons={
                dataset_id: "ashare_evidence_metadata_not_ready"
                for dataset_id in PRIMARY_DATASET_IDS
            },
            decision_time=datetime(2026, 8, 9, 11, 10, tzinfo=timezone.utc),
            store_root=(tmp_path / "snapshots").resolve(),
        )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _profile() -> MinuteDatasetProfile:
    fields = ("ts_code", "bar_time", "freq", "open", "high", "low", "close", "vol", "amount", "pre_close", "suspended")
    return MinuteDatasetProfile(
        expected_catalog_version="catalog-v1", observed_catalog_version="catalog-v1",
        dataset_id="cn.dataset.rt_min", schema_major=1, default_fields=fields,
        default_order=("ts_code:asc", "bar_time:asc"),
        filter_operators=tuple((field, ("eq",)) for field in fields),
        dataset_contract_fingerprint=_sha("dataset"), consumer_profile_sha256=_sha("profile"),
        identity_fields=("ts_code", "bar_time"), symbol_field="ts_code", timestamp_field="bar_time",
        open_field="open", high_field="high", low_field="low", close_field="close", volume_field="vol",
        amount_field="amount", previous_close_field="pre_close", suspension_field="suspended",
        frequency_field="freq", frequency_value="5min", timestamp_format="%Y%m%d %H:%M:%S",
        timestamp_semantics=MinuteTimestampSemantics.BAR_END, volume_multiplier_to_shares=1,
        amount_multiplier_to_cny=1, price_adjustment="raw_unadjusted", max_pages=2, max_rows=20, page_limit=20,
    )


def _bar(stamp: str, close: float) -> MinuteBarEvidence:
    end = datetime.fromisoformat(stamp)
    return MinuteBarEvidence(
        symbol="600000.SH", bar_start=end - timedelta(minutes=5), bar_end=end,
        open_cny=close - 0.03, high_cny=close + 0.08, low_cny=close - 0.08, close_cny=close,
        volume_shares=100_000, amount_cny=close * 100_000, previous_close_cny=9.8, suspended=False,
        market_session="continuous_auction_am", dataset_id="cn.dataset.rt_min", catalog_version="catalog-v1",
        receipt_id="minute-receipt", data_through=end, observed_at=end + timedelta(seconds=5),
        available_at=end + timedelta(seconds=5), decision_time=end + timedelta(seconds=10),
        source_lineage_sha256=_sha("lineage"), envelope_proof_sha256=_sha("envelope"),
        source_row_sha256=_sha(stamp), reference_evidence_sha256=_sha("reference"),
    )


def _snapshot() -> MinuteBarSnapshot:
    bars = (_bar("2026-07-31T09:35:00+08:00", 9.9), _bar("2026-07-31T09:40:00+08:00", 10.05))
    return MinuteBarSnapshot(
        profile=_profile(), bars=bars, page_count=1, row_count=2,
        pagination_trace_sha256=_sha("pagination"), first_semantic_sha256=_sha("semantic"),
        replay_semantic_sha256=_sha("semantic"), same_observation=True,
    )


def _source() -> dict:
    return {
        "transportContract": "tradingdatas_v1_catalog_query", "datasetId": "cn.equity.security_master",
        "receiptId": "company-receipt", "receiptSha256": _sha("company"),
        "lineageSha256": _sha("company-lineage"),
        "dataThrough": "2026-08-01T01:40:00+00:00", "retrievedAt": "2026-08-01T01:40:05+00:00",
        "freshness": "fresh", "adjustment": "none",
    }


def _authority(*, dataset_id: str, data_through: str, receipt_id: str, receipt_sha256: str, lineage_sha256: str) -> dict:
    return {
        "datasetId": dataset_id, "market": "ashare", "timezone": "Asia/Shanghai",
        "calendar": {
            "id": "sse", "version": "2026.08", "sourceDatasetId": "cn.market.trade_calendar",
            "receiptId": "calendar-receipt", "receiptSha256": _sha("calendar"),
            "lineageSha256": _sha("calendar-lineage"), "calendarSha256": _sha("calendar-content"),
        },
        "session": {"state": "closed", "asOf": "2026-08-01T01:40:00+00:00"},
        "dataThrough": data_through,
        "source": {"receiptId": receipt_id, "receiptSha256": receipt_sha256, "lineageSha256": lineage_sha256},
    }


def _authorities() -> dict[str, dict]:
    company = _source()
    return {
        "cn.dataset.rt_min": _authority(
            dataset_id="cn.dataset.rt_min", data_through="2026-07-31T09:40:00+08:00",
            receipt_id="minute-receipt", receipt_sha256=_sha("envelope"), lineage_sha256=_sha("lineage"),
        ),
        "cn.equity.security_master": _authority(
            dataset_id="cn.equity.security_master", data_through=company["dataThrough"],
            receipt_id=company["receiptId"], receipt_sha256=company["receiptSha256"], lineage_sha256=company["lineageSha256"],
        ),
        "cn.dataset.anns_d": _authority(
            dataset_id="cn.dataset.anns_d", data_through="2026-08-01T01:19:00+00:00",
            receipt_id="event-receipt", receipt_sha256=_sha("event-envelope"), lineage_sha256=_sha("event-lineage"),
        ),
    }


def _companies() -> dict[str, dict]:
    return {"600000.SH": {
        "symbol": "600000.SH", "name": "浦发银行", "industry": "银行", "area": "上海",
        "listingDate": "1999-11-10", "description": "证券主数据已绑定正式回执。", "source": _source(),
        "marketRules": {"board": "main", "priceLimitPct": 10, "stStatus": "normal"},
        "turnoverRate": None, "peTtm": -3.2, "marketCapCny": None,
    }}


def _event(*, url: str | None = "https://example.invalid/disclosure") -> EventEvidenceSnapshot:
    return EventEvidenceSnapshot(
        dataset_id="cn.dataset.anns_d", catalog_version="catalog-v1", event_time="20260731",
        event_time_precision="date", as_of=datetime(2026, 8, 1, 1, 40, tzinfo=timezone.utc),
        data_through=datetime(2026, 8, 1, 1, 19, tzinfo=timezone.utc), available_at=datetime(2026, 8, 1, 1, 20, tzinfo=timezone.utc),
        available_at_source="query_envelope.metadata.observed_at", entity="浦发银行", symbol="600000.SH",
        title="业绩公告", content="正式公告内容摘要。", url=url, source="交易所",
        receipt_id="event-receipt", source_lineage_sha256=_sha("event-lineage"),
        source_row_sha256=_sha("event-row"), envelope_proof_sha256=_sha("event-envelope"),
        evidence_ref=f"td-v1:cn.dataset.anns_d:event-receipt:{_sha('event-row')[:16]}",
        evidence_confidence=0.9, event_time_instant_proven=False, historical_known_time_proven=False,
        pit_feature_eligible=False,
    )


def _event_evidence_profile(dataset_id: str) -> EvidenceDatasetProfile:
    is_macro = dataset_id == "cn.dataset.major_news"
    fields = ("event_id", "ts_code", "event_time", "title", "content")
    return EvidenceDatasetProfile(
        expected_catalog_version="catalog-v1",
        observed_catalog_version="catalog-v1",
        dataset_id=dataset_id,
        schema_major=1,
        default_fields=fields,
        default_order=("event_id:asc",),
        filter_operators=(
            ("event_id", ("eq",)),
            ("ts_code", ("eq", "in")),
        ),
        dataset_contract_fingerprint=_sha(f"{dataset_id}:catalog"),
        consumer_profile_sha256=_sha(f"{dataset_id}:profile"),
        identity_fields=("event_id",),
        event_time_field="event_time",
        symbol_field=None if is_macro else "ts_code",
        entity_field=None,
        title_field="title",
        content_field="content",
        url_field=None,
        source_field=None,
        default_entity="CN-MACRO" if is_macro else None,
        optional_dataset=is_macro,
        max_pages=1,
        max_rows=100,
        page_limit=100,
        omit_as_of=is_macro,
    )


def test_builds_and_publishes_direct_observation_with_all_receipts(tmp_path: Path) -> None:
    generated = datetime(2026, 8, 1, 1, 40, 10, tzinfo=timezone.utc)
    batch = build_projection_batch(
        snapshot=_snapshot(), company_facts=_companies(), events=(_event(),),
        activity_authorities=_authorities(),
        generated_at=generated, valid_until=generated + timedelta(days=2),
    )
    item = batch["items"][0]
    assert item["series"]["1D"][-1]["forecastMedian"] is None
    assert item["events"][0]["sentimentConfidence"] is None
    assert item["events"][0]["impactDirection"] == "uncertain"
    input_path = (tmp_path / "batch.json").resolve()
    input_path.write_text(json.dumps(batch), encoding="utf-8")
    result = publish_projection_batch(
        input_path=input_path, output_root=(tmp_path / "out").resolve(),
        now=generated + timedelta(minutes=1),
    )
    receipt = json.loads((tmp_path / "out" / "600000.SH.receipt.json").read_text())
    assert result["symbolCount"] == 1
    assert {row["receiptId"] for row in receipt["sourceReceipts"]} == {
        "minute-receipt", "company-receipt", "event-receipt", "calendar-receipt"
    }


def test_requires_activity_authority_for_each_consumed_dataset() -> None:
    generated = datetime(2026, 8, 1, 1, 40, 10, tzinfo=timezone.utc)
    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_activity_authority_required:cn.dataset.rt_min",
    ):
        build_projection_batch(
            snapshot=_snapshot(),
            company_facts=_companies(),
            events=(_event(),),
            generated_at=generated,
            valid_until=generated + timedelta(days=2),
        )


def test_event_projection_preserves_the_verified_data_capability() -> None:
    generated = datetime(2026, 8, 1, 1, 40, 10, tzinfo=timezone.utc)
    batch = build_projection_batch(
        snapshot=_snapshot(), company_facts=_companies(), events=(_event(),),
        activity_authorities=_authorities(),
        generated_at=generated, valid_until=generated + timedelta(days=2),
    )

    capability = batch["items"][0]["events"][0]["dataCapability"]
    assert capability == {
        "inputContract": "tradingagent.trading_copilot_projection_batch_input.v2",
        "transportContract": "tradingdatas_v1_catalog_query",
        "datasetId": "cn.dataset.anns_d",
        "catalogVersion": "catalog-v1",
        "asOf": "2026-08-01T01:40:00+00:00",
        "dataThrough": "2026-08-01T01:19:00+00:00",
        "freshness": "fresh",
        "receiptId": "event-receipt",
        "receiptSha256": _sha("event-envelope"),
        "lineageSha256": _sha("event-lineage"),
        "activityAuthority": _authorities()["cn.dataset.anns_d"],
    }


def test_historical_projection_marks_price_source_stale() -> None:
    original = _snapshot()
    historical = replace(
        original,
        bars=tuple(
            replace(bar, evidence_use=MinuteEvidenceUse.HISTORICAL_DISPLAY)
            for bar in original.bars
        ),
    )
    generated = datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc)

    batch = build_projection_batch(
        snapshot=historical,
        company_facts=_companies(),
        events=(),
        activity_authorities=_authorities(),
        generated_at=generated,
        valid_until=generated + timedelta(hours=1),
    )

    assert batch["items"][0]["source"]["freshness"] == "stale"


def test_event_catalog_failure_returns_explicit_full_coverage_debt(monkeypatch) -> None:
    monkeypatch.setattr(
        observation_worker,
        "build_runtime_transport",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        observation_worker,
        "SharedSignalsV1Client",
        lambda *args, **kwargs: object(),
    )

    class BlockedPort:
        def __init__(self, client) -> None:
            pass

        def freeze_profiles(self, *, audit_ledger):
            raise AshareEvidenceContractError("ashare_evidence_catalog_identity_mismatch")

    monkeypatch.setattr(
        observation_worker,
        "TradingDatasAshareEvidencePort",
        BlockedPort,
    )

    events, blocked, reasons = load_current_event_snapshots(
        minute_config=SimpleNamespace(
            transport_id="http-json-v1",
            base_url="http://127.0.0.1:18082",
            expected_catalog_version="catalog-v1",
            access_policy_id="test-read-v1",
            timeout_seconds=1,
        ),
        token_file=Path("/not-read-by-test"),
        decision_time=datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc),
        symbols=("600000.SH",),
    )

    assert events == ()
    assert blocked == tuple(PRIMARY_DATASET_IDS)
    assert reasons == {
        dataset_id: "ashare_evidence_catalog_identity_mismatch"
        for dataset_id in PRIMARY_DATASET_IDS
    }


def test_event_loader_rejects_missing_formal_runtime_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        observation_worker,
        "build_runtime_transport",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        observation_worker,
        "SharedSignalsV1Client",
        lambda *args, **kwargs: object(),
    )

    class MissingRuntimeEvidencePort:
        def __init__(self, client) -> None:
            pass

        def freeze_profiles(self, *, audit_ledger):
            return SimpleNamespace(
                by_dataset={
                    dataset_id: _event_evidence_profile(dataset_id)
                    for dataset_id in PRIMARY_DATASET_IDS
                }
            )

        def load_event_snapshot(self, **kwargs):
            return None

    monkeypatch.setattr(
        observation_worker,
        "TradingDatasAshareEvidencePort",
        MissingRuntimeEvidencePort,
    )

    events, blocked, reasons = load_current_event_snapshots(
        minute_config=SimpleNamespace(
            transport_id="http-json-v1",
            base_url="http://127.0.0.1:18082",
            expected_catalog_version="catalog-v1",
            access_policy_id="test-read-v1",
            timeout_seconds=1,
        ),
        token_file=Path("/not-read-by-test"),
        decision_time=datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc),
        symbols=("600000.SH",),
    )

    assert events == ()
    assert blocked == tuple(PRIMARY_DATASET_IDS)
    assert reasons == {
        dataset_id: "copilot_event_consumer_runtime_evidence_missing"
        for dataset_id in PRIMARY_DATASET_IDS
    }


def test_event_loader_pushes_allowed_symbols_into_query(monkeypatch) -> None:
    dataset_id = "cn.dataset.research_report"
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        observation_worker,
        "load_event_consumer_profiles",
        lambda: (SimpleNamespace(
            dataset_id=dataset_id,
            explicit_request_required=False,
            symbol_binding="required",
        ),),
    )
    monkeypatch.setattr(
        observation_worker,
        "build_runtime_transport",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        observation_worker,
        "SharedSignalsV1Client",
        lambda *args, **kwargs: object(),
    )
    # This unit test isolates query shaping.  Cross-contract and runtime
    # evidence validation are covered with typed local catalog fixtures.
    monkeypatch.setattr(
        observation_worker,
        "validate_event_consumer_profile_contract",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        observation_worker,
        "validate_event_consumer_runtime_evidence",
        lambda **kwargs: None,
    )

    class FilteredPort:
        def __init__(self, client) -> None:
            pass

        def freeze_profiles(self, *, audit_ledger):
            return SimpleNamespace(
                by_dataset={
                    dataset_id: SimpleNamespace(
                        symbol_field="ts_code",
                        filter_operators=(("ts_code", ("eq", "in")),),
                    )
                }
            )

        def load_event_snapshot(self, **kwargs):
            seen["filters"] = kwargs["filters"]
            return SimpleNamespace(events=())

    monkeypatch.setattr(
        observation_worker,
        "TradingDatasAshareEvidencePort",
        FilteredPort,
    )

    events, blocked, reasons = load_current_event_snapshots(
        minute_config=SimpleNamespace(
            transport_id="http-json-v1",
            base_url="http://127.0.0.1:18082",
            expected_catalog_version="catalog-v1",
            access_policy_id="test-read-v1",
            timeout_seconds=1,
        ),
        token_file=Path("/not-read-by-test"),
        decision_time=datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc),
        symbols=("002294.SZ", "000333.SZ"),
    )

    assert events == ()
    assert blocked == ()
    assert reasons == {}
    assert seen["filters"] == {
        "ts_code": {"in": ["000333.SZ", "002294.SZ"]}
    }


def test_event_loader_only_queries_major_news_after_explicit_on_demand_request(monkeypatch) -> None:
    seen: dict[str, object] = {"calls": []}
    monkeypatch.setattr(observation_worker, "build_runtime_transport", lambda *args, **kwargs: object())

    class Client:
        def __init__(self, config, *, transport) -> None:
            seen["dataset_ids"] = config.dataset_ids

    monkeypatch.setattr(observation_worker, "SharedSignalsV1Client", Client)
    # This unit test isolates selection and on-demand query routing.  It does
    # not model a formal catalog/query evidence snapshot.
    monkeypatch.setattr(
        observation_worker,
        "validate_event_consumer_profile_contract",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        observation_worker,
        "validate_event_consumer_runtime_evidence",
        lambda **kwargs: None,
    )

    class ProfiledPort:
        def __init__(self, client) -> None:
            pass

        def freeze_profiles(self, *, audit_ledger):
            return SimpleNamespace(by_dataset={
                "cn.dataset.anns_d": SimpleNamespace(symbol_field="ts_code", filter_operators=(("ts_code", ("in",)),)),
                "cn.dataset.cctv_news": SimpleNamespace(symbol_field="ts_code", filter_operators=(("ts_code", ("in",)),)),
                "cn.dataset.irm_qa_sh": SimpleNamespace(symbol_field="ts_code", filter_operators=(("ts_code", ("in",)),)),
                "cn.dataset.irm_qa_sz": SimpleNamespace(symbol_field="ts_code", filter_operators=(("ts_code", ("in",)),)),
                "cn.dataset.research_report": SimpleNamespace(symbol_field="ts_code", filter_operators=(("ts_code", ("in",)),)),
                "cn.dataset.major_news": SimpleNamespace(symbol_field=None, filter_operators=()),
            })

        def load_event_snapshot(self, **kwargs):
            seen["calls"].append((kwargs["profile"], kwargs["filters"], kwargs["allowed_symbols"]))
            return SimpleNamespace(events=())

    monkeypatch.setattr(observation_worker, "TradingDatasAshareEvidencePort", ProfiledPort)

    events, blocked, reasons = load_current_event_snapshots(
        minute_config=SimpleNamespace(
            transport_id="http-json-v1", base_url="http://127.0.0.1:18082",
            expected_catalog_version="catalog-v1", access_policy_id="test-read-v1", timeout_seconds=1,
        ),
        token_file=Path("/not-read-by-test"),
        decision_time=datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc),
        symbols=("000333.SZ",),
        requested_on_demand_dataset_ids=("cn.dataset.major_news",),
    )

    assert events == () and blocked == () and reasons == {}
    assert seen["dataset_ids"] == frozenset({
        "cn.dataset.anns_d", "cn.dataset.cctv_news", "cn.dataset.irm_qa_sh",
        "cn.dataset.irm_qa_sz", "cn.dataset.research_report", "cn.dataset.major_news",
    })
    assert seen["calls"][-1][1:] == ({}, None)


def test_event_artifact_failure_blocks_batch_before_flattening(tmp_path: Path, monkeypatch) -> None:
    profile = _event_evidence_profile("cn.dataset.anns_d")
    retained = EventEvidenceSnapshotBatch(
        profile=profile,
        events=(_event(),),
        page_count=1,
        row_count=1,
        pagination_trace_sha256=_sha("pages"),
        first_semantic_sha256=_sha("replay"),
        replay_semantic_sha256=_sha("replay"),
        same_observation=True,
    )
    monkeypatch.setattr(observation_worker, "build_runtime_transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(observation_worker, "SharedSignalsV1Client", lambda *args, **kwargs: object())
    monkeypatch.setattr(observation_worker, "select_event_consumer_profiles", lambda *args, **kwargs: (SimpleNamespace(dataset_id="cn.dataset.anns_d", symbol_binding="required"),))
    monkeypatch.setattr(observation_worker, "validate_event_consumer_profile_contract", lambda **kwargs: None)
    monkeypatch.setattr(observation_worker, "validate_event_consumer_runtime_evidence", lambda **kwargs: None)

    class Port:
        def __init__(self, client) -> None:
            pass

        def freeze_profiles(self, *, audit_ledger):
            return SimpleNamespace(by_dataset={"cn.dataset.anns_d": profile})

        def load_event_snapshot(self, **kwargs):
            return retained

    monkeypatch.setattr(observation_worker, "TradingDatasAshareEvidencePort", Port)
    monkeypatch.setattr(
        observation_worker,
        "write_event_evidence_batch_artifact",
        lambda **kwargs: (_ for _ in ()).throw(AshareEvidenceContractError("artifact_write_failed")),
    )

    events, blocked, reasons, paths = load_current_event_snapshots(
        minute_config=SimpleNamespace(transport_id="http-json-v1", base_url="http://127.0.0.1:18082", expected_catalog_version="catalog-v1", access_policy_id="test-read-v1", timeout_seconds=1),
        token_file=Path("/not-read-by-test"),
        decision_time=datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc),
        symbols=("600000.SH",),
        retained_artifact_root=(tmp_path / "artifacts").resolve(),
    )

    assert events == () and paths == ()
    assert blocked == ("cn.dataset.anns_d",)
    assert reasons == {"cn.dataset.anns_d": "artifact_write_failed"}


def test_event_bundle_cannot_substitute_for_verified_td_runtime_evidence(tmp_path: Path) -> None:
    path = (tmp_path / "events.json").resolve()
    path.write_text(json.dumps({
        "contractId": "tradingagent.ashare_event_evidence_bundle.v1",
        "items": [],
    }), encoding="utf-8")

    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_event_bundle_runtime_evidence_required",
    ):
        load_event_bundle(path)


def test_omits_event_without_verifiable_url_and_never_invents_sentiment() -> None:
    generated = datetime(2026, 8, 1, 1, 40, 10, tzinfo=timezone.utc)
    batch = build_projection_batch(
        snapshot=_snapshot(), company_facts=_companies(), events=(_event(url=None),),
        activity_authorities=_authorities(),
        generated_at=generated, valid_until=generated + timedelta(days=1),
    )
    assert batch["items"][0]["events"] == []


def test_event_timeline_publishes_accepted_events_with_independent_coverage_debt(tmp_path: Path) -> None:
    generated = datetime(2026, 8, 1, 1, 40, tzinfo=timezone.utc)
    batch = build_event_timeline_batch(
        symbols=("600000.SH", "000001.SZ"), events=(_event(),),
        blocked_dataset_reasons={"cn.dataset.irm_qa_sh": "ashare_evidence_metadata_not_ready"},
        generated_at=generated, valid_until=generated + timedelta(hours=2),
    )
    result = publish_event_timeline_batch(batch=batch, output_root=(tmp_path / "timeline").resolve(), now=generated)
    timeline = json.loads((tmp_path / "timeline" / "600000.SH.json").read_text())
    assert result["symbolCount"] == 2
    assert timeline["events"][0]["sentiment"] == "neutral"
    assert timeline["coverage"]["blockedDatasetIds"] == ["cn.dataset.irm_qa_sh"]
    assert (tmp_path / "timeline" / "000001.SZ.receipt.json").is_file()


def test_event_timeline_accepts_declared_on_demand_major_news_coverage_debt() -> None:
    generated = datetime(2026, 8, 1, 1, 40, tzinfo=timezone.utc)

    batch = build_event_timeline_batch(
        symbols=("600000.SH",), events=(),
        blocked_dataset_reasons={"cn.dataset.major_news": "ashare_evidence_metadata_not_ready"},
        generated_at=generated, valid_until=generated + timedelta(hours=2),
    )

    assert batch["items"][0]["coverage"]["blockedDatasetIds"] == ["cn.dataset.major_news"]


def test_blocks_symbol_without_verified_company_facts() -> None:
    with pytest.raises(TradingCopilotObservationError, match="copilot_company_fact_missing"):
        build_projection_batch(
            snapshot=_snapshot(), company_facts={}, events=(),
            generated_at=datetime(2026, 8, 1, 1, 40, 10, tzinfo=timezone.utc),
            valid_until=datetime(2026, 8, 2, 1, 40, 10, tzinfo=timezone.utc),
        )


def test_company_facts_loader_binds_one_verified_source(tmp_path: Path) -> None:
    path = (tmp_path / "company.json").resolve()
    path.write_text(json.dumps({
        "contractId": "tradingagent.trading_copilot_company_facts.v1", "source": _source(),
        "items": [{key: value for key, value in _companies()["600000.SH"].items() if key != "source"}],
    }), encoding="utf-8")
    assert load_company_facts(path)["600000.SH"]["source"]["receiptId"] == "company-receipt"


def test_company_observation_load_failure_is_structured(monkeypatch, tmp_path: Path) -> None:
    dataset = SimpleNamespace(schema_major=2, probe_role="security_master")
    manifest = SimpleNamespace(
        datasets=(dataset,),
        profile_id="profile-v1",
        catalog_version="catalog-v1",
        as_of=datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc),
        manifest_sha256=_sha("manifest"),
    )
    monkeypatch.setattr(observation_worker, "load_probe_manifest", lambda path: manifest)

    def blocked(**kwargs):
        raise observation_worker.AshareRuntimeAuthorityLoadBlocked(
            "observation_membership_missing"
        )

    monkeypatch.setattr(
        observation_worker,
        "load_verified_ashare_runtime_authority_bundle",
        blocked,
    )

    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_observation_bundle_blocked:observation_membership_missing",
    ):
        company_facts_from_verified_observation(
            manifest_path=tmp_path / "manifest.json",
            state_root=tmp_path / "state",
        )


def test_company_facts_can_only_come_from_verified_observation_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import Ashare.trading_copilot_observation_worker as module

    manifest = SimpleNamespace(
        profile_id="profile", catalog_version="catalog", as_of="2026-08-01T08:00:00+00:00",
        manifest_sha256=_sha("manifest"),
        datasets=(SimpleNamespace(probe_role="security_master", dataset_id="cn.equity.security_master", schema_major=2),),
    )
    master = SimpleNamespace(
        dataset_id="cn.equity.security_master", eligible=True, receipt_id="master-receipt",
        source_proof_sha256=_sha("master-proof"), data_through="2026-08-01T07:00:00+00:00",
        observed_at="2026-08-01T07:01:00+00:00", lineage_sha256=_sha("master-lineage"),
        decoded_rows=lambda: [{"ts_code": "600000.SH", "name": "浦发银行", "list_date": "19991110"}],
    )
    bundle = SimpleNamespace(research_snapshot=SimpleNamespace(datasets=(master,)))
    monkeypatch.setattr(module, "load_probe_manifest", lambda _: manifest)
    monkeypatch.setattr(module, "load_verified_ashare_runtime_authority_bundle", lambda **_: bundle)
    facts = company_facts_from_verified_observation(
        manifest_path=(tmp_path / "manifest.json").resolve(), state_root=tmp_path.resolve(),
    )
    assert facts["600000.SH"]["industry"] == "未交付"
    assert facts["600000.SH"]["source"]["receiptSha256"] == _sha("master-proof")


def _minute_config() -> MinuteCanaryConfig:
    return MinuteCanaryConfig(
        base_url="http://127.0.0.1:18082",
        expected_catalog_version="catalog-v1",
        dataset_id="cn.dataset.rt_min",
        access_policy_id="test-read-v1",
        transport_id="http-json-v1",
        timeout_seconds=1,
        filters={},
        profile={
            "timestamp_field": "time",
            "symbol_field": "ts_code",
            "timestamp_format": "%Y-%m-%d %H:%M:%S",
        },
    )


def test_snapshot_plan_pins_universe_at_availability_boundary() -> None:
    config = _minute_config()
    facts = {"000001.SZ": object(), "600000.SH": object()}
    decision = datetime(2026, 8, 7, 11, 40, tzinfo=timezone(timedelta(hours=8)))
    pinned, snapshot_decision = _pinned_snapshot_plan(config, facts, decision)
    assert pinned.filters == {
        "time": {"eq": "2026-08-07 11:30:00"},
        "ts_code": {"in": ("000001.SZ", "600000.SH")},
    }
    assert snapshot_decision == datetime(
        2026, 8, 7, 11, 37, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert snapshot_decision <= decision


def test_snapshot_plan_rejects_before_any_available_bar() -> None:
    config = _minute_config()
    decision = datetime(2026, 8, 7, 9, 10, tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_minute_snapshot_bar_unavailable",
    ):
        _pinned_snapshot_plan(config, {"600000.SH": object()}, decision)


def test_main_pins_snapshot_query_for_retention_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "minute-manifest.json"
    manifest.write_text(json.dumps({
        "dataset_id": "cn.dataset.rt_min",
        "expected_catalog_version": "catalog-v1",
        "base_url": "http://127.0.0.1:18082",
        "access_policy_id": "test-read-v1",
        "transport_id": "http-json-v1",
        "timeout_seconds": 1,
        "filters": {},
        "profile": {
            "timestamp_field": "time",
            "symbol_field": "ts_code",
            "timestamp_format": "%Y-%m-%d %H:%M:%S",
        },
    }), encoding="utf-8")
    reference = tmp_path / "reference-facts.json"
    reference.write_text(json.dumps([
        {
            "symbol": "600000.SH",
            "trade_date": "2026-08-07",
            "previous_close_cny": 9.8,
            "suspended": False,
            "evidence_sha256": _sha("ref"),
        }
    ]), encoding="utf-8")
    captured: dict[str, object] = {}

    def fail_with_captured(config, **kwargs):
        captured["filters"] = config.filters
        captured["decision_time"] = kwargs["decision_time"]
        raise MinuteDataContractError("minute_test_fail")

    monkeypatch.setattr(observation_worker, "load_minute_snapshot", fail_with_captured)
    rc = observation_worker.main([
        "--minute-manifest", str(manifest),
        "--reference-facts", str(reference),
        "--company-facts", str(tmp_path / "company.json"),
        "--token-file", str(tmp_path / "token"),
        "--decision-time", "2026-08-07T11:40:00+08:00",
        "--trading-date", "2026-08-07",
        "--evidence-use", "historical_display",
        "--valid-until", "2026-08-10T09:00:00+08:00",
        "--activity-authorities", str(tmp_path / "authorities.json"),
        "--batch-output", str(tmp_path / "batch.json"),
        "--projection-output-root", str(tmp_path / "projection"),
        "--result-output", str(tmp_path / "result.json"),
    ])
    assert rc == 2
    assert captured["filters"] == {
        "time": {"eq": "2026-08-07 11:30:00"},
        "ts_code": {"in": ("600000.SH",)},
    }
    assert captured["decision_time"] == datetime(
        2026, 8, 7, 11, 37, 0, tzinfo=timezone(timedelta(hours=8))
    )


def test_offline_projection_batch_uses_canary_rows_without_query_or_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ashare_minute_canary import _Transport, _config
    from Ashare.minute_canary import run_minute_canary

    config = _config()
    transport = _Transport()
    references = {
        "600000.SH": MinuteReferenceFact(
            symbol="600000.SH", trade_date=date(2026, 7, 28),
            previous_close_cny=9.8, suspended=False, evidence_sha256=_sha("ref"),
        )
    }
    receipt = run_minute_canary(
        config,
        token_file=Path("/run/secrets/fixture.token"),
        decision_time=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
        trading_date=date(2026, 7, 28), reference_facts=references,
        bar_end="2026-07-28 09:35:00",
        transport_factory=lambda *args, **kwargs: transport,
    )
    canary_path = (tmp_path / "canary.json").resolve()
    canary_path.write_text(json.dumps(receipt), encoding="utf-8")
    references_path = (tmp_path / "references.json").resolve()
    references_path.write_text(json.dumps([{
        "symbol": "600000.SH", "trade_date": "2026-07-28",
        "previous_close_cny": 9.8, "suspended": False, "evidence_sha256": _sha("ref"),
    }]), encoding="utf-8")
    manifest = tmp_path / "minute.json"
    profile = dict(config.profile)
    bound = config.build_profile(
        SharedSignalsV1Client(config.client_config(), transport=transport),
        require_declared_bindings=False,
    )
    profile.update({
        "schema_major": bound.schema_major,
        "default_fields": list(bound.default_fields),
        "default_order": list(bound.default_order),
        "filter_operators": dict(bound.filter_operators),
        "dataset_contract_fingerprint": bound.dataset_contract_fingerprint,
        "consumer_profile_sha256": bound.consumer_profile_sha256,
        "observed_catalog_version": bound.observed_catalog_version,
    })
    manifest.write_text(json.dumps({
        "base_url": config.base_url,
        "expected_catalog_version": config.expected_catalog_version,
        "dataset_id": config.dataset_id,
        "access_policy_id": config.access_policy_id,
        "transport_id": config.transport_id,
        "timeout_seconds": config.timeout_seconds,
        "filters": dict(config.filters), "profile": profile,
    }), encoding="utf-8")
    company = _companies()["600000.SH"]
    row = receipt["bars"][0]
    window = receipt["snapshot_rows"]["items"][0]["data_through"]
    company_window = "2026-07-27T01:35:00+00:00"
    company = dict(company)
    company["source"] = {
        **company["source"],
        "receiptId": "company-receipt",
        "receiptSha256": _sha("company"),
        "lineageSha256": _sha("company-lineage"),
        "dataThrough": company_window,
        "retrievedAt": "2026-07-27T01:35:05+00:00",
    }
    company_path = tmp_path / "company.json"
    company_path.write_text(json.dumps({
        "contractId": "tradingagent.trading_copilot_company_facts.v1",
        "source": company["source"],
        "items": [{key: value for key, value in company.items() if key != "source"}],
    }), encoding="utf-8")
    authority_path = tmp_path / "authorities.json"
    authority_path.write_text(json.dumps({
        config.dataset_id: _authority(
            dataset_id=config.dataset_id, data_through=window,
            receipt_id=receipt["receipt_id"], receipt_sha256=row["envelope_proof_sha256"],
            lineage_sha256=row["source_lineage_sha256"],
        ),
        "cn.equity.security_master": _authority(
            dataset_id="cn.equity.security_master", data_through=company_window,
            receipt_id="company-receipt", receipt_sha256=_sha("company"),
            lineage_sha256=_sha("company-lineage"),
        ),
    }), encoding="utf-8")
    monkeypatch.setattr(observation_worker, "load_minute_snapshot", lambda *a, **k: pytest.fail("query"))
    monkeypatch.setattr(observation_worker, "publish_projection_batch", lambda *a, **k: pytest.fail("publish"))
    batch = build_offline_projection_batch(
        canary_receipt_path=canary_path, minute_manifest_path=manifest,
        reference_facts_path=references_path, company_facts_path=company_path,
        activity_authorities_path=authority_path,
        generated_at=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
        valid_until=datetime.fromisoformat("2026-07-29T09:00:00+08:00"),
    )
    assert batch["contractId"] == BATCH_INPUT_CONTRACT
    assert batch["items"][0]["symbol"] == "600000.SH"
    output_path = (tmp_path / "offline-batch.json").resolve()
    rc = observation_worker.main([
        "--minute-manifest", str(manifest),
        "--reference-facts", str(references_path),
        "--company-facts", str(company_path),
        "--activity-authorities", str(authority_path),
        "--decision-time", "2026-07-28T09:35:25+08:00",
        "--evidence-use", "historical_display",
        "--valid-until", "2026-07-29T09:00:00+08:00",
        "--batch-output", str(output_path),
        "--offline-canary-receipt", str(canary_path),
    ])
    assert rc == 0
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted["contractId"] == BATCH_INPUT_CONTRACT

    bad_company = json.loads(company_path.read_text(encoding="utf-8"))
    bad_company["items"][0]["symbol"] = "000001.SZ"
    bad_company_path = (tmp_path / "bad-company.json").resolve()
    bad_company_path.write_text(json.dumps(bad_company), encoding="utf-8")
    with pytest.raises(TradingCopilotObservationError, match="company_symbol_set"):
        build_offline_projection_batch(
            canary_receipt_path=canary_path, minute_manifest_path=manifest,
            reference_facts_path=references_path, company_facts_path=bad_company_path,
            activity_authorities_path=authority_path,
            generated_at=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
            valid_until=datetime.fromisoformat("2026-07-29T09:00:00+08:00"),
        )

    bad_authorities = json.loads(authority_path.read_text(encoding="utf-8"))
    bad_authorities["cn.equity.security_master"]["source"]["receiptId"] = "wrong-receipt"
    bad_authorities_path = (tmp_path / "bad-authorities.json").resolve()
    bad_authorities_path.write_text(json.dumps(bad_authorities), encoding="utf-8")
    with pytest.raises(TradingCopilotObservationError, match="authority_source_mismatch"):
        build_offline_projection_batch(
            canary_receipt_path=canary_path, minute_manifest_path=manifest,
            reference_facts_path=references_path, company_facts_path=company_path,
            activity_authorities_path=bad_authorities_path,
            generated_at=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
            valid_until=datetime.fromisoformat("2026-07-29T09:00:00+08:00"),
        )

    flagged_authorities = json.loads(authority_path.read_text(encoding="utf-8"))
    flagged_authorities["cn.equity.security_master"]["source"]["realTradingEnabled"] = True
    flagged_authorities_path = (tmp_path / "flagged-authorities.json").resolve()
    flagged_authorities_path.write_text(json.dumps(flagged_authorities), encoding="utf-8")
    with pytest.raises(TradingCopilotObservationError, match="activity_authority_present"):
        build_offline_projection_batch(
            canary_receipt_path=canary_path, minute_manifest_path=manifest,
            reference_facts_path=references_path, company_facts_path=company_path,
            activity_authorities_path=flagged_authorities_path,
            generated_at=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
            valid_until=datetime.fromisoformat("2026-07-29T09:00:00+08:00"),
        )

    bad_canary = json.loads(canary_path.read_text(encoding="utf-8"))
    bad_canary["snapshot_rows"]["items"][0]["bar_end"] = "2026-07-28T09:40:00+08:00"
    bad_canary_path = (tmp_path / "bad-canary.json").resolve()
    bad_canary_path.write_text(json.dumps(bad_canary), encoding="utf-8")
    with pytest.raises(TradingCopilotObservationError, match="sha256_mismatch"):
        build_offline_projection_batch(
            canary_receipt_path=bad_canary_path, minute_manifest_path=manifest,
            reference_facts_path=references_path, company_facts_path=company_path,
            activity_authorities_path=authority_path,
            generated_at=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
            valid_until=datetime.fromisoformat("2026-07-29T09:00:00+08:00"),
        )

    bad_flags = json.loads(company_path.read_text(encoding="utf-8"))
    bad_flags["items"][0]["real_trading_enabled"] = True
    bad_flags_path = (tmp_path / "bad-flags.json").resolve()
    bad_flags_path.write_text(json.dumps(bad_flags), encoding="utf-8")
    with pytest.raises(TradingCopilotObservationError, match="authority_present"):
        build_offline_projection_batch(
            canary_receipt_path=canary_path, minute_manifest_path=manifest,
            reference_facts_path=references_path, company_facts_path=bad_flags_path,
            activity_authorities_path=authority_path,
            generated_at=datetime.fromisoformat("2026-07-28T09:35:25+08:00"),
            valid_until=datetime.fromisoformat("2026-07-29T09:00:00+08:00"),
        )


def test_offline_projection_batch_rejects_legacy_canary_and_symbol_drift(tmp_path: Path) -> None:
    legacy_path = (tmp_path / "legacy.json").resolve()
    legacy_path.write_text(json.dumps({
        "status": "pass", "authority_tier": "observation_only",
    }), encoding="utf-8")
    with pytest.raises(TradingCopilotObservationError, match="snapshot_rows"):
        build_offline_projection_batch(
            canary_receipt_path=legacy_path,
            minute_manifest_path=(tmp_path / "minute.json").resolve(),
            reference_facts_path=(tmp_path / "references.json").resolve(),
            company_facts_path=(tmp_path / "company.json").resolve(),
            activity_authorities_path=(tmp_path / "authorities.json").resolve(),
            generated_at=datetime(2026, 7, 28, 1, 35, tzinfo=timezone.utc),
            valid_until=datetime(2026, 7, 29, 1, tzinfo=timezone.utc),
        )

def _td_envelope(dataset_id: str, rows: list[dict], *, data_through: str, observed_at: str) -> dict:
    return {
        "api_version": "v1",
        "catalog_version": "catalog-td-v1",
        "data": rows,
        "dataset_id": dataset_id,
        "metadata": {
            "data_through": data_through,
            "degraded": False,
            "freshness": {"sla_seconds": 604800, "stale": False, "state": "fresh"},
            "lineage": {
                "authority": "sqlite_ingest_receipts", "complete": True,
                "dataset_id": dataset_id, "provider_neutral": True,
                "receipt_watermark": _sha(f"{dataset_id}:watermark"),
                "state": "complete", "transport_profile_id": "td-v1",
                "transport_profile_sha256": _sha("td-profile"),
                "transport_service": "quicksync",
            },
            "observed_at": observed_at,
            "quality": {"evidence": [], "state": "valid", "valid": True},
            "reasons": [], "receipt_id": f"receipt:{dataset_id}",
            "requested_as_of": None, "resolved_as_of": None,
            "runtime_state": "success", "state": "ready",
        },
        "next_cursor": None,
        "request_id": _sha(f"{dataset_id}:request")[:24],
        "schema_version": "2.0.0",
    }


def _td_authorities(envelopes: dict[str, dict]) -> dict[str, dict]:
    def source(dataset_id: str) -> dict[str, str]:
        envelope = envelopes[dataset_id]
        metadata = envelope["metadata"]
        binding = {
            "dataset_id": dataset_id,
            "catalog_version": envelope["catalog_version"],
            "receipt_id": metadata["receipt_id"],
            "data_through": metadata["data_through"],
            "observed_at": metadata["observed_at"],
            "freshness": metadata["freshness"],
            "quality": metadata["quality"],
            "lineage": metadata["lineage"],
        }
        return {
            "receiptId": metadata["receipt_id"],
            "receiptSha256": observation_worker._canonical_sha256(binding),
            "lineageSha256": observation_worker._canonical_sha256(metadata["lineage"]),
        }

    calendar = envelopes["cn.market.trade_calendar"]
    calendar_source = source("cn.market.trade_calendar")
    calendar_binding = {
        "id": "sse-calendar",
        "version": "2026.08.v1",
        "sourceDatasetId": "cn.market.trade_calendar",
        **calendar_source,
        "calendarSha256": _sha("calendar-content"),
    }
    result = {
        "cn.market.trade_calendar": {
            "datasetId": "cn.market.trade_calendar",
            "market": "ashare",
            "timezone": "Asia/Shanghai",
            "calendar": calendar_binding,
            "session": {"state": "closed", "asOf": calendar["metadata"]["observed_at"]},
            "dataThrough": calendar["metadata"]["data_through"],
            "source": calendar_source,
        }
    }
    for dataset_id in ("cn.equity.daily", "cn.equity.security_master"):
        result[dataset_id] = {
            "datasetId": dataset_id,
            "market": "ashare",
            "timezone": "Asia/Shanghai",
            "calendar": calendar_binding,
            "session": {"state": "closed", "asOf": calendar["metadata"]["observed_at"]},
            "dataThrough": envelopes[dataset_id]["metadata"]["data_through"],
            "source": source(dataset_id),
        }
    return result


def test_build_td_projection_batch_and_validate_with_existing_publisher(tmp_path: Path) -> None:
    envelopes = {
        "cn.equity.daily": _td_envelope(
            "cn.equity.daily",
            [{"ts_code": "600000.SH", "trade_date": "20260811", "open": 9.27,
              "high": 9.34, "low": 9.18, "close": 9.21, "pre_close": 9.29,
              "vol": 509424.33, "amount": 470381.696}],
            data_through="2026-08-11T00:00:00+08:00",
            observed_at="2026-08-11T08:31:26.100730+00:00",
        ),
        "cn.equity.security_master": _td_envelope(
            "cn.equity.security_master",
            [{"ts_code": "600000.SH", "symbol": "600000", "name": "浦发银行",
              "area": "上海", "industry": "银行", "market": "主板",
              "list_date": "19991110", "list_status": "L"}],
            data_through="2026-08-11T10:35:04.025706+00:00",
            observed_at="2026-08-11T10:40:11.953029+00:00",
        ),
        "cn.market.trade_calendar": _td_envelope(
            "cn.market.trade_calendar",
            [{"exchange": "SSE", "cal_date": "20260811", "is_open": 1,
              "pretrade_date": "20260810"}],
            data_through="2026-08-10T16:30:28.787756+00:00",
            observed_at="2026-08-10T16:30:28.787756+00:00",
        ),
    }
    paths = {}
    for dataset_id, envelope in envelopes.items():
        path = (tmp_path / f"{dataset_id.replace('.', '_')}.json").resolve()
        path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        paths[dataset_id] = path
    authorities = (tmp_path / "activity-authorities.json").resolve()
    authorities.write_text(json.dumps(_td_authorities(envelopes), ensure_ascii=False), encoding="utf-8")
    batch_path = (tmp_path / "batch.json").resolve()
    batch = build_td_projection_batch(
        daily_envelope_path=paths["cn.equity.daily"],
        security_master_envelope_path=paths["cn.equity.security_master"],
        trade_calendar_envelope_path=paths["cn.market.trade_calendar"],
        activity_authorities_path=authorities,
        generated_at=datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),
        valid_until=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
    )
    assert batch["contractId"] == "tradingagent.trading_copilot_projection_batch_input.v2"
    assert batch["items"][0]["symbol"] == "600000.SH"
    cli_batch_path = (tmp_path / "cli-batch.json").resolve()
    assert observation_worker.main([
        "--td-daily-envelope", str(paths["cn.equity.daily"]),
        "--td-security-master-envelope", str(paths["cn.equity.security_master"]),
        "--td-trade-calendar-envelope", str(paths["cn.market.trade_calendar"]),
        "--activity-authorities", str(authorities),
        "--decision-time", "2026-08-12T01:00:00+00:00",
        "--valid-until", "2026-08-13T01:00:00+00:00",
        "--batch-output", str(cli_batch_path),
    ]) == 0
    assert json.loads(cli_batch_path.read_text(encoding="utf-8"))["contractId"] == batch["contractId"]
    batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    result = publish_projection_batch(
        input_path=batch_path,
        output_root=(tmp_path / "private-publisher-output").resolve(),
        now=datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "pass"
    assert result["authority"]["realTradingEnabled"] is False


def test_td_projection_requires_explicit_calendar_authority(tmp_path: Path) -> None:
    envelopes = {
        "cn.equity.daily": _td_envelope(
            "cn.equity.daily", [{"ts_code": "600000.SH", "trade_date": "20260811", "open": 9.27,
                                  "high": 9.34, "low": 9.18, "close": 9.21, "pre_close": 9.29, "vol": 1}],
            data_through="2026-08-11T00:00:00+08:00", observed_at="2026-08-11T08:31:26+00:00"),
        "cn.equity.security_master": _td_envelope(
            "cn.equity.security_master", [{"ts_code": "600000.SH", "name": "浦发银行", "area": "上海",
                                            "industry": "银行", "market": "主板", "list_date": "19991110"}],
            data_through="2026-08-11T10:35:04+00:00", observed_at="2026-08-11T10:40:11+00:00"),
        "cn.market.trade_calendar": _td_envelope(
            "cn.market.trade_calendar", [{"exchange": "SSE", "cal_date": "20260811", "is_open": 1}],
            data_through="2026-08-10T16:30:28+00:00", observed_at="2026-08-10T16:30:28+00:00"),
    }
    paths = {}
    for dataset_id, envelope in envelopes.items():
        path = (tmp_path / f"{dataset_id.replace('.', '_')}.json").resolve()
        path.write_text(json.dumps(envelope), encoding="utf-8")
        paths[dataset_id] = path
    authorities = (tmp_path / "authorities.json").resolve()
    authorities.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(
        TradingCopilotObservationError,
        match="copilot_td_activity_authorities_invalid",
    ):
        build_td_projection_batch(
            daily_envelope_path=paths["cn.equity.daily"],
            security_master_envelope_path=paths["cn.equity.security_master"],
            trade_calendar_envelope_path=paths["cn.market.trade_calendar"],
            activity_authorities_path=authorities,
            generated_at=datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),
            valid_until=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
