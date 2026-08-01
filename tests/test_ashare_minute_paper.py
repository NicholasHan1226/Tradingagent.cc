from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from Ashare.minute_data import MinuteBarEvidence, MinuteEvidenceUse
from Ashare.minute_paper import (
    MinuteDecisionOutcome,
    MinuteExecutionPair,
    MinuteFixturePaperBook,
    MinutePaperContractError,
    MinuteSmallAccountConstraints,
    build_minute_paper_market_snapshot,
    minute_action_allowed_during_data_failure,
    minute_decision_record,
)
from shared.review.decision_ledger import (
    ExposureDisposition,
    InMemoryDecisionLedger,
)


def _sha(character: str) -> str:
    return character * 64


def _bar(
    end: str,
    *,
    symbol: str = "600000.SH",
    open_price: float = 10.0,
    high: float = 10.2,
    low: float = 9.9,
    close: float = 10.1,
    volume: int = 100_000,
    observed_delay_seconds: int = 20,
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
) -> MinuteBarEvidence:
    bar_end = datetime.fromisoformat(end)
    return MinuteBarEvidence(
        symbol=symbol,
        bar_start=bar_end - timedelta(minutes=5),
        bar_end=bar_end,
        open_cny=open_price,
        high_cny=high,
        low_cny=low,
        close_cny=close,
        volume_shares=volume,
        amount_cny=volume * 10.1,
        previous_close_cny=10.0,
        suspended=False,
        market_session=(
            "continuous_auction_am" if bar_end.hour < 12 else "continuous_auction_pm"
        ),
        dataset_id="fixture.cn.equity.five_minute",
        catalog_version="fixture-minute-catalog-v1",
        receipt_id=f"receipt-{end}",
        data_through=bar_end,
        observed_at=bar_end + timedelta(seconds=observed_delay_seconds),
        available_at=bar_end + timedelta(seconds=observed_delay_seconds),
        decision_time=bar_end + timedelta(seconds=observed_delay_seconds + 5),
        source_lineage_sha256=_sha("a"),
        envelope_proof_sha256=_sha("b"),
        source_row_sha256=_sha("c"),
        reference_evidence_sha256=_sha("d"),
        evidence_use=evidence_use,
    )


def _calendar_receipt() -> dict:
    return {
        "authority_tier": "non_production_fixture",
        "production_eligible": False,
        "calendar": {"fixture": "calendar"},
        "verification": {"fixture": "verification"},
    }


def test_execution_pair_requires_first_reachable_bar_and_handles_lunch_gap() -> None:
    pair = MinuteExecutionPair(
        _bar("2026-07-27T09:35:00+08:00"),
        _bar("2026-07-27T09:45:00+08:00"),
    )
    assert pair.execution_bar.bar_start > pair.decision_bar.decision_time

    lunch_pair = MinuteExecutionPair(
        _bar("2026-07-27T11:30:00+08:00"),
        _bar("2026-07-27T13:05:00+08:00"),
    )
    assert lunch_pair.execution_bar.market_session == "continuous_auction_pm"

    with pytest.raises(MinutePaperContractError, match="first_reachable_bar"):
        MinuteExecutionPair(
            _bar("2026-07-27T09:35:00+08:00"),
            _bar("2026-07-27T09:40:00+08:00"),
        )
    with pytest.raises(MinutePaperContractError, match="first_reachable_bar"):
        MinuteExecutionPair(
            _bar("2026-07-27T09:35:00+08:00"),
            _bar("2026-07-27T09:35:00+08:00"),
        )


def test_small_account_constraints_bind_canonical_50000_policy() -> None:
    constraints = MinuteSmallAccountConstraints.canonical()
    assert constraints.policy.initial_equity_cny == 50_000
    assert constraints.single_name_cap_cny == 7_500
    assert constraints.canary_monitor_count == 10
    assert constraints.initial_monitor_count == 500
    assert constraints.expanded_monitor_count == 6_000
    assert constraints.operating_max_positions == 6
    constraints.validate_buy_quantity(price_cny=20.0, quantity=300)
    with pytest.raises(MinutePaperContractError, match="round_lot"):
        constraints.validate_buy_quantity(price_cny=20.0, quantity=150)
    with pytest.raises(MinutePaperContractError, match="single_name"):
        constraints.validate_buy_quantity(price_cny=20.0, quantity=400)
    assert constraints.trade_required(current_notional=0, target_notional=2_000) is True
    assert (
        constraints.trade_required(current_notional=1_500, target_notional=2_000)
        is False
    )


