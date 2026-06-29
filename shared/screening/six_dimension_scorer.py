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

import csv
import sqlite3
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

# Ashare 数据根目录
_ASHARE_DATA = Path("/opt/investment/Ashare/data")
_MARKETGRAPH_DATA = Path("/opt/investment/MarketGraph/data")
_MARKETDATA_DB = Path("/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite")
_REGIME_FILE = _MARKETGRAPH_DATA / "all_weather_regime.csv"
_EVENT_FILE = _MARKETGRAPH_DATA / "intake" / "event_candidates.csv"
_SENTIMENT_FILE = _MARKETGRAPH_DATA / "intake" / "sentiment_signals.csv"
_MONEYFLOW_DIR = _ASHARE_DATA / "tushare_cache" / "moneyflow"

_MARKETDATA_CONN: sqlite3.Connection | None = None
_MARKETDATA_CONN_FAILED = False


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


# ── 六维打分函数 (各维度从真实数据源读取) ──

def _get_marketdata_conn() -> sqlite3.Connection | None:
    """Open MarketGraphRuntime marketdata.sqlite once per process, read-only."""
    global _MARKETDATA_CONN, _MARKETDATA_CONN_FAILED
    if _MARKETDATA_CONN is not None:
        return _MARKETDATA_CONN
    if _MARKETDATA_CONN_FAILED:
        return None
    try:
        if not _MARKETDATA_DB.exists():
            _MARKETDATA_CONN_FAILED = True
            return None
        conn = sqlite3.connect(f"file:{_MARKETDATA_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        _MARKETDATA_CONN = conn
        return _MARKETDATA_CONN
    except Exception:
        _MARKETDATA_CONN_FAILED = True
        return None


def _strip_suffix(ts_code: str) -> str:
    """Return code without exchange suffix, e.g. 600519.SH -> 600519."""
    return ts_code.split(".", 1)[0] if "." in ts_code else ts_code


def _direction_score(impact_hint: Any) -> float:
    direction = str(impact_hint or "").split(":", 1)[0].strip().lower()
    return {"positive": 1.0, "negative": 0.0, "mixed": 0.5, "neutral": 0.5}.get(direction, 0.5)


def _score_macro(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """宏观维度 — 从 MarketGraph all_weather_regime.csv 获取当前 regime。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("macro", {})
        regime_scores = dim_cfg.get("regime_scores", {})
        if not _REGIME_FILE.exists():
            return 0.5
        with open(_REGIME_FILE, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
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
    """事件维度 — 从 MarketGraph event_candidates.csv 聚合个股事件方向。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("event", {})
        min_conf = _safe_float(dim_cfg.get("min_confidence", 0.30), 0.30)
        if not _EVENT_FILE.exists():
            return 0.5
        total_weight = 0.0
        weighted = 0.0
        allowed_status = {"needs_review", "promoted", "approved"}
        with open(_EVENT_FILE, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("subject_code") != ts_code:
                    continue
                if row.get("subject_type") != "stock":
                    continue
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
    """基本面维度 — 从 market_factors 读取最新因子值。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("fundamental", {})
        factor_weights = dim_cfg.get("factors", {"value": 0.30, "growth": 0.30, "quality": 0.20, "momentum": 0.20})
        conn = _get_marketdata_conn()
        if conn is None:
            return 0.5
        symbols = [_strip_suffix(ts_code)]
        if ts_code not in symbols:
            symbols.append(ts_code)
        rows: list[sqlite3.Row] = []
        for symbol in symbols:
            rows = conn.execute(
                "SELECT factor_name, value FROM market_factors "
                "WHERE market='Ashare' AND symbol=? ORDER BY event_time DESC",
                (symbol,),
            ).fetchall()
            if rows:
                break
        if not rows:
            return 0.5
        latest_by_factor: dict[str, float] = {}
        for row in rows:
            factor = str(row["factor_name"] or "").strip().lower()
            if factor and factor not in latest_by_factor:
                raw = _safe_float(row["value"], 0.5)
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
    """资金维度 — 从 Ashare moneyflow CSV 读取窗口内主力净流入。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("capital", {})
        window = max(1, int(dim_cfg.get("window_days", 5)))
        if not _MONEYFLOW_DIR.exists():
            return 0.5
        end_date = datetime.strptime(date, "%Y%m%d")
        total_net = 0.0
        found_file = False
        for i in range(window):
            day = (end_date - timedelta(days=i)).strftime("%Y%m%d")
            moneyflow_file = _MONEYFLOW_DIR / f"{day}.csv"
            if not moneyflow_file.exists():
                continue
            found_file = True
            with open(moneyflow_file, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("ts_code") == ts_code:
                        total_net += _safe_float(row.get("net_mf_amount"), 0.0)
        if not found_file:
            return 0.5
        if total_net > 0:
            score = 0.6 + _clamp(total_net / 1e5, 0.0, 0.4)
        else:
            score = 0.4 + _clamp(total_net / 1e5, -0.4, 0.2)
        return _clamp(score)
    except Exception:
        return 0.5


def _score_technical(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """技术维度 — 从 market_bars_daily 读取日线并计算动量和均线趋势。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("technical", {})
        window = max(1, int(dim_cfg.get("window_days", 20)))
        ma_short = max(1, int(dim_cfg.get("ma_short", 5)))
        ma_long = max(1, int(dim_cfg.get("ma_long", 20)))
        needed = max(window, ma_short, ma_long)
        conn = _get_marketdata_conn()
        if conn is None:
            return 0.5
        symbols = [_strip_suffix(ts_code)]
        if ts_code not in symbols:
            symbols.append(ts_code)
        rows: list[sqlite3.Row] = []
        for symbol in symbols:
            rows = conn.execute(
                "SELECT trade_date, close FROM market_bars_daily "
                "WHERE market='Ashare' AND symbol=? ORDER BY trade_date DESC LIMIT 60",
                (symbol,),
            ).fetchall()
            if len(rows) >= ma_long:
                break
        if len(rows) < ma_long:
            return 0.5
        closes_desc = [_safe_float(row["close"], 0.0) for row in rows]
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
    """情绪维度 — 从 MarketGraph sentiment_signals.csv 聚合个股情绪信号。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("sentiment", {})
        extreme_threshold = _safe_float(dim_cfg.get("extreme_threshold", 0.85), 0.85)
        if not _SENTIMENT_FILE.exists():
            return 0.5
        total_weight = 0.0
        weighted = 0.0
        allowed_status = {"sentiment_signal", "needs_review", "promoted"}
        with open(_SENTIMENT_FILE, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row.get("subject_code"):
                    continue
                if row.get("subject_code") != ts_code:
                    continue
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
