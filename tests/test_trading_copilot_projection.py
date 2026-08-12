from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from Ashare.trading_copilot_projection import (
    TradingCopilotProjectionError,
    publish_projection_batch,
)


NOW = datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc)


def _authority(*, dataset_id: str, data_through: str, receipt_id: str, receipt_sha256: str, lineage_sha256: str) -> dict:
    return {
        "datasetId": dataset_id,
        "market": "ashare",
        "timezone": "Asia/Shanghai",
        "calendar": {
            "id": "sse", "version": "2026.08", "sourceDatasetId": "cn.market.trade_calendar",
            "receiptId": "calendar-receipt", "receiptSha256": hashlib.sha256(b"calendar").hexdigest(),
            "lineageSha256": hashlib.sha256(b"calendar-lineage").hexdigest(),
            "calendarSha256": hashlib.sha256(b"calendar-content").hexdigest(),
        },
        "session": {"state": "closed", "asOf": "2026-08-01T07:00:00+00:00"},
        "dataThrough": data_through,
        "source": {"receiptId": receipt_id, "receiptSha256": receipt_sha256, "lineageSha256": lineage_sha256},
    }


def _item(symbol: str = "000400.SZ") -> dict:
    stamp = "2026-08-01T07:00:00+00:00"
    sha = hashlib.sha256(symbol.encode()).hexdigest()
    source_lineage = hashlib.sha256(f"source-lineage-{symbol}".encode()).hexdigest()
    company_lineage = hashlib.sha256(f"company-lineage-{symbol}".encode()).hexdigest()
    event_lineage = hashlib.sha256(f"event-lineage-{symbol}".encode()).hexdigest()
    return {
        "symbol": symbol,
        "name": "许继电气" if symbol == "000400.SZ" else symbol,
        "source": {
            "transportContract": "tradingdatas_v1_catalog_query",
            "datasetId": "cn.dataset.rt_min",
            "receiptId": f"receipt-{symbol}",
            "receiptSha256": sha,
            "lineageSha256": source_lineage,
            "dataThrough": stamp,
            "retrievedAt": "2026-08-01T07:00:10+00:00",
            "freshness": "fresh",
            "adjustment": "none",
            "activityAuthority": _authority(dataset_id="cn.dataset.rt_min", data_through=stamp, receipt_id=f"receipt-{symbol}", receipt_sha256=sha, lineage_sha256=source_lineage),
        },
        "marketRules": {
            "board": "main", "lotSize": 100, "tPlusOne": True,
            "priceLimitPct": 10, "stStatus": "normal", "tradingStatus": "trading",
            "session": "closed", "corporateActionAdjusted": False,
        },
        "quote": {
            "price": 31.42, "previousClose": 30.76, "open": 30.84, "high": 31.8,
            "low": 30.39, "volume": 23_040_000, "turnoverRate": None,
            "peTtm": None, "marketCapCny": None,
        },
        "company": {
            "exchange": "SZ", "industry": "电网设备", "area": "河南",
            "listingDate": "1997-04-18", "description": "公司资料来自已验收证券主数据。",
            "source": {
                "transportContract": "tradingdatas_v1_catalog_query",
                "datasetId": "cn.equity.security_master",
                "receiptId": f"company-receipt-{symbol}", "receiptSha256": sha,
                "lineageSha256": company_lineage,
                "dataThrough": stamp, "retrievedAt": stamp,
                "freshness": "fresh", "adjustment": "none",
                "activityAuthority": _authority(dataset_id="cn.equity.security_master", data_through=stamp, receipt_id=f"company-receipt-{symbol}", receipt_sha256=sha, lineage_sha256=company_lineage),
            },
        },
        "series": {
            "1D": [
                {"key": "0950", "label": "09:50", "price": 30.9, "volume": 1_000_000, "forecastMedian": None, "forecastNarrowEnvelope": None, "forecastWideEnvelope": None},
                {"key": "1500", "label": "15:00", "price": 31.42, "volume": 2_000_000, "forecastMedian": None, "forecastNarrowEnvelope": None, "forecastWideEnvelope": None},
            ],
            "5D": [], "1M": [], "6M": [], "YTD": [], "1Y": [],
        },
        "events": [{
            "id": f"ann-{symbol}", "kind": "announcement", "title": "项目进展公告",
            "summary": "公告内容按股票代码和回执绑定。", "source": "交易所披露",
            "sourceClass": "primary_disclosure", "sourceConfidence": "high",
            "publishedAt": "2026-07-31T01:18:00+00:00", "retrievedAt": "2026-07-31T01:20:00+00:00",
            "revisedAt": None, "novelty": "new", "sentiment": "neutral",
            "sentimentConfidence": None, "impactDirection": "uncertain", "impactHorizon": "unknown",
            "relatedSymbols": [symbol], "url": "https://example.invalid/announcement",
            "sourceReceiptId": f"event-receipt-{symbol}", "sourceReceiptSha256": sha,
            "contentSha256": hashlib.sha256(f"event-{symbol}".encode()).hexdigest(),
            "dataCapability": {
                "inputContract": "tradingagent.trading_copilot_projection_batch_input.v2",
                "transportContract": "tradingdatas_v1_catalog_query",
                "datasetId": "cn.dataset.anns_d",
                "catalogVersion": "catalog-v1",
                "asOf": NOW.isoformat(),
                "dataThrough": stamp,
                "freshness": "fresh",
                "receiptId": f"event-receipt-{symbol}",
                "receiptSha256": sha,
                "lineageSha256": event_lineage,
                "activityAuthority": _authority(dataset_id="cn.dataset.anns_d", data_through=stamp, receipt_id=f"event-receipt-{symbol}", receipt_sha256=sha, lineage_sha256=event_lineage),
            },
        }],
        "summary": "正式行情与双向证据可读；仍需人工等待触发条件。",
        "support": [{"title": "趋势结构", "detail": "收盘高于前收。", "sourceRef": f"td:{symbol}:price", "knownAt": stamp}],
        "oppose": [{"title": "追涨风险", "detail": "价格接近日内高位。", "sourceRef": f"td:{symbol}:range", "knownAt": stamp}],
        "buyConditions": ["放量后仍守住观察位"],
        "invalidation": ["跌破前收且成交量放大"],
    }


