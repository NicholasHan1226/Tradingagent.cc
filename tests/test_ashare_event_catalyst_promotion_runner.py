"""Contract tests for the automatic promotion-gate runner."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json

import pytest

from Ashare.event_catalyst_shadow import (
    CatalystEntry,
    DailyBar,
    build_catalyst_shadow_batch,
)
from Ashare.event_catalyst_journal import append_shadow_batch_to_journal
from Ashare.event_catalyst_promotion import SCORED_HYPOTHESES
from Ashare.event_catalyst_promotion_runner import (
    EVENT_CATALYST_RUNNER_CONTRACT,
    EventCatalystRunnerError,
    decision_record_from_decision,
    labels_from_journal_records,
    load_frozen_policy,
    main,
    run_promotion_gate,
)
from Ashare.event_catalyst_promotion import PromotionPolicy, evaluate_promotion
from shared.review.sample_journal import SampleJournal


AS_OF = datetime.fromisoformat("2026-08-14T18:00:00+08:00")
SYMBOL = "600519.SH"
EVENT_DATE = date(2026, 8, 5)

POLICY_DOC = {
    "policy_id": "event-catalyst-promotion-v1",
    "min_labeled_observations": 60,
    "min_distinct_event_clusters": 10,
    "min_time_windows": 4,
    "min_window_hit_rate": 0.6,
    "cost_per_round_trip": 0.003,
    "demote_recent_labels": 20,
    "demote_max_hit_rate": 0.4,
}


def _policy_file(tmp_path) -> tuple:
    policy = PromotionPolicy(**POLICY_DOC)
    doc = dict(POLICY_DOC)
    doc["policy_sha256"] = policy.policy_sha256
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path, policy


def _bars(start: date, count: int, *, step: float = 0.5):
    bars = []
    current = start
    close = 100.0
    while len(bars) < count:
        if current.weekday() < 5:
            bars.append(DailyBar(trade_date=current, close=round(close, 4)))
            close += step
        current += timedelta(days=1)
    return bars


def _labeled_journal(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    batch = build_catalyst_shadow_batch(
        [
            CatalystEntry(
                event_id="cal-1:entry-1",
                event_type="policy_meeting",
                scheduled_date=EVENT_DATE,
                date_confidence="hard_date",
                impact_direction="positive",
                source_ref="fixture",
                symbol=SYMBOL,
                event_cluster_id="cal-1:entry-1",
            )
        ],
        {SYMBOL: _bars(date(2026, 7, 15), 21)},
        as_of=AS_OF,
        pre_window_sessions=10,
        post_window_sessions=5,
    )
    append_shadow_batch_to_journal(journal, batch)
    return journal


class TestLoadFrozenPolicy:
    def test_loads_registered_policy(self, tmp_path):
        path, policy = _policy_file(tmp_path)
        loaded = load_frozen_policy(path)
        assert loaded.policy_sha256 == policy.policy_sha256
        assert loaded.min_labeled_observations == 60

    def test_threshold_drift_fails_closed(self, tmp_path):
        path, _ = _policy_file(tmp_path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["min_labeled_observations"] = 3  # tuned after registration
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(EventCatalystRunnerError) as excinfo:
            load_frozen_policy(path)
        assert excinfo.value.reason_code == "event_catalyst_runner_policy_drift"

    def test_unreadable_policy_fails_closed(self, tmp_path):
        with pytest.raises(EventCatalystRunnerError) as excinfo:
            load_frozen_policy(tmp_path / "missing.json")
        assert (
            excinfo.value.reason_code
            == "event_catalyst_runner_policy_unreadable"
        )

    def test_missing_field_fails_closed(self, tmp_path):
        path, _ = _policy_file(tmp_path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        del doc["min_time_windows"]
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(EventCatalystRunnerError) as excinfo:
            load_frozen_policy(path)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_runner_policy_field_missing"
        )


class TestLabelsFromJournalRecords:
    def test_maps_shadow_records_to_labels(self, tmp_path):
        journal = _labeled_journal(tmp_path)
        labels = labels_from_journal_records(journal.read_events())
        assert len(labels) == 1
        label = labels[0]
        assert label.hypothesis in SCORED_HYPOTHESES
        assert label.event_cluster == "cal-1:entry-1"
        assert label.window_id == "2026-08-14"
        assert isinstance(label.signed_post_return, float)

    def test_unscored_hypotheses_and_foreign_records_skipped(self):
        records = [
            {"record_type": "shadow_research",
             "bridge_contract": "other.bridge.v1",
             "positioning_hypothesis": "realize_on_event",
             "event_cluster_id": "x", "as_of": "2026-08-14T18:00:00+08:00",
             "post_return": 0.01},
            {"record_type": "shadow_research",
             "bridge_contract": "tradingagent.ashare.event_catalyst_journal.v1",
             "positioning_hypothesis": "no_signal",
             "event_cluster_id": "x", "as_of": "2026-08-14T18:00:00+08:00",
             "post_return": 0.01},
            {"record_type": "fill", "symbol": SYMBOL},
        ]
        assert labels_from_journal_records(records) == ()

    def test_malformed_scored_record_fails_closed(self):
        records = [
            {"record_type": "shadow_research",
             "bridge_contract": "tradingagent.ashare.event_catalyst_journal.v1",
             "positioning_hypothesis": "realize_on_event",
             "event_cluster_id": "",
             "as_of": "2026-08-14T18:00:00+08:00",
             "post_return": 0.01}
        ]
        with pytest.raises(EventCatalystRunnerError) as excinfo:
            labels_from_journal_records(records)
        assert (
            excinfo.value.reason_code == "event_catalyst_runner_label_invalid"
        )


class TestRunPromotionGate:
    def test_end_to_end_appends_decision(self, tmp_path):
        journal = _labeled_journal(tmp_path)
        policy_path, _ = _policy_file(tmp_path)
        before = len(journal.read_events())
        summary = run_promotion_gate(
            journal=journal, policy_path=policy_path, as_of=AS_OF
        )
        assert summary["contract"] == EVENT_CATALYST_RUNNER_CONTRACT
        assert summary["label_count"] == 1
        assert summary["decision_append_status"] == "appended"
        assert summary["execution_eligible"] is False
        assert summary["real_trading_enabled"] is False
        events = journal.read_events()
        assert len(events) == before + 1
        decision_event = events[-1]
        assert decision_event["record_type"] == "promotion_decision"
        assert decision_event["sample_layers"] == ["shadow_research"]
        assert decision_event["capital_layer"] == "simulated"
        assert "capital_authority_id" not in decision_event
        # With 1 label against 60 required, every hypothesis keeps shadow.
        assert all(
            verdict["decision"] == "keep_shadow"
            for verdict in decision_event["verdicts"]
        )

    def test_rerun_is_idempotent(self, tmp_path):
        journal = _labeled_journal(tmp_path)
        policy_path, _ = _policy_file(tmp_path)
        first = run_promotion_gate(
            journal=journal, policy_path=policy_path, as_of=AS_OF
        )
        second = run_promotion_gate(
            journal=journal, policy_path=policy_path, as_of=AS_OF
        )
        assert first["decision_receipt_sha256"] == (
            second["decision_receipt_sha256"]
        )
        assert second["decision_append_status"] == "idempotent"
        # One shadow fact + one decision fact.
        assert len(journal.read_events()) == 2

    def test_empty_journal_still_journals_keep_shadow_decision(self, tmp_path):
        journal = SampleJournal(tmp_path / "samples.jsonl")
        policy_path, _ = _policy_file(tmp_path)
        summary = run_promotion_gate(
            journal=journal, policy_path=policy_path, as_of=AS_OF
        )
        assert summary["label_count"] == 0
        assert summary["decision_append_status"] == "appended"
        assert all(
            verdict["decision"] == "keep_shadow"
            for verdict in summary["verdicts"]
        )

    def test_no_append_mode(self, tmp_path):
        journal = _labeled_journal(tmp_path)
        policy_path, _ = _policy_file(tmp_path)
        before = len(journal.read_events())
        summary = run_promotion_gate(
            journal=journal,
            policy_path=policy_path,
            as_of=AS_OF,
            append_decision=False,
        )
        assert summary["decision_append_status"] == "skipped"
        assert len(journal.read_events()) == before

    def test_graduation_when_policy_gates_are_met(self, tmp_path):
        """Enough winning realize_on_event labels across clusters/windows."""
        journal = SampleJournal(tmp_path / "samples.jsonl")
        # Two-phase price path: +0.6/session into the event (front-run),
        # then -0.8/session after it (news-fade) — the realize_on_event win.
        phases = _bars(date(2026, 7, 15), 16, step=0.6)
        peak = phases[-1].close
        tail = []
        current = date(2026, 8, 6)
        close = peak
        while len(tail) < 5:
            if current.weekday() < 5:
                tail.append(DailyBar(trade_date=current, close=round(close, 4)))
                close -= 0.8
            current += timedelta(days=1)
        bars = phases + tail
        as_of = AS_OF
        for cluster in range(10):
            for index in range(6):
                batch = build_catalyst_shadow_batch(
                    [
                        CatalystEntry(
                            event_id=f"cal-{cluster}:entry-{index}",
                            event_type="policy_meeting",
                            scheduled_date=EVENT_DATE,
                            date_confidence="hard_date",
                            impact_direction="positive",
                            source_ref="fixture",
                            symbol=SYMBOL,
                            event_cluster_id=f"cal-{cluster}",
                        )
                    ],
                    {SYMBOL: bars},
                    as_of=as_of,
                    pre_window_sessions=10,
                    post_window_sessions=5,
                )
                append_shadow_batch_to_journal(journal, batch)
            as_of += timedelta(days=7)
        policy_path, _ = _policy_file(tmp_path)
        summary = run_promotion_gate(
            journal=journal, policy_path=policy_path, as_of=as_of
        )
        verdicts = {
            verdict["hypothesis"]: verdict for verdict in summary["verdicts"]
        }
        # Front-run then fade: realize_on_event (expects decline) graduates.
        assert verdicts["realize_on_event"]["decision"] == (
            "graduate_to_validated_factor"
        )
        # hold_through_event never appeared in the evidence base.
        assert verdicts["hold_through_event"]["decision"] == "keep_shadow"
        assert verdicts["hold_through_event"]["labeled_observations"] == 0


class TestCli:
    def test_main_outputs_summary(self, tmp_path, capsys):
        journal = _labeled_journal(tmp_path)
        policy_path, _ = _policy_file(tmp_path)
        exit_code = main(
            [
                "--journal-path", str(journal.path),
                "--policy-path", str(policy_path),
                "--as-of", "2026-08-14T18:00:00+08:00",
            ]
        )
        assert exit_code == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["contract"] == EVENT_CATALYST_RUNNER_CONTRACT

    def test_main_fail_closed_exit_code(self, tmp_path, capsys):
        exit_code = main(
            [
                "--journal-path", str(tmp_path / "x.jsonl"),
                "--policy-path", str(tmp_path / "missing.json"),
            ]
        )
        assert exit_code == 2
        error = json.loads(capsys.readouterr().err)
        assert error["error"] == "event_catalyst_runner_policy_unreadable"
