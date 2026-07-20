from __future__ import annotations

import json

from shared.wrappers import run_sim


class FakeReader:
    degraded = False
    stale = False
    errors: list[str] = []

    def __init__(self, rows):
        self.rows = rows

    def get_crypto_klines(self, *, symbol: str, limit: int = 50):
        return self.rows.get(symbol, [])

    def get_market_data(self, *, ts_code: str, market: str, start: str, end: str, freq: str):
        return self.rows.get(ts_code, [])

    def get_pm_markets(self, limit: int = 10, active_only: bool = True):
        return self.rows.get("pm", [])[:limit]


def test_price_rows_do_not_become_trade_signals_by_default(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "crypto")
    monkeypatch.delenv("TRADINGAGENT_SIM_ALLOW_PRICE_ONLY_SIGNALS", raising=False)
    reader = FakeReader({"BTCUSDT": [{"symbol": "BTCUSDT", "close": 100.0, "trade_date": "20260704"}]})

    assert run_sim._load_signals(reader, "crypto", limit=10) == []


def test_explicit_trade_side_is_accepted(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "crypto")
    reader = FakeReader({"BTCUSDT": [{"symbol": "BTCUSDT", "close": 100.0, "side": "buy", "trade_date": "20260704"}]})

    signals = run_sim._load_signals(reader, "crypto", limit=10)

    assert len(signals) == 1
    assert signals[0]["symbol"] == "BTCUSDT"
    assert signals[0]["side"] == "buy"


def test_price_only_smoke_signals_are_dashboard_excluded(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "crypto")
    monkeypatch.setenv("TRADINGAGENT_SIM_ALLOW_PRICE_ONLY_SIGNALS", "1")
    reader = FakeReader({"BTCUSDT": [{"symbol": "BTCUSDT", "close": 100.0, "trade_date": "20260704"}]})

    signals = run_sim._load_signals(reader, "crypto", limit=10)

    assert len(signals) == 1
    assert signals[0]["exclude_from_dashboard"] is True
    assert signals[0]["sample_type"] == "price_only_smoke"


def test_crypto_momentum_series_generates_explicit_buy_signal(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "crypto")
    monkeypatch.delenv("TRADINGAGENT_SIM_ALLOW_PRICE_ONLY_SIGNALS", raising=False)
    reader = FakeReader({
        "BTCUSDT": [
            {"symbol": "BTCUSDT", "close": 100.0, "trade_date": "20260701"},
            {"symbol": "BTCUSDT", "close": 101.0, "trade_date": "20260702"},
            {"symbol": "BTCUSDT", "close": 104.0, "trade_date": "20260703"},
        ]
    })

    signals = run_sim._load_signals(reader, "crypto", limit=10)

    assert len(signals) == 1
    assert signals[0]["side"] == "buy"
    assert signals[0]["strategy_name"] == "crypto_momentum_breakout"
    assert signals[0]["signal_source"] == "explicit_strategy_signal"


def test_crypto_no_signal_diagnostics_explain_empty_klines(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "crypto")
    monkeypatch.setattr(run_sim, "_symbols_for_market", lambda name: ("BTCUSDT",))
    reader = FakeReader({"BTCUSDT": []})

    diagnostics = run_sim._signal_diagnostics(reader, "crypto", limit=10)

    assert diagnostics["total_priced_rows"] == 0
    assert diagnostics["strategy_candidate_rows"] == 0
    assert diagnostics["reason"] == "crypto_klines_empty"
    assert diagnostics["no_priced_symbols"] == ["BTCUSDT"]


def test_crypto_no_signal_diagnostics_explain_momentum_not_met(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "crypto")
    monkeypatch.setattr(run_sim, "_symbols_for_market", lambda name: ("BTCUSDT",))
    reader = FakeReader({
        "BTCUSDT": [
            {"symbol": "BTCUSDT", "close": 100.0, "trade_date": "20260701"},
            {"symbol": "BTCUSDT", "close": 100.5, "trade_date": "20260702"},
        ]
    })

    diagnostics = run_sim._signal_diagnostics(reader, "crypto", limit=10)

    assert diagnostics["total_priced_rows"] == 2
    assert diagnostics["strategy_candidate_rows"] == 0
    assert diagnostics["reason"] == "crypto_momentum_threshold_not_met"
    assert diagnostics["below_threshold_symbols"] == ["BTCUSDT"]
    assert diagnostics["sample"][0]["one_bar_return"] < diagnostics["momentum_thresholds"]["one_bar_return"]


