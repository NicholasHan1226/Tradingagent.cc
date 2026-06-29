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

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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

# Ashare 数据根目录 (placeholder, 用于定位 regime/moneyflow/scores/signals)
_ASHARE_DATA = Path("/opt/investment/Ashare/data")


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


# ── 六维打分函数 (placeholder 实现, 各维度从对应数据源读取) ──

def _score_macro(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """宏观维度 — 从 regime detection 获取当前经济季节。

    regime: growth / inflation / recession / recovery
    不同 regime 对应不同的宏观倾向分。
    Placeholder: 读 regime 文件, 按 config 中 regime_scores 映射。
    """
    dim_cfg = config.get("dimensions", {}).get("macro", {})
    regime_scores = dim_cfg.get("regime_scores", {})
    regime_file = _ASHARE_DATA / "regime" / "current.json"
    regime = "growth"  # 默认
    try:
        import json
        if regime_file.exists():
            with open(regime_file, encoding="utf-8") as f:
                data = json.load(f)
            regime = data.get("regime", regime)
    except (OSError, ValueError, KeyError):
        pass
    return _clamp(_safe_float(regime_scores.get(regime, 0.5), 0.5))


def _score_event(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """事件维度 — 从 raw_events 获取个股相关事件影响。

    Placeholder: 查找该股票的近期事件, 按 direction × confidence 聚合。
    """
    dim_cfg = config.get("dimensions", {}).get("event", {})
    min_conf = _safe_float(dim_cfg.get("min_confidence", 0.30), 0.30)
    # Placeholder: 无事件数据时返回中性
    events_dir = _ASHARE_DATA / "research_probability"
    score = 0.5
    try:
        import json
        events_file = events_dir / "event_impacts.json"
        if events_file.exists():
            with open(events_file, encoding="utf-8") as f:
                data = json.load(f)
            impacts = data.get(ts_code, [])
            if impacts:
                total_weight = 0.0
                weighted = 0.0
                for ev in impacts:
                    conf = _safe_float(ev.get("confidence", 0.0))
                    if conf < min_conf:
                        continue
                    direction = ev.get("direction", "neutral")
                    dir_score = {"positive": 1.0, "negative": 0.0, "neutral": 0.5}.get(direction, 0.5)
                    weighted += dir_score * conf
                    total_weight += conf
                if total_weight > 1e-9:
                    score = weighted / total_weight
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _clamp(score)


def _score_fundamental(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """基本面维度 — 从 scores 获取因子打分。

    因子: value / growth / quality / momentum
    """
    dim_cfg = config.get("dimensions", {}).get("fundamental", {})
    factor_weights = dim_cfg.get("factors", {"value": 0.30, "growth": 0.30, "quality": 0.20, "momentum": 0.20})
    scores_dir = _ASHARE_DATA / "forecasts"
    score = 0.5
    try:
        import json
        scores_file = scores_dir / "factor_scores.json"
        if scores_file.exists():
            with open(scores_file, encoding="utf-8") as f:
                data = json.load(f)
            stock_scores = data.get(ts_code, {})
            if stock_scores:
                total_w = 0.0
                weighted = 0.0
                for factor, w in factor_weights.items():
                    raw = _safe_float(stock_scores.get(factor, 0.5), 0.5)
                    # 原始分假设已在 [0, 1]
                    weighted += _clamp(raw) * _safe_float(w, 0.0)
                    total_w += _safe_float(w, 0.0)
                if total_w > 1e-9:
                    score = weighted / total_w
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _clamp(score)


def _score_capital(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """资金维度 — 从 moneyflow 获取主力资金净流入。

    Placeholder: 近 N 日主力净流入 > 0 则加分。
    """
    dim_cfg = config.get("dimensions", {}).get("capital", {})
    window = int(dim_cfg.get("window_days", 5))
    positive_threshold = _safe_float(dim_cfg.get("positive_threshold", 0.0), 0.0)
    score = 0.5
    try:
        import json
        moneyflow_dir = _ASHARE_DATA / "tushare_cache"
        # Placeholder: 假设有 moneyflow 缓存
        moneyflow_file = moneyflow_dir / "moneyflow.json"
        if moneyflow_file.exists():
            with open(moneyflow_file, encoding="utf-8") as f:
                data = json.load(f)
            flows = data.get(ts_code, [])
            recent = flows[:window] if isinstance(flows, list) else []
            if recent:
                total_net = sum(_safe_float(f.get("net_amount", 0.0)) for f in recent if isinstance(f, dict))
                if total_net > positive_threshold:
                    score = 0.6 + _clamp(total_net / 1e8 / 10.0, 0.0, 0.4)  # 1亿净流入 → 满分
                else:
                    score = 0.4 + _clamp(total_net / 1e8 / 10.0, -0.4, 0.2)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _clamp(score)


def _score_technical(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """技术维度 — 从 momentum / price action 获取动量信号。

    Placeholder: 近 N 日涨幅 + 均线趋势确认。
    """
    dim_cfg = config.get("dimensions", {}).get("technical", {})
    window = int(dim_cfg.get("window_days", 20))
    ma_short = int(dim_cfg.get("ma_short", 5))
    ma_long = int(dim_cfg.get("ma_long", 20))
    score = 0.5
    try:
        import json
        # Placeholder: 假设有日线缓存
        daily_file = _ASHARE_DATA / "tushare_cache" / f"{ts_code}_daily.json"
        if daily_file.exists():
            with open(daily_file, encoding="utf-8") as f:
                bars = json.load(f)
            if isinstance(bars, list) and len(bars) >= ma_long:
                closes = [_safe_float(b.get("close", 0.0)) for b in bars if isinstance(b, dict)]
                if len(closes) >= ma_long:
                    # 动量: 近 window 日涨幅
                    momentum = (closes[-1] - closes[-window]) / closes[-window] if closes[-window] > 1e-9 else 0.0
                    # 均线趋势: MA_short > MA_long → 多头
                    ma_s = sum(closes[-ma_short:]) / ma_short
                    ma_l = sum(closes[-ma_long:]) / ma_long
                    trend_bonus = 0.1 if ma_s > ma_l else -0.1
                    # 动量 [-10%, +10%] → [0, 1]
                    score = 0.5 + _clamp(momentum / 0.20, -0.5, 0.5) + trend_bonus
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
        pass
    return _clamp(score)


def _score_sentiment(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """情绪维度 — 从 signals 获取市场情绪信号。

    Placeholder: 涨跌停比 / 换手率 / 北向情绪。
    极端情绪降权 (过热 → 谨慎)。
    """
    dim_cfg = config.get("dimensions", {}).get("sentiment", {})
    extreme_threshold = _safe_float(dim_cfg.get("extreme_threshold", 0.85), 0.85)
    score = 0.5
    try:
        import json
        signals_dir = _ASHARE_DATA / "sector"
        signals_file = signals_dir / "sentiment.json"
        if signals_file.exists():
            with open(signals_file, encoding="utf-8") as f:
                data = json.load(f)
            stock_sentiment = data.get(ts_code, {})
            if stock_sentiment:
                raw = _safe_float(stock_sentiment.get("score", 0.5), 0.5)
                # 极端情绪降权
                if raw > extreme_threshold:
                    score = 0.5 - (raw - extreme_threshold) * 2.0  # 过热 → 降分
                else:
                    score = raw
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _clamp(score)


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
            from universe_filter import filter_universe
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
