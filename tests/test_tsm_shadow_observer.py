from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from Crypto.tsm_shadow_observer import (
    BASE_POSITION_FRACTION,
    TSM_SHADOW_CONTRACT,
    TsmShadowObserverError,
    aggregate_daily_bars,
    append_tsm_shadow_rows,
    build_shadow_rows,
    read_ledger,
    run_tsm_shadow_once,
)


STEP_MS = 5 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(open_ms: int, close: float, *, open_price: float | None = None) -> dict:
    open_price = float(open_price) if open_price is not None else close
    return {
        "open_time_ms": open_ms,
        "open": open_price,
        "high": max(open_price, close),
        "low": min(open_price, close),
        "close": close,
        "volume": "100.0",
    }


def _days_bars(
    n_days: int,
    *,
    base: float = 100.0,
    drift: float = 0.0,
    noise: float = 0.002,
) -> list[dict]:
    bars: list[dict] = []
    close = base
    for day in range(n_days):
        day_open = close
        # deterministic pseudo-noise so realized vol is non-zero
        wiggle = ((day * 7) % 11 - 5) / 5 * noise
        close = day_open * (1.0 + drift + wiggle)
        for slot in range(288):
            open_ms = int(START.timestamp() * 1000) + day * DAY_MS + slot * STEP_MS
            price = day_open + (close - day_open) * (slot + 1) / 288
            bars.append(_bar(open_ms, price, open_price=day_open if slot == 0 else None))
    return bars


def test_aggregate_daily_bars_groups_and_tracks_ohlc() -> None:
    bars = _days_bars(3, base=100.0, drift=0.02, noise=0.0)
    daily = aggregate_daily_bars(bars, symbol="BTCUSDT")
    assert len(daily) == 3
    for item in daily:
        assert item["n_bars"] == 288
        assert item["complete"] is True
    # first day: open=100, close=102 (drift 2%)
    assert daily[0]["open"] == 100.0
    assert daily[0]["close"] == 102.0
    assert daily[0]["low"] <= daily[0]["open"]
    assert daily[0]["high"] >= daily[0]["close"]


def test_build_shadow_rows_pit_and_warmup() -> None:
    bars = _days_bars(100, base=100.0, drift=0.01)  # 100 rising days
    daily = aggregate_daily_bars(bars, symbol="BTCUSDT")
    rows = build_shadow_rows(daily, symbol="BTCUSDT", input_sha256="x" * 64)
    assert len(rows) == 100
    # first 20 rows: tsm_20d warmup (need 20 closes for the 20d return)
    assert rows[0]["warmup"] is True
    assert rows[19]["warmup"] is True
    assert rows[20]["warmup"] is False
    assert rows[0]["tsm_20d"] is None
    # rising market -> 20d TSM positive for later rows
    assert rows[30]["tsm_20d"] > 0
    assert rows[30]["suggested_fraction_20d"] is not None
    assert 0 < rows[30]["suggested_fraction_20d"] <= BASE_POSITION_FRACTION * 3.0
    # falling market -> flat
    falling = _days_bars(100, base=100.0, drift=-0.01)
    falling_daily = aggregate_daily_bars(falling, symbol="ETHUSDT")
    falling_rows = build_shadow_rows(
        falling_daily, symbol="ETHUSDT", input_sha256="y" * 64
    )
    assert falling_rows[40]["tsm_20d"] < 0
    assert falling_rows[40]["suggested_fraction_20d"] == 0.0


def test_ledger_append_idempotent_and_checksum(tmp_path: Path) -> None:
    bars = _days_bars(30, base=100.0, drift=0.005)
    daily = aggregate_daily_bars(bars, symbol="BTCUSDT")
    rows = build_shadow_rows(daily, symbol="BTCUSDT", input_sha256="z" * 64)
    written = append_tsm_shadow_rows(ledger_root=tmp_path, rows=rows)
    assert written == len(rows)
    back = read_ledger(tmp_path, symbol="BTCUSDT")
    assert len(back) == len(rows)
    # re-append same days -> idempotent, no duplication
    written2 = append_tsm_shadow_rows(ledger_root=tmp_path, rows=rows[:3])
    assert written2 == 0
    back2 = read_ledger(tmp_path, symbol="BTCUSDT")
    assert len(back2) == len(rows)
    # unsupported symbol rejected
    try:
        append_tsm_shadow_rows(
            ledger_root=tmp_path,
            rows=[{"symbol": "DOGEUSDT"}],
        )
        raise AssertionError("expected TsmShadowObserverError")
    except TsmShadowObserverError:
        pass


def test_run_tsm_shadow_once_full_flow(tmp_path: Path) -> None:
    bars = {
        "BTCUSDT": _days_bars(40, base=100.0, drift=0.003),
        "ETHUSDT": _days_bars(40, base=50.0, drift=-0.002),
    }
    result = run_tsm_shadow_once(five_minute_bars=bars, ledger_root=tmp_path)
    assert result["contract"] == TSM_SHADOW_CONTRACT
    assert result["status"] == "completed"
    assert result["written"] > 0
    assert result["per_symbol"]["BTCUSDT"]["shadow_rows"] > 0
    assert result["per_symbol"]["ETHUSDT"]["shadow_rows"] > 0
    assert (tmp_path / "BTCUSDT" / "tsm_shadow.ledger.jsonl").exists()
    assert read_ledger(tmp_path, symbol="BTCUSDT")[0]["row_sha256"]
