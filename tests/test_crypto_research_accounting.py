from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from Crypto.research_accounting import funded_hedge, linear_leg, number
from Crypto.forty_symbol_funding_carry_research import evaluate_settled_carry
from Crypto.forty_symbol_basis_carry_research import _evaluate_cell


@pytest.mark.parametrize("exit", ["1", "90", "100", "110", "1000"])
def test_equal_quantity_linear_hedge_has_no_price_alpha(exit):
    args = dict(quantity=D(2), entry=D(100), exit=D(exit), fee_rate=D(0), slippage=D(0))
    assert linear_leg(side=1, **args)["gross_pnl"] + linear_leg(side=-1, **args)["gross_pnl"] == 0


def test_each_fee_uses_actual_executed_notional():
    leg = linear_leg(quantity=D(2), entry=D(100), exit=D(110), side=-1,
                     fee_rate=D("0.001"), slippage=D("0.0002"))
    assert leg["gross_pnl"] == -20
    assert leg["entry_fill"] == D("99.98")
    assert leg["exit_fill"] == D("110.022")
    assert leg["fees"] == D("0.420004")
    assert leg["net_pnl"] == leg["gross_pnl"] - leg["slippage_cost"] - leg["fees"]


@pytest.mark.parametrize("value", [True, 1.5, "NaN", "Infinity", "bad"])
def test_nonfinite_or_lossy_numbers_rejected(value):
    with pytest.raises(ValueError):
        number(value)


def material():
    start = datetime(2026, 8, 30, 7, 55, tzinfo=timezone.utc)
    prices = [{"time": (start + i * timedelta(minutes=5)).isoformat(),
               "spot": "100", "perp": "100", "mark": "100"} for i in range(3)]
    funding = [{"funding_time": prices[1]["time"], "funding_rate": "0.001", "mark_price": "100"}]
    kwargs = dict(expected_funding_times=[prices[1]["time"]], quantity=D(1), spot_fee=D("0.001"),
                  perp_fee=D("0.001"), slippage=D(0), collateral=D(100), maintenance_rate=D("0.05"))
    return prices, funding, kwargs


def test_funding_is_discrete_and_return_uses_total_capital(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    prices, funding, kwargs = material()
    result = funded_hedge(prices, funding, **kwargs)
    assert D(result["funding_cashflow"]) == D("0.1")
    assert D(result["fees"]) == D("0.4")
    assert D(result["net_pnl"]) == D("-0.3")
    assert D(result["capital_committed"]) == D("200.1")
    assert D(result["return_on_committed_capital"]) == D("-0.3") / D("200.1")
    funding[0]["funding_rate"] = "-0.001"
    assert D(funded_hedge(prices, funding, **kwargs)["net_pnl"]) == D("-0.5")


@pytest.mark.parametrize("fault", ["missing", "duplicate", "gap", "entry_settlement", "leverage"])
def test_carry_rejects_incomplete_events_and_invalid_boundaries(monkeypatch, fault):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    prices, funding, kwargs = material()
    if fault == "missing":
        funding.clear()
    elif fault == "duplicate":
        funding.append(dict(funding[0]))
    elif fault == "gap":
        prices.pop(1)
    elif fault == "entry_settlement":
        kwargs["expected_funding_times"] = [prices[0]["time"]]
        funding[0]["funding_time"] = prices[0]["time"]
    else:
        kwargs["collateral"] = D(10)
    with pytest.raises(ValueError):
        funded_hedge(prices, funding, **kwargs)


def test_margin_spike_cannot_report_terminal_profit(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    prices, funding, kwargs = material()
    prices[1]["mark"] = "250"
    result = funded_hedge(prices, funding, **kwargs)
    assert result["status"] == "margin_screen_failed"
    assert result["net_pnl"] is None
    assert result["return_on_committed_capital"] is None


def test_no_proxies_or_absent_data_as_profit(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    prices, funding, kwargs = material()
    result = evaluate_settled_carry(prices=prices, funding=funding, source={"kind": "tradingdatas_catalog_query"}, **kwargs)
    assert result["status"] == "data_unavailable" and result["net_pnl"] is None
    source = {key: "fixture-receipt" for key in ("spot", "perp_trade", "perp_mark", "settled_funding", "funding_schedule")}
    source.update(kind="fixture", premium_proxy=True)
    with pytest.raises(RuntimeError, match="proxy_forbidden"):
        evaluate_settled_carry(prices=prices, funding=funding, source=source, **kwargs)


def test_basis_cell_uses_same_quantity_not_reciprocal_short():
    data = {"BTCUSDT": {"times": [0, 1], "spot": [D(100), D(110)],
                        "perp": [D(101), D(111)], "premium": [D("0.01"), D(111)/110-1]}}
    result = _evaluate_cell(data, symbols=("BTCUSDT",), threshold=D("0.001"), horizon_bars=1,
                           context={"kept_slots": {0}, "universe_count": 1, "spot_baseline_net_mean": D(0)})
    assert D(result["metrics"]["mean_gross"]) == 0


def test_real_trading_gate(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    prices, funding, kwargs = material()
    with pytest.raises(RuntimeError, match="real_trading"):
        funded_hedge(prices, funding, **kwargs)


def test_millisecond_settlement_is_not_rounded_or_prorated(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    prices, funding, kwargs = material()
    time = datetime.fromisoformat(funding[0]["funding_time"]) + timedelta(milliseconds=2)
    funding[0]["funding_time"] = time.isoformat()
    kwargs["expected_funding_times"] = [time.isoformat()]
    assert D(funded_hedge(prices, funding, **kwargs)["funding_cashflow"]) == D("0.1")
