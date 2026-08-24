from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path

import pytest

from Ashare.minute_canary import MinuteCanaryConfig
from Ashare.minute_data import (
    MinuteBarEvidence,
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    MinuteEvidenceAuditLedger,
    MinuteEvidenceUse,
    MinuteRowRejection,
    MinuteTimestampSemantics,
)
from Ashare.minute_event_aux import HITS_FILENAME, MinuteEventAuxError
from Ashare.minute_loop import MinuteFixtureClosedLoop, MinuteLoopContractError, _canonical_sha256
from Ashare.minute_paper_runner import (
    MinutePaperRunnerError,
    run_delayed_minute_paper_once,
)


def _sha(character: str) -> str:
    return character * 64


def _profile() -> MinuteDatasetProfile:
    fields = (
        "ts_code",
        "time",
        "open",
        "high",
        "low",
        "close",
        "vol",
        "amount",
    )
    return MinuteDatasetProfile(
        expected_catalog_version="fixture-rt-min-v1",
        observed_catalog_version="fixture-rt-min-v1",
        dataset_id="fixture.cn.dataset.rt_min",
        schema_major=2,
        default_fields=fields,
        default_order=("ts_code:asc", "time:asc"),
        filter_operators=tuple((field, ("eq",)) for field in fields),
        dataset_contract_fingerprint=_sha("1"),
        consumer_profile_sha256=_sha("2"),
        identity_fields=("ts_code", "time"),
        symbol_field="ts_code",
        timestamp_field="time",
        open_field="open",
        high_field="high",
        low_field="low",
        close_field="close",
        volume_field="vol",
        amount_field="amount",
        previous_close_field=None,
        suspension_field=None,
        frequency_field=None,
        frequency_value=None,
        timestamp_format="%Y-%m-%d %H:%M:%S",
        timestamp_semantics=MinuteTimestampSemantics.BAR_END,
        volume_multiplier_to_shares=1.0,
        amount_multiplier_to_cny=1.0,
        price_adjustment="raw_unadjusted",
        max_pages=1,
        max_rows=10,
        page_limit=10,
    )


def _snapshot(end: str, close: float) -> MinuteBarSnapshot:
    bar_end = datetime.fromisoformat(end)
    bars = []
    for index, symbol in enumerate(("600000.SH", "000001.SZ")):
        value = close - index * 0.05
        bars.append(
            MinuteBarEvidence(
                symbol=symbol,
                bar_start=bar_end - timedelta(minutes=5),
                bar_end=bar_end,
                open_cny=value - 0.02,
                high_cny=value + 0.10,
                low_cny=value - 0.10,
                close_cny=value,
                volume_shares=100_000 + index * 1_000,
                amount_cny=(100_000 + index * 1_000) * value,
                previous_close_cny=9.8,
                suspended=False,
                market_session="continuous_auction_am",
                dataset_id="fixture.cn.dataset.rt_min",
                catalog_version="fixture-rt-min-v1",
                receipt_id=f"receipt-{symbol}-{end}",
                data_through=bar_end + timedelta(minutes=5),
                observed_at=bar_end + timedelta(minutes=5, seconds=6),
                available_at=bar_end + timedelta(minutes=5, seconds=6),
                decision_time=bar_end + timedelta(minutes=5, seconds=7),
                source_lineage_sha256=_sha("2"),
                envelope_proof_sha256=_sha("3"),
                source_row_sha256=_sha("4"),
                reference_evidence_sha256=_sha("5"),
                evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
            )
        )
    return MinuteBarSnapshot(
        profile=_profile(),
        bars=tuple(bars),
        page_count=1,
        row_count=2,
        pagination_trace_sha256=_sha("6"),
        first_semantic_sha256=_sha("7"),
        replay_semantic_sha256=_sha("7"),
        same_observation=True,
    )


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    manifest = tmp_path / "manifest.json"
    references = tmp_path / "references.json"
    universe = tmp_path / "universe.json"
    manifest.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:18082",
                "expected_catalog_version": "fixture-rt-min-v1",
                "dataset_id": "fixture.cn.dataset.rt_min",
                "access_policy_id": "fixture",
                "transport_id": "tradingdatas-v1-bearer",
                "timeout_seconds": 5,
                "filters": {},
                "profile": {
                    "timestamp_field": "time",
                    "symbol_field": "ts_code",
                    "page_limit": 10,
                },
            }
        ),
        encoding="utf-8",
    )
    references.write_text(
        json.dumps(
            [
                {
                    "symbol": symbol,
                    "trade_date": "2026-07-28",
                    "previous_close_cny": 9.8,
                    "suspended": False,
                    "evidence_sha256": _sha(character),
                }
                for symbol, character in (("600000.SH", "8"), ("000001.SZ", "9"))
            ]
        ),
        encoding="utf-8",
    )
    universe.write_text(
        json.dumps(
            [
                {
                    "symbol": "600000.SH",
                    "name": "AI fixture",
                    "industry": "electronics",
                    "research_theme": "ai_semiconductor_infrastructure",
                    "list_date": "1999-11-10",
                },
                {
                    "symbol": "000001.SZ",
                    "name": "Robot fixture",
                    "industry": "automation",
                    "research_theme": "robotics_industrial_automation",
                    "list_date": "1991-04-03",
                },
            ]
        ),
        encoding="utf-8",
    )
    return manifest, references, universe


