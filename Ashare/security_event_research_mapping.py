"""Receipt-bound security-master and event mapping for A-share TA research.

The mapper consumes only already-frozen TradingDatas evidence.  It performs no
network or persistence work and cannot grant candidate, execution, training,
promotion, risk, position, order, or real-trading authority.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Mapping, Sequence

from Ashare.event_evidence import (
    EventEvidenceSnapshot,
    EventEvidenceSnapshotBatch,
    PRIMARY_DATASET_IDS,
)
from shared.data.research_snapshot import (
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)
from shared.universe.policy import classify_instrument


RESEARCH_MAPPING_CONTRACT = (
    "tradingagent.ashare.security_event_research_mapping.v1"
)
SECURITY_MASTER_DATASET_ID = "cn.equity.security_master"


class SecurityEventResearchMappingError(ValueError):
    """Fail-closed mapping error with a stable reason code."""


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_payload_invalid"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _text(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise SecurityEventResearchMappingError(reason)
    return value


def _sha256(value: object, reason: str) -> str:
    text = _text(value, reason)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SecurityEventResearchMappingError(reason)
    return text


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SecurityEventResearchMappingError(reason)
    return value


def _aware_iso(value: object, reason: str) -> datetime:
    raw = _text(value, reason)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SecurityEventResearchMappingError(reason) from exc
    return _aware(parsed, reason)


def _security_master(
    snapshot: ResearchDataSnapshot,
    *,
    decision_time: datetime,
) -> ResearchDatasetSnapshot:
    if (
        not isinstance(snapshot, ResearchDataSnapshot)
        or snapshot.execution_eligible is not True
        or snapshot.blocking_reasons
        or snapshot.historical_pit_eligible is not False
        or not snapshot.profile_id
        or not snapshot.catalog_version
    ):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_snapshot_not_eligible"
        )
    _sha256(
        snapshot.profile_contract_sha256,
        "ashare_research_mapping_profile_contract_invalid",
    )
    _sha256(snapshot.snapshot_sha256, "ashare_research_mapping_snapshot_hash_invalid")
    if _aware_iso(
        snapshot.decision_as_of,
        "ashare_research_mapping_snapshot_time_invalid",
    ) > decision_time:
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_snapshot_after_decision"
        )
    matches = tuple(
        dataset
        for dataset in snapshot.datasets
        if dataset.dataset_id == SECURITY_MASTER_DATASET_ID
    )
    if len(matches) != 1:
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_security_master_required"
        )
    master = matches[0]
    if (
        master.role != "required_execution"
        or master.catalog_version != snapshot.catalog_version
        or master.evidence_state != "ready"
        or master.evidence_action != "accept"
        or master.eligible is not True
        or master.source_proof_complete is not True
        or master.observation_mode != "current_observation"
        or master.historical_pit_eligible is not False
        or master.next_cursor is not None
        or not 1 <= master.page_count <= master.max_pages
        or not 1 <= master.row_count <= master.max_rows
    ):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_security_master_not_eligible"
        )
    _text(master.receipt_id, "ashare_research_mapping_security_receipt_invalid")
    for value, reason in (
        (master.lineage_sha256, "ashare_research_mapping_security_lineage_invalid"),
        (master.source_proof_sha256, "ashare_research_mapping_security_proof_invalid"),
        (master.identity_sha256, "ashare_research_mapping_security_identity_invalid"),
        (
            master.pagination_semantic_sha256,
            "ashare_research_mapping_security_pagination_invalid",
        ),
        (master.response_sha256, "ashare_research_mapping_security_response_invalid"),
    ):
        _sha256(value, reason)
    if (
        _aware_iso(
            master.data_through,
            "ashare_research_mapping_security_time_invalid",
        )
        > _aware_iso(
            master.observed_at,
            "ashare_research_mapping_security_time_invalid",
        )
        or _aware_iso(
            master.observed_at,
            "ashare_research_mapping_security_time_invalid",
        )
        > decision_time
    ):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_security_time_order_invalid"
        )
    expected_snapshot_sha256 = _canonical_sha256(
        {
            "profile_id": snapshot.profile_id,
            "profile_contract_sha256": snapshot.profile_contract_sha256,
            "catalog_version": snapshot.catalog_version,
            "decision_as_of": snapshot.decision_as_of,
            "datasets": [
                {
                    "dataset_id": dataset.dataset_id,
                    "role": dataset.role,
                    "response_sha256": dataset.response_sha256,
                }
                for dataset in snapshot.datasets
            ],
            "blocking_reasons": list(snapshot.blocking_reasons),
        }
    )
    if snapshot.snapshot_sha256 != expected_snapshot_sha256:
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_snapshot_hash_invalid"
        )
    return master


def _symbols(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_symbols_invalid"
        )
    normalized: list[str] = []
    for raw in values:
        symbol = _text(raw, "ashare_research_mapping_symbols_invalid").upper()
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != symbol
            or symbol in normalized
        ):
            raise SecurityEventResearchMappingError(
                "ashare_research_mapping_symbols_invalid"
            )
        normalized.append(symbol)
    if not normalized:
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_symbols_invalid"
        )
    return tuple(sorted(normalized))


def _master_rows(
    master: ResearchDatasetSnapshot,
    *,
    symbols: tuple[str, ...],
) -> dict[str, dict[str, str]]:
    rows = master.decoded_rows()
    if len(rows) != master.row_count:
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_security_row_count_invalid"
        )
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise SecurityEventResearchMappingError(
                "ashare_research_mapping_security_row_invalid"
            )
        symbol = _text(
            row.get("ts_code"), "ashare_research_mapping_security_row_invalid"
        ).upper()
        if symbol in indexed:
            raise SecurityEventResearchMappingError(
                "ashare_research_mapping_security_identity_duplicate"
            )
        name = _text(row.get("name"), "ashare_research_mapping_security_row_invalid")
        list_status = _text(
            row.get("list_status"), "ashare_research_mapping_security_row_invalid"
        )
        raw_date = _text(
            row.get("list_date"), "ashare_research_mapping_security_row_invalid"
        ).replace("-", "")[:8]
        if list_status != "L" or len(raw_date) != 8 or not raw_date.isdigit():
            raise SecurityEventResearchMappingError(
                "ashare_research_mapping_security_row_invalid"
            )
        indexed[symbol] = {
            "symbol": symbol,
            "name": name,
            "listStatus": list_status,
            "listingDate": datetime.strptime(raw_date, "%Y%m%d").date().isoformat(),
        }
    missing = set(symbols).difference(indexed)
    if missing:
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_security_symbol_missing"
        )
    return {symbol: indexed[symbol] for symbol in symbols}


def _event_payload(event: EventEvidenceSnapshot) -> dict[str, Any]:
    return {
        **event.canonical_payload(),
        "as_of": event.as_of.isoformat(),
        "data_through": event.data_through.isoformat(),
        "mapping_sha256": event.sha256,
    }


def build_security_event_research_mapping(
    *,
    research_snapshot: ResearchDataSnapshot,
    event_batches: Sequence[EventEvidenceSnapshotBatch],
    symbols: Sequence[str],
    blocked_dataset_reasons: Mapping[str, str],
    decision_time: datetime,
) -> dict[str, Any]:
    """Bind current security-master and event evidence into one TA research view."""

    decision_time = _aware(
        decision_time, "ashare_research_mapping_decision_time_invalid"
    )
    normalized_symbols = _symbols(symbols)
    master = _security_master(research_snapshot, decision_time=decision_time)
    companies = _master_rows(master, symbols=normalized_symbols)
    if not isinstance(blocked_dataset_reasons, Mapping):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_coverage_invalid"
        )
    blocked = {
        _text(dataset_id, "ashare_research_mapping_coverage_invalid"): _text(
            reason, "ashare_research_mapping_coverage_invalid"
        )
        for dataset_id, reason in blocked_dataset_reasons.items()
    }
    batches = tuple(event_batches)
    if any(not isinstance(batch, EventEvidenceSnapshotBatch) for batch in batches):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_event_batch_invalid"
        )
    by_dataset = {batch.profile.dataset_id: batch for batch in batches}
    if len(by_dataset) != len(batches) or set(by_dataset).intersection(blocked):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_coverage_invalid"
        )
    if set(by_dataset).union(blocked) != set(PRIMARY_DATASET_IDS):
        raise SecurityEventResearchMappingError(
            "ashare_research_mapping_coverage_invalid"
        )

    by_symbol: dict[str, list[dict[str, Any]]] = {
        symbol: [] for symbol in normalized_symbols
    }
    context_events: list[dict[str, Any]] = []
    source_coverage: list[dict[str, Any]] = []
    accepted_event_count = 0
    for dataset_id in sorted(by_dataset):
        batch = by_dataset[dataset_id]
        receipts: set[str] = set()
        lineages: set[str] = set()
        for event in batch.events:
            if event.as_of > decision_time or event.available_at > decision_time:
                raise SecurityEventResearchMappingError(
                    "ashare_research_mapping_event_after_decision"
                )
            if event.symbol is not None and event.symbol not in by_symbol:
                raise SecurityEventResearchMappingError(
                    "ashare_research_mapping_event_symbol_outside_universe"
                )
            payload = _event_payload(event)
            if event.symbol is None:
                context_events.append(payload)
            else:
                by_symbol[event.symbol].append(payload)
            receipts.add(event.receipt_id)
            lineages.add(event.source_lineage_sha256)
            accepted_event_count += 1
        profile = batch.profile
        source_coverage.append(
            {
                "datasetId": dataset_id,
                "catalogVersion": batch.events[0].catalog_version,
                "datasetContractFingerprint": profile.dataset_contract_fingerprint,
                "consumerProfileSha256": profile.consumer_profile_sha256,
                "receiptIds": sorted(receipts),
                "lineageSha256s": sorted(lineages),
                "rowCount": batch.row_count,
                "pageCount": batch.page_count,
                "maxPages": profile.max_pages,
                "maxRows": profile.max_rows,
                "paginationTraceSha256": batch.pagination_trace_sha256,
                "firstSemanticSha256": batch.first_semantic_sha256,
                "replaySemanticSha256": batch.replay_semantic_sha256,
                "sameObservation": batch.same_observation,
            }
        )

    security_source = {
        "datasetId": master.dataset_id,
        "catalogVersion": master.catalog_version,
        "profileId": research_snapshot.profile_id,
        "profileContractSha256": research_snapshot.profile_contract_sha256,
        "snapshotSha256": research_snapshot.snapshot_sha256,
        "receiptId": master.receipt_id,
        "receiptSha256": master.source_proof_sha256,
        "lineageSha256": master.lineage_sha256,
        "identitySha256": master.identity_sha256,
        "responseSha256": master.response_sha256,
        "dataThrough": master.data_through,
        "observedAt": master.observed_at,
        "rowCount": master.row_count,
        "pageCount": master.page_count,
        "maxPages": master.max_pages,
        "maxRows": master.max_rows,
        "paginationSemanticSha256": master.pagination_semantic_sha256,
        "queryAsOfMode": master.query_as_of_mode,
        "observationMode": master.observation_mode,
        "historicalPitEligible": master.historical_pit_eligible,
    }
    payload: dict[str, Any] = {
        "contractId": RESEARCH_MAPPING_CONTRACT,
        "generatedAt": decision_time.isoformat(),
        "observationMode": "current_observation",
        "historicalPitEligible": False,
        "securities": [
            {
                **companies[symbol],
                "securityMasterSource": security_source,
                "events": sorted(
                    by_symbol[symbol],
                    key=lambda event: (
                        event["available_at"],
                        event["dataset_id"],
                        event["evidence_ref"],
                    ),
                ),
            }
            for symbol in normalized_symbols
        ],
        "contextEvents": sorted(
            context_events,
            key=lambda event: (
                event["available_at"],
                event["dataset_id"],
                event["evidence_ref"],
            ),
        ),
        "sourceCoverage": source_coverage,
        "coverage": {
            "symbolCount": len(normalized_symbols),
            "acceptedEventCount": accepted_event_count,
            "acceptedDatasetIds": sorted(by_dataset),
            "blockedDatasetIds": sorted(blocked),
            "blockedDatasetReasons": dict(sorted(blocked.items())),
        },
        "candidateEligible": False,
        "executionEligible": False,
        "trainingEligible": False,
        "promotionEligible": False,
        "executionAuthority": False,
        "riskAuthority": False,
        "positionAuthority": False,
        "realTradingEnabled": False,
    }
    payload["mappingSha256"] = _canonical_sha256(payload)
    return payload


__all__ = [
    "RESEARCH_MAPPING_CONTRACT",
    "SECURITY_MASTER_DATASET_ID",
    "SecurityEventResearchMappingError",
    "build_security_event_research_mapping",
]
