#!/usr/bin/env python3
"""多空对辩 — LLM 驱动的 bull vs bear debate。

Bull case 来自六维打分 (宏观/事件/基本面/资金/技术/情绪),
Bear case 来自风险/估值陷阱/负向信号。
输出 belief_score ∈ [0, 1], 默认 0.5 (中性)。

不排除标的, 仅调整仓位权重。
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any

# DeepSeek API 配置 (与 MarketGraph/deploy/mg_agent/deepseek_client.py 一致)
_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
_MODEL = "deepseek-v4-pro"  # 推理用 pro
_TIMEOUT = int(os.environ.get("TRADINGS_DEEPSEEK_TIMEOUT", "90"))
_MAX_RETRIES = int(os.environ.get("TRADINGS_DEEPSEEK_RETRIES", "3"))
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}

# 六维名称 (与 screening 模块对齐)
_DIMENSIONS = ["macro", "event", "fundamental", "capital", "technical", "sentiment"]


def _get_key() -> str:
    """读取 DeepSeek API key。优先 env, 其次 cron env 文件。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    # 回退: 从 marketgraph_cron.env 读取
    for path in (
        "/opt/investment/MarketGraph/deploy/marketgraph_cron.env",
        "/opt/investment/.env",
    ):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return ""


def _call_deepseek(messages: list[dict[str, str]], *, agent: str = "bull_bear_debate") -> dict[str, Any]:
    """调用 DeepSeek, 指数退避重试。返回解析后的 content dict。

    Dry-run 模式 (key 缺失): 返回占位结果, 不崩溃。
    """
    key = _get_key()
    if not key:
        print(f"[bull_bear_debate] DEEPSEEK_API_KEY not set — dry-run mode", flush=True)
        return {
            "bull_case": "dry-run: API key missing",
            "bear_case": "dry-run: API key missing",
            "belief_score": 0.5,
            "_dry_run": True,
        }

    # JSON 模式要求 prompt 含 "json"
    msgs = [dict(m) for m in messages]
    has_json = any("json" in (m.get("content", "") or "").lower() for m in msgs)
    if not has_json:
        for m in reversed(msgs):
            if m.get("role") == "user":
                m["content"] = (m.get("content", "") or "") + "\nRespond in JSON format."
                break

    payload = {
        "model": _MODEL,
        "messages": msgs,
        "max_tokens": 2000,
        "temperature": 0.3,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode("utf-8")
    last_err = ""

    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                _DEEPSEEK_URL, data=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw_data = resp.read()

            data = json.loads(raw_data)
            choices = data.get("choices", [])
            if not choices:
                last_err = "empty_choices"
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(1 * (2 ** attempt) + random.uniform(0, 1))
                    continue
                raise RuntimeError(f"DeepSeek empty choices: {last_err}")

            content = choices[0].get("message", {}).get("content", "")
            try:
                parsed = json.loads(content) if content else {}
            except json.JSONDecodeError:
                parsed = {"_raw": content}
            if not isinstance(parsed, dict):
                parsed = {"_raw": parsed}
            parsed["_dry_run"] = False
            return parsed

        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {err_body}"
            if e.code in _RETRYABLE_HTTP and attempt < _MAX_RETRIES - 1:
                time.sleep(1 * (2 ** attempt) + random.uniform(0, 1))
                continue
            raise RuntimeError(f"DeepSeek call failed: {last_err}")

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = f"network: {e}"
            if attempt < _MAX_RETRIES - 1:
                time.sleep(1 * (2 ** attempt) + random.uniform(0, 1))
                continue
            raise RuntimeError(f"DeepSeek network error: {last_err}")

    raise RuntimeError(f"DeepSeek failed after {_MAX_RETRIES} retries: {last_err}")


def _build_prompt(ts_code: str, scores: dict[str, Any]) -> list[dict[str, str]]:
    """构建多空对辩 prompt。"""
    # 六维得分摘要
    dim_summary = []
    for d in _DIMENSIONS:
        v = scores.get(d)
        if isinstance(v, dict):
            dim_summary.append(f"  - {d}: score={v.get('score', 'N/A')}, note={v.get('note', '')}")
        elif v is not None:
            dim_summary.append(f"  - {d}: {v}")
    dims_text = "\n".join(dim_summary) if dim_summary else "  (无六维数据)"

    combined = scores.get("combined")
    if combined is None:
        combined = scores.get("composite")
    if combined is None:
        combined = "N/A"

    system = (
        "你是资深A股多空对辩分析师。对给定标的构建 bull case 和 bear case, "
        "并输出 belief_score ∈ [0, 1] 表示多头信心。\n"
        "bull case 基于六维打分中的正向信号。\n"
        "bear case 必须覆盖: 估值陷阱 / 逻辑证伪 / 流动性风险 / 政策反转。\n"
        "belief_score=0.5 表示中性, >0.5 偏多, <0.5 偏空。\n"
        "不排除标的, 仅调整权重。"
    )
    user = (
        f"标的: {ts_code}\n"
        f"六维综合得分: {combined}\n"
        f"六维明细:\n{dims_text}\n\n"
        "请输出 JSON, 包含字段:\n"
        '{"bull_case": "多头理由(2-4条)", '
        '"bear_case": "空头风险(2-4条, 必须含估值陷阱/逻辑证伪/流动性/政策)", '
        '"belief_score": 0.0到1.0的浮点数, '
        '"key_risk": "最大单一风险"}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _clamp_belief(v: Any) -> float:
    """安全裁剪 belief_score 到 [0, 1]。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    if f != f:  # NaN
        return 0.5
    return max(0.0, min(1.0, f))


def _score_value(scores: dict[str, Any], key: str) -> float:
    value = scores.get(key)
    if isinstance(value, dict):
        value = value.get("score", value.get("value"))
    return _clamp_belief(value if value is not None else 0.5)


def _fast_debate(ts_code: str, scores: dict[str, Any], source: str) -> dict[str, Any]:
    combined = scores.get("combined", scores.get("composite", 0.5))
    belief = _clamp_belief(combined)
    positives = [dim for dim in _DIMENSIONS if _score_value(scores, dim) >= 0.6]
    risks = [dim for dim in _DIMENSIONS if _score_value(scores, dim) <= 0.4]
    bull = "正向维度: " + (", ".join(positives) if positives else "暂无明显优势")
    bear = "风险维度: " + (", ".join(risks) if risks else "暂无明显短板")
    return {
        "ts_code": ts_code,
        "bull_case": bull,
        "bear_case": bear,
        "belief_score": belief,
        "key_risk": bear,
        "source": source,
    }


def _debate_mode() -> str:
    return os.environ.get("TRADINGS_DEBATE_MODE", "live").strip().lower()


def debate(ts_code: str, scores: dict[str, Any]) -> dict[str, Any]:
    """多空对辩主函数。

    Args:
        ts_code: 标的代码, 如 "600519.SH"
        scores: 六维打分 dict, 可含 macro/event/fundamental/capital/technical/sentiment
                及 combined/composite 综合分

    Returns:
        {
            "ts_code": str,
            "bull_case": str,
            "bear_case": str,
            "belief_score": float,  # [0, 1]
            "key_risk": str,
        }
    """
    if not ts_code:
        raise ValueError("ts_code is required")
    if not isinstance(scores, dict):
        scores = {}

    mode = _debate_mode()
    if mode in {"fast", "heuristic", "deterministic", "off", "disabled"}:
        return _fast_debate(ts_code, scores, source=f"{mode}_debate")

    messages = _build_prompt(ts_code, scores)
    result = _call_deepseek(messages)

    belief = _clamp_belief(result.get("belief_score", 0.5))

    return {
        "ts_code": ts_code,
        "bull_case": str(result.get("bull_case", "")) or "N/A",
        "bear_case": str(result.get("bear_case", "")) or "N/A",
        "belief_score": belief,
        "key_risk": str(result.get("key_risk", "")) or "N/A",
        "source": "dry_run" if result.get("_dry_run") else "live",
    }


if __name__ == "__main__":
    # 自测
    test_scores = {
        "macro": {"score": 0.7, "note": "regime=growth, 适合权益"},
        "event": {"score": 0.8, "note": "利好政策落地"},
        "fundamental": {"score": 0.6, "note": "ROE 15%"},
        "capital": {"score": 0.5, "note": "主力净流入中性"},
        "technical": {"score": 0.7, "note": "突破前高"},
        "sentiment": {"score": 0.6, "note": "舆情偏多"},
        "combined": 0.68,
    }
    r = debate("600519.SH", test_scores)
    print(json.dumps(r, ensure_ascii=False, indent=2))
