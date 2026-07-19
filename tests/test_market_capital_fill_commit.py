from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from shared.capital.market_ledger import (
    CN_FUTURES_CONTRACT_SPEC_VERSION,
    MarketCapitalAshareSellCommitRequest,
    MarketCapitalFillCommitRequest,
    MarketCapitalPositionCloseCommitRequest,
    MarketCapitalLedger,
    MarketCapitalLedgerError,
    MarketCapitalReservationRequest,
    OpeningStateManifest,
    ReconcileManifest,
    RECONCILE_SOURCE_SCHEMA_VERSION,
    commit_market_capital_fill,
    commit_market_capital_ashare_sell,
    commit_market_capital_position_close,
    cn_futures_contract_spec_sha256,
)
from shared.capital.market_policy import (
    PINNED_CUTOVER_DECISION_ID,
    PINNED_SOURCE_THREAD_ID,
    MarketPolicy,
)


TRADE_DATE = "20260712"
LINEAGE_ID = "fill-commit-lineage-001"
PIT = "2026-07-12T09:30:00+08:00"
FILL_TIME = "2026-07-12T09:31:00+08:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _init_ledger(tmp_path: Path, market: str) -> MarketCapitalLedger:
    archive = tmp_path / "legacy_archive"
    archive.mkdir(parents=True)
    old_events = tmp_path / "legacy_events.jsonl"
    old_events.write_text(json.dumps({"event_id": "OLD-1"}) + "\n", "utf-8")
    freeze = {
        "events_path": str(old_events),
        "sha256": hashlib.sha256(old_events.read_bytes()).hexdigest(),
        "last_event_id": "OLD-1",
        "row_count": 1,
        "frozen_at": "2026-07-12T00:00:00+08:00",
        "archive_path": str(archive),
        "imported": False,
    }
    policy = MarketPolicy.load(market)
    ledger = MarketCapitalLedger(tmp_path / market, policy=policy)
    ledger.initialize(
        OpeningStateManifest(
            market=market,
            authority_id=policy.capital_authority_id,
            cutover_decision_id=PINNED_CUTOVER_DECISION_ID,
            mode="fresh_start",
            as_of=TRADE_DATE,
            cash_balance_cny=50_000.0,
            opening_equity_cny=50_000.0,
            active_reservations_cny=0.0,
            consecutive_losses=0,
            inherited_high_water_equity_cny=0.0,
            positions_by_risk_unit={},
            position_margin_by_risk_unit={},
            frozen_order_cash_cny=0.0,
            realized_pnl_cny=0.0,
            unrealized_pnl_cny=0.0,
            source="test",
            source_sha256=_sha("opening"),
            execution_lineage_id=LINEAGE_ID,
            real=False,
        ),
        cutover_manifest={
            "cutover_decision_id": PINNED_CUTOVER_DECISION_ID,
            "source_thread_id": PINNED_SOURCE_THREAD_ID,
            "cutover_state": "fresh_start_approved",
            "authority_generation": 1,
        },
        legacy_freeze_manifest=freeze,
    )
    ledger.mtm_reconcile(
        _canonical_reconcile_manifest(
            ledger,
            ReconcileManifest(
                market=market,
                authority_id=policy.capital_authority_id,
                as_of=TRADE_DATE,
                cash_balance_cny=50_000.0,
                positions_market_value={},
                unrealized_pnl_cny=0.0,
                position_margin_by_risk_unit={},
                active_reservations_cny=0.0,
                frozen_order_cash_cny=0.0,
                frozen_order_margin_cny=0.0,
                authority_generation=1,
                execution_lineage_id=LINEAGE_ID,
                pit_timestamp=PIT,
                source="test",
                source_sha256=_sha("opening-reconcile"),
            ),
        )
    )
    return ledger


def _reserve_ashare(
    ledger: MarketCapitalLedger,
    *,
    reference_id: str = "ORDER-1",
    cash: float = 1_005.0,
    exposure: float = 1_000.0,
):
    return ledger.reserve(
        MarketCapitalReservationRequest(
            market="ashare",
            reference_id=reference_id,
            risk_unit_key="000001.XSHE",
            worst_case_amount_cny=cash,
            authority_id="ashare-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of=PIT,
            lineage_sha256=_sha("lineage"),
            authority_generation=1,
            execution_lineage_id=LINEAGE_ID,
            worst_case_cash_cny=cash,
            worst_case_exposure_cny=exposure,
        )
    )


def _head(ledger: MarketCapitalLedger) -> dict:
    return ledger._load_events_unlocked()[-1]


