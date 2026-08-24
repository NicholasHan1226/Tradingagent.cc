"""Contract tests for the multi-signal event tracker (lockup + earnings).

Covers the three tracked signals — ``lockup`` (sell_off into expiry),
``earnings_pos`` (prior positive forecast) and ``earnings_neg`` (prior
negative forecast) — plus the journal write path with its ledger/journal
read-back dedup guard.
"""

from __future__ import annotations

import csv
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
    TURNOVER_LABEL_ORDER,
    HOLDER_LABEL_ORDER,
    REPURCHASE_LABEL_ORDER,
    _absorption_labels_for_observations,
    _block_labels_for_observations,
    _chips_labels_for_observations,
    _holder_labels_for_observations,
    _repurchase_labels_for_observations,
    _turnover_labels_for_observations,
    absorption_breakdown,
    block_breakdown,
    chips_breakdown,
    holders_breakdown,
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
