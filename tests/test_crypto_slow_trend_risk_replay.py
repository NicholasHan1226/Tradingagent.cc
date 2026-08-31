from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

import Crypto.slow_trend_risk_replay as replay


START = datetime(2026, 5, 1, tzinfo=timezone.utc)


def rows(count=70, *, flat=False):
    result = {}
    for symbol in replay.original.frozen_plan()["symbols"]:
        result[symbol] = []
        for i in range(count):
            price = D(100) if flat else D(100 + i)
            for offset in (5, 1435):
                result[symbol].append({"open_time": START + timedelta(days=i, minutes=offset),
                                       "open": price, "close": price})
    return result


def run(source):
    return replay.analyze(source, as_of=START + timedelta(days=100))


@pytest.fixture(autouse=True)
def paper(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")


def test_costs_and_cash_conservation_reconcile_every_leg():
    result = run(rows())["historical"]
    for name in ("risk_trend", "btc_causal_exposure_cash"):
        arm = result[name]
        cash, holdings = D(10000), {}
        fee_sum, slip_sum = D(0), D(0)
        for leg in arm["ledger"]:
            qty, price, fee = D(leg["quantity"]), D(leg["fill_price"]), D(leg["fee"])
            assert fee == qty * price * D(".001")
            fee_sum += fee
            slip_sum += D(leg["slippage_cost"])
            signed = qty if leg["side"] == "buy" else -qty
            cash -= signed * price + fee
            holdings[leg["symbol"]] = holdings.get(leg["symbol"], D(0)) + signed
            assert abs(cash - D(leg["cash_after"])) < D("1e-20")
            assert cash >= D("-1e-18")
            assert holdings[leg["symbol"]] >= 0
        assert all(q == 0 for q in holdings.values())
        assert abs(cash - D(arm["final_equity"])) < D("1e-20")
        assert fee_sum == D(arm["fees"])
        assert slip_sum == D(arm["slippage_cost"])


def test_no_risk_event_matches_original_trend_exact_return():
    result = run(rows())["historical"]
    assert result["risk_trend"]["return"] == result["original_trend"]["return"]
    assert result["risk_trend"]["risk_events"] == []
    assert result["risk_trend"]["max_drawdown_daily_close"] == result["original_trend"]["max_drawdown_daily_close"]


def test_flat_cash_and_btc_causal_baseline_remain_cash():
    result = run(rows(flat=True))["historical"]
    assert result["risk_trend"]["return"] == "0"
    assert result["btc_causal_exposure_cash"]["return"] == "0"
    assert D(result["btc_buy_hold"]["return"]) < 0


@pytest.mark.parametrize("equity,reason", [("9700", "daily_loss_pause"), ("9300", "drawdown_halt")])
def test_threshold_inclusive_and_pause_never_auto_clears(equity, reason):
    state = replay._Risk(D(10000))
    state.observe(D(equity), D(10000), at=START, phase="fixture")
    assert reason in state.pauses
    state.observe(D(11000), D(11000), at=START + timedelta(days=1), phase="fixture")
    assert reason in state.pauses


def test_tightening_boundary_sticky_and_daily_baseline_resets_only_metric():
    state = replay._Risk(D(10000))
    state.observe(D(9501), D(9501), at=START, phase="fixture")
    assert state.multiplier == 1 and not state.pauses
    state.observe(D(9500), D(9500), at=START, phase="fixture")
    assert state.multiplier == D(".75") and not state.pauses
    state.observe(D(10100), D(10100), at=START, phase="fixture")
    assert state.multiplier == D(".75")


def test_consecutive_closed_batches_include_fees_zero_resets():
    state = replay._Risk(D(10000))
    for pnl in (D(-1), D(-1), D(0), D(-1), D(-1)):
        state.record_closed_batch(pnl)
    assert state.streak == 2
    state.record_closed_batch(D("-.001"))
    state.observe(D(10000), D(10000), at=START, phase="fixture")
    assert state.pauses == {"consecutive_loss_pause"}


def test_partial_exits_count_one_episode_and_third_loss_blocks_next_buy(monkeypatch):
    schedule = [D(".1"), D(".05"), D(0), D(".1"), D(0), D(".1"), D(0), D(".1")]
    source = rows(8, flat=True)
    days = {s: replay.original._daily(bars, as_of=START + timedelta(days=8))[0] for s, bars in source.items()}
    dates = [START + timedelta(days=i) for i in range(8)]
    def weights(_days, day):
        return {s: schedule[(day - START).days] if s == "BTCUSDT" else D(0) for s in days}
    monkeypatch.setattr(replay.original, "_weights", weights)
    arm = replay._simulate(days, dates, mode="risk_trend")
    assert len(arm["completed_position_episodes"]) == 3
    assert arm["final_consecutive_losing_exit_batches"] == 3
    assert arm["final_pause_reasons"] == ["consecutive_loss_pause"]
    assert len([leg for leg in arm["ledger"] if leg["side"] == "sell"]) == 4
    assert not any(leg["side"] == "buy" and leg["at"] >= replay.original.history._iso(dates[6]) for leg in arm["ledger"])


def test_simultaneous_ten_losing_positions_count_one_exit_batch(monkeypatch):
    source = rows(2, flat=True)
    days = {s: replay.original._daily(bars, as_of=START + timedelta(days=2))[0] for s, bars in source.items()}
    monkeypatch.setattr(replay.original, "_weights", lambda _days, day: {s: D(".1") if day == START else D(0) for s in days})
    arm = replay._simulate(days, [START, START + timedelta(days=1)], mode="risk_trend")
    assert len(arm["completed_position_episodes"]) == 10
    assert arm["final_consecutive_losing_exit_batches"] == 1
    assert "consecutive_loss_pause" not in arm["final_pause_reasons"]


def test_five_percent_tightening_reduces_target_without_auto_recovery(monkeypatch):
    source = rows(4, flat=True)
    for bars in source.values():
        for i, price in enumerate([D(100), D("97.6"), D(95), D(101)]):
            for row in bars[i * 2:i * 2 + 2]:
                row["open"] = row["close"] = price
    days = {s: replay.original._daily(bars, as_of=START + timedelta(days=4))[0] for s, bars in source.items()}
    monkeypatch.setattr(replay.original, "_weights", lambda _days, day: {s: D(".1") for s in days})
    arm = replay._simulate(days, [START + timedelta(days=i) for i in range(4)], mode="risk_trend")
    assert arm["final_pause_reasons"] == []
    assert D(arm["targets"][2]["risk_multiplier"]) == D(".75")
    assert sum(map(D, arm["targets"][2]["weights"].values())) == D(".75")
    assert sum(map(D, arm["targets"][3]["weights"].values())) == D(".75")


@pytest.mark.parametrize("fee,reason", [(".4", "daily_loss_pause"), (".8", "drawdown_halt")])
def test_buy_fee_observed_before_next_leg_blocks_batch(monkeypatch, fee, reason):
    # Deliberately extreme fixture fee isolates a per-leg threshold crossing;
    # production plan and policy thresholds are not changed.
    plan = replay.original.frozen_plan()
    plan["fee_each_side"] = fee
    monkeypatch.setattr(replay.original, "frozen_plan", lambda: plan)
    source = rows(1, flat=True)
    days = {s: replay.original._daily(bars, as_of=START + timedelta(days=1))[0] for s, bars in source.items()}
    selected = sorted(days)[:2]
    monkeypatch.setattr(replay.original, "_weights", lambda _days, day: {s: D(".1") if s in selected else D(0) for s in days})
    arm = replay._simulate(days, [START], mode="risk_trend")
    buys = [leg for leg in arm["ledger"] if leg["side"] == "buy"]
    assert [leg["symbol"] for leg in buys] == selected[:1]
    assert any(e["reason"] == reason and e["phase"] == "after_buy:" + selected[0] for e in arm["risk_events"])


def test_buy_fee_crossing_five_percent_caps_remaining_leg(monkeypatch):
    # Seed a prior observed high and a newer day baseline. The first ordinary
    # fee crosses 5% DD, without crossing the 3% daily-loss trigger.
    base_risk = replay._Risk
    class PriorPeakRisk(base_risk):
        def __init__(self, initial):
            super().__init__(initial)
            self.peak = initial / D(".95001")
    monkeypatch.setattr(replay, "_Risk", PriorPeakRisk)
    source = rows(1, flat=True)
    days = {s: replay.original._daily(bars, as_of=START + timedelta(days=1))[0] for s, bars in source.items()}
    selected = sorted(days)[:2]
    monkeypatch.setattr(replay.original, "_weights", lambda _days, day: {s: D(".1") if s in selected else D(0) for s in days})
    arm = replay._simulate(days, [START], mode="risk_trend")
    buys = [leg for leg in arm["ledger"] if leg["side"] == "buy"]
    assert len(buys) == 2
    assert any(e["reason"] == "drawdown_tighten" and e["phase"] == "after_buy:" + selected[0] for e in arm["risk_events"])
    nav_after_first = D(buys[0]["cash_after"]) + D(buys[0]["quantity_after"]) * D(100)
    assert D(buys[1]["quantity"]) == D(".1") * D(".75") * nav_after_first / D(100)
    assert D(buys[1]["quantity"]) < D(buys[0]["quantity"]) * D(".75")


def test_daily_close_drawdown_is_separate_from_open_drawdown(monkeypatch):
    source = rows(2, flat=True)
    for bars in source.values():
        bars[2]["open"] = D(95)
        bars[3]["close"] = D(100)
    days = {s: replay.original._daily(bars, as_of=START + timedelta(days=2))[0] for s, bars in source.items()}
    monkeypatch.setattr(replay.original, "_weights", lambda _days, day: {s: D(".1") for s in days})
    arm = replay._simulate(days, [START, START + timedelta(days=1)], mode="btc_cash")
    assert D(arm["max_drawdown_sampled"]) > D(arm["max_drawdown_daily_close"])
    peak, dd = D(10000), D(0)
    for _, value in arm["daily_equity"]:
        nav = D(value)
        peak = max(peak, nav)
        dd = max(dd, (peak - nav) / peak)
    assert dd == D(arm["max_drawdown_daily_close"])


def test_close_trigger_waits_next_open_and_never_auto_buys_after_rebound():
    source = rows()
    for bars in source.values():
        bars[61 * 2 + 1]["close"] = D(130)
        bars[62 * 2]["open"] = D(120)
    arm = run(source)["historical"]["risk_trend"]
    trigger = replay.original.history._iso(START + timedelta(days=62))
    exit_time = replay.original.history._iso(START + timedelta(days=62, minutes=5))
    assert any(e["at"] == trigger and e["reason"] == "drawdown_halt" for e in arm["risk_events"])
    assert any(l["at"] == exit_time and l["reason"] == "risk_flatten" for l in arm["ledger"])
    assert not any(l["side"] == "buy" and l["at"] >= trigger for l in arm["ledger"])
    assert D(arm["max_drawdown_sampled"]) > D(".07")


def test_same_day_close_cannot_change_morning_orders_or_causal_btc_target():
    before = rows()
    after = deepcopy(before)
    for bars in after.values():
        bars[61 * 2 + 1]["close"] = D(1)
    first, second = run(before)["historical"], run(after)["historical"]
    morning = replay.original.history._iso(START + timedelta(days=61, minutes=5))
    for name in ("risk_trend", "btc_causal_exposure_cash"):
        assert [l for l in first[name]["ledger"] if l["at"] <= morning] == [l for l in second[name]["ledger"] if l["at"] <= morning]
        assert [t for t in first[name]["targets"] if t["at"] <= morning] == [t for t in second[name]["targets"] if t["at"] <= morning]


def test_missing_day_not_stitched_and_duplicate_rejected():
    source = rows()
    source["BTCUSDT"].pop(64 * 2 + 1)
    result = run(source)
    assert result["data_quality"]["segments"] == 2
    assert result["historical"]["days"] == 5
    assert result["historical"]["start"] == replay.original.history._iso(START + timedelta(days=65))
    source["BTCUSDT"].insert(1, source["BTCUSDT"][0])
    with pytest.raises(ValueError, match="duplicate"):
        run(source)


def test_forward_rows_never_evaluated_even_after_original_readout():
    source = rows(170)
    before = replay.analyze(source, as_of=datetime(2026, 12, 1, tzinfo=timezone.utc))
    for bars in source.values():
        for row in bars:
            if row["open_time"] >= replay.HISTORY_END:
                row["open"] = row["close"] = D(1)
    after = replay.analyze(source, as_of=datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert before == after
    assert after["forward"]["net_returns"] is None
    assert after["promotion_authorized"] is False
    assert after["execution_authority"] is False


def test_insufficient_history_and_bad_value_and_live_gate(monkeypatch):
    assert run(rows(59))["historical"]["status"] == "insufficient_complete_history"
    source = rows()
    source["BTCUSDT"][0]["open"] = D("NaN")
    with pytest.raises(ValueError):
        run(source)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(RuntimeError, match="real_trading"):
        run({})
