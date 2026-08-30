from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

import Crypto.slow_trend_research as trend


def rows(start, count, *, rising=True):
    bars = []
    for day in range(count):
        price = D(100) + day if rising else D(100)
        for bar in range(288):
            bars.append({"open_time": start + timedelta(days=day, minutes=bar * 5), "open": price, "close": price})
    return {symbol: bars for symbol in trend.frozen_plan()["symbols"]}


@pytest.fixture(autouse=True)
def paper(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")


def test_flat_market_trend_stays_cash_and_btc_pays_both_legs():
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = trend.analyze(rows(start, 64, rising=False), as_of=start + timedelta(days=64))
    hist = result["historical"]
    assert hist["days"] == 4
    assert hist["trend"]["return"] == "0"
    assert hist["trend"]["trade_leg_count"] == 0
    assert hist["btc_buy_hold"]["trade_leg_count"] == 2
    assert D(hist["btc_buy_hold"]["return"]) < 0
    assert result["forward"]["net_returns"] is None


def test_positive_fixture_is_not_promotion_and_quantities_stay_nonnegative():
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = trend.analyze(rows(start, 70), as_of=start + timedelta(days=70))
    hist = result["historical"]
    assert D(hist["trend"]["return"]) > 0
    assert all(D(leg[3]) > 0 for leg in hist["trend"]["ledger"])
    assert result["promotion_authorized"] is False
    assert result["clean_holdout"] is False
    assert result["plan"]["trial_count"] == 1


def test_missing_required_close_disables_only_affected_symbol():
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    source = rows(start, 64)
    source["BTCUSDT"] = source["BTCUSDT"][:]
    source["BTCUSDT"].pop(30 * 288 + 287)
    result = trend.analyze(source, as_of=start + timedelta(days=64))
    assert result["historical"]["status"] == "historical_diagnostic_not_holdout"
    assert not any(leg[1] == "BTCUSDT" for leg in result["historical"]["trend"]["ledger"])
    assert result["data_quality"]["incomplete_days"]["BTCUSDT"] == 1


def test_unused_intraday_gap_does_not_change_daily_strategy():
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    source = rows(start, 64)
    expected = trend.analyze(source, as_of=start + timedelta(days=64))["historical"]
    source["BTCUSDT"] = source["BTCUSDT"][:]
    source["BTCUSDT"].pop(30 * 288 + 100)
    assert trend.analyze(source, as_of=start + timedelta(days=64))["historical"] == expected


def test_same_day_close_cannot_change_same_day_orders():
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    source = rows(start, 62)
    daily = {s: trend._daily(bars, as_of=start + timedelta(days=62))[0] for s, bars in source.items()}
    day = start + timedelta(days=60)
    before = trend._weights(daily, day)
    daily["BTCUSDT"][day]["close"] = D("100000")
    assert trend._weights(daily, day) == before


def test_forward_is_sealed_and_never_leaks_into_historical_selection():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    source = rows(start, 100)
    cutoff = start + timedelta(days=100)
    first = trend.analyze(source, as_of=cutoff)
    for bars in source.values():
        # Shared fixture rows are intentional; this remains deterministic.
        for bar in bars[-288:]:
            bar["close"] = D("100000")
    second = trend.analyze(source, as_of=cutoff)
    assert first["historical"] == second["historical"]
    assert first["forward"] == second["forward"]
    assert second["forward"]["net_returns"] is None


def test_fixed_readout_rejects_missing_window_without_extending_deadline():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    result = trend.analyze(rows(start, 100), as_of=datetime(2026, 11, 30, tzinfo=timezone.utc))
    assert result["forward"]["status"] == "fixed_window_incomplete_no_return_claim"
    assert result["forward"]["net_returns"] is None


def test_duplicate_bar_and_live_gate_fail(monkeypatch):
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    source = rows(start, 1)
    source["BTCUSDT"].insert(1, dict(source["BTCUSDT"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        trend.analyze(source, as_of=start + timedelta(days=1))
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(RuntimeError, match="real_trading"):
        trend.analyze({}, as_of=start)


def test_full_forward_readout_is_fixed_and_still_not_promotion():
    plan = trend.frozen_plan()
    start = trend.history._parse_utc(plan["forward_start"]) - timedelta(days=60)
    end = trend.history._parse_utc(plan["forward_end"])
    source = rows(start, 151)
    first = trend.analyze(source, as_of=end)
    second = trend.analyze(source, as_of=end + timedelta(days=1))
    assert first["forward"] == second["forward"]
    assert first["forward"]["comparison"]["days"] == 90
    assert first["forward"]["promotion_authorized"] is False
    assert first["forward"]["clean_holdout"] is False


class DailyTransport:
    def __init__(self, fault=None):
        self.calls = []
        self.fault = fault

    def __call__(self, **kwargs):
        from tests.test_crypto_ten_symbol_support import catalog_payload, query_metadata, CATALOG_VERSION
        from tests.test_crypto_ten_symbol_factor_prescreen import _wire_rows
        from shared.data.sharedsignals_v1 import HTTPResponse
        if kwargs["method"] == "GET":
            return HTTPResponse(200, catalog_payload())
        body = kwargs["json_body"]
        self.calls.append(body)
        times = [trend.history._parse_utc(t) for t in body["filters"]["open_time"]["in"]]
        assert len(times) <= 100 and body["limit"] == 100
        symbol = body["filters"]["symbol"]["eq"]
        page = [_wire_rows(["100"], symbol=symbol, start=t)[0] for t in times]
        if self.fault == "missing":
            page.pop()
        if self.fault == "outside":
            page.append(_wire_rows(["100"], symbol=symbol, start=times[-1] + trend.BAR)[0])
        meta = query_metadata(body["dataset_id"], data_through=times[-1] + trend.BAR,
                              observed_at=times[-1] + trend.BAR + timedelta(seconds=20))
        if self.fault == "degraded":
            meta["degraded"] = True
        return HTTPResponse(200, {"api_version": "v1", "catalog_version": CATALOG_VERSION,
            "request_id": "daily-fixture", "dataset_id": body["dataset_id"], "data": page,
            "next_cursor": "unexpected" if self.fault == "cursor" else None, "metadata": meta})


def test_formal_daily_fetch_bounds_batches_and_binds_receipts():
    from tests.test_crypto_ten_symbol_support import client
    transport = DailyTransport()
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    source, proof = trend.fetch_daily_inputs(client(transport), start=start, end=start + timedelta(days=51))
    assert len(transport.calls) == 20
    assert all(len(bars) == 102 for bars in source.values())
    assert all(len(receipts) == 2 for receipts in proof["receipts"].values())
    assert all(value == 0 for value in proof["missing_requested_points"].values())
    assert proof["historical_backfill_no_pit"] is True


@pytest.mark.parametrize("fault", ["cursor", "outside", "degraded"])
def test_formal_daily_fetch_does_not_accept_truncated_or_bad_metadata(fault):
    from tests.test_crypto_ten_symbol_support import client
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with pytest.raises((RuntimeError, ValueError)):
        trend.fetch_daily_inputs(client(DailyTransport(fault)), start=start, end=start + timedelta(days=2))


def test_missing_required_daily_point_is_reported_not_filled():
    from tests.test_crypto_ten_symbol_support import client
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    source, proof = trend.fetch_daily_inputs(client(DailyTransport("missing")), start=start, end=start + timedelta(days=2))
    assert all(len(bars) == 3 for bars in source.values())
    assert all(value == 1 for value in proof["missing_requested_points"].values())
