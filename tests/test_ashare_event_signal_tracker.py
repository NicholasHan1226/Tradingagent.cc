"""Contract tests for the multi-signal event tracker (lockup + earnings).

Covers the three tracked signals — ``lockup`` (sell_off into expiry),
``earnings_pos`` (prior positive forecast) and ``earnings_neg`` (prior
negative forecast) — plus the journal write path with its ledger/journal
read-back dedup guard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from Ashare.event_catalyst_shadow import (
    CatalystEntry,
    DailyBar,
    build_catalyst_shadow_batch,
)
from Ashare.event_signal_lockup_tracker import (
    DEFAULT_SINCE,
    EARNINGS_DISCLOSURE_EVENT_TYPE,
    EARNINGS_NEG_SIGNAL,
    EARNINGS_POS_SIGNAL,
    LEDGER_FILENAME,
    LOCKUP_SIGNAL,
    SIGNAL_ANTICIPATION_CLASS,
    SIGNAL_EVENT_TYPE,
    TrackerError,
    append_new_outcomes,
    build_tracker_view,
    load_disclosure_entries,
    load_lockup_entries,
    parse_signals,
    render_report,
    run_tracker,
    signal_journal_records,
)
from shared.review.sample_journal import SampleJournal


AS_OF = datetime.fromisoformat("2026-08-20T10:00:00+08:00")
SYMBOL = "600001.SH"
EVENT_DATE = date(2026, 8, 5)

# 26 weekday bars from 2026-07-15: flat, a -4.8% slide into the August 5
# event day (sell_off class), then a +2% recovery across the five post
# sessions and a flat tail so the August 10 event can label too.
SELL_OFF_CLOSES = (
    [105.0] * 5
    + [104.0, 103.0, 102.0, 101.0, 100.0]
    + [100.0] * 5
    + [100.0]
    + [100.4, 100.8, 101.2, 101.6, 102.0]
    + [102.0, 102.0, 102.0, 102.0, 102.0]
)

ALL_SIGNALS = (LOCKUP_SIGNAL, EARNINGS_POS_SIGNAL, EARNINGS_NEG_SIGNAL)


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


def _disc_entry(
    symbol: str = SYMBOL,
    event_date: date = EVENT_DATE,
    impact_direction: str = "positive",
) -> CatalystEntry:
    return CatalystEntry(
        event_id=f"disc:{symbol}:20260630:{event_date.isoformat()}",
        event_type=EARNINGS_DISCLOSURE_EVENT_TYPE,
        scheduled_date=event_date,
        date_confidence="hard_date",
        impact_direction=impact_direction,
        source_ref="fixture-disclosure-date",
        symbol=symbol,
    )


def _batch(symbols_closes: dict[str, list[float]], as_of: datetime = AS_OF):
    """Batch with one lockup entry plus a positive and a negative disclosure
    appointment per symbol, over identical synthetic price history."""

    entries: list[CatalystEntry] = []
    for symbol in symbols_closes:
        entries.append(_entry(symbol=symbol))
        entries.append(_disc_entry(symbol=symbol, impact_direction="positive"))
        entries.append(
            _disc_entry(
                symbol=symbol,
                event_date=date(2026, 8, 10),
                impact_direction="negative",
            )
        )
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


# --- signal predicates -------------------------------------------------------


class TestTrackerView:
    def test_sell_off_labeled_outcome_is_a_lockup_signal(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), (LOCKUP_SIGNAL,))
        assert len(view.buckets[LOCKUP_SIGNAL].labeled) == 1
        obs = view.buckets[LOCKUP_SIGNAL].labeled[0]
        assert obs.symbol == SYMBOL
        assert obs.anticipation_class == SIGNAL_ANTICIPATION_CLASS
        assert obs.post_label_state == "labeled"
        assert view.buckets[LOCKUP_SIGNAL].active == ()

    def test_open_post_window_is_an_active_signal(self):
        # Truncate after the event day + 3 sessions: post window still open.
        truncated = SELL_OFF_CLOSES[:19]
        view = build_tracker_view(_batch({SYMBOL: truncated}), (LOCKUP_SIGNAL,))
        assert len(view.buckets[LOCKUP_SIGNAL].active) == 1
        assert view.buckets[LOCKUP_SIGNAL].labeled == ()
        assert view.buckets[LOCKUP_SIGNAL].active[0].post_label_state == "pending"

    def test_non_sell_off_lockups_are_not_signals(self):
        rising = [100.0 + 0.6 * i for i in range(26)]  # front_run territory
        view = build_tracker_view(_batch({SYMBOL: rising}), (LOCKUP_SIGNAL,))
        assert view.buckets[LOCKUP_SIGNAL].labeled == ()
        assert view.buckets[LOCKUP_SIGNAL].active == ()
        assert view.unattributed_labeled >= 1

    def test_future_event_is_not_observed(self):
        future_entry = _entry(event_date=date(2026, 12, 1))
        batch = build_catalyst_shadow_batch(
            [future_entry],
            {SYMBOL: _weekday_bars(date(2026, 7, 15), SELL_OFF_CLOSES)},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        view = build_tracker_view(batch, (LOCKUP_SIGNAL,))
        assert view.status_counts.get("insufficient_history", 0) == 1
        assert view.buckets[LOCKUP_SIGNAL].active == ()
        assert view.buckets[LOCKUP_SIGNAL].labeled == ()

    def test_earnings_directions_land_in_their_own_buckets(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        assert len(view.buckets[EARNINGS_POS_SIGNAL].labeled) == 1
        assert len(view.buckets[EARNINGS_NEG_SIGNAL].labeled) == 1
        assert len(view.buckets[LOCKUP_SIGNAL].labeled) == 1
        pos_obs = view.buckets[EARNINGS_POS_SIGNAL].labeled[0]
        assert pos_obs.event_type == EARNINGS_DISCLOSURE_EVENT_TYPE
        assert pos_obs.impact_direction == "positive"
        # The negative appointment sits on 2026-08-10.
        assert view.buckets[EARNINGS_NEG_SIGNAL].labeled[
            0
        ].scheduled_date == date(2026, 8, 10)

    def test_unclear_disclosures_are_never_tracked(self):
        entries = [
            _entry(),
            _disc_entry(impact_direction="unclear"),
        ]
        batch = build_catalyst_shadow_batch(
            entries,
            {SYMBOL: _weekday_bars(date(2026, 7, 15), SELL_OFF_CLOSES)},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        view = build_tracker_view(batch, ALL_SIGNALS)
        assert view.buckets[EARNINGS_POS_SIGNAL].labeled == ()
        assert view.buckets[EARNINGS_NEG_SIGNAL].labeled == ()
        # Labelled but matching no requested signal -> counted, not journaled.
        assert view.unattributed_labeled == 1

    def test_subset_request_leaves_other_signals_unattributed(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), (LOCKUP_SIGNAL,))
        assert set(view.buckets) == {LOCKUP_SIGNAL}
        assert len(view.buckets[LOCKUP_SIGNAL].labeled) == 1
        # Both labelled disclosures match no requested signal here.
        assert view.unattributed_labeled >= 2


class TestParseSignals:
    def test_valid_list_parses_with_whitespace(self):
        assert parse_signals(" lockup, earnings_neg ") == (
            LOCKUP_SIGNAL,
            EARNINGS_NEG_SIGNAL,
        )

    def test_unknown_signal_fails_closed(self):
        with pytest.raises(TrackerError, match="signal_unknown"):
            parse_signals("lockup,momentum")

    def test_empty_list_fails_closed(self):
        with pytest.raises(TrackerError, match="signals_empty"):
            parse_signals(",,")


# --- journal write path -----------------------------------------------------


class TestJournalWritePath:
    def _records(self) -> tuple[dict, ...]:
        return signal_journal_records(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)

    def test_records_restricted_to_signal_observations(self):
        records = self._records()
        assert len(records) == 3  # lockup sell_off + positive + negative
        assert {r["record_type"] for r in records} == {"shadow_research"}
        assert {
            r["journal_event_id"] for r in records
        } == {
            f"catalyst:{obs.observation_sha256[:32]}"
            for obs in _batch({SYMBOL: SELL_OFF_CLOSES}).observations
            if obs.post_label_state == "labeled"
        }

    def test_subset_filter_drops_other_signals_from_journal(self):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        records = signal_journal_records(batch, (LOCKUP_SIGNAL,))
        assert len(records) == 1
        assert records[0]["event_type"] == SIGNAL_EVENT_TYPE

    def test_append_then_rerun_freezes_on_ledger(self, tmp_path):
        journal = SampleJournal(tmp_path / "journal.jsonl")
        ledger = tmp_path / LEDGER_FILENAME
        records = self._records()
        first = append_new_outcomes(journal, ledger, records)
        assert len(first) == 3
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
        rising = [100.0 + 0.6 * i for i in range(26)]
        records = signal_journal_records(
            _batch({SYMBOL: rising}), (LOCKUP_SIGNAL,)
        )
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

    # Disclosure appointments: positive (Aug 5) and negative (Aug 10) both
    # carry a usable prior forecast; the no-forecast and uncertain rows must
    # be skipped by the tracker.
    _write_csv(
        cache / "disclosure.csv",
        ["ts_code", "ann_date", "end_date", "pre_date"],
        [
            [SYMBOL, "20260701", "20260630", "20260805"],
            [SYMBOL, "20260705", "20260930", "20260810"],
            [SYMBOL, "20260706", "20251231", "20260813"],  # no prior forecast
            [SYMBOL, "20260707", "20260331", "20260814"],  # uncertain forecast
        ],
    )
    _write_csv(
        cache / "forecast.csv",
        ["ts_code", "ann_date", "end_date", "type", "update_flag"],
        [
            [SYMBOL, "20260615", "20260630", "预增", "1"],
            [SYMBOL, "20260720", "20260930", "预减", "1"],
            [SYMBOL, "20260721", "20260331", "不确定", "1"],
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
        [[SYMBOL, d, "1.0"] for d in close_by_day],
    )
    return cache


class TestRunTracker:
    def test_end_to_pass_tracks_all_three_signals(self, mini_cache, tmp_path):
        journal_path = tmp_path / "journal.jsonl"
        view = run_tracker(
            mini_cache,
            journal_path,
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
            signals=ALL_SIGNALS,
        )
        assert len(view.buckets[LOCKUP_SIGNAL].labeled) == 1
        assert len(view.buckets[EARNINGS_POS_SIGNAL].labeled) == 1
        assert len(view.buckets[EARNINGS_NEG_SIGNAL].labeled) == 1
        assert view.appended_records and len(view.appended_records) == 3
        rows = SampleJournal(journal_path).read_events()
        assert [r["record_type"] for r in rows] == ["shadow_research"] * 3

        # A second identical pass must not duplicate any journaled outcome.
        second = run_tracker(
            mini_cache,
            journal_path,
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
            signals=ALL_SIGNALS,
        )
        assert second.appended_records == []
        assert len(SampleJournal(journal_path).read_events()) == 3

    def test_default_run_stays_lockup_only(self, mini_cache, tmp_path):
        journal_path = tmp_path / "journal.jsonl"
        view = run_tracker(
            mini_cache,
            journal_path,
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
        )
        assert view.signals == (LOCKUP_SIGNAL,)
        assert len(view.buckets[LOCKUP_SIGNAL].labeled) == 1
        assert view.appended_records and len(view.appended_records) == 1
        rows = SampleJournal(journal_path).read_events()
        assert len(rows) == 1

    def test_lockup_only_skips_invalid_rows_and_counts_skips(self, mini_cache):
        samples = {SYMBOL}
        entries, skipped = load_lockup_entries(
            mini_cache, samples, since=date(2026, 1, 1)
        )
        assert skipped == 1  # blank float_share row fails closed
        assert len(entries) == 1
        assert entries[0].event_type == SIGNAL_EVENT_TYPE

    def test_disclosure_loader_skips_forecastless_and_uncertain_rows(
        self, mini_cache
    ):
        samples = {SYMBOL}
        entries, skipped = load_disclosure_entries(
            mini_cache, samples, since=date(2026, 1, 1)
        )
        assert skipped == 2  # no-forecast row + uncertain row
        assert len(entries) == 2
        impacts = sorted(entry.impact_direction for entry in entries)
        assert impacts == ["negative", "positive"]
        types = {entry.event_type for entry in entries}
        assert types == {EARNINGS_DISCLOSURE_EVENT_TYPE}

    def test_dry_run_writes_nothing(self, mini_cache, tmp_path):
        journal_path = tmp_path / "journal.jsonl"
        run_tracker(
            mini_cache,
            journal_path,
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
            signals=ALL_SIGNALS,
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
                signals=ALL_SIGNALS,
            )


# --- report rendering --------------------------------------------------------


class TestRenderReport:
    def test_report_contains_signal_sections(self):
        view = build_tracker_view(
            _batch({SYMBOL: SELL_OFF_CLOSES}), (LOCKUP_SIGNAL,)
        )
        report = render_report(view, since=date(2026, 6, 1), as_of=AS_OF, dry_run=False)
        assert "research_only" in report
        assert "Lockup oversold-rebound" in report
        assert "Labelled outcomes this pass" in report
        assert "win_rate" in report or "n=" in report

    def test_report_covers_all_requested_signals(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        report = render_report(view, since=date(2026, 6, 1), as_of=AS_OF, dry_run=True)
        assert "Disclosure positive drift" in report
        assert "Disclosure negative relief" in report
        # The control semantics must be stated so nobody reads the labelled
        # earnings_pos post window as the trade itself.
        assert "hold-past-disclosure control" in report
        assert "dry run" in report

    def test_active_signal_row_lists_symbol_and_pre_return(self):
        truncated = SELL_OFF_CLOSES[:19]
        view = build_tracker_view(
            _batch({SYMBOL: truncated}), (LOCKUP_SIGNAL,)
        )
        report = render_report(view, since=date(2026, 6, 1), as_of=AS_OF, dry_run=True)
        assert SYMBOL in report
        assert "-4.76%" in report
