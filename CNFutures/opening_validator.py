#!/usr/bin/env python3
"""Read-only opening validation for CN futures 5-minute data."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .review import DEFAULT_REVIEW_PATH
    from .contract_rules import get_contract_rule, is_executable_contract_symbol, normalize_product
except ImportError:  # pragma: no cover - direct script execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from CNFutures.review import DEFAULT_REVIEW_PATH
    from CNFutures.contract_rules import get_contract_rule, is_executable_contract_symbol, normalize_product

try:
    from shared.data.reader import TradingagentDataReader
except Exception:  # pragma: no cover
    TradingagentDataReader = None  # type: ignore[assignment]

DEFAULT_SQLITE_DB = Path("/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite")
DEFAULT_SIGNALS_DIR = Path(__file__).resolve().parents[1] / "signals"
DEFAULT_RECEIPT_PATH = Path(__file__).resolve().parents[1] / "signals" / "sim_execution_receipts.jsonl"
DEFAULT_STYLE_WEIGHTS_PATH = Path(__file__).resolve().parents[1] / "shared" / "review" / "cn_futures" / "style_weights.json"
CN_TZ = timezone(timedelta(hours=8))
READER_MARKET = "Futures"


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _parse_now(value: str | None) -> datetime:
    if not value:
        return _now_cn()
    parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _session_start(now: datetime) -> tuple[str, datetime | None]:
    current = now.time()
    if time(9, 0) <= current <= time(15, 0):
        return "day", datetime.combine(now.date(), time(9, 0), tzinfo=CN_TZ)
    if current >= time(21, 0):
        return "night", datetime.combine(now.date(), time(21, 0), tzinfo=CN_TZ)
    if current <= time(2, 30):
        return "night", datetime.combine(now.date() - timedelta(days=1), time(21, 0), tzinfo=CN_TZ)
    return "closed", None


def _pre_open_session(now: datetime) -> tuple[str, datetime | None]:
    current = now.time()
    if time(8, 0) <= current < time(9, 0):
        return "day", datetime.combine(now.date(), time(9, 0), tzinfo=CN_TZ)
    if time(12, 0) <= current < time(13, 0):
        return "afternoon", datetime.combine(now.date(), time(13, 0), tzinfo=CN_TZ)
    if time(20, 0) <= current < time(21, 0):
        return "night", datetime.combine(now.date(), time(21, 0), tzinfo=CN_TZ)
    return "closed", None


def _default_reader() -> Any | None:
    if TradingagentDataReader is None:
        return None
    try:
        return TradingagentDataReader()
    except Exception:
        return None


def _reader_for_db(db_path: Path, reader: Any | None) -> Any | None:
    if reader is not None:
        return reader
    try:
        if db_path.resolve() != DEFAULT_SQLITE_DB.resolve():
            return None
    except OSError:
        if str(db_path) != str(DEFAULT_SQLITE_DB):
            return None
    return _default_reader()


def _compact_date(value: Any) -> str:
    return str(value or "").strip()[:10].replace("-", "")


def _reader_daily_start(trade_date: str, *, lookback_days: int = 30) -> str:
    compact = _compact_date(trade_date)
    try:
        parsed = datetime.strptime(compact, "%Y%m%d")
    except ValueError:
        return ""
    return (parsed - timedelta(days=max(1, int(lookback_days)))).strftime("%Y%m%d")


def _reader_symbols(reader: Any | None, *, limit: int = 80, as_of: str = "") -> list[str]:
    if reader is None:
        return []
    get_assets = getattr(reader, "get_assets", None)
    if not callable(get_assets):
        return []
    try:
        rows = get_assets(market=READER_MARKET)
    except TypeError:
        try:
            rows = get_assets(READER_MARKET)
        except Exception:
            return []
    except Exception:
        return []
    symbols: list[str] = []
    seen: set[str] = set()
    as_of_compact = _compact_date(as_of)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ts_code") or "").strip().upper()
        if not is_executable_contract_symbol(symbol):
            continue
        try:
            get_contract_rule(symbol)
        except ValueError:
            continue
        if as_of_compact:
            list_date = _compact_date(row.get("list_date"))
            expiry_date = _compact_date(row.get("expiry_date"))
            last_trade_date = _compact_date(row.get("last_trade_date"))
            if list_date and list_date > as_of_compact:
                continue
            if expiry_date and expiry_date < as_of_compact:
                continue
            if last_trade_date and last_trade_date < as_of_compact:
                continue
        key = symbol.lower()
        if not symbol or key in seen:
            continue
        seen.add(key)
        symbols.append(symbol)
        if len(symbols) >= max(1, int(limit)):
            break
    return symbols


def _contract_coverage(symbols: list[str]) -> dict[str, Any]:
    raw_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    executable: list[str] = []
    unsupported_products: set[str] = set()
    products: set[str] = set()
    for symbol in raw_symbols:
        if not is_executable_contract_symbol(symbol):
            continue
        try:
            product = normalize_product(symbol)
            get_contract_rule(symbol)
        except ValueError:
            try:
                product_name = normalize_product(symbol)
            except ValueError:
                product_name = str(symbol).split(".", 1)[0].lower()
            unsupported_products.add(product_name)
            continue
        products.add(product)
        executable.append(symbol)
    return {
        "raw_symbol_count": len(raw_symbols),
        "symbol_count": len(executable),
        "executable_symbol_count": len(executable),
        "executable_symbols_sample": executable[:12],
        "product_coverage": sorted(products),
        "product_count": len(products),
        "unsupported_products": sorted(unsupported_products),
    }


def _query_daily_bars_via_reader(reader: Any | None, trade_date: str, *, min_symbols: int) -> dict[str, Any]:
    get_bars_daily = getattr(reader, "get_bars_daily", None)
    if not callable(get_bars_daily):
        return {"error": "sharedsignals_reader_unavailable", "symbol_count": 0}
    symbols = _reader_symbols(reader, limit=max(80, int(min_symbols) * 20), as_of=trade_date)
    if not symbols:
        return {"error": "futures_assets_empty_from_sharedsignals_reader", "symbol_count": 0}
    latest_dates: list[str] = []
    daily_bar_count = 0
    priced_symbols: list[str] = []
    start_date = _reader_daily_start(trade_date)
    for symbol in symbols:
        try:
            rows = get_bars_daily(READER_MARKET, symbol, start_date, trade_date)
        except Exception:
            rows = []
        priced_rows = [
            dict(row)
            for row in rows or []
            if float(dict(row).get("close") or 0) > 0
        ]
        if not priced_rows:
            continue
        daily_bar_count += len(priced_rows)
        priced_symbols.append(symbol)
        latest_dates.extend(str(row.get("trade_date") or "") for row in priced_rows if row.get("trade_date"))
    coverage = _contract_coverage(priced_symbols)
    return {
        "daily_bar_count": daily_bar_count,
        **coverage,
        "first_trade_date": min(latest_dates) if latest_dates else None,
        "latest_trade_date": max(latest_dates) if latest_dates else None,
        "query_source": "TradingagentDataReader",
    }


def _query_daily_bars_sqlite(db_path: Path, trade_date: str) -> dict[str, Any]:
    if not db_path.exists():
        return {"error": f"sqlite database not found: {db_path}", "symbol_count": 0}
    start_date = _reader_daily_start(trade_date)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            """
            SELECT symbol, COUNT(*) AS bar_count, MIN(trade_date), MAX(trade_date)
            FROM market_bars_daily
            WHERE market='Futures'
              AND (? = '' OR trade_date >= ?)
              AND trade_date <= ?
              AND close > 0
            GROUP BY symbol
            """,
            (start_date, start_date, trade_date),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}", "symbol_count": 0}
    finally:
        if conn is not None:
            conn.close()
    symbols = [str(row[0] or "") for row in rows or []]
    coverage = _contract_coverage(symbols)
    latest_dates = [str(row[3] or "") for row in rows or [] if row[3]]
    first_dates = [str(row[2] or "") for row in rows or [] if row[2]]
    return {
        "daily_bar_count": sum(int(row[1] or 0) for row in rows or []),
        **coverage,
        "first_trade_date": min(first_dates) if first_dates else None,
        "latest_trade_date": max(latest_dates) if latest_dates else None,
    }


def _query_intraday_readiness_sqlite(db_path: Path, trade_date: str) -> dict[str, Any]:
    if not db_path.exists():
        return {"reachable": False, "error": f"sqlite database not found: {db_path}", "bar_count": 0, "symbol_count": 0}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(substr(bar_time, 1, 10)), MAX(bar_time)
            FROM market_bars_intraday
            WHERE market='Futures'
              AND lower(COALESCE(interval, '')) IN ('5min', '5m', '5')
              AND replace(substr(bar_time, 1, 10), '-', '') <= ?
            """,
            (trade_date,),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{exc.__class__.__name__}: {exc}", "bar_count": 0, "symbol_count": 0}
    finally:
        if conn is not None:
            conn.close()
    bar_count = int(row[0] or 0) if row else 0
    return {
        "reachable": bar_count > 0,
        "bar_count": bar_count,
        "symbol_count": int(row[1] or 0) if row else 0,
        "latest_trade_date": row[2] if row else None,
        "latest_bar_time": row[3] if row else None,
        "query_source": "SharedSignals read_model/sqlite",
    }


