#!/usr/bin/env python3
"""Run five-minute simulated trading from SharedSignals reader/API data."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL_ROOT))
sys.path.insert(0, os.environ.get("TRADINGAGENT_ROOT", "/opt/investment/tradingagent"))

# Stop a direct script/module invocation before importing the retired reader or
# constructing any output. Market-specific fixture runners replace this mixed
# legacy command.
if __name__ == "__main__":
    from shared.governance.retirement import retired_cli

    raise SystemExit(retired_cli("shared.wrappers.run_sim"))

from PM.probability_model import enrich_pm_rows

market = os.environ.get("SIM_MARKET", "crypto")
market = str(market or "crypto").strip().lower()


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _run_audit_scope() -> dict[str, Any]:
    context = str(os.environ.get("TRADINGAGENT_SIM_RUN_CONTEXT") or "").strip()
    mode = str(os.environ.get("TRADINGAGENT_SIM_RUN_MODE") or "").strip()
    source = str(os.environ.get("TRADINGAGENT_SIM_RUN_SOURCE") or "").strip()
    sample_type = str(os.environ.get("TRADINGAGENT_SIM_SAMPLE_TYPE") or "").strip()
    excluded = _env_enabled("TRADINGAGENT_SIM_EXCLUDE_FROM_DASHBOARD")
    scoped_text = " ".join([context, mode, source, sample_type]).lower()
    if any(token in scoped_text for token in ("maintenance", "backfill", "smoke", "repair", "bootstrap", "dry_run", "dry-run")):
        excluded = True
    payload: dict[str, Any] = {}
    if excluded:
        payload["exclude_from_dashboard"] = True
    if context:
        payload["run_context"] = context
    if mode:
        payload["run_mode"] = mode
    if source:
        payload["run_source"] = source
    if sample_type:
        payload["sample_type"] = sample_type
    return payload


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

DEFAULT_SYMBOLS = {
    "crypto": ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"),
    "us": ("TSLA", "NVDA", "META", "AMZN", "GOOGL", "AMD", "NFLX", "AVGO", "COIN", "PLTR"),
    "hk": ("00700.HK", "09988.HK", "03690.HK", "09618.HK", "00005.HK", "00388.HK"),
}
DEFAULT_HK_PROXY_SYMBOLS = ("HSI",)
CRYPTO_ONE_BAR_THRESHOLD = 0.012
CRYPTO_LOOKBACK_THRESHOLD = 0.025


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


def _priced_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((row for row in rows if _price(row) > 0), key=_row_time)


def _pct_change(latest: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (latest / previous) - 1.0


def _strategy_signal_from_series(name: str, symbol: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    priced = _priced_rows(rows)
    if len(priced) < 2:
        return None
    first = priced[0]
    previous = priced[-2]
    latest = priced[-1]
    latest_price = _price(latest)
    previous_price = _price(previous)
    first_price = _price(first)
    one_bar_return = _pct_change(latest_price, previous_price)
    lookback_return = _pct_change(latest_price, first_price)

    if name == "crypto":
        if one_bar_return < CRYPTO_ONE_BAR_THRESHOLD and lookback_return < CRYPTO_LOOKBACK_THRESHOLD:
            return None
        strategy_name = "crypto_momentum_breakout"
        conviction = min(0.95, max(0.55, 0.55 + one_bar_return * 4 + lookback_return * 2))
    elif name == "us":
        if len(priced) < 3:
            return None
        if not ((one_bar_return >= 0.01 and lookback_return >= 0.02) or lookback_return >= 0.04):
            return None
        strategy_name = "us_trend_follow"
        conviction = min(0.95, max(0.55, 0.55 + one_bar_return * 3 + lookback_return * 2))
    else:
        return None

    return {
        **latest,
        "symbol": str(symbol).strip().upper(),
        "price": latest_price,
        "side": "buy",
        "strategy_name": strategy_name,
        "signal_source": "explicit_strategy_signal",
        "reason": f"{strategy_name}: one_bar_return={one_bar_return:.4f}, lookback_return={lookback_return:.4f}",
        "conviction": round(conviction, 4),
        "score": round(conviction, 4),
    }


def _explicit_trade_side(row: dict[str, Any]) -> str:
    for key in ("side", "action", "direction", "signal", "decision", "recommendation"):
        raw = str(row.get(key) or "").strip().lower()
        if raw in {"buy", "long", "open_long", "increase"}:
            return "buy"
        if raw in {"sell", "short", "open_short", "reduce", "close"}:
            return "sell"
    return ""


def _price_only_signals_enabled() -> bool:
    return _env_enabled("TRADINGAGENT_SIM_ALLOW_PRICE_ONLY_SIGNALS")


def _probability(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _safe_float(row.get(key))
        if 0 < value < 1:
            return value
    return 0.0


def _pm_min_model_edge() -> float:
    raw = os.environ.get("TRADINGAGENT_PM_MIN_MODEL_EDGE", "0.08")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.08
    return value if 0 < value < 1 else 0.08


def _pm_strategy_signal(row: dict[str, Any]) -> dict[str, Any] | None:
    yes_price = _probability(
        row,
        (
            "yes_price",
            "market_price",
            "last_price",
            "price",
            "probability",
            "implied_probability",
        ),
    )
    model_probability = _probability(
        row,
        (
            "model_probability",
            "model_prob",
            "fair_probability",
            "estimated_probability",
        ),
    )
    if not yes_price or not model_probability:
        return None
    edge = model_probability - yes_price
    threshold = _pm_min_model_edge()
    if edge >= threshold:
        outcome = "yes"
        order_price = yes_price
        confidence_edge = edge
    elif -edge >= threshold:
        outcome = "no"
        no_price = _probability(row, ("no_price", "no_market_price"))
        order_price = no_price or max(0.01, min(0.99, 1.0 - yes_price))
        confidence_edge = -edge
    else:
        return None
    conviction = min(0.95, max(0.55, 0.55 + confidence_edge))
    return {
        **row,
        "symbol": str(row.get("symbol") or row.get("market_id") or row.get("slug") or "").strip(),
        "price": order_price,
        "side": "buy",
        "outcome": outcome,
        "strategy_name": "pm_probability_edge",
        "signal_source": "explicit_strategy_signal",
        "reason": f"model_edge={edge:.4f}, threshold={threshold:.4f}",
        "conviction": round(conviction, 4),
        "score": round(conviction, 4),
        "model_probability": model_probability,
        "market_probability": yes_price,
        "edge": round(edge, 4),
    }


def _pm_signal_diagnostics(reader: TradingagentDataReader, limit: int = 10) -> dict[str, Any]:
    rows = enrich_pm_rows(_unwrap_rows(reader.get_pm_markets(limit=limit)))
    priced = [row for row in rows if _price(row) > 0]
    modeled = [
        row for row in priced
        if _probability(row, ("model_probability", "model_prob", "fair_probability", "estimated_probability")) > 0
    ]
    explicit = [row for row in rows if _explicit_trade_side(row)]
    candidates = [row for row in rows if _pm_strategy_signal(row)]
    threshold = _pm_min_model_edge()
    return {
        "market_rows": len(rows),
        "priced_rows": len(priced),
        "modeled_rows": len(modeled),
        "explicit_side_rows": len(explicit),
        "strategy_candidate_rows": len(candidates),
        "min_model_edge": threshold,
        "reason": (
            "pm_market_rows_empty"
            if not rows
            else "pm_prices_missing"
            if not priced
            else "pm_model_probability_missing"
            if not modeled and not explicit
            else "pm_model_edge_below_threshold"
        ),
        "sample": [
            {
                key: row.get(key)
                for key in (
                    "market_id",
                    "symbol",
                    "slug",
                    "yes_price",
                    "no_price",
                    "model_probability",
                    "model_source",
                    "model_reason",
                    "fair_probability",
                    "estimated_probability",
                    "side",
                    "decision",
                )
            }
            for row in rows[:3]
        ],
    }


def _crypto_signal_diagnostics(reader: TradingagentDataReader, limit: int = 10) -> dict[str, Any]:
    symbols = _symbols_for_market("crypto")
    samples: list[dict[str, Any]] = []
    total_priced_rows = 0
    explicit_side_rows = 0
    strategy_candidate_rows = 0
    symbols_with_priced_rows = 0
    insufficient_rows_symbols: list[str] = []
    below_threshold_symbols: list[str] = []
    no_priced_symbols: list[str] = []

    for symbol in symbols:
        rows = _unwrap_rows(reader.get_crypto_klines(symbol=symbol, limit=50))
        priced = _priced_rows(rows)
        total_priced_rows += len(priced)
        explicit_side_rows += sum(1 for row in rows if _explicit_trade_side(row))
        strategy_signal = _strategy_signal_from_series("crypto", symbol, rows)
        if strategy_signal:
            strategy_candidate_rows += 1
        if priced:
            symbols_with_priced_rows += 1
        if not priced:
            reason = "crypto_klines_empty"
            no_priced_symbols.append(symbol)
            one_bar_return = 0.0
            lookback_return = 0.0
            latest_price = 0.0
        elif len(priced) < 2:
            reason = "crypto_insufficient_priced_rows"
            insufficient_rows_symbols.append(symbol)
            one_bar_return = 0.0
            lookback_return = 0.0
            latest_price = _price(priced[-1])
        else:
            latest_price = _price(priced[-1])
            one_bar_return = _pct_change(latest_price, _price(priced[-2]))
            lookback_return = _pct_change(latest_price, _price(priced[0]))
            reason = "crypto_strategy_candidate" if strategy_signal else "crypto_momentum_threshold_not_met"
            if not strategy_signal:
                below_threshold_symbols.append(symbol)
        if len(samples) < min(limit, 5):
            samples.append(
                {
                    "symbol": symbol,
                    "rows": len(rows),
                    "priced_rows": len(priced),
                    "latest_price": latest_price,
                    "latest_time": _row_time(priced[-1]) if priced else "",
                    "one_bar_return": round(one_bar_return, 6),
                    "lookback_return": round(lookback_return, 6),
                    "reason": reason,
                }
            )

    if not symbols:
        reason = "crypto_symbols_empty"
    elif total_priced_rows <= 0:
        reason = "crypto_klines_empty"
    elif strategy_candidate_rows <= 0 and insufficient_rows_symbols and not below_threshold_symbols:
        reason = "crypto_insufficient_priced_rows"
    elif strategy_candidate_rows <= 0:
        reason = "crypto_momentum_threshold_not_met"
    else:
        reason = "crypto_strategy_candidates_available"
    return {
        "symbols_checked": len(symbols),
        "symbols_with_priced_rows": symbols_with_priced_rows,
        "total_priced_rows": total_priced_rows,
        "explicit_side_rows": explicit_side_rows,
        "strategy_candidate_rows": strategy_candidate_rows,
        "momentum_thresholds": {
            "one_bar_return": CRYPTO_ONE_BAR_THRESHOLD,
            "lookback_return": CRYPTO_LOOKBACK_THRESHOLD,
        },
        "reason": reason,
        "no_priced_symbols": no_priced_symbols,
        "insufficient_rows_symbols": insufficient_rows_symbols,
        "below_threshold_symbols": below_threshold_symbols,
        "sample": samples,
    }


def _signal_diagnostics(reader: TradingagentDataReader, name: str, limit: int = 10) -> dict[str, Any]:
    if name == "pm":
        return _pm_signal_diagnostics(reader, limit=limit)
    if name == "crypto":
        return _crypto_signal_diagnostics(reader, limit=limit)
    return {}


def _lookback_window(days: int = 10) -> tuple[str, str]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _load_signals(reader: TradingagentDataReader, name: str, limit: int = 10) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if name == "pm":
        source_rows = []
        for row in enrich_pm_rows(_unwrap_rows(reader.get_pm_markets(limit=limit))):
            if _explicit_trade_side(row):
                source_rows.append(row)
                continue
            strategy_signal = _pm_strategy_signal(row)
            if strategy_signal:
                source_rows.append(strategy_signal)
            elif _price_only_signals_enabled():
                source_rows.append(row)
    elif name == "crypto":
        source_rows = []
        for symbol in _symbols_for_market(name):
            rows = _unwrap_rows(reader.get_crypto_klines(symbol=symbol, limit=50))
            latest = _latest(rows)
            if latest and _explicit_trade_side(latest):
                source_rows.append(latest)
                continue
            strategy_signal = _strategy_signal_from_series(name, symbol, rows)
            if strategy_signal:
                source_rows.append(strategy_signal)
            elif latest and _price_only_signals_enabled():
                source_rows.append(latest)
    elif name in {"us", "hk"}:
        source_rows = []
        start, end = _lookback_window()
        market_name = "HK" if name == "hk" else "US"
        for symbol in _symbols_for_market(name):
            rows = _unwrap_rows(
                reader.get_market_data(
                    ts_code=symbol,
                    market=market_name,
                    start=start,
                    end=end,
                    freq="daily",
                )
            )
            latest = _latest(rows)
            if latest:
                latest.setdefault("symbol", symbol)
                if _explicit_trade_side(latest):
                    source_rows.append(latest)
                    continue
            strategy_signal = _strategy_signal_from_series(name, symbol, rows)
            if strategy_signal:
                source_rows.append(strategy_signal)
            elif latest and _price_only_signals_enabled():
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
        side = _explicit_trade_side(row)
        price_only = not side
        if price_only and not _price_only_signals_enabled():
            continue
        signals.append(
            {
                "symbol": symbol,
                "price": price,
                "trade_date": _row_time(row),
                "side": side or "buy",
                "quantity": 1,
                "market": name,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_execution": False,
                "data_source": str(row.get("data_source") or "SharedSignals reader/API"),
                **({"exclude_from_dashboard": True, "sample_type": "price_only_smoke"} if price_only else {}),
            }
        )
        for key in (
            "outcome",
            "market_id",
            "strategy_name",
            "reason",
            "conviction",
            "score",
            "belief_score",
            "signal_source",
            "model_probability",
            "model_source",
            "model_reason",
            "model_confidence",
            "market_probability",
            "edge",
        ):
            if row.get(key) not in (None, ""):
                signals[-1][key] = row[key]
        if len(signals) >= limit:
            break
    return signals


def main() -> int:
    from shared.governance.retirement import retired_cli

    return retired_cli("shared.wrappers.run_sim")


if __name__ == "__main__":
    raise SystemExit(main())