def test_runner_pins_exact_universe_filter_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, references, universe = _write_inputs(tmp_path)
    seen_filters: list[dict] = []

    def fake_load(config: MinuteCanaryConfig, **kwargs):
        seen_filters.append(dict(config.filters))
        return _profile(), _snapshot("2026-07-28T09:35:00+08:00", 10.0), MinuteEvidenceAuditLedger()

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot",
        fake_load,
    )
    state = tmp_path / "state" / "bundle.json"
    run_delayed_minute_paper_once(
        manifest=manifest,
        reference_facts_path=references,
        universe_path=universe,
        token_file=tmp_path / "token",
        state_bundle=state,
        decision_time=datetime.fromisoformat("2026-07-28T09:40:07+08:00"),
        trading_date=date(2026, 7, 28),
        bar_end="2026-07-28 09:35:00",
        pin_universe_filter=True,
    )
    assert seen_filters == [
        {
            "time": {"eq": "2026-07-28 09:35:00"},
            "ts_code": {"in": ("000001.SZ", "600000.SH")},
        }
    ]


def test_runner_returns_proof_bound_partial_before_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, references, universe = _write_inputs(tmp_path)
    full = _snapshot("2026-07-28T09:35:00+08:00", 10.0)
    partial = replace(full, bars=full.bars[:1], row_count=1)

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot",
        lambda *_args, **_kwargs: (
            _profile(),
            partial,
            MinuteEvidenceAuditLedger(),
        ),
    )
    state = tmp_path / "state" / "bundle.json"
    result = run_delayed_minute_paper_once(
        manifest=manifest,
        reference_facts_path=references,
        universe_path=universe,
        token_file=tmp_path / "token",
        state_bundle=state,
        decision_time=datetime.fromisoformat("2026-07-28T09:40:07+08:00"),
        trading_date=date(2026, 7, 28),
        bar_end="2026-07-28 09:35:00",
        pin_universe_filter=True,
        partial_observation_minimum=1,
    )

    assert result["status"] == "partial_observation"
    assert result["accepted_count"] == 1
    assert result["missing_count"] == 1
    assert result["proof_complete"] is True
    assert result["lineage_complete"] is True
    assert len(result["per_row_evidence"]) == 1
    assert not state.exists()