def _style_state_summary(path: Path = DEFAULT_STYLE_WEIGHTS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "style_count": 0, "active_styles": 0, "paused_styles": 0, "real_trading_enabled": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"path": str(path), "exists": True, "error": f"{exc.__class__.__name__}: {exc}", "style_count": 0, "active_styles": 0, "paused_styles": 0, "real_trading_enabled": False}
    styles = payload.get("styles") if isinstance(payload, dict) else {}
    if not isinstance(styles, dict):
        styles = {}
    active = 0
    paused = 0
    night_allowed = 0
    for style in styles.values():
        if not isinstance(style, dict):
            continue
        status = str(style.get("status") or "active").lower()
        is_paused = status in {"paused", "deprecated", "disabled"} or style.get("paused") is True
        paused += 1 if is_paused else 0
        active += 0 if is_paused else 1
        night_allowed += 1 if style.get("night_session_allowed") is True else 0
    return {
        "path": str(path),
        "exists": True,
        "style_count": len(styles),
        "active_styles": active,
        "paused_styles": paused,
        "night_session_allowed_styles": night_allowed,
        "real_trading_enabled": bool(payload.get("real_trading_enabled")) if isinstance(payload, dict) else False,
    }


def _query_session_bars_sqlite(db_path: Path, start: datetime, now: datetime) -> dict[str, Any]:
    if not db_path.exists():
        return {"error": f"sqlite database not found: {db_path}", "symbol_count": 0, "bar_count": 0}
    start_text = start.strftime("%Y-%m-%d %H:%M:%S")
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(bar_time), MAX(bar_time)
            FROM market_bars_intraday
            WHERE market='Futures'
              AND lower(COALESCE(interval, '')) IN ('5min', '5m', '5')
              AND bar_time >= ?
              AND bar_time <= ?
            """,
            (start_text, now_text),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}", "symbol_count": 0, "bar_count": 0}
    finally:
        if conn is not None:
            conn.close()
    bar_count = int(row[0] or 0) if row else 0
    symbol_count = int(row[1] or 0) if row else 0
    return {
        "bar_count": bar_count,
        "symbol_count": symbol_count,
        "first_bar_time": row[2] if row else None,
        "latest_bar_time": row[3] if row else None,
    }


def _query_session_bars_via_reader(
    reader: Any | None,
    start: datetime,
    now: datetime,
    *,
    min_symbols: int,
) -> dict[str, Any]:
    get_bars_intraday = getattr(reader, "get_bars_intraday", None)
    if not callable(get_bars_intraday):
        return {"error": "sharedsignals_reader_unavailable", "symbol_count": 0, "bar_count": 0}
    symbols = _reader_symbols(reader, limit=max(80, int(min_symbols) * 20), as_of=start.strftime("%Y%m%d"))
    if not symbols:
        return {"error": "futures_assets_empty_from_sharedsignals_reader", "symbol_count": 0, "bar_count": 0}
    start_text = start.strftime("%Y-%m-%d %H:%M:%S")
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    first_bar = ""
    latest_bar = ""
    bar_count = 0
    symbol_count = 0
    for symbol in symbols:
        try:
            rows = get_bars_intraday(READER_MARKET, symbol, "5min", start.strftime("%Y%m%d"), now.strftime("%Y%m%d"))
        except Exception:
            rows = []
        in_session = []
        for row in rows or []:
            payload = dict(row)
            bar_time = str(payload.get("bar_time") or payload.get("time") or "")
            if start_text <= bar_time <= now_text:
                in_session.append(payload)
        if not in_session:
            continue
        symbol_count += 1
        bar_count += len(in_session)
        times = [str(row.get("bar_time") or row.get("time") or "") for row in in_session]
        if times:
            first_bar = min([first_bar, *times]) if first_bar else min(times)
            latest_bar = max([latest_bar, *times]) if latest_bar else max(times)
    return {
        "bar_count": bar_count,
        "symbol_count": symbol_count,
        "first_bar_time": first_bar or None,
        "latest_bar_time": latest_bar or None,
        "query_source": "TradingagentDataReader",
    }


def _allow_sqlite_fallback(sqlite_db: Path) -> bool:
    value = sqlite_db.exists()
    env_value = os.environ.get("CN_FUTURES_ALLOW_DIRECT_SQLITE_FALLBACK", "")
    env_disabled = env_value.strip().lower() in {"0", "false", "no", "off"}
    return (value and not env_disabled) or env_value.strip().lower() in {"1", "true", "yes", "on"}


def _query_daily_bars(db_path: Path, trade_date: str, *, reader: Any | None = None, min_symbols: int = 4) -> dict[str, Any]:
    payload = _query_daily_bars_via_reader(_reader_for_db(db_path, reader), trade_date, min_symbols=min_symbols)
    if not payload.get("error") and int(payload.get("symbol_count") or 0) >= max(1, int(min_symbols)):
        return payload
    if _allow_sqlite_fallback(db_path):
        fallback = _query_daily_bars_sqlite(db_path, trade_date)
        fallback["query_source"] = "SharedSignals read_model/sqlite"
        if payload.get("error"):
            fallback["reader_error"] = payload.get("error")
        elif int(payload.get("symbol_count") or 0) < max(1, int(min_symbols)):
            fallback["reader_shortfall"] = payload
        return fallback
    return payload


def _query_session_bars(db_path: Path, start: datetime, now: datetime, *, reader: Any | None = None, min_symbols: int = 4) -> dict[str, Any]:
    payload = _query_session_bars_via_reader(_reader_for_db(db_path, reader), start, now, min_symbols=min_symbols)
    if (
        not payload.get("error")
        and int(payload.get("bar_count") or 0) > 0
        and int(payload.get("symbol_count") or 0) >= max(1, int(min_symbols))
    ):
        return payload
    if _allow_sqlite_fallback(db_path):
        fallback = _query_session_bars_sqlite(db_path, start, now)
        fallback["query_source"] = "SharedSignals read_model/sqlite"
        if payload.get("error"):
            fallback["reader_error"] = payload.get("error")
        elif (
            int(payload.get("bar_count") or 0) <= 0
            or int(payload.get("symbol_count") or 0) < max(1, int(min_symbols))
        ):
            fallback["reader_shortfall"] = payload
        return fallback
    return payload


def _read_latest_review(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return {}
    for line in reversed(lines):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _count_jsonl_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())


def _count_filled_signals(signals_dir: Path, date: str) -> int:
    filled_dir = signals_dir / "filled"
    if not filled_dir.exists():
        return 0
    trade_date = date.replace("-", "")
    count = 0
    for path in filled_dir.glob("SIM-CNF-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        card_date = str(payload.get("trade_date") or payload.get("valid_until") or payload.get("bar_time") or "")[:10].replace("-", "")
        if card_date == trade_date:
            count += 1
    return count


def _count_market_receipts(receipt_path: Path, date: str) -> int:
    if not receipt_path.exists():
        return 0
    count = 0
    for line in receipt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("market") or "").lower() != "cn_futures":
            continue
        receipt_date = str(payload.get("trade_date") or payload.get("receipt_at") or "")[:10].replace("-", "")
        if receipt_date == date:
            count += 1
    return count


def _opening_30m_review(
    *,
    bars: dict[str, Any],
    latest_review: dict[str, Any],
    filled_signal_count: int,
    receipt_count: int,
    elapsed_minutes: int | None,
    min_symbols: int,
) -> dict[str, Any]:
    bar_count = int(bars.get("bar_count") or 0)
    symbol_count = int(bars.get("symbol_count") or 0)
    hold_summary = latest_review.get("hold_reason_summary", {}) if isinstance(latest_review.get("hold_reason_summary"), dict) else {}
    hold_by_reason = hold_summary.get("by_reason") if isinstance(hold_summary.get("by_reason"), dict) else {}
    hold_count = int(latest_review.get("hold_count") or hold_summary.get("total") or 0) if latest_review else 0
    top_hold_reason = ""
    if hold_by_reason:
        top_hold_reason = max(hold_by_reason.items(), key=lambda item: int(item[1] or 0))[0]
    if elapsed_minutes is None:
        status = "waiting"
        phase = "outside_session"
        action = "wait_for_next_session"
    elif elapsed_minutes < 30:
        status = "pass"
        phase = "accumulating_opening_30m"
        action = "continue_accumulating_samples"
    elif bars.get("error"):
        status = "warn"
        phase = "data_query_failed"
        action = "check_sharedsignals_futures_read_model"
    elif bar_count <= 0 or symbol_count < max(1, int(min_symbols)):
        status = "warn"
        phase = "insufficient_5min_data"
        action = "check_cn_futures_5min_collector"
    elif int(latest_review.get("filled_count") or 0) <= 0 and filled_signal_count <= 0 and hold_count > 0:
        if top_hold_reason in {"style_session_not_allowed", "night_session_not_allowed"}:
            status = "pass"
            phase = "no_night_session"
            action = "enable_only_explicit_night_session_styles_or_wait_for_day_session"
        else:
            status = "warn"
            phase = "strategy_hold"
            action = "review_hold_reasons_and_strategy_filters"
    elif int(latest_review.get("filled_count") or 0) <= 0 and filled_signal_count <= 0:
        status = "warn"
        phase = "no_simulated_trade"
        action = "check_sim_runner_after_data_ready"
    elif filled_signal_count > 0 and receipt_count <= 0:
        status = "warn"
        phase = "receipt_missing"
        action = "check_cn_futures_receipt_writer"
    else:
        status = "pass"
        phase = "opening_30m_ready"
        action = "continue_observation"
    return {
        "window_minutes": 30,
        "status": status,
        "phase": phase,
        "next_action": action,
        "top_hold_reason": top_hold_reason,
        "inputs": {
            "elapsed_minutes": elapsed_minutes,
            "bar_count": bar_count,
            "symbol_count": symbol_count,
            "min_symbols": max(1, int(min_symbols)),
            "latest_review_exists": bool(latest_review),
            "latest_review_filled_count": int(latest_review.get("filled_count") or 0) if latest_review else 0,
            "latest_review_hold_count": hold_count,
            "filled_signals": filled_signal_count,
            "sim_execution_receipts": receipt_count,
        },
    }


def validate_pre_open(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    now: datetime | None = None,
    min_symbols: int = 4,
) -> dict[str, Any]:
    current = now or _now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    session_name, start = _pre_open_session(current)
    result: dict[str, Any] = {
        "market": "cn_futures",
        "report_type": "pre_open_acceptance",
        "checked_at": current.isoformat(timespec="seconds"),
        "sqlite_db": str(sqlite_db),
        "data_source": "SharedSignals read_model",
        "read_only": True,
        "session": session_name,
        "session_start": start.isoformat(timespec="seconds") if start else None,
        "min_symbols": max(1, int(min_symbols)),
        "real_trading_enabled": False,
    }
    if start is None:
        return {**result, "status": "warn", "reason": "not_in_pre_open_window"}
    bars = _query_daily_bars(sqlite_db, start.strftime("%Y%m%d"))
    intraday_readiness = _query_intraday_readiness_sqlite(sqlite_db, start.strftime("%Y%m%d"))
    style_state = _style_state_summary()
    warnings: list[str] = []
    if not intraday_readiness.get("reachable"):
        warnings.append("intraday_read_model_not_ready")
    if style_state.get("exists") and int(style_state.get("style_count") or 0) > 0 and int(style_state.get("active_styles") or 0) <= 0:
        warnings.append("no_active_cn_futures_styles")
    if style_state.get("real_trading_enabled"):
        warnings.append("real_trading_enabled_unexpected_for_simulated_only_market")
    result.update(bars)
    result["intraday_readiness"] = intraday_readiness
    result["style_state"] = style_state
    result["warnings"] = warnings
    if bars.get("error"):
        result["status"] = "fail"
        result["reason"] = "pre_open_daily_query_failed"
    elif int(bars.get("symbol_count") or 0) < max(1, int(min_symbols)):
        result["status"] = "warn"
        result["reason"] = "pre_open_executable_daily_bars_missing"
    elif "intraday_read_model_not_ready" in warnings or "no_active_cn_futures_styles" in warnings:
        result["status"] = "warn"
        result["reason"] = warnings[0]
    else:
        result["status"] = "pass"
        result["reason"] = "pre_open_acceptance_passed"
    return result


def first_sample_alerts(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    review_path: Path = DEFAULT_REVIEW_PATH,
    signals_dir: Path = DEFAULT_SIGNALS_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    min_symbols: int = 4,
    wait_minutes: int = 10,
) -> dict[str, Any]:
    current = now or _now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    session_name, start = _session_start(current)
    elapsed_minutes = int((current - start).total_seconds() // 60) if start is not None else None
    result: dict[str, Any] = {
        "market": "cn_futures",
        "report_type": "first_sample_alert",
        "checked_at": current.isoformat(timespec="seconds"),
        "sqlite_db": str(sqlite_db),
        "review_path": str(review_path),
        "data_source": "SharedSignals read_model",
        "read_only": True,
        "session": session_name,
        "session_start": start.isoformat(timespec="seconds") if start else None,
        "elapsed_minutes": elapsed_minutes,
        "min_symbols": max(1, int(min_symbols)),
        "wait_minutes": max(1, int(wait_minutes)),
        "alerts": [],
        "real_trading_enabled": False,
    }
    if start is None:
        return {**result, "status": "warn", "reason": "outside_cn_futures_session"}
    if elapsed_minutes is not None and elapsed_minutes < max(1, int(wait_minutes)):
        return {**result, "status": "pass", "reason": "first_sample_check_not_due"}

    bars = _query_session_bars(sqlite_db, start, current)
    result.update(bars)
    alerts: list[dict[str, Any]] = []
    if bars.get("error"):
        alerts.append({"severity": "error", "code": "futures_5min_check_failed", "message": "期货5分钟首样本检查无法读取 SharedSignals read model。"})
    elif int(bars.get("bar_count") or 0) <= 0 or int(bars.get("symbol_count") or 0) < max(1, int(min_symbols)):
        alerts.append({"severity": "warn", "code": "futures_5min_missing_in_session", "message": "期货交易时段开始后仍缺少足够的 Futures 5分钟数据。"})

    latest_review = _read_latest_review(review_path)
    latest_filled_count = int(latest_review.get("filled_count") or 0) if latest_review else 0
    trade_date = current.strftime("%Y%m%d")
    filled_signal_count = _count_filled_signals(signals_dir, trade_date)
    receipt_count = _count_market_receipts(receipt_path, trade_date)
    result["latest_review"] = {
        "exists": bool(latest_review),
        "generated_at": latest_review.get("generated_at", "") if latest_review else "",
        "latest_bar_time": latest_review.get("latest_bar_time") or latest_review.get("bar_time") or "",
        "filled_count": latest_filled_count,
        "real_trading_enabled": bool(latest_review.get("real_trading_enabled")) if latest_review else False,
    }
    result["samples"] = {
        "filled_signals": filled_signal_count,
        "sim_execution_receipts": receipt_count,
        "review_rows": _count_jsonl_rows(review_path),
    }
    result["opening_30m_review"] = _opening_30m_review(
        bars=bars,
        latest_review=latest_review,
        filled_signal_count=filled_signal_count,
        receipt_count=receipt_count,
        elapsed_minutes=elapsed_minutes,
        min_symbols=min_symbols,
    )
    if result["opening_30m_review"]["status"] == "warn":
        alerts.append({
            "severity": "warn",
            "code": f"cn_futures_opening_30m_{result['opening_30m_review']['phase']}",
            "message": "CNFutures 开盘后30分钟验收未完全通过。",
        })
    if result["latest_review"]["real_trading_enabled"]:
        alerts.append({"severity": "error", "code": "cn_futures_real_trading_flag_enabled", "message": "CNFutures 复盘样本错误带有实盘启用标记。"})
    opening_phase = str(result["opening_30m_review"].get("phase") or "")
    if latest_filled_count <= 0 and filled_signal_count <= 0 and opening_phase not in {"strategy_hold", "no_night_session"}:
        alerts.append({"severity": "warn", "code": "cn_futures_first_sim_sample_missing", "message": "期货5分钟数据已进入会话窗口，但 TradingAgent 尚无首个模拟成交样本。"})
    if filled_signal_count > 0 and receipt_count <= 0:
        alerts.append({"severity": "warn", "code": "cn_futures_first_receipt_missing", "message": "CNFutures 已有模拟成交信号，但签名回执尚未生成。"})
    result["alerts"] = alerts
    result["status"] = "warn" if alerts else "pass"
    result["reason"] = "first_sample_alerts_present" if alerts else "first_sample_ready"
    return result


def validate_opening(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    now: datetime | None = None,
    min_symbols: int = 4,
) -> dict[str, Any]:
    current = now or _now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    session_name, start = _session_start(current)
    result: dict[str, Any] = {
        "market": "cn_futures",
        "report_type": "opening_validation",
        "checked_at": current.isoformat(timespec="seconds"),
        "sqlite_db": str(sqlite_db),
        "data_source": "SharedSignals read_model",
        "read_only": True,
        "session": session_name,
        "session_start": start.isoformat(timespec="seconds") if start else None,
        "min_symbols": max(1, int(min_symbols)),
        "real_trading_enabled": False,
    }
    if start is None:
        return {**result, "status": "warn", "reason": "outside_cn_futures_session"}
    bars = _query_session_bars(sqlite_db, start, current)
    result.update(bars)
    if bars.get("error"):
        result["status"] = "fail"
        result["reason"] = "opening_validation_query_failed"
    elif int(bars.get("bar_count") or 0) <= 0:
        result["status"] = "warn"
        result["reason"] = "opening_session_has_no_5min_bars"
    elif int(bars.get("symbol_count") or 0) < max(1, int(min_symbols)):
        result["status"] = "warn"
        result["reason"] = "opening_session_symbol_coverage_low"
    else:
        result["status"] = "pass"
        result["reason"] = "opening_session_5min_data_ready"
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only CN futures opening 5-minute data validation.")
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--review-path", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR)
    parser.add_argument("--receipt-path", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--now", default=None)
    parser.add_argument("--min-symbols", type=int, default=4)
    parser.add_argument("--pre-open", action="store_true")
    parser.add_argument("--first-sample", action="store_true")
    parser.add_argument("--wait-minutes", type=int, default=10)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = _parse_now(args.now)
    if args.pre_open:
        report = validate_pre_open(sqlite_db=args.sqlite_db, now=now, min_symbols=args.min_symbols)
    elif args.first_sample:
        report = first_sample_alerts(
            sqlite_db=args.sqlite_db,
            review_path=args.review_path,
            signals_dir=args.signals_dir,
            receipt_path=args.receipt_path,
            now=now,
            min_symbols=args.min_symbols,
            wait_minutes=args.wait_minutes,
        )
    else:
        report = validate_opening(sqlite_db=args.sqlite_db, now=now, min_symbols=args.min_symbols)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 2 if report.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
