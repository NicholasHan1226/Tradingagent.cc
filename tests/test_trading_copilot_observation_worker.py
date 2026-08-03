from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from Ashare.event_evidence import (
    AshareEvidenceContractError,
    EvidenceDatasetProfile,
    EventEvidenceSnapshot,
    PRIMARY_DATASET_IDS,
)
from Ashare.minute_data import (
    MinuteBarEvidence,
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    MinuteEvidenceUse,
    MinuteTimestampSemantics,
)
from Ashare.trading_copilot_observation_worker import (
    TradingCopilotObservationError,
    build_projection_batch,
    company_facts_from_verified_observation,
    load_event_bundle,
    load_company_facts,
    load_current_event_snapshots,
)
import Ashare.trading_copilot_observation_worker as observation_worker
from Ashare.trading_copilot_projection import publish_projection_batch
from Ashare.trading_copilot_event_timeline import build_event_timeline_batch, publish_event_timeline_batch


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
        "dataThrough": "2026-08-01T01:40:00+00:00", "retrievedAt": "2026-08-01T01:40:05+00:00",
        "freshness": "fresh", "adjustment": "none",
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
        "minute-receipt", "company-receipt", "event-receipt"
    }


def test_event_projection_preserves_the_verified_data_capability() -> None:
    generated = datetime(2026, 8, 1, 1, 40, 10, tzinfo=timezone.utc)
    batch = build_projection_batch(
        snapshot=_snapshot(), company_facts=_companies(), events=(_event(),),
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
        observed_at="2026-08-01T07:01:00+00:00",
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