def _batch(items: list[dict]) -> dict:
    return {
        "contractId": "tradingagent.trading_copilot_projection_batch_input.v2",
        "generatedAt": "2026-08-02T03:55:00+00:00",
        "validUntil": "2026-08-03T08:00:00+00:00",
        "items": items,
    }


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path.resolve()


def test_publishes_exact_projection_and_detached_receipt(tmp_path: Path) -> None:
    root = (tmp_path / "projection").resolve()
    result = publish_projection_batch(
        input_path=_write(tmp_path / "input.json", _batch([_item()])),
        output_root=root,
        now=NOW,
    )
    assert result["status"] == "pass"
    assert result["symbolCount"] == 1
    projection_bytes = (root / "000400.SZ.json").read_bytes()
    projection = json.loads(projection_bytes)
    receipt = json.loads((root / "000400.SZ.receipt.json").read_text())
    assert projection["mode"] == "tradingagent_observation"
    assert projection["analysis"]["evidenceStrength"]["semantics"] == "typed_evidence_strength_v1"
    assert projection["analysis"]["evidenceStrength"]["label"].endswith("（不是买入概率）")
    assert projection["forecast"] is None
    assert receipt["projectionSha256"] == hashlib.sha256(projection_bytes).hexdigest()
    assert {item["receiptId"] for item in receipt["sourceReceipts"]} == {
        "receipt-000400.SZ", "company-receipt-000400.SZ", "event-receipt-000400.SZ", "calendar-receipt"
    }
    assert json.loads((root / "batch-receipt.json").read_text())["authority"]["orders"] is False


def test_preserves_receipt_bound_facts_when_activity_authority_is_missing(tmp_path: Path) -> None:
    """Missing calendar/session authority degrades only session-sensitive fields."""

    item = _item()
    del item["source"]["activityAuthority"]
    result = publish_projection_batch(
        input_path=_write(tmp_path / "input.json", _batch([item])),
        output_root=(tmp_path / "projection").resolve(),
        now=NOW,
    )
    projection = json.loads((tmp_path / "projection" / "000400.SZ.json").read_text())
    assert result["status"] == "pass"
    assert projection["source"]["receiptId"] == "receipt-000400.SZ"
    assert projection["source"]["activityAuthority"] is None
    assert projection["source"]["activityAuthorityStatus"] == {
        "quality": "usable_degraded",
        "reason": "activity_authority_incomplete",
        "missingFields": [
            "calendar.id", "calendar.version", "calendar.receiptId",
            "calendar.receiptSha256", "calendar.lineageSha256",
            "calendar.calendarSha256", "session.state", "session.asOf",
        ],
    }
    assert projection["analysis"]["readiness"]["action"] == "observe_only"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda item: item["source"]["activityAuthority"].__setitem__(
                "dataThrough", "2026-08-01T07:01:00+00:00"
            ),
            "projection_activity_authority_data_through_mismatch",
        ),
        (
            lambda item: item["events"][0]["dataCapability"]["activityAuthority"]["source"].__setitem__(
                "receiptId", "different-receipt"
            ),
            "projection_activity_authority_source_mismatch",
        ),
    ],
)
def test_rejects_activity_authority_unequal_to_enclosing_binding(
    tmp_path: Path, mutate, reason: str
) -> None:
    item = _item()
    mutate(item)
    with pytest.raises(TradingCopilotProjectionError, match=reason):
        publish_projection_batch(
            input_path=_write(tmp_path / "input.json", _batch([item])),
            output_root=(tmp_path / "projection").resolve(),
            now=NOW,
        )


def test_validates_entire_batch_before_changing_any_symbol(tmp_path: Path) -> None:
    good = _item()
    bad = _item("600000.SH")
    bad["company"]["exchange"] = "SZ"
    root = (tmp_path / "projection").resolve()
    with pytest.raises(TradingCopilotProjectionError, match="projection_exchange_invalid"):
        publish_projection_batch(
            input_path=_write(tmp_path / "input.json", _batch([good, bad])),
            output_root=root,
            now=NOW,
        )
    assert not root.exists()


def test_requires_formal_event_provenance_and_two_sided_evidence(tmp_path: Path) -> None:
    value = _item()
    value["events"][0]["url"] = None
    value["oppose"] = []
    with pytest.raises(TradingCopilotProjectionError):
        publish_projection_batch(
            input_path=_write(tmp_path / "input.json", _batch([value])),
            output_root=(tmp_path / "projection").resolve(),
            now=NOW,
        )


def test_supports_current_verified_batch_without_granting_authority(tmp_path: Path) -> None:
    symbols = [f"{index:06d}.SZ" for index in range(1, 31)]
    result = publish_projection_batch(
        input_path=_write(tmp_path / "input.json", _batch([_item(symbol) for symbol in symbols])),
        output_root=(tmp_path / "projection").resolve(),
        now=NOW,
    )
    assert result["symbolCount"] == 30
    assert result["authority"] == {
        "capital": False, "orders": False, "broker": False, "training": False,
        "promotion": False, "realTradingEnabled": False,
    }