def test_us_small_move_does_not_generate_trade_signal(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "us")
    monkeypatch.setattr(run_sim, "_symbols_for_market", lambda name: ("TSLA",))
    reader = FakeReader({
        "TSLA": [
            {"symbol": "TSLA", "close": 100.0, "trade_date": "20260701"},
            {"symbol": "TSLA", "close": 100.4, "trade_date": "20260702"},
            {"symbol": "TSLA", "close": 100.8, "trade_date": "20260703"},
        ]
    })

    assert run_sim._load_signals(reader, "us", limit=10) == []


def test_us_trend_series_generates_explicit_buy_signal(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "us")
    monkeypatch.setattr(run_sim, "_symbols_for_market", lambda name: ("TSLA",))
    reader = FakeReader({
        "TSLA": [
            {"symbol": "TSLA", "close": 100.0, "trade_date": "20260701"},
            {"symbol": "TSLA", "close": 102.0, "trade_date": "20260702"},
            {"symbol": "TSLA", "close": 104.5, "trade_date": "20260703"},
        ]
    })

    signals = run_sim._load_signals(reader, "us", limit=10)

    assert len(signals) == 1
    assert signals[0]["side"] == "buy"
    assert signals[0]["strategy_name"] == "us_trend_follow"


def test_pm_ignores_sharedsignals_embedded_model_probability(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "pm")
    reader = FakeReader({
        "pm": [{
            "market_id": "558943",
            "yes_price": 0.48,
            "model_probability": 0.60,
            "trade_date": "20260703",
        }]
    })

    signals = run_sim._load_signals(reader, "pm", limit=10)

    assert signals == []


def test_pm_without_model_edge_does_not_generate_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(run_sim, "market", "pm")
    monkeypatch.setenv("TRADINGAGENT_PM_MODEL_PROBABILITY_FILE", str(tmp_path / "missing.jsonl"))
    reader = FakeReader({"pm": [{"market_id": "558943", "yes_price": 0.48, "trade_date": "20260703"}]})

    assert run_sim._load_signals(reader, "pm", limit=10) == []


def test_pm_no_signal_diagnostics_explain_empty_market_rows(monkeypatch):
    monkeypatch.setattr(run_sim, "market", "pm")
    reader = FakeReader({"pm": []})

    diagnostics = run_sim._signal_diagnostics(reader, "pm", limit=10)

    assert diagnostics["market_rows"] == 0
    assert diagnostics["reason"] == "pm_market_rows_empty"


def test_pm_no_signal_diagnostics_explain_missing_model_probability(monkeypatch, tmp_path):
    monkeypatch.setattr(run_sim, "market", "pm")
    monkeypatch.setenv("TRADINGAGENT_PM_MODEL_PROBABILITY_FILE", str(tmp_path / "missing.jsonl"))
    reader = FakeReader({"pm": [{"market_id": "558943", "yes_price": 0.48, "trade_date": "20260703"}]})

    diagnostics = run_sim._signal_diagnostics(reader, "pm", limit=10)

    assert diagnostics["priced_rows"] == 1
    assert diagnostics["modeled_rows"] == 1
    assert diagnostics["strategy_candidate_rows"] == 0
    assert diagnostics["reason"] == "pm_model_edge_below_threshold"
    assert diagnostics["sample"][0]["model_source"] == "pm_market_consensus_baseline"


def test_pm_research_probability_file_can_generate_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(run_sim, "market", "pm")
    source = tmp_path / "model_probabilities.jsonl"
    source.write_text('{"market_id":"558943","model_probability":0.60,"model_source":"test_research"}\n', encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENT_PM_MODEL_PROBABILITY_FILE", str(source))
    reader = FakeReader({"pm": [{"market_id": "558943", "yes_price": 0.48, "trade_date": "20260703"}]})

    signals = run_sim._load_signals(reader, "pm", limit=10)

    assert len(signals) == 1
    assert signals[0]["outcome"] == "yes"
    assert signals[0]["model_source"] == "test_research"


def test_legacy_run_sim_is_tombstoned_for_every_market(monkeypatch, capsys):
    monkeypatch.setattr(run_sim, "market", "ashare")

    assert run_sim.main() == 78

    payload = json.loads(capsys.readouterr().err)
    assert payload["state"] == "retired"
    assert payload["reason"] == "legacy_runtime_retired"
    assert payload["real_trading_enabled"] is False
