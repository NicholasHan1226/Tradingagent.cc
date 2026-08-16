"""Adapters that mint event-catalyst calendar entries from frozen evidence.

This module is the only bridge into the event-catalyst shadow factor.  It
converts three already-validated input families into ``CatalystEntry`` values:

* ``EventEvidenceSnapshot`` rows from the TradingDatas ``disclosure_date``
  dataset (earnings disclosure appointments are hard-dated events),
* validated provider-native ``share_float`` rows (lockup expiries are
  hard-dated once announced), and
* caller-maintained calendar documents (policy meetings, conferences,
  product launches, index rebalances, macro releases),
  which stay plain data documents validated here field by field.

The adapter performs no network, no persistence and no scheduling.  It grants
no authority: minted entries remain inputs to a shadow-only research factor,
and every failure is fail-closed with a stable reason code.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

from Ashare.event_catalyst_shadow import (
    DATE_CONFIDENCE_LEVELS,
    EVENT_TYPES,
    IMPACT_DIRECTIONS,
    CatalystEntry,
    EventCatalystShadowError,
)
from Ashare.event_evidence import EventEvidenceSnapshot


EVENT_CATALYST_ADAPTER_CONTRACT = (
    "tradingagent.ashare.event_catalyst_adapter.v1"
)
DISCLOSURE_DATE_DATASET_ID = "cn.dataset.disclosure_date"
SHARE_FLOAT_DATASET_ID = "cn.dataset.share_float"

_LOCKUP_REQUIRED_FIELDS = (
    "ts_code",
    "ann_date",
    "float_date",
    "float_share",
    "float_ratio",
    "holder_name",
    "share_type",
)


class EventCatalystAdapterError(ValueError):
    """Fail-closed adapter failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _text(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise EventCatalystAdapterError(reason)
    return value


def _parse_event_date(value: object, reason: str) -> date:
    raw = _text(value, reason)
    try:
        parsed = date.fromisoformat(raw)
        return parsed
    except ValueError:
        pass
    try:
        parsed_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventCatalystAdapterError(reason) from exc
    return parsed_dt.date()


def catalyst_entry_from_disclosure_snapshot(
    snapshot: EventEvidenceSnapshot,
) -> CatalystEntry:
    """Mint one hard-dated earnings-disclosure entry from frozen evidence.

    Only the ``disclosure_date`` dataset carries appointment semantics; any
    other dataset, a missing symbol, or an unparseable event time fails
    closed.  The entry's ``source_ref`` binds back to the evidence receipt so
    downstream shadow observations stay replayable.
    """

    if not isinstance(snapshot, EventEvidenceSnapshot):
        raise EventCatalystAdapterError(
            "event_catalyst_adapter_snapshot_invalid"
        )
    if snapshot.dataset_id != DISCLOSURE_DATE_DATASET_ID:
        raise EventCatalystAdapterError(
            "event_catalyst_adapter_dataset_not_appointment"
        )
    if snapshot.symbol is None:
        raise EventCatalystAdapterError(
            "event_catalyst_adapter_symbol_missing"
        )
    scheduled = _parse_event_date(
        snapshot.event_time, "event_catalyst_adapter_event_time_invalid"
    )
    try:
        return CatalystEntry(
            event_id=f"disclosure:{snapshot.evidence_ref}",
            event_type="earnings_disclosure",
            scheduled_date=scheduled,
            date_confidence="hard_date",
            impact_direction="unclear",
            source_ref=snapshot.evidence_ref,
            entity=snapshot.entity,
            symbol=snapshot.symbol,
            # One disclosure snapshot is one underlying event; the cluster id
            # is explicit so journal-side grouping never parses event ids.
            event_cluster_id=f"disclosure:{snapshot.evidence_ref}",
        )
    except EventCatalystShadowError as exc:
        raise EventCatalystAdapterError(exc.reason_code) from exc


def catalyst_entries_from_calendar_document(
    document: Mapping[str, Any],
) -> tuple[CatalystEntry, ...]:
    """Validate one caller-maintained calendar document into entries.

    The document must be a mapping with ``calendar_id`` (text) and
    ``entries`` (a non-empty sequence of mappings with the CatalystEntry
    fields).  Unknown event types, confidence levels or impact directions,
    duplicate ``event_id`` values, and malformed dates all fail closed.
    """

    if not isinstance(document, Mapping):
        raise EventCatalystAdapterError("event_catalyst_calendar_doc_invalid")
    calendar_id = _text(
        document.get("calendar_id"), "event_catalyst_calendar_id_invalid"
    )
    raw_entries = document.get("entries")
    if (
        not isinstance(raw_entries, Sequence)
        or isinstance(raw_entries, (str, bytes))
        or not raw_entries
    ):
        raise EventCatalystAdapterError("event_catalyst_calendar_entries_invalid")
    entries: list[CatalystEntry] = []
    seen_ids: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise EventCatalystAdapterError(
                "event_catalyst_calendar_entry_invalid"
            )
        event_id = _text(
            raw.get("event_id"), "event_catalyst_calendar_entry_invalid"
        )
        if event_id in seen_ids:
            raise EventCatalystAdapterError(
                "event_catalyst_calendar_event_id_duplicate"
            )
        seen_ids.add(event_id)
        event_type = _text(
            raw.get("event_type"), "event_catalyst_calendar_entry_invalid"
        )
        if event_type not in EVENT_TYPES:
            raise EventCatalystAdapterError(
                "event_catalyst_calendar_event_type_invalid"
            )
        date_confidence = _text(
            raw.get("date_confidence"),
            "event_catalyst_calendar_entry_invalid",
        )
        if date_confidence not in DATE_CONFIDENCE_LEVELS:
            raise EventCatalystAdapterError(
                "event_catalyst_calendar_confidence_invalid"
            )
        impact_direction = _text(
            raw.get("impact_direction"),
            "event_catalyst_calendar_entry_invalid",
        )
        if impact_direction not in IMPACT_DIRECTIONS:
            raise EventCatalystAdapterError(
                "event_catalyst_calendar_direction_invalid"
            )
        source_ref = _text(
            raw.get("source_ref"), "event_catalyst_calendar_entry_invalid"
        )
        entity = raw.get("entity")
        if entity is not None:
            entity = _text(entity, "event_catalyst_calendar_entry_invalid")
        symbol = raw.get("symbol")
        if symbol is not None:
            symbol = _text(symbol, "event_catalyst_calendar_entry_invalid")
        scheduled = _parse_event_date(
            raw.get("scheduled_date"),
            "event_catalyst_calendar_scheduled_date_invalid",
        )
        try:
            entries.append(
                CatalystEntry(
                    event_id=f"{calendar_id}:{event_id}",
                    event_type=event_type,
                    scheduled_date=scheduled,
                    date_confidence=date_confidence,
                    impact_direction=impact_direction,
                    source_ref=source_ref,
                    entity=entity,
                    symbol=symbol,
                    event_cluster_id=f"{calendar_id}:{event_id}",
                )
            )
        except EventCatalystShadowError as exc:
            raise EventCatalystAdapterError(exc.reason_code) from exc
    return tuple(entries)


def catalyst_entry_from_lockup_row(
    row: Mapping[str, Any],
    *,
    dataset_id: str,
    receipt_id: str,
) -> CatalystEntry:
    """Mint one hard-dated lockup-expiry entry from a validated provider row.

    ``row`` must be one already-validated ``share_float`` provider-native row
    obtained by the caller through the frozen TradingDatas catalog/query
    boundary; this adapter performs no transport itself.  ``float_date`` is a
    fixed exchange-registered date once announced, so the entry is
    ``hard_date``; the direction is ``negative`` by convention (new sellable
    supply) and stays an explicit hypothesis, not a calibrated claim.  Rows
    with missing/blank required fields, malformed dates, non-positive share
    counts, or symbols outside the mainboard research scope fail closed.
    """

    if dataset_id != SHARE_FLOAT_DATASET_ID:
        raise EventCatalystAdapterError(
            "event_catalyst_adapter_lockup_dataset_invalid"
        )
    receipt = _text(receipt_id, "event_catalyst_adapter_lockup_receipt_invalid")
    if not isinstance(row, Mapping):
        raise EventCatalystAdapterError(
            "event_catalyst_adapter_lockup_row_invalid"
        )
    for field_name in _LOCKUP_REQUIRED_FIELDS:
        value = row.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise EventCatalystAdapterError(
                "event_catalyst_adapter_lockup_field_missing"
            )
    symbol = _text(
        row.get("ts_code"), "event_catalyst_adapter_lockup_row_invalid"
    )
    float_date = _parse_event_date(
        row.get("float_date"), "event_catalyst_adapter_lockup_float_date_invalid"
    )
    _parse_event_date(
        row.get("ann_date"), "event_catalyst_adapter_lockup_ann_date_invalid"
    )
    float_share = row.get("float_share")
    if (
        isinstance(float_share, bool)
        or not isinstance(float_share, (int, float))
        or float(float_share) <= 0
    ):
        raise EventCatalystAdapterError(
            "event_catalyst_adapter_lockup_share_invalid"
        )
    holder = _text(
        row.get("holder_name"), "event_catalyst_adapter_lockup_row_invalid"
    )
    share_type = _text(
        row.get("share_type"), "event_catalyst_adapter_lockup_row_invalid"
    )
    try:
        return CatalystEntry(
            event_id=(
                f"lockup:{dataset_id}:{receipt}:{symbol}:"
                f"{float_date.isoformat()}:{holder}:{share_type}"
            ),
            event_type="lockup_expiry",
            scheduled_date=float_date,
            date_confidence="hard_date",
            impact_direction="negative",
            source_ref=f"td-v1:{dataset_id}:{receipt}",
            entity=holder,
            symbol=symbol,
            # All holders unlocked on one float_date for one symbol share the
            # same underlying event cluster; receipts identify the row, not
            # the event.
            event_cluster_id=f"lockup:{dataset_id}:{symbol}:{float_date.isoformat()}",
        )
    except EventCatalystShadowError as exc:
        raise EventCatalystAdapterError(exc.reason_code) from exc
