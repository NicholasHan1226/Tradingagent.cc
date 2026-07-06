from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PM.probability_model import MARKET_PROBABILITY_KEYS


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = ROOT / "shared" / "review" / "pm" / "model_probabilities.jsonl"
DEFAULT_SUMMARY_PATH = ROOT / "shared" / "review" / "pm" / "model_probabilities_summary.json"
EXPLICIT_PROBABILITY_KEYS = (
    "research_probability",
    "marketgraph_probability",
    "external_probability",
    "forecast_probability",
    "calibrated_probability",
    "analyst_probability",
    "fair_probability",
    "estimated_probability",
)


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:
        return None
    return result


def _probability(value: Any) -> float | None:
    result = _safe_float(value)
    if result is None:
        return None
    return result if 0 < result < 1 else None


def _first_probability(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        result = _probability(row.get(key))
        if result is not None:
            return result
    return None


def _clamp(value: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, value))


def _row_id(row: dict[str, Any]) -> str:
    for key in ("market_id", "condition_id", "symbol", "slug", "question"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _sentiment_probability(row: dict[str, Any]) -> float | None:
    for key in ("sentiment_score", "nlp_sentiment", "research_sentiment"):
        raw = _safe_float(row.get(key))
        if raw is None:
            continue
        if -1 <= raw < 0:
            return _clamp((raw + 1.0) / 2.0)
        if 0 <= raw <= 1:
            return raw
    return None


def _liquidity_confidence(row: dict[str, Any]) -> float:
    explicit = _safe_float(row.get("liquidity_score") or row.get("depth_score"))
    if explicit is not None:
        return _clamp(explicit, 0.0, 1.0)
    liquidity = max(_safe_float(row.get("liquidity")) or 0.0, _safe_float(row.get("volume")) or 0.0)
    if liquidity >= 50000:
        return 1.0
    if liquidity >= 5000:
        return 0.70 + (liquidity - 5000) / 45000 * 0.30
    if liquidity > 0:
        return 0.30 + liquidity / 5000 * 0.40
    return 0.45


def _event_clarity(row: dict[str, Any]) -> float:
    explicit = _safe_float(row.get("event_clarity") or row.get("clarity_score") or row.get("resolution_clarity"))
    if explicit is not None:
        return _clamp(explicit, 0.0, 1.0)
    score = 0.35
    if row.get("question") or row.get("title"):
        score += 0.15
    if row.get("description") or row.get("rules"):
        score += 0.15
    if row.get("resolution_source") or row.get("outcome_source"):
        score += 0.25
    return _clamp(score, 0.0, 1.0)


def _parse_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for candidate, fmt in ((raw[:10], "%Y-%m-%d"), (raw[:10], "%Y/%m/%d"), (raw[:8], "%Y%m%d")):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def _settlement_confidence(row: dict[str, Any], generated_at: str) -> float:
    end_date = _parse_date(row.get("end_date") or row.get("end_time") or row.get("close_time") or row.get("resolution_time"))
    current = _parse_date(generated_at)
    if end_date is None or current is None:
        return 0.50
    days = (end_date.date() - current.date()).days
    if days < 0:
        return 0.10
    if days <= 7:
        return 0.85
    if days <= 30:
        return 0.95
    if days <= 90:
        return 0.75
    if days <= 180:
        return 0.45
    return 0.30


def _combined_confidence(row: dict[str, Any], generated_at: str) -> float:
    confidence = (
        _event_clarity(row) * 0.40
        + _liquidity_confidence(row) * 0.35
        + _settlement_confidence(row, generated_at) * 0.25
    )
    return round(_clamp(confidence, 0.0, 1.0), 4)


def _explicit_research_probability(row: dict[str, Any]) -> tuple[float, str] | None:
    for key in EXPLICIT_PROBABILITY_KEYS:
        probability = _probability(row.get(key))
        if probability is not None:
            source = str(row.get("research_source") or row.get("model_source") or row.get("source") or "pm_research_probability")
            return probability, source
    return None


def _build_probability_record(row: dict[str, Any], generated_at: str, max_adjustment: float, min_confidence: float) -> tuple[dict[str, Any] | None, str]:
    market_probability = _first_probability(row, MARKET_PROBABILITY_KEYS)
    if market_probability is None:
        return None, "missing_market_probability"

    explicit = _explicit_research_probability(row)
    if explicit:
        model_probability, source = explicit
        confidence = _combined_confidence(row, generated_at)
        reason = "explicit_independent_probability"
    else:
        sentiment_probability = _sentiment_probability(row)
        if sentiment_probability is None:
            return None, "missing_independent_probability"
        confidence = _combined_confidence(row, generated_at)
        if confidence < min_confidence or abs(sentiment_probability - 0.5) < 0.20:
            return None, "independent_evidence_too_weak"
        confidence = min(confidence, 0.49)
        adjustment = (sentiment_probability - 0.5) * 2.0 * max_adjustment * confidence
        model_probability = _clamp(market_probability + adjustment)
        source = "pm_research_sentiment_v1"
        reason = "bounded_sentiment_adjustment"

    market_id = _row_id(row)
    if not market_id:
        return None, "missing_market_id"
    edge = model_probability - market_probability
    return {
        "generated_at": generated_at,
        "market_id": market_id,
        "slug": row.get("slug"),
        "question": row.get("question") or row.get("title"),
        "model_probability": round(model_probability, 6),
        "market_probability": round(market_probability, 6),
        "edge": round(edge, 6),
        "model_source": source,
        "model_confidence": confidence,
        "model_reason": reason,
    }, "ok"


def _read_markets(reader: Any, limit: int) -> list[dict[str, Any]]:
    try:
        rows = reader.get_pm_markets(limit=limit, active_only=True)
    except TypeError:
        try:
            rows = reader.get_pm_markets(limit=limit)
        except TypeError:
            rows = reader.get_pm_markets()
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    tmp_path.replace(path)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def generate_pm_model_probabilities(
    *,
    reader: Any | None = None,
    output_path: Path | str | None = None,
    summary_path: Path | str | None = None,
    limit: int = 100,
    generated_at: str | None = None,
    max_adjustment: float = 0.06,
    min_confidence: float = 0.55,
) -> dict[str, Any]:
    if reader is None:
        from shared.data.reader import TradingagentDataReader

        reader = TradingagentDataReader()
    generated = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    markets = _read_markets(reader, limit=limit)
    records: list[dict[str, Any]] = []
    skip_reasons: dict[str, int] = {}
    for row in markets:
        record, reason = _build_probability_record(row, generated, max_adjustment, min_confidence)
        if record is None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        records.append(record)

    output = Path(output_path) if output_path is not None else DEFAULT_OUTPUT_PATH
    summary = Path(summary_path) if summary_path is not None else DEFAULT_SUMMARY_PATH
    _write_jsonl_atomic(output, records)
    payload = {
        "job": "job_pm_research_probability",
        "state": "ok",
        "generated_at": generated,
        "market": "PM",
        "market_rows": len(markets),
        "record_count": len(records),
        "skipped_count": sum(skip_reasons.values()),
        "skip_reasons": skip_reasons,
        "output_file": str(output),
        "model_policy": "explicit_independent_probability_or_bounded_sentiment_only",
        "reader_degraded": bool(getattr(reader, "degraded", False)),
        "reader_errors": list(getattr(reader, "errors", []) or [])[-5:],
    }
    _write_json_atomic(summary, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY_PATH))
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    payload = generate_pm_model_probabilities(output_path=args.output, summary_path=args.summary, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