def _canonical_reconcile_manifest(
    ledger: MarketCapitalLedger,
    manifest: ReconcileManifest,
) -> ReconcileManifest:
    payload = {
        "schema_version": RECONCILE_SOURCE_SCHEMA_VERSION,
        "market": manifest.market,
        "trade_date": str(manifest.as_of).replace("-", ""),
        "pit_timestamp": manifest.pit_timestamp,
        "execution_lineage_id": manifest.execution_lineage_id,
        "cash_balance_cny": manifest.cash_balance_cny,
        "positions_market_value": manifest.positions_market_value,
        "unrealized_pnl_cny": manifest.unrealized_pnl_cny,
        "position_margin_by_risk_unit": manifest.position_margin_by_risk_unit,
        "active_reservations_cny": manifest.active_reservations_cny,
        "active_reservations": manifest.active_reservations,
        "frozen_order_cash_cny": manifest.frozen_order_cash_cny,
        "frozen_order_margin_cny": manifest.frozen_order_margin_cny,
        "positions_quantity_by_risk_unit": (
            manifest.positions_quantity_by_risk_unit or {}
        ),
        "positions_cost_basis_cny_by_risk_unit": (
            manifest.positions_cost_basis_cny_by_risk_unit or {}
        ),
        "positions_entry_fee_cny_by_risk_unit": (
            manifest.positions_entry_fee_cny_by_risk_unit or {}
        ),
        "position_entry_price_by_risk_unit": (
            manifest.position_entry_price_by_risk_unit or {}
        ),
        "position_side_by_risk_unit": manifest.position_side_by_risk_unit or {},
        "position_contract_multiplier_by_risk_unit": (
            manifest.position_contract_multiplier_by_risk_unit or {}
        ),
        "position_contract_spec_sha256_by_risk_unit": (
            manifest.position_contract_spec_sha256_by_risk_unit or {}
        ),
        "position_mark_price_by_risk_unit": (
            manifest.position_mark_price_by_risk_unit or {}
        ),
        "expected_ledger_event_id": manifest.expected_ledger_event_id,
        "expected_ledger_checksum": manifest.expected_ledger_checksum,
        "included_fill_commit_ids": list(manifest.included_fill_commit_ids),
        "real_trading_enabled": False,
    }
    path = ledger.root / f"reconcile-source-{len(ledger._load_events_unlocked())}.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return replace(
        manifest,
        source_sha256=sha,
        canonical_snapshot_path=str(path.resolve()),
        canonical_snapshot_sha256=sha,
    )


def _ashare_fill_request(
    ledger: MarketCapitalLedger,
    reservation,
    **overrides,
) -> MarketCapitalFillCommitRequest:
    head = _head(ledger)
    defaults = dict(
        market="ashare",
        reference_id=f"MCAPFILL:1:{LINEAGE_ID}:{reservation.reservation_id}:FILL-1",
        reservation_id=reservation.reservation_id,
        reservation_event_id=reservation.event_id,
        reservation_reference_id="ORDER-1",
        risk_unit_key="000001.XSHE",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage_id=LINEAGE_ID,
        lineage_sha256=_sha("lineage"),
        order_id="ORDER-1",
        idempotency_key="ashare:sim:20260712:000001.XSHE:buy",
        execution_fill_id="FILL-1",
        fill_sequence=1,
        side="buy",
        status="filled",
        terminal=True,
        actual_filled_quantity=100,
        actual_fill_price=10.0,
        actual_cash_debit_cny=1_005.0,
        actual_exposure_cny=1_000.0,
        actual_margin_cny=0.0,
        actual_fee_cash_cny=5.0,
        filled_at=FILL_TIME,
        point_in_time_as_of=PIT,
        source="local_sim_trade",
        source_sha256=_sha("trade-source"),
        receipt_sha256=_sha("receipt"),
        local_trade_sha256=_sha("local-trade"),
        expected_ledger_event_id=str(head["event_id"]),
        expected_ledger_checksum=str(head["checksum"]),
    )
    defaults.update(overrides)
    if "execution_fill_id" in overrides and "reference_id" not in overrides:
        defaults["reference_id"] = (
            f"MCAPFILL:1:{LINEAGE_ID}:{reservation.reservation_id}:"
            f"{overrides['execution_fill_id']}"
        )
    return MarketCapitalFillCommitRequest(**defaults)


def _open_cn_position(
    ledger: MarketCapitalLedger,
    *,
    quantity: int = 2,
    margin: float = 9_000.0,
    fee: float = 20.0,
):
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="CN-ORDER-OPEN",
            risk_unit_key="IF2607",
            worst_case_amount_cny=margin,
            authority_id="cn-futures-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of=PIT,
            lineage_sha256=_sha("cn-lineage"),
            authority_generation=1,
            execution_lineage_id=LINEAGE_ID,
            worst_case_cash_cny=fee,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=margin,
        )
    )
    assert reservation.approved is True
    assert reservation.snapshot is not None
    request = MarketCapitalFillCommitRequest(
        market="cn_futures",
        reference_id=(
            f"MCAPFILL:1:{LINEAGE_ID}:{reservation.reservation_id}:CN-FILL-OPEN"
        ),
        reservation_id=reservation.reservation_id,
        reservation_event_id=reservation.event_id,
        reservation_reference_id="CN-ORDER-OPEN",
        risk_unit_key="IF2607",
        authority_id="cn-futures-capital-v1",
        authority_generation=1,
        execution_lineage_id=LINEAGE_ID,
        lineage_sha256=_sha("cn-lineage"),
        order_id="CN-ORDER-OPEN",
        idempotency_key="cn:sim:20260712:IF2607:open",
        execution_fill_id="CN-FILL-OPEN",
        fill_sequence=1,
        side="buy",
        status="filled",
        terminal=True,
        actual_filled_quantity=quantity,
        actual_fill_price=3_500.0,
        actual_cash_debit_cny=fee,
        actual_exposure_cny=0.0,
        actual_margin_cny=margin,
        actual_fee_cash_cny=fee,
        contract_multiplier=10.0,
        contract_margin_per_lot_cny=margin / quantity,
        contract_spec_version=CN_FUTURES_CONTRACT_SPEC_VERSION,
        contract_spec_sha256=cn_futures_contract_spec_sha256(
            "IF2607", 10.0, margin / quantity
        ),
        filled_at=FILL_TIME,
        point_in_time_as_of=PIT,
        source="cn_futures_sim_fill",
        source_sha256=_sha("cn-source-open"),
        receipt_sha256=_sha("cn-receipt-open"),
        local_trade_sha256=_sha("cn-local-open"),
        expected_ledger_event_id=reservation.snapshot.event_id,
        expected_ledger_checksum=reservation.snapshot.event_checksum,
    )
    opened = ledger.commit_fill(request)
    assert opened.committed is True
    assert opened.snapshot is not None
    return opened


