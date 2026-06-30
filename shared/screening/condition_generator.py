#!/usr/bin/env python3
"""条件生成 — breakout/pullback/event/value/rotation 五类条件。

条件驱动, 主动发现。不为每只股票实时扫描, 而是生成触发条件,
由 condition_monitor 盘中监控。

generate_conditions(pool, scores, date) → list[condition]
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from shared.data.reader import TradingsDataReader

_DATA_READER: TradingsDataReader | None = None

# 条件类型
CONDITION_TYPES = ["breakout", "pullback", "event", "value", "rotation"]


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


def _symbol_variants(ts_code: str) -> list[str]:
    symbol = str(ts_code or "").strip()
    if "." in symbol:
        stripped = symbol.split(".", 1)[0]
        return [stripped, symbol]
    return [symbol]


def _get_daily_data(
    market: str,
    ts_code: str,
    date: str,
    reader: Any | None = None,
) -> list[dict[str, Any]]:
    """获取日线数据, 返回最近数据在前。"""
    data_reader = _get_data_reader(reader)
    start_date = _add_days(date, -120)
    for symbol in _symbol_variants(ts_code):
        bars = data_reader.get_bars_daily(market, symbol, start_date, date)
        if bars:
            return list(reversed(bars))
    return []


def _gen_breakout(
    ts_code: str,
    scores: dict[str, float],
    date: str,
    reader: Any | None = None,
    market: str = "ashare",
) -> dict[str, Any] | None:
    """突破条件 — 价格突破近 N 日高点。

    触发: 当日最高价 > 近 20 日最高价
    """
    bars = _get_daily_data(market, ts_code, date, reader)
    if len(bars) < 20:
        return None

    recent = bars[:20]
    highs = [_safe_float(b.get("high", 0.0)) for b in recent if isinstance(b, dict)]
    if not highs:
        return None

    n_high = max(highs[1:])  # 排除最近一天
    last_close = _safe_float(bars[0].get("close", 0.0))

    return {
        "type": "breakout",
        "ts_code": ts_code,
        "date": date,
        "trigger_price": round(n_high, 2),
        "direction": "long",
        "description": f"突破20日高点 {n_high:.2f}",
        "scores": scores,
        "params": {
            "window": 20,
            "threshold_pct": 0.0,  # 突破0%即触发
        },
        "valid_until": _add_days(date, 3),  # 3日内有效
    }


def _gen_pullback(
    ts_code: str,
    scores: dict[str, float],
    date: str,
    reader: Any | None = None,
    market: str = "ashare",
) -> dict[str, Any] | None:
    """回踩条件 — 上升趋势中回踩均线。

    触发: MA5 回踩 MA20 附近 (±2%)
    """
    bars = _get_daily_data(market, ts_code, date, reader)
    if len(bars) < 20:
        return None

    closes = [_safe_float(b.get("close", 0.0)) for b in bars if isinstance(b, dict)]
    if len(closes) < 20:
        return None

    ma5 = sum(closes[:5]) / 5.0
    ma20 = sum(closes[:20]) / 20.0

    if ma20 < 1e-9:
        return None

    # 趋势向上: MA20 > 30日前
    if len(closes) >= 30:
        ma20_prev = sum(closes[10:30]) / 20.0
        if ma20 <= ma20_prev:
            return None  # 非上升趋势

    # 回踩区间: MA5 在 MA20 ±2%
    diff_pct = (ma5 - ma20) / ma20
    if abs(diff_pct) > 0.05:
        return None

    return {
        "type": "pullback",
        "ts_code": ts_code,
        "date": date,
        "trigger_price": round(ma20, 2),
        "direction": "long",
        "description": f"回踩MA20 {ma20:.2f} (MA5={ma5:.2f})",
        "scores": scores,
        "params": {
            "ma_short": 5,
            "ma_long": 20,
            "band_pct": 0.02,
        },
        "valid_until": _add_days(date, 5),
    }


def _gen_event(
    ts_code: str,
    scores: dict[str, float],
    date: str,
    reader: Any | None = None,
    market: str = "ashare",
) -> dict[str, Any] | None:
    """事件条件 — 事件驱动 (政策/行业/公司事件)。

    触发: 有 positive 方向事件 + confidence > 0.5
    """
    data_reader = _get_data_reader(reader)
    try:
        for symbol in _symbol_variants(ts_code):
            for ev in data_reader.get_events(market, symbol, None, date):
                if not isinstance(ev, dict):
                    continue
                direction = str(ev.get("direction") or ev.get("proposed_impact_hint") or "neutral").lower()
                confidence = _safe_float(ev.get("confidence", ev.get("score", 0.0)))
                if "positive" in direction and confidence > 0.5:
                    return {
                        "type": "event",
                        "ts_code": ts_code,
                        "date": date,
                        "trigger_price": None,
                        "direction": "long",
                        "description": f"事件: {ev.get('title') or ev.get('event_type') or '未知事件'} (conf={confidence:.2f})",
                        "scores": scores,
                        "params": {
                            "event_id": ev.get("event_hash", ""),
                            "confidence": confidence,
                        },
                        "valid_until": _add_days(date, 2),
                    }
        for ev in data_reader.get_event_candidates():
            if not isinstance(ev, dict):
                continue
            if str(ev.get("subject_code") or "").strip() != ts_code:
                continue
            direction = str(ev.get("proposed_impact_hint") or ev.get("direction") or "neutral").lower()
            confidence = _safe_float(ev.get("confidence", ev.get("score", 0.0)))
            if "positive" in direction and confidence > 0.5:
                return {
                    "type": "event",
                    "ts_code": ts_code,
                    "date": date,
                    "trigger_price": None,
                    "direction": "long",
                    "description": f"事件: {ev.get('title') or ev.get('subject_code') or '未知事件'} (conf={confidence:.2f})",
                    "scores": scores,
                    "params": {
                        "event_id": ev.get("event_hash", ""),
                        "confidence": confidence,
                    },
                    "valid_until": _add_days(date, 2),
                }
    except Exception:
        pass
    return None


def _gen_value(
    ts_code: str,
    scores: dict[str, float],
    date: str,
    reader: Any | None = None,
    market: str = "ashare",
) -> dict[str, Any] | None:
    """价值条件 — 低估值 + 高质量。

    触发: value 因子分 > 0.7 且 quality 因子分 > 0.6
    """
    try:
        data_reader = _get_data_reader(reader)
        for symbol in _symbol_variants(ts_code):
            latest_scores: dict[str, float] = {}
            for row in data_reader.get_factors(market, symbol):
                if not isinstance(row, dict):
                    continue
                factor = str(row.get("factor_name") or "").strip().lower()
                if factor not in {"value", "quality"} or factor in latest_scores:
                    continue
                latest_scores[factor] = _safe_float(row.get("value"), 0.0)

            value_score = latest_scores.get("value", 0.0)
            quality_score = latest_scores.get("quality", 0.0)
            if value_score > 0.7 and quality_score > 0.6:
                return {
                    "type": "value",
                    "ts_code": ts_code,
                    "date": date,
                    "trigger_price": None,
                    "direction": "long",
                    "description": f"价值机会: value={value_score:.2f} quality={quality_score:.2f}",
                    "scores": scores,
                    "params": {
                        "value_score": value_score,
                        "quality_score": quality_score,
                    },
                    "valid_until": _add_days(date, 10),
                }
    except Exception:
        pass
    return None


def _gen_rotation(
    ts_code: str,
    scores: dict[str, float],
    date: str,
    reader: Any | None = None,
    market: str = "ashare",
) -> dict[str, Any] | None:
    """轮动条件 — 板块轮动信号。

    触发: 该股所在板块资金净流入排名前 3
    """
    try:
        data_reader = _get_data_reader(reader)
        asset: dict[str, Any] | None = None
        factor_rows: list[dict[str, Any]] = []
        for symbol in _symbol_variants(ts_code):
            asset = data_reader.get_asset(market, symbol)
            factor_rows = data_reader.get_factors(market, symbol)
            if asset or factor_rows:
                break

        sector = str((asset or {}).get("sector") or (asset or {}).get("industry") or "").strip()
        latest_by_factor: dict[str, float] = {}
        for row in factor_rows:
            if not isinstance(row, dict):
                continue
            factor = str(row.get("factor_name") or "").strip().lower()
            if factor and factor not in latest_by_factor:
                latest_by_factor[factor] = _safe_float(row.get("value"), 0.0)

        rank = int(_safe_float(latest_by_factor.get("rotation_rank", latest_by_factor.get("sector_rank", 999.0)), 999.0))
        rotation_score = _safe_float(
            latest_by_factor.get("sector_rotation", latest_by_factor.get("rotation", latest_by_factor.get("rotation_score", 0.0))),
            0.0,
        )
        net_inflow = _safe_float(
            latest_by_factor.get("main_net_inflow", latest_by_factor.get("net_mf_amount", latest_by_factor.get("moneyflow", 0.0))),
            0.0,
        )

        rotation_hit = (rank <= 3 and net_inflow > 0) or (rotation_score >= 0.7 and net_inflow > 0)
        if rotation_hit:
            sector_label = sector or "unknown"
            rank_text = f" 排名#{rank}" if rank < 999 else ""
            return {
                "type": "rotation",
                "ts_code": ts_code,
                "date": date,
                "trigger_price": None,
                "direction": "long",
                "description": f"板块轮动: {sector_label}{rank_text} 净流入{net_inflow/1e8:.2f}亿",
                "scores": scores,
                "params": {
                    "sector": sector_label,
                    "rank": rank,
                    "net_inflow": net_inflow,
                    "rotation_score": rotation_score,
                },
                "valid_until": _add_days(date, 5),
            }
    except Exception:
        pass
    return None


def _add_days(date_str: str, days: int) -> str:
    """日期加 N 天。"""
    try:
        d = datetime.strptime(date_str, "%Y%m%d")
        return (d + timedelta(days=days)).strftime("%Y%m%d")
    except ValueError:
        return date_str


_GEN_FUNCS = {
    "breakout": _gen_breakout,
    "pullback": _gen_pullback,
    "event": _gen_event,
    "value": _gen_value,
    "rotation": _gen_rotation,
}


def generate_conditions(
    pool: dict[str, list[str]] | None = None,
    scores_map: dict[str, dict[str, float]] | None = None,
    date: str | None = None,
    types: list[str] | None = None,
    reader: Any | None = None,
    market: str = "ashare",
) -> list[dict[str, Any]]:
    """为候选池中的股票生成条件。

    Args:
        pool: build_pool 返回的字典
        scores_map: {ts_code: scores} 预计算的打分 (避免重复打分)
        date: 日期 (YYYYMMDD), 默认今天
        types: 生成哪些类型的条件, 默认全部

    Returns:
        条件列表 [{type, ts_code, trigger_price, ...}]
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    if types is None:
        types = CONDITION_TYPES

    if pool is None:
        try:
            from .candidate_pool import build_pool
            pool = build_pool(date, market=market, reader=reader)
        except ImportError:
            pool = {}

    if scores_map is None:
        scores_map = {}

    # 从 candidate + watch 层生成条件
    target_codes: list[str] = []
    for layer in ("candidate", "watch"):
        target_codes.extend(pool.get(layer, []))
    # 去重
    seen: set[str] = set()
    target_codes = [c for c in target_codes if not (c in seen or seen.add(c))]

    conditions: list[dict[str, Any]] = []
    for ts_code in target_codes:
        scores = scores_map.get(ts_code, {})
        if not scores:
            try:
                from .six_dimension_scorer import score_stock
                scores = score_stock(market, ts_code, reader, date)
            except ImportError:
                scores = {"combined": 0.5}

        for cond_type in types:
            gen_func = _GEN_FUNCS.get(cond_type)
            if gen_func is None:
                continue
            try:
                cond = gen_func(ts_code, scores, date, reader, market)
                if cond is not None:
                    conditions.append(cond)
            except Exception:
                continue

    return conditions


if __name__ == "__main__":
    import json

    test_pool = {
        "holdings": [],
        "watch": ["000858.SZ"],
        "candidate": ["600519.SH"],
        "universe": [],
        "fundamental": [],
    }
    conds = generate_conditions(test_pool, date="20260629")
    print(json.dumps(conds, ensure_ascii=False, indent=2))
