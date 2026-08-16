"""Bridge event-catalyst shadow batches into the A-share SampleJournal.

The SampleJournal (``shared/review/ashare/sample_journal.jsonl``) is the only
evolution fact source.  This bridge converts labeled
``CatalystShadowObservation`` values into ``shadow_research`` journal samples
so the automatic promotion gate consumes journaled facts rather than ad-hoc
files.

Rules:

* only ``post_label_state == "labeled"`` observations become journal facts;
  pending observations stay in the batch artifact until the post window is
  observable — the journal is append-only, so a pending row can never be
  "updated" later;
* records carry no capital authority, execution lineage, or candidate fields;
  per shared/review policy they are therefore excluded from trading-layer
  KPIs and counted only in the separate ``shadow_research`` layer;
* the journal id is derived from the immutable observation receipt, so
  re-appending the same batch is idempotent and any content drift raises
  ``JournalConflictError`` instead of duplicating;
* the journal itself enforces sim-only markers; this bridge adds nothing
  live and performs no network or scheduling work.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from Ashare.event_catalyst_shadow import (
    CatalystShadowBatch,
    EventCatalystShadowError,
)


EVENT_CATALYST_JOURNAL_CONTRACT = (
    "tradingagent.ashare.event_catalyst_journal.v1"
)


class EventCatalystJournalError(ValueError):
    """Fail-closed bridge failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _event_cluster_id(event_id: str, symbol: str | None) -> str:
    """Cluster key grouping every symbol row of one underlying event.

    Adapters mint ``{calendar}:{entry}`` ids shared across symbols, while
    research runs often use ``{event}:{symbol}``; stripping a trailing
    ``:{symbol}`` normalizes both to the underlying event.
    """

    if symbol and event_id.endswith(f":{symbol}"):
        return event_id[: -len(symbol) - 1]
    return event_id


def journal_records_from_shadow_batch(
    batch: CatalystShadowBatch,
) -> tuple[dict[str, Any], ...]:
    """Convert one shadow batch into journal-ready shadow_research samples.

    The batch must carry the v2 shadow contract; unlabeled or
    insufficient-history observations are skipped (never journaled as facts).
    """

    if not isinstance(batch, CatalystShadowBatch):
        raise EventCatalystJournalError("event_catalyst_journal_batch_invalid")
    records: list[dict[str, Any]] = []
    for observation in batch.observations:
        if observation.post_label_state != "labeled":
            continue
        if observation.observation_status != "observed":
            continue
        if observation.post_return is None or observation.pre_return is None:
            raise EventCatalystJournalError(
                "event_catalyst_journal_label_payload_invalid"
            )
        records.append(
            {
                "record_type": "shadow_research",
                "sample_layers": ["shadow_research"],
                "sample_intent": "shadow",
                "market": "CN-A",
                "research_contract": batch.contract,
                "bridge_contract": EVENT_CATALYST_JOURNAL_CONTRACT,
                "journal_event_id": (
                    f"catalyst:{observation.observation_sha256[:32]}"
                ),
                "event_id": observation.event_id,
                "event_cluster_id": _event_cluster_id(
                    observation.event_id, observation.symbol
                ),
                "event_type": observation.event_type,
                "symbol": observation.symbol,
                "entity": observation.entity,
                "scheduled_date": observation.scheduled_date.isoformat(),
                "date_confidence": observation.date_confidence,
                "anticipation_class": observation.anticipation_class,
                "anticipation_intensity": observation.anticipation_intensity,
                "positioning_hypothesis": observation.positioning_hypothesis,
                "pre_window_sessions": observation.pre_window_sessions,
                "post_window_sessions": observation.post_window_sessions,
                "pre_return": float(observation.pre_return),
                "post_return": float(observation.post_return),
                "observation_sha256": observation.observation_sha256,
                "input_receipt_sha256": observation.input_receipt_sha256,
                "batch_receipt_sha256": batch.batch_receipt_sha256,
                "as_of": batch.as_of.isoformat(),
                "evidence_available_at": batch.as_of.isoformat(),
            }
        )
    return tuple(records)


def append_shadow_batch_to_journal(
    journal: Any,
    batch: CatalystShadowBatch,
) -> list[dict[str, Any]]:
    """Append one batch's labeled observations to an injected SampleJournal.

    The caller owns the journal instance (path, locking, lifecycle); this
    bridge only maps and delegates to ``journal.append_samples`` so batch
    atomicity and idempotency stay with the journal contract.
    """

    records = journal_records_from_shadow_batch(batch)
    if not records:
        return []
    append = getattr(journal, "append_samples", None)
    if append is None or not callable(append):
        raise EventCatalystJournalError(
            "event_catalyst_journal_journal_invalid"
        )
    return append(records)
