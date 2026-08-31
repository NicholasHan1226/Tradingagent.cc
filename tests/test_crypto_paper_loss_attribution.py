from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from Crypto.paper_loss_attribution import attribute_snapshot, read_attribution
from Crypto.round_trip_capital import ROUND_TRIP_CAPITAL_POLICY
from tests.test_crypto_delayed_paper_round_trip_health import _completed_round_trip, _tree_bytes
from tests.test_crypto_5m_support import WINDOW_END

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def snapshot(*, closed=True):
    policy = ROUND_TRIP_CAPITAL_POLICY
    buy = dict(intent_id="buy1", receipt_id="rb", symbol="BTCUSDT", side="buy",
               execution_slot=(NOW - timedelta(hours=3)).isoformat(),
               status="fixture_simulated", filled_quantity="1", average_price="100.02",
               reference_price="100.02", quote_bid="100", quote_ask="100", notional="100.02", fee="0.10002", fee_rate="0.001")
    sell = dict(intent_id="sell1", receipt_id="rs", symbol="BTCUSDT", side="sell",
                execution_slot=(NOW - timedelta(hours=1)).isoformat(),
                status="fixture_simulated", filled_quantity="1", average_price="89.982",
                reference_price="89.982", quote_bid="90", quote_ask="90", notional="89.982", fee="0.089982", fee_rate="0.001")
    return dict(account_id=policy.account_id, authority_id=policy.authority_id,
                generation=policy.generation, currency="USDT", initial_cash="10000",
                real_trading_enabled=False, execution_authority=False,
                production_eligible=False, aggregate_with_prior_generations=False,
                cash="9989.771998" if closed else "9899.87998",
                equity="9989.771998" if closed else "9989.87998",
                fees="0.190002" if closed else "0.10002",
                realized_pnl="-10.228002" if closed else "0",
                orders={"buy1": buy, **({"sell1": sell} if closed else {})},
                marks={"BTCUSDT": "90"},
                positions={} if closed else {"BTCUSDT": dict(quantity="1", entry_notional="100.02", entry_fee="0.10002")})


def analyze(value):
    return attribute_snapshot(value, mark_times={"BTCUSDT": (NOW - timedelta(hours=1)).isoformat()}, as_of=NOW)


def test_closed_loss_exact_components():
    result = analyze(snapshot())
    assert Decimal(result["price_movement_at_quote_mid"]) == -10
    assert Decimal(result["execution_price_impact_cost"]) == Decimal("0.038")
    assert Decimal(result["fees"]) == Decimal("0.190002")
    assert Decimal(result["model_slippage_and_rounding_cost"]) == Decimal("0.038")
    assert Decimal(result["model_spread_cost"]) == 0
    assert Decimal(result["net_pnl"]) == Decimal("-10.228002")
    assert all(Decimal(v) == 0 for v in result["reconciliation_residuals"].values())
    assert result["freshness"] == "dated_snapshot"
    assert result["current_account_pnl_claim"] is False
    assert result["symbol_attribution"][0]["holding_hours_on_sell"] == ["2.0"]


def test_unrealized_includes_unallocated_entry_fee_without_double_count():
    result = analyze(snapshot(closed=False))
    assert Decimal(result["realized_net"]) == 0
    assert Decimal(result["unrealized_net_including_entry_fee"]) == Decimal("-10.12002")
    assert Decimal(result["net_pnl"]) == Decimal("-10.12002")


def test_partial_sell_reconciles_remaining_basis_and_entry_fee():
    value = snapshot(closed=False)
    sell = snapshot()["orders"]["sell1"]
    sell.update(filled_quantity="0.5", notional="44.991", fee="0.044991", status="fixture_partially_simulated")
    value["orders"]["sell1"] = sell
    value.update(cash="9944.825989", equity="9989.825989", fees="0.145011", realized_pnl="-5.114001")
    value["positions"]["BTCUSDT"].update(quantity="0.5", entry_notional="50.01", entry_fee="0.05001")
    result = analyze(value)
    assert Decimal(result["net_pnl"]) == Decimal("-10.174011")
    assert result["symbol_attribution"][0]["closed_position_count"] == 0


@pytest.mark.parametrize("field,value", [("cash", "10001"), ("equity", "10000"), ("fees", "0"), ("realized_pnl", "0")])
def test_mismatched_summary_rejected(field, value):
    raw = snapshot()
    raw[field] = value
    with pytest.raises(ValueError, match="reconciliation_failed"):
        analyze(raw)


@pytest.mark.parametrize("field,value", [("real_trading_enabled", True), ("aggregate_with_prior_generations", True), ("generation", 1)])
def test_wrong_authority_rejected(field, value):
    raw = snapshot()
    raw[field] = value
    with pytest.raises(ValueError, match="boundary_invalid"):
        analyze(raw)


def test_duplicate_receipt_rejected():
    raw = snapshot()
    raw["orders"]["sell1"]["receipt_id"] = "rb"
    with pytest.raises(ValueError, match="identity_invalid"):
        analyze(raw)


def test_missing_mark_not_replaced_by_entry_price():
    raw = snapshot(closed=False)
    raw["marks"] = {}
    with pytest.raises(KeyError):
        analyze(raw)


def test_future_mark_rejected():
    with pytest.raises(ValueError, match="mark_time_invalid"):
        attribute_snapshot(snapshot(closed=False), mark_times={"BTCUSDT": (NOW + timedelta(hours=1)).isoformat()}, as_of=NOW)


def test_rejected_order_cannot_have_fill():
    raw = snapshot()
    raw["orders"]["sell1"]["status"] = "fixture_rejected"
    with pytest.raises(ValueError, match="rejected_fill_nonzero"):
        analyze(raw)


def test_live_environment_rejected(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(Exception):
        analyze(snapshot())


def test_existing_capital_snapshot_read_is_nonmutating(tmp_path, monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    _completed_round_trip(tmp_path)
    before = _tree_bytes(tmp_path)
    result = read_attribution(tmp_path / "round_trip_capital", as_of=WINDOW_END + timedelta(minutes=5))
    assert _tree_bytes(tmp_path) == before
    assert result["source"]["full_ledger_replayed_this_read"] is False
    assert result["read_only"] is True
    assert Decimal(result["fees"]) > 0


def test_missing_root_never_created(tmp_path):
    target = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        read_attribution(target)
    assert not target.exists()
