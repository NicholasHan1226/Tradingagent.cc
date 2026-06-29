#!/usr/bin/env python3
"""全市场过滤 — 排除 ST/停牌/新股/流动性不足。

降权不硬拒, 但有硬底线 (新股 <30 天直接排除)。

filter_universe(date, stock_list) → list[ts_code]
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ASHARE_DATA = Path("/opt/investment/Ashare/data")

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
}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _is_st(name: str) -> bool:
    """判断是否 ST / *ST / 退市股。"""
    if not name:
        return False
    upper = name.upper()
    return "ST" in upper or "*ST" in upper or "退" in name


def _is_suspended(ts_code: str, date: str) -> bool:
    """判断是否停牌 (placeholder: 检查是否有当日行情数据)。"""
    try:
        import json
        daily_file = _ASHARE_DATA / "tushare_cache" / f"{ts_code}_daily.json"
        if daily_file.exists():
            with open(daily_file, encoding="utf-8") as f:
                bars = json.load(f)
            if isinstance(bars, list):
                # 检查最近一条是否有成交量
                for bar in bars:
                    if isinstance(bar, dict) and bar.get("trade_date") == date:
                        vol = _safe_float(bar.get("vol", 0.0))
                        if vol < 1e-9:
                            return True
                        return False
    except (OSError, ValueError, TypeError):
        pass
    return False


def _list_days(ts_code: str, date: str) -> int:
    """获取上市天数 (placeholder: 从缓存读 list_date)。"""
    try:
        import json
        basic_file = _ASHARE_DATA / "tushare_cache" / "stock_basic.json"
        if basic_file.exists():
            with open(basic_file, encoding="utf-8") as f:
                data = json.load(f)
            stock_info = data.get(ts_code, {})
            list_date = stock_info.get("list_date", "")
            if list_date:
                d1 = datetime.strptime(date, "%Y%m%d")
                d2 = datetime.strptime(str(list_date), "%Y%m%d")
                return (d1 - d2).days
    except (OSError, ValueError, TypeError):
        pass
    return 999  # 未知时不过滤


def _turnover_wan(ts_code: str, date: str) -> float:
    """获取日均成交额 (万元) — 近 5 日平均。"""
    try:
        import json
        daily_file = _ASHARE_DATA / "tushare_cache" / f"{ts_code}_daily.json"
        if daily_file.exists():
            with open(daily_file, encoding="utf-8") as f:
                bars = json.load(f)
            if isinstance(bars, list):
                recent = [b for b in bars if isinstance(b, dict)][:5]
                amounts = [_safe_float(b.get("amount", 0.0)) for b in recent]
                if amounts:
                    # amount 单位: 千元 → 转万元
                    return sum(amounts) / len(amounts) / 10.0
    except (OSError, ValueError, TypeError):
        pass
    return 99999.0  # 未知时不过滤


def filter_universe(
    date: str | None = None,
    stock_list: list[str] | None = None,
    config: dict[str, Any] | None = None,
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

    # 获取候选列表
    if stock_list is None:
        try:
            import json
            basic_file = _ASHARE_DATA / "tushare_cache" / "stock_basic.json"
            if basic_file.exists():
                with open(basic_file, encoding="utf-8") as f:
                    data = json.load(f)
                stock_list = [k for k, v in data.items()
                              if isinstance(v, dict) and v.get("list_status") != "D"]
            else:
                stock_list = []
        except (OSError, ValueError, TypeError):
            stock_list = []

    if not stock_list:
        return []

    excluded: list[tuple[str, str]] = []  # (ts_code, reason)
    result: list[str] = []

    for ts_code in stock_list:
        # 1. ST 排除
        if cfg["exclude_st"]:
            try:
                import json
                basic_file = _ASHARE_DATA / "tushare_cache" / "stock_basic.json"
                if basic_file.exists():
                    with open(basic_file, encoding="utf-8") as f:
                        data = json.load(f)
                    name = data.get(ts_code, {}).get("name", "")
                    if _is_st(name):
                        excluded.append((ts_code, "ST"))
                        continue
            except (OSError, ValueError, TypeError):
                pass

        # 2. 停牌排除
        if cfg["exclude_suspended"] and _is_suspended(ts_code, date):
            excluded.append((ts_code, "suspended"))
            continue

        # 3. 新股排除
        min_days = int(cfg.get("min_list_days", 30))
        if _list_days(ts_code, date) < min_days:
            excluded.append((ts_code, f"new_stock<{min_days}d"))
            continue

        # 4. 流动性排除
        min_turnover = _safe_float(cfg.get("min_turnover_wan", 5000), 5000)
        if _turnover_wan(ts_code, date) < min_turnover:
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
