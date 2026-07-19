#!/usr/bin/env python3
"""5层候选池 — holdings→watch→candidate→universe→fundamental。

A股 Phase 0-3 的所有个股层均由统一资格策略限制为沪深主板普通股；
双创指数与行业汇总属于独立市场环境快照，不进入本候选池。

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

from shared.data.reader import TradingagentDataReader
from shared.universe.policy import is_mainboard_tradable

_DATA_READER: TradingagentDataReader | None = None

# 池大小限制
_POOL_LIMITS: dict[str, int] = {
    "holdings": 5,  # 持仓上限
    "watch": 20,  # 观察池上限
    "candidate": 50,  # 候选池上限
    "universe": 500,  # 全市场上限 (过滤后)
    "fundamental": 100,  # 基本面池上限
}

# 六维打分阈值 (进入 candidate 层的最低 combined)
_CANDIDATE_THRESHOLD = 0.55
# 进入 watch 层的最低 combined
_WATCH_THRESHOLD = 0.45
# A股 candidate 不允许只靠技术/资金证据穿过综合分阈值。
_MIN_ASHARE_CANDIDATE_EVIDENCE_COVERAGE = 0.5
_ASHARE_RESEARCH_EVIDENCE_DIMENSIONS = ("event", "fundamental", "sentiment")
_EVIDENCE_METADATA_KEYS = {
    "evidence_coverage",
    "missing_evidence_dimensions",
    "evidence_sources",
}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _metric_name(row: dict[str, Any]) -> str:
    raw = (
        str(row.get("factor_name") or row.get("name") or row.get("metric") or "")
        .strip()
        .lower()
    )
    return raw.split(":", 1)[1] if ":" in raw else raw


def _get_data_reader(reader: Any | None = None) -> Any:
    if reader is not None:
        return reader
    global _DATA_READER
    if _DATA_READER is None:
        _DATA_READER = TradingagentDataReader()
    return _DATA_READER


def _filter_market_symbols(symbols: list[str], market: str) -> list[str]:
    if str(market or "").lower() != "ashare":
        return symbols
    return [symbol for symbol in symbols if is_mainboard_tradable(symbol)]


def _dimension_has_evidence(scores: dict[str, Any], dimension: str) -> bool | None:
    evidence_sources = (
        scores.get("evidence_sources")
        if isinstance(scores.get("evidence_sources"), dict)
        else {}
    )
    source_info = (
        evidence_sources.get(dimension) if isinstance(evidence_sources, dict) else None
    )
    if isinstance(source_info, dict) and "has_evidence" in source_info:
        return source_info.get("has_evidence") is True

    missing_dimensions = scores.get("missing_evidence_dimensions")
    if isinstance(missing_dimensions, (list, tuple, set)):
        return dimension not in {str(item) for item in missing_dimensions}
    return None


def _ashare_candidate_evidence_allowed(scores: dict[str, Any], market: str) -> bool:
    """Keep A-share technical/capital-only names in watch, not executable candidate."""
    if str(market or "").lower() != "ashare":
        return True
    if not any(key in scores for key in _EVIDENCE_METADATA_KEYS):
        return True

    if "evidence_coverage" in scores:
        coverage = _safe_float(scores.get("evidence_coverage"), 0.0)
        if coverage < _MIN_ASHARE_CANDIDATE_EVIDENCE_COVERAGE:
            return False

    research_evidence = [
        _dimension_has_evidence(scores, dimension)
        for dimension in _ASHARE_RESEARCH_EVIDENCE_DIMENSIONS
    ]
    if any(value is True for value in research_evidence):
        return True
    if all(value is None for value in research_evidence):
        return True
    return False


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
                factor = _metric_name(row)
                if factor not in {"value", "quality"} or factor in latest_scores:
                    continue
                latest_scores[factor] = _safe_float(row.get("value"), 0.0)
            if (
                latest_scores.get("value", 0.0) > 0.7
                and latest_scores.get("quality", 0.0) > 0.6
            ):
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
    scores_by_symbol: dict[str, dict[str, Any]] | None = None,
    scores_map: dict[str, dict[str, Any]] | None = None,
    include_fundamental_pool: bool | None = None,
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
    holdings = _filter_market_symbols(holdings, market)[: _POOL_LIMITS["holdings"]]

    # 2. Universe 层 (过滤后全市场)
    if universe is None:
        try:
            from .universe_filter import filter_universe

            universe = filter_universe(date)
        except ImportError:
            universe = []
    universe = _filter_market_symbols(universe, market)[: _POOL_LIMITS["universe"]]

    precomputed_scores = (
        scores_by_symbol if scores_by_symbol is not None else scores_map
    )
    precomputed_scores = dict(precomputed_scores or {})
    if include_fundamental_pool is None:
        include_fundamental_pool = not bool(precomputed_scores)

    # 3. Fundamental 层 (长期跟踪)
    fundamental = (
        _filter_market_symbols(
            _load_fundamental_pool(reader=reader, market=market), market
        )[: _POOL_LIMITS["fundamental"]]
        if include_fundamental_pool
        else []
    )

    score_cache: dict[str, dict[str, Any]] = {}

    def score_for(ts_code: str) -> dict[str, Any]:
        cached = precomputed_scores.get(ts_code) or score_cache.get(ts_code)
        if isinstance(cached, dict):
            return cached
        from .six_dimension_scorer import score_stock

        score = score_stock(market, ts_code, reader, date)
        score_cache[ts_code] = dict(score or {})
        return score_cache[ts_code]

    # 4. Candidate 层 (六维打分通过)
    candidate: list[str] = []
    try:
        for ts_code in universe:
            if ts_code in holdings:
                continue
            scores = score_for(ts_code)
            combined = _safe_float(scores.get("combined"), 0.0)
            if combined >= _CANDIDATE_THRESHOLD and _ashare_candidate_evidence_allowed(
                scores, market
            ):
                candidate.append(ts_code)
                if len(candidate) >= _POOL_LIMITS["candidate"]:
                    break
    except ImportError:
        pass

    # 5. Watch 层 (打分稍低, 但有潜在条件)
    watch: list[str] = []
    try:
        for ts_code in universe:
            if ts_code in holdings or ts_code in candidate:
                continue
            scores = score_for(ts_code)
            combined = _safe_float(scores.get("combined"), 0.0)
            if _WATCH_THRESHOLD <= combined < _CANDIDATE_THRESHOLD or (
                combined >= _CANDIDATE_THRESHOLD
                and not _ashare_candidate_evidence_allowed(scores, market)
            ):
                watch.append(ts_code)
                if len(watch) >= _POOL_LIMITS["watch"]:
                    break
    except ImportError:
        pass

    # Fundamental 池中的股票也加入 watch (如果不在其他池中)
    for ts_code in fundamental:
        if (
            ts_code not in holdings
            and ts_code not in candidate
            and ts_code not in watch
        ):
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

    pool = build_pool(
        "20260629",
        holdings=["600519.SH"],
        universe=["600519.SH", "000858.SZ", "601318.SH"],
    )
    print(json.dumps(pool, ensure_ascii=False, indent=2))
    print(f"\nCandidate layer: {get_layer(pool, 'candidate')}")
