"""Contract tests for the lockup oversold-rebound signal tracker."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from Ashare.event_catalyst_shadow import (
    CatalystEntry,
    DailyBar,
    build_catalyst_shadow_batch,
)
from Ashare.event_signal_lockup_tracker import (
    DEFAULT_SINCE,
    LEDGER_FILENAME,
    SIGNAL_ANTICIPATION_CLASS,
    SIGNAL_EVENT_TYPE,
    TrackerError,
    append_new_outcomes,
    build_tracker_view,
    load_lockup_entries,
    render_report,
    run_tracker,
    signal_journal_records,
)
from shared.review.sample_journal import SampleJournal


AS_OF = datetime.fromisoformat("2026-08-20T10:00:00+08:00")
SYMBOL = "600001.SH"
EVENT_DATE = date(2026, 8, 5)

# 21 weekday bars from 2026-07-15: flat, a -4.8% slide into the event day
# (sell_off class), then a +2% recovery across the five post sessions.
SELL_OFF_CLOSES = (
    [105.0] * 5
    + [104.0, 103.0, 102.0, 101.0, 100.0]
    + [100.0] * 5
    + [100.0]
    + [100.4, 100.8, 101.2, 101.6, 102.0]
)


def _weekday_bars(start: date, closes: list[float]) -> list[DailyBar]:
    bars: list[DailyBar] = []
    current = start
    for close in closes:
        while current.weekday() >= 5:
            current += timedelta(days=1)
        bars.append(DailyBar(trade_date=current, close=close))
        current += timedelta(days=1)
    return bars


def _entry(symbol: str = SYMBOL, event_date: date = EVENT_DATE) -> CatalystEntry:
    return CatalystEntry(
        event_id=f"lock:{symbol}:{event_date.isoformat()}",
        event_type=SIGNAL_EVENT_TYPE,
        scheduled_date=event_date,
        date_confidence="hard_date",
        impact_direction="negative",
        source_ref="fixture-share-float",
        symbol=symbol,
    )


def _batch(symbols_closes: dict[str, list[float]], as_of: datetime = AS_OF):
    entries = [_entry(symbol=symbol) for symbol in symbols_closes]
    start = date(2026, 7, 15)
    bars_by_symbol = {
        symbol: _weekday_bars(start, closes)
        for symbol, closes in symbols_closes.items()
    }
    return build_catalyst_shadow_batch(
        entries,
        bars_by_symbol,
        as_of=as_of,
        pre_window_sessions=10,
        post_window_sessions=5,
    )


# --- signal classification -------------------------------------------------


class TestTrackerView:
    def test_sell_off_labeled_outcome_is_a_signal(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}))
        assert len(view.labeled_signals) == 1
        obs = view.labeled_signals[0]
        assert obs.symbol == SYMBOL
        assert obs.anticipation_class == SIGNAL_ANTICIPATION_CLASS
        assert obs.post_label_state == "labeled"
        assert view.active_signals == ()

    def test_open_post_window_is_an_active_signal(self):
        # Truncate after the event day + 3 sessions: post window still open.
        truncated = SELL_OFF_CLOSES[:19]
        view = build_tracker_view(_batch({SYMBOL: truncated}))
        assert len(view.active_signals) == 1
        assert view.labeled_signals == ()
        assert view.active_signals[0].post_label_state == "pending"

    def test_non_sell_off_lockups_are_not_signals(self):
        rising = [100.0 + 0.6 * i for i in range(21)]  # front_run territory
        view = build_tracker_view(_batch({SYMBOL: rising}))
        assert view.labeled_signals == ()
        assert view.active_signals == ()
        assert view.other_labeled >= 1

    def test_future_event_is_not_observed(self):
        future_entry = _entry(event_date=date(2026, 12, 1))
        batch = build_catalyst_shadow_batch(
            [future_entry],
            {SYMBOL: _weekday_bars(date(2026, 7, 15), SELL_OFF_CLOSES)},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        view = build_tracker_view(batch)
        assert view.status_counts.get("insufficient_history", 0) == 1
        assert view.active_signals == ()
        assert view.labeled_signals == ()


# --- journal write path -----------------------------------------------------


class TestJournalWritePath:
    def _records(self) -> tuple[dict, ...]:
        return signal_journal_records(_batch({SYMBOL: SELL_OFF_CLOSES}))

    def test_records_restricted_to_signal_observations(self):
        records = self._records()
        assert len(records) == 1
        record = records[0]
        assert record["record_type"] == "shadow_research"
        assert record["event_type"] == SIGNAL_EVENT_TYPE
        assert record["anticipation_class"] == "sell_off"
        assert record["sample_layers"] == ["shadow_research"]

    def test_append_then_rerun_freezes_on_ledger(self, tmp_path):
        journal = SampleJournal(tmp_path / "journal.jsonl")
        ledger = tmp_path / LEDGER_FILENAME
        records = self._records()
        first = append_new_outcomes(journal, ledger, records)
        assert len(first) == 1
        second = append_new_outcomes(journal, ledger, records)
        assert second == []

    def test_journal_read_back_survives_lost_ledger(self, tmp_path):
        journal = SampleJournal(tmp_path / "journal.jsonl")
        ledger = tmp_path / LEDGER_FILENAME
        records = self._records()
        append_new_outcomes(journal, ledger, records)
        ledger.unlink()  # simulate a lost/truncated ledger file
        second = append_new_outcomes(journal, ledger, records)
        assert second == []

    def test_non_signal_events_never_reach_the_journal(self, tmp_path):
        rising = [100.0 + 0.6 * i for i in range(21)]
        records = signal_journal_records(_batch({SYMBOL: rising}))
        assert records == ()

    def test_empty_records_write_nothing(self, tmp_path):
        journal = SampleJournal(tmp_path / "journal.jsonl")
        ledger = tmp_path / LEDGER_FILENAME
        assert append_new_outcomes(journal, ledger, ()) == []
        assert not ledger.exists()


# --- end-to-end pass over a mini cache --------------------------------------


def _write_csv(path, header, rows):
    lines = [",".join(header)]
    lines.extend(",".join(str(cell) for cell in row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def mini_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    stem = SYMBOL.replace(".", "")
    _write_csv(cache / "sample_symbols.csv", ["ts_code"], [[SYMBOL]])

    ann = "20260701"
    float_day = EVENT_DATE.strftime("%Y%m%d")
    _write_csv(
        cache / "share_float.csv",
        ["ts_code", "ann_date", "float_date", "float_share", "float_ratio", "holder_name", "share_type"],
        [
            [SYMBOL, ann, float_day, "1200000", "1.5", "Holder A", "IPO"],
            [SYMBOL, ann, "20261201", "", "1.0", "Holder B", "IPO"],  # invalid row
        ],
    )

    start = date(2026, 7, 15)
    days: list[str] = []
    current = start
    for _ in range(30):
        while current.weekday() >= 5:
            current += timedelta(days=1)
        days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    close_by_day = dict(zip(days, SELL_OFF_CLOSES))
    _write_csv(
        cache / f"daily_{stem}.csv",
        ["ts_code", "trade_date", "close"],
        [[SYMBOL, d, f"{close:.4f}"] for d, close in close_by_day.items()],
    )
    _write_csv(
        cache / f"adjfactor_{stem}.csv",
        ["ts_code", "trade_date", "adj_factor"],
        [[SYMBOL, d, "1.0"] for d in days],
    )
    return cache


class TestRunTracker:
    def test_end_to_end_pass_labels_and_journals(self, mini_cache, tmp_path):
        journal_path = tmp_path / "journal.jsonl"
        view = run_tracker(
            mini_cache,
            journal_path,
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
        )
        # The valid August expiry is labelled; the December row fails closed.
        assert len(view.labeled_signals) == 1
        assert view.appended_records and len(view.appended_records) == 1
        rows = SampleJournal(journal_path).read_events()
        assert [r["record_type"] for r in rows] == ["shadow_research"]

        # A second identical pass must not duplicate the journaled outcome.
        second = run_tracker(
            mini_cache,
            journal_path,
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
        )
        assert second.appended_records == []
        assert len(SampleJournal(journal_path).read_events()) == 1

    def test_dry_run_writes_nothing(self, mini_cache, tmp_path):
        journal_path = tmp_path / "journal.jsonl"
        run_tracker(
            mini_cache,
            journal_path,
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
            dry_run=True,
        )
        assert not journal_path.exists()
        assert not (tmp_path / LEDGER_FILENAME).exists()

    def test_missing_cache_fails_closed(self, tmp_path):
        with pytest.raises(TrackerError, match="cache_missing"):
            run_tracker(
                tmp_path / "nope",
                tmp_path / "journal.jsonl",
                since=date(2026, 6, 1),
                as_of=AS_OF,
            )

    def test_adapter_invalid_rows_are_counted_and_skipped(self, mini_cache):
        samples = {SYMBOL}
        entries, skipped = load_lockup_entries(
            mini_cache, samples, since=date(2026, 1, 1)
        )
        assert skipped == 1
        assert len(entries) == 1
        assert entries[0].event_type == SIGNAL_EVENT_TYPE


# --- report rendering --------------------------------------------------------


class TestRenderReport:
    def test_report_contains_signal_sections(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}))
        report = render_report(view, since=date(2026, 6, 1), as_of=AS_OF, dry_run=False)
        assert "research_only" in report
        assert "Currently tracked signals" in report
        assert "Labelled outcomes" in report
        assert "win_rate" in report or "n=" in report

    def test_active_signal_row_lists_symbol_and_pre_return(self):
        truncated = SELL_OFF_CLOSES[:19]
        view = build_tracker_view(_batch({SYMBOL: truncated}))
        report = render_report(view, since=date(2026, 6, 1), as_of=AS_OF, dry_run=True)
        assert SYMBOL in report
        assert "-4.76%" in report
