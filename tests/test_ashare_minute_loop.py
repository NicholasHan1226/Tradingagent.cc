from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from Ashare.minute_data import (
    MinuteBarEvidence,
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    MinuteTimestampSemantics,
)
from Ashare.minute_loop import (
    MinuteAuxiliaryEvidence,
    MinuteFixtureClosedLoop,
    MinuteLoopContractError,
    SLEEVE_IDS,
    _canonical_sha256,
)
from Ashare.minute_research import (
    MinuteResearchUniverse,
    MinuteUniverseInstrument,
)
from shared.review.decision_ledger import ExposureDisposition


def _sha(character: str) -> str:
    return character * 64


def _profile() -> MinuteDatasetProfile:
    fields = (
        "ts_code",
        "bar_time",
        "freq",
        "open",
        "high",
        "low",
        "close",
        "vol",
        "amount",
        "pre_close",
        "suspended",
    )
    return MinuteDatasetProfile(
        expected_catalog_version="fixture-minute-catalog-v1",
        observed_catalog_version="fixture-minute-catalog-v1",
        dataset_id="fixture.cn.equity.five_minute",
        schema_major=1,
        default_fields=fields,
        default_order=("ts_code:asc", "bar_time:asc"),
        filter_operators=tuple((field, ("eq",)) for field in fields),
        dataset_contract_fingerprint=_sha("1"),
        consumer_profile_sha256=_sha("2"),
        identity_fields=("ts_code", "bar_time"),
        symbol_field="ts_code",
        timestamp_field="bar_time",
        open_field="open",
        high_field="high",
        low_field="low",
        close_field="close",
        volume_field="vol",
        amount_field="amount",
        previous_close_field="pre_close",
        suspension_field="suspended",
        frequency_field="freq",
        frequency_value="5min",
        timestamp_format="%Y%m%d %H:%M:%S",
        timestamp_semantics=MinuteTimestampSemantics.BAR_END,
        volume_multiplier_to_shares=1.0,
        amount_multiplier_to_cny=1.0,
        price_adjustment="raw_unadjusted",
        max_pages=1,
        max_rows=10,
        page_limit=10,
    )


def _bar(
    end: str,
    *,
    symbol: str,
    close: float,
    volume: int,
) -> MinuteBarEvidence:
    bar_end = datetime.fromisoformat(end)
    return MinuteBarEvidence(
        symbol=symbol,
        bar_start=bar_end - timedelta(minutes=5),
        bar_end=bar_end,
        open_cny=close - 0.05,
        high_cny=close + 0.1,
        low_cny=close - 0.1,
        close_cny=close,
        volume_shares=volume,
        amount_cny=volume * close,
        previous_close_cny=9.8,
        suspended=False,
        market_session=(
            "continuous_auction_am" if bar_end.hour < 12 else "continuous_auction_pm"
        ),
        dataset_id="fixture.cn.equity.five_minute",
        catalog_version="fixture-minute-catalog-v1",
        receipt_id=f"receipt-{symbol}-{end}",
        data_through=bar_end,
        observed_at=bar_end + timedelta(seconds=15),
        available_at=bar_end + timedelta(seconds=15),
        decision_time=bar_end + timedelta(seconds=20),
        source_lineage_sha256=_sha("2"),
        envelope_proof_sha256=_sha("3"),
        source_row_sha256=_sha("4"),
        reference_evidence_sha256=_sha("5"),
    )


def _snapshot(end: str, *, close: float, volume: int) -> MinuteBarSnapshot:
    bars = (
        _bar(end, symbol="600000.SH", close=close, volume=volume),
        _bar(end, symbol="000001.SZ", close=close * 0.99, volume=volume - 1_000),
    )
    return MinuteBarSnapshot(
        profile=_profile(),
        bars=bars,
        page_count=1,
        row_count=2,
        pagination_trace_sha256=_sha("5"),
        first_semantic_sha256=_sha("6"),
        replay_semantic_sha256=_sha("6"),
        same_observation=True,
    )


def _universe() -> MinuteResearchUniverse:
    return MinuteResearchUniverse(
        instruments=(
            MinuteUniverseInstrument(
                symbol="600000.SH",
                name="AI fixture",
                industry="electronics",
                research_theme="ai_semiconductor_infrastructure",
                list_date=date(1999, 11, 10),
            ),
            MinuteUniverseInstrument(
                symbol="000001.SZ",
                name="Robot fixture",
                industry="automation",
                research_theme="robotics_industrial_automation",
                list_date=date(1991, 4, 3),
            ),
            MinuteUniverseInstrument(
                symbol="399006.SZ",
                name="ChiNext context",
                industry="broad",
                research_theme="broad_market_control",
                list_date=None,
                context_only=True,
            ),
        )
    )


