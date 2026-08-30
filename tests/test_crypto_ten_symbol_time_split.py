"""Leakage, gap, and simulation-boundary tests for the historical diagnostic."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from Crypto.ten_symbol_time_split import BAR, _hour_grid, analyze, partition, run
from Crypto.ten_symbol_factor_prescreen import CryptoTenSymbolFactorPrescreenError


START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def bars(count=900):
    return {
        symbol: [
            {
                "open_time": START + index * BAR,
                "close": Decimal(100) + Decimal(index) / 100 + Decimal((index + offset) % 21) / 50,
                "quote_volume": Decimal(100000),
            }
            for index in range(count)
        ]
        for symbol, offset in (("BTCUSDT", 0), ("ETHUSDT", 7))
    }


def test_training_label_close_is_purged_and_test_features_embargoed():
    rows = bars(100)
    split = START + 40 * BAR
    samples = {"BTCUSDT": {START + index * BAR: {} for index in range(100)}}
    train, test = partition(samples, rows, split=split, as_of=START + 99 * BAR)
    assert START + 27 * BAR in train["BTCUSDT"]
    assert START + 28 * BAR not in train["BTCUSDT"]
    assert START + 51 * BAR not in test["BTCUSDT"]
    assert START + 52 * BAR in test["BTCUSDT"]
    assert START + 86 * BAR in test["BTCUSDT"]
    assert START + 87 * BAR not in test["BTCUSDT"]


def test_future_endpoints_do_not_hide_a_missing_interior_bar():
    rows = bars(100)
    rows["BTCUSDT"] = [row for row in rows["BTCUSDT"] if row["open_time"] != START + 8 * BAR]
    samples = {"BTCUSDT": {START: {}, START + 9 * BAR: {}}}
    train, _ = partition(samples, rows, split=START + 40 * BAR, as_of=START + 99 * BAR)
    assert START not in train["BTCUSDT"]
    assert START + 9 * BAR in train["BTCUSDT"]


def test_grid_does_not_shift_when_a_slot_is_missing():
    slots = {START + index * BAR: {} for index in range(40)}
    slots.pop(START + 12 * BAR)
    selected = _hour_grid({"BTCUSDT": slots})["BTCUSDT"]
    assert list(selected) == [START, START + 24 * BAR, START + 36 * BAR]


def test_test_prices_cannot_change_training_selection_or_volatility_fit(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    original = bars()
    split, as_of = START + 600 * BAR, START + 900 * BAR
    first = analyze(original, split=split, as_of=as_of)
    changed = {
        symbol: [dict(row, close=row["close"] + Decimal(index * index) / 500) if row["open_time"] >= split else row for index, row in enumerate(rows)]
        for symbol, rows in original.items()
    }
    second = analyze(changed, split=split, as_of=as_of)
    assert first["volatility_threshold_train_only"] == second["volatility_threshold_train_only"]
    for family in first["comparisons"]:
        before, after = first["comparisons"][family], second["comparisons"][family]
        assert before["train"] == after["train"]
        assert before["train_selected_variant"] == after["train_selected_variant"]
    assert first["comparisons"]["xs_rs"]["test"] != second["comparisons"]["xs_rs"]["test"]
    assert first["clean_holdout"] is False
    assert first["promotion_authorized"] is False
    assert first["not_promotion_evidence"] is True


def test_real_trading_flag_rejected_before_analysis(monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(CryptoTenSymbolFactorPrescreenError, match="real_trading"):
        analyze(bars(30), split=START + BAR, as_of=START + 30 * BAR)


def test_store_run_is_read_only_and_reproducible(monkeypatch, tmp_path):
    from tests.test_crypto_ten_symbol_research_loop import _accumulate
    from tests.test_crypto_ten_symbol_support import WINDOW_END

    root = _accumulate(monkeypatch, tmp_path, 20)
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    as_of = WINDOW_END + 19 * BAR
    first, second = run(root, as_of=as_of), run(root, as_of=as_of)
    assert first == second
    assert before == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    for window in first["source"]["data_window"].values():
        assert datetime.fromisoformat(window["last_open_time"].replace("Z", "+00:00")) + BAR <= as_of
