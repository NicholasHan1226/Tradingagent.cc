from __future__ import annotations

from pathlib import Path

from Crypto.tsm_shadow_observer import TsmShadowObserverError, read_ledger
from Crypto.tsm_shadow_worker import run_tsm_shadow_worker_once


def _write_csv(path: Path) -> None:
    lines = ["open_time_ms,open,high,low,close,volume"]
    # 25 complete days of 5m bars per symbol, rising BTC, falling ETH
    day_ms = 24 * 60 * 60 * 1000
    step_ms = 5 * 60 * 1000
    start = 1767225600000  # 2026-01-01
    for sym, base, drift in (("BTCUSDT", 100.0, 0.005), ("ETHUSDT", 50.0, -0.004)):
        lines.append(f"###{sym}###")
        close = base
        for day in range(25):
            day_open = close
            close = day_open * (1.0 + drift)
            for slot in range(288):
                price = day_open + (close - day_open) * (slot + 1) / 288
                ms = start + day * day_ms + slot * step_ms
                lines.append(
                    f"{ms},{price:.8f},{price:.8f},{price:.8f},{price:.8f},100.0"
                )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_worker_csv_mode_builds_ledger(tmp_path: Path) -> None:
    csv = tmp_path / "export.csv"
    ledger = tmp_path / "ledger"
    _write_csv(csv)
    receipt = run_tsm_shadow_worker_once(csv_path=csv, ledger_root=ledger)
    assert receipt["status"] == "completed"
    assert receipt["written"] == 50
    btc = read_ledger(ledger, symbol="BTCUSDT")
    eth = read_ledger(ledger, symbol="ETHUSDT")
    assert len(btc) == 25
    assert len(eth) == 25
    # rising BTC -> 20d TSM positive later; falling ETH -> flat
    assert btc[-1]["suggested_fraction_20d"] is not None
    assert btc[-1]["tsm_20d"] > 0
    assert eth[-1]["suggested_fraction_20d"] == 0.0


def test_worker_rejects_no_input_mode(tmp_path: Path) -> None:
    try:
        run_tsm_shadow_worker_once(ledger_root=tmp_path)
        raise AssertionError("expected TsmShadowObserverError")
    except TsmShadowObserverError:
        pass
