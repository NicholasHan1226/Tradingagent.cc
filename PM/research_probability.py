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


def _row_keys(row: dict[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []
    for key in ("market_id", "condition_id", "symbol", "slug", "question", "title"):
        value = str(row.get(key) or "").strip()
        if value:
            keys.append(value)
            keys.append(value.lower())
    return tuple(dict.fromkeys(keys))


def _explicit_research_probability(row: dict[str, Any]) -> tuple[float, str] | None:
    for key in EXPLICIT_PROBABILITY_KEYS:
        probability = _probability(row.get(key))
        if probability is not None:
            source = str(row.get("probability_source") or row.get("research_source") or row.get("model_source") or row.get("source") or "marketgraph_pm_research")
            return probability, source
    return None


def _build_probability_record(research_row: dict[str, Any], market_row: dict[str, Any] | None, generated_at: str) -> tuple[dict[str, Any] | None, str]:
    market_probability = _first_probability(market_row or {}, MARKET_PROBABILITY_KEYS)
    if market_probability is None:
        return None, "missing_market_probability"

    explicit = _explicit_research_probability(research_row)
    if not explicit:
        return None, "missing_marketgraph_research_probability"
    model_probability, source = explicit
    confidence = _safe_float(research_row.get("confidence") or research_row.get("model_confidence"))
    if confidence is None:
        confidence = 0.5
    confidence = round(_clamp(confidence, 0.0, 1.0), 4)
    reason = str(research_row.get("model_reason") or research_row.get("reason") or "marketgraph_research_probability")

    market_id = _row_id(research_row) or _row_id(market_row or {})
    if not market_id:
        return None, "missing_market_id"
    edge = model_probability - market_probability
    return {
        "generated_at": generated_at,
        "market_id": market_id,
        "slug": research_row.get("slug") or (market_row or {}).get("slug"),
        "question": research_row.get("question") or research_row.get("title") or (market_row or {}).get("question") or (market_row or {}).get("title"),
        "model_probability": round(model_probability, 6),
        "market_probability": round(market_probability, 6),
        "edge": round(edge, 6),
        "model_source": source,
        "model_confidence": confidence,
        "model_reason": reason,
        "evidence_refs": research_row.get("evidence_refs") or research_row.get("evidence") or [],
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


def _read_price_rows(reader: Any, limit: int) -> list[dict[str, Any]]:
    method = getattr(reader, "get_pm_prices", None)
    if not callable(method):
        return []
    try:
        rows = method(limit=limit)
    except TypeError:
        try:
            rows = method(None, limit=limit)
        except TypeError:
            try:
                rows = method(None)
            except TypeError:
                rows = []
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _read_marketgraph_research_with_meta(client: Any, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = client.get_pm_research_probabilities(limit=limit)
    meta: dict[str, Any] = {}
    if isinstance(rows, dict):
        meta = {
            key: rows.get(key)
            for key in (
                "source",
                "storage",
                "row_count",
                "degraded",
                "degrade_reason",
                "skip_reasons",
                "readiness",
                "response_safety",
            )
            if key in rows
        }
        nested = rows.get("rows") or rows.get("data") or []
        if isinstance(nested, dict):
            nested = nested.get("rows") or nested.get("data") or []
        rows = nested
    return [dict(row) for row in rows or [] if isinstance(row, dict)], meta


def _market_lookup(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in markets:
        for key in _row_keys(row):
            lookup.setdefault(key, row)
    return lookup


def _merge_market_prices(markets: list[dict[str, Any]], price_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not price_rows:
        return markets
    by_key = _market_lookup(markets)
    merged_by_id: dict[int, dict[str, Any]] = {id(row): dict(row) for row in markets}
    extra_rows: list[dict[str, Any]] = []
    for price_row in price_rows:
        matched = next((by_key[key] for key in _row_keys(price_row) if key in by_key), None)
        if matched is None:
            extra_rows.append(dict(price_row))
            continue
        merged = merged_by_id.setdefault(id(matched), dict(matched))
        if _first_probability(merged, MARKET_PROBABILITY_KEYS) is None:
            for key in ("price", "latest_price", "yes_price", "market_probability"):
                if key in price_row and price_row.get(key) not in (None, ""):
                    if merged.get(key) in (None, ""):
                        merged[key] = price_row.get(key)
            if merged.get("latest_price_time") in (None, ""):
                merged["latest_price_time"] = price_row.get("price_time") or price_row.get("latest_price_time")
            if merged.get("latest_token_id") in (None, ""):
                merged["latest_token_id"] = price_row.get("token_id") or price_row.get("latest_token_id")
    return list(merged_by_id.values()) + extra_rows


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
    marketgraph_client: Any | None = None,
    output_path: Path | str | None = None,
    summary_path: Path | str | None = None,
    limit: int = 100,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if reader is None:
        from shared.data.reader import TradingagentDataReader

        reader = TradingagentDataReader()
    if marketgraph_client is None:
        from shared.data.marketgraph_api import MarketGraphAPIClient

        marketgraph_client = MarketGraphAPIClient()
    generated = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    markets = _read_markets(reader, limit=limit)
    price_rows = _read_price_rows(reader, limit=limit)
    priced_markets = _merge_market_prices(markets, price_rows)
    research_rows, marketgraph_meta = _read_marketgraph_research_with_meta(marketgraph_client, limit=limit)
    market_by_key = _market_lookup(priced_markets)
    records: list[dict[str, Any]] = []
    skip_reasons: dict[str, int] = {}
    if not research_rows:
        skip_reasons["marketgraph_research_empty"] = 1
    for research_row in research_rows:
        market_row = next((market_by_key[key] for key in _row_keys(research_row) if key in market_by_key), None)
        record, reason = _build_probability_record(research_row, market_row, generated)
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
        "price_rows": len(price_rows),
        "marketgraph_rows": len(research_rows),
        "record_count": len(records),
        "skipped_count": sum(skip_reasons.values()),
        "skip_reasons": skip_reasons,
        "output_file": str(output),
        "model_policy": "marketgraph_research_probability_only",
        "reader_degraded": bool(getattr(reader, "degraded", False)),
        "reader_errors": list(getattr(reader, "errors", []) or [])[-5:],
        "marketgraph_degraded": bool(getattr(marketgraph_client, "degraded", False)),
        "marketgraph_errors": list(getattr(marketgraph_client, "errors", []) or [])[-5:],
        "marketgraph_research_meta": marketgraph_meta,
        "next_action": _next_action(skip_reasons, marketgraph_meta),
    }
    _write_json_atomic(summary, payload)
    return payload


def _next_action(skip_reasons: dict[str, int], marketgraph_meta: dict[str, Any]) -> str:
    if skip_reasons.get("marketgraph_research_empty"):
        readiness = marketgraph_meta.get("readiness") if isinstance(marketgraph_meta.get("readiness"), dict) else {}
        blocker = str((readiness or {}).get("primary_blocker") or marketgraph_meta.get("degrade_reason") or "").strip()
        if blocker:
            return f"repair_marketgraph_pm_research_probability: {blocker}"
        return "repair_marketgraph_pm_research_probability_samples"
    if skip_reasons.get("missing_market_probability"):
        return "check_sharedsignals_pm_market_price_snapshot"
    if skip_reasons.get("missing_marketgraph_research_probability"):
        return "check_marketgraph_pm_research_probability_fields"
    if not skip_reasons:
        return "pm_model_probability_ready"
    return "review_pm_model_probability_skip_reasons"


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
