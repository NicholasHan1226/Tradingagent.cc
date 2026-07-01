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
score_stock(market, ts_code, reader, date, config) → same result with market-aware reader queries
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

from shared.data.reader import TradingagentDataReader

_DATA_READER: TradingagentDataReader | None = None


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

def _get_data_reader(config: dict[str, Any] | None = None) -> TradingagentDataReader:
    """Return the configured fail-safe data reader."""
    injected = (config or {}).get("_data_reader")
    if injected is not None:
        return injected
    global _DATA_READER
    if _DATA_READER is None:
        _DATA_READER = TradingagentDataReader()
    return _DATA_READER


def _strip_suffix(ts_code: str) -> str:
    """Return code without exchange suffix, e.g. 600519.SH -> 600519."""
    return ts_code.split(".", 1)[0] if "." in ts_code else ts_code


def _symbol_variants(ts_code: str) -> list[str]:
    stripped = _strip_suffix(ts_code)
    return [stripped, ts_code] if stripped != ts_code else [ts_code]


def _looks_like_date(value: Any) -> bool:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return True
    if len(raw) == 10 and raw[4] in {"-", "/"} and raw[7] in {"-", "/"}:
        return True
    return False


def _reader_market(config: dict[str, Any]) -> str:
    return str(config.get("_market") or "ashare")


def _direction_score(impact_hint: Any) -> float:
    direction = str(impact_hint or "").split(":", 1)[0].strip().lower()
    return {"positive": 1.0, "negative": 0.0, "mixed": 0.5, "neutral": 0.5}.get(direction, 0.5)


def _score_macro(ts_code: str, date: str, config: dict[str, Any]) -> float:
    """宏观维度 — 从 MarketGraph all_weather_regime.csv 获取当前 regime。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("macro", {})
        regime_scores = dim_cfg.get("regime_scores", {})
        row = _get_data_reader(config).get_regime()
        if not row:
            return 0.5
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
        total_weight = 0.0
        weighted = 0.0
        allowed_status = {"needs_review", "promoted", "approved"}
        data_reader = _get_data_reader(config)
        market = _reader_market(config)
        for symbol in _symbol_variants(ts_code):
            get_events = getattr(data_reader, "get_events", None)
            rows = get_events(market, symbol, None, date) if callable(get_events) else []
            for row in rows:
                impact = (
                    row.get("proposed_impact_hint")
                    or row.get("impact_hint")
                    or row.get("direction")
                    or row.get("sentiment")
                )
                if impact is None:
                    continue
                conf = _safe_float(row.get("confidence"), 0.0)
                if conf <= 0.0:
                    conf = _safe_float(row.get("score"), 0.0)
                if conf <= 0.0:
                    conf = 0.5
                if conf < min_conf:
                    continue
                weighted += _direction_score(impact) * conf
                total_weight += conf
            if total_weight > 1e-9:
                return _clamp(weighted / total_weight)
        for row in _get_data_reader(config).get_event_candidates():
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
        rows: list[dict[str, Any]] = []
        data_reader = _get_data_reader(config)
        market = _reader_market(config)
        for symbol in _symbol_variants(ts_code):
            rows = data_reader.get_factors(market, symbol)
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
    """资金维度 — 从 SharedSignals factor rows 读取窗口内主力净流入。"""
    try:
        dim_cfg = config.get("dimensions", {}).get("capital", {})
        window = max(1, int(dim_cfg.get("window_days", 5)))
        end_date = datetime.strptime(date, "%Y%m%d")
        start_date = end_date - timedelta(days=window - 1)
        total_net = 0.0
        found = False
        data_reader = _get_data_reader(config)
        market = _reader_market(config)
        moneyflow_names = {
            "net_mf_amount",
            "moneyflow",
            "capital_flow",
            "main_net_inflow",
            "main_moneyflow",
        }
        for symbol in _symbol_variants(ts_code):
            for row in data_reader.get_factors(market, symbol):
                factor_name = str(row.get("factor_name") or "").strip().lower()
                if factor_name not in moneyflow_names:
                    continue
                raw_time = str(row.get("event_time") or "")[:10]
                try:
                    event_day = datetime.strptime(raw_time.replace("-", ""), "%Y%m%d")
                except ValueError:
                    continue
                if start_date <= event_day <= end_date:
                    total_net += _safe_float(row.get("value"), 0.0)
                    found = True
            if found:
                break
        if not found:
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
        end_date = datetime.strptime(date, "%Y%m%d")
        start_date = (end_date - timedelta(days=90)).strftime("%Y%m%d")
        rows: list[dict[str, Any]] = []
        data_reader = _get_data_reader(config)
        market = _reader_market(config)
        for symbol in _symbol_variants(ts_code):
            rows = data_reader.get_bars_daily(market, symbol, start_date, date)
            if len(rows) >= ma_long:
                break
        if len(rows) < ma_long:
            return 0.5
        closes = [_safe_float(row.get("close"), 0.0) for row in rows]
        closes = [c for c in closes if c > 0]
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
        total_weight = 0.0
        weighted = 0.0
        allowed_status = {"sentiment_signal", "needs_review", "promoted"}
        for row in _get_data_reader(config).get_sentiment():
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


def score_stock(
    market_or_ts_code: str,
    symbol_or_date: str | None = None,
    reader: TradingagentDataReader | None = None,
    date: str | None = None,
    config: dict[str, Any] | None = None,
    *,
    data_reader: TradingagentDataReader | None = None,
    market: str | None = None,
) -> dict[str, float]:
    """对单只股票进行六维打分。

    Args:
        market_or_ts_code: 旧式调用中为股票代码；新式调用中为 market
        symbol_or_date: 旧式调用中为日期；新式调用中为股票代码
        reader: 新式调用中的 reader 注入
        date: 日期 (YYYYMMDD), 默认今天
        config: 可选权重配置覆盖
        data_reader: 旧式调用中的 reader 注入，便于测试或隔离数据源
        market: 旧式调用的可选 market 覆盖，默认 "ashare"

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
    if (
        symbol_or_date is not None
        and not _looks_like_date(symbol_or_date)
        and (reader is not None or date is not None or config is not None)
        and market is None
    ):
        market_name = str(market_or_ts_code or "ashare")
        ts_code = str(symbol_or_date)
        if data_reader is None:
            data_reader = reader
    else:
        market_name = str(market or "ashare")
        ts_code = str(market_or_ts_code)
        if date is None:
            date = symbol_or_date
        if data_reader is None:
            data_reader = reader

    if not ts_code:
        return {k: 0.0 for k in (*_DEFAULT_WEIGHTS, "combined")}
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    loaded_config = _load_weights()
    if config:
        for key, value in config.items():
            if key == "dimensions" and isinstance(value, dict):
                merged_dimensions = dict(loaded_config.get("dimensions", {}))
                merged_dimensions.update(value)
                loaded_config["dimensions"] = merged_dimensions
            elif key == "combined" and isinstance(value, dict):
                merged_combined = dict(loaded_config.get("combined", {}))
                merged_combined.update(value)
                loaded_config["combined"] = merged_combined
            else:
                loaded_config[key] = value
    config = loaded_config
    config["_market"] = market_name
    if data_reader is not None:
        config["_data_reader"] = data_reader
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
    data_reader: TradingagentDataReader | None = None,
    market: str = "ashare",
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
        scores = score_stock(ts_code, date, data_reader=data_reader, market=market)
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