def test_next_bar_open_plus_slippage_is_bounded_and_fixture_only() -> None:
    pair = MinuteExecutionPair(
        _bar("2026-07-27T09:35:00+08:00"),
        _bar("2026-07-27T09:45:00+08:00"),
    )
    snapshot = build_minute_paper_market_snapshot(
        order_id="ORDER-1",
        pair=pair,
        side="buy",
        session_calendar_receipt=_calendar_receipt(),
        session_calendar_receipt_sha256=_sha("d"),
        capital_authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage_id="minute-fixture-lineage",
    )
    assert snapshot.expected_fill_price_cny == 10.01
    assert snapshot.maximum_fill_quantity == 10_000
    assert snapshot.market_snapshot["real_trading_enabled"] is False
    assert snapshot.market_snapshot["retrospective_bar_fill_evidence"] is True
    assert snapshot.market_snapshot["modeled_fill_time"] == "2026-07-27T09:40:00+08:00"
    assert snapshot.market_snapshot["execution_latency_eligible"] is True

    snapshot.validate_receipt(
        {
            "status": "simulated_filled",
            "filled_quantity": 300,
            "fill_price": 10.01,
        }
    )
    snapshot.validate_receipt({"status": "not_filled", "filled_quantity": 0})
    with pytest.raises(MinutePaperContractError, match="outside_bar"):
        snapshot.validate_receipt(
            {
                "status": "simulated_filled",
                "filled_quantity": 300,
                "fill_price": 10.3,
            }
        )


def test_delayed_paper_fill_waits_for_a_bar_open_after_data_arrival() -> None:
    decision = _bar(
        "2026-07-27T11:00:00+08:00",
        observed_delay_seconds=300,
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )
    pair = MinuteExecutionPair(
        decision,
        _bar("2026-07-27T11:15:00+08:00"),
    )
    snapshot = build_minute_paper_market_snapshot(
        order_id="ORDER-DELAYED",
        pair=pair,
        side="buy",
        session_calendar_receipt=_calendar_receipt(),
        session_calendar_receipt_sha256=_sha("d"),
        capital_authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage_id="minute-delayed-fixture-lineage",
    )
    modeled_fill_time = datetime.fromisoformat(
        snapshot.market_snapshot["modeled_fill_time"]
    )
    assert modeled_fill_time == datetime.fromisoformat("2026-07-27T11:10:00+08:00")
    assert modeled_fill_time > decision.available_at
    assert snapshot.market_snapshot["decision_minute_evidence_use"] == "delayed_paper"
    assert snapshot.market_snapshot["decision_execution_latency_eligible"] is False
    assert (
        snapshot.market_snapshot["execution_minute_evidence_use"]
        == "low_latency_execution"
    )
    assert snapshot.market_snapshot["real_trading_enabled"] is False
    assert decision.execution_latency_eligible is False


def test_fill_is_blocked_by_bar_range_price_limit_and_capacity() -> None:
    decision = _bar("2026-07-27T09:35:00+08:00")
    with pytest.raises(MinutePaperContractError, match="outside_bar"):
        build_minute_paper_market_snapshot(
            order_id="ORDER-RANGE",
            pair=MinuteExecutionPair(
                decision,
                _bar(
                    "2026-07-27T09:45:00+08:00",
                    high=10.0,
                    low=9.9,
                    close=10.0,
                ),
            ),
            side="buy",
            session_calendar_receipt=_calendar_receipt(),
            session_calendar_receipt_sha256=_sha("d"),
            capital_authority_id="ashare-capital-v1",
            authority_generation=1,
            execution_lineage_id="minute-fixture-lineage",
        )
    with pytest.raises(MinutePaperContractError, match="capacity"):
        build_minute_paper_market_snapshot(
            order_id="ORDER-CAPACITY",
            pair=MinuteExecutionPair(
                decision,
                _bar("2026-07-27T09:45:00+08:00", volume=999),
            ),
            side="buy",
            session_calendar_receipt=_calendar_receipt(),
            session_calendar_receipt_sha256=_sha("d"),
            capital_authority_id="ashare-capital-v1",
            authority_generation=1,
            execution_lineage_id="minute-fixture-lineage",
        )


