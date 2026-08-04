"""Publish receipt-bound, read-only TradingCopilot event timelines.

This runner is deliberately independent from minute bars, company facts,
forecasts, candidates, capital and orders.  A blocked source remains visible as
coverage debt; it never suppresses events from another accepted source.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from Ashare.event_evidence import (
    EventEvidenceSnapshot,
    load_event_evidence_batch_artifact,
)
from Ashare.trading_copilot_event_consumer_profile import (
    TradingCopilotEventConsumerProfileError,
    load_event_consumer_profiles,
)
from Ashare.trading_copilot_observation_worker import (
    TradingCopilotObservationError,
    _aware,
    _event_for_projection,
)


EVENT_TIMELINE_CONTRACT = "tradingagent.trading_copilot_event_timeline.v1"
EVENT_TIMELINE_RECEIPT_CONTRACT = "tradingagent.trading_copilot_event_timeline_receipt.v1"
_SYMBOL = re.compile(r"^(?:0|3|6)\d{5}\.(?:SZ|SH)$")


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def build_event_timeline_batch(
    *,
    symbols: Sequence[str],
    events: Sequence[EventEvidenceSnapshot],
    blocked_dataset_reasons: Mapping[str, str],
    generated_at: datetime,
    valid_until: datetime,
) -> dict[str, Any]:
    """Create a source-faithful event-only batch for a reviewed symbol list."""

    generated_at = _aware(generated_at, "event_timeline_generated_at_timezone_required")
    valid_until = _aware(valid_until, "event_timeline_valid_until_timezone_required")
    if valid_until <= generated_at:
        raise TradingCopilotObservationError("event_timeline_validity_invalid")
    normalized_symbols = tuple(sorted(set(symbol.upper() for symbol in symbols)))
    if not normalized_symbols or any(not _SYMBOL.fullmatch(symbol) for symbol in normalized_symbols):
        raise TradingCopilotObservationError("event_timeline_symbols_invalid")
    blocked = {dataset_id: str(reason) for dataset_id, reason in blocked_dataset_reasons.items()}
    try:
        declared_dataset_ids = {
            profile.dataset_id for profile in load_event_consumer_profiles()
        }
    except TradingCopilotEventConsumerProfileError as exc:
        raise TradingCopilotObservationError(
            "event_timeline_consumer_profile_invalid"
        ) from exc
    if set(blocked).difference(declared_dataset_ids) or any(not reason for reason in blocked.values()):
        raise TradingCopilotObservationError("event_timeline_coverage_invalid")
    by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in normalized_symbols}
    for event in events:
        if event.symbol not in by_symbol:
            continue
        projected = _event_for_projection(event, generated_at)
        if projected is not None:
            by_symbol[event.symbol].append(projected)
    items: list[dict[str, Any]] = []
    for symbol in normalized_symbols:
        projected = sorted(by_symbol[symbol], key=lambda item: item["retrievedAt"], reverse=True)
        accepted_ids = sorted({item["sourceReceiptId"] for item in projected})
        items.append({
            "contractId": EVENT_TIMELINE_CONTRACT,
            "symbol": symbol,
            "generatedAt": generated_at.isoformat(),
            "validUntil": valid_until.isoformat(),
            "events": projected,
            "coverage": {
                "acceptedEventCount": len(projected),
                "acceptedReceiptIds": accepted_ids,
                "blockedDatasetIds": sorted(blocked),
                "blockedDatasetReasons": dict(sorted(blocked.items())),
                "sentimentLabelsInvented": False,
            },
        })
    return {"contractId": EVENT_TIMELINE_CONTRACT, "generatedAt": generated_at.isoformat(), "validUntil": valid_until.isoformat(), "items": items}


def publish_event_timeline_batch(*, batch: Mapping[str, Any], output_root: Path | str, now: datetime) -> dict[str, Any]:
    """Atomically publish each independently verified event timeline and receipt."""

    now = _aware(now, "event_timeline_publish_now_timezone_required")
    if batch.get("contractId") != EVENT_TIMELINE_CONTRACT or not isinstance(batch.get("items"), list):
        raise TradingCopilotObservationError("event_timeline_batch_invalid")
    root = Path(output_root)
    if not root.is_absolute() or root.is_symlink():
        raise TradingCopilotObservationError("event_timeline_output_root_invalid")
    prepared: list[tuple[str, bytes, bytes]] = []
    for value in batch["items"]:
        if not isinstance(value, Mapping) or value.get("contractId") != EVENT_TIMELINE_CONTRACT:
            raise TradingCopilotObservationError("event_timeline_item_invalid")
        symbol = value.get("symbol")
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise TradingCopilotObservationError("event_timeline_symbol_invalid")
        valid_until = datetime.fromisoformat(str(value.get("validUntil", "")).replace("Z", "+00:00"))
        if valid_until.tzinfo is None or valid_until <= now:
            raise TradingCopilotObservationError("event_timeline_expired")
        payload = _canonical_bytes(value)
        receipts = sorted({(event["sourceReceiptId"], event["sourceReceiptSha256"]) for event in value.get("events", [])})
        receipt = {"contractId": EVENT_TIMELINE_RECEIPT_CONTRACT, "symbol": symbol, "timelineSha256": hashlib.sha256(payload).hexdigest(), "generatedAt": value["generatedAt"], "validUntil": value["validUntil"], "sourceReceipts": [{"receiptId": receipt_id, "receiptSha256": receipt_sha} for receipt_id, receipt_sha in receipts]}
        prepared.append((symbol, payload, _canonical_bytes(receipt)))
    for symbol, payload, receipt in prepared:
        _atomic_write(root / f"{symbol}.json", payload)
        _atomic_write(root / f"{symbol}.receipt.json", receipt)
    return {"contractId": EVENT_TIMELINE_CONTRACT, "symbolCount": len(prepared), "outputRoot": str(root), "sentimentLabelsInvented": False}


def publish_retained_event_timeline(
    *,
    artifact_paths: Sequence[Path | str],
    symbols: Sequence[str],
    blocked_dataset_reasons: Mapping[str, str],
    generated_at: datetime,
    valid_until: datetime,
    output_root: Path | str,
) -> dict[str, Any]:
    """Load only validated retained batches, then use the existing publisher."""

    paths = tuple(Path(path) for path in artifact_paths)
    if len(paths) != len(set(paths)) or any(
        not path.is_absolute() or path.is_symlink() for path in paths
    ):
        raise TradingCopilotObservationError("event_timeline_retained_artifact_invalid")
    events: list[EventEvidenceSnapshot] = []
    for path in paths:
        try:
            batch = load_event_evidence_batch_artifact(path)
        except ValueError as exc:
            raise TradingCopilotObservationError(
                "event_timeline_retained_artifact_invalid"
            ) from exc
        events.extend(batch.events)
    batch = build_event_timeline_batch(
        symbols=symbols,
        events=events,
        blocked_dataset_reasons=blocked_dataset_reasons,
        generated_at=generated_at,
        valid_until=valid_until,
    )
    return publish_event_timeline_batch(
        batch=batch,
        output_root=output_root,
        now=generated_at,
    )
