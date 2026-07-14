from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from CNFutures import sim_executor
from CNFutures.contract_rules import get_contract_rule
from CNFutures import sim_runner
from CNFutures.replay import _execution_annotation
from shared.execution.sim_broker import SimResult
from shared.capital.market_ledger import (
    CN_FUTURES_CONTRACT_SPEC_VERSION,
    MarketCapitalFillCommitRequest,
    MarketCapitalLedger,
    MarketCapitalReservationRequest,
    OpeningStateManifest,
    RECONCILE_SOURCE_SCHEMA_VERSION,
    ReconcileManifest,
    cn_futures_contract_spec_sha256,
    load_market_capital_provider_state,
)
from shared.capital.market_policy import MarketPolicy


def _valid_market_provider_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "source": "market_capital_ledger",
        "reconciled": True,
        "fresh": True,
        "market": "cn_futures",
        "authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "test-lineage-20260710-0001",
        "trade_date": "20260710",
        "initial_equity_cny": 50_000.0,
        "equity_cny": 50_000.0,
        "available_margin": 25_000.0,
        "margin_utilization_limit_cny": 25_000.0,
        "margin_used_cny": 0.0,
        "unrealized_pnl_cny": 0.0,
        "event_id": "MCAP-20260710-RECONCILED",
        "event_checksum": "a" * 64,
        "cumulative_pnl": 0.0,
        "daily_realized_pnl": 0.0,
        "max_daily_loss": 1_500.0,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
        "high_water_equity": 50_000.0,
        "max_drawdown": 3_500.0,
        "real_trading_enabled": False,
    }
    state.update(overrides)
    return state


def _approved_market_reservation(
    *, reference_id: str, worst_case_amount_cny: float, trade_date: str, **extra: object
) -> dict[str, object]:
    return {
        "approved": True,
        "reason": "reserved",
        "reservation_id": f"TEST-RES-{reference_id}",
        "event_id": f"TEST-EVENT-{reference_id}",
        "reference_id": reference_id,
        "risk_unit_key": "RB2610.SHF",
        "authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "amount_cny": worst_case_amount_cny,
        "trade_date": trade_date,
        "point_in_time_as_of": str(
            extra.get("point_in_time_as_of") or "2026-07-10T09:35:00+08:00"
        ),
        "lineage_sha256": str(extra.get("lineage_sha256") or "b" * 64),
        "execution_lineage_id": str(
            extra.get("execution_lineage_id") or "test-lineage-20260710-0001"
        ),
        "event_checksum": "c" * 64,
        "fee_cash_cny": float(extra.get("worst_case_fee_cash_cny") or 0.0),
        "real_trading_enabled": False,
    }


def _released_market_reservation(**kwargs: object) -> dict[str, object]:
    return {
        "status": "released",
        "reservation_id": kwargs.get("reservation_id", ""),
        "amount_cny": kwargs.get("amount_cny", 0.0),
        "reference_id": kwargs.get("reference_id", ""),
        "real_trading_enabled": False,
    }


def _init_market_capital_ledger(capital_root: Path) -> "MarketCapitalLedger":
    """Initialize a MarketCapitalLedger for testing with bootstrap + reconcile."""
    import hashlib
    import json

    # Create legacy freeze archive directory
    archive_dir = capital_root / ".legacy_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create legacy events file with proper content
    legacy_file = archive_dir / "legacy_events.jsonl"
    legacy_event_id = "legacy-dummy-001"
    legacy_content = (
        json.dumps({"event_id": legacy_event_id, "_": "dummy"}, ensure_ascii=False)
        + "\n"
    )
    legacy_file.write_text(legacy_content, encoding="utf-8")
    legacy_sha = hashlib.sha256(legacy_file.read_bytes()).hexdigest()

    policy = MarketPolicy.load("cn_futures")
    ledger = MarketCapitalLedger(capital_root, policy=policy)

    manifest = OpeningStateManifest(
        market="cn_futures",
        authority_id="cn-futures-capital-v1",
        cutover_decision_id="nicholas-fresh-start-019f5040-20260712",
        mode="fresh_start",
        as_of="20260710",
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
        source="test-init",
        source_sha256=legacy_sha,
        execution_lineage_id="test-lineage-20260710-0001",
    )

    cutover = {
        "cutover_decision_id": "nicholas-fresh-start-019f5040-20260712",
        "source_thread_id": "019f5040-76a7-7672-b2fc-91c1526312bf",
        "cutover_state": "fresh_start_approved",
        "authority_generation": 1,
    }

    legacy = {
        "imported": False,
        "events_path": str(legacy_file.absolute()),
        "sha256": legacy_sha,
        "row_count": 1,
        "last_event_id": legacy_event_id,
        "archive_path": str(archive_dir.absolute()),
        "frozen_at": "2026-07-10T09:30:00+08:00",
    }

    ledger.initialize(manifest, cutover_manifest=cutover, legacy_freeze_manifest=legacy)

    reconcile_source = {
        "schema_version": RECONCILE_SOURCE_SCHEMA_VERSION,
        "market": "cn_futures",
        "trade_date": "20260710",
        "pit_timestamp": "2026-07-10T09:30:00+08:00",
        "execution_lineage_id": "test-lineage-20260710-0001",
        "cash_balance_cny": 50_000.0,
        "positions_market_value": {},
        "unrealized_pnl_cny": 0.0,
        "position_margin_by_risk_unit": {},
        "active_reservations_cny": 0.0,
        "active_reservations": None,
        "frozen_order_cash_cny": 0.0,
        "frozen_order_margin_cny": 0.0,
        "positions_quantity_by_risk_unit": {},
        "positions_cost_basis_cny_by_risk_unit": {},
        "positions_entry_fee_cny_by_risk_unit": {},
        "position_entry_price_by_risk_unit": {},
        "position_side_by_risk_unit": {},
        "position_contract_multiplier_by_risk_unit": {},
        "position_contract_spec_sha256_by_risk_unit": {},
        "position_mark_price_by_risk_unit": {},
        "expected_ledger_event_id": "",
        "expected_ledger_checksum": "",
        "included_fill_commit_ids": [],
        "real_trading_enabled": False,
    }
    reconcile_path = capital_root / "test-reconcile-source.json"
    reconcile_path.write_text(
        json.dumps(reconcile_source, sort_keys=True),
        encoding="utf-8",
    )
    reconcile_sha = hashlib.sha256(reconcile_path.read_bytes()).hexdigest()
    reconcile = ReconcileManifest(
        market="cn_futures",
        authority_id="cn-futures-capital-v1",
        as_of="20260710",
        cash_balance_cny=50_000.0,
        positions_market_value={},
        unrealized_pnl_cny=0.0,
        position_margin_by_risk_unit={},
        active_reservations_cny=0.0,
        frozen_order_cash_cny=0.0,
        frozen_order_margin_cny=0.0,
        authority_generation=1,
        execution_lineage_id="test-lineage-20260710-0001",
        pit_timestamp="2026-07-10T09:30:00+08:00",
        source="test-reconcile",
        source_sha256=reconcile_sha,
        canonical_snapshot_path=str(reconcile_path.resolve()),
        canonical_snapshot_sha256=reconcile_sha,
    )
    ledger.mtm_reconcile(reconcile)

    return ledger


def _commit_test_futures_open(
    ledger: MarketCapitalLedger,
    *,
    symbol: str = "RB2610.SHF",
    margin: float = 4_550.0,
) -> dict[str, object]:
    lineage = "test-lineage-20260710-0001"
    lineage_sha = "0" * 64
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id=f"OPEN:{symbol}",
            risk_unit_key=symbol,
            worst_case_amount_cny=margin,
            authority_id="cn-futures-capital-v1",
            authority_generation=1,
            trade_date="20260710",
            point_in_time_as_of="2026-07-10T09:35:00+08:00",
            lineage_sha256=lineage_sha,
            execution_lineage_id=lineage,
            worst_case_cash_cny=0.0,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=margin,
        )
    )
    assert reservation.approved is True
    assert reservation.snapshot is not None
    fill_id = f"OPEN-FILL-{symbol}"
    multiplier = float(get_contract_rule(symbol).contract_multiplier)
    decision = ledger.commit_fill(
        MarketCapitalFillCommitRequest(
            market="cn_futures",
            reference_id=(
                f"MCAPFILL:1:{lineage}:{reservation.reservation_id}:{fill_id}"
            ),
            reservation_id=reservation.reservation_id,
            reservation_event_id=reservation.event_id,
            reservation_reference_id=f"OPEN:{symbol}",
            risk_unit_key=symbol,
            authority_id="cn-futures-capital-v1",
            authority_generation=1,
            execution_lineage_id=lineage,
            lineage_sha256=lineage_sha,
            order_id=f"OPEN:{symbol}",
            idempotency_key=f"OPEN:{symbol}",
            execution_fill_id=fill_id,
            fill_sequence=1,
            side="buy",
            status="filled",
            terminal=True,
            actual_filled_quantity=1,
            actual_fill_price=3_520.0,
            actual_cash_debit_cny=0.0,
            actual_exposure_cny=0.0,
            actual_margin_cny=margin,
            actual_fee_cash_cny=0.0,
            contract_multiplier=multiplier,
            contract_margin_per_lot_cny=margin,
            contract_spec_version=CN_FUTURES_CONTRACT_SPEC_VERSION,
            contract_spec_sha256=cn_futures_contract_spec_sha256(
                symbol, multiplier, margin
            ),
            filled_at="2026-07-10T09:36:00+08:00",
            point_in_time_as_of="2026-07-10T09:35:00+08:00",
            source="test-open",
            source_sha256="1" * 64,
            receipt_sha256="2" * 64,
            local_trade_sha256="3" * 64,
            expected_ledger_event_id=reservation.snapshot.event_id,
            expected_ledger_checksum=reservation.snapshot.event_checksum,
        )
    )
    assert decision.committed is True
    return {
        "reservation": reservation,
        "commit": decision,
    }


class _AuthorityAdapter:
    universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

    def __init__(self, account: dict[str, object]) -> None:
        self._account = account

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "styles": {
                "trend": {
                    "risk_per_trade": 0.10,
                    "max_margin_usage": 0.30,
                    "weight": 1.0,
                    "no_overnight": True,
                }
            }
        }

    def get_sim_account(self) -> dict[str, object]:
        return dict(self._account)

    def get_intraday_universe(self, date: str, interval: str) -> list[str]:
        return ["RB2610.SHF"]