def _large_universe_and_snapshot(
    end: str,
    *,
    close_offset: float,
    volume_offset: int,
) -> tuple[MinuteResearchUniverse, MinuteBarSnapshot]:
    symbols = tuple(
        [f"600{index:03d}.SH" for index in range(250)]
        + [f"000{index:03d}.SZ" for index in range(1, 251)]
    )
    universe = MinuteResearchUniverse(
        instruments=tuple(
            MinuteUniverseInstrument(
                symbol=symbol,
                name=f"Mainboard fixture {index}",
                industry="mainboard_scan",
                research_theme="mainboard_opportunity_scan",
                list_date=date(2000, 1, 1),
            )
            for index, symbol in enumerate(symbols)
        )
    )
    bars = tuple(
        _bar(
            end,
            symbol=symbol,
            close=10.0 + close_offset + index / 10_000,
            volume=100_000 + volume_offset + index,
        )
        for index, symbol in enumerate(symbols)
    )
    snapshot = MinuteBarSnapshot(
        profile=replace(_profile(), max_rows=500, page_limit=500),
        bars=bars,
        page_count=1,
        row_count=500,
        pagination_trace_sha256=_sha("5"),
        first_semantic_sha256=_sha("6"),
        replay_semantic_sha256=_sha("6"),
        same_observation=True,
    )
    return universe, snapshot


def _auxiliary(end: str) -> tuple[MinuteAuxiliaryEvidence, ...]:
    decision = datetime.fromisoformat(end) + timedelta(seconds=20)
    event = datetime.fromisoformat(end)
    return (
        MinuteAuxiliaryEvidence(
            symbol="600000.SH",
            evidence_type="event",
            normalized_score=0.4,
            event_time=event,
            available_at=event + timedelta(seconds=10),
            decision_time=decision,
            expires_at=event + timedelta(minutes=5),
            evidence_sha256=_sha("7"),
        ),
        MinuteAuxiliaryEvidence(
            symbol="600000.SH",
            evidence_type="flow",
            normalized_score=0.3,
            event_time=event,
            available_at=event + timedelta(seconds=10),
            decision_time=decision,
            expires_at=event + timedelta(minutes=5),
            evidence_sha256=_sha("8"),
        ),
    )


def test_500_symbol_snapshot_reaches_features_and_candidates_without_truncation() -> (
    None
):
    universe, first_snapshot = _large_universe_and_snapshot(
        "2026-07-27T09:35:00+08:00",
        close_offset=0.0,
        volume_offset=0,
    )
    loop = MinuteFixtureClosedLoop(universe=universe)
    manifest = _sha("f")

    first = loop.process_snapshot(
        snapshot=first_snapshot,
        manifest_sha256=manifest,
    )
    assert first.feature_count == 0
    assert first.candidate_count == 0

    _, second_snapshot = _large_universe_and_snapshot(
        "2026-07-27T09:40:00+08:00",
        close_offset=0.1,
        volume_offset=20_000,
    )
    second = loop.process_snapshot(
        snapshot=second_snapshot,
        manifest_sha256=manifest,
    )

    assert second.feature_count == 500
    assert second.candidate_count == 500
    assert second_snapshot.row_count == 500
    assert all(step.ranked_count == 500 for step in second.sleeves)
    assert all(
        step.scheduled_order is None
        or step.scheduled_order.requested_notional_cny <= 7_500
        for step in second.sleeves
    )