def test_runner_persists_fixture_state_and_waits_for_reachable_fill(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, references, universe = _write_inputs(tmp_path)
    snapshots = iter(
        (
            _snapshot("2026-07-28T09:35:00+08:00", 10.0),
            _snapshot("2026-07-28T09:40:00+08:00", 10.1),
            _snapshot("2026-07-28T09:45:00+08:00", 10.15),
            _snapshot("2026-07-28T09:50:00+08:00", 10.2),
            _snapshot("2026-07-28T09:55:00+08:00", 10.25),
        )
    )
    seen_filters: list[dict] = []

    def fake_load(config: MinuteCanaryConfig, **kwargs):
        seen_filters.append(dict(config.filters))
        return _profile(), next(snapshots), MinuteEvidenceAuditLedger()

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot",
        fake_load,
    )
    state = tmp_path / "state" / "bundle.json"
    receipts = []
    for end in ("09:35:00", "09:40:00", "09:45:00", "09:50:00", "09:55:00"):
        receipts.append(
            run_delayed_minute_paper_once(
                manifest=manifest,
                reference_facts_path=references,
                universe_path=universe,
                token_file=tmp_path / "token",
                state_bundle=state,
                decision_time=datetime.fromisoformat(f"2026-07-28T{end}+08:00")
                + timedelta(minutes=5, seconds=7),
                trading_date=date(2026, 7, 28),
                bar_end=f"2026-07-28 {end}",
            )
        )

    assert receipts[0]["feature_count"] == 0
    assert receipts[1]["feature_count"] == 2
    assert receipts[3]["pending_sleeves"]
    assert any(
        sleeve["settled_status"] in {"filled", "partial"}
        for sleeve in receipts[4]["sleeves"]
    )
    assert receipts[4]["authority_tier"] == "non_production_fixture"
    assert receipts[4]["execution_authority"] is False
    assert receipts[4]["real_trading_enabled"] is False
    assert seen_filters == [
        {"time": {"eq": f"2026-07-28 {end}"}}
        for end in ("09:35:00", "09:40:00", "09:45:00", "09:50:00", "09:55:00")
    ]
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["real_trading_enabled"] is False
    assert persisted["last_receipt"] == receipts[-1]
    assert oct(state.stat().st_mode & 0o777) == "0o600"