def _cn_close_request(
    ledger: MarketCapitalLedger,
    **overrides,
) -> MarketCapitalPositionCloseCommitRequest:
    head = ledger.snapshot()
    defaults = dict(
        market="cn_futures",
        reference_id=f"MCAPCLOSE:1:{LINEAGE_ID}:IF2607:CN-FILL-CLOSE-1",
        risk_unit_key="IF2607",
        authority_id="cn-futures-capital-v1",
        authority_generation=1,
        execution_lineage_id=LINEAGE_ID,
        lineage_sha256=_sha("cn-lineage"),
        order_id="CN-ORDER-CLOSE-1",
        idempotency_key="cn:sim:20260712:IF2607:close:1",
        execution_fill_id="CN-FILL-CLOSE-1",
        fill_sequence=1,
        side="sell",
        status="partial",
        terminal=False,
        actual_closed_quantity=1,
        actual_fill_price=3_520.0,
        actual_margin_released_cny=4_500.0,
        actual_fee_cash_cny=5.0,
        actual_gross_realized_pnl_cny=200.0,
        filled_at="2026-07-12T09:32:00+08:00",
        point_in_time_as_of=PIT,
        source="cn_futures_sim_close",
        source_sha256=_sha("cn-source-close"),
        receipt_sha256=_sha("cn-receipt-close"),
        local_position_sha256=_sha("cn-position-after-close"),
        expected_ledger_event_id=head.event_id,
        expected_ledger_checksum=head.event_checksum,
    )
    defaults.update(overrides)
    if "execution_fill_id" in overrides and "reference_id" not in overrides:
        defaults["reference_id"] = (
            f"MCAPCLOSE:1:{LINEAGE_ID}:IF2607:{overrides['execution_fill_id']}"
        )
    return MarketCapitalPositionCloseCommitRequest(**defaults)


def _ashare_sell_request(
    ledger: MarketCapitalLedger,
    **overrides,
) -> MarketCapitalAshareSellCommitRequest:
    head = ledger.snapshot()
    defaults = dict(
        market="ashare",
        reference_id=f"MCAPSELL:1:{LINEAGE_ID}:000001.XSHE:A-FILL-SELL-1",
        risk_unit_key="000001.XSHE",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage_id=LINEAGE_ID,
        lineage_sha256=_sha("lineage"),
        order_id="A-ORDER-SELL-1",
        idempotency_key="ashare:sim:20260713:000001.XSHE:sell:1",
        execution_fill_id="A-FILL-SELL-1",
        fill_sequence=1,
        side="sell",
        status="filled",
        terminal=True,
        actual_closed_quantity=100,
        actual_fill_price=11.0,
        actual_gross_proceeds_cny=1_100.0,
        actual_fee_cash_cny=6.0,
        actual_net_cash_credit_cny=1_094.0,
        actual_gross_realized_pnl_cny=100.0,
        filled_at="2026-07-13T09:33:00+08:00",
        point_in_time_as_of=PIT,
        source="ashare_local_sim_sell",
        source_sha256=_sha("a-sell-source"),
        receipt_sha256=_sha("a-sell-receipt"),
        local_position_sha256=_sha("a-position-after-sell"),
        expected_ledger_event_id=head.event_id,
        expected_ledger_checksum=head.event_checksum,
    )
    defaults.update(overrides)
    if "execution_fill_id" in overrides and "reference_id" not in overrides:
        defaults["reference_id"] = (
            f"MCAPSELL:1:{LINEAGE_ID}:000001.XSHE:{overrides['execution_fill_id']}"
        )
    return MarketCapitalAshareSellCommitRequest(**defaults)


