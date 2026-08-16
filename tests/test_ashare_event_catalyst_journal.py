"""Contract tests for the catalyst-shadow → SampleJournal bridge."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from Ashare.event_catalyst_shadow import (
    CatalystEntry,
    DailyBar,
    build_catalyst_shadow_batch,
)
from Ashare.event_catalyst_journal import (
    EVENT_CATALYST_JOURNAL_CONTRACT,
    EventCatalystJournalError,
    _event_cluster_id,
    append_shadow_batch_to_journal,
    journal_records_from_shadow_batch,
)
from shared.review.sample_journal import (
    JournalConflictError,
    SampleJournal,
)
from shared.review.sample_kpi import SAMPLE_LAYERS, classify_sample_layers


AS_OF = datetime.fromisoformat("2026-08-14T18:00:00+08:00")
SYMBOL = "600519.SH"
EVENT_DATE = date(2026, 8, 5)


def _bars(start: date, count: int, *, start_close: float = 100.0, step: float = 0.0):
    bars = []
    current = start
    close = start_close
    while len(bars) < count:
        if current.weekday() < 5:
            bars.append(DailyBar(trade_date=current, close=round(close, 4)))
            close += step
        current += timedelta(days=1)
    return bars


def _entry(**overrides) -> CatalystEntry:
    payload = {
        "event_id": "calendar-2026h2:entry-7",
        "event_type": "policy_meeting",
        "scheduled_date": EVENT_DATE,
        "date_confidence": "hard_date",
        "impact_direction": "positive",
        "source_ref": "fixture-calendar-v1",
        "symbol": SYMBOL,
    }
    payload.update(overrides)
    return CatalystEntry(**payload)


def _labeled_batch(**overrides):
    kwargs = {"as_of": AS_OF, "pre_window_sessions": 10, "post_window_sessions": 5}
    kwargs.update(overrides)
    return build_catalyst_shadow_batch(
        [_entry()],
        {SYMBOL: _bars(date(2026, 7, 15), 21, step=0.5)},
        **kwargs,
    )


def _pending_batch():
    # as_of one session after the event: the 5-session post window is not
    # yet observable, so the label must stay pending.
    return build_catalyst_shadow_batch(
        [_entry()],
        {SYMBOL: _bars(date(2026, 7, 15), 17, step=0.5)},
        as_of=datetime.fromisoformat("2026-08-06T18:00:00+08:00"),
        pre_window_sessions=10,
        post_window_sessions=5,
    )


class TestEventClusterId:
    def test_calendar_style_id_unchanged(self):
        assert (
            _event_cluster_id("calendar-2026h2:entry-7", SYMBOL)
            == "calendar-2026h2:entry-7"
        )

    def test_event_symbol_suffix_stripped(self):
        assert _event_cluster_id("evt-9:600519.SH", SYMBOL) == "evt-9"

    def test_suffix_only_stripped_for_matching_symbol(self):
        assert (
            _event_cluster_id("evt-9:600519.SH", "000001.SZ")
            == "evt-9:600519.SH"
        )

    def test_no_symbol_keeps_id(self):
        assert _event_cluster_id("evt-9:600519.SH", None) == "evt-9:600519.SH"


class TestJournalRecordsFromShadowBatch:
    def test_labeled_observation_becomes_shadow_research_record(self):
        records = journal_records_from_shadow_batch(_labeled_batch())
        assert len(records) == 1
        record = records[0]
        assert record["record_type"] == "shadow_research"
        assert record["sample_layers"] == ["shadow_research"]
        assert record["research_contract"].startswith(
            "tradingagent.ashare.event_catalyst_shadow"
        )
        assert record["bridge_contract"] == EVENT_CATALYST_JOURNAL_CONTRACT
        assert record["journal_event_id"].startswith("catalyst:")
        assert record["event_cluster_id"] == "calendar-2026h2:entry-7"
        assert record["scheduled_date"] == EVENT_DATE.isoformat()
        assert isinstance(record["pre_return"], float)
        assert isinstance(record["post_return"], float)

    def test_event_symbol_id_normalizes_cluster(self):
        batch = build_catalyst_shadow_batch(
            [_entry(event_id="evt-9:%s" % SYMBOL)],
            {SYMBOL: _bars(date(2026, 7, 15), 21, step=0.5)},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (record,) = journal_records_from_shadow_batch(batch)
        assert record["event_id"] == "evt-9:%s" % SYMBOL
        assert record["event_cluster_id"] == "evt-9"

    def test_pending_observations_are_never_journaled(self):
        batch = _pending_batch()
        assert batch.observations[0].post_label_state == "pending"
        assert journal_records_from_shadow_batch(batch) == ()

    def test_insufficient_history_observations_are_skipped(self):
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: _bars(date(2026, 8, 3), 8, step=0.5)},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        assert batch.observations[0].observation_status == "insufficient_history"
        assert journal_records_from_shadow_batch(batch) == ()

    def test_records_carry_no_capital_authority_or_execution_fields(self):
        (record,) = journal_records_from_shadow_batch(_labeled_batch())
        for forbidden in (
            "capital_authority_id",
            "authority_generation",
            "execution_lineage_id",
            "execution_eligible",
            "candidate_eligible",
            "order_authority",
            "fill_identity",
            "prediction_snapshot_id",
        ):
            assert forbidden not in record

    def test_rejects_non_batch_input(self):
        with pytest.raises(EventCatalystJournalError) as excinfo:
            journal_records_from_shadow_batch({"observations": []})
        assert excinfo.value.reason_code == "event_catalyst_journal_batch_invalid"

    def test_layer_classification_recognizes_shadow_research(self):
        (record,) = journal_records_from_shadow_batch(_labeled_batch())
        assert "shadow_research" in SAMPLE_LAYERS
        assert classify_sample_layers(record) == ("shadow_research",)


class TestAppendShadowBatchToJournal:
    def test_appends_to_real_sample_journal(self, tmp_path):
        journal = SampleJournal(tmp_path / "samples.jsonl")
        batch = _labeled_batch()
        results = append_shadow_batch_to_journal(journal, batch)
        assert len(results) == 1
        assert results[0]["status"] == "appended"
        events = journal.read_events()
        assert len(events) == 1
        event = events[0]
        assert event["journal_event_type"] == "sample_event"
        assert event["record_type"] == "shadow_research"
        assert event["sample_layers"] == ["shadow_research"]
        assert event["capital_layer"] == "simulated"
        assert event["real_trading_enabled"] is False
        assert event["journal_event_id"].startswith("sample:catalyst:")

    def test_reappend_is_idempotent(self, tmp_path):
        journal = SampleJournal(tmp_path / "samples.jsonl")
        batch = _labeled_batch()
        first = append_shadow_batch_to_journal(journal, batch)
        second = append_shadow_batch_to_journal(journal, batch)
        assert first[0]["status"] == "appended"
        assert second[0]["status"] == "idempotent"
        assert len(journal.read_events()) == 1

    def test_content_drift_raises_conflict(self, tmp_path):
        journal = SampleJournal(tmp_path / "samples.jsonl")
        append_shadow_batch_to_journal(journal, _labeled_batch())
        # Reusing the same observation-derived journal_event_id with changed
        # content must conflict rather than duplicate or silently overwrite.
        record = journal_records_from_shadow_batch(_labeled_batch())[0]
        forged = dict(record)
        forged["post_return"] = 9.99
        with pytest.raises(JournalConflictError):
            journal.append_samples([forged])
        assert len(journal.read_events()) == 1

    def test_empty_batch_appends_nothing(self, tmp_path):
        journal = SampleJournal(tmp_path / "samples.jsonl")
        assert append_shadow_batch_to_journal(journal, _pending_batch()) == []
        assert journal.read_events() == []

    def test_rejects_journal_without_append_samples(self):
        with pytest.raises(EventCatalystJournalError) as excinfo:
            append_shadow_batch_to_journal(object(), _labeled_batch())
        assert (
            excinfo.value.reason_code == "event_catalyst_journal_journal_invalid"
        )


class TestExplicitClusterPropagation:
    def test_explicit_cluster_id_wins_over_heuristic(self):
        batch = build_catalyst_shadow_batch(
            [_entry(event_id="legacy-1:%s" % SYMBOL,
                    event_cluster_id="cal-2026h2:entry-3")],
            {SYMBOL: _bars(date(2026, 7, 15), 21, step=0.5)},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (record,) = journal_records_from_shadow_batch(batch)
        assert record["event_cluster_id"] == "cal-2026h2:entry-3"
        assert record["style"] == "event_catalyst_shadow"


class TestIntradayLabelsInJournalRecords:
    def test_rally_labels_flow_into_journal_record(self):
        bars = [
            DailyBar(
                trade_date=bar.trade_date,
                close=bar.close,
                high=round(bar.close + 0.3, 4),
            )
            for bar in _bars(date(2026, 7, 15), 21, step=0.5)
        ]
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: bars},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (record,) = journal_records_from_shadow_batch(batch)
        assert record["post_optimal_exit_offset"] == 5
        assert record["post_max_intraday_premium"] == pytest.approx(
            (100.0 + 0.5 * 20 + 0.3) / (100.0 + 0.5 * 15) - 1.0
        )
        assert record["post_optimal_exit_return"] is not None

    def test_close_only_batch_journals_null_intraday_fields(self):
        (record,) = journal_records_from_shadow_batch(_labeled_batch())
        assert record["post_max_intraday_premium"] is None
        assert record["post_optimal_exit_offset"] is None
        assert record["post_optimal_exit_return"] is None