def test_runner_persists_gap_recovery_and_blocks_learning(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, references, universe = _write_inputs(tmp_path)
    snapshots = iter(
        (
            _snapshot("2026-07-28T09:35:00+08:00", 10.0),
            _snapshot("2026-07-28T09:45:00+08:00", 10.2),
        )
    )

    def fake_load(config: MinuteCanaryConfig, **kwargs):
        return _profile(), next(snapshots), MinuteEvidenceAuditLedger()

    monkeypatch.setattr("Ashare.minute_paper_runner.load_minute_snapshot", fake_load)
    state = tmp_path / "state" / "bundle.json"
    common = {
        "manifest": manifest,
        "reference_facts_path": references,
        "universe_path": universe,
        "token_file": tmp_path / "token",
        "state_bundle": state,
        "trading_date": date(2026, 7, 28),
    }
    run_delayed_minute_paper_once(
        **common,
        decision_time=datetime.fromisoformat("2026-07-28T09:45:07+08:00"),
        bar_end="2026-07-28 09:35:00",
    )
    receipt = run_delayed_minute_paper_once(
        **common,
        decision_time=datetime.fromisoformat("2026-07-28T09:55:07+08:00"),
        bar_end="2026-07-28 09:45:00",
        gap_recovery={
            "reason_code": "minute_session_gap_detected",
            "skipped_session_slots": ("2026-07-28 09:40:00",),
        },
    )

    assert receipt["feature_count"] == 0
    assert receipt["candidate_count"] == 0
    assert receipt["gap_recovery"] is True
    assert receipt["gap_slots"] == ["2026-07-28 09:40:00"]
    assert receipt["full_session_complete"] is False
    assert receipt["learning_eligible"] is False
    bundle = json.loads(state.read_text(encoding="utf-8"))
    assert bundle["loop_state"]["accepted_bar_ends"] == [
        "2026-07-28 09:35:00",
        "2026-07-28 09:45:00",
    ]
    assert bundle["loop_state"]["session_gaps"] == ["2026-07-28 09:40:00"]


def test_runner_rejects_mixed_bar_end_despite_complete_symbol_set(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, references, universe = _write_inputs(tmp_path)
    requested_end = "2026-07-28 10:20:00"
    snapshot = _snapshot("2026-07-28T10:20:00+08:00", 10.0)
    mixed_end = datetime.fromisoformat("2026-07-28T10:25:00+08:00")
    mixed_bar = replace(
        snapshot.bars[1],
        bar_start=mixed_end - timedelta(minutes=5),
        bar_end=mixed_end,
        data_through=mixed_end + timedelta(minutes=5),
        observed_at=mixed_end + timedelta(minutes=5),
        available_at=mixed_end + timedelta(minutes=5),
        decision_time=mixed_end + timedelta(minutes=5, seconds=1),
    )
    mixed_snapshot = replace(snapshot, bars=(snapshot.bars[0], mixed_bar))
    seen_filters: list[dict] = []

    def fake_load(config: MinuteCanaryConfig, **kwargs):
        seen_filters.append(dict(config.filters))
        return _profile(), mixed_snapshot, MinuteEvidenceAuditLedger()

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot",
        fake_load,
    )
    state = tmp_path / "state" / "bundle.json"

    with pytest.raises(MinuteLoopContractError, match="research_rejected") as error:
        run_delayed_minute_paper_once(
            manifest=manifest,
            reference_facts_path=references,
            universe_path=universe,
            token_file=tmp_path / "token",
            state_bundle=state,
            decision_time=datetime.fromisoformat("2026-07-28T10:35:01+08:00"),
            trading_date=date(2026, 7, 28),
            bar_end=requested_end,
        )

    assert error.value.__cause__ is not None
    assert str(error.value.__cause__) == "minute_snapshot_mixed_bar_end"
    assert seen_filters == [{"time": {"eq": requested_end}}]
    assert not state.exists()


def test_runner_persists_accepted_subset_without_blocking_the_universe(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, references, universe = _write_inputs(tmp_path)
    snapshot = _snapshot("2026-07-28T10:20:00+08:00", 10.0)
    partial_snapshot = replace(
        snapshot,
        bars=snapshot.bars[:1],
        row_count=1,
    )

    def fake_load(config: MinuteCanaryConfig, **kwargs):
        return _profile(), partial_snapshot, MinuteEvidenceAuditLedger()

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot",
        fake_load,
    )
    state = tmp_path / "state" / "bundle.json"

    result = run_delayed_minute_paper_once(
        manifest=manifest,
        reference_facts_path=references,
        universe_path=universe,
        token_file=tmp_path / "token",
        state_bundle=state,
        decision_time=datetime.fromisoformat("2026-07-28T10:30:01+08:00"),
        trading_date=date(2026, 7, 28),
        bar_end="2026-07-28 10:20:00",
    )

    assert result["status"] == "pass"
    assert result["coverage_status"] == "partial"
    assert result["requested_count"] == 2
    assert result["accepted_count"] == 1
    assert result["missing_count"] == 1
    assert state.exists()


def test_runner_keeps_row_quality_rejection_audit_only(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, references, universe = _write_inputs(tmp_path)
    snapshot = _snapshot("2026-07-28T10:20:00+08:00", 10.0)
    partial_snapshot = replace(snapshot, bars=snapshot.bars[:1], row_count=1)
    audit = MinuteEvidenceAuditLedger()
    audit.append_row_rejection(
        MinuteRowRejection(
            symbol="000001.SZ",
            reason_code="minute_open_invalid",
            dataset_id="fixture.cn.dataset.rt_min",
            catalog_version="fixture-rt-min-v1",
            rejected_payload_sha256=_sha("a"),
        )
    )

    def fake_load(config: MinuteCanaryConfig, **kwargs):
        return _profile(), partial_snapshot, audit

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot",
        fake_load,
    )
    state = tmp_path / "state" / "bundle.json"

    result = run_delayed_minute_paper_once(
        manifest=manifest,
        reference_facts_path=references,
        universe_path=universe,
        token_file=tmp_path / "token",
        state_bundle=state,
        decision_time=datetime.fromisoformat("2026-07-28T10:30:01+08:00"),
        trading_date=date(2026, 7, 28),
        bar_end="2026-07-28 10:20:00",
    )

    assert result["status"] == "pass"
    assert result["audit_rejections"] == 0
    assert result["row_rejection_count"] == 1
    assert result["row_rejections"][0]["symbol"] == "000001.SZ"
    assert state.exists()


def test_event_aux_cache_wires_lockup_evidence_into_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the cache present, the event sleeve receives real evidence."""

    manifest, references, universe = _write_inputs(tmp_path)
    source_binding = {
        "receipt_id": "td-receipt-001",
        "data_through": "2026-07-28T09:35:00+08:00",
        "observed_at": "2026-07-28T09:35:10+08:00",
        "catalog_version": "catalog-v1",
        "lineage": {"receipt": "lineage-001"},
        "pagination_trace_sha256": "a" * 64,
        "semantic_sha256": "b" * 64,
        "ordered_rows_sha256": "c" * 64,
        "row_receipt_proofs_sha256": "d" * 64,
    }
    source_binding["source_binding_sha256"] = _canonical_sha256(source_binding)
    hits = {
        "600000.SH": {
            "latest_float_date": "20260710",
            "max_ratio": 4.2,
        }
    }
    (tmp_path / HITS_FILENAME).write_text(
        json.dumps(
            {
                "schema": "tradingagent.ashare.minute_event_aux_hits.v2",
                "session_date": "2026-07-28",
                "fetched_at": "2026-07-28T09:35:10+08:00",
                "lookback_days": 30,
                "hit_count": 1,
                "hits": hits,
                "hits_sha256": _canonical_sha256(hits),
                "source_binding": source_binding,
            }
        ),
        encoding="utf-8",
    )
    captured: list[tuple] = []
    real_process = MinuteFixtureClosedLoop.process_snapshot

    def spying_process(
        self, *, snapshot, manifest_sha256, auxiliary_evidence=()
    ):
        captured.append(auxiliary_evidence)
        return real_process(
            self,
            snapshot=snapshot,
            manifest_sha256=manifest_sha256,
            auxiliary_evidence=auxiliary_evidence,
        )

    def fake_load(config: MinuteCanaryConfig, **kwargs):
        return (
            _profile(),
            _snapshot("2026-07-28T09:35:00+08:00", 10.0),
            MinuteEvidenceAuditLedger(),
        )

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot", fake_load
    )
    monkeypatch.setattr(MinuteFixtureClosedLoop, "process_snapshot", spying_process)
    state = tmp_path / "state" / "bundle.json"

    result = run_delayed_minute_paper_once(
        manifest=manifest,
        reference_facts_path=references,
        universe_path=universe,
        token_file=tmp_path / "token",
        state_bundle=state,
        decision_time=datetime.fromisoformat("2026-07-28T09:40:07+08:00"),
        trading_date=date(2026, 7, 28),
        bar_end="2026-07-28 09:35:00",
        event_aux_enabled=True,
    )

    assert result["status"] == "pass"
    assert result["event_aux_status"] == "ok:1"
    assert len(captured) == 1
    evidence = captured[0]
    assert len(evidence) == 1
    assert evidence[0].symbol == "600000.SH"
    assert evidence[0].normalized_score == 1.0
    assert evidence[0].execution_authority is False


def test_event_aux_fetch_failure_degrades_without_failing_the_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache miss plus a dead feed degrades to the abstain status quo."""

    manifest, references, universe = _write_inputs(tmp_path)

    def broken_client(**kwargs):
        raise MinuteEventAuxError("minute_event_aux_catalog_failed:offline")

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.make_session_client", broken_client
    )
    def fake_load(config: MinuteCanaryConfig, **kwargs):
        return (
            _profile(),
            _snapshot("2026-07-28T09:35:00+08:00", 10.0),
            MinuteEvidenceAuditLedger(),
        )

    monkeypatch.setattr(
        "Ashare.minute_paper_runner.load_minute_snapshot", fake_load
    )
    state = tmp_path / "state" / "bundle.json"

    result = run_delayed_minute_paper_once(
        manifest=manifest,
        reference_facts_path=references,
        universe_path=universe,
        token_file=tmp_path / "token",
        state_bundle=state,
        decision_time=datetime.fromisoformat("2026-07-28T09:40:07+08:00"),
        trading_date=date(2026, 7, 28),
        bar_end="2026-07-28 09:35:00",
        event_aux_enabled=True,
    )

    assert result["status"] == "pass"
    assert result["event_aux_status"].startswith(
        "degraded:minute_event_aux_catalog_failed"
    )
    assert not (tmp_path / HITS_FILENAME).exists()
