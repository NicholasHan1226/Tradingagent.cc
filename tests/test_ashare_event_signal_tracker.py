"""Contract tests for the multi-signal event tracker (lockup + earnings).

Covers the three tracked signals — ``lockup`` (sell_off into expiry),
``earnings_pos`` (prior positive forecast) and ``earnings_neg`` (prior
negative forecast) — plus the journal write path with its ledger/journal
read-back dedup guard.
"""

from __future__ import annotations

import csv
import time
from datetime import date, datetime, timedelta

import pytest

from Ashare.event_catalyst_shadow import (
    CatalystEntry,
    DailyBar,
    build_catalyst_shadow_batch,
)
from Ashare.event_signal_lockup_tracker import (
    ABSORPTION_LABEL_ORDER,
    BLOCK_LABEL_ORDER,
    DEFAULT_SINCE,
    EARNINGS_DISCLOSURE_EVENT_TYPE,
    EARNINGS_NEG_SIGNAL,
    EARNINGS_POS_SIGNAL,
    LEDGER_FILENAME,
    LOCKUP_SIGNAL,
    SIGNAL_ANTICIPATION_CLASS,
    SIGNAL_EVENT_TYPE,
    TrackerError,
    CHIPS_LABEL_ORDER,
    TOPLIST_LABEL_ORDER,
    HOLDERNUM_LABEL_ORDER,
    MACRO_LABEL_ORDER,
    VALUATION_LABEL_ORDER,
    HOLDERTYPE_LABEL_ORDER,
    TURNOVER_LABEL_ORDER,
    HOLDER_LABEL_ORDER,
    PLEDGE_LABEL_ORDER,
    REPURCHASE_LABEL_ORDER,
    _absorption_labels_for_observations,
    _block_labels_for_observations,
    _chips_labels_for_observations,
    _holder_labels_for_observations,
    _pledge_labels_for_observations,
    _toplist_labels_for_observations,
    _holdernum_labels_for_observations,
    _macro_labels_for_observations,
    _valuation_labels_for_observations,
    _holdertype_labels_for_observations,
    _repurchase_labels_for_observations,
    _turnover_labels_for_observations,
    absorption_breakdown,
    block_breakdown,
    chips_breakdown,
    holders_breakdown,
    pledges_breakdown,
    toplists_breakdown,
    holdernums_breakdown,
    macro_breakdown,
    valuations_breakdown,
    holdertypes_breakdown,
    repurchases_breakdown,
    turnover_breakdown,
    append_new_outcomes,
    build_tracker_view,
    load_disclosure_entries,
    load_lockup_entries,
    make_regime_lookup,
    parse_signals,
    prewindow_return,
    ratio_breakdown,
    regime_breakdown,
    render_report,
    rule_subset_breakdown,
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


class TestRegimeTags:
    @staticmethod
    def _pairs(closes):
        base = date(2026, 1, 1)
        return [(base + timedelta(days=i), close) for i, close in enumerate(closes)]

    def test_regime_boundaries(self):
        lookup = make_regime_lookup(self._pairs([100.0] * 15))
        assert lookup(date(2026, 1, 15)) == "sideways"
        weak = make_regime_lookup(
            self._pairs([100.0] * 10 + [100.0] * 4 + [97.0])
        )
        assert weak(date(2026, 1, 15)) == "weak"
        strong = make_regime_lookup(
            self._pairs([100.0] * 10 + [100.0] * 4 + [103.0])
        )
        assert strong(date(2026, 1, 15)) == "strong"

    def test_regime_unknown_before_history_or_far_future(self):
        lookup = make_regime_lookup(self._pairs([100.0] * 15))
        # Fewer than 10 sessions of history before the day.
        assert lookup(date(2026, 1, 5)) == "unknown"
        # A day beyond the series resolves to the LAST known session...
        late = make_regime_lookup(
            self._pairs([100.0] * 10 + [100.0] * 4 + [97.0])
        )
        assert late(date(2026, 2, 20)) == "weak"

    def test_regime_falls_back_to_last_session_before_non_trading_day(self):
        # Sessions on Jan 1..15 with a gap: an appointment landing on a
        # missing (weekend/holiday) day must use the last completed session
        # instead of degrading to unknown.
        closes = [100.0] * 10 + [100.0] * 4 + [97.0]
        pairs = [
            (date(2026, 1, 1) + timedelta(days=i), close)
            for i, close in enumerate(closes)
            if i not in (13,)  # one missing calendar day mid-series
        ]
        lookup = make_regime_lookup(pairs)
        missing_day = date(2026, 1, 14)
        assert all(day != missing_day for day, _ in pairs)
        assert lookup(missing_day) == lookup(date(2026, 1, 13))

    def test_regime_breakdown_groups_labelled_outcomes(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.regime_by_date = {
            obs.scheduled_date.isoformat(): "weak"
            for obs in _batch({SYMBOL: SELL_OFF_CLOSES}).observations
        }
        lockup = view.buckets[LOCKUP_SIGNAL]
        breakdown = regime_breakdown(lockup.labeled, view.regime_by_date)
        assert set(breakdown) == {"weak"}
        assert breakdown["weak"]["n"] == 1


class TestRatioTagsAndPrewindow:
    def test_ratio_breakdown_skips_untagged_and_orders_bins(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        # Only the lockup event carries a tag; bins render in stratification order.
        breakdown = ratio_breakdown(
            lockup.labeled + pos.labeled,
            {lockup.labeled[0].event_id: ">=5%"},
        )
        assert list(breakdown) == [">=5%"]
        assert breakdown[">=5%"]["n"] == 1

    def test_prewindow_return_entry_after_announcement_exit_at_target(self):
        # Sessions: Jul15=100, Jul16=101, Jul17=102, Jul20=103, Jul21=104.
        bars = _weekday_bars(
            date(2026, 7, 15),
            [100.0, 101.0, 102.0, 103.0, 104.0],
        )
        # Entry at the first session strictly after the announcement day...
        ret = prewindow_return(bars, date(2026, 7, 15), date(2026, 7, 20))
        assert ret == pytest.approx(103.0 / 101.0 - 1.0)
        # ...and exit at the last session on/before the appointment.
        ret = prewindow_return(bars, date(2026, 7, 15), date(2026, 7, 19))
        assert ret == pytest.approx(102.0 / 101.0 - 1.0)

    def test_prewindow_return_fails_soft_on_unobservable_endpoints(self):
        bars = _weekday_bars(date(2026, 7, 15), [100.0, 101.0])
        # Announcement after every session -> no entry point.
        assert prewindow_return(bars, date(2026, 7, 16), date(2026, 7, 17)) is None
        # Appointment before the entry session -> window holds nothing.
        assert prewindow_return(bars, date(2026, 7, 15), date(2026, 7, 14)) is None
        assert prewindow_return([], date(2026, 7, 15), date(2026, 7, 20)) is None


class TestRuleSubsetReadout:
    """The report-only weak-market / band-avoiding practice-rule preview."""

    @staticmethod
    def _lockup_obs():
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        return view.buckets[LOCKUP_SIGNAL].labeled[0]

    def test_subset_splits_weak_nonband_from_rest(self):
        obs = self._lockup_obs()
        day = obs.scheduled_date.isoformat()
        breakdown = rule_subset_breakdown(
            (obs,), {day: "weak"}, {obs.event_id: ">=5%"}
        )
        assert breakdown["rule"]["n"] == 1
        assert breakdown["excluded"]["n"] == 0

    def test_excluded_band_other_regimes_and_missing_tags(self):
        obs = self._lockup_obs()
        day = obs.scheduled_date.isoformat()
        for regime, tag in (("weak", "3-5%"), ("sideways", ">=5%"), ("weak", None)):
            breakdown = rule_subset_breakdown((obs,), {day: regime}, {obs.event_id: tag})
            assert breakdown["rule"].get("n", 0) == 0
            assert breakdown["excluded"]["n"] == 1

    def test_sides_always_sum_to_input(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        total = len(lockup.labeled)
        breakdown = rule_subset_breakdown(
            lockup.labeled,
            {o.scheduled_date.isoformat(): "unknown" for o in lockup.labeled},
            {},
        )
        assert breakdown["rule"].get("n", 0) + breakdown["excluded"]["n"] == total

    def test_net_columns_deduct_one_round_trip(self):
        obs = self._lockup_obs()
        day = obs.scheduled_date.isoformat()
        breakdown = rule_subset_breakdown(
            (obs,), {day: "weak"}, {obs.event_id: "1-3%"}
        )
        rule = breakdown["rule"]
        assert rule["mean_net_bps"] == pytest.approx(rule["mean_bps"] - 15.0, abs=0.2)

    def test_render_report_shows_rule_subset_for_lockup(self):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        view = build_tracker_view(batch, ALL_SIGNALS)
        view.regime_by_date = {
            o.scheduled_date.isoformat(): "weak" for o in batch.observations
        }
        view.ratio_by_event = {
            o.event_id: ">=5%" for o in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Practice-rule subset" in report
        assert "rule_subset: n=" in report
        assert "mean_net=" in report


class TestAbsorptionTags:
    """Moneyflow side-table labels: report-only, degrade to no labels."""

    @staticmethod
    def _moneyflow_cache(tmp_path):
        """25 weekday moneyflow sessions ending on the event day itself.

        Uniform rows (buy_lg=sell_lg=500, everything else 100) give a zero
        large-order net everywhere -> every labeled event is ``balanced``.
        """

        flow_dir = tmp_path / "moneyflow_daily"
        flow_dir.mkdir(parents=True)
        fields = [
            "trade_date", "ts_code",
            "buy_sm_amount", "sell_sm_amount",
            "buy_md_amount", "sell_md_amount",
            "buy_lg_amount", "sell_lg_amount",
            "buy_elg_amount", "sell_elg_amount",
        ]
        days: list[date] = []
        cursor = EVENT_DATE
        while len(days) < 25:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        for d in reversed(days):
            with (flow_dir / f"{d:%Y%m%d}.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.writer(handle)
                writer.writerow(fields)
                # buy_sm..sell_md=100 x4, buy_lg=500, sell_lg=500,
                # buy_elg=100, sell_elg=100 -> net 0, total positive.
                writer.writerow(
                    [f"{d:%Y%m%d}", SYMBOL] + [100.0] * 4
                    + [500.0, 500.0, 100.0, 100.0]
                )
        return flow_dir.parent

    def test_labels_cover_observed_events_from_moneyflow_cache(
        self, tmp_path
    ):
        cache = self._moneyflow_cache(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _absorption_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        assert set(labels.values()) == {"balanced"}

    def test_missing_moneyflow_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _absorption_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_absorption_breakdown_groups_in_label_order_and_skips_untagged(
        self,
    ):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = absorption_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "inflow",
                pos.labeled[0].event_id: "outflow",
            },
        )
        assert list(breakdown) == ["outflow", "inflow"]
        assert list(ABSORPTION_LABEL_ORDER) == ["outflow", "balanced", "inflow"]
        assert breakdown["outflow"]["n"] == 1
        assert breakdown["inflow"]["n"] == 1
        assert absorption_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_absorption_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.absorption_by_event = {
            obs.event_id: "balanced"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Labelled outcomes by absorption bucket:" in report
        assert "balanced: n=" in report


class TestBlockTags:
    """Block-trade side-table labels: report-only, degrade to no labels."""

    @staticmethod
    def _block_caches(tmp_path):
        """Daily history (written NEWEST-first, as Tushare delivers it) plus
        a blocktrade cache whose only print sits outside every window, so
        each labeled event resolves to ``none``."""

        stem = f"daily_{SYMBOL[:6]}{SYMBOL[7:]}"
        with (tmp_path / f"{stem}.csv").open("w", newline="",
                                             encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "close", "amount"])
            days: list[date] = []
            cursor = EVENT_DATE
            while len(days) < 25:
                if cursor.weekday() < 5:
                    days.append(cursor)
                cursor -= timedelta(days=1)
            for d in days:  # newest first on purpose
                writer.writerow([SYMBOL, f"{d:%Y%m%d}", 10.0, 1000.0])
        flow_dir = tmp_path / "blocktrade_daily"
        flow_dir.mkdir(parents=True)
        old_day = (days[-1] - timedelta(days=40)).strftime("%Y%m%d")
        with (flow_dir / f"{old_day}.csv").open("w", newline="",
                                                encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "price", "vol",
                             "amount", "buyer", "seller"])
            writer.writerow([SYMBOL, old_day, 9.0, 100.0, 900.0, "买", "卖"])
        return tmp_path

    def test_labels_cover_observed_events_from_block_caches(self, tmp_path):
        cache = self._block_caches(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _block_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        assert set(labels.values()) == {"none"}

    def test_missing_block_caches_degrade_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _block_labels_for_observations(tmp_path, batch.observations) == {}
        )

    def test_block_breakdown_groups_in_label_order_and_skips_untagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = block_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "near_flat",
                pos.labeled[0].event_id: "none",
            },
        )
        assert list(breakdown) == ["none", "near_flat"]
        assert list(BLOCK_LABEL_ORDER) == ["none", "discount_deep", "near_flat"]
        assert breakdown["none"]["n"] == 1
        assert breakdown["near_flat"]["n"] == 1
        assert block_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_block_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.blocks_by_event = {
            obs.event_id: "discount_deep"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Labelled outcomes by pre-event block bucket:" in report
        assert "discount_deep: n=" in report


class TestTurnoverTags:
    """Turnover side-table labels: report-only, degrade to no labels."""

    @staticmethod
    def _turnover_caches(tmp_path):
        """Daily history plus a dailybasic cache (written NEWEST-first, as
        Tushare delivers it) whose free-float turnover is constant 1.0, so
        each labeled event resolves to ``normal`` (ratio exactly 1.0)."""

        stem = f"daily_{SYMBOL[:6]}{SYMBOL[7:]}"
        with (tmp_path / f"{stem}.csv").open("w", newline="",
                                             encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "close", "amount"])
            days: list[date] = []
            cursor = EVENT_DATE
            while len(days) < 25:
                if cursor.weekday() < 5:
                    days.append(cursor)
                cursor -= timedelta(days=1)
            for d in days:  # newest first on purpose
                writer.writerow([SYMBOL, f"{d:%Y%m%d}", 10.0, 1000.0])
        db_stem = f"dailybasic_{SYMBOL[:6]}{SYMBOL[7:]}"
        with (tmp_path / f"{db_stem}.csv").open("w", newline="",
                                                encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "close",
                             "turnover_rate_f", "turnover_rate"])
            for d in days:  # newest first on purpose (#437 regression guard)
                writer.writerow([SYMBOL, f"{d:%Y%m%d}", 10.0, 1.0, 1.0])
        return tmp_path

    def test_labels_cover_observed_events_from_dailybasic(self, tmp_path):
        cache = self._turnover_caches(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _turnover_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        # flat tape -> window mean == baseline -> ratio 1.0 -> normal
        assert set(labels.values()) == {"normal"}

    def test_missing_dailybasic_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _turnover_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_turnover_breakdown_groups_in_label_order_and_skips_untagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = turnover_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "surge",
                pos.labeled[0].event_id: "shrink",
            },
        )
        assert list(breakdown) == ["shrink", "surge"]
        assert list(TURNOVER_LABEL_ORDER) == ["shrink", "normal", "surge"]
        assert breakdown["shrink"]["n"] == 1
        assert breakdown["surge"]["n"] == 1
        assert turnover_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_turnover_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.turnover_by_event = {
            obs.event_id: "normal"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Labelled outcomes by pre-event turnover bucket:" in report
        assert "normal: n=" in report


class TestChipsTags:
    """Chips (winner_rate) side-table labels: report-only, degrade clean."""

    @staticmethod
    def _chips_caches(tmp_path):
        """A cyqperf cache (written NEWEST-first, as Tushare delivers it)
        whose winner_rate is a constant 50.0 (feed percent) so each labeled
        event resolves to ``mid`` (fraction 0.5).  No daily cache needed:
        the chips lookup reads only the cyqperf files."""

        stem = f"cyqperf_{SYMBOL[:6]}{SYMBOL[7:]}"
        with (tmp_path / f"{stem}.csv").open("w", newline="",
                                             encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "his_low", "his_high",
                             "cost_5pct", "cost_15pct", "cost_50pct",
                             "cost_85pct", "cost_95pct", "weight_avg",
                             "winner_rate"])
            days: list[date] = []
            cursor = EVENT_DATE
            while len(days) < 25:
                if cursor.weekday() < 5:
                    days.append(cursor)
                cursor -= timedelta(days=1)
            for d in days:  # newest first on purpose (#437 regression guard)
                writer.writerow([SYMBOL, f"{d:%Y%m%d}", 9.0, 11.0, 9.5,
                                 9.8, 10.0, 10.3, 10.6, 10.05, 50.0])
        return tmp_path

    def test_labels_cover_observed_events_from_cyqperf(self, tmp_path):
        cache = self._chips_caches(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _chips_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        # constant 50.0 percent -> fraction 0.5 -> mid band
        assert set(labels.values()) == {"mid"}

    def test_missing_cyqperf_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _chips_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_chips_breakdown_groups_in_label_order_and_skips_untagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = chips_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "underwater",
                pos.labeled[0].event_id: "profit",
            },
        )
        assert list(breakdown) == ["underwater", "profit"]
        assert list(CHIPS_LABEL_ORDER) == ["underwater", "mid", "profit"]
        assert breakdown["underwater"]["n"] == 1
        assert breakdown["profit"]["n"] == 1
        assert chips_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_chips_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.chips_by_event = {
            obs.event_id: "mid"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Labelled outcomes by pre-event chips bucket:" in report
        assert "mid: n=" in report


class TestHolderTags:
    """Pre-event holder-trade side-table labels: report-only, degrade clean."""

    @staticmethod
    def _holder_caches(tmp_path):
        """A holdertrade cache with one IN record dated EVENT_DATE-10
        calendar days (inside the frozen [day-30d, day) window), so every
        observed event resolves to ``net_buy``.  Files are one ann_date
        each, as the fetcher lays them out."""

        ann = (EVENT_DATE - timedelta(days=10)).strftime("%Y%m%d")
        folder = tmp_path / "holdertrade_daily"
        folder.mkdir()
        with (folder / f"{ann}.csv").open("w", newline="",
                                          encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "ann_date", "holder_name",
                             "holder_type", "in_de", "change_vol"])
            writer.writerow([SYMBOL, ann, "某投资有限公司", "G", "IN",
                             500000.0])
        return tmp_path

    def test_labels_cover_observed_events_from_holdertrade(self, tmp_path):
        cache = self._holder_caches(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _holder_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        # single IN record inside the window -> net_buy
        assert set(labels.values()) == {"net_buy"}

    def test_empty_window_yields_no_records_label(self, tmp_path):
        # cache exists but holds nothing near EVENT_DATE: no_records is a
        # real label, not an omission.
        folder = tmp_path / "holdertrade_daily"
        folder.mkdir()
        ann = date(2025, 1, 6).strftime("%Y%m%d")
        (folder / f"{ann}.csv").write_text(
            "ts_code,ann_date,in_de,change_vol\n"
            f"{SYMBOL},{ann},IN,1000.0\n",
            encoding="utf-8",
        )
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _holder_labels_for_observations(tmp_path, batch.observations)
        assert set(labels.values()) == {"no_records"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _holder_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_holders_breakdown_groups_in_label_order_and_skips_untagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = holders_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "net_sell",
                pos.labeled[0].event_id: "no_records",
            },
        )
        assert list(breakdown) == ["net_sell", "no_records"]
        assert list(HOLDER_LABEL_ORDER) == [
            "net_buy", "flat", "net_sell", "no_records"
        ]
        assert breakdown["net_sell"]["n"] == 1
        assert breakdown["no_records"]["n"] == 1
        assert holders_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_holders_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.holders_by_event = {
            obs.event_id: "net_buy"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Labelled outcomes by pre-event holder-trade bucket:" in report
        assert "net_buy: n=" in report


class TestRepurchaseTags:
    """Pre-event repurchase side-table labels: report-only, degrade clean."""

    @staticmethod
    def _repurchase_caches(tmp_path):
        """A repurchase cache with one 实施 record dated EVENT_DATE-10
        calendar days (inside the frozen [day-30d, day) window), so every
        observed event resolves to ``active``.  Files are one ann_date
        each, as the fetcher lays them out."""

        ann = (EVENT_DATE - timedelta(days=10)).strftime("%Y%m%d")
        folder = tmp_path / "repurchase_ann"
        folder.mkdir()
        with (folder / f"{ann}.csv").open("w", newline="",
                                          encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "ann_date", "end_date", "proc",
                             "amount"])
            writer.writerow([SYMBOL, ann, "", "实施", 1200000.0])
        return tmp_path

    def test_labels_cover_observed_events_from_repurchase(self, tmp_path):
        cache = self._repurchase_caches(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _repurchase_labels_for_observations(
            cache, batch.observations
        )
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        # single 实施 record inside the window -> active
        assert set(labels.values()) == {"active"}

    def test_empty_window_yields_no_records_label(self, tmp_path):
        # cache exists but holds nothing near EVENT_DATE: no_records is a
        # real label, not an omission.
        folder = tmp_path / "repurchase_ann"
        folder.mkdir()
        ann = date(2025, 1, 6).strftime("%Y%m%d")
        (folder / f"{ann}.csv").write_text(
            "ts_code,ann_date,end_date,proc\n"
            f"{SYMBOL},{ann},,完成\n",
            encoding="utf-8",
        )
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _repurchase_labels_for_observations(
            tmp_path, batch.observations
        )
        assert set(labels.values()) == {"no_records"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _repurchase_labels_for_observations(
                tmp_path, batch.observations
            )
            == {}
        )

    def test_repurchases_breakdown_groups_in_label_order_and_skips(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = repurchases_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "done",
                pos.labeled[0].event_id: "no_records",
            },
        )
        assert list(breakdown) == ["done", "no_records"]
        assert list(REPURCHASE_LABEL_ORDER) == [
            "active", "stopped", "done", "no_records"
        ]
        assert breakdown["done"]["n"] == 1
        assert breakdown["no_records"]["n"] == 1
        assert repurchases_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_repurchases_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.repurchases_by_event = {
            obs.event_id: "active"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert (
            "Labelled outcomes by pre-event repurchase state:" in report
        )
        assert "active: n=" in report


class TestPledgeTags:
    """Pre-event pledge side-table labels: report-only, degrade clean."""

    @staticmethod
    def _pledge_cache(tmp_path):
        """A pledgestat cache with one snapshot dated EVENT_DATE-10
        calendar days (inside the frozen [day-30d, day) window) at a
        high ratio, so every observed event resolves to ``high``.
        Files are one symbol each, as the fetcher lays them out."""

        end = (EVENT_DATE - timedelta(days=10)).strftime("%Y%m%d")
        with (tmp_path / "pledgestat_600001SH.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "end_date", "pledge_ratio"])
            writer.writerow([SYMBOL, end, 25.0])
        return tmp_path

    def test_labels_cover_observed_events_from_pledge(self, tmp_path):
        cache = self._pledge_cache(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _pledge_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        # single 25% snapshot inside the window -> high
        assert set(labels.values()) == {"high"}

    def test_empty_window_yields_no_snapshot_label(self, tmp_path):
        # cache exists but holds nothing near EVENT_DATE: no_snapshot is a
        # real label, not an omission.
        with (tmp_path / "pledgestat_600001SH.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "end_date", "pledge_ratio"])
            writer.writerow([SYMBOL, "20251201", 25.0])
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _pledge_labels_for_observations(
            tmp_path, batch.observations
        )
        assert set(labels.values()) == {"no_snapshot"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _pledge_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_pledges_breakdown_groups_in_label_order_and_skips(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = pledges_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "mid",
                pos.labeled[0].event_id: "no_snapshot",
            },
        )
        assert list(breakdown) == ["mid", "no_snapshot"]
        assert list(PLEDGE_LABEL_ORDER) == [
            "high", "mid", "low", "no_snapshot"
        ]
        assert breakdown["mid"]["n"] == 1
        assert breakdown["no_snapshot"]["n"] == 1
        assert pledges_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_pledge_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.pledges_by_event = {
            obs.event_id: "high"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Labelled outcomes by pre-event pledge bucket:" in report
        assert "high: n=" in report


class TestToplistTags:
    """Pre-event dragon-tiger side-table labels: degrade clean, label
    vocabulary mirrors the #477 frozen buckets."""

    @staticmethod
    def _toplist_cache(tmp_path):
        """A toplist_daily cache with one listing dated EVENT_DATE-10
        calendar days (inside the frozen [day-30d, day) window) with a
        sell-deviation reason, so every observed event resolves to
        ``sell_dev``.  Files are one trading day each, as the fetcher
        lays them out."""

        day = (EVENT_DATE - timedelta(days=10)).strftime("%Y%m%d")
        folder = tmp_path / "toplist_daily"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{day}.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "reason"])
            writer.writerow([SYMBOL, "日跌幅偏离值达到7%的前5只证券"])
        return tmp_path

    def test_labels_cover_observed_events_from_toplist(self, tmp_path):
        cache = self._toplist_cache(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _toplist_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        # single sell-deviation listing inside the window -> sell_dev
        assert set(labels.values()) == {"sell_dev"}

    def test_empty_window_yields_no_listing_label(self, tmp_path):
        # cache exists but holds nothing near EVENT_DATE: no_listing is a
        # real label, not an omission.
        folder = tmp_path / "toplist_daily"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "20251201.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "reason"])
            writer.writerow([SYMBOL, "日涨幅偏离值达到7%的前5只证券"])
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _toplist_labels_for_observations(
            tmp_path, batch.observations
        )
        assert set(labels.values()) == {"no_listing"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _toplist_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_toplists_breakdown_groups_in_label_order_and_skips(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = toplists_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "rise_dev",
                pos.labeled[0].event_id: "no_listing",
            },
        )
        assert list(breakdown) == ["rise_dev", "no_listing"]
        assert list(TOPLIST_LABEL_ORDER) == [
            "sell_dev", "rise_dev", "other", "no_listing"
        ]
        assert breakdown["rise_dev"]["n"] == 1
        assert breakdown["no_listing"]["n"] == 1
        assert toplists_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_toplist_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.toplists_by_event = {
            obs.event_id: "sell_dev"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert "Labelled outcomes by pre-event dragon-tiger bucket:" in report
        assert "sell_dev: n=" in report


class TestHoldernumTags:
    """Pre-event shareholder-count side-table labels: degrade clean,
    label vocabulary mirrors the #497 frozen buckets."""

    @staticmethod
    def _holdernum_cache(tmp_path):
        """A holdernum cache with two announcements inside the frozen
        [day-365d, day) window: holder count drops 100000 -> 80000
        (-20%), so every observed event resolves to ``contract``.
        One file per symbol, stems re-dotted by the study loader
        (holdernum_600001SH.csv -> ts_code 600001.SH)."""

        prev_day = (EVENT_DATE - timedelta(days=100)).strftime("%Y%m%d")
        anchor_day = (EVENT_DATE - timedelta(days=40)).strftime("%Y%m%d")
        with (tmp_path / "holdernum_600001SH.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "ann_date", "end_date", "holder_num"])
            writer.writerow([SYMBOL, prev_day, "20260331", "100000"])
            writer.writerow([SYMBOL, anchor_day, "20260531", "80000"])
        return tmp_path

    def test_labels_cover_observed_events_from_holdernum(self, tmp_path):
        cache = self._holdernum_cache(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _holdernum_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed" and obs.symbol
        }
        assert set(labels) == expected
        # 100000 -> 80000 across consecutive announcements: -20% contract
        assert set(labels.values()) == {"contract"}

    def test_empty_window_yields_no_snapshot_label(self, tmp_path):
        # cache exists but both disclosures sit outside the window:
        # no_snapshot is a real label, not an omission.
        with (tmp_path / "holdernum_600001SH.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "ann_date", "end_date", "holder_num"])
            writer.writerow([SYMBOL, "20250110", "20241231", "100000"])
            writer.writerow([SYMBOL, "20250210", "20250131", "90000"])
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _holdernum_labels_for_observations(
            tmp_path, batch.observations
        )
        assert set(labels.values()) == {"no_snapshot"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _holdernum_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_holdernums_breakdown_groups_in_label_order_and_skips(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = holdernums_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "expand",
                pos.labeled[0].event_id: "no_snapshot",
            },
        )
        assert list(breakdown) == ["expand", "no_snapshot"]
        assert list(HOLDERNUM_LABEL_ORDER) == [
            "contract", "stable", "expand", "no_snapshot"
        ]
        assert breakdown["expand"]["n"] == 1
        assert breakdown["no_snapshot"]["n"] == 1
        assert holdernums_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_holdernum_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.holdernums_by_event = {
            obs.event_id: "expand"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert (
            "Labelled outcomes by pre-event shareholder-count bucket:"
            in report
        )
        assert "expand: n=" in report


class TestMacroTags:
    """Macro release-window side-table labels (#509/#512): market-level
    (keyed by entry day only, no symbol pairing), degrade clean when the
    macro_* caches are unavailable."""

    @staticmethod
    def _macro_cache(tmp_path):
        """Index calendar with a gap after the 2026-08-05 event day plus
        the four endpoint files.  CPI/PPI (month 2026-07 -> presumed day
        9) land off-calendar and shift forward to 08-10 = the very next
        trading session after the event day, so every observed event is
        ``ante``; M2 shifts to 08-12; GDP Q1 sits before the span start
        and must be skipped silently (no fail-closed error)."""

        days = ["20260701", "20260715", "20260803", "20260804",
                "20260805", "20260810", "20260812", "20260818"]
        with (tmp_path / "index_000001SH.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["trade_date", "close"])
            for i, day in enumerate(days):
                writer.writerow([day, 3000 + i])
        for stem, fields, rows in [
            ("cpi", ["month", "nt_val", "nt_yoy"], [["202607", 100.1, 0.1]]),
            ("ppi", ["month", "ppi_yoy"], [["202607", 3.0]]),
            ("money", ["month", "m2_yoy"], [["202607", 7.0]]),
            ("gdp", ["quarter", "gdp_yoy"], [["2026Q1", 4.0]]),
        ]:
            with (tmp_path / f"macro_{stem}.csv").open(
                    "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(fields)
                writer.writerows(rows)
        return tmp_path

    def test_labels_cover_observed_events_from_macro_caches(self, tmp_path):
        cache = self._macro_cache(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _macro_labels_for_observations(cache, batch.observations)
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed"
        }
        assert set(labels) == expected
        # release lands on the next trading day after entry: ante wins
        assert set(labels.values()) == {"ante"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _macro_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_macro_breakdown_groups_in_label_order_and_skips(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = macro_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "outside",
                pos.labeled[0].event_id: "ante",
            },
        )
        assert list(breakdown) == ["ante", "outside"]  # MACRO_LABEL_ORDER
        assert list(MACRO_LABEL_ORDER) == [
            "ante", "same_day", "post", "outside"
        ]
        assert breakdown["ante"]["n"] == 1
        assert breakdown["outside"]["n"] == 1
        # market-level table missing -> empty breakdown, never an error
        assert macro_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_macro_lines_when_tagged(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.macro_by_event = {
            obs.event_id: "same_day"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert (
            "Labelled outcomes by macro release-window bucket:" in report
        )
        assert "same_day: n=" in report


class TestValuationTags:
    """Pre-entry valuation-percentile side-table labels (#534/#536):
    per-symbol dailybasic shards keyed by (symbol, scheduled_date),
    degrade clean when a shard is missing."""

    @staticmethod
    def _weekday_days(start: date, end: date) -> list[str]:
        days: list[str] = []
        cur = start
        while cur <= end:
            if cur.weekday() < 5:
                days.append(cur.strftime("%Y%m%d"))
            cur += timedelta(days=1)
        return days

    @classmethod
    def _valuation_cache(cls, tmp_path):
        """Descending shard (newest first, like production dailybasic
        exports): steadily FALLING pe_ttm over ~a year of weekdays, so
        the last value strictly before each August 2026 entry sits below
        its whole history window -> low_le25."""
        days = cls._weekday_days(date(2025, 8, 4), date(2026, 8, 14))
        rows = [
            (day, f"{1600 - 2 * i:.2f}") for i, day in enumerate(days)
        ]
        rows.reverse()  # production shards store newest-first
        with (tmp_path / "dailybasic_600001SH.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "pe_ttm"])
            writer.writerows([["600001.SH", d, v] for d, v in rows])
        return tmp_path

    def test_labels_cover_observed_events_from_valuation_cache(
            self, tmp_path):
        cache = self._valuation_cache(tmp_path)
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _valuation_labels_for_observations(
            cache, batch.observations
        )
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed"
        }
        assert set(labels) == expected
        # falling pe_ttm -> current sits at the bottom of its own history
        assert set(labels.values()) == {"low_le25"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _valuation_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_adapter_snaps_event_day_and_covers_edge_buckets(
            self, tmp_path):
        from Ashare.event_valuation_prelockup_study import (
            valuation_buckets_for_entries,
        )

        # 600001.SH: ~220 rising-pe sessions ending 2026-08-05 -> high_ge75
        days = self._weekday_days(date(2025, 9, 15), date(2026, 8, 5))
        rising = [(d, f"{100 + i}.0") for i, d in enumerate(days)]
        # 000002.SZ: five sessions only -> always short_history
        tiny = [("20260729", "10.0"), ("20260730", "11.0"),
                ("20260731", "12.0"), ("20260803", "13.0"),
                ("20260804", "14.0")]
        # 000003.SZ: long book whose next-to-last value is null ->
        # loss_or_missing when querying the last day
        gap_days = self._weekday_days(date(2025, 9, 1), date(2026, 8, 5))
        gap = [(d, f"{i + 1}.0") if i != len(gap_days) - 2 else (d, "")
               for i, d in enumerate(gap_days)]
        books = {
            "600001SH": ("600001.SH", rising),
            "000002SZ": ("000002.SZ", tiny),
            "000003SZ": ("000003.SZ", gap),
        }
        for stem, (code, rows) in books.items():
            with (tmp_path / f"dailybasic_{stem}.csv").open(
                    "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["ts_code", "trade_date", "pe_ttm"])
                writer.writerows([[code, d, v] for d, v in rows])
        labels = valuation_buckets_for_entries(
            tmp_path,
            [
                ("600001.SH", "20260806"),  # off-calendar -> snaps to 08-05
                ("600001.SH", "20260805"),
                ("600001.SH", "20150101"),  # before the shard starts
                ("000002.SZ", "20260804"),
                ("000003.SZ", gap_days[-1]),
            ],
        )
        assert labels[("600001.SH", "20260806")] \
            == labels[("600001.SH", "20260805")] == "high_ge75"
        assert labels[("600001.SH", "20150101")] == "short_history"
        assert labels[("000002.SZ", "20260804")] == "short_history"
        assert labels[("000003.SZ", gap_days[-1])] == "loss_or_missing"
        # shard absent -> that entry stays unlabeled, the rest still
        # label (one unknown symbol must not silence the whole table)
        assert valuation_buckets_for_entries(
            tmp_path,
            [("999999.SZ", "20260805"), ("000002.SZ", "20260804")],
        ) == {("000002.SZ", "20260804"): "short_history"}

    def test_valuation_breakdown_groups_in_label_order_and_skips(self):
        view = build_tracker_view(
            _batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS
        )
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = valuations_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "high_ge75",
                pos.labeled[0].event_id: "low_le25",
            },
        )
        assert list(breakdown) == ["low_le25", "high_ge75"]
        assert list(VALUATION_LABEL_ORDER) == [
            "low_le25", "mid", "high_ge75", "short_history",
            "loss_or_missing",
        ]
        assert breakdown["low_le25"]["n"] == 1
        # label table missing -> empty breakdown, never an error
        assert valuations_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_valuation_lines_when_tagged(self):
        view = build_tracker_view(
            _batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS
        )
        view.valuations_by_event = {
            obs.event_id: "mid"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert (
            "Labelled outcomes by pre-entry valuation-percentile bucket:"
            in report
        )
        assert "mid: n=" in report


class TestHolderTypeTags:
    """Unlock-batch holder-type side-table labels (#531/#536): keyed on
    (symbol, scheduled_date) == (ts_code, float_date) identity; unknown
    batches label as no_match so coverage is total."""

    @staticmethod
    def _write_float_cache(tmp_path, rows):
        with (tmp_path / "share_float.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["ts_code", "ann_date", "float_date", "float_ratio",
                 "share_type"]
            )
            writer.writerows(rows)
        return tmp_path

    def test_labels_cover_observed_events_from_holdertype_cache(
            self, tmp_path):
        cache = self._write_float_cache(
            tmp_path,
            [("600001.SH", "20260701", "20260805", "5.0", "定增股份")],
        )
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        labels = _holdertype_labels_for_observations(
            cache, batch.observations
        )
        expected = {
            obs.event_id
            for obs in batch.observations
            if obs.observation_status == "observed"
        }
        assert set(labels) == expected  # no_match keeps coverage total
        assert set(labels.values()) == {"placement", "no_match"}

    def test_missing_cache_degrades_to_no_labels(self, tmp_path):
        batch = _batch({SYMBOL: SELL_OFF_CLOSES})
        assert (
            _holdertype_labels_for_observations(tmp_path, batch.observations)
            == {}
        )

    def test_adapter_precedence_and_invalid_rows(self, tmp_path):
        from Ashare.event_unlock_holdertype_study import (
            holdertype_buckets_for_entries,
        )

        cache = self._write_float_cache(
            tmp_path,
            [
                # mixed batch: placement outranks incentive (frozen order)
                ("600001.SH", "20260701", "20260805", "5.0", "定增股份"),
                ("600001.SH", "20260701", "20260805", "1.0",
                 "股权激励限售流通"),
                ("600002.SH", "20260701", "20260805", "2.0",
                 "股权激励限售流通"),
                ("600003.SH", "20260701", "20260805", "1.0",
                 "高管锁定股份"),
                # invalid rows are skipped silently -> no_match
                ("600004.SH", "20260701", "20260601", "3.0", "首发原始股"),
                ("600005.SH", "20260701", "20260805", "bad", "定增股份"),
                ("600006.SH", "20170601", "20170101", "9.9", "首发原始股"),
            ],
        )
        labels = holdertype_buckets_for_entries(
            tmp_path,
            [
                ("600001.SH", "20260805"),
                ("600002.SH", "20260805"),
                ("600003.SH", "20260805"),
                ("600004.SH", "20260805"),
                ("600005.SH", "20260805"),
                ("600006.SH", "20260805"),
            ],
        )
        assert [labels[(code, "20260805")] for code in (
            "600001.SH", "600002.SH", "600003.SH", "600004.SH",
            "600005.SH", "600006.SH",
        )] == [
            "placement", "incentive", "other_legacy", "no_match",
            "no_match", "no_match",
        ]

    def test_holdertypes_breakdown_groups_in_label_order_and_skips(self):
        view = build_tracker_view(
            _batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS
        )
        lockup = view.buckets[LOCKUP_SIGNAL]
        pos = view.buckets[EARNINGS_POS_SIGNAL]
        breakdown = holdertypes_breakdown(
            lockup.labeled + pos.labeled,
            {
                lockup.labeled[0].event_id: "incentive",
                pos.labeled[0].event_id: "placement",
            },
        )
        assert list(breakdown) == ["placement", "incentive"]
        assert list(HOLDERTYPE_LABEL_ORDER) == [
            "placement", "insider", "incentive", "other_legacy",
            "no_match",
        ]
        assert breakdown["placement"]["n"] == 1
        assert holdertypes_breakdown(lockup.labeled, {}) == {}

    def test_render_report_lists_holdertype_lines_when_tagged(self):
        view = build_tracker_view(
            _batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS
        )
        view.holdertypes_by_event = {
            obs.event_id: "incentive"
            for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        report = render_report(
            view, since=date(2026, 4, 1), as_of=AS_OF, dry_run=True
        )
        assert (
            "Labelled outcomes by unlock-batch holder-type bucket:"
            in report
        )
        assert "incentive: n=" in report


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

    # SSE index series aligned with the same sessions: the 10-session return
    # ending at the Aug 5 close is -3% (weak) and ending at the Aug 10 close
    # is +3% (strong); every other date sits inside the sideways band.
    index_days = sorted(close_by_day)
    index_pos = {d: i for i, d in enumerate(index_days)}
    aug5 = EVENT_DATE.strftime("%Y%m%d")
    aug10 = date(2026, 8, 10).strftime("%Y%m%d")
    index_closes: list[float] = []
    for d in index_days:
        if d == aug5:
            index_closes.append(97.0)
        elif d == aug10:
            index_closes.append(103.0)
        else:
            base = 97.0 if index_pos[d] > index_pos[aug5] and index_pos[d] < index_pos[aug10] else 100.0
            index_closes.append(base)
    _write_csv(
        cache / "index_000001SH.csv",
        ["ts_code", "trade_date", "close"],
        [["000001.SH", d, f"{close:.4f}"] for d, close in zip(index_days, index_closes)],
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
        # Regime tags: the fixture index makes Aug 5 weak and Aug 10 strong.
        assert view.regime_by_date["2026-08-05"] == "weak"
        assert view.regime_by_date["2026-08-10"] == "strong"
        lockup_breakdown = regime_breakdown(
            view.buckets[LOCKUP_SIGNAL].labeled, view.regime_by_date
        )
        assert lockup_breakdown["weak"]["n"] == 1
        neg_breakdown = regime_breakdown(
            view.buckets[EARNINGS_NEG_SIGNAL].labeled, view.regime_by_date
        )
        assert neg_breakdown["strong"]["n"] == 1
        # Float-ratio tags land on lockup events only.
        lockup_obs = view.buckets[LOCKUP_SIGNAL].labeled[0]
        assert view.ratio_by_event[lockup_obs.event_id] == "1-3%"
        ratio_stats = ratio_breakdown(
            view.buckets[LOCKUP_SIGNAL].labeled, view.ratio_by_event
        )
        assert ratio_stats["1-3%"]["n"] == 1
        assert ratio_breakdown(
            view.buckets[EARNINGS_POS_SIGNAL].labeled, view.ratio_by_event
        ) == {}
        # Pre-disclosure trade window for earnings_pos: forecast announced
        # 2026-06-15 -> first session close after it is Jul 15 (105.0);
        # exit at the Aug 5 appointment close (100.0) => -4.76%.
        pre = view.prewindow_stats[EARNINGS_POS_SIGNAL]
        assert pre["n"] == 1
        assert pre["mean_bps"] == pytest.approx(-476.2, abs=0.2)
        assert EARNINGS_NEG_SIGNAL not in view.prewindow_stats
        # Per-sample rows mirror the aggregate and feed the state-JSON
        # milestone export (net mean / win rate / two-half consistency).
        samples = view.prewindow_samples[EARNINGS_POS_SIGNAL]
        assert len(samples) == 1
        assert samples[0]["event_date"] == "2026-08-05"
        assert samples[0]["pre_return_bps"] == pytest.approx(-476.2, abs=0.2)
        assert EARNINGS_NEG_SIGNAL not in view.prewindow_samples
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
        entries, skipped, ratio_by_event = load_lockup_entries(
            mini_cache, samples, since=date(2026, 1, 1)
        )
        assert skipped == 1  # blank float_share row fails closed
        assert len(entries) == 1
        assert entries[0].event_type == SIGNAL_EVENT_TYPE
        # The valid fixture row unlocks 1.5% of the float.
        assert ratio_by_event[entries[0].event_id] == "1-3%"

    def test_disclosure_loader_skips_forecastless_and_uncertain_rows(
        self, mini_cache
    ):
        samples = {SYMBOL}
        entries, skipped, ann_by_event = load_disclosure_entries(
            mini_cache, samples, since=date(2026, 1, 1)
        )
        assert skipped == 2  # no-forecast row + uncertain row
        assert len(entries) == 2
        # Forecast announcement dates re-join onto the prefixed event ids so
        # the pre-disclosure trade window can be measured.
        assert sorted(ann_by_event.values()) == [
            date(2026, 6, 15),
            date(2026, 7, 20),
        ]
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


# --- universe selection (top-200 vs top-1000 expansion) ----------------------


class TestUniverseSelection:
    def _add_expanded_symbol(self, cache, extra: str) -> None:
        """Give ``extra`` the same closed lockup window the base symbol has."""

        stem = extra.replace(".", "")
        base_stem = SYMBOL.replace(".", "")
        bar_lines = (
            cache / f"daily_{base_stem}.csv"
        ).read_text(encoding="utf-8").splitlines()
        header, rows = bar_lines[0], bar_lines[1:]
        (cache / f"daily_{stem}.csv").write_text(
            "\n".join([header] + rows) + "\n", encoding="utf-8"
        )
        adj_lines = (
            cache / f"adjfactor_{base_stem}.csv"
        ).read_text(encoding="utf-8").splitlines()
        (cache / f"adjfactor_{stem}.csv").write_text(
            "\n".join(adj_lines) + "\n", encoding="utf-8"
        )
        float_day = EVENT_DATE.strftime("%Y%m%d")
        # share_float.csv already exists in the fixture; append one valid
        # batch for the expanded symbol instead of rewriting it.
        with (cache / "share_float.csv").open("a", encoding="utf-8") as handle:
            handle.write(
                f"{extra},20260701,{float_day},1200000,1.5,Holder X,IPO\n"
            )

    def test_default_tracks_baseline_universe(self, mini_cache, tmp_path):
        view = run_tracker(
            mini_cache,
            tmp_path / "journal.jsonl",
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
            signals=(LOCKUP_SIGNAL,),
            dry_run=True,
        )
        assert view.samples_file == "sample_symbols"
        assert view.universe_size == 1
        assert len(view.buckets[LOCKUP_SIGNAL].labeled) == 1

    def test_expanded_file_extends_the_tracked_set(self, mini_cache, tmp_path):
        extra = "600777.SH"
        self._add_expanded_symbol(mini_cache, extra)
        _write_csv(
            mini_cache / "sample_symbols_expanded.csv",
            ["ts_code"],
            [[SYMBOL], [extra]],
        )

        view = run_tracker(
            mini_cache,
            tmp_path / "journal.jsonl",
            since=date.fromisoformat(DEFAULT_SINCE),
            as_of=AS_OF,
            signals=(LOCKUP_SIGNAL,),
            dry_run=True,
            samples_file="sample_symbols_expanded",
        )
        assert view.samples_file == "sample_symbols_expanded"
        assert view.universe_size == 2
        labeled_codes = {
            obs.symbol for obs in view.buckets[LOCKUP_SIGNAL].labeled
        }
        assert labeled_codes == {SYMBOL, extra}


# --- incremental forecast warm-up (universe expansion support) ---------------


class TestForecastCacheIncremental:
    def _read_rows(self, path):
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[0], [line.split(",") for line in lines[1:]]

    def test_existing_cache_gains_only_missing_symbols(self, tmp_path, monkeypatch):
        from Ashare import event_calendar_earnings_groups as groups

        cache = tmp_path
        header = ["ts_code", "ann_date", "end_date", "type", "update_flag"]
        today = time.strftime("%Y%m%d")
        _write_csv(
            cache / "forecast.csv",
            header,
            # Fresh newest-announcement date keeps the incremental path.
            [[SYMBOL, today, "20260630", "预增", "1"]],
        )
        extra = "600777.SH"

        calls: list[str] = []

        def fake_call(ts_code: str):
            calls.append(ts_code)
            return header, [
                [extra, today, "20260930", "预减", "1"],
                [extra, "20260702", "20260930", "预减", "2"],
            ]

        monkeypatch.setattr(groups, "_call_forecast", fake_call)

        path = groups.ensure_forecast_cache(cache, {SYMBOL, extra})
        assert calls == [extra]  # covered symbol never re-fetched
        out_header, rows = self._read_rows(path)
        assert out_header == ",".join(header)
        codes = {r[0] for r in rows}
        assert codes == {SYMBOL, extra}
        # Original row preserved verbatim.
        assert [SYMBOL, today, "20260630", "预增", "1"] in rows

        # Second pass over the same universe is a pure no-op.
        path2 = groups.ensure_forecast_cache(cache, {SYMBOL, extra})
        assert calls == [extra]
        assert path2 == path
        _, rows_again = self._read_rows(path2)
        assert rows_again == rows

    def test_stale_cache_repulls_everything_without_duplicates(
        self, tmp_path, monkeypatch
    ):
        """A frozen cache silently starves future disclosure events (#544).

        When the stored newest announcement is older than the freshness
        ceiling every sample symbol is re-pulled; batch identity dedupe
        keeps already-stored rows exactly once while genuinely new
        periods land.
        """

        from Ashare import event_calendar_earnings_groups as groups

        cache = tmp_path
        header = ["ts_code", "ann_date", "end_date", "type", "update_flag"]
        today = time.strftime("%Y%m%d")
        old_day = "20260101"
        _write_csv(
            cache / "forecast.csv",
            header,
            [[SYMBOL, old_day, "20260331", "预增", "1"]],
        )
        extra = "600777.SH"
        calls: list[str] = []

        def fake_call(ts_code: str):
            calls.append(ts_code)
            if ts_code == SYMBOL:
                return header, [
                    # Duplicate of the stored batch -> collapsed.
                    [SYMBOL, old_day, "20260331", "预增", "1"],
                    # New report period published later -> must land.
                    [SYMBOL, today, "20260930", "预增", "1"],
                ]
            return header, [[extra, today, "20260930", "预减", "2"]]

        monkeypatch.setattr(groups, "_call_forecast", fake_call)

        groups.ensure_forecast_cache(cache, {SYMBOL, extra})
        assert sorted(calls) == sorted([SYMBOL, extra])
        _, rows = self._read_rows(cache / "forecast.csv")
        stored_old = [r for r in rows if r[1] == old_day]
        assert len(stored_old) == 1  # duplicate batch collapsed
        assert [SYMBOL, today, "20260930", "预增", "1"] in rows
        assert [extra, today, "20260930", "预减", "2"] in rows
        assert len(rows) == 3

        # The re-pull refreshed staleness: next pass is incremental again.
        before = len(calls)
        groups.ensure_forecast_cache(cache, {SYMBOL, extra})
        assert len(calls) == before

    def test_fresh_build_fetches_everything(self, tmp_path, monkeypatch):
        from Ashare import event_calendar_earnings_groups as groups

        cache = tmp_path
        header = ["ts_code", "ann_date", "end_date", "type", "update_flag"]
        calls: list[str] = []

        def fake_call(ts_code: str):
            calls.append(ts_code)
            return header, [[ts_code, "20260615", "20260630", "预增", "1"]]

        monkeypatch.setattr(groups, "_call_forecast", fake_call)
        path = groups.ensure_forecast_cache(cache, {"600001.SH", "600777.SH"})
        assert sorted(calls) == ["600001.SH", "600777.SH"]
        _, rows = self._read_rows(path)
        assert len(rows) == 2


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
        assert "float_ratio" in report  # active-table column

    def test_report_shows_prewindow_readout_when_present(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        view.prewindow_stats = {
            EARNINGS_POS_SIGNAL: {
                "n": 1,
                "mean_bps": -476.2,
                "median_bps": -476.2,
                "win_rate": 0.0,
            }
        }
        report = render_report(view, since=date(2026, 6, 1), as_of=AS_OF, dry_run=True)
        assert "Pre-disclosure trade window" in report
        assert "report-only, not journaled" in report
        assert "-476.2bps" in report

    def test_report_omits_prewindow_and_ratio_sections_when_absent(self):
        view = build_tracker_view(_batch({SYMBOL: SELL_OFF_CLOSES}), ALL_SIGNALS)
        report = render_report(view, since=date(2026, 6, 1), as_of=AS_OF, dry_run=True)
        assert "Pre-disclosure trade window" not in report
        assert "Labelled outcomes by float-ratio bucket" not in report
