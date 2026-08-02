#!/usr/bin/env python3
"""Build TradingCopilot projections from accepted TradingDatas observations.

This is a one-shot, read-only adapter.  Minute bars are loaded through the
existing catalog/query canary; company facts and event snapshots must carry
their own TradingDatas receipt bindings.  The worker cannot create candidates,
reserve capital, write orders, call a broker, train a model, or promote one.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from Ashare.event_evidence import (
    AshareEvidenceAuditLedger,
    AshareEvidenceContractError,
    EventEvidenceSnapshot,
    PRIMARY_DATASET_IDS,
    TradingDatasAshareEvidencePort,
)
from Ashare.minute_canary import (
    load_minute_canary_config,
    load_minute_snapshot,
    load_reference_facts,
)
from Ashare.minute_data import MinuteBarEvidence, MinuteBarSnapshot, MinuteEvidenceUse
from Ashare.trading_copilot_projection import (
    BATCH_INPUT_CONTRACT,
    FIXED_SOURCE_TRANSPORT,
    TradingCopilotProjectionError,
    _source,
    publish_projection_batch,
)
from shared.runtime.ashare_runtime_ports import (
    AshareRuntimeAuthorityLoadBlocked,
    load_verified_ashare_runtime_authority_bundle,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import load_probe_manifest
from shared.data.sharedsignals_v1 import (
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_transport import build_runtime_transport


COMPANY_FACTS_CONTRACT = "tradingagent.trading_copilot_company_facts.v1"
EVENT_BUNDLE_CONTRACT = "tradingagent.ashare_event_evidence_bundle.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class TradingCopilotObservationError(ValueError):
    """Stable fail-closed error for the projection observation adapter."""


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TradingCopilotObservationError(reason)
    return value


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise TradingCopilotObservationError(reason)
    return value


def _aware(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TradingCopilotObservationError(reason)
    return value


def _load_json(path: Path, reason: str) -> Mapping[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise TradingCopilotObservationError(reason)
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), reason)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TradingCopilotObservationError(reason) from exc


def load_company_facts(path: Path | str) -> dict[str, dict[str, Any]]:
    raw = _load_json(Path(path), "copilot_company_facts_invalid")
    if raw.get("contractId") != COMPANY_FACTS_CONTRACT:
        raise TradingCopilotObservationError("copilot_company_facts_contract_invalid")
    source = _source(raw.get("source"))
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        raise TradingCopilotObservationError("copilot_company_facts_empty")
    result: dict[str, dict[str, Any]] = {}
    for item_value in items:
        item = dict(_mapping(item_value, "copilot_company_fact_invalid"))
        symbol = _text(item.get("symbol"), "copilot_company_symbol_invalid").upper()
        if symbol in result:
            raise TradingCopilotObservationError("copilot_company_symbol_duplicate")
        item["source"] = source
        result[symbol] = item
    return result


def company_facts_from_verified_observation(
    *,
    manifest_path: Path | str,
    state_root: Path | str,
) -> dict[str, dict[str, Any]]:
    """Read security-master rows only through a committed observation bundle."""

    manifest = load_probe_manifest(Path(manifest_path))
    schema_majors = {item.schema_major for item in manifest.datasets}
    if len(schema_majors) != 1:
        raise TradingCopilotObservationError("copilot_observation_schema_major_mismatch")
    try:
        bundle = load_verified_ashare_runtime_authority_bundle(
            state_root=Path(state_root),
            profile_id=manifest.profile_id,
            catalog_version=manifest.catalog_version,
            decision_as_of=manifest.as_of,
            manifest_as_of=manifest.as_of,
            manifest_sha256=manifest.manifest_sha256,
            schema_major=next(iter(schema_majors)),
        )
    except AshareRuntimeAuthorityLoadBlocked as exc:
        raise TradingCopilotObservationError(
            f"copilot_observation_bundle_blocked:{exc}"
        ) from exc
    master_dataset_id = next(
        (item.dataset_id for item in manifest.datasets if item.probe_role == "security_master"),
        None,
    )
    master = next(
        (dataset for dataset in bundle.research_snapshot.datasets if dataset.dataset_id == master_dataset_id),
        None,
    )
    if (
        master is None
        or not master.eligible
        or not master.receipt_id
        or not master.source_proof_sha256
        or not master.data_through
        or not master.observed_at
    ):
        raise TradingCopilotObservationError("copilot_security_master_binding_invalid")
    source = {
        "transportContract": FIXED_SOURCE_TRANSPORT,
        "datasetId": master.dataset_id,
        "receiptId": master.receipt_id,
        "receiptSha256": master.source_proof_sha256,
        "dataThrough": master.data_through,
        "retrievedAt": master.observed_at,
        "freshness": "fresh",
        "adjustment": "none",
    }
    facts: dict[str, dict[str, Any]] = {}
    for row in master.decoded_rows():
        symbol = row.get("ts_code")
        name = row.get("name")
        listing = row.get("list_date")
        if not isinstance(symbol, str) or not isinstance(name, str) or not isinstance(listing, str):
            raise TradingCopilotObservationError("copilot_security_master_row_invalid")
        list_date = listing.replace("-", "")[:8]
        if len(list_date) != 8 or not list_date.isdigit():
            raise TradingCopilotObservationError("copilot_security_master_row_invalid")
        listed = datetime.strptime(list_date, "%Y%m%d").date().isoformat()
        normalized_name = name.strip()
        is_st = "ST" in normalized_name.upper()
        facts[symbol] = {
            "symbol": symbol,
            "name": normalized_name,
            "industry": "未交付",
            "area": "未交付",
            "listingDate": listed,
            "description": "当前已验收证券主数据仅交付代码、名称、上市状态与上市日期；行业、地区和公司简介未交付。",
            "source": source,
            "marketRules": {
                "board": "main",
                "priceLimitPct": 5 if is_st else 10,
                "stStatus": "st" if is_st else "normal",
            },
            "turnoverRate": None,
            "peTtm": None,
            "marketCapCny": None,
        }
    return facts


def load_event_bundle(path: Path | str | None) -> tuple[EventEvidenceSnapshot, ...]:
    if path is None:
        return ()
    raw = _load_json(Path(path), "copilot_event_bundle_invalid")
    if raw.get("contractId") != EVENT_BUNDLE_CONTRACT:
        raise TradingCopilotObservationError("copilot_event_bundle_contract_invalid")
    values = raw.get("items")
    if not isinstance(values, list):
        raise TradingCopilotObservationError("copilot_event_bundle_items_invalid")
    result: list[EventEvidenceSnapshot] = []
    for value in values:
        item = dict(_mapping(value, "copilot_event_item_invalid"))
        try:
            item["available_at"] = datetime.fromisoformat(
                _text(item.get("available_at"), "copilot_event_available_at_invalid").replace("Z", "+00:00")
            )
            result.append(EventEvidenceSnapshot(**item))
        except (TypeError, ValueError) as exc:
            raise TradingCopilotObservationError("copilot_event_item_invalid") from exc
    return tuple(result)


def load_current_event_snapshots(
    *,
    minute_config: Any,
    token_file: Path | str,
    decision_time: datetime,
    symbols: Sequence[str],
) -> tuple[tuple[EventEvidenceSnapshot, ...], tuple[str, ...]]:
    """Read current event evidence through the same two fixed TD V1 routes.

    A dataset-level failure is returned as explicit coverage debt.  It never
    causes the worker to synthesize an event or sentiment label.
    """

    transport = build_runtime_transport(
        minute_config.transport_id,
        token_file=token_file,
        base_url=minute_config.base_url,
    )
    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=minute_config.base_url,
            expected_catalog_version=minute_config.expected_catalog_version,
            dataset_ids=frozenset(PRIMARY_DATASET_IDS),
            access_policy_id=minute_config.access_policy_id,
            catalog_version_policy="evidence_only",
            timeout_seconds=float(minute_config.timeout_seconds),
            max_limit=10_000,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )
    audit = AshareEvidenceAuditLedger()
    port = TradingDatasAshareEvidencePort(client)
    try:
        profiles = port.freeze_profiles(audit_ledger=audit)
    except AshareEvidenceContractError:
        return (), tuple(PRIMARY_DATASET_IDS)
    accepted: list[EventEvidenceSnapshot] = []
    blocked: list[str] = []
    allowed = tuple(sorted(set(symbols)))
    for dataset_id in PRIMARY_DATASET_IDS:
        profile = profiles.by_dataset.get(dataset_id)
        if profile is None:
            blocked.append(dataset_id)
            continue
        try:
            snapshot = port.load_event_snapshot(
                profile=profile,
                filters={},
                decision_time=decision_time,
                audit_ledger=audit,
                allowed_symbols=allowed,
            )
        except AshareEvidenceContractError:
            blocked.append(dataset_id)
            continue
        accepted.extend(snapshot.events)
    return tuple(accepted), tuple(blocked)


def _published_at(event: EventEvidenceSnapshot) -> str:
    if event.event_time_precision == "instant":
        return datetime.fromisoformat(event.event_time.replace("Z", "+00:00")).isoformat()
    raw = event.event_time
    parsed = datetime.strptime(raw, "%Y%m%d").date() if len(raw) == 8 else date.fromisoformat(raw)
    return datetime.combine(parsed, time.min, tzinfo=SHANGHAI).isoformat()


def _event_kind(event: EventEvidenceSnapshot) -> tuple[str, str]:
    if event.dataset_id == "cn.dataset.anns_d":
        return "announcement", "primary_disclosure"
    return "news", "professional_news"


def _event_for_projection(event: EventEvidenceSnapshot, generated_at: datetime) -> dict[str, Any] | None:
    if event.symbol is None or event.url is None:
        return None
    kind, source_class = _event_kind(event)
    confidence = "high" if event.evidence_confidence >= 0.8 else "medium" if event.evidence_confidence >= 0.55 else "low"
    age = generated_at - event.available_at.astimezone(generated_at.tzinfo)
    novelty = "new" if timedelta(0) <= age <= timedelta(hours=24) else "repeated"
    title = event.title or event.content
    summary = event.content or event.title
    assert title is not None and summary is not None
    return {
        "id": event.evidence_ref,
        "kind": kind,
        "title": title[:180],
        "summary": summary[:600],
        "source": event.source,
        "sourceClass": source_class,
        "sourceConfidence": confidence,
        "publishedAt": _published_at(event),
        "retrievedAt": event.available_at.isoformat(),
        "revisedAt": None,
        "novelty": novelty,
        "sentiment": "neutral",
        "sentimentConfidence": None,
        "impactDirection": "uncertain",
        "impactHorizon": "unknown",
        "relatedSymbols": [event.symbol],
        "url": event.url,
        "sourceReceiptId": event.receipt_id,
        "sourceReceiptSha256": event.envelope_proof_sha256,
        "contentSha256": event.source_row_sha256,
    }


def _session_at(value: datetime) -> str:
    local = value.astimezone(SHANGHAI)
    if local.weekday() >= 5 or local.time() >= time(15, 0) or local.time() < time(9, 15):
        return "closed"
    if local.time() < time(9, 30):
        return "call_auction"
    if time(11, 30) <= local.time() < time(13, 0):
        return "midday_break"
    if local.time() >= time(14, 57):
        return "closing_auction"
    return "continuous"


def _receipt_pairs(bars: Sequence[MinuteBarEvidence]) -> list[dict[str, str]]:
    pairs = {(bar.receipt_id, bar.envelope_proof_sha256) for bar in bars}
    return [
        {"receiptId": receipt_id, "receiptSha256": receipt_sha}
        for receipt_id, receipt_sha in sorted(pairs)
    ]


def build_projection_batch(
    *,
    snapshot: MinuteBarSnapshot,
    company_facts: Mapping[str, Mapping[str, Any]],
    events: Sequence[EventEvidenceSnapshot],
    generated_at: datetime,
    valid_until: datetime,
) -> dict[str, Any]:
    """Project accepted bars/events without granting model or trade authority."""

    generated_at = _aware(generated_at, "copilot_generated_at_timezone_required")
    valid_until = _aware(valid_until, "copilot_valid_until_timezone_required")
    if valid_until <= generated_at:
        raise TradingCopilotObservationError("copilot_projection_time_invalid")
    grouped: dict[str, list[MinuteBarEvidence]] = defaultdict(list)
    for bar in snapshot.bars:
        grouped[bar.symbol].append(bar)
    event_groups: dict[str, list[EventEvidenceSnapshot]] = defaultdict(list)
    for event in events:
        if event.symbol is not None:
            event_groups[event.symbol].append(event)
    items: list[dict[str, Any]] = []
    for symbol in sorted(grouped):
        company = company_facts.get(symbol)
        if company is None:
            raise TradingCopilotObservationError(f"copilot_company_fact_missing:{symbol}")
        bars = sorted(grouped[symbol], key=lambda bar: bar.bar_end)
        first, latest = bars[0], bars[-1]
        source = {
            "transportContract": FIXED_SOURCE_TRANSPORT,
            "datasetId": latest.dataset_id,
            "receiptId": latest.receipt_id,
            "receiptSha256": latest.envelope_proof_sha256,
            "dataThrough": max(bar.data_through for bar in bars).isoformat(),
            "retrievedAt": max(bar.available_at for bar in bars).isoformat(),
            "freshness": (
                "stale"
                if latest.evidence_use is MinuteEvidenceUse.HISTORICAL_DISPLAY
                else "fresh"
            ),
            "adjustment": "none",
        }
        rules = _mapping(company.get("marketRules"), "copilot_company_rules_invalid")
        price_change = latest.close_cny - latest.previous_close_cny
        direction_detail = (
            f"最新已验收五分钟收盘较前收高 {price_change:.2f} 元。"
            if price_change >= 0
            else f"最新已验收五分钟收盘较前收低 {abs(price_change):.2f} 元。"
        )
        projected_events = [
            projected
            for event in sorted(event_groups.get(symbol, []), key=lambda item: item.available_at, reverse=True)
            if (projected := _event_for_projection(event, generated_at)) is not None
        ]
        company_source = _source(company.get("source"))
        items.append({
            "symbol": symbol,
            "name": _text(company.get("name"), "copilot_company_name_invalid"),
            "source": source,
            "sourceReceipts": _receipt_pairs(bars),
            "marketRules": {
                "board": _text(rules.get("board"), "copilot_company_board_invalid"),
                "lotSize": 100,
                "tPlusOne": True,
                "priceLimitPct": rules.get("priceLimitPct"),
                "stStatus": _text(rules.get("stStatus"), "copilot_company_st_invalid"),
                "tradingStatus": "trading",
                "session": _session_at(generated_at),
                "corporateActionAdjusted": False,
            },
            "quote": {
                "price": latest.close_cny,
                "previousClose": latest.previous_close_cny,
                "open": first.open_cny,
                "high": max(bar.high_cny for bar in bars),
                "low": min(bar.low_cny for bar in bars),
                "volume": sum(bar.volume_shares for bar in bars),
                "turnoverRate": company.get("turnoverRate"),
                "peTtm": company.get("peTtm"),
                "marketCapCny": company.get("marketCapCny"),
            },
            "company": {
                "exchange": symbol[-2:],
                "industry": _text(company.get("industry"), "copilot_company_industry_invalid"),
                "area": _text(company.get("area"), "copilot_company_area_invalid"),
                "listingDate": _text(company.get("listingDate"), "copilot_company_listing_date_invalid"),
                "description": _text(company.get("description"), "copilot_company_description_invalid"),
                "source": company_source,
            },
            "series": {
                "1D": [{
                    "key": bar.bar_end.isoformat(),
                    "label": bar.bar_end.astimezone(SHANGHAI).strftime("%H:%M"),
                    "price": bar.close_cny,
                    "volume": bar.volume_shares,
                    "forecastMedian": None,
                    "forecastNarrowEnvelope": None,
                    "forecastWideEnvelope": None,
                } for bar in bars],
                "5D": [], "1M": [], "6M": [], "YTD": [], "1Y": [],
            },
            "events": projected_events,
            "summary": "正式行情、规则、证券主数据与可验证事件已投影；系统只提供人工计划条件复核。",
            "support": [{
                "title": "已验收价格事实",
                "detail": direction_detail,
                "sourceRef": f"td-v1:{latest.dataset_id}:{latest.receipt_id}:{latest.source_row_sha256[:16]}",
                "knownAt": latest.available_at.isoformat(),
            }],
            "oppose": [{
                "title": "方向证据不足",
                "detail": "单日五分钟量价与事件关联不能证明后续方向，且尚无通过样本外门禁的概率预测。",
                "sourceRef": f"td-v1:{latest.dataset_id}:{latest.receipt_id}:{latest.envelope_proof_sha256[:16]}",
                "knownAt": latest.available_at.isoformat(),
            }],
            "buyConditions": ["人工设定观察价后，使用更新后的正式量价再次确认，并复核现金、集中度与T+1约束"],
            "invalidation": ["行情或任一来源回执失效、数据转为陈旧/降级，或价格条件被破坏时停止采用该计划"],
        })
    return {
        "contractId": BATCH_INPUT_CONTRACT,
        "generatedAt": generated_at.isoformat(),
        "validUntil": valid_until.isoformat(),
        "items": items,
    }


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minute-manifest", type=Path, required=True)
    parser.add_argument("--reference-facts", type=Path, required=True)
    company_group = parser.add_mutually_exclusive_group(required=True)
    company_group.add_argument("--company-facts", type=Path)
    company_group.add_argument("--observation-manifest", type=Path)
    parser.add_argument("--observation-state-root", type=Path)
    event_group = parser.add_mutually_exclusive_group()
    event_group.add_argument("--event-bundle", type=Path)
    event_group.add_argument("--load-current-events", action="store_true")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--decision-time", required=True)
    parser.add_argument("--trading-date", required=True)
    parser.add_argument(
        "--evidence-use",
        choices=(
            MinuteEvidenceUse.DELAYED_PAPER.value,
            MinuteEvidenceUse.HISTORICAL_DISPLAY.value,
        ),
        default=MinuteEvidenceUse.DELAYED_PAPER.value,
    )
    parser.add_argument("--valid-until", required=True)
    parser.add_argument("--batch-output", type=Path, required=True)
    parser.add_argument("--projection-output-root", type=Path, required=True)
    parser.add_argument("--result-output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        decision_time = datetime.fromisoformat(arguments.decision_time.replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(arguments.valid_until.replace("Z", "+00:00"))
        config = load_minute_canary_config(arguments.minute_manifest.resolve())
        _, snapshot, _ = load_minute_snapshot(
            config,
            token_file=arguments.token_file.resolve(),
            decision_time=decision_time,
            trading_date=date.fromisoformat(arguments.trading_date),
            reference_facts=load_reference_facts(arguments.reference_facts.resolve()),
            evidence_use=MinuteEvidenceUse(arguments.evidence_use),
        )
        if arguments.observation_manifest:
            if not arguments.observation_state_root:
                raise TradingCopilotObservationError("copilot_observation_state_root_required")
            company_facts = company_facts_from_verified_observation(
                manifest_path=arguments.observation_manifest.resolve(),
                state_root=arguments.observation_state_root.resolve(),
            )
        else:
            if arguments.observation_state_root:
                raise TradingCopilotObservationError("copilot_observation_manifest_required")
            company_facts = load_company_facts(arguments.company_facts.resolve())
        if arguments.load_current_events:
            events, blocked_event_datasets = load_current_event_snapshots(
                minute_config=config,
                token_file=arguments.token_file.resolve(),
                decision_time=decision_time,
                symbols=tuple(bar.symbol for bar in snapshot.bars),
            )
        else:
            events = load_event_bundle(arguments.event_bundle.resolve() if arguments.event_bundle else None)
            blocked_event_datasets = ()
        batch = build_projection_batch(
            snapshot=snapshot,
            company_facts=company_facts,
            events=events,
            generated_at=decision_time,
            valid_until=valid_until,
        )
        batch_path = arguments.batch_output.resolve()
        _atomic_json(batch_path, batch)
        result = publish_projection_batch(
            input_path=batch_path,
            output_root=arguments.projection_output_root.resolve(),
        )
    except (
        TradingCopilotObservationError,
        TradingCopilotProjectionError,
        SharedSignalsV1Error,
        ValueError,
    ) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    result["eventCoverage"] = {
        "acceptedEventCount": len(events),
        "blockedDatasetIds": list(blocked_event_datasets),
        "sentimentLabelsInvented": False,
    }
    result["resultOutput"] = str(arguments.result_output.resolve())
    _atomic_json(arguments.result_output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
