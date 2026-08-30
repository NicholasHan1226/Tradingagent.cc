from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext
import json
from pathlib import Path

import pytest

import Crypto.confirmation_forward_research as forward

REGISTERED = datetime(2026, 8, 30, tzinfo=timezone.utc)
DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def paper(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")


@pytest.fixture
def registration():
    return forward.register(registered_at=REGISTERED)


def inputs(*, flat=False):
    rows = {}
    for index, symbol in enumerate(forward.frozen_plan()["signal"]["symbols"]):
        rows[symbol] = []
        for i in range(151):
            price = D(100) if flat else D(100 + index) + D(i) / 10
            for minutes in (5, 1435):
                rows[symbol].append({"open_time": forward.WARMUP + i * DAY + timedelta(minutes=minutes),
                                     "open": price, "close": price})
    return rows


def test_exact_independent_plan_and_dependencies():
    plan = forward.frozen_plan()
    assert forward.END - forward.START == 90 * DAY
    assert forward.WARMUP == datetime(2026, 7, 2, tzinfo=timezone.utc)
    assert forward.START - forward.WARMUP == 61 * DAY
    assert plan["candidate_count"] == 1 and plan["historical_selection_trial_cells"] == 12
    assert plan["variant"] == "confirmation_only" and plan["cost_multipliers"] == ["1", "2"]
    assert forward.original.frozen_plan()["forward_start"] == "2026-08-31T00:00:00Z"
    assert forward.original.frozen_plan()["forward_end"] == "2026-11-29T00:00:00Z"
    assert set(forward.calculation_hashes()) == set(forward.CALCULATION_FILES)


@pytest.mark.parametrize("at", [forward.START, forward.END, forward.START + DAY])
def test_late_registration_refused(at):
    with pytest.raises(ValueError, match="too_late"):
        forward.register(registered_at=at)


@pytest.mark.parametrize("at", [datetime(2026, 8, 30),
                                    datetime(2026, 8, 30, tzinfo=timezone(timedelta(hours=8)))])
def test_non_UTC_clocks_refused(at, registration):
    with pytest.raises(ValueError, match="UTC_required"):
        forward.register(registered_at=at)
    with pytest.raises(ValueError, match="UTC_required"):
        forward.status(registration, now=at)


def test_future_registration_refused(registration):
    with pytest.raises(ValueError, match="in_future"):
        forward.status(registration, now=REGISTERED - DAY)


@pytest.mark.parametrize("field", ["digest", "plan", "source", "timestamp", "extra", "parent"])
def test_registration_tampering_refused(registration, field):
    if field == "digest":
        registration["registration_sha256"] = "0" * 64
    elif field == "plan":
        registration["plan"]["days"] = 91
    elif field == "source":
        registration["calculation_sources"]["Crypto/cost_aware_trend_research.py"] = "0" * 64
    elif field == "timestamp":
        registration["registered_at"] = "2026-08-31T00:00:00Z"
    elif field == "extra":
        registration["accept_any_cost"] = True
    else:
        registration["plan"]["parent_candidate_commit"] = "0" * 40
    with pytest.raises(ValueError, match="drift"):
        forward.status(registration, now=forward.END)


def test_rehashed_plan_and_changed_local_sources_refused(registration, monkeypatch):
    changed = deepcopy(registration)
    changed["plan"]["days"] = 91
    changed["plan_sha256"] = forward.history._sha256(changed["plan"])
    changed.pop("registration_sha256")
    changed = forward._seal(changed, "registration_sha256")
    with pytest.raises(ValueError, match="drift"):
        forward.status(changed, now=forward.END)
    hashes = forward.calculation_hashes()
    hashes["Crypto/confirmation_forward_research.py"] = "f" * 64
    monkeypatch.setattr(forward, "calculation_hashes", lambda: hashes)
    with pytest.raises(ValueError, match="drift"):
        forward.status(registration, now=forward.END)


@pytest.mark.parametrize("at,expected", [
    (forward.START - timedelta(seconds=1), "registered_not_started"),
    (forward.START, "sealed_until_fixed_readout"),
    (forward.END - timedelta(microseconds=1), "sealed_until_fixed_readout"),
])
def test_early_evaluation_does_not_touch_rows_or_simulator(registration, monkeypatch, at, expected):
    def fail(*args, **kwargs):
        pytest.fail("sealed readout consumed prices or invoked simulation")
    class Trap:
        def __iter__(self):
            fail()
    monkeypatch.setattr(forward.risk, "_simulate", fail)
    result = forward.evaluate(registration, Trap(), now=at, source=Trap())
    assert result["status"] == expected and result["results"] is None
    assert result["clean_holdout"] is result["promotion_authorized"] is False
    assert "input_rows_sha256" not in result


@pytest.mark.parametrize("offset,minute", [(0, 5), (60, 1435), (61, 5), (150, 1435)])
def test_warmup_and_valuation_gaps_block_all_returns(registration, monkeypatch, offset, minute):
    rows = inputs()
    missing_at = forward.WARMUP + offset * DAY + timedelta(minutes=minute)
    rows["BTCUSDT"] = [r for r in rows["BTCUSDT"] if r["open_time"] != missing_at]
    def fail(*args, **kwargs):
        pytest.fail("incomplete window reached simulator")
    monkeypatch.setattr(forward.risk, "_simulate", fail)
    for at in (forward.END, forward.END + 100 * DAY):
        result = forward.evaluate(registration, rows, now=at)
        assert result["status"] == "fixed_window_incomplete_no_return_claim"
        assert result["results"] is None
        assert result["coverage"]["missing_days"]["BTCUSDT"] == [forward.history._iso(forward.WARMUP + offset * DAY)]
        assert result["window"]["days"] == 90


@pytest.mark.parametrize("fault", ["universe", "symbol", "duplicate", "unsorted", "naive", "nan", "zero"])
def test_bad_prices_and_identity_refused(registration, fault):
    rows = inputs()
    if fault == "universe":
        rows.pop("BTCUSDT")
    elif fault == "symbol":
        rows["BTCUSDT"][0]["symbol"] = "ETHUSDT"
    elif fault == "duplicate":
        rows["BTCUSDT"].insert(1, dict(rows["BTCUSDT"][0]))
    elif fault == "unsorted":
        rows["BTCUSDT"][0], rows["BTCUSDT"][1] = rows["BTCUSDT"][1], rows["BTCUSDT"][0]
    elif fault == "naive":
        rows["BTCUSDT"][0]["open_time"] = datetime(2026, 7, 2)
    else:
        rows["BTCUSDT"][0]["open"] = D("NaN") if fault == "nan" else D(0)
    with pytest.raises(ValueError):
        forward.evaluate(registration, rows, now=forward.END)


def test_due_readout_exact_reuse_and_independent_cash_conservation(registration, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("readout entered old experiment or model fitting")
    monkeypatch.setattr(forward.original, "analyze", fail)
    monkeypatch.setattr(forward.risk, "analyze", fail)
    monkeypatch.setattr(forward.candidate, "analyze", fail)
    monkeypatch.setattr(forward.candidate, "fit_model", fail)
    rows = inputs()
    result = forward.evaluate(registration, rows, now=forward.END)
    later = forward.evaluate(registration, rows, now=forward.END + DAY)
    assert result["status"] == "fixed_window_offline_readout_not_PIT"
    assert result["results"] == later["results"]
    assert result["clean_holdout"] is result["promotion_authorized"] is False
    days = {s: forward.original._daily(bars, as_of=forward.END)[0] for s, bars in rows.items()}
    dates = [forward.START + i * DAY for i in range(90)]
    fs = {d: forward.candidate.features(days, d) for d in [forward.START - DAY, *dates]}
    targets = forward.candidate.target_path(fs, dates, variant="confirmation_only")
    assert all(w > 0 for w in targets[forward.START].values())
    assert len(result["results"]) == 2
    for multiplier, scenario in result["results"].items():
        assert scenario["cash_return"] == "0" and len(scenario["arms"]) == 3
        expected = forward.risk._simulate(days, dates, mode="risk_trend",
                    target_provider=targets.__getitem__, cost_multiplier=D(multiplier))
        assert scenario["arms"]["confirmation_only"] == expected
        for arm in scenario["arms"].values():
            cash, fees = D(10000), D(0)
            holdings = {}
            for leg in arm["ledger"]:
                quantity, fee = D(leg["quantity"]), D(leg["fee"])
                notional = quantity * D(leg["fill_price"])
                sign = 1 if leg["side"] == "buy" else -1
                cash -= sign * notional + fee
                fees += fee
                holdings[leg["symbol"]] = holdings.get(leg["symbol"], D(0)) + sign * quantity
                assert abs(fee - notional * D(".001") * D(multiplier)) < D("1e-18")
                expected_fill = D(leg["mark_price"]) * (1 + sign * D(".0002") * D(multiplier))
                assert D(leg["fill_price"]) == expected_fill
                assert forward.history._parse_utc(leg["at"]) >= forward.START
                assert forward.history._parse_utc(leg["at"]) <= forward.END
            assert abs(cash - D(arm["final_equity"])) < D("1e-18")
            assert abs(fees - D(arm["fees"])) < D("1e-18")
            assert all(abs(v) < D("1e-18") for v in holdings.values())
    digest = result.pop("report_sha256")
    assert digest == forward.history._sha256(result)


def test_flat_fixture_retains_negative_and_zero_results(registration):
    result = forward.evaluate(registration, inputs(flat=True), now=forward.END)
    assert result["descriptive_criterion_met"] is False
    for scenario in result["results"].values():
        assert scenario["summary"]["confirmation_only"]["return"] == "0"
        assert scenario["summary"]["confirmation_only"]["trade_leg_count"] == 0


def test_fixed_window_ignores_outside_prices_and_pins_decimal_context(registration):
    rows = inputs()
    expected = forward.evaluate(registration, rows, now=forward.END)
    for bars in rows.values():
        bars.append({"open_time": forward.END + timedelta(minutes=5), "open": D("999999"), "close": D("999999")})
    with localcontext() as ctx:
        ctx.prec = 12
        result = forward.evaluate(registration, rows, now=forward.END + DAY)
    assert result["input_rows_sha256"] == expected["input_rows_sha256"]
    assert result["results"] == expected["results"]


def test_later_market_move_cannot_change_prior_orders(registration):
    rows = inputs()
    original = forward.evaluate(registration, rows, now=forward.END)
    altered_at = forward.START + 30 * DAY
    for bars in rows.values():
        for bar in bars:
            if bar["open_time"] >= altered_at:
                bar["open"] *= 2
                bar["close"] *= 2
    changed = forward.evaluate(registration, rows, now=forward.END)
    for cost in ("1", "2"):
        for arm in original["results"][cost]["arms"]:
            before = lambda r: [leg for leg in r["results"][cost]["arms"][arm]["ledger"]
                                if forward.history._parse_utc(leg["at"]) < altered_at]
            assert before(original) == before(changed)


def test_CLI_clock_gate_never_opens_early_input_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "_now", lambda: REGISTERED)
    registration_path, output = tmp_path / "registration.json", tmp_path / "status.json"
    assert forward.main(["register", "--output", str(registration_path)]) == 0
    original_bytes = registration_path.read_bytes()
    with pytest.raises(FileExistsError):
        forward.main(["register", "--output", str(registration_path)])
    assert registration_path.read_bytes() == original_bytes
    monkeypatch.setattr(forward, "_now", lambda: forward.END - timedelta(seconds=1))
    assert forward.main(["readout", "--registration", str(registration_path),
                        "--input", str(tmp_path / "does-not-exist.json"), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["status"] == "sealed_until_fixed_readout" and report["results"] is None
    with pytest.raises(SystemExit):
        forward.main(["readout", "--as-of", "2030-01-01", "--output", str(output)])


def test_CLI_due_without_inputs_is_explicit(tmp_path, registration, monkeypatch):
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(registration))
    output = tmp_path / "status.json"
    monkeypatch.setattr(forward, "_now", lambda: forward.END)
    forward.main(["readout", "--registration", str(path), "--output", str(output)])
    result = json.loads(output.read_text())
    assert result["status"] == "fixed_readout_due_inputs_required" and result["results"] is None


def test_live_gate_precedes_all_other_work(registration, monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(RuntimeError, match="real_trading"):
        forward.register(registered_at=REGISTERED)
    with pytest.raises(RuntimeError, match="real_trading"):
        forward.evaluate(registration, {}, now=forward.END)


def test_committed_registration_and_status_match_current_sources():
    reports = Path(__file__).resolve().parents[1] / "Crypto" / "reports"
    registration = json.loads((reports / "2026-08-30-confirmation-forward-registration.json").read_text())
    saved = json.loads((reports / "2026-08-30-confirmation-forward-status.json").read_text())
    at = forward.history._parse_utc(saved["as_of"])
    assert at < forward.START
    assert forward.status(registration, now=at) == saved
    assert saved["results"] is None and saved["runtime_installed"] is False
