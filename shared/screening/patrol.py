#!/usr/bin/env python3
"""巡检 — 因子衰减 / 分布偏斜 / 偏差检查。

定时扫描六维打分系统的健康状态:
- 因子衰减: 某维度分数是否持续偏低/偏高 (信号失效)
- 分布偏斜: 全市场打分分布是否严重偏斜 (过度集中)
- 偏差检查: 实际涨跌与打分方向是否一致 (预测力)

patrol(date, scores_universe) → {alerts, summary, timestamp}
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from shared.data.reader import TradingagentDataReader

_DATA_READER: TradingagentDataReader | None = None

# 巡检阈值
_THRESHOLDS: dict[str, float] = {
    # 因子衰减: 某维度均值 < 此值视为信号衰减
    "factor_decay_low": 0.30,
    # 因子衰减: 某维度均值 > 此值视为信号过热
    "factor_decay_high": 0.80,
    # 分布偏斜: 标准差 < 此值视为过度集中
    "distribution_concentrated_std": 0.05,
    # 分布偏斜: 偏度绝对值 > 此值视为严重偏斜
    "distribution_skew_threshold": 1.0,
    # 偏差检查: 打分前 20% 股票次日涨幅均值 < 此值视为预测力不足
    "prediction_power_threshold": 0.0,
    # 最小样本数
    "min_sample_size": 50,
}

_DIMENSIONS = ["macro", "event", "fundamental", "capital", "technical", "sentiment"]


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


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    variance = sum((x - m) ** 2 for x in values) / len(values)
    return variance ** 0.5


def _skewness(values: list[float]) -> float:
    """计算偏度 (skewness)。"""
    if len(values) < 3:
        return 0.0
    m = _mean(values)
    s = _std(values)
    if s < 1e-9:
        return 0.0
    n = len(values)
    return (n / ((n - 1) * (n - 2))) * sum(((x - m) / s) ** 3 for x in values)


def _check_factor_decay(scores_list: list[tuple[str, dict[str, float]]]) -> list[dict[str, Any]]:
    """因子衰减检查 — 某维度均值过低/过高。"""
    alerts: list[dict[str, Any]] = []
    if len(scores_list) < _THRESHOLDS["min_sample_size"]:
        return alerts

    now = datetime.now().isoformat(timespec="seconds")
    for dim in _DIMENSIONS + ["combined"]:
        values = [_safe_float(s.get(dim, 0.5)) for _, s in scores_list]
        dim_mean = _mean(values)
        dim_std = _std(values)

        if dim_mean < _THRESHOLDS["factor_decay_low"]:
            alerts.append({
                "type": "factor_decay",
                "severity": "high",
                "dimension": dim,
                "message": f"{dim} 均值 {dim_mean:.4f} < {_THRESHOLDS['factor_decay_low']:.2f} (信号衰减)",
                "mean": round(dim_mean, 4),
                "std": round(dim_std, 4),
                "timestamp": now,
            })
        elif dim_mean > _THRESHOLDS["factor_decay_high"]:
            alerts.append({
                "type": "factor_overheated",
                "severity": "medium",
                "dimension": dim,
                "message": f"{dim} 均值 {dim_mean:.4f} > {_THRESHOLDS['factor_decay_high']:.2f} (信号过热)",
                "mean": round(dim_mean, 4),
                "std": round(dim_std, 4),
                "timestamp": now,
            })

    return alerts


def _check_distribution(scores_list: list[tuple[str, dict[str, float]]]) -> list[dict[str, Any]]:
    """分布偏斜检查 — 打分分布是否过度集中或严重偏斜。"""
    alerts: list[dict[str, Any]] = []
    if len(scores_list) < _THRESHOLDS["min_sample_size"]:
        return alerts

    now = datetime.now().isoformat(timespec="seconds")
    combined_values = [_safe_float(s.get("combined", 0.5)) for _, s in scores_list]

    std = _std(combined_values)
    skew = _skewness(combined_values)

    if std < _THRESHOLDS["distribution_concentrated_std"]:
        alerts.append({
            "type": "distribution_concentrated",
            "severity": "high",
            "message": f"打分标准差 {std:.4f} < {_THRESHOLDS['distribution_concentrated_std']:.2f} (过度集中, 无区分度)",
            "std": round(std, 4),
            "skew": round(skew, 4),
            "timestamp": now,
        })

    if abs(skew) > _THRESHOLDS["distribution_skew_threshold"]:
        direction = "右偏 (高分集中)" if skew > 0 else "左偏 (低分集中)"
        alerts.append({
            "type": "distribution_skewed",
            "severity": "medium",
            "message": f"打分偏度 {skew:.4f} > {_THRESHOLDS['distribution_skew_threshold']:.1f} ({direction})",
            "std": round(std, 4),
            "skew": round(skew, 4),
            "timestamp": now,
        })

    return alerts


def _check_prediction_bias(
    scores_list: list[tuple[str, dict[str, float]]],
    date: str,
    reader: Any | None = None,
    market: str = "ashare",
) -> list[dict[str, Any]]:
    """偏差检查 — 打分方向与实际涨跌是否一致。

    Placeholder: 比较打分前 20% 股票的次日涨幅。
    """
    alerts: list[dict[str, Any]] = []
    if len(scores_list) < _THRESHOLDS["min_sample_size"]:
        return alerts

    now = datetime.now().isoformat(timespec="seconds")

    # 按 combined 降序 (已排, 但确保)
    sorted_scores = sorted(scores_list, key=lambda x: _safe_float(x[1].get("combined", 0.0)), reverse=True)

    # 前 20%
    top_n = max(1, len(sorted_scores) // 5)
    top_codes = [code for code, _ in sorted_scores[:top_n]]

    # 获取次日涨跌幅 (placeholder)
    next_returns: list[float] = []
    data_reader = _get_data_reader(reader)
    try:
        for ts_code in top_codes:
            bars: list[dict[str, Any]] = []
            for symbol in _symbol_variants(ts_code):
                bars = data_reader.get_bars_daily(market, symbol, None, date)
                if len(bars) >= 2:
                    break
            if len(bars) < 2:
                continue
            close_today = _safe_float(bars[-1].get("close", 0.0))
            close_prev = _safe_float(bars[-2].get("close", 0.0))
            if close_prev > 1e-9:
                ret = (close_today - close_prev) / close_prev
                next_returns.append(ret)
    except Exception:
        pass

    if next_returns:
        avg_return = _mean(next_returns)
        if avg_return < _THRESHOLDS["prediction_power_threshold"]:
            alerts.append({
                "type": "prediction_bias",
                "severity": "high",
                "message": f"打分前20%股票次日均涨跌 {avg_return:.4f} < 0 (预测力不足)",
                "top_n": top_n,
                "avg_return": round(avg_return, 4),
                "timestamp": now,
            })

    return alerts


def patrol(
    date: str | None = None,
    scores_list: list[tuple[str, dict[str, float]]] | None = None,
    reader: Any | None = None,
    market: str = "ashare",
) -> dict[str, Any]:
    """巡检主函数 — 聚合因子衰减/分布偏斜/偏差检查。

    Args:
        date: 日期 (YYYYMMDD), 默认今天
        scores_list: [(ts_code, scores), ...] 全市场打分结果, 默认自动计算

    Returns:
        {
            "timestamp": str,
            "date": str,
            "alerts": list[dict],
            "summary": {
                "total_alerts": int,
                "high_severity": int,
                "medium_severity": int,
                "sample_size": int,
                "factor_health": dict,  # 各维度均值/标准差
                "distribution": dict,   # combined 的均值/标准差/偏度
            },
        }
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    # 获取打分数据
    if scores_list is None:
        try:
            from .six_dimension_scorer import score_universe
            scores_list = score_universe(date, data_reader=reader, market=market)
        except ImportError:
            scores_list = []

    alerts: list[dict[str, Any]] = []
    now = datetime.now().isoformat(timespec="seconds")

    # 1. 因子衰减检查
    alerts.extend(_check_factor_decay(scores_list))

    # 2. 分布偏斜检查
    alerts.extend(_check_distribution(scores_list))

    # 3. 偏差检查
    alerts.extend(_check_prediction_bias(scores_list, date, reader, market))

    # 按 severity 排序
    severity_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: severity_order.get(a.get("severity", "low"), 2))

    # 汇总
    high_n = sum(1 for a in alerts if a.get("severity") == "high")
    medium_n = sum(1 for a in alerts if a.get("severity") == "medium")

    # 因子健康度
    factor_health: dict[str, dict[str, float]] = {}
    if scores_list:
        for dim in _DIMENSIONS + ["combined"]:
            values = [_safe_float(s.get(dim, 0.5)) for _, s in scores_list]
            factor_health[dim] = {
                "mean": round(_mean(values), 4),
                "std": round(_std(values), 4),
            }

    # 分布统计
    distribution: dict[str, float] = {}
    if scores_list:
        combined_values = [_safe_float(s.get("combined", 0.5)) for _, s in scores_list]
        distribution = {
            "mean": round(_mean(combined_values), 4),
            "std": round(_std(combined_values), 4),
            "skew": round(_skewness(combined_values), 4),
            "min": round(min(combined_values), 4) if combined_values else 0.0,
            "max": round(max(combined_values), 4) if combined_values else 0.0,
        }

    return {
        "timestamp": now,
        "date": date,
        "alerts": alerts,
        "summary": {
            "total_alerts": len(alerts),
            "high_severity": high_n,
            "medium_severity": medium_n,
            "sample_size": len(scores_list),
            "factor_health": factor_health,
            "distribution": distribution,
        },
    }


if __name__ == "__main__":
    import json

    # 构造测试数据
    test_scores = [
        ("600519.SH", {"macro": 0.7, "event": 0.6, "fundamental": 0.8, "capital": 0.55, "technical": 0.65, "sentiment": 0.5, "combined": 0.65}),
        ("000858.SZ", {"macro": 0.7, "event": 0.5, "fundamental": 0.7, "capital": 0.45, "technical": 0.55, "sentiment": 0.5, "combined": 0.58}),
        ("601318.SH", {"macro": 0.7, "event": 0.4, "fundamental": 0.65, "capital": 0.5, "technical": 0.5, "sentiment": 0.45, "combined": 0.53}),
    ] * 20  # 60 samples

    result = patrol("20260629", test_scores)
    print(json.dumps(result, ensure_ascii=False, indent=2))
