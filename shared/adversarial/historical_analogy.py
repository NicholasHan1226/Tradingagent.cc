#!/usr/bin/env python3
"""历史类比 — 检索历史相似条件, 返回先验分布参考。

从 memory/global/event_analogies.jsonl 读取历史类比记录。
不预测, 只提供先验分布参考。

文件格式 (jsonl, 每行一个 JSON):
    {"date": "2024-03-15", "condition": "regime=growth+sector=AI", "ts_code": "600519.SH",
     "outcome": "上涨", "return": 0.12, "horizon_days": 30, "tags": ["growth", "AI"]}
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 候选路径: 相对脚本 → 项目根 → 绝对路径
_CANDIDATE_PATHS = [
    Path(__file__).resolve().parent.parent.parent / "memory" / "global" / "event_analogies.jsonl",
    Path(__file__).resolve().parent.parent.parent.parent / "memory" / "global" / "event_analogies.jsonl",
    Path("/opt/investment/memory/global/event_analogies.jsonl"),
    Path("/opt/investment/Tradings/memory/global/event_analogies.jsonl"),
]


def _find_analogies_file() -> Path | None:
    """查找 event_analogies.jsonl 文件位置。"""
    for p in _CANDIDATE_PATHS:
        if p.exists() and p.is_file():
            return p
    return None


def _load_analogies(filepath: Path | None = None) -> list[dict[str, Any]]:
    """加载所有历史类比记录。"""
    if filepath is None:
        filepath = _find_analogies_file()
    if filepath is None or not filepath.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            # 兼容两种格式: jsonl (每行一个) 或 json array
            if content.startswith("["):
                data = json.loads(content)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            records.append(item)
            else:
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict):
                            records.append(rec)
                    except json.JSONDecodeError:
                        continue
    except (OSError, json.JSONDecodeError):
        return []

    return records


def _match_score(record: dict[str, Any], condition: str, ts_code: str | None = None) -> float:
    """计算记录与查询条件的匹配度 (0-1)。"""
    score = 0.0

    # ts_code 匹配
    rec_code = record.get("ts_code", "")
    if ts_code and rec_code and ts_code == rec_code:
        score += 0.3

    # condition 关键词匹配
    rec_condition = str(record.get("condition", "")).lower()
    rec_tags = record.get("tags", [])
    if isinstance(rec_tags, list):
        rec_tags_str = " ".join(str(t).lower() for t in rec_tags)
    else:
        rec_tags_str = str(rec_tags).lower()

    rec_text = f"{rec_condition} {rec_tags_str}"

    # 拆分 condition 为关键词
    keywords = [k.strip().lower() for k in condition.replace("+", " ").replace(",", " ").split() if k.strip()]
    if not keywords:
        return score

    matched = 0
    for kw in keywords:
        if kw in rec_text:
            matched += 1

    if keywords:
        score += 0.7 * (matched / len(keywords))

    return min(1.0, score)


def find_analogies(
    ts_code: str | None = None,
    condition: str = "",
    top_n: int = 10,
    min_score: float = 0.1,
) -> list[dict[str, Any]]:
    """检索历史相似条件。

    Args:
        ts_code: 可选, 标的代码 (匹配同标的加分)
        condition: 条件描述, 如 "regime=growth+sector=AI" 或 "美联储加息"
        top_n: 返回前 N 条
        min_score: 最小匹配度阈值

    Returns:
        list of {
            "date": str,
            "outcome": str,
            "return": float,
            "horizon_days": int,
            "condition": str,
            "ts_code": str,
            "match_score": float,
        }
    """
    if not condition and not ts_code:
        raise ValueError("至少提供 condition 或 ts_code 之一")

    records = _load_analogies()
    if not records:
        return []

    # 计算匹配度并排序
    scored: list[dict[str, Any]] = []
    for rec in records:
        ms = _match_score(rec, condition or "", ts_code)
        if ms >= min_score:
            entry = {
                "date": rec.get("date", ""),
                "outcome": rec.get("outcome", ""),
                "return": rec.get("return", 0.0),
                "horizon_days": rec.get("horizon_days", 0),
                "condition": rec.get("condition", ""),
                "ts_code": rec.get("ts_code", ""),
                "match_score": round(ms, 3),
            }
            scored.append(entry)

    # 按匹配度降序, 取 top_n
    scored.sort(key=lambda x: x["match_score"], reverse=True)
    return scored[:top_n]


def analogy_summary(analogies: list[dict[str, Any]]) -> dict[str, Any]:
    """对类比结果做统计摘要 (先验分布参考)。

    Returns:
        {
            "count": int,
            "avg_return": float,
            "win_rate": float,  # return > 0 的比例
            "median_return": float,
            "sample_note": str,  # 样本量提示
        }
    """
    if not analogies:
        return {
            "count": 0,
            "avg_return": 0.0,
            "win_rate": 0.0,
            "median_return": 0.0,
            "sample_note": "无历史类比数据, 先验无参考",
        }

    returns = []
    for a in analogies:
        try:
            r = float(a.get("return", 0.0))
            returns.append(r)
        except (TypeError, ValueError):
            continue

    if not returns:
        return {
            "count": len(analogies),
            "avg_return": 0.0,
            "win_rate": 0.0,
            "median_return": 0.0,
            "sample_note": f"样本量 {len(analogies)}, 无有效收益数据",
        }

    returns.sort()
    n = len(returns)
    avg = sum(returns) / n
    median = returns[n // 2] if n % 2 == 1 else (returns[n // 2 - 1] + returns[n // 2]) / 2
    wins = sum(1 for r in returns if r > 0)
    win_rate = wins / n

    note = f"样本量 {n}"
    if n < 5:
        note += ", 样本不足, 先验参考价值有限"
    elif n < 20:
        note += ", 样本中等, 谨慎参考"
    else:
        note += ", 样本充足"

    return {
        "count": n,
        "avg_return": round(avg, 4),
        "win_rate": round(win_rate, 4),
        "median_return": round(median, 4),
        "sample_note": note,
    }


if __name__ == "__main__":
    # 自测
    print("=== find_analogies ===")
    r = find_analogies(condition="regime=growth", top_n=5)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("=== summary ===")
    print(json.dumps(analogy_summary(r), ensure_ascii=False, indent=2))
