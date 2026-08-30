from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json

import pytest

import Crypto.cost_aware_trend_research as research

START = datetime(2026, 2, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def simulation_only(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")


def inputs(count=105, flat=False):
    rows = {}
    for index, symbol in enumerate(research.original.frozen_plan()["symbols"]):
        rows[symbol] = []
        for i in range(count):
            price = D(100) if flat else D(100 + index) + D(i) / 10
            for minutes in (5, 1435):
                rows[symbol].append({"open_time": START + i * DAY + timedelta(minutes=minutes),
                                     "open": price, "close": price})
    return rows


def daily(rows):
    return {s: research.original._daily(bars, as_of=START + 200 * DAY)[0] for s, bars in rows.items()}


def fmap(days):
    return {d: research.features(days, d) for d in sorted(next(iter(days.values())))}


def test_frozen_budget_and_no_forward_reuse():
    p = research.frozen_plan()
    assert len(p["variants"]) == 6 and p["trial_cells"] == 12
    assert p["new_forward_window"] is None and p["parameter_search"] is False
    assert research.original.frozen_plan()["forward_start"] == "2026-08-31T00:00:00Z"


def test_forecast_excludes_same_day_open_unmatured_labels_and_future_prices():
    rows = inputs(); days = daily(rows); day = START + 95 * DAY
    first = research.fit_model(days, fmap(days), day)
    assert first["status"] == "fitted_uncalibrated"
    assert research.history._parse_utc(first["latest_label_open"]) < day
    changed = deepcopy(rows)
    for bars in changed.values():
        for bar in bars:
            if bar["open_time"] >= day:
                bar["open"] *= 7
                bar["close"] *= 9
    altered = daily(changed)
    assert research.features(days, day) == research.features(altered, day)
    assert first == research.fit_model(altered, fmap(altered), day)
    for variant in research.VARIANTS:
        dates = [START + i * DAY for i in range(60, 96)]
        assert research.target_path(fmap(days), dates, variant=variant) == research.target_path(fmap(altered), dates, variant=variant)


def test_grid_label_on_decision_day_is_excluded():
    days = daily(inputs()); fs = fmap(days)
    day = START + 95 * DAY
    at = research.fit_model(days, fs, day)
    after = research.fit_model(days, fs, day + DAY)
    assert at["asset_windows"] + 10 == after["asset_windows"]
    assert research.history._parse_utc(after["latest_label_open"]) == day + timedelta(minutes=5)


def test_gap_removes_crossing_label_and_training_insufficiency_blocks():
    days = daily(inputs()); fs = fmap(days); day = START + 95 * DAY
    good = research.fit_model(days, fs, day)
    for series in days.values():
        del series[START + 83 * DAY]
    bad = research.fit_model(days, fmap(days), day)
    assert bad["asset_windows"] < good["asset_windows"]
    early = research.fit_model(days, fs, START + 70 * DAY)
    assert early["status"] == "insufficient_training" and early["intercept"] is None


def test_confirmation_and_component_ablation_targets():
    symbols = research.original.frozen_plan()["symbols"]
    signs = [True, True, False, True, False, False]
    fs = {START + i * DAY: {s: {"trend": sign, "strength": D('.01') if sign else D('-.01'),
                               "vol_weight": D('.04')} for s in symbols} for i, sign in enumerate(signs)}
    dates = sorted(fs)
    actual = research.target_path(fs, dates, variant="combined")
    assert [actual[d][symbols[0]] for d in dates] == [D(0),D('.04'),D('.04'),D('.04'),D('.04'),D(0)]
    no_trend = research.target_path(fs, dates, variant="combined_no_trend")
    assert all(v[symbols[0]] == D('.04') for v in no_trend.values())
    no_vol = research.target_path(fs, dates, variant="combined_no_vol")
    assert no_vol[dates[1]][symbols[0]] == D('.10')
    del fs[dates[3]][symbols[0]]
    assert research.target_path(fs, dates, variant="combined")[dates[3]][symbols[0]] == 0


def test_cost_threshold_exact_formula_and_stress():
    fee, slip = D('.001'), D('.0002')
    assert research.cost_threshold(D(1)) == 2*((1+fee)*(1+slip)/((1-fee)*(1-slip))-1)
    assert research.cost_threshold(D(2)) > 2*research.cost_threshold(D(1))
    with pytest.raises(ValueError): research.cost_threshold(D(0))


def test_buy_filter_never_prevents_normal_reduction_or_risk_flatten():
    days = daily(inputs()); dates = [START + i*DAY for i in range(60,65)]
    symbols = research.original.frozen_plan()["symbols"]
    def targets(day): return {s:D('.1') if day < dates[2] else D(0) for s in symbols}
    arm = research.risk._simulate(days, dates, mode="risk_trend", target_provider=targets,
                                  buy_allowed=lambda d,s: d == dates[0])
    assert any(l["side"] == "sell" and l["at"] == research.history._iso(dates[2]+research.original.BAR) for l in arm["ledger"])
    for series in days.values():
        series[dates[1]]["close"] *= D('.5')
        for day in dates[2:]:
            series[day]["execution_open"] *= D('.5')
            series[day]["close"] *= D('.5')
    armed = research.risk._simulate(days, dates, mode="risk_trend", target_provider=lambda d:{s:D('.1') for s in symbols},
                                    buy_allowed=lambda d,s:d==dates[0])
    assert "drawdown_halt" in armed["final_pause_reasons"]
    assert any(l["reason"] == "risk_flatten" for l in armed["ledger"])
    assert not any(l["side"]=="buy" and l["at"]>research.history._iso(dates[1]) for l in armed["ledger"])


def test_all_cells_reconcile_and_legacy_baseline_is_identical():
    rows=inputs(); at=START+105*DAY
    result=research.analyze(rows,as_of=at)
    again=research.analyze(rows,as_of=at)
    assert result==again and not result["promotion_authorized"]
    assert result["window"]["days"]==45
    assert len(result["scenarios"])==2
    legacy=research.risk.analyze(rows,as_of=at)["historical"]["risk_trend"]
    for k,v in legacy.items(): assert result["scenarios"]["1"]["arms"]["baseline_risk"][k]==v
    for cost,scenario in result["scenarios"].items():
        assert len(scenario["arms"])==6
        for arm in scenario["arms"].values():
            cash=D(10000); holdings={}; fees=D(0)
            for leg in arm["ledger"]:
                q,p,fee=D(leg["quantity"]),D(leg["fill_price"]),D(leg["fee"])
                assert fee==q*p*D('.001')*D(cost)
                assert abs(p/D(leg["mark_price"])-1)==D('.0002')*D(cost)
                sign=1 if leg["side"]=="buy" else -1
                cash-=sign*q*p+fee; fees+=fee
                holdings[leg["symbol"]]=holdings.get(leg["symbol"],D(0))+sign*q
                assert abs(cash-D(leg["cash_after"]))<D('1e-18')
            assert abs(cash-D(arm["final_equity"]))<D('1e-18')
            assert fees==D(arm["fees"]) and all(abs(v)<D('1e-18') for v in holdings.values())


def test_no_trading_on_insufficient_training_is_not_omitted():
    result=research.analyze(inputs(70),as_of=START+70*DAY)
    arm=result["scenarios"]["1"]["arms"]["combined"]
    assert arm["return"]=='0' and arm["trade_leg_count"]==0
    assert arm["filtered_buys"]


def test_duplicates_and_live_flags_rejected(monkeypatch):
    rows=inputs(65); rows[next(iter(rows))].insert(1,rows[next(iter(rows))][0])
    with pytest.raises(ValueError): research.analyze(rows,as_of=START+65*DAY)
    monkeypatch.setenv('REAL_TRADING_ENABLED','true')
    with pytest.raises(research.history.CryptoTenSymbolFactorPrescreenError):
        research.analyze(inputs(65),as_of=START+65*DAY)


def test_bad_custom_targets_rejected():
    days=daily(inputs(65)); dates=[START+60*DAY]
    for weight in (D('.2'),D('-1'),D('NaN')):
        with pytest.raises(ValueError):
            research.risk._simulate(days,dates,mode='risk_trend',target_provider=lambda d:{s:weight for s in days})


def test_cli_fails_closed_before_overwriting_existing_output(tmp_path):
    output=tmp_path/'result.json'; output.write_text('preserve')
    bad=tmp_path/'bad.json'; bad.write_text('{}')
    assert research.main(['--input',str(bad),'--reference-report',str(bad),'--output',str(output)])==2
    assert output.read_text()=='preserve'


def test_cli_valid_reference_exclusive_output_and_tamper_rejection(tmp_path):
    rows = inputs(95)
    source = {"kind": "test_fixture_no_market_authority"}
    reference = research.risk.analyze(rows, as_of=START+95*DAY)
    reference.pop("report_sha256")
    reference["source"] = source
    reference["report_sha256"] = research.history._sha256(reference)
    input_file, reference_file, output = (tmp_path/name for name in ('input.json','reference.json','result.json'))
    input_file.write_text(json.dumps({"rows":rows,"source":source},default=str))
    reference_file.write_text(json.dumps(reference))
    args=['--input',str(input_file),'--reference-report',str(reference_file),'--output',str(output)]
    assert research.main(args)==0
    first=output.read_bytes()
    assert research.main(args)==2 and output.read_bytes()==first
    payload=json.loads(input_file.read_text())
    payload['rows'][next(iter(rows))][125]['close']='999'
    input_file.write_text(json.dumps(payload))
    untouched=tmp_path/'must-not-exist.json'
    assert research.main(args[:-1]+[str(untouched)])==2 and not untouched.exists()
    input_file.write_text(json.dumps({"rows":rows,"source":source},default=str))
    reference['source']['kind']='tampered'
    reference_file.write_text(json.dumps(reference))
    assert research.main(args[:-1]+[str(untouched)])==2 and not untouched.exists()


def test_cli_live_gate_returns_failure_without_output(tmp_path, monkeypatch):
    monkeypatch.setenv('REAL_TRADING_ENABLED','true')
    output=tmp_path/'no-output.json'
    assert research.main(['--input',str(tmp_path/'absent-input'),'--reference-report',str(tmp_path/'absent-ref'),
                          '--output',str(output)])==2
    assert not output.exists()