class _FreshRunnerReader:
    def get_bars_intraday(
        self, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        return [
            {"bar_time": "2026-07-10 09:30:00", "close": 3_490.0, "volume": 1_000},
            {"bar_time": "2026-07-10 09:35:00", "close": 3_500.0, "volume": 1_000},
        ]


def test_minimum_contract_above_risk_budget_returns_zero() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={
            "equity": 50_000.0,
            "available_margin": 50_000.0,
            "consecutive_losses": 0,
            "max_consecutive_losses": 3,
        },
        style={
            "risk_per_trade": 0.01,
            "max_margin_usage": 0.30,
            "weight": 0.25,
        },
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    assert decision["margin_budget"] == 125.0
    assert decision["quantity"] == 0
    assert decision["eligible"] is False
    assert decision["reason"] == "minimum_contract_exceeds_risk_budget"


def test_unaffordable_contract_creates_hold_not_error() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={
            "equity": 50_000.0,
            "available_margin": 50_000.0,
            "consecutive_losses": 0,
            "max_consecutive_losses": 3,
        },
        style={
            "risk_per_trade": 0.01,
            "max_margin_usage": 0.30,
            "weight": 0.25,
        },
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    hold = sim_runner.build_affordability_hold(
        symbol="RB2610.SHF",
        style_name="trend",
        size_decision=decision,
        cadence="5min",
        bar_time="2026-07-10T09:35:00+08:00",
        session="day",
    )

    assert hold["stage"] == "risk"
    assert hold["reason"] == "minimum_contract_exceeds_risk_budget"
    assert hold["size_decision"]["quantity"] == 0


def test_missing_stop_or_gap_risk_input_fails_closed() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={"equity": 50_000.0, "available_margin": 50_000.0},
        style={
            "risk_per_trade": 0.01,
            "max_margin_usage": 0.30,
            "weight": 0.25,
        },
        exit_plan={},
        session={"can_hold_overnight": True},
    )

    assert decision["quantity"] == 0
    assert decision["eligible"] is False
    assert decision["reason"] == "missing_contract_risk_inputs"
    assert decision["counterfactual_only"] is True


def test_consecutive_loss_gate_precedes_affordability() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={
            "equity": 50_000.0,
            "available_margin": 4_000.0,
            "consecutive_losses": 3,
            "max_consecutive_losses": 3,
        },
        style={
            "risk_per_trade": 0.01,
            "max_margin_usage": 0.30,
            "weight": 0.25,
        },
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    assert decision["quantity"] == 0
    assert decision["reason"] == "consecutive_loss_limit"


def test_overnight_sizing_uses_larger_of_stop_and_modeled_gap() -> None:
    common = {
        "symbol": "RB2610.SHF",
        "price": 3_500.0,
        "account_state": {
            "equity": 10_000_000.0,
            "available_margin": 10_000_000.0,
        },
        "style": {
            "risk_per_trade": 0.20,
            "max_margin_usage": 0.30,
            "weight": 1.0,
        },
        "exit_plan": {"stop_loss_pct": 0.001},
    }

    day = sim_runner.quantity_for_style_decision(
        **common,
        session={"can_hold_overnight": False},
    )
    overnight = sim_runner.quantity_for_style_decision(
        **common,
        session={"can_hold_overnight": True},
    )

    rule = get_contract_rule("RB2610.SHF")
    assert overnight["directional_loss_rate"] == rule.modeled_overnight_gap_pct
    assert overnight["modeled_loss_per_lot"] > day["modeled_loss_per_lot"]


def test_sizing_slippage_cannot_drop_below_contract_modeled_minimum() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={"equity": 500_000.0, "available_margin": 500_000.0},
        style={
            "risk_per_trade": 0.10,
            "max_margin_usage": 0.30,
            "weight": 1.0,
            "slippage_bps": 0.0,
        },
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    assert (
        decision["modeled_slippage_bps"]
        == get_contract_rule("RB2610.SHF").modeled_slippage_bps
    )


def test_daily_loss_gate_precedes_affordability() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={
            "equity": 48_400.0,
            "available_margin": 48_400.0,
            "daily_realized_pnl": -1_600.0,
            "max_daily_loss": 1_500.0,
        },
        style={
            "risk_per_trade": 0.10,
            "max_margin_usage": 0.30,
            "weight": 1.0,
        },
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    assert decision["quantity"] == 0
    assert decision["reason"] == "daily_loss_limit"


def test_maximum_drawdown_gate_precedes_affordability() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={
            "equity": 44_000.0,
            "available_margin": 44_000.0,
            "drawdown": 6_000.0,
            "max_drawdown": 5_000.0,
        },
        style={
            "risk_per_trade": 0.10,
            "max_margin_usage": 0.30,
            "weight": 1.0,
        },
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    assert decision["quantity"] == 0
    assert decision["reason"] == "maximum_drawdown_limit"


def test_five_percent_drawdown_derisks_sizing_instead_of_hard_stopping() -> None:
    common = {
        "symbol": "RB2610.SHF",
        "price": 3_500.0,
        "style": {
            "risk_per_trade": 0.10,
            "max_margin_usage": 0.30,
            "weight": 1.0,
        },
        "exit_plan": {"stop_loss_pct": 0.01},
        "session": {"can_hold_overnight": False},
    }
    normal = sim_runner.quantity_for_style_decision(
        **common,
        account_state={
            "equity": 50_000.0,
            "available_margin": 5_000.0,
            "drawdown": 0.0,
            "max_drawdown": 3_500.0,
        },
    )
    tightened = sim_runner.quantity_for_style_decision(
        **common,
        account_state={
            "equity": 47_500.0,
            "available_margin": 5_000.0,
            "drawdown": 2_500.0,
            "max_drawdown": 3_500.0,
        },
    )

    assert tightened["reason"] != "maximum_drawdown_limit"
    assert tightened["drawdown_tightened"] is True
    assert tightened["risk_multiplier"] == 0.75
    assert tightened["loss_budget"] < normal["loss_budget"]
    assert tightened["margin_budget"] < normal["margin_budget"]


def test_gate_precedence_is_daily_then_consecutive_then_drawdown() -> None:
    common = {
        "symbol": "RB2610.SHF",
        "price": 3_500.0,
        "style": {
            "risk_per_trade": 0.10,
            "max_margin_usage": 0.30,
            "weight": 1.0,
        },
        "exit_plan": {"stop_loss_pct": 0.01},
        "session": {"can_hold_overnight": False},
    }
    all_breached = sim_runner.quantity_for_style_decision(
        **common,
        account_state={
            "equity": 44_000.0,
            "available_margin": 44_000.0,
            "daily_realized_pnl": -1_600.0,
            "max_daily_loss": 1_500.0,
            "consecutive_losses": 3,
            "max_consecutive_losses": 3,
            "drawdown": 6_000.0,
            "max_drawdown": 5_000.0,
        },
    )
    streak_and_drawdown = sim_runner.quantity_for_style_decision(
        **common,
        account_state={
            "equity": 44_000.0,
            "available_margin": 44_000.0,
            "daily_realized_pnl": 0.0,
            "max_daily_loss": 1_500.0,
            "consecutive_losses": 3,
            "max_consecutive_losses": 3,
            "drawdown": 6_000.0,
            "max_drawdown": 5_000.0,
        },
    )

    assert all_breached["reason"] == "daily_loss_limit"
    assert streak_and_drawdown["reason"] == "consecutive_loss_limit"


def test_zero_or_non_finite_sizing_inputs_fail_closed() -> None:
    base = {
        "symbol": "RB2610.SHF",
        "price": 3_500.0,
        "account_state": {"equity": 50_000.0, "available_margin": 50_000.0},
        "style": {
            "risk_per_trade": 0.01,
            "max_margin_usage": 0.30,
            "weight": 1.0,
        },
        "exit_plan": {"stop_loss_pct": 0.01},
        "session": {"can_hold_overnight": False},
    }
    zero_risk = sim_runner.quantity_for_style_decision(
        **{**base, "style": {**base["style"], "risk_per_trade": 0.0}}
    )
    zero_weight = sim_runner.quantity_for_style_decision(
        **{**base, "style": {**base["style"], "weight": 0.0}}
    )
    infinite_equity = sim_runner.quantity_for_style_decision(
        **{
            **base,
            "account_state": {"equity": float("inf"), "available_margin": 50_000.0},
        }
    )

    for decision in (zero_risk, zero_weight, infinite_equity):
        assert decision["quantity"] == 0
        assert decision["reason"] == "missing_contract_risk_inputs"
        assert decision["counterfactual_only"] is True


def test_missing_contract_fee_metadata_fails_closed() -> None:
    cost = SimpleNamespace(
        margin_required=4_550.0,
        total_estimated_fee=0.0,
        rule=SimpleNamespace(
            contract_multiplier=10,
            margin_rate=0.13,
            open_fee_rate=None,
            close_fee_rate=0.0001,
            modeled_overnight_gap_pct=0.03,
            modeled_slippage_bps=2.0,
        ),
    )
    with patch.object(sim_runner, "estimate_order_cost", return_value=cost):
        decision = sim_runner.quantity_for_style_decision(
            symbol="RB2610.SHF",
            price=3_500.0,
            account_state={"equity": 50_000.0, "available_margin": 50_000.0},
            style={"risk_per_trade": 0.10, "max_margin_usage": 0.30, "weight": 1.0},
            exit_plan={"stop_loss_pct": 0.01},
            session={"can_hold_overnight": False},
        )

    assert decision["quantity"] == 0
    assert decision["reason"] == "missing_contract_risk_inputs"
    assert decision["counterfactual_only"] is True


def test_unknown_contract_rule_is_a_hold_decision_not_an_exception() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="ZZ2610.UNKNOWN",
        price=3_500.0,
        account_state={"equity": 50_000.0, "available_margin": 50_000.0},
        style={"risk_per_trade": 0.10, "max_margin_usage": 0.30, "weight": 1.0},
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    assert decision["quantity"] == 0
    assert decision["reason"] == "missing_contract_risk_inputs"
    assert decision["counterfactual_only"] is True


def test_sim_executor_rejects_zero_quantity_before_cost_model() -> None:
    with patch.object(sim_executor, "estimate_order_cost") as estimate:
        result = sim_executor.cn_futures_sim_execute(
            {
                "order_id": "SIM-CNF-zero",
                "symbol": "RB2610.SHF",
                "side": "buy",
                "quantity": 0,
                "price": 3_500.0,
            }
        )

    estimate.assert_not_called()
    assert result.status == "rejected"
    assert result.filled_qty == 0
    assert result.raw_response["reason"] == "non_positive_quantity"


