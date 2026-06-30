#!/usr/bin/env python3
"""5层候选池 — holdings→watch→candidate→universe→fundamental。

层次越内, 优先级越高。每层有独立的进入/退出条件。
- holdings: 当前持仓 (最高优先, 监控退出)
- watch: 观察池 (有条件待触发, 盯盘中)
- candidate: 候选池 (已通过六维打分, 待生成条件)
- universe: 全市场 (已过滤, 用于打分)
- fundamental: 基本面池 (长期跟踪, 低频更新)

build_pool(date, holdings, market="ashare") → {layer: [ts_code]}
get_layer(pool, layer) → [ts_code]
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from shared.data.reader import TradingsDataReader

_DATA_READER: TradingsDataReader | None = None

# 池大小限制
_POOL_LIMITS: dict[str, int] = {
    "holdings": 5,        # 持仓上限
    "watch": 20,          # 观察池上限
    "candidate": 50,      # 候选池上限
    "universe": 500,      # 全市场上限 (过滤后)
    "fundamental": 100,   # 基本面池上限
}

# 六维打分阈值 (进入 candidate 层的最低 combined)
_CANDIDATE_THRESHOLD = 0.55
# 进入 watch 层的最低 combined
_WATCH_THRESHOLD = 0.45


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
        _DATA_READER = TradingsDataReader()
    return _DATA_READER


def _load_holdings() -> list[str]:
    """加载当前持仓列表。"""
    try:
        from shared.accounting.position_ledger import get_positions

        positions = get_positions(capital_layer="all")
        seen: set[str] = set()
        holdings: list[str] = []
        for position in positions:
            if not isinstance(position, dict):
                continue
            ts_code = str(position.get("ts_code") or "").strip()
            if not ts_code or ts_code in seen:
                continue
            seen.add(ts_code)
            holdings.append(ts_code)
        if holdings:
            return holdings
    except Exception:
        pass
    return []


def _load_fundamental_pool(
    reader: Any | None = None,
    market: str = "ashare",
) -> list[str]:
    """加载基本面池 (长期跟踪的优质股)。"""
    try:
        data_reader = _get_data_reader(reader)
        assets = data_reader.get_assets(market)
        if not assets and market.lower() == "ashare":
            assets = data_reader.get_assets("Ashare")

        selected: list[str] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("symbol") or "").strip()
            if not symbol:
                continue
            latest_scores: dict[str, float] = {}
            for row in data_reader.get_factors(market, symbol):
                if not isinstance(row, dict):
                    continue
                factor = str(row.get("factor_name") or "").strip().lower()
                if factor not in {"value", "quality"} or factor in latest_scores:
                    continue
                latest_scores[factor] = _safe_float(row.get("value"), 0.0)
            if latest_scores.get("value", 0.0) > 0.7 and latest_scores.get("quality", 0.0) > 0.6:
                selected.append(symbol)
                if len(selected) >= _POOL_LIMITS["fundamental"]:
                    break
        return selected
    except Exception:
        pass
    return []


def build_pool(
    date: str | None = None,
    holdings: list[str] | None = None,
    universe: list[str] | None = None,
    market: str | None = None,
    reader: Any | None = None,
    market_adapter: Any | None = None,
) -> dict[str, list[str]]:
    """构建5层候选池。

    Args:
        date: 日期 (YYYYMMDD), 默认今天
        holdings: 当前持仓列表, 默认从 positions 加载
        universe: 已过滤的全市场列表, 默认从 universe_filter 获取
        market: 市场名称, 默认 "ashare"
        reader: 可选数据读取器, 透传给六维打分
        market_adapter: 可选 market adapter, 用于推断 market

    Returns:
        {
            "holdings": [...],       # 当前持仓
            "watch": [...],          # 观察池 (有条件待触发)
            "candidate": [...],      # 候选池 (打分通过)
            "universe": [...],       # 全市场 (过滤后)
            "fundamental": [...],    # 基本面池
        }
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    if market_adapter is not None and market is None:
        get_market = getattr(market_adapter, "get_market", None)
        if callable(get_market):
            try:
                market = str(get_market()).strip() or None
            except Exception:
                market = None
    market = str(market or "ashare")

    # 1. Holdings 层
    if holdings is None:
        holdings = _load_holdings()
    holdings = holdings[:_POOL_LIMITS["holdings"]]

    # 2. Universe 层 (过滤后全市场)
    if universe is None:
        try:
            from .universe_filter import filter_universe
            universe = filter_universe(date)
        except ImportError:
            universe = []
    universe = universe[:_POOL_LIMITS["universe"]]

    # 3. Fundamental 层 (长期跟踪)
    fundamental = _load_fundamental_pool(reader=reader, market=market)[:_POOL_LIMITS["fundamental"]]

    # 4. Candidate 层 (六维打分通过)
    candidate: list[str] = []
    try:
        from .six_dimension_scorer import score_stock
        for ts_code in universe:
            if ts_code in holdings:
                continue
            scores = score_stock(market, ts_code, reader, date)
            if scores.get("combined", 0.0) >= _CANDIDATE_THRESHOLD:
                candidate.append(ts_code)
                if len(candidate) >= _POOL_LIMITS["candidate"]:
                    break
    except ImportError:
        pass

    # 5. Watch 层 (打分稍低, 但有潜在条件)
    watch: list[str] = []
    try:
        from .six_dimension_scorer import score_stock
        for ts_code in universe:
            if ts_code in holdings or ts_code in candidate:
                continue
            scores = score_stock(market, ts_code, reader, date)
            combined = scores.get("combined", 0.0)
            if _WATCH_THRESHOLD <= combined < _CANDIDATE_THRESHOLD:
                watch.append(ts_code)
                if len(watch) >= _POOL_LIMITS["watch"]:
                    break
    except ImportError:
        pass

    # Fundamental 池中的股票也加入 watch (如果不在其他池中)
    for ts_code in fundamental:
        if ts_code not in holdings and ts_code not in candidate and ts_code not in watch:
            watch.append(ts_code)
            if len(watch) >= _POOL_LIMITS["watch"]:
                break

    return {
        "holdings": holdings,
        "watch": watch,
        "candidate": candidate,
        "universe": universe,
        "fundamental": fundamental,
    }


def get_layer(pool: dict[str, list[str]], layer: str) -> list[str]:
    """获取指定层的股票列表。

    Args:
        pool: build_pool 返回的字典
        layer: "holdings" / "watch" / "candidate" / "universe" / "fundamental"

    Returns:
        该层的 ts_code 列表
    """
    if not isinstance(pool, dict):
        return []
    return pool.get(layer, [])


def promote(
    pool: dict[str, list[str]],
    ts_code: str,
    from_layer: str,
    to_layer: str,
) -> dict[str, list[str]]:
    """将股票从一层提升到另一层。

    Args:
        pool: 当前候选池
        ts_code: 股票代码
        from_layer: 源层
        to_layer: 目标层

    Returns:
        更新后的 pool
    """
    pool = dict(pool)
    src = list(pool.get(from_layer, []))
    dst = list(pool.get(to_layer, []))

    if ts_code in src:
        src.remove(ts_code)
    if ts_code not in dst:
        dst.append(ts_code)

    pool[from_layer] = src
    pool[to_layer] = dst
    return pool


if __name__ == "__main__":
    import json

    pool = build_pool("20260629", holdings=["600519.SH"], universe=["600519.SH", "000858.SZ", "601318.SH"])
    print(json.dumps(pool, ensure_ascii=False, indent=2))
    print(f"\nCandidate layer: {get_layer(pool, 'candidate')}")
