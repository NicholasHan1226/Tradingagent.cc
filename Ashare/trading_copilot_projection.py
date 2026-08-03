#!/usr/bin/env python3
"""Publish verified, read-only A-share projections for TradingCopilot.

The publisher is downstream of TradingAgent's accepted TradingDatas
``GET /v1/catalog`` / ``POST /v1/query`` evidence.  It has no provider,
capital, order, broker, sample, training, or promotion authority.  Every
symbol is packaged with a detached receipt that binds the exact projection
bytes and all upstream receipt bindings.  Invalid batches fail before any
symbol is changed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterator, Mapping, Sequence


BATCH_INPUT_CONTRACT = "tradingagent.trading_copilot_projection_batch_input.v2"
BATCH_RECEIPT_CONTRACT = "tradingagent.trading_copilot_projection_batch_receipt.v1"
PROJECTION_RECEIPT_CONTRACT = "tradingagent.trading_copilot_stock_projection_receipt.v1"
VERIFIER_ID = "tradingagent.trading_copilot_projection_publisher"
VERIFIER_VERSION = "1"
FIXED_SOURCE_TRANSPORT = "tradingdatas_v1_catalog_query"
FIXED_RANGES = ("1D", "5D", "1M", "6M", "YTD", "1Y")
_SYMBOL = re.compile(r"^(?:0|3|6)\d{5}\.(?:SZ|SH)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOCK_NAME = ".projection-publish.lock"


class TradingCopilotProjectionError(ValueError):
    """Fail-closed projection validation or publication error."""


def _canonical_bytes(value: object, *, pretty: bool = True) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TradingCopilotProjectionError("projection_json_not_canonical") from exc


def _sha(value: bytes | object) -> str:
    payload = value if isinstance(value, bytes) else _canonical_bytes(value, pretty=False)
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TradingCopilotProjectionError(reason)
    return value


def _sequence(value: object, reason: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise TradingCopilotProjectionError(reason)
    return value


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise TradingCopilotProjectionError(reason)
    return value


def _optional_text(value: object, reason: str) -> str | None:
    if value is None:
        return None
    return _text(value, reason)


def _timestamp(value: object, reason: str) -> str:
    raw = _text(value, reason)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradingCopilotProjectionError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TradingCopilotProjectionError(reason)
    return raw


def _number(
    value: object,
    reason: str,
    *,
    positive: bool = False,
    nullable: bool = False,
    signed: bool = False,
) -> float | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise TradingCopilotProjectionError(reason)
    normalized = float(value)
    if (positive and normalized <= 0) or (not positive and not signed and normalized < 0):
        raise TradingCopilotProjectionError(reason)
    return normalized


def _sha_text(value: object, reason: str) -> str:
    raw = _text(value, reason)
    if not _SHA256.fullmatch(raw):
        raise TradingCopilotProjectionError(reason)
    return raw


def _load_batch(path: Path) -> Mapping[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise TradingCopilotProjectionError("projection_input_path_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TradingCopilotProjectionError("projection_input_invalid") from exc
    return _mapping(value, "projection_input_invalid")


def _source(value: object) -> dict[str, Any]:
    source = _mapping(value, "projection_source_invalid")
    if source.get("transportContract") != FIXED_SOURCE_TRANSPORT:
        raise TradingCopilotProjectionError("projection_source_transport_invalid")
    freshness = _text(source.get("freshness"), "projection_source_freshness_invalid")
    if freshness not in {"fresh", "stale", "degraded"}:
        raise TradingCopilotProjectionError("projection_source_freshness_invalid")
    adjustment = _text(source.get("adjustment"), "projection_source_adjustment_invalid")
    if adjustment not in {"none", "forward", "backward", "unknown"}:
        raise TradingCopilotProjectionError("projection_source_adjustment_invalid")
    return {
        "transportContract": FIXED_SOURCE_TRANSPORT,
        "datasetId": _text(source.get("datasetId"), "projection_source_dataset_invalid"),
        "receiptId": _text(source.get("receiptId"), "projection_source_receipt_invalid"),
        "receiptSha256": _sha_text(source.get("receiptSha256"), "projection_source_receipt_sha_invalid"),
        "dataThrough": _timestamp(source.get("dataThrough"), "projection_source_time_invalid"),
        "retrievedAt": _timestamp(source.get("retrievedAt"), "projection_source_time_invalid"),
        "freshness": freshness,
        "adjustment": adjustment,
    }


def _market_rules(value: object) -> dict[str, Any]:
    rules = _mapping(value, "projection_market_rules_invalid")
    board = _text(rules.get("board"), "projection_board_invalid")
    st_status = _text(rules.get("stStatus"), "projection_st_status_invalid")
    trading_status = _text(rules.get("tradingStatus"), "projection_trading_status_invalid")
    session = _text(rules.get("session"), "projection_session_invalid")
    if board not in {"main", "gem", "star", "beijing", "unknown"}:
        raise TradingCopilotProjectionError("projection_board_invalid")
    if st_status not in {"normal", "st", "star_st", "unknown"}:
        raise TradingCopilotProjectionError("projection_st_status_invalid")
    if trading_status not in {"trading", "suspended", "unknown"}:
        raise TradingCopilotProjectionError("projection_trading_status_invalid")
    if session not in {"call_auction", "continuous", "midday_break", "closing_auction", "closed", "unknown"}:
        raise TradingCopilotProjectionError("projection_session_invalid")
    if rules.get("lotSize") != 100 or rules.get("tPlusOne") is not True:
        raise TradingCopilotProjectionError("projection_ashare_rules_invalid")
    price_limit = _number(rules.get("priceLimitPct"), "projection_price_limit_invalid", positive=True, nullable=True)
    adjusted = rules.get("corporateActionAdjusted")
    if adjusted not in {True, False, None}:
        raise TradingCopilotProjectionError("projection_adjustment_status_invalid")
    return {
        "board": board,
        "lotSize": 100,
        "tPlusOne": True,
        "priceLimitPct": price_limit,
        "stStatus": st_status,
        "tradingStatus": trading_status,
        "session": session,
        "corporateActionAdjusted": adjusted,
    }


def _quote(value: object) -> dict[str, Any]:
    quote = _mapping(value, "projection_quote_invalid")
    price = _number(quote.get("price"), "projection_price_invalid", positive=True)
    previous = _number(quote.get("previousClose"), "projection_previous_close_invalid", positive=True)
    opening = _number(quote.get("open"), "projection_open_invalid", positive=True)
    high = _number(quote.get("high"), "projection_high_invalid", positive=True)
    low = _number(quote.get("low"), "projection_low_invalid", positive=True)
    volume = _number(quote.get("volume"), "projection_volume_invalid")
    assert price is not None and previous is not None and opening is not None and high is not None and low is not None and volume is not None
    if high < max(price, opening, low) or low > min(price, opening, high):
        raise TradingCopilotProjectionError("projection_ohlc_invalid")
    change = price - previous
    return {
        "price": price,
        "previousClose": previous,
        "change": change,
        "changePct": change / previous * 100,
        "open": opening,
        "high": high,
        "low": low,
        "volume": volume,
        "turnoverRate": _number(quote.get("turnoverRate"), "projection_turnover_invalid", nullable=True),
        "peTtm": _number(quote.get("peTtm"), "projection_pe_invalid", nullable=True, signed=True),
        "marketCapCny": _number(quote.get("marketCapCny"), "projection_market_cap_invalid", nullable=True),
    }


def _company(value: object, symbol: str) -> tuple[dict[str, Any], dict[str, str]]:
    company = _mapping(value, "projection_company_invalid")
    exchange = _text(company.get("exchange"), "projection_exchange_invalid")
    if exchange not in {"SH", "SZ"} or not symbol.endswith(f".{exchange}"):
        raise TradingCopilotProjectionError("projection_exchange_invalid")
    listing = _text(company.get("listingDate"), "projection_listing_date_invalid")
    try:
        datetime.strptime(listing, "%Y-%m-%d")
    except ValueError as exc:
        raise TradingCopilotProjectionError("projection_listing_date_invalid") from exc
    source = _source(company.get("source"))
    normalized = {
        "exchange": exchange,
        "industry": _text(company.get("industry"), "projection_industry_invalid"),
        "area": _text(company.get("area"), "projection_area_invalid"),
        "listingDate": listing,
        "description": _text(company.get("description"), "projection_description_invalid"),
    }
    return normalized, {"receiptId": source["receiptId"], "receiptSha256": source["receiptSha256"]}


def _receipt(value: object) -> dict[str, str]:
    receipt = _mapping(value, "projection_source_receipt_invalid")
    return {
        "receiptId": _text(receipt.get("receiptId"), "projection_source_receipt_invalid"),
        "receiptSha256": _sha_text(receipt.get("receiptSha256"), "projection_source_receipt_sha_invalid"),
    }


def _point(value: object) -> dict[str, Any]:
    point = _mapping(value, "projection_series_point_invalid")
    result: dict[str, Any] = {
        "key": _text(point.get("key"), "projection_series_key_invalid"),
        "label": _text(point.get("label"), "projection_series_label_invalid"),
    }
    for field in ("price", "volume", "forecastMedian"):
        result[field] = _number(point.get(field), f"projection_series_{field}_invalid", nullable=True)
    for field in ("forecastNarrowEnvelope", "forecastWideEnvelope"):
        raw = point.get(field)
        if raw is None:
            result[field] = None
            continue
        values = _sequence(raw, f"projection_series_{field}_invalid")
        if len(values) != 2:
            raise TradingCopilotProjectionError(f"projection_series_{field}_invalid")
        lower = _number(values[0], f"projection_series_{field}_invalid")
        upper = _number(values[1], f"projection_series_{field}_invalid")
        assert lower is not None and upper is not None
        if lower > upper:
            raise TradingCopilotProjectionError(f"projection_series_{field}_invalid")
        result[field] = [lower, upper]
    return result


def _series(value: object) -> dict[str, list[dict[str, Any]]]:
    raw = _mapping(value, "projection_series_invalid")
    if set(raw) != set(FIXED_RANGES):
        raise TradingCopilotProjectionError("projection_series_ranges_invalid")
    result = {name: [_point(item) for item in _sequence(raw[name], "projection_series_invalid")] for name in FIXED_RANGES}
    if not result["1D"] or all(item["price"] is None for item in result["1D"]):
        raise TradingCopilotProjectionError("projection_intraday_series_required")
    return result


def _event(value: object, symbol: str) -> tuple[dict[str, Any], dict[str, str]]:
    event = _mapping(value, "projection_event_invalid")
    kind = _text(event.get("kind"), "projection_event_kind_invalid")
    if kind not in {"announcement", "news", "sentiment"}:
        raise TradingCopilotProjectionError("projection_event_kind_invalid")
    related = [_text(item, "projection_event_symbol_invalid").upper() for item in _sequence(event.get("relatedSymbols"), "projection_event_symbols_invalid")]
    if symbol not in related or len(related) != len(set(related)):
        raise TradingCopilotProjectionError("projection_event_symbol_binding_invalid")
    source_class = _text(event.get("sourceClass"), "projection_event_source_class_invalid")
    confidence = _text(event.get("sourceConfidence"), "projection_event_confidence_invalid")
    novelty = _text(event.get("novelty"), "projection_event_novelty_invalid")
    sentiment = _text(event.get("sentiment"), "projection_event_sentiment_invalid")
    direction = _text(event.get("impactDirection"), "projection_event_direction_invalid")
    horizon = _text(event.get("impactHorizon"), "projection_event_horizon_invalid")
    if source_class not in {"primary_disclosure", "professional_news", "aggregated_sentiment"} or confidence not in {"high", "medium", "low"} or novelty not in {"new", "updated", "repeated"} or sentiment not in {"positive", "neutral", "negative"} or direction not in {"positive", "neutral", "negative", "uncertain"} or horizon not in {"intraday", "short_term", "medium_term", "unknown"}:
        raise TradingCopilotProjectionError("projection_event_enum_invalid")
    receipt_id = _text(event.get("sourceReceiptId"), "projection_event_receipt_invalid")
    receipt_sha = _sha_text(event.get("sourceReceiptSha256"), "projection_event_receipt_sha_invalid")
    url = _text(event.get("url"), "projection_event_url_required")
    if not url.startswith(("https://", "http://")):
        raise TradingCopilotProjectionError("projection_event_url_invalid")
    content_sha = _sha_text(event.get("contentSha256"), "projection_event_content_sha_invalid")
    capability = _mapping(
        event.get("dataCapability"), "projection_event_capability_invalid"
    )
    if capability.get("transportContract") != FIXED_SOURCE_TRANSPORT:
        raise TradingCopilotProjectionError("projection_event_capability_transport_invalid")
    freshness = _text(
        capability.get("freshness"), "projection_event_capability_freshness_invalid"
    )
    if freshness != "fresh":
        raise TradingCopilotProjectionError("projection_event_capability_freshness_invalid")
    capability_receipt_id = _text(
        capability.get("receiptId"), "projection_event_capability_receipt_invalid"
    )
    capability_receipt_sha = _sha_text(
        capability.get("receiptSha256"), "projection_event_capability_receipt_sha_invalid"
    )
    if capability_receipt_id != receipt_id or capability_receipt_sha != receipt_sha:
        raise TradingCopilotProjectionError("projection_event_capability_receipt_binding_invalid")
    normalized_capability = {
        "inputContract": _text(
            capability.get("inputContract"), "projection_event_capability_input_contract_invalid"
        ),
        "transportContract": FIXED_SOURCE_TRANSPORT,
        "datasetId": _text(
            capability.get("datasetId"), "projection_event_capability_dataset_invalid"
        ),
        "catalogVersion": _text(
            capability.get("catalogVersion"), "projection_event_capability_catalog_invalid"
        ),
        "asOf": _timestamp(
            capability.get("asOf"), "projection_event_capability_as_of_invalid"
        ),
        "dataThrough": _timestamp(
            capability.get("dataThrough"), "projection_event_capability_data_through_invalid"
        ),
        "freshness": freshness,
        "receiptId": capability_receipt_id,
        "receiptSha256": capability_receipt_sha,
        "lineageSha256": _sha_text(
            capability.get("lineageSha256"), "projection_event_capability_lineage_invalid"
        ),
    }
    if normalized_capability["inputContract"] != BATCH_INPUT_CONTRACT:
        raise TradingCopilotProjectionError("projection_event_capability_input_contract_invalid")
    if (
        datetime.fromisoformat(normalized_capability["dataThrough"].replace("Z", "+00:00"))
        > datetime.fromisoformat(normalized_capability["asOf"].replace("Z", "+00:00"))
    ):
        raise TradingCopilotProjectionError("projection_event_capability_time_order_invalid")
    normalized = {
        "id": _text(event.get("id"), "projection_event_id_invalid"),
        "kind": kind,
        "title": _text(event.get("title"), "projection_event_title_invalid"),
        "summary": _text(event.get("summary"), "projection_event_summary_invalid"),
        "source": _text(event.get("source"), "projection_event_source_invalid"),
        "sourceClass": source_class,
        "sourceConfidence": confidence,
        "publishedAt": _timestamp(event.get("publishedAt"), "projection_event_published_invalid"),
        "retrievedAt": _timestamp(event.get("retrievedAt"), "projection_event_retrieved_invalid"),
        "revisedAt": _timestamp(event.get("revisedAt"), "projection_event_revised_invalid") if event.get("revisedAt") is not None else None,
        "novelty": novelty,
        "sentiment": sentiment,
        "sentimentConfidence": _number(event.get("sentimentConfidence"), "projection_event_sentiment_confidence_invalid", nullable=True),
        "impactDirection": direction,
        "impactHorizon": horizon,
        "relatedSymbols": related,
        "url": url,
        "sourceReceiptId": receipt_id,
        "sourceReceiptSha256": receipt_sha,
        "contentSha256": content_sha,
        "dataCapability": normalized_capability,
    }
    score = normalized["sentimentConfidence"]
    if score is not None and not 0 <= score <= 1:
        raise TradingCopilotProjectionError("projection_event_sentiment_confidence_invalid")
    return normalized, {"receiptId": receipt_id, "receiptSha256": receipt_sha}


def _evidence_items(value: object, *, direction: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _sequence(value, f"projection_{direction}_evidence_invalid"):
        row = _mapping(item, f"projection_{direction}_evidence_invalid")
        result.append({
            "title": _text(row.get("title"), f"projection_{direction}_title_invalid"),
            "detail": _text(row.get("detail"), f"projection_{direction}_detail_invalid"),
            "sourceRef": _text(row.get("sourceRef"), f"projection_{direction}_source_invalid"),
            "knownAt": _timestamp(row.get("knownAt"), f"projection_{direction}_known_at_invalid"),
        })
    return result


def _normalize_item(raw: object, generated_at: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    item = _mapping(raw, "projection_item_invalid")
    symbol = _text(item.get("symbol"), "projection_symbol_invalid").upper()
    if not _SYMBOL.fullmatch(symbol):
        raise TradingCopilotProjectionError("projection_symbol_invalid")
    source = _source(item.get("source"))
    rules = _market_rules(item.get("marketRules"))
    quote = _quote(item.get("quote"))
    company, company_receipt = _company(item.get("company"), symbol)
    series = _series(item.get("series"))
    events_and_receipts = [_event(value, symbol) for value in _sequence(item.get("events"), "projection_events_invalid")]
    events = [value[0] for value in events_and_receipts]
    support = _evidence_items(item.get("support"), direction="support")
    oppose = _evidence_items(item.get("oppose"), direction="oppose")
    if not support or not oppose:
        raise TradingCopilotProjectionError("projection_two_sided_evidence_required")
    buy_conditions = [_text(value, "projection_buy_condition_invalid") for value in _sequence(item.get("buyConditions"), "projection_buy_conditions_invalid")]
    invalidation = [_text(value, "projection_invalidation_invalid") for value in _sequence(item.get("invalidation"), "projection_invalidation_invalid")]
    if not buy_conditions or not invalidation:
        raise TradingCopilotProjectionError("projection_decision_conditions_required")
    source_refs = sorted({entry["sourceRef"] for entry in (*support, *oppose)})
    completeness = min(100, 45 + min(20, len(series["1D"])) + min(15, len(source_refs) * 3) + min(20, len(events) * 5))
    actionable = source["freshness"] == "fresh" and rules["tradingStatus"] == "trading" and rules["priceLimitPct"] is not None and rules["stStatus"] != "unknown" and source["adjustment"] != "unknown"
    verdict = "积极观察" if actionable and len(support) >= len(oppose) + 2 else "暂不参与" if len(oppose) >= len(support) + 2 else "等待条件"
    analysis = {
        "symbol": symbol,
        "name": _text(item.get("name"), "projection_name_invalid"),
        "mode": "tradingagent_observation",
        "generatedAt": generated_at,
        "evidenceStrength": {
            "value": completeness,
            "label": f"已验证证据完整度 {completeness}/100（不是买入概率）",
            "semantics": "typed_evidence_strength_v1",
            "contractVersion": "v1",
            "sourceRefs": source_refs,
            "asOf": generated_at,
        },
        "readiness": {
            "data": "verified",
            "evidence": "typed",
            "model": "not_applicable",
            "action": "eligible_for_human_review" if actionable else "observe_only",
            "reasons": ["正式行情与双向证据已绑定独立回执"] if actionable else ["正式证据可读，但交易状态、复权或规则字段尚未全部满足人工计划门禁"],
        },
        "verdict": verdict,
        "summary": _text(item.get("summary"), "projection_summary_invalid"),
        "support": support,
        "oppose": oppose,
        "buyConditions": buy_conditions,
        "invalidation": invalidation,
    }
    projection = {
        "symbol": symbol,
        "name": analysis["name"],
        "mode": "tradingagent_observation",
        "updatedAt": generated_at,
        "analysis": analysis,
        "source": source,
        "marketRules": rules,
        "quote": quote,
        "company": company,
        "series": series,
        "forecast": None,
        "events": events,
    }
    receipts = [
        {"receiptId": source["receiptId"], "receiptSha256": source["receiptSha256"]},
        company_receipt,
        *(_receipt(value) for value in _sequence(item.get("sourceReceipts", []), "projection_source_receipts_invalid")),
        *(value[1] for value in events_and_receipts),
    ]
    unique = {(entry["receiptId"], entry["receiptSha256"]): entry for entry in receipts}
    return projection, [unique[key] for key in sorted(unique)]


def _validate_root(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise TradingCopilotProjectionError("projection_output_root_invalid")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise TradingCopilotProjectionError("projection_output_root_invalid")
    return path


@contextmanager
def _publish_lock(root: Path) -> Iterator[None]:
    path = root / _LOCK_NAME
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise TradingCopilotProjectionError("projection_publish_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def publish_projection_batch(*, input_path: Path | str, output_root: Path | str, now: datetime | None = None) -> dict[str, Any]:
    """Validate the complete batch, then publish projection + receipt pairs."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise TradingCopilotProjectionError("real_trading_must_remain_disabled")
    batch = _load_batch(Path(input_path))
    if batch.get("contractId") != BATCH_INPUT_CONTRACT:
        raise TradingCopilotProjectionError("projection_batch_contract_invalid")
    generated_at = _timestamp(batch.get("generatedAt"), "projection_batch_generated_at_invalid")
    valid_until = _timestamp(batch.get("validUntil"), "projection_batch_valid_until_invalid")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise TradingCopilotProjectionError("projection_clock_timezone_required")
    if datetime.fromisoformat(generated_at.replace("Z", "+00:00")) > clock or datetime.fromisoformat(valid_until.replace("Z", "+00:00")) <= clock:
        raise TradingCopilotProjectionError("projection_batch_time_invalid")
    normalized: list[tuple[dict[str, Any], list[dict[str, str]]]] = [_normalize_item(item, generated_at) for item in _sequence(batch.get("items"), "projection_batch_items_invalid")]
    if not normalized:
        raise TradingCopilotProjectionError("projection_batch_empty")
    symbols = [item[0]["symbol"] for item in normalized]
    if len(symbols) != len(set(symbols)):
        raise TradingCopilotProjectionError("projection_batch_symbol_duplicate")
    prepared: list[tuple[str, bytes, bytes, str]] = []
    for projection, source_receipts in normalized:
        projection_bytes = _canonical_bytes(projection)
        projection_sha = _sha(projection_bytes)
        receipt_id = f"tcopilot:{projection['symbol']}:{projection_sha[:24]}"
        receipt = {
            "contractId": PROJECTION_RECEIPT_CONTRACT,
            "symbol": projection["symbol"],
            "receiptId": receipt_id,
            "projectionSha256": projection_sha,
            "generatedAt": generated_at,
            "validUntil": valid_until,
            "verifierId": VERIFIER_ID,
            "verifierVersion": VERIFIER_VERSION,
            "sourceReceipts": source_receipts,
        }
        prepared.append((projection["symbol"], projection_bytes, _canonical_bytes(receipt), projection_sha))
    root = _validate_root(Path(output_root))
    with _publish_lock(root):
        for symbol, projection_bytes, receipt_bytes, _ in prepared:
            _atomic_write(root / f"{symbol}.json", projection_bytes)
            _atomic_write(root / f"{symbol}.receipt.json", receipt_bytes)
        batch_receipt = {
            "contractId": BATCH_RECEIPT_CONTRACT,
            "generatedAt": generated_at,
            "validUntil": valid_until,
            "symbolCount": len(prepared),
            "symbols": [entry[0] for entry in prepared],
            "projectionSha256": {entry[0]: entry[3] for entry in prepared},
            "inputSha256": _sha(Path(input_path).read_bytes()),
            "authority": {
                "capital": False,
                "orders": False,
                "broker": False,
                "training": False,
                "promotion": False,
                "realTradingEnabled": False,
            },
        }
        _atomic_write(root / "batch-receipt.json", _canonical_bytes(batch_receipt))
    return {"status": "pass", **batch_receipt, "outputRoot": str(root)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        result = publish_projection_batch(input_path=arguments.input.resolve(), output_root=arguments.output_root.resolve())
    except TradingCopilotProjectionError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