def test_runner_persists_unaffordable_candidate_as_hold_before_order() -> None:
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {
                "styles": {
                    "trend": {
                        "risk_per_trade": 0.01,
                        "max_margin_usage": 0.30,
                        "weight": 1.0,
                        "no_overnight": True,
                    }
                }
            }

        def get_sim_account(self) -> dict[str, object]:
            return {"sim_capital": 50_000.0}

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    class Reader:
        def get_bars_intraday(
            self,
            market: str,
            symbol: str,
            interval: str,
            start: object = None,
            end: object = None,
        ) -> list[dict[str, object]]:
            return [
                {"bar_time": "2026-07-10 09:30:00", "close": 3_490.0, "volume": 1_000},
                {"bar_time": "2026-07-10 09:35:00", "close": 3_500.0, "volume": 1_000},
            ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        review_path = root / "cn_futures_reviews.jsonl"
        with (
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(sim_runner, "execute_sim_order") as execute,
        ):
            result = sim_runner.run_multi_style_simulation(
                Adapter(),
                "20260710",
                Reader(),
                signals_dir=root / "signals",
                review_path=review_path,
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

        execute.assert_not_called()
        assert result["filled_count"] == 0
        assert result["errors"] == []
        assert result["holds"][0]["reason"] == "account_state_unavailable"
        assert result["holds"][0]["size_decision"]["counterfactual_only"] is True
        affordability = json.loads(
            (root / "cn_futures_affordability_latest.json").read_text(encoding="utf-8")
        )
        assert affordability["raw_distinct_products"] == ["rb"]
        assert affordability["affordable_distinct_products"] == []
        assert affordability["contracts"][0]["reason"] == "account_state_unavailable"
        review = json.loads(review_path.read_text(encoding="utf-8").splitlines()[-1])
        assert (
            review["affordability"]["contracts"][0]["reason"]
            == "account_state_unavailable"
        )


def test_startup_order_projection_mismatch_blocks_execution_but_keeps_observation(
    tmp_path: Path,
) -> None:
    signals_dir = tmp_path / "signals"
    filled_dir = signals_dir / "filled"
    filled_dir.mkdir(parents=True)
    (filled_dir / "SIM-CNF-LEGACY-1.json").write_text(
        json.dumps(
            {
                "order_id": "SIM-CNF-LEGACY-1",
                "market": "cn_futures",
                "symbol": "CU2610.SHF",
                "status": "filled",
                "filled_qty": 1,
                "filled_price": 80_000.0,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_trading_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    size_decision = {
        "quantity": 1,
        "eligible": True,
        "reason": "eligible",
        "counterfactual_only": False,
        "counterfactual_eligible": True,
        "margin_required": 4_550.0,
        "stop_loss_amount": 100.0,
    }
    with (
        patch.object(
            sim_runner,
            "get_cn_futures_capital_provider_state",
            return_value=_valid_market_provider_state(),
        ),
        patch.object(
            sim_runner,
            "generate_style_signal",
            return_value={
                "action": "buy",
                "side": "buy",
                "price": 3_500.0,
                "directional_score": 0.0125,
                "confidence": 0.7,
            },
        ),
        patch.object(
            sim_runner,
            "quantity_for_style_decision",
            return_value=size_decision,
        ),
        patch.object(sim_runner, "execute_sim_order") as execute,
    ):
        result = sim_runner.run_multi_style_simulation(
            _AuthorityAdapter({"sim_capital": 50_000.0}),
            "20260710",
            _FreshRunnerReader(),
            signals_dir=signals_dir,
            review_path=tmp_path / "cn_futures_reviews.jsonl",
            now=datetime.fromisoformat("2026-07-10 09:36:00"),
        )

    execute.assert_not_called()
    assert result["filled_count"] == 0
    assert result["order_projection_reconcile"]["ready"] is False
    assert result["order_projection_reconcile"]["state"] == "HALTED"
    assert any(
        hold.get("reason") == "order_projection_reconcile_halted"
        for hold in result["holds"]
    )
    assert result["review"]["observation_sample_count"] >= 1
    assert (
        result["review"]["affordability"]["order_projection_reconcile"]["state"]
        == "HALTED"
    )


def test_runner_persists_complete_prediction_evidence_for_one_lot_unaffordable_hold() -> (
    None
):
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {
                "styles": {
                    "trend": {
                        "style_version": "trend-v7",
                        "risk_per_trade": 0.01,
                        "max_margin_usage": 0.30,
                        "weight": 1.0,
                        "prediction_horizon_bars": 3,
                        "time_stop_bars": 3,
                        "max_hold_bars": 6,
                        "no_overnight": True,
                        "mg_enabled": True,
                    }
                }
            }

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    signal = {
        "action": "buy",
        "side": "buy",
        "price": 3_500.0,
        "directional_score": 0.0125,
        "momentum": 0.01,
        "confidence": 0.73,
        "scenario_tags": {"volatility_bucket": "high"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        review_path = root / "cn_futures_reviews.jsonl"
        with (
            patch.object(
                sim_runner,
                "get_cn_futures_capital_provider_state",
                return_value=_valid_market_provider_state(),
            ),
            patch.object(sim_runner, "generate_style_signal", return_value=signal),
            patch.object(sim_runner, "execute_sim_order") as execute,
        ):
            result = sim_runner.run_multi_style_simulation(
                Adapter(),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=review_path,
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

        execute.assert_not_called()
        hold = result["holds"][0]
        assert hold["reason"] == "minimum_contract_exceeds_risk_budget"
        assert hold["execution_class"] == "counterfactual_only"
        assert hold["counterfactual_only"] is True
        assert hold["real"] is False
        assert hold["real_trading_enabled"] is False
        assert hold["direction"] == "buy"
        assert hold["side"] == "buy"
        assert hold["style"] == "trend"
        assert hold["style_version"] == "trend-v7"
        assert hold["raw_heuristic_score"] == 0.0125
        assert hold["uncalibrated_confidence_prior"] == 0.73
        assert hold["calibrated_probability"] is None
        assert hold["probability_model_state"] == "not_calibrated"
        assert "probability" not in hold
        assert hold["market_regime"] == "volatility_high"
        assert hold["prediction_snapshot"]["market_regime_source"] == (
            "scenario_tags.volatility_bucket"
        )
        assert hold["mg_on"] is True
        assert hold["holding_horizon"] == {
            "unit": "bars",
            "prediction_horizon_bars": 3,
            "time_stop_bars": 3,
            "max_hold_bars": 6,
            "no_overnight": True,
        }
        assert hold["prediction_evidence_status"] == "complete"
        assert hold["forward_label_status"] == "pending_future_bars"
        assert hold["forward_outcome"]["status"] == "pending_future_bars"
        assert hold["prediction_snapshot"]["raw_signal"] == signal
        affordability = result["affordability"]["contracts"][0]
        assert affordability["execution_class"] == "counterfactual_only"
        assert affordability["counterfactual_only"] is True
        assert affordability["size_decision"]["counterfactual_only"] is True

        persisted = json.loads(review_path.read_text(encoding="utf-8").splitlines()[-1])
        assert persisted["observation_sample_count"] == 1
        observation = persisted["observation_samples"][0]
        assert observation["execution_class"] == "counterfactual_only"
        assert observation["label_status"] == "pending"
        assert observation["forward_outcome"]["status"] == "pending_future_bars"
        assert (
            observation["decision_snapshot"]["prediction_snapshot"]["raw_signal"]
            == signal
        )
        assert observation["real_trading_enabled"] is False


def test_runner_marks_incomplete_prediction_evidence_without_pending_label() -> None:
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {
                "styles": {
                    "trend": {
                        "style_version": "trend-v7",
                        "risk_per_trade": 0.01,
                        "max_margin_usage": 0.30,
                        "weight": 1.0,
                        "prediction_horizon_bars": 3,
                        "no_overnight": True,
                    }
                }
            }

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    incomplete_signal = {"action": "buy", "side": "buy", "price": 3_500.0}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        review_path = root / "cn_futures_reviews.jsonl"
        with (
            patch.object(
                sim_runner,
                "get_cn_futures_capital_provider_state",
                return_value=_valid_market_provider_state(),
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value=incomplete_signal,
            ),
            patch.object(sim_runner, "execute_sim_order") as execute,
        ):
            result = sim_runner.run_multi_style_simulation(
                Adapter(),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=review_path,
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

        execute.assert_not_called()
        hold = result["holds"][0]
        assert hold["execution_class"] == "counterfactual_only"
        assert hold["real"] is False
        assert hold["prediction_evidence_status"] == "incomplete"
        assert hold["prediction_evidence_reason"] == "prediction_evidence_incomplete"
        assert hold["prediction_snapshot"]["missing_fields"] == [
            "raw_heuristic_score",
            "market_regime",
        ]
        assert hold["forward_label_status"] == "prediction_evidence_incomplete"
        assert hold["forward_outcome"] == {
            "status": "prediction_evidence_incomplete",
            "reason": "prediction_evidence_incomplete",
        }

        persisted = json.loads(review_path.read_text(encoding="utf-8").splitlines()[-1])
        observation = persisted["observation_samples"][0]
        assert observation["prediction_evidence_status"] == "incomplete"
        assert observation["label_status"] == "prediction_evidence_incomplete"
        assert (
            observation["forward_outcome"]["status"] == "prediction_evidence_incomplete"
        )
        assert (
            observation["decision_snapshot"]["prediction_snapshot"]["raw_signal"]
            == incomplete_signal
        )
        assert "pending" not in json.dumps(observation, ensure_ascii=False)


def test_adapter_account_dict_cannot_self_assert_master_authority() -> None:
    forged_account = {
        "sim_capital": 20_000_000.0,
        "account_state_authoritative": True,
        "account_state": _valid_market_provider_state(
            initial_equity_cny=20_000_000.0,
            equity_cny=20_000_000.0,
            available_margin=20_000_000.0,
            high_water_equity=20_000_000.0,
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with (
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(sim_runner, "execute_sim_order") as execute,
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter(forged_account),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

    execute.assert_not_called()
    assert result["filled_count"] == 0
    assert result["holds"][0]["reason"] == "account_state_unavailable"
    assert result["affordability"]["account_state"]["equity"] == 50_000.0


def test_provider_and_mock_reservation_cannot_bypass_atomic_capital_commit() -> None:
    filled = SimResult(
        status="filled",
        filled_qty=1,
        avg_price=3_500.0,
        fee=7.0,
        message="filled",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-provider",
        market="cn_futures",
        raw_response={
            "real_trading_enabled": False,
            "fill_evidence_type": "bar_volume_participation",
            "evidence_timestamp": "2026-07-10T09:35:00+08:00",
            "slippage_bps": 2.0,
            "requested_price": 3_499.3,
            "margin_required": 4_550.0,
            "notional": 35_000.0,
            "contract_multiplier": 10,
            "open_fee": 3.0,
            "estimated_close_fee": 4.0,
            "total_estimated_fee": 7.0,
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with (
            patch.object(
                sim_runner,
                "get_cn_futures_capital_provider_state",
                return_value=_valid_market_provider_state(),
                create=True,
            ) as provider,
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(
                sim_runner,
                "_reserve_cn_futures_market_margin",
                side_effect=_approved_market_reservation,
            ),
            patch.object(
                sim_runner,
                "_record_cn_futures_market_pnl",
                return_value={"status": "recorded", "event_id": "TEST-PNL"},
            ),
            patch.object(
                sim_runner,
                "_release_cn_futures_market_margin",
                side_effect=_released_market_reservation,
            ),
            patch.object(
                sim_runner, "execute_sim_order", return_value=filled
            ) as execute,
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

    provider.assert_called_once_with(trade_date="20260710")
    execute.assert_called_once()
    assert result["filled_count"] == 1
    assert result["account_state"]["authoritative"] is False
    assert result["records"][0]["receipt"]["execution_eligible"] is False
    assert (
        result["records"][0]["receipt"]["execution_class"] == "capital_commit_pending"
    )


def test_real_market_ledger_reserves_futures_margin_before_simulated_fill() -> None:
    filled = SimResult(
        status="filled",
        filled_qty=1,
        avg_price=3_500.0,
        fee=7.0,
        message="filled",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-ledger-open",
        market="cn_futures",
        raw_response={
            "real_trading_enabled": False,
            "fill_evidence_type": "bar_volume_participation",
            "evidence_timestamp": "2026-07-10T09:35:00+08:00",
            "slippage_bps": 2.0,
            "requested_price": 3_499.3,
            "margin_required": 4_550.0,
            "notional": 35_000.0,
            "contract_multiplier": 10,
            "open_fee": 3.0,
            "estimated_close_fee": 4.0,
            "total_estimated_fee": 7.0,
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capital_root = root / "capital"
        _init_market_capital_ledger(capital_root)
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(sim_runner, "execute_sim_order", return_value=filled),
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        assert result["filled_count"] == 1
        assert state is not None
        assert state["active_reservations_cny"] == 0.0
        assert state["margin_used_cny"] == 4_550.0
        assert state["cumulative_pnl"] == -3.0
        assert state["equity_cny"] == 49_997.0
        assert state["consecutive_losses"] == 0
        events = [
            json.loads(line)
            for line in (capital_root / "cn_futures_sim_capital_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        open_fill = next(row for row in events if row["event_type"] == "fill_commit")
        assert open_fill["actual_fee_cash_cny"] == 3.0
        assert not any(row["event_type"] == "realized_pnl" for row in events)
        outbox = json.loads(
            (root / "signals" / "capital" / "cn_futures_capital_outbox.json").read_text(
                encoding="utf-8"
            )
        )
        assert outbox["actions"][0]["status"] == "completed"
        position = json.loads(
            (
                root / "signals" / "positions" / "cn_futures_sim_positions.json"
            ).read_text(encoding="utf-8")
        )["positions"][0]
        assert position["cn_futures_capital_reservations"] == []
        assert position["capital_commit_status"] == "committed"
        assert position["entry_execution_evidence"]["execution_fill_id"]
        assert position["entry_evidence_quantity_remaining"] == 1
        assert result["records"][0]["order"]["cn_futures_capital_event_id"]
        assert result["records"][0]["receipt"]["execution_eligible"] is True, result[
            "records"
        ][0]["receipt"].get("execution_evidence_error")
        evidence = result["records"][0]["execution_evidence"]
        assert evidence["capital_authority_id"] == "cn-futures-capital-v1"
        assert evidence["capital_commit_status"] == "committed"
        assert len(evidence["execution_evidence_sha256"]) == 64
        review = json.loads((root / "reviews.jsonl").read_text().splitlines()[-1])
        assert review["authority_scope"] == {
            "capital_authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "test-lineage-20260710-0001",
        }
        session = review["session_decisions"][0]
        assert session["capital_authority_id"] == "cn-futures-capital-v1"
        assert session["authority_generation"] == 1
        assert session["execution_lineage_id"] == "test-lineage-20260710-0001"

        class _ClosingReader:
            def get_bars_intraday(
                self, *args: object, **kwargs: object
            ) -> list[dict[str, object]]:
                return [
                    {
                        "bar_time": "2026-07-10 14:50:00",
                        "close": 3_510.0,
                        "volume": 1_000,
                    },
                    {
                        "bar_time": "2026-07-10 14:55:00",
                        "close": 3_520.0,
                        "volume": 1_000,
                    },
                ]

        closed = SimResult(
            status="filled",
            filled_qty=1,
            avg_price=3_520.0,
            fee=4.0,
            message="filled",
            capital_layer="simulated",
            account_type="simulated",
            order_id="SIM-CNF-ledger-close",
            market="cn_futures",
            raw_response={
                "real_trading_enabled": False,
                "fill_evidence_type": "bar_volume_participation",
                "evidence_timestamp": "2026-07-10T14:55:00+08:00",
                "slippage_bps": 2.0,
                "requested_price": 3_520.7,
                "margin_required": 4_576.0,
                "notional": 35_200.0,
                "contract_multiplier": 10,
                "estimated_close_fee": 4.0,
                "total_estimated_fee": 4.0,
            },
        )
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(sim_runner, "execute_sim_order", return_value=closed),
        ):
            close_result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _ClosingReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 14:56:00"),
            )

        close_record = close_result["records"][0]
        assert close_record["receipt"]["execution_eligible"] is True
        round_trip = close_record["round_trip_evidence"]
        assert round_trip["round_trip_complete"] is True
        assert round_trip["entry_fill_id"] == evidence["execution_fill_id"]
        assert (
            round_trip["exit_fill_id"]
            == close_record["execution_evidence"]["execution_fill_id"]
        )
        assert round_trip["net_pnl_cny"] == 193.0
        close_review = json.loads((root / "reviews.jsonl").read_text().splitlines()[-1])
        assert close_review["session_decisions"][0]["round_trip_evidence"] == round_trip


def test_rejected_futures_fill_releases_pre_execution_margin_reservation() -> None:
    rejected = SimResult(
        status="rejected",
        filled_qty=0,
        avg_price=0.0,
        fee=0.0,
        message="rejected",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-ledger-rejected",
        market="cn_futures",
        raw_response={"reason": "price_limit_guard", "real_trading_enabled": False},
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capital_root = root / "capital"
        _init_market_capital_ledger(capital_root)
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(sim_runner, "execute_sim_order", return_value=rejected),
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        events = [
            json.loads(line)
            for line in (capital_root / "cn_futures_sim_capital_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert result["filled_count"] == 0
        assert state is not None
        assert state["active_reservations_cny"] == 0.0
        assert state["cumulative_pnl"] == 0.0
        assert sum(row["event_type"] == "reserve" for row in events) == 1
        assert sum(row["event_type"] == "release" for row in events) == 1


def test_full_futures_flatten_releases_position_master_margin() -> None:
    class ClosingReader:
        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return [
                {"bar_time": "2026-07-10 14:50:00", "close": 3_510.0, "volume": 1_000},
                {"bar_time": "2026-07-10 14:55:00", "close": 3_500.0, "volume": 1_000},
            ]

    closed = SimResult(
        status="filled",
        filled_qty=1,
        avg_price=3_500.0,
        fee=4.0,
        message="filled",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-ledger-close",
        market="cn_futures",
        raw_response={
            "margin_required": 0.0,
            "notional": 35_000.0,
            "contract_multiplier": 10,
            "estimated_close_fee": 4.0,
            "total_estimated_fee": 4.0,
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capital_root = root / "capital"
        ledger = _init_market_capital_ledger(capital_root)
        opened = _commit_test_futures_open(ledger)
        positions_path = (
            root / "signals" / "positions" / "cn_futures_sim_positions.json"
        )
        positions_path.parent.mkdir(parents=True)
        positions_path.write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "style": "trend",
                            "symbol": "RB2610.SHF",
                            "net_qty": 1,
                            "avg_price": 3_520.0,
                            "mark_price": 3_500.0,
                            "contract_multiplier": 10,
                            "margin_required": 4_550.0,
                            "cn_futures_capital_reservations": [],
                            "capital_commit_status": "committed",
                            "capital_commit_event_id": opened["commit"].event_id,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(sim_runner, "execute_sim_order", return_value=closed),
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                ClosingReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 14:56:00"),
            )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        assert result["filled_count"] == 1
        assert state is not None
        assert state["active_reservations_cny"] == 0.0
        assert state["margin_used_cny"] == 0.0
        assert state["cumulative_pnl"] == -204.0
        assert state["consecutive_losses"] == 1
        events = [
            json.loads(line)
            for line in (capital_root / "cn_futures_sim_capital_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        close_commit = next(
            row for row in events if row["event_type"] == "position_close_commit"
        )
        assert close_commit["amount_cny"] == -204.0
        assert not any(row["event_type"] == "realized_pnl" for row in events)
        assert (
            json.loads(positions_path.read_text(encoding="utf-8"))["position_count"]
            == 0
        )


def test_executor_exception_releases_market_reservation_and_returns_hold() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capital_root = root / "capital"
        _init_market_capital_ledger(capital_root)
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(
                sim_runner,
                "execute_sim_order",
                side_effect=RuntimeError("executor crashed"),
            ),
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        assert result["filled_count"] == 0
        assert result["holds"][0]["reason"] == "sim_executor_exception"
        assert state is not None
        assert state["active_reservations_cny"] == 0.0


def test_fill_margin_above_reservation_is_not_accepted_as_position() -> None:
    inconsistent_fill = SimResult(
        status="filled",
        filled_qty=1,
        avg_price=3_500.0,
        fee=3.0,
        message="filled",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-invalid-margin",
        market="cn_futures",
        raw_response={
            "margin_required": 6_000.0,
            "notional": 35_000.0,
            "contract_multiplier": 10,
            "open_fee": 3.0,
            "estimated_close_fee": 4.0,
            "total_estimated_fee": 7.0,
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capital_root = root / "capital"
        _init_market_capital_ledger(capital_root)
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(
                sim_runner, "execute_sim_order", return_value=inconsistent_fill
            ),
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        assert result["filled_count"] == 1
        assert any(
            hold["reason"] == "capital_commit_pending" for hold in result["holds"]
        )
        assert state is not None
        assert state["active_reservations_cny"] > 4_550.0
        assert state["margin_used_cny"] == 0.0
        assert (
            root / "signals" / "positions" / "cn_futures_sim_positions.json"
        ).exists()
        assert result["records"][0]["receipt"]["execution_eligible"] is False
        assert (
            result["records"][0]["receipt"]["execution_class"]
            == "capital_commit_pending"
        )


def test_reduce_does_not_release_market_margin_before_position_is_durable() -> None:
    class ClosingReader:
        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return [
                {"bar_time": "2026-07-10 14:50:00", "close": 3_510.0, "volume": 1_000},
                {"bar_time": "2026-07-10 14:55:00", "close": 3_500.0, "volume": 1_000},
            ]

    closed = SimResult(
        status="filled",
        filled_qty=1,
        avg_price=3_500.0,
        fee=4.0,
        message="filled",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-durable-close",
        market="cn_futures",
        raw_response={
            "margin_required": 0.0,
            "notional": 35_000.0,
            "contract_multiplier": 10,
            "estimated_close_fee": 4.0,
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capital_root = root / "capital"
        ledger = _init_market_capital_ledger(capital_root)
        opened = _commit_test_futures_open(ledger)
        positions_path = (
            root / "signals" / "positions" / "cn_futures_sim_positions.json"
        )
        positions_path.parent.mkdir(parents=True)
        positions_path.write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "style": "trend",
                            "symbol": "RB2610.SHF",
                            "net_qty": 1,
                            "avg_price": 3_520.0,
                            "mark_price": 3_500.0,
                            "contract_multiplier": 10,
                            "margin_required": 4_550.0,
                            "cn_futures_capital_reservations": [],
                            "capital_commit_status": "committed",
                            "capital_commit_event_id": opened["commit"].event_id,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(sim_runner, "execute_sim_order", return_value=closed),
            patch.object(
                sim_runner,
                "_write_position_snapshot",
                side_effect=OSError("position write failed"),
            ),
        ):
            with pytest.raises(OSError, match="position write failed"):
                sim_runner.run_multi_style_simulation(
                    _AuthorityAdapter({"sim_capital": 50_000.0}),
                    "20260710",
                    ClosingReader(),
                    signals_dir=root / "signals",
                    review_path=root / "reviews.jsonl",
                    now=datetime.fromisoformat("2026-07-10 14:56:00"),
                )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        assert state is not None
        assert state["active_reservations_cny"] == 0.0
        assert state["margin_used_cny"] == 4_550.0


def test_market_provider_trust_policy_accepts_only_strict_current_state() -> None:
    filled = SimResult(
        status="filled",
        filled_qty=1,
        avg_price=3_500.0,
        fee=7.0,
        message="filled",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-provider-validation",
        market="cn_futures",
        raw_response={
            "margin_required": 4_550.0,
            "notional": 35_000.0,
            "contract_multiplier": 10,
            "open_fee": 3.0,
            "estimated_close_fee": 4.0,
            "total_estimated_fee": 7.0,
        },
    )

    def allows_execution(state: dict[str, object]) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(
                    sim_runner,
                    "get_cn_futures_capital_provider_state",
                    return_value=state,
                    create=True,
                ),
                patch.object(
                    sim_runner,
                    "generate_style_signal",
                    return_value={"action": "buy", "side": "buy", "price": 3_500.0},
                ),
                patch.object(
                    sim_runner,
                    "_reserve_cn_futures_market_margin",
                    side_effect=_approved_market_reservation,
                ),
                patch.object(
                    sim_runner, "execute_sim_order", return_value=filled
                ) as execute,
            ):
                sim_runner.run_multi_style_simulation(
                    _AuthorityAdapter({"sim_capital": 50_000.0}),
                    "20260710",
                    _FreshRunnerReader(),
                    signals_dir=root / "signals",
                    review_path=root / "reviews.jsonl",
                    now=datetime.fromisoformat("2026-07-10 09:36:00"),
                )
            return execute.call_count == 1

    valid = _valid_market_provider_state()
    mtm_state = _valid_market_provider_state(
        equity_cny=49_900.0,
        unrealized_pnl_cny=-100.0,
    )
    invalid_states = [
        _valid_market_provider_state(reconciled=1),
        _valid_market_provider_state(fresh="true"),
        _valid_market_provider_state(authority_generation=99.0),
        _valid_market_provider_state(authority_generation="1"),
        _valid_market_provider_state(authority_generation=True),
        _valid_market_provider_state(authority_generation=2.5),
        _valid_market_provider_state(trade_date="20260709"),
        _valid_market_provider_state(equity_cny=float("nan")),
        _valid_market_provider_state(available_margin=float("inf")),
        _valid_market_provider_state(available_margin=-1.0),
        _valid_market_provider_state(consecutive_losses=0.5),
        _valid_market_provider_state(max_consecutive_losses=True),
        _valid_market_provider_state(initial_equity_cny="50000"),
        _valid_market_provider_state(daily_realized_pnl="0"),
        _valid_market_provider_state(real_trading_enabled=True),
        _valid_market_provider_state(cumulative_pnl=-40_000.0),
        _valid_market_provider_state(max_daily_loss=50_000.0),
        _valid_market_provider_state(max_drawdown=50_000.0),
        _valid_market_provider_state(max_consecutive_losses=999),
        {key: value for key, value in valid.items() if key != "max_drawdown"},
    ]

    assert allows_execution(valid) is True
    accepted_mtm, _ = sim_runner._validate_market_capital_provider_state(
        mtm_state,
        trade_date="20260710",
    )
    assert accepted_mtm is not None
    assert [allows_execution(state) for state in invalid_states] == [False] * len(
        invalid_states
    )


def test_trusted_provider_cannot_mint_capital_or_exceed_futures_allocation() -> None:
    invalid_states = [
        _valid_market_provider_state(
            initial_equity_cny=500_000.0,
            equity_cny=500_000.0,
            high_water_equity=500_000.0,
        ),
        _valid_market_provider_state(
            initial_equity_cny=20_000_000.0,
            equity_cny=20_000_000.0,
            high_water_equity=20_000_000.0,
        ),
        _valid_market_provider_state(margin_utilization_limit_cny=5_001.0),
        _valid_market_provider_state(available_margin=26_000.0),
        _valid_market_provider_state(margin_used_cny=5_001.0),
        _valid_market_provider_state(execution_lineage_id=""),
        _valid_market_provider_state(authority_id="wrong-auth"),
        _valid_market_provider_state(event_id=""),
    ]

    accepted, _ = sim_runner._validate_market_capital_provider_state(
        _valid_market_provider_state(),
        trade_date="20260710",
    )
    rejected = [
        sim_runner._validate_market_capital_provider_state(
            state, trade_date="20260710"
        )[0]
        for state in invalid_states
    ]

    assert accepted is not None
    assert rejected == [None] * len(invalid_states)


def test_market_provider_failure_fails_closed_without_calling_executor() -> None:
    caught: Exception | None = None
    result: dict[str, object] | None = None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with (
            patch.object(
                sim_runner,
                "get_cn_futures_capital_provider_state",
                side_effect=RuntimeError("provider unavailable"),
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(sim_runner, "execute_sim_order") as execute,
        ):
            try:
                result = sim_runner.run_multi_style_simulation(
                    _AuthorityAdapter({"sim_capital": 50_000.0}),
                    "20260710",
                    _FreshRunnerReader(),
                    signals_dir=root / "signals",
                    review_path=root / "reviews.jsonl",
                    now=datetime.fromisoformat("2026-07-10 09:36:00"),
                )
            except Exception as exc:  # pragma: no cover - asserted below
                caught = exc

    assert caught is None
    execute.assert_not_called()
    assert result is not None
    assert result["holds"][0]["reason"] == "account_state_unavailable"
    assert result["account_state"]["history_status"] == "market_capital_provider_error"


def test_account_sidecar_charges_one_leg_fee_per_open_and_close_intent() -> None:
    class Reader:
        latest = "2026-07-10 09:35:00"
        close = 3_500.0

        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return [
                {"bar_time": "2026-07-10 09:30:00", "close": 3_490.0, "volume": 1_000},
                {"bar_time": self.latest, "close": self.close, "volume": 1_000},
            ]

    def receipt(*, price: float, order_id: str) -> SimResult:
        return SimResult(
            status="filled",
            filled_qty=1,
            avg_price=price,
            fee=7.0,
            message="filled",
            capital_layer="simulated",
            account_type="simulated",
            order_id=order_id,
            market="cn_futures",
            raw_response={
                "margin_required": 4_550.0,
                "notional": price * 10,
                "contract_multiplier": 10,
                "open_fee": 3.0,
                "estimated_close_fee": 4.0,
                "total_estimated_fee": 7.0,
            },
        )

    second_state = _valid_market_provider_state(
        equity_cny=49_997.0,
        available_margin=450.0,
        margin_used_cny=4_550.0,
        cumulative_pnl=-3.0,
        daily_realized_pnl=-3.0,
    )
    reader = Reader()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        review_path = root / "reviews.jsonl"
        with (
            patch.object(
                sim_runner,
                "get_cn_futures_capital_provider_state",
                side_effect=[_valid_market_provider_state(), second_state],
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                side_effect=[
                    {"action": "buy", "side": "buy", "price": 3_500.0},
                    {"action": "sell", "side": "sell", "price": 3_510.0},
                ],
            ),
            patch.object(
                sim_runner,
                "execute_sim_order",
                side_effect=[
                    receipt(price=3_500.0, order_id="SIM-CNF-open"),
                    receipt(price=3_510.0, order_id="SIM-CNF-close"),
                ],
            ),
            patch.object(
                sim_runner,
                "_reserve_cn_futures_market_margin",
                side_effect=_approved_market_reservation,
            ),
            patch.object(
                sim_runner,
                "_release_cn_futures_market_margin",
                side_effect=_released_market_reservation,
            ),
            patch.object(
                sim_runner,
                "_record_cn_futures_market_pnl",
                side_effect=lambda **kwargs: {
                    "status": "recorded",
                    "reference_id": kwargs.get("reference_id", ""),
                    "amount_cny": kwargs.get("amount_cny", 0.0),
                },
            ),
        ):
            sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                reader,
                signals_dir=root / "signals",
                review_path=review_path,
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )
            account_path = root / "cn_futures_account_state.json"
            after_open = json.loads(account_path.read_text(encoding="utf-8"))

            reader.latest = "2026-07-10 09:40:00"
            reader.close = 3_510.0
            sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                reader,
                signals_dir=root / "signals",
                review_path=review_path,
                now=datetime.fromisoformat("2026-07-10 09:41:00"),
            )
            after_close = json.loads(account_path.read_text(encoding="utf-8"))

    assert after_open["cumulative_pnl"] == -3.0
    assert after_close["cumulative_pnl"] == 93.0


def test_affordability_sidecar_records_each_pre_sizing_contract_rejection() -> None:
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def __init__(self, *, symbol: str, style: dict[str, object]) -> None:
            self.symbol = symbol
            self.style = style

        def get_strategy_config(self) -> dict[str, object]:
            return {"styles": {"trend": self.style}}

        def get_sim_account(self) -> dict[str, object]:
            return {"sim_capital": 50_000.0}

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return [self.symbol]

    class Reader:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return list(self.rows)

    def run_case(
        *,
        symbol: str = "RB2610.SHF",
        rows: list[dict[str, object]],
        style: dict[str, object] | None = None,
        date: str = "20260710",
        now: str = "2026-07-10 09:36:00",
        signal_price: float = 3_500.0,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": signal_price},
            ):
                return sim_runner.run_multi_style_simulation(
                    Adapter(
                        symbol=symbol,
                        style=style
                        or {
                            "risk_per_trade": 0.10,
                            "max_margin_usage": 0.30,
                            "weight": 1.0,
                        },
                    ),
                    date,
                    Reader(rows),
                    signals_dir=root / "signals",
                    review_path=root / "reviews.jsonl",
                    now=datetime.fromisoformat(now),
                )

    missing = run_case(rows=[])
    stale = run_case(
        rows=[
            {"bar_time": "2026-07-10 09:20:00", "close": 3_490.0, "volume": 1_000},
            {"bar_time": "2026-07-10 09:25:00", "close": 3_500.0, "volume": 1_000},
        ]
    )
    rollover = run_case(
        symbol="RB2607.SHF",
        rows=[
            {"bar_time": "2026-07-03 14:50:00", "close": 3_490.0, "volume": 1_000},
            {"bar_time": "2026-07-03 14:55:00", "close": 3_500.0, "volume": 1_000},
        ],
        style={
            "risk_per_trade": 0.10,
            "rollover_min_days_to_contract_month_start": 5,
        },
        date="20260703",
        now="2026-07-03 14:56:00",
    )
    invalid_price = run_case(
        rows=[
            {"bar_time": "2026-07-10 09:30:00", "close": 3_490.0, "volume": 1_000},
            {"bar_time": "2026-07-10 09:35:00", "close": 3_500.0, "volume": 1_000},
        ],
        signal_price=0.0,
    )

    results = [missing, stale, rollover, invalid_price]
    expected_reasons = [
        "missing_intraday_bars",
        "stale_intraday_bar",
        "contract_rollover_guard",
        "invalid_price",
    ]
    assert [
        result["affordability"]["contracts"][0]["reason"] for result in results
    ] == expected_reasons
    assert all(
        result["affordability"]["contracts"][0]["execution_class"]
        == "counterfactual_only"
        for result in results
    )


def test_runner_allows_exact_reduce_only_without_account_authority() -> None:
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {
                "styles": {"trend": {"risk_per_trade": 0.01, "no_overnight": False}}
            }

        def get_sim_account(self) -> dict[str, object]:
            return {"sim_capital": 50_000.0}

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    class Reader:
        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return [
                {"bar_time": "2026-07-10 09:30:00", "close": 3_510.0, "volume": 1_000},
                {"bar_time": "2026-07-10 09:35:00", "close": 3_500.0, "volume": 1_000},
            ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        positions_path = (
            root / "signals" / "positions" / "cn_futures_sim_positions.json"
        )
        positions_path.parent.mkdir(parents=True)
        positions_path.write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "style": "trend",
                            "symbol": "RB2610.SHF",
                            "net_qty": 2,
                            "avg_price": 3_520.0,
                            "mark_price": 3_500.0,
                            "contract_multiplier": 10,
                            "margin_required": 9_100.0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with patch.object(
            sim_runner,
            "generate_style_signal",
            return_value={"action": "sell", "side": "sell", "price": 3_500.0},
        ):
            result = sim_runner.run_multi_style_simulation(
                Adapter(),
                "20260710",
                Reader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

        assert result["filled_count"] == 1
        assert result["records"][0]["order"]["quantity"] == 2
        assert result["records"][0]["order"]["intent"] == "reduce_only"
        assert (
            result["records"][0]["size_decision"]["reason"]
            == "reduce_only_existing_position"
        )
        snapshot = json.loads(positions_path.read_text(encoding="utf-8"))
        assert snapshot["position_count"] == 0


def test_force_flatten_still_requires_fresh_fill_evidence() -> None:
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {
                "styles": {
                    "trend": {
                        "risk_per_trade": 0.01,
                        "no_overnight": True,
                        "flatten_before_session_close_minutes": 10,
                    }
                }
            }

        def get_sim_account(self) -> dict[str, object]:
            return {"sim_capital": 50_000.0}

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    class Reader:
        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return [
                {"bar_time": "2026-07-10 14:25:00", "close": 3_510.0, "volume": 1_000},
                {"bar_time": "2026-07-10 14:30:00", "close": 3_500.0, "volume": 1_000},
            ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        positions_path = (
            root / "signals" / "positions" / "cn_futures_sim_positions.json"
        )
        positions_path.parent.mkdir(parents=True)
        positions_path.write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "style": "trend",
                            "symbol": "RB2610.SHF",
                            "net_qty": 1,
                            "avg_price": 3_520.0,
                            "mark_price": 3_500.0,
                            "contract_multiplier": 10,
                            "margin_required": 4_550.0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with patch.object(sim_runner, "execute_sim_order") as execute:
            result = sim_runner.run_multi_style_simulation(
                Adapter(),
                "20260710",
                Reader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 14:56:00"),
                max_intraday_bar_age_minutes=10,
            )

        execute.assert_not_called()
        assert result["filled_count"] == 0
        assert result["errors"][0]["error"] == "stale_intraday_bar"


def test_rejected_executor_status_is_not_written_as_filled_signal() -> None:
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {"styles": {"trend": {"risk_per_trade": 0.10, "no_overnight": True}}}

        def get_sim_account(self) -> dict[str, object]:
            return {
                "sim_capital": 50_000.0,
                "account_state_authoritative": True,
                "account_state": _valid_market_provider_state(),
            }

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    class Reader:
        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return [
                {"bar_time": "2026-07-10 09:30:00", "close": 3_490.0, "volume": 1_000},
                {"bar_time": "2026-07-10 09:35:00", "close": 3_500.0, "volume": 1_000},
            ]

    rejection = SimResult(
        status="rejected",
        filled_qty=0,
        avg_price=0.0,
        fee=0.0,
        message="rejected",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-rejected",
        market="cn_futures",
        raw_response={"reason": "price_limit_guard", "real_trading_enabled": False},
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with (
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(
                sim_runner,
                "get_cn_futures_capital_provider_state",
                return_value=_valid_market_provider_state(),
            ),
            patch.object(
                sim_runner,
                "_reserve_cn_futures_market_margin",
                side_effect=_approved_market_reservation,
            ),
            patch.object(
                sim_runner,
                "_release_cn_futures_market_margin",
                side_effect=_released_market_reservation,
            ),
            patch.object(sim_runner, "execute_sim_order", return_value=rejection),
        ):
            result = sim_runner.run_multi_style_simulation(
                Adapter(),
                "20260710",
                Reader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

        assert result["filled_count"] == 0
        assert result["holds"][0]["reason"] == "price_limit_guard"
        assert list((root / "signals" / "filled").glob("*.json")) == []


def test_replay_affordability_is_counterfactual_without_account_authority() -> None:
    annotation = _execution_annotation(
        symbol="RB2610.SHF",
        style={
            "name": "trend",
            "capital": 200_000.0,
            "risk_per_trade": 0.10,
            "max_margin_usage": 0.30,
            "weight": 1.0,
            "products": ["rb"],
            "no_overnight": True,
        },
        action="buy",
        price=3_500.0,
        bar_time="2026-07-10 09:35:00",
    )

    assert annotation["execution_eligible"] is False
    assert annotation["execution_reason"] == "account_state_unavailable"
    assert annotation["counterfactual_only"] is True
    assert annotation["size_decision"]["counterfactual_eligible"] is True


def test_account_authority_requires_current_reconciled_epoch() -> None:
    variants = [
        _valid_market_provider_state(reconciled=False),
        _valid_market_provider_state(fresh=False),
        _valid_market_provider_state(authority_generation=99),
        _valid_market_provider_state(trade_date="20260710"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for index, state in enumerate(variants):
            account = {
                "sim_capital": 50_000.0,
                "account_state_authoritative": True,
                "account_state": state,
            }
            ledger, _ = sim_runner._load_sim_account_ledger(
                account=account,
                capital=50_000.0,
                date="20260710",
                review_path=Path(tmp) / f"review-{index}.jsonl",
            )
            current = sim_runner._current_account_state(
                account=account,
                ledger=ledger,
                position_snapshot={"positions": []},
            )
            assert current["authoritative"] is False
            assert current["counterfactual_only"] is True


def test_authoritative_sizing_requires_all_immutable_gate_fields() -> None:
    decision = sim_runner.quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        account_state={
            "authoritative": True,
            "equity": 500_000.0,
            "available_margin": 500_000.0,
        },
        style={"risk_per_trade": 0.10, "max_margin_usage": 0.30, "weight": 1.0},
        exit_plan={"stop_loss_pct": 0.01},
        session={"can_hold_overnight": False},
    )

    assert decision["quantity"] == 0
    assert decision["reason"] == "missing_account_risk_gates"
    assert decision["counterfactual_only"] is True


def test_cn_futures_capital_outbox_persists_before_dispatch_and_retries_until_done() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        signals_dir = Path(tmp) / "signals"
        action = sim_runner._queue_cn_futures_capital_action(
            signals_dir,
            action="realized_pnl",
            reference_id="ORDER-1:pnl",
            amount_cny=-3.0,
            trade_date="20260710",
            affects_loss_streak=False,
        )
        outbox_path = signals_dir / "capital" / "cn_futures_capital_outbox.json"

        def fail_after_observing_durable_action(**kwargs: object) -> dict[str, object]:
            durable = json.loads(outbox_path.read_text(encoding="utf-8"))
            assert durable["actions"][0]["status"] == "pending"
            assert durable["actions"][0]["action_id"] == action["action_id"]
            return {"status": "market_capital_pnl_error"}

        with patch.object(
            sim_runner,
            "_record_cn_futures_market_pnl",
            side_effect=[
                fail_after_observing_durable_action(),
                {"status": "recorded", "event_id": "MCAP-PNL-1"},
            ],
        ) as record:
            first = sim_runner._dispatch_cn_futures_capital_outbox(signals_dir)
            second = sim_runner._dispatch_cn_futures_capital_outbox(signals_dir)
            third = sim_runner._dispatch_cn_futures_capital_outbox(signals_dir)

        durable = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert first["pending_count"] == 1
        assert second["pending_count"] == 0
        assert third["pending_count"] == 0
        assert durable["actions"][0]["status"] == "completed"
        assert durable["actions"][0]["result"]["status"] == "recorded"
        assert record.call_count == 2


def test_run_startup_does_not_import_legacy_pending_release_into_fresh_ledger() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        signals_dir = root / "signals"
        capital_root = root / "capital"
        ledger = _init_market_capital_ledger(capital_root)
        reservation = ledger.reserve(
            MarketCapitalReservationRequest(
                market="cn_futures",
                reference_id="OPEN-BEFORE-CRASH",
                risk_unit_key="RB2610.SHF",
                worst_case_amount_cny=1_000.0,
                authority_id="cn-futures-capital-v1",
                authority_generation=1,
                trade_date="20260710",
                point_in_time_as_of="2026-07-10T09:35:00+08:00",
                lineage_sha256="0" * 64,
                execution_lineage_id="test-lineage-20260710-0001",
            )
        )
        positions_path = signals_dir / "positions" / "cn_futures_sim_positions.json"
        positions_path.parent.mkdir(parents=True)
        positions_path.write_text(
            json.dumps(
                {
                    "positions": [],
                    "pending_capital_releases": [
                        {
                            "order_id": "CLOSE-BEFORE-CRASH",
                            "symbol": "RB2610.SHF",
                            "style": "trend",
                            "reservations": [
                                {
                                    "reservation_id": reservation.reservation_id,
                                    "event_id": reservation.event_id,
                                    "amount_cny": 1_000.0,
                                }
                            ],
                            "reason": "closed_position_capital_release_pending",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(
            "os.environ",
            {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=signals_dir,
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 16:00:00"),
            )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        durable_positions = json.loads(positions_path.read_text(encoding="utf-8"))
        assert result["state"] == "market_closed"
        assert state is not None
        assert state["active_reservations_cny"] == 1_000.0
        assert len(durable_positions["pending_capital_releases"]) == 1
        assert not (signals_dir / "capital" / "cn_futures_capital_outbox.json").exists()
        assert result["capital_outbox"]["pending_count"] == 1
        assert result["capital_outbox"]["fresh_atomic_commits_only"] is False


def test_pending_capital_outbox_blocks_new_risk_but_not_observation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        signals_dir = root / "signals"
        sim_runner._queue_cn_futures_capital_action(
            signals_dir,
            action="realized_pnl",
            reference_id="CRASHED-FILL:pnl",
            amount_cny=-3.0,
            trade_date="20260710",
            affects_loss_streak=False,
        )
        with (
            patch.object(
                sim_runner,
                "_record_cn_futures_market_pnl",
                return_value={"status": "market_capital_pnl_error"},
            ),
            patch.object(
                sim_runner,
                "get_cn_futures_capital_provider_state",
                return_value=_valid_market_provider_state(),
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(sim_runner, "_reserve_cn_futures_market_margin") as reserve,
            patch.object(sim_runner, "execute_sim_order") as execute,
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=signals_dir,
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

        reserve.assert_not_called()
        execute.assert_not_called()
        assert result["record_count"] == 0
        assert result["holds"][0]["reason"] == "account_state_unavailable"
        assert result["holds"][0]["size_decision"]["account_history_status"] == (
            "capital_outbox_pending"
        )


def test_corrupt_position_snapshot_fails_closed_before_provider_or_execution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        positions_path = (
            root / "signals" / "positions" / "cn_futures_sim_positions.json"
        )
        positions_path.parent.mkdir(parents=True)
        positions_path.write_text("{corrupt\n", encoding="utf-8")

        with (
            patch.object(
                sim_runner, "get_cn_futures_capital_provider_state"
            ) as provider,
            patch.object(sim_runner, "execute_sim_order") as execute,
        ):
            with pytest.raises(
                RuntimeError, match="cn_futures_position_snapshot_unreadable"
            ):
                sim_runner.run_multi_style_simulation(
                    _AuthorityAdapter({"sim_capital": 50_000.0}),
                    "20260710",
                    _FreshRunnerReader(),
                    signals_dir=root / "signals",
                    review_path=root / "reviews.jsonl",
                    now=datetime.fromisoformat("2026-07-10 09:36:00"),
                )

        provider.assert_not_called()
        execute.assert_not_called()


def test_position_snapshot_checksum_rejects_valid_json_tampering() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        signals_dir = Path(tmp) / "signals"
        sim_runner._write_position_snapshot(
            signals_dir,
            {"positions": [], "pending_capital_releases": []},
        )
        path = signals_dir / "positions" / "cn_futures_sim_positions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["payload_sha256"] == sim_runner._runtime_payload_sha256(payload)
        payload["total_margin_required"] = 9_999.0
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with pytest.raises(
            RuntimeError,
            match="cn_futures_position_snapshot_checksum_mismatch",
        ):
            sim_runner._read_position_snapshot(signals_dir)


def test_independent_50k_futures_state_uses_five_and_seven_percent_drawdown() -> None:
    state = sim_runner._current_account_state(
        account={},
        ledger={
            "base_capital": 50_000.0,
            "cumulative_pnl": -2_500.0,
            "daily_realized_pnl": 0.0,
            "consecutive_losses": 0,
            "high_water_equity": 50_000.0,
            "history_complete": False,
            "authority": "none",
        },
        position_snapshot={"positions": []},
    )

    assert state["drawdown"] == 2_500.0
    assert state["drawdown_tighten"] == 2_500.0
    assert state["max_drawdown"] == 3_500.0
    assert state["drawdown_tightened"] is True
    assert state["risk_multiplier"] == 0.75


def test_runner_session_enum_matches_acceptance_contract() -> None:
    assert (
        sim_runner._session_bucket(datetime.fromisoformat("2026-07-10 09:36:00"))
        == "day_morning"
    )
    assert (
        sim_runner._session_bucket(datetime.fromisoformat("2026-07-10 14:36:00"))
        == "day_afternoon"
    )
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-10 21:36:00"),
            symbol="RB2610.SHF",
        )
        == "night"
    )


def test_session_bucket_rejects_weekends_and_requires_product_night_session() -> None:
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-11 09:36:00"),
            symbol="RB2610.SHF",
        )
        == "closed"
    )
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-12 21:05:00"),
            symbol="CU2609.SHF",
        )
        == "closed"
    )
    assert (
        sim_runner._session_bucket(datetime.fromisoformat("2026-07-10 21:05:00"))
        == "closed"
    )
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-10 21:05:00"),
            symbol="IF2609.CFX",
        )
        == "closed"
    )
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-10 21:05:00"),
            symbol="RB2610.SHF",
        )
        == "night"
    )
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-10 23:05:00"),
            symbol="RB2610.SHF",
        )
        == "closed"
    )
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-09 23:05:00"),
            symbol="CU2609.SHF",
        )
        == "night"
    )
    assert (
        sim_runner._session_bucket(
            datetime.fromisoformat("2026-07-10 01:05:00"),
            symbol="CU2609.SHF",
        )
        == "closed"
    )


def test_night_exchange_trade_date_skips_weekend() -> None:
    assert (
        sim_runner._exchange_trade_date(datetime.fromisoformat("2026-07-09 21:05:00"))
        == "20260710"
    )
    assert (
        sim_runner._exchange_trade_date(datetime.fromisoformat("2026-07-10 21:05:00"))
        == "20260713"
    )
    assert (
        sim_runner._exchange_trade_date(datetime.fromisoformat("2026-07-10 01:00:00"))
        == "20260710"
    )


def test_runner_uses_exchange_trade_date_not_calendar_next_day_at_friday_night() -> (
    None
):
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {"styles": {"trend": {"enabled": True}}}

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    class MissingReader:
        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.object(
            sim_runner,
            "get_cn_futures_capital_provider_state",
            return_value=_valid_market_provider_state(trade_date="20260713"),
        ) as provider:
            result = sim_runner.run_multi_style_simulation(
                Adapter(),
                "20260711",  # Legacy calendar-next-day input must not survive.
                MissingReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 21:05:00"),
            )

    provider.assert_called_once_with(trade_date="20260713")
    assert result["date"] == "20260713"
    assert result["session"] == "night"


def test_missing_market_data_emits_structured_session_risk_rejection() -> None:
    class MissingReader:
        def get_bars_intraday(
            self, *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            return []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.object(
            sim_runner,
            "get_cn_futures_capital_provider_state",
            return_value=_valid_market_provider_state(),
        ):
            result = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                MissingReader(),
                signals_dir=root / "signals",
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 09:36:00"),
            )

        rejection = result["holds"][0]
        assert result["session"] == "day_morning"
        assert rejection["record_type"] == "risk_reject"
        assert rejection["session"] == "day_morning"
        assert rejection["reason"] == "missing_intraday_bars"
        assert rejection["counterfactual_only"] is True
        assert rejection["label_status"] == "rejected_data_unreliable"


# ---------------------------------------------------------------------------
# P0: Point-in-time / 5-minute clustering – RED tests
# ---------------------------------------------------------------------------


def test_forward_outcome_label_explicit_as_of_guards_future_leakage() -> None:
    """_forward_outcome_label must accept point_in_time_as_of and reject
    bar timestamps beyond it, returning pending/rejected instead of
    reading future data."""
    bars = [
        {"bar_time": "2026-07-10 09:30:00", "close": 3490.0, "volume": 1000},
        {"bar_time": "2026-07-10 09:35:00", "close": 3500.0, "volume": 1000},
        {"bar_time": "2026-07-10 09:40:00", "close": 3510.0, "volume": 1000},
        {"bar_time": "2026-07-10 09:45:00", "close": 3520.0, "volume": 1000},
    ]
    signal = {"side": "buy", "price": 3500.0}
    exit_plan = {"prediction_horizon_bars": 3}

    # as_of at 09:35:00 – only bars up to index 1 are visible
    outcome = sim_runner._forward_outcome_label(
        bars,
        signal,
        exit_plan,
        point_in_time_as_of="2026-07-10 09:35:00",
    )
    assert outcome["status"] == "pending_future_bars", (
        f"Expected pending_future_bars, got {outcome.get('status')}"
    )
    assert "point_in_time_as_of" in outcome

    # as_of at 09:30:00 – only first bar visible, not enough for entry
    outcome2 = sim_runner._forward_outcome_label(
        bars,
        signal,
        exit_plan,
        point_in_time_as_of="2026-07-10 09:30:00",
    )
    assert outcome2["status"] in {"pending_future_bars", "unscored"}


def test_prediction_snapshot_includes_pit_lineage_fields() -> None:
    """_prediction_snapshot_before_risk must carry point_in_time_as_of,
    source_event_time, source_snapshot_id, source_snapshot_sha256,
    authority, and lineage_status."""
    style = {"style_version": "trend-v8"}
    signal = {
        "side": "buy",
        "action": "buy",
        "price": 3500.0,
        "directional_score": 0.015,
        "momentum": 0.01,
        "confidence": 0.75,
        "score": 0.015,
        "probability": 0.75,
        "scenario_tags": {"volatility_bucket": "medium"},
    }
    scenario_tags = {"time_bucket": "morning"}
    exit_plan = {
        "prediction_horizon_bars": 3,
        "time_stop_bars": 3,
        "max_hold_bars": 6,
        "no_overnight": True,
    }
    forward_outcome = {"status": "pending_future_bars"}

    snapshot = sim_runner._prediction_snapshot_before_risk(
        style_name="trend",
        style=style,
        signal=signal,
        scenario_tags=scenario_tags,
        exit_plan=exit_plan,
        forward_outcome=forward_outcome,
        bar_time="2026-07-10 09:35:00",
        authority="market_capital_ledger",
        symbol="RB2610.SHF",
        source_name="sharedsignals_futures_bars",
        source_cadence="5min",
        source_bars=[
            {
                "symbol": "RB2610.SHF",
                "trade_date": "20260710",
                "bar_time": "2026-07-10 09:35:00",
                "open": 3498.0,
                "high": 3502.0,
                "low": 3495.0,
                "close": 3500.0,
                "volume": 1000,
            }
        ],
    )

    assert "point_in_time_as_of" in snapshot
    assert snapshot["point_in_time_as_of"] == "2026-07-10T09:35:00+08:00"
    assert snapshot["source_event_time"] == "2026-07-10T09:35:00+08:00"
    assert "source_snapshot_id" in snapshot
    assert "source_snapshot_sha256" in snapshot
    assert "authority" in snapshot
    assert snapshot["authority"] == "market_capital_ledger"
    assert "lineage_status" in snapshot
    assert snapshot["lineage_status"] == "complete"
    assert snapshot["raw_heuristic_score"] == 0.015
    assert snapshot["uncalibrated_confidence_prior"] == 0.75
    assert snapshot["calibrated_probability"] is None
    assert snapshot["probability_model_state"] == "not_calibrated"
    assert "probability" not in snapshot
    assert "style_version" in snapshot
    assert snapshot["source_name"] == "sharedsignals_futures_bars"
    assert snapshot["source_symbol"] == "RB2610.SHF"
    assert snapshot["source_bar_count"] == 1
    assert snapshot["source_rule_version"]
    assert snapshot["evidence_envelope_validation"]["status"] == (
        "missing_receipt_timestamps"
    )
    assert snapshot["point_in_time_lineage"] == {}


def test_prediction_snapshot_persists_real_provider_receipt_lineage() -> None:
    snapshot = sim_runner._prediction_snapshot_before_risk(
        style_name="trend",
        style={"style_version": "trend-v8"},
        signal={
            "side": "buy",
            "action": "buy",
            "price": 3500.0,
            "score": 0.015,
            "probability": 0.75,
        },
        scenario_tags={"market_regime": "directional_up"},
        exit_plan={
            "prediction_horizon_bars": 3,
            "time_stop_bars": 3,
            "max_hold_bars": 6,
            "no_overnight": True,
        },
        forward_outcome={"status": "pending_future_bars"},
        bar_time="2026-07-10 09:35:00",
        authority="market_capital_ledger",
        symbol="RB2610.SHF",
        source_name="sharedsignals_futures_bars",
        source_cadence="5min",
        source_bars=[
            {
                "symbol": "RB2610.SHF",
                "bar_time": "2026-07-10 09:35:00",
                "close": 3500.0,
                "available_at": "2026-07-10T09:35:01+08:00",
                "ingested_at": "2026-07-10T09:35:02+08:00",
                "retrieved_as_of": "2026-07-10T09:35:03+08:00",
            }
        ],
    )

    assert snapshot["evidence_envelope_validation"]["status"] == "valid"
    assert snapshot["point_in_time_lineage"]["complete"] is True
    assert snapshot["point_in_time_as_of"] == "2026-07-10T01:35:03+00:00"
    assert snapshot["source_event_time"] == "2026-07-10T09:35:00+08:00"
    retrieval = snapshot["evidence_envelope"]["retrieval_time_fields"]
    assert retrieval["retrieved_as_of"] == "2026-07-10T09:35:03+08:00"


def test_prediction_snapshot_hash_binds_bar_source_signal_style_rule_and_pit() -> None:
    style = {"style_version": "trend-v8", "risk_per_trade": 0.01}
    signal = {
        "side": "buy",
        "action": "buy",
        "price": 3500.0,
        "score": 0.015,
        "probability": 0.75,
    }
    source_bar = {
        "symbol": "RB2610.SHF",
        "trade_date": "20260710",
        "bar_time": "2026-07-10 09:35:00",
        "open": 3498.0,
        "high": 3502.0,
        "low": 3495.0,
        "close": 3500.0,
        "volume": 1000,
    }
    common = {
        "style_name": "trend",
        "style": style,
        "signal": signal,
        "scenario_tags": {"market_regime": "directional_up"},
        "exit_plan": {
            "prediction_horizon_bars": 3,
            "time_stop_bars": 3,
            "max_hold_bars": 6,
            "no_overnight": True,
        },
        "forward_outcome": {"status": "pending_future_bars"},
        "bar_time": "2026-07-10 09:35:00",
        "authority": "market_capital_ledger",
        "symbol": "RB2610.SHF",
        "source_name": "sharedsignals_futures_bars",
        "source_cadence": "5min",
        "source_bars": [source_bar],
    }

    baseline = sim_runner._prediction_snapshot_before_risk(**common)
    repeated = sim_runner._prediction_snapshot_before_risk(**common)
    assert baseline["source_snapshot_sha256"] == repeated["source_snapshot_sha256"]
    assert baseline["source_snapshot_id"] == repeated["source_snapshot_id"]

    variants = [
        {**common, "source_bars": [{**source_bar, "close": 3501.0}]},
        {**common, "source_name": "another_market_source"},
        {**common, "signal": {**signal, "score": 0.016}},
        {**common, "style": {**style, "style_version": "trend-v9"}},
        {
            **common,
            "bar_time": "2026-07-10 09:40:00",
            "source_bars": [
                {
                    **source_bar,
                    "bar_time": "2026-07-10 09:40:00",
                }
            ],
        },
    ]
    hashes = {
        sim_runner._prediction_snapshot_before_risk(**variant)["source_snapshot_sha256"]
        for variant in variants
    }
    assert baseline["source_snapshot_sha256"] not in hashes
    assert len(hashes) == len(variants)


def test_prediction_snapshot_lineage_is_incomplete_without_immutable_source_bars() -> (
    None
):
    snapshot = sim_runner._prediction_snapshot_before_risk(
        style_name="trend",
        style={"style_version": "trend-v8"},
        signal={
            "side": "buy",
            "action": "buy",
            "price": 3500.0,
            "score": 0.015,
            "probability": 0.75,
        },
        scenario_tags={"market_regime": "directional_up"},
        exit_plan={
            "prediction_horizon_bars": 3,
            "time_stop_bars": 3,
            "max_hold_bars": 6,
            "no_overnight": True,
        },
        forward_outcome={"status": "pending_future_bars"},
        bar_time="2026-07-10 09:35:00",
        authority="market_capital_ledger",
        symbol="RB2610.SHF",
        source_name="sharedsignals_futures_bars",
        source_cadence="5min",
        source_bars=[],
    )

    assert snapshot["lineage_status"] == "incomplete"
    assert "source_bars" in snapshot["lineage_missing_fields"]


def test_missing_authority_marks_lineage_incomplete() -> None:
    """When authority is not provided, lineage_status must be 'incomplete'
    and execution_eligible must be blocked downstream."""
    style = {"style_version": "trend-v8"}
    signal = {
        "side": "buy",
        "action": "buy",
        "price": 3500.0,
        "directional_score": 0.015,
        "momentum": 0.01,
        "confidence": 0.75,
        "score": 0.015,
        "probability": 0.75,
        "scenario_tags": {"volatility_bucket": "medium"},
    }
    scenario_tags = {"time_bucket": "morning"}
    exit_plan = {
        "prediction_horizon_bars": 3,
        "time_stop_bars": 3,
        "max_hold_bars": 6,
        "no_overnight": True,
    }
    forward_outcome = {"status": "pending_future_bars"}

    snapshot = sim_runner._prediction_snapshot_before_risk(
        style_name="trend",
        style=style,
        signal=signal,
        scenario_tags=scenario_tags,
        exit_plan=exit_plan,
        forward_outcome=forward_outcome,
        bar_time="2026-07-10 09:35:00",
    )

    assert snapshot["authority"] == ""
    assert snapshot["lineage_status"] == "incomplete"


def test_five_minute_cluster_id_computation() -> None:
    """Same authority+symbol+style_version+side+5min_bucket must yield
    identical cluster_id; different buckets must differ."""
    args = dict(
        authority="market_capital_ledger",
        symbol="RB2610.SHF",
        style_version="trend-v8",
        side="buy",
    )
    id1 = sim_runner._compute_cluster_id(
        bar_time="2026-07-10 09:33:00",
        **args,
    )
    id2 = sim_runner._compute_cluster_id(
        bar_time="2026-07-10 09:34:59",
        **args,
    )
    id3 = sim_runner._compute_cluster_id(
        bar_time="2026-07-10 09:35:00",
        **args,
    )
    id4 = sim_runner._compute_cluster_id(
        bar_time="2026-07-10 09:37:00",
        **args,
    )

    # Same 5-min bucket (09:30-09:35) should match
    assert id1 == id2, "09:33 and 09:34:59 should be same bucket"
    # Different bucket (09:35-09:40) should differ
    assert id1 != id3, "09:33 and 09:35:00 should be different buckets"
    # 09:35 and 09:37 should be same bucket
    assert id3 == id4, "09:35 and 09:37 should be same bucket"


def test_cluster_dedup_marks_duplicate_prediction() -> None:
    """Consecutive predictions in the same cluster must get cluster_duplicate
    marker; first prediction must be cluster_origin."""

    # Simulate cluster tracking state
    cluster_state: dict[str, dict[str, object]] = {}
    authority = "market_capital_ledger"
    symbol = "RB2610.SHF"
    style_version = "trend-v8"
    side = "buy"

    cid = sim_runner._compute_cluster_id(
        authority=authority,
        symbol=symbol,
        style_version=style_version,
        side=side,
        bar_time="2026-07-10 09:33:00",
    )

    # First occurrence – origin
    cluster_info1 = sim_runner._classify_cluster_occurrence(
        cluster_state,
        cid,
        is_execution_eligible=False,
    )
    assert cluster_info1["cluster_role"] == "origin"
    assert cluster_info1["occurrence_index"] == 0

    # Second occurrence in same cluster – duplicate
    cluster_info2 = sim_runner._classify_cluster_occurrence(
        cluster_state,
        cid,
        is_execution_eligible=False,
    )
    assert cluster_info2["cluster_role"] == "duplicate"
    assert cluster_info2["occurrence_index"] == 1
    assert cluster_info2["weight_multiplier"] < 1.0


def test_execution_eligible_requires_pit_lineage() -> None:
    """_receipt_execution_eligible must return False when PIT lineage
    is incomplete, even if all other evidence is present."""
    receipt = {
        "status": "filled",
        "filled_qty": 1,
        "avg_price": 3500.0,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "raw_response": {
            "real_trading_enabled": False,
            "fill_evidence_type": "bar_volume_participation",
            "evidence_timestamp": "2026-07-10T09:35:00+08:00",
            "margin_required": 4550.0,
            "contract_multiplier": 10,
        },
    }
    # Missing PIT proof is never execution-eligible.
    eligible_no_flag = sim_runner._receipt_execution_eligible(receipt)
    assert eligible_no_flag is False

    # With pit_lineage_complete=False, must be False
    receipt["pit_lineage_complete"] = False
    eligible_blocked = sim_runner._receipt_execution_eligible(receipt)
    assert eligible_blocked is False

    # With pit_lineage_complete=True, must be True
    receipt["pit_lineage_complete"] = True
    eligible_ok = sim_runner._receipt_execution_eligible(receipt)
    assert eligible_ok is True


def test_crash_after_durable_position_before_outbox_queue_recovers_atomic_open() -> (
    None
):
    filled = SimResult(
        status="filled",
        filled_qty=1,
        avg_price=3_500.0,
        fee=7.0,
        message="filled",
        capital_layer="simulated",
        account_type="simulated",
        order_id="SIM-CNF-crash-open",
        market="cn_futures",
        raw_response={
            "margin_required": 4_550.0,
            "notional": 35_000.0,
            "contract_multiplier": 10,
            "open_fee": 3.0,
            "estimated_close_fee": 4.0,
            "total_estimated_fee": 7.0,
            "fill_evidence_type": "bar_volume_participation",
            "evidence_timestamp": "2026-07-10T09:35:00+08:00",
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        signals_dir = root / "signals"
        capital_root = root / "capital"
        _init_market_capital_ledger(capital_root)
        with (
            patch.dict(
                "os.environ",
                {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
            ),
            patch.object(
                sim_runner,
                "generate_style_signal",
                return_value={"action": "buy", "side": "buy", "price": 3_500.0},
            ),
            patch.object(sim_runner, "execute_sim_order", return_value=filled),
            patch.object(
                sim_runner,
                "_queue_cn_futures_capital_action",
                side_effect=RuntimeError("crash-before-outbox-queue"),
            ),
        ):
            with pytest.raises(RuntimeError, match="crash-before-outbox-queue"):
                sim_runner.run_multi_style_simulation(
                    _AuthorityAdapter({"sim_capital": 50_000.0}),
                    "20260710",
                    _FreshRunnerReader(),
                    signals_dir=signals_dir,
                    review_path=root / "reviews.jsonl",
                    now=datetime.fromisoformat("2026-07-10 09:36:00"),
                )

        pending_position = json.loads(
            (signals_dir / "positions" / "cn_futures_sim_positions.json").read_text(
                encoding="utf-8"
            )
        )
        assert len(pending_position["pending_capital_commits"]) == 1
        assert pending_position["positions"][0]["capital_commit_status"] == "pending"
        assert not (signals_dir / "capital" / "cn_futures_capital_outbox.json").exists()

        with patch.dict(
            "os.environ",
            {"TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT": str(capital_root)},
        ):
            recovered = sim_runner.run_multi_style_simulation(
                _AuthorityAdapter({"sim_capital": 50_000.0}),
                "20260710",
                _FreshRunnerReader(),
                signals_dir=signals_dir,
                review_path=root / "reviews.jsonl",
                now=datetime.fromisoformat("2026-07-10 16:00:00"),
            )
            state = load_market_capital_provider_state("cn_futures", "20260710")

        durable = json.loads(
            (signals_dir / "positions" / "cn_futures_sim_positions.json").read_text(
                encoding="utf-8"
            )
        )
        assert recovered["state"] == "market_closed"
        assert recovered["capital_outbox"]["pending_count"] == 0
        assert state is not None
        assert state["active_reservations_cny"] == 0.0
        assert state["margin_used_cny"] == 4_550.0
        assert state["cumulative_pnl"] == -3.0
        assert durable["pending_capital_commits"] == []
        assert durable["positions"][0]["capital_commit_status"] == "committed"
