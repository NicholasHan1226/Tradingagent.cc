#!/usr/bin/env python3
"""5层候选池 — holdings→watch→candidate→universe→fundamental。

层次越内, 优先级越高。每层有独立的进入/退出条件。
- holdings: 当前持仓 (最高优先, 监控退出)
- watch: 观察池 (有条件待触发, 盯盘中)
- candidate: 候选池 (已通过六维打分, 待生成条件)
- universe: 全市场 (已过滤, 用于打分)
- fundamental: 基本面池 (长期跟踪, 低频更新)

build_pool(date, holdings) → {layer: [ts_code]}
get_layer(pool, layer) → [ts_code]
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

_ASHARE_DATA = Path("/opt/investment/Ashare/data")

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


def _load_holdings() -> list[str]:
    """加载当前持仓列表。"""
    try:
        import json
        positions_file = _ASHARE_DATA / "positions" / "current.json"
        if positions_file.exists():
            with open(positions_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [p.get("ts_code") for p in data
                        if isinstance(p, dict) and p.get("ts_code")]
            if isinstance(data, dict):
                return [k for k in data.keys()]
    except (OSError, ValueError, TypeError):
        pass
    return []


def _load_fundamental_pool() -> list[str]:
    """加载基本面池 (长期跟踪的优质股)。"""
    try:
        import json
        fund_file = _ASHARE_DATA / "forecasts" / "fundamental_pool.json"
        if fund_file.exists():
            with open(fund_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except (OSError, ValueError, TypeError):
        pass
    return []


def build_pool(
    date: str | None = None,
    holdings: list[str] | None = None,
    universe: list[str] | None = None,
) -> dict[str, list[str]]:
    """构建5层候选池。

    Args:
        date: 日期 (YYYYMMDD), 默认今天
        holdings: 当前持仓列表, 默认从 positions 加载
        universe: 已过滤的全市场列表, 默认从 universe_filter 获取

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
    fundamental = _load_fundamental_pool()[:_POOL_LIMITS["fundamental"]]

    # 4. Candidate 层 (六维打分通过)
    candidate: list[str] = []
    try:
        from .six_dimension_scorer import score_stock
        for ts_code in universe:
            if ts_code in holdings:
                continue
            scores = score_stock(ts_code, date)
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
            scores = score_stock(ts_code, date)
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
