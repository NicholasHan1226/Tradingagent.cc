#!/usr/bin/env python3
"""Run five-minute simulated trading from SharedSignals reader/API data."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL_ROOT))
sys.path.insert(0, os.environ.get("TRADINGAGENT_ROOT", "/opt/investment/tradingagent"))
sys.path.insert(0, os.environ.get("SHARED_SIGNALS_ROOT", "/opt/investment/SharedSignals"))

os.environ.setdefault("SHAREDSIGNALS_API_URL", "http://127.0.0.1:8082")
os.environ.setdefault(
    "SHARED_SIGNALS_DB",
    "/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite",
)

from shared.data.reader import TradingagentDataReader
from shared.markets.style_runner import StyleRunner

market = os.environ.get("SIM_MARKET", "crypto")
market = str(market or "crypto").strip().lower()


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


if market == "hk" and not _env_enabled("TRADINGAGENT_HK_SIM_ENABLED"):
    print(
        json.dumps(
            {
                "market": market,
                "status": "disabled",
                "signals": 0,
                "reason": "hk_sim_paused",
                "enable_with": "TRADINGAGENT_HK_SIM_ENABLED=1",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

configs = {
    "crypto": {
        "sim_mod": "Crypto.simulator",
        "sim_cls": "CryptoSimulator",
        "cfg_mod": "Crypto.common",
        "cfg_cls": "CryptoConfig",
    },
    "pm": {
        "sim_mod": "PM.simulator",
        "sim_cls": "PMSimulator",
        "cfg_mod": "PM.common",
        "cfg_cls": "PMConfig",
    },
    "us": {
        "sim_mod": "US.simulator",
        "sim_cls": "USSimulator",
        "cfg_mod": "US.common",
        "cfg_cls": "USConfig",
    },
    "hk": {
        "sim_mod": "HK.simulator",
        "sim_cls": "HKSimulator",
        "cfg_mod": "HK.common",
        "cfg_cls": "HKConfig",
    },
}
cfg = configs.get(market, configs["crypto"])

DEFAULT_SYMBOLS = {
    "crypto": ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"),
    "us": ("TSLA", "NVDA", "META", "AMZN", "GOOGL", "AMD", "NFLX", "AVGO", "COIN", "PLTR"),
    "hk": ("00700.HK", "09988.HK", "03690.HK", "09618.HK", "00005.HK", "00388.HK"),
}
DEFAULT_HK_PROXY_SYMBOLS = ("HSI",)


def _symbols_for_market(name: str) -> tuple[str, ...]:
    raw = os.environ.get(f"SIM_{name.upper()}_SYMBOLS") or os.environ.get("SIM_SYMBOLS")
    if raw:
        return tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    return DEFAULT_SYMBOLS.get(name, ())


def _hk_proxy_symbols() -> tuple[str, ...]:
    raw = os.environ.get("SIM_HK_PROXY_SYMBOLS")
    if raw:
        return tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    return DEFAULT_HK_PROXY_SYMBOLS


def _unwrap_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data", [rows])
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = row.get("data")
        result.append(dict(data) if isinstance(data, dict) else dict(row))
    return result


def _safe_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number > 0 else 0.0


def _price(row: dict[str, Any]) -> float:
    for key in (
        "latest_price",
        "price",
        "close",
        "last_price",
        "market_price",
        "yes_price",
        "probability",
    ):
        value = _safe_float(row.get(key))
        if value > 0:
            return value
    return 0.0


def _row_time(row: dict[str, Any]) -> str:
    return str(
        row.get("trade_date")
        or row.get("price_time")
        or row.get("latest_price_time")
        or row.get("collected_at")
        or row.get("open_time")
        or ""
    )


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    priced = [row for row in rows if _price(row) > 0]
    if not priced:
        return None
    return sorted(priced, key=_row_time)[-1]


def _lookback_window(days: int = 10) -> tuple[str, str]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _load_signals(reader: TradingagentDataReader, name: str, limit: int = 10) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if name == "pm":
        source_rows = _unwrap_rows(reader.get_pm_markets(limit=limit))
    elif name == "crypto":
        source_rows = []
        for symbol in _symbols_for_market(name):
            latest = _latest(_unwrap_rows(reader.get_crypto_klines(symbol=symbol, limit=50)))
            if latest:
                source_rows.append(latest)
    elif name in {"us", "hk"}:
        source_rows = []
        start, end = _lookback_window()
        market_name = "HK" if name == "hk" else "US"
        for symbol in _symbols_for_market(name):
            latest = _latest(
                _unwrap_rows(
                    reader.get_market_data(
                        ts_code=symbol,
                        market=market_name,
                        start=start,
                        end=end,
                        freq="daily",
                    )
                )
            )
            if latest:
                latest.setdefault("symbol", symbol)
                source_rows.append(latest)
        if name == "hk" and not source_rows and _env_enabled("SIM_HK_PROXY_ENABLED"):
            for symbol in _hk_proxy_symbols():
                latest = _latest(
                    _unwrap_rows(
                        reader.get_market_data(
                            ts_code=symbol,
                            market="Global",
                            start=start,
                            end=end,
                            freq="daily",
                        )
                    )
                )
                if latest:
                    latest["symbol"] = symbol
                    latest["market_proxy_for"] = "HK"
                    latest["data_source"] = f"SharedSignals HK proxy/{symbol}"
                    source_rows.append(latest)
    else:
        source_rows = []

    for row in source_rows:
        symbol = str(row.get("symbol") or row.get("market_id") or row.get("ts_code") or "").strip()
        price = _price(row)
        if not symbol or price <= 0:
            continue
        signals.append(
            {
                "symbol": symbol,
                "price": price,
                "trade_date": _row_time(row),
                "side": "buy",
                "quantity": 1,
                "market": name,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_execution": False,
                "data_source": str(row.get("data_source") or "SharedSignals reader/API"),
            }
        )
        if len(signals) >= limit:
            break
    return signals


reader = TradingagentDataReader()
signals = _load_signals(reader, market)

if not signals:
    print(
        json.dumps(
            {
                "market": market,
                "status": "no_data",
                "signals": 0,
                "data_source": "SharedSignals reader/API",
                "reader_degraded": bool(reader.degraded or reader.stale),
                "reader_errors": reader.errors[-5:],
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

sim_mod = __import__(cfg["sim_mod"], fromlist=[cfg["sim_cls"]])
cfg_mod = __import__(cfg["cfg_mod"], fromlist=[cfg["cfg_cls"]])
config = getattr(cfg_mod, cfg["cfg_cls"])()
simulator = getattr(sim_mod, cfg["sim_cls"])(config=config)

from datetime import date

runner = StyleRunner(market, simulator)
result = runner.run(signals, date=str(date.today()))

print(
    json.dumps(
        {
            "market": market,
            "status": "ok",
            "signals": len(signals),
            "data_rows": len(signals),
            "timestamp": str(date.today()),
            "data_source": "SharedSignals reader/API",
            "reader_degraded": bool(reader.degraded or reader.stale),
            "reader_errors": reader.errors[-5:],
        },
        ensure_ascii=False,
        default=str,
    )
)
