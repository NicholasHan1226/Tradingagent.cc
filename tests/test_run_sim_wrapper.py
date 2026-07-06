from __future__ import annotations

from shared.wrappers import run_sim


class FakeReader:
    degraded = False
    stale = False
    errors: list[str] = []

    def __init__(self, rows):
        self.rows = rows

    def get_crypto_klines(self, *, symbol: str, limit: int = 50):
        return self.rows.get(symbol, [])


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