@pytest.mark.parametrize(
    ("outcome", "reason", "disposition"),
    [
        (
            MinuteDecisionOutcome.PAPER_NOT_FILLED,
            "limit_up_not_fillable",
            ExposureDisposition.PAPER_NOT_FILLED,
        ),
        (
            MinuteDecisionOutcome.DATA_REJECTED,
            "minute_metadata_not_fresh",
            ExposureDisposition.REJECTED,
        ),
        (
            MinuteDecisionOutcome.MODEL_REJECTED,
            "model_abstained",
            ExposureDisposition.REJECTED,
        ),
        (
            MinuteDecisionOutcome.HUMAN_REJECTED,
            "human_rejected",
            ExposureDisposition.REJECTED,
        ),
        (
            MinuteDecisionOutcome.INSUFFICIENT_CAPITAL,
            "insufficient_cash",
            ExposureDisposition.REJECTED,
        ),
        (
            MinuteDecisionOutcome.RANKED_NOT_TRADED,
            None,
            ExposureDisposition.SHADOW_ONLY,
        ),
    ],
)
def test_all_nonfilled_and_shadow_outcomes_enter_existing_decision_ledger(
    outcome: MinuteDecisionOutcome,
    reason: str | None,
    disposition: ExposureDisposition,
) -> None:
    record = minute_decision_record(
        decision_id=f"DECISION-{outcome.value}",
        decision_cluster_id="CLUSTER-1",
        decision_time=datetime.fromisoformat("2026-07-27T09:35:25+08:00"),
        symbol="600000.SH",
        model_id="minute-baseline",
        model_version="v1",
        manifest_sha256=_sha("e"),
        action="buy",
        outcome=outcome,
        requested_notional_cny=2_000,
        reason_code=reason,
    )
    ledger = InMemoryDecisionLedger()
    assert ledger.append(record) is True
    assert ledger.append(record) is False
    assert ledger.records()[0].disposition is disposition
    assert ledger.records()[0].real_trading_enabled is False


def test_filled_outcome_requires_and_records_simulated_fill() -> None:
    record = minute_decision_record(
        decision_id="DECISION-FILL",
        decision_cluster_id="CLUSTER-1",
        decision_time=datetime.fromisoformat("2026-07-27T09:35:25+08:00"),
        symbol="600000.SH",
        model_id="minute-baseline",
        model_version="v1",
        manifest_sha256=_sha("f"),
        action="buy",
        outcome=MinuteDecisionOutcome.PAPER_FILLED,
        requested_notional_cny=3_003,
        filled_quantity=300,
        filled_notional_cny=3_003,
        actual_cost_cny=5.03,
        simulated_fill_id="SIM-FILL-1",
    )
    assert record.disposition is ExposureDisposition.PAPER_FILLED
    assert record.broker_order_id is None
    assert record.live_transition_authorized is False


def _pair_for_day(day: str, *, volume: int = 100_000, open_price: float = 10.0):
    return MinuteExecutionPair(
        _bar(f"{day}T09:35:00+08:00", open_price=open_price, volume=volume),
        _bar(f"{day}T09:45:00+08:00", open_price=open_price, volume=volume),
    )


def test_fixture_book_partial_fill_cash_position_reconcile_and_restart_idempotency() -> (
    None
):
    book = MinuteFixturePaperBook()
    pair = _pair_for_day("2026-07-27", volume=2_000)
    receipt = book.execute(
        order_id="MINUTE-BUY-1",
        pair=pair,
        side="buy",
        requested_quantity=300,
    )

    assert receipt.status == "partial"
    assert receipt.filled_quantity == 200
    assert receipt.residual_quantity == 100
    assert receipt.fill_price_cny == 10.01
    assert receipt.fee_cny > 5
    assert book.positions["600000.SH"].quantity == 200
    assert book.cash_cny < 48_000
    reconcile = book.reconcile(marks={"600000.SH": 10.01})
    assert reconcile["reconciled"] is True
    assert reconcile["real_trading_enabled"] is False
    assert reconcile["position_count"] == 1

    restarted = MinuteFixturePaperBook.restore(book.export_state())
    replay = restarted.execute(
        order_id="MINUTE-BUY-1",
        pair=pair,
        side="buy",
        requested_quantity=300,
    )
    assert replay == receipt
    assert restarted.cash_cny == book.cash_cny
    assert restarted.positions == book.positions
    with pytest.raises(MinutePaperContractError, match="idempotency_conflict"):
        restarted.execute(
            order_id="MINUTE-BUY-1",
            pair=pair,
            side="buy",
            requested_quantity=400,
        )