def test_closed_loop_schedules_only_after_features_then_settles_next_bar() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("a")
    first = loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    assert first.feature_count == 0
    assert loop.pending == {}

    second_end = "2026-07-27T09:40:00+08:00"
    second = loop.process_snapshot(
        snapshot=_snapshot(second_end, close=10.1, volume=120_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary(second_end),
    )
    assert second.feature_count == 2
    assert set(loop.pending) == set(SLEEVE_IDS)
    assert all(
        item.scheduled_order is not None
        and item.scheduled_order.real_trading_enabled is False
        for item in second.sleeves
    )
    assert all(
        item.scheduled_order.symbol != "399006.SZ"
        for item in second.sleeves
        if item.scheduled_order is not None
    )

    third_end = "2026-07-27T09:45:00+08:00"
    third = loop.process_snapshot(
        snapshot=_snapshot(third_end, close=10.15, volume=130_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary(third_end),
    )
    assert all(item.settled_receipt is None for item in third.sleeves)
    assert set(loop.pending) == set(SLEEVE_IDS)

    fourth_end = "2026-07-27T09:50:00+08:00"
    fourth = loop.process_snapshot(
        snapshot=_snapshot(fourth_end, close=10.2, volume=140_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary(fourth_end),
    )
    assert all(item.settled_receipt is not None for item in fourth.sleeves)
    assert all(
        item.settled_receipt.status in {"filled", "partial"}
        for item in fourth.sleeves
        if item.settled_receipt is not None
    )
    assert all(item.reconciliation["reconciled"] for item in fourth.sleeves)
    assert all(
        loop.ledgers[sleeve].by_disposition(ExposureDisposition.PAPER_FILLED)
        for sleeve in SLEEVE_IDS
    )


def test_missing_auxiliary_is_audited_and_never_silently_falls_back() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("b")
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    step = loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000),
        manifest_sha256=manifest,
    )
    assert step.sleeves[1].scheduled_order is None
    assert step.sleeves[2].scheduled_order is None
    event_rejections = loop.ledgers["event"].by_disposition(
        ExposureDisposition.REJECTED
    )
    assert event_rejections
    assert all(
        item.rejection_reason == "minute_event_evidence_missing"
        for item in event_rejections
    )
    assert "baseline" in loop.pending
    assert "dynamic_position" in loop.pending


def test_expired_auxiliary_fails_closed_before_shadow_ranking() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("0")
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    end = "2026-07-27T09:40:00+08:00"
    event_time = datetime.fromisoformat(end) - timedelta(minutes=10)
    expired = MinuteAuxiliaryEvidence(
        symbol="600000.SH",
        evidence_type="event",
        normalized_score=0.4,
        event_time=event_time,
        available_at=event_time + timedelta(seconds=10),
        decision_time=event_time + timedelta(seconds=20),
        expires_at=event_time + timedelta(minutes=5),
        evidence_sha256=_sha("7"),
    )
    with pytest.raises(MinuteLoopContractError, match="expired"):
        loop.process_snapshot(
            snapshot=_snapshot(end, close=10.1, volume=120_000),
            manifest_sha256=manifest,
            auxiliary_evidence=(expired,),
        )


def test_skipped_execution_bar_is_nonfill_not_same_bar_or_late_fill() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("c")
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    second_end = "2026-07-27T09:40:00+08:00"
    loop.process_snapshot(
        snapshot=_snapshot(second_end, close=10.1, volume=120_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary(second_end),
    )
    late_end = "2026-07-27T09:55:00+08:00"
    loop.process_snapshot(
        snapshot=_snapshot(late_end, close=10.2, volume=130_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary(late_end),
    )
    baseline_nonfills = loop.ledgers["baseline"].by_disposition(
        ExposureDisposition.PAPER_NOT_FILLED
    )
    assert baseline_nonfills
    assert (
        baseline_nonfills[0].nonfill_reason
        == "minute_execution_not_first_reachable_bar"
    )
    assert loop.counterfactual_books["baseline"].positions == {}


def test_unaffordable_top_symbol_abstains_instead_of_failing_the_bar() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("c")
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    # 600000.SH rallies to 80 CNY: top raw score, but one lot (8000 CNY)
    # exceeds the 7500 CNY single-name cap, so no whole lot is affordable.
    expensive = _bar(
        "2026-07-27T09:40:00+08:00",
        symbol="600000.SH",
        close=80.0,
        volume=150_000,
    )
    normal = _bar(
        "2026-07-27T09:40:00+08:00",
        symbol="000001.SZ",
        close=10.0,
        volume=110_000,
    )
    snapshot = MinuteBarSnapshot(
        profile=_profile(),
        bars=(expensive, normal),
        page_count=1,
        row_count=2,
        pagination_trace_sha256=_sha("5"),
        first_semantic_sha256=_sha("6"),
        replay_semantic_sha256=_sha("6"),
        same_observation=True,
    )

    step = loop.process_snapshot(snapshot=snapshot, manifest_sha256=manifest)

    for sleeve_step in step.sleeves:
        assert sleeve_step.scheduled_order is None
    for sleeve_id in ("baseline", "dynamic_position"):
        rejections = loop.ledgers[sleeve_id].by_disposition(
            ExposureDisposition.REJECTED
        )
        assert any(
            record.rejection_reason == "minute_symbol_too_expensive_for_account"
            for record in rejections
        )
    assert loop.counterfactual_books["baseline"].positions == {}
    assert not loop.pending


def test_data_failure_records_every_trade_symbol_and_adds_no_risk() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    loop.record_data_failure(
        decision_time=datetime.fromisoformat("2026-07-27T09:35:20+08:00"),
        manifest_sha256=_sha("d"),
        reason_code="minute_metadata_stale",
    )
    assert loop.pending == {}
    for sleeve in SLEEVE_IDS:
        records = loop.ledgers[sleeve].by_disposition(ExposureDisposition.REJECTED)
        assert {item.symbol for item in records} == {"600000.SH", "000001.SZ"}
        assert all(item.action == "hold" for item in records)
        assert all(item.requested_notional_cny == 0 for item in records)


def test_data_failure_cancels_pending_new_risk_and_human_reject_is_audited() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("9")
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    second_end = "2026-07-27T09:40:00+08:00"
    loop.process_snapshot(
        snapshot=_snapshot(second_end, close=10.1, volume=120_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary(second_end),
    )
    assert set(loop.pending) == set(SLEEVE_IDS)
    loop.reject_pending_by_human(
        sleeve_id="event", reason_code="fixture_manual_review_rejected"
    )
    assert "event" not in loop.pending
    human_rejections = loop.ledgers["event"].by_disposition(
        ExposureDisposition.REJECTED
    )
    assert any(
        item.rejection_reason == "fixture_manual_review_rejected"
        for item in human_rejections
    )

    loop.record_data_failure(
        decision_time=datetime.fromisoformat("2026-07-27T09:45:20+08:00"),
        manifest_sha256=manifest,
        reason_code="minute_metadata_stale",
    )
    assert loop.pending == {}
    for sleeve in ("baseline", "flow", "dynamic_position"):
        nonfills = loop.ledgers[sleeve].by_disposition(
            ExposureDisposition.PAPER_NOT_FILLED
        )
        assert any(
            item.nonfill_reason
            == "minute_data_failure_before_execution:minute_metadata_stale"
            for item in nonfills
        )


def test_gap_recovery_cancels_pending_and_restarts_features_without_order() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("9")
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary("2026-07-27T09:40:00+08:00"),
    )
    assert set(loop.pending) == set(SLEEVE_IDS)

    loop.resume_after_gap(
        decision_time=datetime.fromisoformat("2026-07-27T09:55:20+08:00"),
        manifest_sha256=manifest,
        reason_code="minute_session_gap_detected",
        skipped_session_slots=(
            "2026-07-27 09:45:00",
            "2026-07-27 09:50:00",
        ),
    )
    assert loop.pending == {}

    resumed = loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:55:00+08:00", close=10.2, volume=130_000),
        manifest_sha256=manifest,
    )
    assert resumed.feature_count == 0
    assert resumed.candidate_count == 0
    assert all(step.scheduled_order is None for step in resumed.sleeves)

    next_step = loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T10:00:00+08:00", close=10.3, volume=140_000),
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary("2026-07-27T10:00:00+08:00"),
    )
    assert next_step.feature_count == 2


def test_restart_preserves_pending_books_ledgers_and_blocks_replay() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("e")
    first_snapshot = _snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000)
    loop.process_snapshot(snapshot=first_snapshot, manifest_sha256=manifest)
    second_end = "2026-07-27T09:40:00+08:00"
    second_snapshot = _snapshot(second_end, close=10.1, volume=120_000)
    loop.process_snapshot(
        snapshot=second_snapshot,
        manifest_sha256=manifest,
        auxiliary_evidence=_auxiliary(second_end),
    )
    state = loop.export_state()
    restored = MinuteFixtureClosedLoop.restore(state)
    assert set(restored.pending) == set(SLEEVE_IDS)
    assert restored.counterfactual_books.export_state() == (
        loop.counterfactual_books.export_state()
    )
    with pytest.raises(MinuteLoopContractError, match="already_processed"):
        restored.process_snapshot(
            snapshot=second_snapshot,
            manifest_sha256=manifest,
            auxiliary_evidence=_auxiliary(second_end),
        )

    state["minimum_raw_score"] = 999
    with pytest.raises(MinuteLoopContractError, match="integrity"):
        MinuteFixtureClosedLoop.restore(state)


def test_legacy_state_derives_contiguous_accepted_slots_with_integrity() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    manifest = _sha("e")
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000),
        manifest_sha256=manifest,
    )
    loop.process_snapshot(
        snapshot=_snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000),
        manifest_sha256=manifest,
    )
    state = loop.export_state()
    payload = dict(state)
    payload.pop("state_sha256")
    payload.pop("accepted_bar_ends")
    payload.pop("session_gaps")
    legacy = {**payload, "state_sha256": _canonical_sha256(payload)}

    restored = MinuteFixtureClosedLoop.restore(legacy)

    assert restored.accepted_bar_ends == (
        "2026-07-27 09:35:00",
        "2026-07-27 09:40:00",
    )
    assert restored.session_gaps == ()


def test_attribution_keeps_four_fixture_accounts_separate() -> None:
    loop = MinuteFixtureClosedLoop(universe=_universe())
    attribution = loop.attribution_snapshot(marks={})
    assert attribution["primary_sleeve"] == "baseline"
    assert attribution["durable"] is False
    assert attribution["real_trading_enabled"] is False
    assert set(attribution["sleeves"]) == set(SLEEVE_IDS)
    assert all(item["equity_cny"] == 50_000 for item in attribution["sleeves"].values())
