#!/usr/bin/env python3
"""六维打分 — macro/event/fundamental/capital/technical/sentiment → combined。

权重式打分, 不设硬门禁。六维互补:
- macro: 宏观定方向 (regime)
- event: 事件找催化 (raw_events)
- fundamental: 基本面定底 (scores)
- capital: 资金确认 (moneyflow)
- technical: 技术择时 (momentum)
- sentiment: 情绪防雷 (signals)

score_stock(ts_code, date) → {macro, event, fundamental, capital, technical, sentiment, combined}
score_universe(date) → list of (ts_code, scores)
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, '/opt/investment/SharedSignals')

from reader import (
    get_capital_flow,
    get_events,
    get_macro_factors,
    get_market_data,
    get_sentiment,
)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# ── 权重默认值 (与 weights.yaml 对齐) ──
_DEFAULT_WEIGHTS: dict[str, float] = {
    "macro": 0.15,
    "event": 0.20,
    "fundamental": 0.25,
    "capital": 0.15,
    "technical": 0.15,
    "sentiment": 0.10,
}

_DEFAULT_MISSING = 0.5

_WEIGHTS_PATH = Path(__file__).resolve().parent / "weights.yaml"

def _unwrap_reader_data(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        rows: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict) or item.get("degraded"):
                continue
            data = item.get("data")
            if isinstance(data, dict) and data:
                rows.append(data)
            elif data is None:
                rows.append(item)
        return rows
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict) and row]
    return []


def _load_weights() -> dict[str, Any]:
    """加载权重配置。yaml 不可用时回退默认值。"""
    if yaml is None:
        return {"combined": {"missing_default": _DEFAULT_MISSING}, **_DEFAULT_WEIGHTS}
    try:
        with open(_WEIGHTS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            result: dict[str, Any] = {}
            for k in ("macro", "event", "fundamental", "capital", "technical", "sentiment"):
                result[k] = float(data.get(k, _DEFAULT_WEIGHTS[k]))
            result["dimensions"] = data.get("dimensions", {})
            combined = data.get("combined", {})
            result["combined"] = {
                "normalize": combined.get("normalize", True),
                "missing_default": float(combined.get("missing_default", _DEFAULT_MISSING)),
            }
            return result
    except (OSError, yaml.YAMLError, ValueError):
        pass
    result = dict(_DEFAULT_WEIGHTS)
    result["dimensions"] = {}
    result["combined"] = {"normalize": True, "missing_default": _DEFAULT_MISSING}
    return result


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default  # NaN check
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ── 六维打分函数 (各维度从 SharedSignals reader 读取) ──

def _strip_suffix(ts_code: str) -> str:
    """Return code without exchange suffix, e.g. 600519.SH -> 600519."""
    return ts_code.split(".", 1)[0] if "." in ts_code else ts_code


def _direction_score(impact_hint: Any) -> float:
    direction = str(impact_hint or "").split(":", 1)[0].strip().lower()
    return {"positive": 1.0, "negative": 0.0, "mixed": 0.5, "neutral": 0.5}.get(direction, 0.5)


def _score_macro(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """宏观维度 — 从 SharedSignals 宏观因子读取当前 regime。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("macro", {})
        regime_scores = dim_cfg.get("regime_scores", {})
        rows = _unwrap_reader_data(get_macro_factors(date=date))
        if not rows:
            return 0.5
        row = max(rows, key=lambda r: str(r.get("generated_at") or ""))
        regime = str(row.get("regime") or "")
        score_value = regime_scores.get(regime)
        if score_value is None:
            prefix = regime.split("_", 1)[0]
            score_value = regime_scores.get(prefix, 0.5)
        regime_score = _safe_float(score_value, 0.5)
        confidence = _clamp(_safe_float(row.get("regime_confidence"), 0.0))
        return _clamp(0.5 + (regime_score - 0.5) * confidence)
    except Exception:
        return 0.5


