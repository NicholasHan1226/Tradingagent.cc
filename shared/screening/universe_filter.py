#!/usr/bin/env python3
"""全市场过滤 — 排除 ST/停牌/新股/流动性不足。

降权不硬拒, 但有硬底线 (新股 <30 天直接排除)。

filter_universe(date, stock_list) → list[ts_code]
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from shared.data.reader import TradingagentDataReader

_DATA_READER: TradingagentDataReader | None = None

# 排除条件默认值
_DEFAULTS: dict[str, Any] = {
    # ST / *ST 排除
    "exclude_st": True,
    # 停牌排除
    "exclude_suspended": True,
    # 新股排除 (上市天数)
    "min_list_days": 30,
    # 流动性: 最小日均成交额 (万元)
    "min_turnover_wan": 5000,
    # 最小流通市值 (亿元)
    "min_float_mktcap_yi": 20,
    # 排除退市
    "exclude_delisted": True,
    # A股只保留普通 A 股代码段，排除 B 股/北交所等非本链路标的
    "exclude_non_a_share": True,
}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _get_data_reader(reader: Any | None = None) -> Any:
    if reader is not None:
        return reader
    global _DATA_READER
    if _DATA_READER is None:
        _DATA_READER = TradingagentDataReader()
    return _DATA_READER


def _symbol_variants(ts_code: str) -> list[str]:
    symbol = str(ts_code or "").strip()
    if "." in symbol:
        stripped = symbol.split(".", 1)[0]
        return [stripped, symbol]
    return [symbol]


def _lookback_start(date: str, calendar_days: int = 14) -> str:
    try:
        end = datetime.strptime(str(date or "").replace("-", "")[:8], "%Y%m%d")
    except ValueError:
        return ""
    return (end - timedelta(days=calendar_days)).strftime("%Y%m%d")


def _is_regular_a_share_symbol(ts_code: Any) -> bool:
    raw = str(ts_code or "").strip().upper()
    if "." in raw:
        digits, exchange = raw.split(".", 1)
    else:
        digits, exchange = raw, ""
    if not re.fullmatch(r"\d{6}", digits):
        return False
    if exchange == "SZ":
        return digits.startswith(("000", "001", "002", "003", "300", "301"))
    if exchange == "SH":
        return digits.startswith(("600", "601", "603", "605", "688", "689"))
    return digits.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689"))


def _is_st(name: str) -> bool:
    """判断是否 ST / *ST / 退市股。"""
    if not name:
        return False
    upper = name.upper()
    return "ST" in upper or "*ST" in upper or "退" in name


def _is_suspended(ts_code: str, date: str, reader: Any | None = None, market: str = "ashare") -> bool:
    """判断是否停牌。"""
    data_reader = _get_data_reader(reader)
    try:
        coverage_rows = data_reader.get_coverage(market, date)
        for row in coverage_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol") or "").strip() not in _symbol_variants(ts_code):
                continue
            coverage_status = str(row.get("coverage_status") or "").strip().lower()
            if coverage_status and coverage_status not in {"normal", "ok", "active", "trading", "covered"}:
                return True
    except Exception:
        pass

    try:
        for symbol in _symbol_variants(ts_code):
            bars = data_reader.get_bars_daily(market, symbol, None, date)
            if not bars:
                continue
            for bar in reversed(bars):
                trade_date = str(bar.get("trade_date") or "").replace("-", "")
                if trade_date != date:
                    continue
                vol = _safe_float(bar.get("vol", bar.get("volume", 0.0)))
                return vol < 1e-9
    except Exception:
        pass
    return False


def _list_days(ts_code: str, date: str, assets_by_symbol: dict[str, dict[str, Any]]) -> int:
    """获取上市天数。"""
    try:
        asset = assets_by_symbol.get(ts_code) or assets_by_symbol.get(ts_code.split(".", 1)[0])
        list_date = str((asset or {}).get("list_date") or "").strip()
        if list_date:
            d1 = datetime.strptime(date, "%Y%m%d")
            d2 = datetime.strptime(list_date[:8].replace("-", "").replace("/", ""), "%Y%m%d")
            return (d1 - d2).days
    except Exception:
        pass
    return 999  # 未知时不过滤


def _turnover_wan(ts_code: str, date: str, reader: Any | None = None, market: str = "ashare") -> float:
    """获取日均成交额 (万元) — 近 5 日平均。"""
    data_reader = _get_data_reader(reader)
    try:
        start_date = _lookback_start(date)
        for symbol in _symbol_variants(ts_code):
            bars = data_reader.get_bars_daily(market, symbol, start_date, date)
            if not bars:
                continue
            recent = [b for b in reversed(bars) if isinstance(b, dict)][:5]
            amounts = [_safe_float(b.get("amount", 0.0)) for b in recent]
            amounts = [amount for amount in amounts if amount > 0.0]
            if amounts:
                return sum(amounts) / len(amounts) / 10.0
    except Exception:
        pass
    return 0.0  # 无近期日线时不能进入可执行 A股候选


def filter_universe(
    date: str | None = None,
    stock_list: list[str] | None = None,
    config: dict[str, Any] | None = None,
    reader: Any | None = None,
    market: str = "ashare",
) -> list[str]:
    """过滤全市场股票 — 排除 ST/停牌/新股/流动性不足。

    Args:
        date: 日期 (YYYYMMDD), 默认今天
        stock_list: 候选股票列表, 默认 None 时尝试从缓存读全市场
        config: 过滤参数, 默认用 _DEFAULTS

    Returns:
        过滤后的 ts_code 列表
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    cfg = dict(_DEFAULTS)
    if config:
        cfg.update(config)
    data_reader = _get_data_reader(reader)

    assets = data_reader.get_assets(market)
    if not assets and market.lower() == "ashare":
        assets = data_reader.get_assets("Ashare")
    assets_by_symbol: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        symbol = str(asset.get("symbol") or "").strip()
        if symbol:
            assets_by_symbol[symbol] = asset

    # 获取候选列表
    if stock_list is None:
        try:
            stock_list = [
                symbol
                for symbol, asset in assets_by_symbol.items()
                if str(asset.get("status") or "").strip().lower() not in {"delisted", "退市", "d", "inactive"}
            ]
        except Exception:
            stock_list = []

    if not stock_list:
        return []

    excluded: list[tuple[str, str]] = []  # (ts_code, reason)
    result: list[str] = []

    for ts_code in stock_list:
        # 0. A股代码段硬过滤
        if market.lower() == "ashare" and cfg.get("exclude_non_a_share", True) and not _is_regular_a_share_symbol(ts_code):
            excluded.append((ts_code, "non_a_share_symbol"))
            continue

        # 1. ST 排除
        if cfg["exclude_st"]:
            asset = assets_by_symbol.get(ts_code) or assets_by_symbol.get(ts_code.split(".", 1)[0], {})
            name = str(asset.get("name") or "")
            if _is_st(name):
                excluded.append((ts_code, "ST"))
                continue

        # 2. 停牌排除
        if cfg["exclude_suspended"] and _is_suspended(ts_code, date, data_reader, market):
            excluded.append((ts_code, "suspended"))
            continue

        # 3. 新股排除
        min_days = int(cfg.get("min_list_days", 30))
        if _list_days(ts_code, date, assets_by_symbol) < min_days:
            excluded.append((ts_code, f"new_stock<{min_days}d"))
            continue

        # 4. 流动性排除
        min_turnover = _safe_float(cfg.get("min_turnover_wan", 5000), 5000)
        if _turnover_wan(ts_code, date, data_reader, market) < min_turnover:
            excluded.append((ts_code, "illiquid"))
            continue

        result.append(ts_code)

    return result


if __name__ == "__main__":
    # 测试
    test_stocks = ["600519.SH", "000858.SZ", "601318.SH"]
    filtered = filter_universe("20260629", test_stocks)
    print(f"Input: {len(test_stocks)} stocks → Output: {len(filtered)} stocks")
    print(f"  {filtered}")