def test_fixture_book_enforces_t1_then_sells_and_conserves_equity() -> None:
    book = MinuteFixturePaperBook()
    buy = book.execute(
        order_id="BUY-DAY-1",
        pair=_pair_for_day("2026-07-27"),
        side="buy",
        requested_quantity=300,
    )
    assert buy.status == "filled"

    same_day_sell = book.execute(
        order_id="SELL-SAME-DAY",
        pair=MinuteExecutionPair(
            _bar("2026-07-27T10:00:00+08:00"),
            _bar("2026-07-27T10:10:00+08:00"),
        ),
        side="sell",
        requested_quantity=100,
    )
    assert same_day_sell.status == "rejected"
    assert same_day_sell.reason_code == "minute_t1_sellable_quantity_insufficient"
    assert book.positions["600000.SH"].quantity == 300

    next_day_sell = book.execute(
        order_id="SELL-DAY-2",
        pair=_pair_for_day("2026-07-28", open_price=10.2),
        side="sell",
        requested_quantity=100,
    )
    assert next_day_sell.status == "filled"
    assert next_day_sell.fill_price_cny == 10.19
    assert book.positions["600000.SH"].quantity == 200
    reconciliation = book.reconcile(marks={"600000.SH": 10.2})
    assert reconciliation["equity_cny"] == pytest.approx(
        reconciliation["conservation_expected_equity_cny"], abs=0.02
    )


def test_fixture_book_records_nonfills_without_mutating_cash_or_positions() -> None:
    book = MinuteFixturePaperBook()
    starting_cash = book.cash_cny
    limit_pair = MinuteExecutionPair(
        _bar(
            "2026-07-27T09:35:00+08:00",
            open_price=11.0,
            high=11.1,
            low=10.9,
            close=11.0,
        ),
        _bar(
            "2026-07-27T09:45:00+08:00",
            open_price=11.0,
            high=11.1,
            low=10.9,
            close=11.0,
        ),
    )
    receipt = book.execute(
        order_id="BUY-LIMIT-UP",
        pair=limit_pair,
        side="buy",
        requested_quantity=100,
    )
    assert receipt.status == "not_filled"
    assert receipt.reason_code == "minute_price_limit_not_fillable"
    assert receipt.filled_quantity == 0
    assert book.cash_cny == starting_cash
    assert book.positions == {}
    assert book.reconcile(marks={})["equity_cny"] == 50_000


def test_fixture_book_state_tamper_and_missing_marks_fail_closed() -> None:
    book = MinuteFixturePaperBook()
    book.execute(
        order_id="BUY-FOR-TAMPER",
        pair=_pair_for_day("2026-07-27"),
        side="buy",
        requested_quantity=200,
    )
    with pytest.raises(MinutePaperContractError, match="marks_incomplete"):
        book.reconcile(marks={})

    state = book.export_state()
    state["cash_cny"] = 50_000
    with pytest.raises(MinutePaperContractError, match="integrity"):
        MinuteFixturePaperBook.restore(state)


def test_cancel_receipt_is_idempotent_and_never_mutates_capital() -> None:
    book = MinuteFixturePaperBook()
    pair = _pair_for_day("2026-07-27")
    first = book.cancel(
        order_id="CANCEL-1",
        pair=pair,
        side="buy",
        requested_quantity=200,
    )
    second = book.cancel(
        order_id="CANCEL-1",
        pair=pair,
        side="buy",
        requested_quantity=200,
    )
    assert first == second
    assert first.status == "cancelled"
    assert first.filled_quantity == 0
    assert book.cash_cny == 50_000
    assert book.positions == {}


def test_data_failure_can_only_hold_or_contract_risk() -> None:
    assert minute_action_allowed_during_data_failure("hold") is True
    assert minute_action_allowed_during_data_failure("reduce") is True
    assert minute_action_allowed_during_data_failure("exit") is True
    assert minute_action_allowed_during_data_failure("open") is False
    assert minute_action_allowed_during_data_failure("increase") is False


def test_fixture_book_applies_no_trade_band_to_new_risk_not_reductions() -> None:
    book = MinuteFixturePaperBook()
    receipt = book.execute(
        order_id="BUY-TOO-SMALL",
        pair=_pair_for_day("2026-07-27"),
        side="buy",
        requested_quantity=100,
    )
    assert receipt.status == "rejected"
    assert receipt.reason_code == "minute_no_trade_band"
    assert book.cash_cny == 50_000