def _score_event(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """事件维度 — 从 SharedSignals 事件流聚合个股事件方向。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("event", {})
        min_conf = _safe_float(dim_cfg.get("min_confidence", 0.30), 0.30)
        rows = _unwrap_reader_data(get_events(date=date, subject_code=ts_code, subject_type="stock"))
        if not rows:
            return 0.5
        total_weight = 0.0
        weighted = 0.0
        allowed_status = {"needs_review", "promoted", "approved"}
        for row in rows:
            if row.get("status") not in allowed_status:
                continue
            conf = _safe_float(row.get("confidence"), 0.0)
            if conf < min_conf:
                continue
            weighted += _direction_score(row.get("proposed_impact_hint")) * conf
            total_weight += conf
        if total_weight <= 1e-9:
            return 0.5
        return _clamp(weighted / total_weight)
    except Exception:
        return 0.5


def _score_fundamental(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """基本面维度 — 从 SharedSignals market_factors 读取最新因子值。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("fundamental", {})
        factor_weights = dim_cfg.get("factors", {"value": 0.30, "growth": 0.30, "quality": 0.20, "momentum": 0.20})
        symbols = [_strip_suffix(ts_code)]
        if ts_code not in symbols:
            symbols.append(ts_code)
        rows = _unwrap_reader_data(get_market_data("market_factors", market="Ashare", symbols=symbols, limit=200))
        if not rows:
            return 0.5
        latest_by_factor: dict[str, float] = {}
        for row in rows:
            factor = str(row.get("factor_name") or "").strip().lower()
            if factor and factor not in latest_by_factor:
                raw = _safe_float(row.get("value"), 0.5)
                if factor in ("pe", "pb"):
                    raw = 1.0 - (raw / 100.0)
                latest_by_factor[factor] = _clamp(raw)
        total_w = 0.0
        weighted = 0.0
        for factor, weight in factor_weights.items():
            name = str(factor).strip().lower()
            if name not in latest_by_factor:
                continue
            w = _safe_float(weight, 0.0)
            weighted += latest_by_factor[name] * w
            total_w += w
        if total_w <= 1e-9:
            return 0.5
        return _clamp(weighted / total_w)
    except Exception:
        return 0.5


def _score_capital(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """资金维度 — 从 SharedSignals 资金流读取窗口内主力净流入。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("capital", {})
        window = max(1, int(dim_cfg.get("window_days", 5)))
        rows = _unwrap_reader_data(get_capital_flow(date=date, window=window, ts_code=ts_code))
        if not rows:
            return 0.5
        total_net = sum(_safe_float(row.get("net_mf_amount"), 0.0) for row in rows)
        if total_net > 0:
            score = 0.6 + _clamp(total_net / 1e5, 0.0, 0.4)
        else:
            score = 0.4 + _clamp(total_net / 1e5, -0.4, 0.2)
        return _clamp(score)
    except Exception:
        return 0.5


def _score_technical(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """技术维度 — 从 SharedSignals 日线读取动量和均线趋势。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("technical", {})
        window = max(1, int(dim_cfg.get("window_days", 20)))
        ma_short = max(1, int(dim_cfg.get("ma_short", 5)))
        ma_long = max(1, int(dim_cfg.get("ma_long", 20)))
        needed = max(window, ma_short, ma_long)
        symbols = [_strip_suffix(ts_code)]
        if ts_code not in symbols:
            symbols.append(ts_code)
        rows = _unwrap_reader_data(get_market_data("market_bars_daily", market="Ashare", symbols=symbols, limit=60))
        if len(rows) < ma_long:
            return 0.5
        closes_desc = [_safe_float(row.get("close"), 0.0) for row in rows]
        closes = list(reversed([c for c in closes_desc if c > 0]))
        if len(closes) < needed:
            return 0.5
        base = closes[-window]
        if base <= 1e-9:
            return 0.5
        momentum = (closes[-1] - base) / base
        ma_s = sum(closes[-ma_short:]) / ma_short
        ma_l = sum(closes[-ma_long:]) / ma_long
        trend_bonus = 0.1 if ma_s > ma_l else -0.1
        return _clamp(0.5 + _clamp(momentum / 0.20, -0.5, 0.5) + trend_bonus)
    except Exception:
        return 0.5


def _score_sentiment(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """情绪维度 — 从 SharedSignals 情绪流聚合个股信号。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("sentiment", {})
        extreme_threshold = _safe_float(dim_cfg.get("extreme_threshold", 0.85), 0.85)
        rows = _unwrap_reader_data(get_sentiment(date=date, subject_code=ts_code))
        if not rows:
            return 0.5
        total_weight = 0.0
        weighted = 0.0
        allowed_status = {"sentiment_signal", "needs_review", "promoted"}
        for row in rows:
            if row.get("status") not in allowed_status:
                continue
            conf = _safe_float(row.get("confidence"), 0.0)
            if conf < 0.20:
                continue
            weighted += _direction_score(row.get("proposed_impact_hint")) * conf
            total_weight += conf
        if total_weight <= 1e-9:
            return 0.5
        raw = _clamp(weighted / total_weight)
        if raw > extreme_threshold:
            return _clamp(0.5 - (raw - extreme_threshold) * 2.0)
        return raw
    except Exception:
        return 0.5


_DIMENSION_FUNCS = {
    "macro": _score_macro,
    "event": _score_event,
    "fundamental": _score_fundamental,
    "capital": _score_capital,
    "technical": _score_technical,
    "sentiment": _score_sentiment,
}


def score_stock(ts_code: str, date: str | None = None) -> dict[str, float]:
    """对单只股票进行六维打分。

    Args:
        ts_code: 股票代码 (如 "600519.SH")
        date: 日期 (YYYYMMDD), 默认今天

    Returns:
        {
            "macro": float,         # [0, 1]
            "event": float,
            "fundamental": float,
            "capital": float,
            "technical": float,
            "sentiment": float,
            "combined": float,      # 加权综合分 [0, 1]
        }
    """
    if not ts_code:
        return {k: 0.0 for k in (*_DEFAULT_WEIGHTS, "combined")}
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    config = _load_weights()
    missing_default = config.get("combined", {}).get("missing_default", _DEFAULT_MISSING)

    scores: dict[str, float] = {}
    for dim, func in _DIMENSION_FUNCS.items():
        try:
            scores[dim] = _clamp(func(ts_code, date, config))
        except Exception:
            scores[dim] = missing_default

    # 加权综合
    combined = 0.0
    total_weight = 0.0
    for dim, weight in _DEFAULT_WEIGHTS.items():
        w = _safe_float(config.get(dim, weight), weight)
        combined += scores.get(dim, missing_default) * w
        total_weight += w

    if total_weight > 1e-9:
        combined /= total_weight
    else:
        combined = missing_default

    scores["combined"] = _clamp(combined)
    return scores


def score_universe(
    date: str | None = None,
    universe: list[str] | None = None,
) -> list[tuple[str, dict[str, float]]]:
    """对整个 universe 进行六维打分。

    Args:
        date: 日期 (YYYYMMDD), 默认今天
        universe: 股票代码列表, 默认 None 时尝试从 universe_filter 获取

    Returns:
        [(ts_code, scores), ...] 按 combined 降序
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    if universe is None:
        try:
            from .universe_filter import filter_universe
            universe = filter_universe(date)
        except ImportError:
            universe = []

    if not universe:
        return []

    results: list[tuple[str, dict[str, float]]] = []
    for ts_code in universe:
        scores = score_stock(ts_code, date)
        results.append((ts_code, scores))

    # 按 combined 降序
    results.sort(key=lambda x: x[1].get("combined", 0.0), reverse=True)
    return results


if __name__ == "__main__":
    import json

    # 单股测试
    print("=== score_stock ===")
    r = score_stock("600519.SH", "20260629")
    print(json.dumps(r, ensure_ascii=False, indent=2))

    # Universe 测试 (小规模)
    print("\n=== score_universe ===")
    test_universe = ["600519.SH", "000858.SZ", "601318.SH"]
    results = score_universe("20260629", test_universe)
    for code, scores in results:
        print(f"  {code}: combined={scores['combined']:.4f}  "
              f"m={scores['macro']:.2f} e={scores['event']:.2f} f={scores['fundamental']:.2f} "
              f"c={scores['capital']:.2f} t={scores['technical']:.2f} s={scores['sentiment']:.2f}")