def test_ashare_terminal_fill_atomically_converts_reservation_to_position(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    assert reservation.approved is True
    before = ledger.snapshot()
    assert before.reserved_cash_cny == 1_005.0
    assert before.reserved_exposure_cny == 1_000.0
    assert before.available_to_reserve_cny == 44_000.0

    decision = ledger.commit_fill(_ashare_fill_request(ledger, reservation))

    assert decision.committed is True
    assert decision.status == "committed"
    after = decision.snapshot
    assert after is not None
    assert after.cash_balance_cny == 48_995.0
    assert after.positions_market_value_cny == 1_000.0
    assert after.reserved_cash_cny == 0.0
    assert after.reserved_exposure_cny == 0.0
    assert after.active_reservations_cny == 0.0
    assert after.available_to_reserve_cny == before.available_to_reserve_cny


def test_ashare_open_partial_preserves_both_headrooms_until_terminal(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(
        ledger,
        reference_id="ORDER-1",
        cash=2_010.0,
        exposure=2_000.0,
    )
    before = ledger.snapshot()

    first = ledger.commit_fill(
        _ashare_fill_request(
            ledger,
            reservation,
            status="partial",
            terminal=False,
        )
    )

    assert first.committed is True
    assert first.snapshot is not None
    assert first.snapshot.cash_balance_cny == 48_995.0
    assert first.snapshot.positions_market_value_cny == 1_000.0
    assert first.snapshot.reserved_cash_cny == 1_005.0
    assert first.snapshot.reserved_exposure_cny == 1_000.0
    assert first.snapshot.available_to_reserve_cny == before.available_to_reserve_cny

    second = ledger.commit_fill(
        _ashare_fill_request(
            ledger,
            reservation,
            execution_fill_id="FILL-2",
            fill_sequence=2,
            status="partial",
            terminal=True,
        )
    )

    assert second.committed is True
    assert second.snapshot is not None
    assert second.snapshot.cash_balance_cny == 47_990.0
    assert second.snapshot.positions_market_value_cny == 2_000.0
    assert second.snapshot.reserved_cash_cny == 0.0
    assert second.snapshot.reserved_exposure_cny == 0.0
    assert second.snapshot.available_to_reserve_cny == before.available_to_reserve_cny


def test_cn_open_fill_converts_margin_reservation_without_double_counting(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="CN-ORDER-1",
            risk_unit_key="IF2607",
            worst_case_amount_cny=4_500.0,
            authority_id="cn-futures-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of=PIT,
            lineage_sha256=_sha("cn-lineage"),
            authority_generation=1,
            execution_lineage_id=LINEAGE_ID,
            worst_case_cash_cny=10.0,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=4_500.0,
        )
    )
    assert reservation.approved is True
    before = ledger.snapshot()
    assert before.reserved_cash_cny == 10.0
    assert before.reserved_margin_cny == 4_500.0
    assert before.available_to_reserve_cny == 20_500.0
    head = _head(ledger)
    request = MarketCapitalFillCommitRequest(
        market="cn_futures",
        reference_id=(
            f"MCAPFILL:1:{LINEAGE_ID}:{reservation.reservation_id}:CN-FILL-1"
        ),
        reservation_id=reservation.reservation_id,
        reservation_event_id=reservation.event_id,
        reservation_reference_id="CN-ORDER-1",
        risk_unit_key="IF2607",
        authority_id="cn-futures-capital-v1",
        authority_generation=1,
        execution_lineage_id=LINEAGE_ID,
        lineage_sha256=_sha("cn-lineage"),
        order_id="CN-ORDER-1",
        idempotency_key="cn:sim:20260712:IF2607:buy",
        execution_fill_id="CN-FILL-1",
        fill_sequence=1,
        side="buy",
        status="filled",
        terminal=True,
        actual_filled_quantity=1,
        actual_fill_price=3_500.0,
        actual_cash_debit_cny=10.0,
        actual_exposure_cny=0.0,
        actual_margin_cny=4_500.0,
        actual_fee_cash_cny=10.0,
        contract_multiplier=10.0,
        contract_margin_per_lot_cny=4_500.0,
        contract_spec_version=CN_FUTURES_CONTRACT_SPEC_VERSION,
        contract_spec_sha256=cn_futures_contract_spec_sha256("IF2607", 10.0, 4_500.0),
        filled_at=FILL_TIME,
        point_in_time_as_of=PIT,
        source="cn_futures_sim_fill",
        source_sha256=_sha("cn-source"),
        receipt_sha256=_sha("cn-receipt"),
        local_trade_sha256=_sha("cn-local-trade"),
        expected_ledger_event_id=str(head["event_id"]),
        expected_ledger_checksum=str(head["checksum"]),
    )

    decision = commit_market_capital_fill(
        "cn_futures",
        request,
        root=ledger.root,
        policy=ledger.policy,
    )

    assert decision.committed is True
    assert decision.snapshot is not None
    assert decision.snapshot.cash_balance_cny == 49_990.0
    assert decision.snapshot.margin_used_cny == 4_500.0
    assert decision.snapshot.reserved_margin_cny == 0.0
    assert decision.snapshot.reserved_cash_cny == 0.0
    assert decision.snapshot.available_to_reserve_cny == before.available_to_reserve_cny


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"actual_filled_quantity": 50}, "ashare_lot_size_invalid"),
        (
            {"actual_exposure_cny": 999.0, "actual_cash_debit_cny": 1_004.0},
            "ashare_fill_notional_mismatch",
        ),
    ],
)
def test_ashare_fill_rejects_non_actual_or_non_lot_accounting(
    tmp_path: Path,
    overrides: dict[str, object],
    reason: str,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)

    decision = ledger.commit_fill(
        _ashare_fill_request(ledger, reservation, **overrides)
    )

    assert decision.committed is False
    assert decision.reason == reason
    assert ledger.snapshot().active_reservations_cny == 1_005.0


