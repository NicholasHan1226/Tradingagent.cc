"""Contract tests for the fixture-first event-catalyst shadow factor."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from Ashare.event_catalyst_shadow import (
    ANTICIPATION_CLASSES,
    EVENT_CATALYST_SHADOW_CONTRACT,
    EVENT_TYPES,
    CatalystEntry,
    CatalystShadowBatch,
    CatalystShadowObservation,
    DailyBar,
    EventCatalystShadowError,
    build_catalyst_shadow_batch,
)


AS_OF = datetime.fromisoformat("2026-08-14T18:00:00+08:00")
SYMBOL = "600519.SH"


def _bars(start: date, count: int, *, start_close: float = 100.0, step: float = 0.0):
    """Weekday-only synthetic bars with a constant daily additive step."""
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
        "event_id": "evt-1",
        "event_type": "policy_meeting",
        "scheduled_date": date(2026, 8, 5),
        "date_confidence": "hard_date",
        "impact_direction": "positive",
        "source_ref": "fixture-calendar-v1",
        "symbol": SYMBOL,
    }
    payload.update(overrides)
    return CatalystEntry(**payload)


class TestCatalystEntryContract:
    def test_accepts_valid_entry(self):
        entry = _entry()
        assert entry.symbol == SYMBOL
        assert entry.event_type in EVENT_TYPES

    def test_accepts_chinext_symbol_in_research_scope(self):
        # Research scope covers ChiNext; execution scope stays mainboard-only.
        assert _entry(symbol="300750.SZ").symbol == "300750.SZ"

    def test_accepts_star_symbol_in_research_scope(self):
        assert _entry(symbol="688981.SH").symbol == "688981.SH"

    def test_rejects_beijing_symbol_outside_research_scope(self):
        with pytest.raises(EventCatalystShadowError) as excinfo:
            _entry(symbol="430047.BJ")
        assert (
            excinfo.value.reason_code
            == "event_catalyst_symbol_outside_research_scope"
        )

    def test_rejects_b_share_symbol_outside_research_scope(self):
        with pytest.raises(EventCatalystShadowError) as excinfo:
            _entry(symbol="200002.SZ")
        assert (
            excinfo.value.reason_code
            == "event_catalyst_symbol_outside_research_scope"
        )

    def test_rejects_unknown_event_type(self):
        with pytest.raises(EventCatalystShadowError) as excinfo:
            _entry(event_type="rumor")
        assert excinfo.value.reason_code == "event_catalyst_event_type_invalid"

    def test_rejects_unknown_date_confidence(self):
        with pytest.raises(EventCatalystShadowError) as excinfo:
            _entry(date_confidence="guessed")
        assert (
            excinfo.value.reason_code
            == "event_catalyst_date_confidence_invalid"
        )

    def test_market_wide_event_needs_entity(self):
        with pytest.raises(EventCatalystShadowError) as excinfo:
            _entry(symbol=None, entity=None)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_entity_or_symbol_required"
        )

    def test_market_wide_event_with_entity_ok(self):
        entry = _entry(symbol=None, entity="CN-MACRO")
        assert entry.symbol is None and entry.entity == "CN-MACRO"


def _sessions(start: date, count: int) -> list[date]:
    sessions = []
    current = start
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions


EVENT_DATE = date(2026, 8, 5)


def _ramped_bars(direction: float) -> list[DailyBar]:
    # 40 sessions ending after 2026-08-14; ramp of ``direction`` per session
    # for the 11 sessions up to and including the event date, flat otherwise.
    sessions = _sessions(date(2026, 6, 22), 40)
    event_index = sessions.index(EVENT_DATE)
    bars = []
    close = 100.0
    for index, session in enumerate(sessions):
        if event_index - 11 < index <= event_index:
            close += direction
        bars.append(DailyBar(trade_date=session, close=round(close, 4)))
    return bars


def _front_run_bars() -> list[DailyBar]:
    return _ramped_bars(1.2)


class TestShadowBatch:
    def test_moderate_front_run_maps_to_realize_hypothesis(self):
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: _ramped_bars(0.6)},  # +6% into the event
            as_of=AS_OF,
        )
        observation = batch.observations[0]
        assert observation.observation_status == "observed"
        assert observation.pre_return == pytest.approx(0.06, abs=0.01)
        assert observation.anticipation_class == "front_run"
        assert observation.anticipation_intensity == "moderate"
        assert observation.positioning_hypothesis == "realize_on_event"

    def test_extreme_front_run_maps_to_reduce_on_confirmation(self):
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: _front_run_bars()},  # +12% into the event
            as_of=AS_OF,
        )
        assert isinstance(batch, CatalystShadowBatch)
        assert batch.contract == EVENT_CATALYST_SHADOW_CONTRACT
        observation = batch.observations[0]
        assert isinstance(observation, CatalystShadowObservation)
        assert observation.observation_status == "observed"
        assert observation.pre_return == pytest.approx(0.12, abs=0.01)
        assert observation.anticipation_class == "front_run"
        assert observation.anticipation_intensity == "extreme"
        assert observation.positioning_hypothesis == (
            "reduce_on_event_confirmation"
        )
        assert observation.post_label_state == "labeled"
        assert observation.post_return == pytest.approx(0.0, abs=0.001)

    def test_sell_off_classified_with_hold_through_hypothesis(self):
        batch = build_catalyst_shadow_batch(
            [_entry()], {SYMBOL: _ramped_bars(-1.2)}, as_of=AS_OF
        )
        observation = batch.observations[0]
        assert observation.anticipation_class == "sell_off"
        assert observation.positioning_hypothesis == "hold_through_event"

    def test_quiet_market_gives_no_signal(self):
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: _bars(date(2026, 6, 22), 40)},
            as_of=AS_OF,
        )
        observation = batch.observations[0]
        assert observation.anticipation_class == "quiet"
        assert observation.positioning_hypothesis == "no_signal"

    def test_pending_label_when_post_window_not_observable(self):
        as_of = datetime.fromisoformat("2026-08-05T18:00:00+08:00")
        bars = [bar for bar in _front_run_bars() if bar.trade_date <= as_of.date()]
        batch = build_catalyst_shadow_batch(
            [_entry()], {SYMBOL: bars}, as_of=as_of
        )
        observation = batch.observations[0]
        assert observation.observation_status == "observed"
        assert observation.post_label_state == "pending"
        assert observation.post_return is None

    def test_missing_bars_give_insufficient_history(self):
        batch = build_catalyst_shadow_batch([_entry()], {}, as_of=AS_OF)
        observation = batch.observations[0]
        assert observation.observation_status == "insufficient_history"
        assert observation.pre_return is None
        assert observation.anticipation_class is None

    def test_market_wide_event_has_no_instrument_observation(self):
        batch = build_catalyst_shadow_batch(
            [_entry(symbol=None, entity="CN-MACRO")], {}, as_of=AS_OF
        )
        assert batch.observations[0].observation_status == "insufficient_history"

    def test_pit_violation_fails_closed(self):
        bars = _bars(date(2026, 6, 22), 40)
        early_as_of = datetime.fromisoformat("2026-07-01T18:00:00+08:00")
        with pytest.raises(EventCatalystShadowError) as excinfo:
            build_catalyst_shadow_batch(
                [_entry()], {SYMBOL: bars}, as_of=early_as_of
            )
        assert excinfo.value.reason_code == (
            "event_catalyst_pit_violation_future_bar"
        )

    def test_duplicate_event_id_fails_closed(self):
        with pytest.raises(EventCatalystShadowError) as excinfo:
            build_catalyst_shadow_batch(
                [_entry(), _entry()], {}, as_of=AS_OF
            )
        assert excinfo.value.reason_code == "event_catalyst_event_id_duplicate"

    def test_naive_as_of_fails_closed(self):
        with pytest.raises(EventCatalystShadowError):
            build_catalyst_shadow_batch(
                [_entry()], {}, as_of=datetime(2026, 8, 14, 18, 0, 0)
            )

    def test_deterministic_receipts(self):
        first = build_catalyst_shadow_batch(
            [_entry()], {SYMBOL: _front_run_bars()}, as_of=AS_OF
        )
        second = build_catalyst_shadow_batch(
            [_entry()], {SYMBOL: _front_run_bars()}, as_of=AS_OF
        )
        assert first.batch_receipt_sha256 == second.batch_receipt_sha256
        assert (
            first.observations[0].observation_sha256
            == second.observations[0].observation_sha256
        )

    def test_receipt_changes_with_inputs(self):
        first = build_catalyst_shadow_batch(
            [_entry()], {SYMBOL: _front_run_bars()}, as_of=AS_OF
        )
        second = build_catalyst_shadow_batch(
            [_entry(event_id="evt-2")],
            {SYMBOL: _front_run_bars()},
            as_of=AS_OF,
        )
        assert first.batch_receipt_sha256 != second.batch_receipt_sha256


class TestAuthorityLocks:
    def test_batch_authority_flags_locked(self):
        batch = build_catalyst_shadow_batch([_entry()], {}, as_of=AS_OF)
        assert batch.shadow_only is True
        assert batch.candidate_eligible is False
        assert batch.execution_eligible is False
        assert batch.training_eligible is False
        assert batch.promotion_eligible is False
        assert batch.real_trading_enabled is False
        with pytest.raises(EventCatalystShadowError) as excinfo:
            CatalystShadowBatch(
                contract=batch.contract,
                as_of=batch.as_of,
                pre_window_sessions=batch.pre_window_sessions,
                post_window_sessions=batch.post_window_sessions,
                observations=batch.observations,
                batch_receipt_sha256=batch.batch_receipt_sha256,
                candidate_eligible=True,
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_batch_authority_invalid"
        )

    def test_observation_authority_flags_locked(self):
        batch = build_catalyst_shadow_batch(
            [_entry()], {SYMBOL: _front_run_bars()}, as_of=AS_OF
        )
        observation = batch.observations[0]
        assert observation.shadow_only is True
        assert observation.calibrated_probability is None
        with pytest.raises(EventCatalystShadowError) as excinfo:
            CatalystShadowObservation(
                event_id=observation.event_id,
                event_type=observation.event_type,
                date_confidence=observation.date_confidence,
                impact_direction=observation.impact_direction,
                scheduled_date=observation.scheduled_date,
                symbol=observation.symbol,
                entity=observation.entity,
                as_of=observation.as_of,
                pre_window_sessions=observation.pre_window_sessions,
                post_window_sessions=observation.post_window_sessions,
                pre_return=observation.pre_return,
                anticipation_class=observation.anticipation_class,
                anticipation_intensity=observation.anticipation_intensity,
                positioning_hypothesis=observation.positioning_hypothesis,
                post_return=observation.post_return,
                post_label_state=observation.post_label_state,
                observation_status=observation.observation_status,
                input_receipt_sha256=observation.input_receipt_sha256,
                observation_sha256=observation.observation_sha256,
                order_authority=True,
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_obs_authority_invalid"
        )

    def test_hypothesis_mismatch_fails_closed(self):
        batch = build_catalyst_shadow_batch(
            [_entry()], {SYMBOL: _front_run_bars()}, as_of=AS_OF
        )
        observation = batch.observations[0]
        assert observation.anticipation_class in ANTICIPATION_CLASSES
        with pytest.raises(EventCatalystShadowError) as excinfo:
            CatalystShadowObservation(
                event_id=observation.event_id,
                event_type=observation.event_type,
                date_confidence=observation.date_confidence,
                impact_direction=observation.impact_direction,
                scheduled_date=observation.scheduled_date,
                symbol=observation.symbol,
                entity=observation.entity,
                as_of=observation.as_of,
                pre_window_sessions=observation.pre_window_sessions,
                post_window_sessions=observation.post_window_sessions,
                pre_return=observation.pre_return,
                anticipation_class=observation.anticipation_class,
                anticipation_intensity=observation.anticipation_intensity,
                positioning_hypothesis="no_signal",
                post_return=observation.post_return,
                post_label_state=observation.post_label_state,
                observation_status=observation.observation_status,
                input_receipt_sha256=observation.input_receipt_sha256,
                observation_sha256=observation.observation_sha256,
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_obs_hypothesis_mismatch"
        )


class TestIntradayRallyLabels:
    def _bars_with_highs(self, *, margin: float = 0.3):
        return [
            DailyBar(
                trade_date=bar.trade_date,
                close=bar.close,
                high=round(bar.close + margin, 4),
            )
            for bar in _bars(date(2026, 7, 15), 21, step=0.5)
        ]

    def test_labeled_observation_computes_rally_labels(self):
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: self._bars_with_highs()},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (observation,) = batch.observations
        assert observation.post_label_state == "labeled"
        # Window = event day + 5 sessions; highs rise with closes, so the
        # best high is the last bar (offset 5).
        assert observation.post_optimal_exit_offset == 5
        event_close = 100.0 + 0.5 * 15
        best_high = 100.0 + 0.5 * 20 + 0.3
        assert observation.post_max_intraday_premium == pytest.approx(
            best_high / event_close - 1.0
        )
        pre_event_close = 100.0 + 0.5 * 14
        assert observation.post_optimal_exit_return == pytest.approx(
            best_high / pre_event_close - 1.0
        )

    def test_event_day_rally_is_offset_zero(self):
        bars = self._bars_with_highs(margin=0.1)
        # Event day spikes intraday, later sessions do not exceed it.
        spiked = DailyBar(
            trade_date=bars[15].trade_date,
            close=bars[15].close,
            high=bars[15].close + 5.0,
        )
        bars = list(bars[:15]) + [spiked] + list(bars[16:])
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: bars},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (observation,) = batch.observations
        assert observation.post_optimal_exit_offset == 0
        assert observation.post_max_intraday_premium == pytest.approx(
            5.0 / bars[15].close, abs=1e-9
        )

    def test_close_only_bars_leave_intraday_labels_none(self):
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: _bars(date(2026, 7, 15), 21, step=0.5)},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (observation,) = batch.observations
        assert observation.post_label_state == "labeled"
        assert observation.post_max_intraday_premium is None
        assert observation.post_optimal_exit_offset is None
        assert observation.post_optimal_exit_return is None

    def test_partial_highs_in_window_leave_labels_none(self):
        bars = self._bars_with_highs()
        # One post-window bar missing its high: no partial rally labels.
        bars[18] = DailyBar(trade_date=bars[18].trade_date, close=bars[18].close)
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: bars},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (observation,) = batch.observations
        assert observation.post_label_state == "labeled"
        assert observation.post_max_intraday_premium is None

    def test_high_below_close_fails_closed(self):
        with pytest.raises(EventCatalystShadowError) as excinfo:
            DailyBar(trade_date=date(2026, 8, 5), close=100.0, high=99.9)
        assert excinfo.value.reason_code == "event_catalyst_bar_high_invalid"

    def test_partial_intraday_payload_rejected(self):
        batch = build_catalyst_shadow_batch(
            [_entry()],
            {SYMBOL: self._bars_with_highs()},
            as_of=AS_OF,
            pre_window_sessions=10,
            post_window_sessions=5,
        )
        (observation,) = batch.observations
        from dataclasses import replace

        with pytest.raises(EventCatalystShadowError) as excinfo:
            replace(observation, post_optimal_exit_offset=None)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_obs_intraday_payload_mismatch"
        )