def test_reconcile_requires_exact_reservation_map_when_fill_overlay_is_pending(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(
        ledger,
        cash=2_010.0,
        exposure=2_000.0,
    )
    fill = ledger.commit_fill(
        _ashare_fill_request(
            ledger,
            reservation,
            status="partial",
            terminal=False,
        )
    )
    assert fill.committed is True
    head = _head(ledger)

    with pytest.raises(
        MarketCapitalLedgerError,
        match="reconcile_active_reservation_map_required",
    ):
        ledger.mtm_reconcile(
            _canonical_reconcile_manifest(
                ledger,
                ReconcileManifest(
                    market="ashare",
                    authority_id="ashare-capital-v1",
                    as_of="20260713",
                    cash_balance_cny=48_995.0,
                    positions_market_value={"000001.XSHE": 1_000.0},
                    unrealized_pnl_cny=-5.0,
                    position_margin_by_risk_unit={},
                    active_reservations_cny=1_005.0,
                    frozen_order_cash_cny=0.0,
                    frozen_order_margin_cny=0.0,
                    authority_generation=1,
                    execution_lineage_id=LINEAGE_ID,
                    pit_timestamp="2026-07-13T09:32:00+08:00",
                    source="local_sim_account",
                    source_sha256=_sha("reconcile-after-fill"),
                    expected_ledger_event_id=str(head["event_id"]),
                    expected_ledger_checksum=str(head["checksum"]),
                    included_fill_commit_ids=tuple(
                        fill.snapshot.unreconciled_fill_commit_ids
                        if fill.snapshot is not None
                        else ()
                    ),
                    positions_quantity_by_risk_unit={"000001.XSHE": 100},
                    positions_cost_basis_cny_by_risk_unit={"000001.XSHE": 1_000.0},
                    positions_entry_fee_cny_by_risk_unit={"000001.XSHE": 5.0},
                ),
            )
        )


def test_reconcile_folds_fill_overlay_with_exact_map_and_cas(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    fill = ledger.commit_fill(_ashare_fill_request(ledger, reservation))
    assert fill.snapshot is not None
    head = _head(ledger)

    result = ledger.mtm_reconcile(
        _canonical_reconcile_manifest(
            ledger,
            ReconcileManifest(
                market="ashare",
                authority_id="ashare-capital-v1",
                as_of=TRADE_DATE,
                cash_balance_cny=48_995.0,
                positions_market_value={"000001.XSHE": 1_000.0},
                unrealized_pnl_cny=-5.0,
                position_margin_by_risk_unit={},
                active_reservations_cny=0.0,
                frozen_order_cash_cny=0.0,
                frozen_order_margin_cny=0.0,
                authority_generation=1,
                execution_lineage_id=LINEAGE_ID,
                pit_timestamp="2026-07-12T09:32:00+08:00",
                source="local_sim_account",
                source_sha256=_sha("folded-account"),
                active_reservations=ledger.active_reservation_manifest(),
                expected_ledger_event_id=str(head["event_id"]),
                expected_ledger_checksum=str(head["checksum"]),
                included_fill_commit_ids=fill.snapshot.unreconciled_fill_commit_ids,
                positions_quantity_by_risk_unit={"000001.XSHE": 100},
                positions_cost_basis_cny_by_risk_unit={"000001.XSHE": 1_000.0},
                positions_entry_fee_cny_by_risk_unit={"000001.XSHE": 5.0},
            ),
        )
    )

    assert result["status"] == "reconciled"
    snapshot = ledger.snapshot()
    assert snapshot.cash_balance_cny == 48_995.0
    assert snapshot.positions_market_value_cny == 1_000.0
    assert snapshot.active_reservations_cny == 0.0
    assert snapshot.available_to_reserve_cny == 44_000.0
    assert snapshot.unreconciled_fill_commit_ids == ()


@pytest.mark.parametrize(
    "mutation",
    ["reservation_map", "fill_watermark", "ledger_head"],
)
def test_reconcile_rejects_exact_state_or_cas_mismatch_before_write(
    tmp_path: Path,
    mutation: str,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(
        ledger,
        cash=2_010.0,
        exposure=2_000.0,
    )
    fill = ledger.commit_fill(
        _ashare_fill_request(
            ledger,
            reservation,
            status="partial",
            terminal=False,
        )
    )
    assert fill.snapshot is not None
    head = _head(ledger)
    active_map = ledger.active_reservation_manifest()
    fill_ids = fill.snapshot.unreconciled_fill_commit_ids
    expected_event_id = str(head["event_id"])
    expected_checksum = str(head["checksum"])
    expected_error = ""
    if mutation == "reservation_map":
        active_map = {
            **active_map,
            reservation.reservation_id: {
                **active_map[reservation.reservation_id],
                "risk_unit_key": "000002.XSHE",
            },
        }
        expected_error = "active_reservation_map_mismatch"
    elif mutation == "fill_watermark":
        fill_ids = ()
        expected_error = "reconcile_fill_watermark_mismatch"
    else:
        expected_checksum = _sha("wrong-head")
        expected_error = "reconcile_ledger_head_cas_mismatch"
    event_count = len(ledger._load_events_unlocked())

    with pytest.raises(MarketCapitalLedgerError, match=expected_error):
        ledger.mtm_reconcile(
            _canonical_reconcile_manifest(
                ledger,
                ReconcileManifest(
                    market="ashare",
                    authority_id="ashare-capital-v1",
                    as_of="20260713",
                    cash_balance_cny=48_995.0,
                    positions_market_value={"000001.XSHE": 1_000.0},
                    unrealized_pnl_cny=-5.0,
                    position_margin_by_risk_unit={},
                    active_reservations_cny=1_005.0,
                    frozen_order_cash_cny=0.0,
                    frozen_order_margin_cny=0.0,
                    authority_generation=1,
                    execution_lineage_id=LINEAGE_ID,
                    pit_timestamp="2026-07-13T09:32:00+08:00",
                    source="local_sim_account",
                    source_sha256=_sha("mismatch-account"),
                    active_reservations=active_map,
                    expected_ledger_event_id=expected_event_id,
                    expected_ledger_checksum=expected_checksum,
                    included_fill_commit_ids=fill_ids,
                    positions_quantity_by_risk_unit={"000001.XSHE": 100},
                    positions_cost_basis_cny_by_risk_unit={"000001.XSHE": 1_000.0},
                    positions_entry_fee_cny_by_risk_unit={"000001.XSHE": 5.0},
                ),
            )
        )
    assert len(ledger._load_events_unlocked()) == event_count


def test_fill_commit_exact_retry_is_idempotent_but_payload_change_conflicts(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    request = _ashare_fill_request(ledger, reservation)

    first = ledger.commit_fill(request)
    event_count = len(ledger._load_events_unlocked())
    retry = ledger.commit_fill(request)

    assert retry.committed is True
    assert retry.idempotent is True
    assert retry.status == "idempotent"
    assert retry.event_id == first.event_id
    assert len(ledger._load_events_unlocked()) == event_count

    with pytest.raises(MarketCapitalLedgerError, match="fill_commit_conflict"):
        ledger.commit_fill(
            replace(
                request,
                actual_fill_price=9.99,
                actual_exposure_cny=999.0,
                actual_cash_debit_cny=1_004.0,
            )
        )
    assert len(ledger._load_events_unlocked()) == event_count


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"expected_ledger_checksum": _sha("stale")}, "ledger_head_cas_mismatch"),
        ({"filled_at": "2026-07-12T09:29:00+08:00"}, "fill_pit_regression"),
        ({"risk_unit_key": "000002.XSHE"}, "fill_risk_unit_key_mismatch"),
        ({"reservation_event_id": "WRONG-EVENT"}, "fill_reservation_event_id_mismatch"),
        (
            {"actual_cash_debit_cny": 1_006.0, "actual_fee_cash_cny": 6.0},
            "fill_cash_exceeds_reservation",
        ),
        ({"local_trade_sha256": "bad"}, "invalid_local_trade_sha256"),
    ],
)
def test_fill_commit_fails_closed_before_append(
    tmp_path: Path,
    overrides: dict[str, object],
    reason: str,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    request = _ashare_fill_request(ledger, reservation, **overrides)
    event_count = len(ledger._load_events_unlocked())

    decision = ledger.commit_fill(request)

    assert decision.committed is False
    assert decision.reason == reason
    assert len(ledger._load_events_unlocked()) == event_count
    assert ledger.snapshot().active_reservations_cny == 1_005.0


def test_terminal_price_improvement_releases_only_real_unused_legs(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(
        ledger,
        cash=1_105.0,
        exposure=1_100.0,
    )
    before = ledger.snapshot()

    decision = ledger.commit_fill(_ashare_fill_request(ledger, reservation))

    assert decision.snapshot is not None
    assert decision.snapshot.available_to_reserve_cny == (
        before.available_to_reserve_cny + 100.0
    )
    event = _head(ledger)
    assert event["event_type"] == "fill_commit"
    assert event["cash_reservation_released_cny"] == 100.0
    assert event["exposure_reservation_released_cny"] == 100.0
    assert not any(
        row["event_type"] == "release" for row in ledger._load_events_unlocked()
    )


def test_reconcile_requires_exact_map_for_active_unfilled_reservation(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    assert reservation.approved is True

    with pytest.raises(
        MarketCapitalLedgerError,
        match="reconcile_active_reservation_map_required",
    ):
        ledger.mtm_reconcile(
            _canonical_reconcile_manifest(
                ledger,
                ReconcileManifest(
                    market="ashare",
                    authority_id="ashare-capital-v1",
                    as_of="20260713",
                    cash_balance_cny=50_000.0,
                    positions_market_value={},
                    unrealized_pnl_cny=0.0,
                    position_margin_by_risk_unit={},
                    active_reservations_cny=1_005.0,
                    frozen_order_cash_cny=0.0,
                    frozen_order_margin_cny=0.0,
                    authority_generation=1,
                    execution_lineage_id=LINEAGE_ID,
                    pit_timestamp="2026-07-13T09:31:00+08:00",
                    source="local_sim_account",
                    source_sha256=_sha("active-unfilled"),
                ),
            )
        )


def test_new_reservation_counts_unreconciled_fill_overlay_for_single_name_cap(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    first_reservation = _reserve_ashare(
        ledger,
        cash=7_005.0,
        exposure=7_000.0,
    )
    first_fill = ledger.commit_fill(
        _ashare_fill_request(
            ledger,
            first_reservation,
            actual_filled_quantity=700,
            actual_exposure_cny=7_000.0,
            actual_cash_debit_cny=7_005.0,
        )
    )
    assert first_fill.committed is True

    second = ledger.reserve(
        MarketCapitalReservationRequest(
            market="ashare",
            reference_id="ORDER-2",
            risk_unit_key="000001.XSHE",
            worst_case_amount_cny=1_005.0,
            authority_id="ashare-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of=PIT,
            lineage_sha256=_sha("lineage"),
            authority_generation=1,
            execution_lineage_id=LINEAGE_ID,
            worst_case_cash_cny=1_005.0,
            worst_case_exposure_cny=1_000.0,
        )
    )

    assert second.approved is False
    assert second.reason == "single_name_cap_exceeded"


def test_cn_provider_available_margin_respects_fee_cash_leg(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="CN-CASH-BOUND",
            risk_unit_key="IF2607",
            worst_case_amount_cny=1_000.0,
            authority_id="cn-futures-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of=PIT,
            lineage_sha256=_sha("cn-cash-bound"),
            authority_generation=1,
            execution_lineage_id=LINEAGE_ID,
            worst_case_cash_cny=40_000.0,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=1_000.0,
        )
    )
    assert reservation.approved is True

    snapshot = ledger.snapshot()
    provider = ledger.provider_state(TRADE_DATE)

    assert snapshot.available_to_reserve_cny == 9_000.0
    assert provider["available_margin"] == snapshot.available_to_reserve_cny


def test_cn_cancel_release_clears_margin_and_fee_cash_legs(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="CN-CANCEL",
            risk_unit_key="IF2607",
            worst_case_amount_cny=1_000.0,
            authority_id="cn-futures-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of=PIT,
            lineage_sha256=_sha("cn-cancel"),
            authority_generation=1,
            execution_lineage_id=LINEAGE_ID,
            worst_case_cash_cny=100.0,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=1_000.0,
        )
    )
    assert reservation.approved is True

    ledger.release(
        reservation.reservation_id,
        1_000.0,
        "cancelled_before_fill",
        reference_id="CN-CANCEL:release",
    )

    snapshot = ledger.snapshot()
    assert snapshot.reserved_margin_cny == 0.0
    assert snapshot.reserved_cash_cny == 0.0
    assert ledger.active_reservation_manifest() == {}


def test_open_partial_requires_real_remaining_reservation(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)

    decision = ledger.commit_fill(
        _ashare_fill_request(
            ledger,
            reservation,
            status="partial",
            terminal=False,
        )
    )

    assert decision.committed is False
    assert decision.reason == "partial_open_without_remaining_reservation"
    assert ledger.snapshot().active_reservations_cny == 1_005.0


def test_runtime_cas_checksum_is_exposed_on_snapshot_and_reservation(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    before = ledger.snapshot()
    assert before.event_id
    assert len(before.event_checksum) == 64

    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="CN-CAS",
            risk_unit_key="IF2607",
            worst_case_amount_cny=1_000.0,
            authority_id="cn-futures-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of=PIT,
            lineage_sha256=_sha("cn-cas"),
            authority_generation=1,
            execution_lineage_id=LINEAGE_ID,
            worst_case_cash_cny=10.0,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=1_000.0,
        )
    )

    assert reservation.approved is True
    assert reservation.snapshot is not None
    assert reservation.snapshot.event_id == reservation.event_id
    assert len(reservation.snapshot.event_checksum) == 64
    provider = ledger.provider_state(TRADE_DATE)
    assert provider["event_checksum"] == reservation.snapshot.event_checksum


def test_cn_open_fee_is_realized_once_and_provider_remains_conserved(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")

    opened = _open_cn_position(ledger)

    assert opened.snapshot is not None
    assert opened.snapshot.cash_balance_cny == 49_980.0
    assert opened.snapshot.margin_used_cny == 9_000.0
    assert opened.snapshot.realized_pnl_cny == -20.0
    provider = ledger.provider_state(TRADE_DATE)
    assert provider["cumulative_pnl"] == -20.0
    assert provider["equity_cny"] == 49_980.0


def test_cn_partial_close_atomically_releases_margin_and_records_net_pnl(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    _open_cn_position(ledger)

    decision = commit_market_capital_position_close(
        "cn_futures",
        _cn_close_request(ledger),
        root=ledger.root,
        policy=ledger.policy,
    )

    assert decision.committed is True
    assert decision.snapshot is not None
    assert decision.snapshot.margin_used_cny == 4_500.0
    assert decision.snapshot.cash_balance_cny == 50_175.0
    assert decision.snapshot.realized_pnl_cny == 175.0
    assert decision.snapshot.available_to_reserve_cny == 20_500.0
    assert (
        decision.snapshot.unreconciled_fill_commit_ids == (_head(ledger)["event_id"],)
        or _head(ledger)["event_id"] in decision.snapshot.unreconciled_fill_commit_ids
    )


def test_cn_terminal_close_releases_all_remaining_margin(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    _open_cn_position(ledger)

    decision = ledger.commit_position_close(
        _cn_close_request(
            ledger,
            status="filled",
            terminal=True,
            actual_closed_quantity=2,
            actual_fill_price=3_480.0,
            actual_margin_released_cny=9_000.0,
            actual_fee_cash_cny=10.0,
            actual_gross_realized_pnl_cny=-400.0,
        )
    )

    assert decision.committed is True
    assert decision.snapshot is not None
    assert decision.snapshot.margin_used_cny == 0.0
    assert decision.snapshot.cash_balance_cny == 49_570.0
    assert decision.snapshot.realized_pnl_cny == -430.0
    assert decision.snapshot.available_to_reserve_cny == 25_000.0


def test_cn_close_exact_retry_is_idempotent_and_changed_payload_conflicts(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    _open_cn_position(ledger)
    request = _cn_close_request(ledger)

    first = ledger.commit_position_close(request)
    retry = ledger.commit_position_close(request)

    assert first.committed is True
    assert retry.committed is True
    assert retry.idempotent is True
    assert retry.event_id == first.event_id
    with pytest.raises(
        MarketCapitalLedgerError,
        match="position_close_commit_conflict",
    ):
        ledger.commit_position_close(replace(request, actual_fee_cash_cny=6.0))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"actual_margin_released_cny": 9_001.0}, "close_margin_exceeds_position"),
        ({"actual_margin_released_cny": 1.0}, "close_margin_release_mismatch"),
        (
            {"actual_gross_realized_pnl_cny": 1_000_000_000.0},
            "close_realized_pnl_mismatch",
        ),
        ({"side": "buy"}, "close_side_mismatch"),
        ({"actual_closed_quantity": 3}, "close_quantity_exceeds_position"),
        ({"expected_ledger_checksum": _sha("stale")}, "ledger_head_cas_mismatch"),
        ({"local_position_sha256": "bad"}, "invalid_local_position_sha256"),
        ({"actual_closed_quantity": 0}, "invalid_actual_closed_quantity"),
    ],
)
def test_cn_close_fails_closed_before_append(
    tmp_path: Path,
    overrides: dict[str, object],
    reason: str,
) -> None:
    ledger = _init_ledger(tmp_path, "cn_futures")
    _open_cn_position(ledger)
    event_count = len(ledger._load_events_unlocked())

    decision = ledger.commit_position_close(_cn_close_request(ledger, **overrides))

    assert decision.committed is False
    assert decision.reason == reason
    assert len(ledger._load_events_unlocked()) == event_count
    assert ledger.snapshot().margin_used_cny == 9_000.0


def test_ashare_sell_atomically_returns_cash_and_reduces_derived_exposure(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(
        ledger,
        cash=2_010.0,
        exposure=2_000.0,
    )
    opened = ledger.commit_fill(
        _ashare_fill_request(
            ledger,
            reservation,
            actual_filled_quantity=200,
            actual_exposure_cny=2_000.0,
            actual_cash_debit_cny=2_005.0,
        )
    )
    assert opened.committed is True
    assert opened.snapshot is not None
    assert opened.snapshot.positions_quantity_by_risk_unit == {"000001.XSHE": 200}

    decision = commit_market_capital_ashare_sell(
        "ashare",
        _ashare_sell_request(ledger),
        root=ledger.root,
        policy=ledger.policy,
    )

    assert decision.committed is True
    assert decision.snapshot is not None
    assert decision.snapshot.cash_balance_cny == 49_089.0
    assert decision.snapshot.positions_market_value_cny == 1_000.0
    assert decision.snapshot.positions_quantity_by_risk_unit == {"000001.XSHE": 100}
    assert decision.snapshot.realized_pnl_cny == 91.5
    assert decision.snapshot.available_to_reserve_cny == 44_000.0
    event = _head(ledger)
    assert event["event_type"] == "ashare_sell_commit"
    assert event["actual_exposure_released_cny"] == 1_000.0


def test_ashare_full_sell_closes_quantity_and_exposure(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    opened = ledger.commit_fill(_ashare_fill_request(ledger, reservation))
    assert opened.committed is True

    decision = ledger.commit_ashare_sell(_ashare_sell_request(ledger))

    assert decision.committed is True
    assert decision.snapshot is not None
    assert decision.snapshot.cash_balance_cny == 50_089.0
    assert decision.snapshot.positions_market_value_cny == 0.0
    assert decision.snapshot.positions_quantity_by_risk_unit == {}
    assert decision.snapshot.realized_pnl_cny == 89.0
    assert decision.snapshot.available_to_reserve_cny == 45_000.0


def test_ashare_same_day_sell_fails_closed_before_append(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    opened = ledger.commit_fill(_ashare_fill_request(ledger, reservation))
    assert opened.committed is True
    event_count = len(ledger._load_events_unlocked())

    decision = ledger.commit_ashare_sell(
        _ashare_sell_request(
            ledger,
            idempotency_key="ashare:sim:20260712:000001.XSHE:sell:blocked",
            filled_at="2026-07-12T09:33:00+08:00",
        )
    )

    assert decision.committed is False
    assert decision.reason == "ashare_sell_quantity_exceeds_t1_sellable"
    assert len(ledger._load_events_unlocked()) == event_count
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.XSHE": 100}


def test_ashare_sell_retry_is_idempotent_and_overclose_fails_closed(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path, "ashare")
    reservation = _reserve_ashare(ledger)
    ledger.commit_fill(_ashare_fill_request(ledger, reservation))
    request = _ashare_sell_request(ledger)

    first = ledger.commit_ashare_sell(request)
    retry = ledger.commit_ashare_sell(request)

    assert first.committed is True
    assert retry.committed is True
    assert retry.idempotent is True
    with pytest.raises(
        MarketCapitalLedgerError,
        match="ashare_sell_commit_conflict",
    ):
        ledger.commit_ashare_sell(
            replace(
                request,
                actual_fee_cash_cny=7.0,
                actual_net_cash_credit_cny=1_093.0,
            )
        )

    second_ledger = _init_ledger(tmp_path / "overclose", "ashare")
    second_reservation = _reserve_ashare(second_ledger)
    second_ledger.commit_fill(_ashare_fill_request(second_ledger, second_reservation))
    event_count = len(second_ledger._load_events_unlocked())
    rejected = second_ledger.commit_ashare_sell(
        _ashare_sell_request(
            second_ledger,
            actual_closed_quantity=200,
            actual_gross_proceeds_cny=2_200.0,
            actual_net_cash_credit_cny=2_194.0,
            actual_gross_realized_pnl_cny=200.0,
        )
    )
    assert rejected.committed is False
    assert rejected.reason == "sell_quantity_exceeds_position"
    assert len(second_ledger._load_events_unlocked()) == event_count
