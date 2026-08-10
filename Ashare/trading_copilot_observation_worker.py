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
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
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
    TradingDatasAshareEvidencePort,
    PRIMARY_DATASET_IDS,
    load_event_evidence_batch_artifact,
    write_event_evidence_batch_artifact,
)
from Ashare.minute_canary import (
    MinuteCanaryConfigurationError,
    load_minute_canary_config,
    load_minute_snapshot,
    load_reference_facts,
    snapshot_from_canary_receipt,
)
from Ashare.minute_auto_runner import PROVIDER_AVAILABILITY_LAG, session_bar_ends
from Ashare.minute_data import (
    MinuteBarEvidence,
    MinuteBarSnapshot,
    MinuteDataContractError,
    MinuteDatasetProfile,
    MinuteEvidenceUse,
    MinuteTimestampSemantics,
)
from Ashare.trading_copilot_projection import (
    BATCH_INPUT_CONTRACT,
    FIXED_SOURCE_TRANSPORT,
    TradingCopilotProjectionError,
    _activity_authority,
    _source,
    publish_projection_batch,
)
from Ashare.trading_copilot_event_consumer_profile import (
    TradingCopilotEventConsumerProfileError,
    load_event_consumer_profiles,
    select_event_consumer_profiles,
    validate_event_consumer_profile_contract,
    validate_event_consumer_runtime_evidence,
)
from shared.runtime.ashare_runtime_ports import (
    AshareRuntimeAuthorityLoadBlocked,
    load_verified_ashare_runtime_authority_bundle,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import load_probe_manifest
from shared.data.research_snapshot import ResearchDataSnapshot
from shared.data.research_snapshot_store import (
    FileResearchSnapshotStore,
    ResearchSnapshotStoreConflict,
    ResearchSnapshotStoreCorruption,
)
from shared.data.sharedsignals_v1 import (
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_transport import build_runtime_transport


COMPANY_FACTS_CONTRACT = "tradingagent.trading_copilot_company_facts.v1"
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
    source = dict(_mapping(raw.get("source"), "copilot_company_source_invalid"))
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


def load_activity_authorities(path: Path | str) -> dict[str, dict[str, Any]]:
    """Load only explicit per-dataset authorities; never derive one locally."""

    raw = _load_json(Path(path), "copilot_activity_authorities_invalid")
    result: dict[str, dict[str, Any]] = {}
    for dataset_id, authority in raw.items():
        canonical_dataset_id = _text(
            dataset_id, "copilot_activity_authorities_invalid"
        )
        if canonical_dataset_id in result:
            raise TradingCopilotObservationError("copilot_activity_authorities_invalid")
        result[canonical_dataset_id] = dict(
            _mapping(authority, "copilot_activity_authorities_invalid")
        )
    if not result:
        raise TradingCopilotObservationError("copilot_activity_authorities_invalid")
    return result


def _pinned_snapshot_plan(
    config: Any,
    reference_facts: Mapping[str, Any],
    decision_time: datetime,
) -> tuple[Any, datetime]:
    """Pin the exact universe and decide at the provider availability boundary.

    Mirrors the A-share delayed-paper runner (``minute_auto_runner``): the
    minute snapshot query is pinned to the reference-fact symbol set and one
    completed bar so it fits the manifest pagination budget, and the snapshot
    decision sits at the provider availability lag so the evidence time
    ordering (bar_end <= data_through <= observed <= available <= decision)
    holds.  The projection's generatedAt remains the caller decision time,
    which the bar selection guarantees is never earlier than the snapshot
    decision.
    """

    profile = _mapping(getattr(config, "profile", None), "copilot_minute_profile_invalid")
    timestamp_field = _text(
        profile.get("timestamp_field"), "copilot_minute_timestamp_field_invalid"
    )
    symbol_field = _text(profile.get("symbol_field"), "copilot_minute_symbol_field_invalid")
    timestamp_format = _text(
        profile.get("timestamp_format"), "copilot_minute_timestamp_format_invalid"
    )
    symbols = tuple(sorted(reference_facts))
    local = _aware(decision_time, "copilot_decision_time_timezone_required").astimezone(
        SHANGHAI
    )
    eligible = [
        slot
        for slot in session_bar_ends(local.date())
        if local - slot >= PROVIDER_AVAILABILITY_LAG
    ]
    if not eligible:
        raise TradingCopilotObservationError("copilot_minute_snapshot_bar_unavailable")
    bar_end = max(eligible)
    snapshot_decision_time = bar_end + PROVIDER_AVAILABILITY_LAG
    filters = {
        timestamp_field: {"eq": bar_end.strftime(timestamp_format)},
        symbol_field: {"in": symbols},
    }
    return replace(config, filters=filters), snapshot_decision_time


def _bind_activity_authority(
    source_value: object,
    activity_authorities: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    source = dict(_mapping(source_value, "copilot_source_invalid"))
    dataset_id = _text(source.get("datasetId"), "copilot_source_dataset_invalid")
    authority = activity_authorities.get(dataset_id)
    if authority is None:
        raise TradingCopilotObservationError(
            f"copilot_activity_authority_required:{dataset_id}"
        )
    source["activityAuthority"] = dict(
        _mapping(authority, "copilot_activity_authority_invalid")
    )
    try:
        return _source(source)
    except TradingCopilotProjectionError as exc:
        raise TradingCopilotObservationError(str(exc)) from exc


def _load_verified_observation_bundle(
    *,
    manifest_path: Path | str,
    state_root: Path | str,
) -> tuple[Any, Any]:
    """Load the exact committed observation bundle once for all consumers."""

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
    return manifest, bundle


def _company_facts_from_bundle(bundle: tuple[Any, Any]) -> dict[str, dict[str, Any]]:
    """Read security-master rows from an already loaded committed bundle."""

    manifest, bundle_value = bundle
    master_dataset_id = next(
        (item.dataset_id for item in manifest.datasets if item.probe_role == "security_master"),
        None,
    )
    master = next(
        (dataset for dataset in bundle_value.research_snapshot.datasets if dataset.dataset_id == master_dataset_id),
        None,
    )
    if (
        master is None
        or not master.eligible
        or not master.receipt_id
        or not master.source_proof_sha256
        or not getattr(master, "lineage_sha256", None)
        or not master.data_through
        or not master.observed_at
    ):
        raise TradingCopilotObservationError("copilot_security_master_binding_invalid")
    source = {
        "transportContract": FIXED_SOURCE_TRANSPORT,
        "datasetId": master.dataset_id,
        "receiptId": master.receipt_id,
        "receiptSha256": master.source_proof_sha256,
        "lineageSha256": getattr(master, "lineage_sha256", None),
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


def company_facts_from_verified_observation(
    *,
    manifest_path: Path | str,
    state_root: Path | str,
) -> dict[str, dict[str, Any]]:
    """Read security-master rows only through a committed observation bundle."""

    return _company_facts_from_bundle(
        _load_verified_observation_bundle(
            manifest_path=manifest_path,
            state_root=state_root,
        )
    )


def load_event_bundle(path: Path | str | None) -> tuple[EventEvidenceSnapshot, ...]:
    if path is None:
        return ()
    raise TradingCopilotObservationError(
        "copilot_event_bundle_runtime_evidence_required"
    )


def load_current_event_snapshots(
    *,
    minute_config: Any,
    token_file: Path | str,
    decision_time: datetime,
    symbols: Sequence[str],
    requested_on_demand_dataset_ids: Sequence[str] = (),
    retained_artifact_root: Path | str | None = None,
) -> tuple[
    tuple[EventEvidenceSnapshot, ...], tuple[str, ...], dict[str, str]
] | tuple[
    tuple[EventEvidenceSnapshot, ...], tuple[str, ...], dict[str, str], tuple[Path, ...]
]:
    """Read current event evidence through the same two fixed TD V1 routes.

    A dataset-level failure is returned as explicit coverage debt.  It never
    causes the worker to synthesize an event or sentiment label.
    """

    artifact_root = None if retained_artifact_root is None else Path(retained_artifact_root)
    if artifact_root is not None and (not artifact_root.is_absolute() or artifact_root.is_symlink()):
        raise TradingCopilotObservationError("copilot_event_artifact_root_invalid")
    try:
        consumer_profiles = select_event_consumer_profiles(
            load_event_consumer_profiles(),
            requested_on_demand_dataset_ids=requested_on_demand_dataset_ids,
        )
    except TradingCopilotEventConsumerProfileError as exc:
        raise TradingCopilotObservationError(str(exc)) from exc
    dataset_ids = tuple(profile.dataset_id for profile in consumer_profiles)
    transport = build_runtime_transport(
        minute_config.transport_id,
        token_file=token_file,
        base_url=minute_config.base_url,
    )
    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=minute_config.base_url,
            expected_catalog_version=minute_config.expected_catalog_version,
            dataset_ids=frozenset(dataset_ids),
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
    except AshareEvidenceContractError as exc:
        result = (
            (),
            dataset_ids,
            {dataset_id: exc.reason_code for dataset_id in dataset_ids},
        )
        return (*result, ()) if artifact_root is not None else result
    accepted: list[EventEvidenceSnapshot] = []
    blocked: list[str] = []
    blocked_reasons: dict[str, str] = {}
    retained_paths: list[Path] = []
    allowed = tuple(sorted(set(symbols)))
    for consumer_profile in consumer_profiles:
        dataset_id = consumer_profile.dataset_id
        profile = profiles.by_dataset.get(dataset_id)
        if profile is None:
            blocked.append(dataset_id)
            blocked_reasons[dataset_id] = "ashare_evidence_profile_missing"
            continue
        try:
            validate_event_consumer_profile_contract(
                consumer_profile=consumer_profile,
                evidence_profile=profile,
            )
        except TradingCopilotEventConsumerProfileError as exc:
            blocked.append(dataset_id)
            blocked_reasons[dataset_id] = str(exc)
            continue
        filter_contract = dict(profile.filter_operators)
        filters: dict[str, Any] = {}
        supports_symbol_filter = (
            profile.symbol_field is not None
            and "in" in filter_contract.get(profile.symbol_field, ())
        )
        if consumer_profile.symbol_binding == "required" and not supports_symbol_filter:
            blocked.append(dataset_id)
            blocked_reasons[dataset_id] = "copilot_event_symbol_query_mapping_unavailable"
            continue
        if supports_symbol_filter:
            assert profile.symbol_field is not None
            filters[profile.symbol_field] = {"in": list(allowed)}
        allowed_symbols = allowed if supports_symbol_filter else None
        try:
            snapshot = port.load_event_snapshot(
                profile=profile,
                filters=filters,
                decision_time=decision_time,
                audit_ledger=audit,
                allowed_symbols=allowed_symbols,
            )
        except AshareEvidenceContractError as exc:
            blocked.append(dataset_id)
            blocked_reasons[dataset_id] = exc.reason_code
            continue
        try:
            validate_event_consumer_runtime_evidence(
                consumer_profile=consumer_profile,
                evidence_profile=profile,
                snapshot=snapshot,
            )
        except TradingCopilotEventConsumerProfileError as exc:
            blocked.append(dataset_id)
            blocked_reasons[dataset_id] = str(exc)
            continue
        if artifact_root is not None:
            receipt_suffix = snapshot.events[0].receipt_id.removeprefix("receipt:")
            artifact_path = artifact_root / f"{dataset_id}.{receipt_suffix}.json"
            try:
                write_event_evidence_batch_artifact(batch=snapshot, path=artifact_path)
            except AshareEvidenceContractError as exc:
                blocked.append(dataset_id)
                blocked_reasons[dataset_id] = exc.reason_code
                continue
            retained_paths.append(artifact_path)
        accepted.extend(snapshot.events)
    result = (tuple(accepted), tuple(blocked), blocked_reasons)
    return (*result, tuple(retained_paths)) if artifact_root is not None else result


def _retention_root(path: Path | str, reason: str) -> Path:
    """Validate an explicitly configured retention root without resolving links."""

    candidate = Path(path)
    if not candidate.is_absolute():
        raise TradingCopilotObservationError(reason)
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise TradingCopilotObservationError(reason)
        except OSError as exc:
            raise TradingCopilotObservationError(reason) from exc
    return absolute


def _canonical_observation_time(value: object, reason: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TradingCopilotObservationError(reason) from exc
    else:
        raise TradingCopilotObservationError(reason)
    return _aware(parsed, reason).astimezone(timezone.utc)


def retain_same_observation_inputs(
    *,
    research_snapshot: ResearchDataSnapshot,
    event_artifact_paths: Sequence[Path | str],
    blocked_dataset_reasons: Mapping[str, str],
    decision_time: datetime,
    store_root: Path | str,
) -> dict[str, Any]:
    """Persist and bind one exact security snapshot with its event artifacts.

    This is deliberately opt-in and runs only after the caller has completed
    the existing typed event artifact writes.  It never creates a second
    snapshot format or combines artifacts by directory discovery.
    """

    if not isinstance(research_snapshot, ResearchDataSnapshot):
        raise TradingCopilotObservationError("copilot_research_snapshot_invalid")
    if (
        research_snapshot.execution_eligible is not True
        or research_snapshot.historical_pit_eligible is not False
        or research_snapshot.blocking_reasons
    ):
        raise TradingCopilotObservationError(
            "copilot_research_snapshot_not_observation_eligible"
        )
    decision = _canonical_observation_time(
        decision_time,
        "copilot_research_snapshot_observation_time_invalid",
    )
    snapshot_decision = _canonical_observation_time(
        research_snapshot.decision_as_of,
        "copilot_research_snapshot_observation_time_invalid",
    )
    if snapshot_decision != decision:
        raise TradingCopilotObservationError(
            "copilot_research_snapshot_observation_identity_mismatch"
        )
    master = next(
        (
            dataset
            for dataset in research_snapshot.datasets
            if dataset.dataset_id == "cn.equity.security_master"
        ),
        None,
    )
    if (
        master is None
        or master.eligible is not True
        or not master.receipt_id
        or not master.source_proof_sha256
        or not master.lineage_sha256
        or not master.data_through
        or not master.observed_at
    ):
        raise TradingCopilotObservationError(
            "copilot_security_master_binding_invalid"
        )
    if not isinstance(blocked_dataset_reasons, Mapping):
        raise TradingCopilotObservationError("copilot_event_retention_coverage_invalid")
    blocked: dict[str, str] = {}
    for dataset_id, reason in blocked_dataset_reasons.items():
        if dataset_id not in PRIMARY_DATASET_IDS or not isinstance(reason, str) or not reason.strip():
            raise TradingCopilotObservationError("copilot_event_retention_coverage_invalid")
        blocked[dataset_id] = reason

    batches: list[tuple[Path, Any]] = []
    seen_paths: set[Path] = set()
    seen_datasets: set[str] = set()
    for raw_path in event_artifact_paths:
        try:
            path = _retention_root(
                raw_path,
                "copilot_event_retention_artifact_invalid",
            )
        except (TradingCopilotObservationError, TypeError, ValueError, OSError) as exc:
            raise TradingCopilotObservationError(
                "copilot_event_retention_artifact_invalid"
            ) from exc
        if path.is_symlink() or path in seen_paths:
            raise TradingCopilotObservationError("copilot_event_retention_artifact_invalid")
        seen_paths.add(path)
        try:
            batch = load_event_evidence_batch_artifact(path)
        except AshareEvidenceContractError as exc:
            raise TradingCopilotObservationError(
                "copilot_event_retention_artifact_invalid"
            ) from exc
        dataset_id = batch.profile.dataset_id
        try:
            event_times = tuple(
                _canonical_observation_time(
                    event.as_of,
                    "copilot_event_retention_artifact_invalid",
                )
                for event in batch.events
            )
        except (TradingCopilotObservationError, AttributeError, TypeError) as exc:
            raise TradingCopilotObservationError(
                "copilot_event_retention_artifact_invalid"
            ) from exc
        if (
            dataset_id not in PRIMARY_DATASET_IDS
            or dataset_id in seen_datasets
            or dataset_id in blocked
            or batch.same_observation is not True
            or batch.observed_catalog_version != research_snapshot.catalog_version
            or any(event_time != snapshot_decision for event_time in event_times)
        ):
            raise TradingCopilotObservationError(
                "copilot_research_snapshot_observation_identity_mismatch"
            )
        seen_datasets.add(dataset_id)
        batches.append((path, batch))

    if seen_datasets | set(blocked) != set(PRIMARY_DATASET_IDS) or seen_datasets & set(blocked):
        raise TradingCopilotObservationError("copilot_event_retention_coverage_invalid")
    root = _retention_root(store_root, "copilot_research_snapshot_store_root_invalid")
    try:
        FileResearchSnapshotStore(root).compare_and_swap(
            snapshot=research_snapshot,
            expected_snapshot_sha256=None,
        )
    except (ResearchSnapshotStoreConflict, ResearchSnapshotStoreCorruption, ValueError) as exc:
        raise TradingCopilotObservationError(
            "copilot_research_snapshot_retention_failed"
        ) from exc

    event_bindings = []
    for path, batch in batches:
        try:
            artifact_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TradingCopilotObservationError(
                "copilot_event_retention_artifact_invalid"
            ) from exc
        event_bindings.append(
            {
                "path": str(path),
                "artifactSha256": artifact_sha,
                "datasetId": batch.profile.dataset_id,
                "catalogVersion": batch.observed_catalog_version,
                "receiptIds": sorted({event.receipt_id for event in batch.events}),
                "rowCount": batch.row_count,
                "pageCount": batch.page_count,
                "sameObservation": batch.same_observation,
            }
        )
    event_bindings.sort(key=lambda item: item["datasetId"])
    return {
        "storeRoot": str(root),
        "snapshotSha256": research_snapshot.snapshot_sha256,
        "snapshotPath": str(root / f"snapshot-{research_snapshot.snapshot_sha256}.json"),
        "profileId": research_snapshot.profile_id,
        "catalogVersion": research_snapshot.catalog_version,
        "decisionAsOf": research_snapshot.decision_as_of,
        "eventArtifacts": event_bindings,
        "blockedDatasetReasons": dict(sorted(blocked.items())),
        "authority": {
            "observationMode": "current_observation",
            "currentObservationAuthority": False,
            "historicalPitEligible": False,
            "trainingEligible": False,
            "candidateEligible": False,
            "promotionEligible": False,
            "executionEligible": False,
            "riskAuthority": False,
            "positionAuthority": False,
            "orderAuthority": False,
            "realTradingEnabled": False,
        },
    }


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


def _event_for_projection(
    event: EventEvidenceSnapshot,
    generated_at: datetime,
    activity_authorities: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if event.symbol is None or event.url is None:
        return None
    kind, source_class = _event_kind(event)
    confidence = "high" if event.evidence_confidence >= 0.8 else "medium" if event.evidence_confidence >= 0.55 else "low"
    age = generated_at - event.available_at.astimezone(generated_at.tzinfo)
    novelty = "new" if timedelta(0) <= age <= timedelta(hours=24) else "repeated"
    title = event.title or event.content
    summary = event.content or event.title
    assert title is not None and summary is not None
    capability = {
        "inputContract": BATCH_INPUT_CONTRACT,
        "transportContract": FIXED_SOURCE_TRANSPORT,
        "datasetId": event.dataset_id,
        "catalogVersion": event.catalog_version,
        "asOf": event.as_of.isoformat(),
        "dataThrough": event.data_through.isoformat(),
        "freshness": "fresh",
        "receiptId": event.receipt_id,
        "receiptSha256": event.envelope_proof_sha256,
        "lineageSha256": event.source_lineage_sha256,
    }
    if activity_authorities is not None:
        authority = activity_authorities.get(event.dataset_id)
        if authority is None:
            raise TradingCopilotObservationError(
                f"copilot_activity_authority_required:{event.dataset_id}"
            )
        try:
            capability["activityAuthority"] = _activity_authority(
                authority,
                dataset_id=event.dataset_id,
                data_through=capability["dataThrough"],
                receipt_id=event.receipt_id,
                receipt_sha256=event.envelope_proof_sha256,
                lineage_sha256=event.source_lineage_sha256,
            )
        except TradingCopilotProjectionError as exc:
            raise TradingCopilotObservationError(str(exc)) from exc
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
        "dataCapability": capability,
    }


def _receipt_pairs(bars: Sequence[MinuteBarEvidence]) -> list[dict[str, str]]:
    pairs = {(bar.receipt_id, bar.envelope_proof_sha256) for bar in bars}
    return [
        {"receiptId": receipt_id, "receiptSha256": receipt_sha}
        for receipt_id, receipt_sha in sorted(pairs)
    ]


_OFFLINE_AUTHORITY_FLAGS = (
    "candidate_eligible",
    "execution_eligible",
    "training_eligible",
    "promotion_eligible",
    "capital_authority",
    "execution_authority",
    "risk_authority",
    "position_authority",
    "real_trading_enabled",
    "candidateEligible",
    "executionEligible",
    "trainingEligible",
    "promotionEligible",
    "capitalAuthority",
    "executionAuthority",
    "riskAuthority",
    "positionAuthority",
    "realTradingEnabled",
)


def _reject_offline_authority_flags(value: object, reason: str) -> None:
    if isinstance(value, Mapping):
        if any(value.get(field_name) is True for field_name in _OFFLINE_AUTHORITY_FLAGS):
            raise TradingCopilotObservationError(reason)
        for nested in value.values():
            _reject_offline_authority_flags(nested, reason)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_offline_authority_flags(nested, reason)


def _offline_timestamp(value: object, reason: str) -> datetime:
    raw = _text(value, reason)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradingCopilotObservationError(reason) from exc
    return _aware(parsed, reason)


def _offline_profile_from_manifest(
    config: Any,
    receipt: Mapping[str, Any],
) -> MinuteDatasetProfile:
    """Rebuild the already-bound minute profile without catalog access.

    Offline projection is allowed only when the manifest carries the complete
    catalog-derived profile that the live canary used.  A partial manifest is
    not upgraded with local defaults or inferred operators.
    """

    values = _mapping(config.profile, "copilot_offline_minute_profile_invalid")
    expected_catalog = _text(
        config.expected_catalog_version,
        "copilot_offline_minute_profile_invalid",
    )
    if _text(receipt.get("expected_catalog_version"), "copilot_offline_profile_binding_invalid") != expected_catalog:
        raise TradingCopilotObservationError("copilot_offline_profile_binding_invalid")
    observed_catalog = _text(
        receipt.get("observed_catalog_version"),
        "copilot_offline_profile_binding_invalid",
    )
    if _text(values.get("observed_catalog_version"), "copilot_offline_minute_profile_invalid") != observed_catalog:
        raise TradingCopilotObservationError("copilot_offline_profile_binding_invalid")

    def _strings(name: str, *, nonempty: bool = True) -> tuple[str, ...]:
        raw = values.get(name)
        if not isinstance(raw, (list, tuple)) or (nonempty and not raw):
            raise TradingCopilotObservationError("copilot_offline_minute_profile_invalid")
        result = tuple(
            _text(item, "copilot_offline_minute_profile_invalid") for item in raw
        )
        if len(result) != len(set(result)):
            raise TradingCopilotObservationError("copilot_offline_minute_profile_invalid")
        return result

    raw_operators = values.get("filter_operators")
    if not isinstance(raw_operators, Mapping):
        raise TradingCopilotObservationError("copilot_offline_minute_profile_invalid")
    operators: list[tuple[str, tuple[str, ...]]] = []
    for field_name in sorted(raw_operators):
        field = _text(field_name, "copilot_offline_minute_profile_invalid")
        raw_values = raw_operators[field_name]
        if not isinstance(raw_values, (list, tuple)) or not raw_values:
            raise TradingCopilotObservationError("copilot_offline_minute_profile_invalid")
        normalized = tuple(
            _text(item, "copilot_offline_minute_profile_invalid")
            for item in raw_values
        )
        if len(normalized) != len(set(normalized)):
            raise TradingCopilotObservationError("copilot_offline_minute_profile_invalid")
        operators.append((field, normalized))

    try:
        profile = MinuteDatasetProfile(
            expected_catalog_version=expected_catalog,
            observed_catalog_version=observed_catalog,
            dataset_id=_text(config.dataset_id, "copilot_offline_minute_profile_invalid"),
            schema_major=values["schema_major"],
            default_fields=_strings("default_fields"),
            default_order=_strings("default_order", nonempty=False),
            filter_operators=tuple(operators),
            dataset_contract_fingerprint=_text(
                values.get("dataset_contract_fingerprint"),
                "copilot_offline_minute_profile_invalid",
            ),
            consumer_profile_sha256=_text(
                values.get("consumer_profile_sha256"),
                "copilot_offline_minute_profile_invalid",
            ),
            identity_fields=_strings("identity_fields"),
            symbol_field=_text(values.get("symbol_field"), "copilot_offline_minute_profile_invalid"),
            timestamp_field=_text(values.get("timestamp_field"), "copilot_offline_minute_profile_invalid"),
            open_field=_text(values.get("open_field"), "copilot_offline_minute_profile_invalid"),
            high_field=_text(values.get("high_field"), "copilot_offline_minute_profile_invalid"),
            low_field=_text(values.get("low_field"), "copilot_offline_minute_profile_invalid"),
            close_field=_text(values.get("close_field"), "copilot_offline_minute_profile_invalid"),
            volume_field=_text(values.get("volume_field"), "copilot_offline_minute_profile_invalid"),
            amount_field=_text(values.get("amount_field"), "copilot_offline_minute_profile_invalid"),
            previous_close_field=(
                None
                if values.get("previous_close_field") is None
                else _text(values.get("previous_close_field"), "copilot_offline_minute_profile_invalid")
            ),
            suspension_field=(
                None
                if values.get("suspension_field") is None
                else _text(values.get("suspension_field"), "copilot_offline_minute_profile_invalid")
            ),
            frequency_field=(
                None
                if values.get("frequency_field") is None
                else _text(values.get("frequency_field"), "copilot_offline_minute_profile_invalid")
            ),
            frequency_value=(
                None
                if values.get("frequency_value") is None
                else _text(values.get("frequency_value"), "copilot_offline_minute_profile_invalid")
            ),
            timestamp_format=_text(values.get("timestamp_format"), "copilot_offline_minute_profile_invalid"),
            timestamp_semantics=MinuteTimestampSemantics(
                _text(values.get("timestamp_semantics"), "copilot_offline_minute_profile_invalid")
            ),
            volume_multiplier_to_shares=values["volume_multiplier_to_shares"],
            amount_multiplier_to_cny=values["amount_multiplier_to_cny"],
            price_adjustment=_text(values.get("price_adjustment"), "copilot_offline_minute_profile_invalid"),
            max_pages=values["max_pages"],
            max_rows=values["max_rows"],
            page_limit=values["page_limit"],
        )
    except (KeyError, MinuteDataContractError, TypeError, ValueError) as exc:
        raise TradingCopilotObservationError(
            "copilot_offline_minute_profile_invalid"
        ) from exc
    if profile.dataset_contract_fingerprint != _text(
        receipt.get("dataset_contract_fingerprint"),
        "copilot_offline_profile_binding_invalid",
    ) or profile.consumer_profile_sha256 != _text(
        receipt.get("consumer_profile_sha256"),
        "copilot_offline_profile_binding_invalid",
    ):
        raise TradingCopilotObservationError("copilot_offline_profile_binding_invalid")
    return profile


def build_offline_projection_batch(
    *,
    canary_receipt_path: Path | str,
    minute_manifest_path: Path | str,
    reference_facts_path: Path | str,
    company_facts_path: Path | str,
    activity_authorities_path: Path | str,
    generated_at: datetime,
    valid_until: datetime,
    event_artifact_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build one v2 batch from retained, receipt-bound files only.

    This function deliberately has no token, transport, provider, or publisher
    input.  It is the only offline bridge from a future exact-slot canary to
    ``build_projection_batch``; all inputs are validated before the builder is
    called and only the caller writes the resulting batch.
    """

    canary = _load_json(
        Path(canary_receipt_path), "copilot_offline_canary_receipt_invalid"
    )
    _reject_offline_authority_flags(
        canary, "copilot_offline_trading_authority_present"
    )
    if canary.get("authority_tier") != "observation_only":
        raise TradingCopilotObservationError("copilot_offline_canary_authority_invalid")
    if "snapshot_rows" not in canary:
        raise TradingCopilotObservationError("copilot_offline_canary_snapshot_rows_required")
    config = load_minute_canary_config(Path(minute_manifest_path))
    profile = _offline_profile_from_manifest(config, canary)
    try:
        reference_facts = load_reference_facts(Path(reference_facts_path))
        snapshot = snapshot_from_canary_receipt(canary, profile=profile)
    except (MinuteCanaryConfigurationError, ValueError) as exc:
        raise TradingCopilotObservationError(str(exc)) from exc
    snapshot_symbols = {bar.symbol for bar in snapshot.bars}
    if snapshot_symbols != set(reference_facts):
        raise TradingCopilotObservationError("copilot_offline_symbol_set_mismatch")
    minute_data_through = max(bar.data_through for bar in snapshot.bars)

    company_facts = load_company_facts(Path(company_facts_path))
    if set(company_facts) != snapshot_symbols:
        raise TradingCopilotObservationError("copilot_offline_company_symbol_set_mismatch")
    for company in company_facts.values():
        _reject_offline_authority_flags(
            company, "copilot_offline_company_authority_present"
        )

    activity_authorities = load_activity_authorities(Path(activity_authorities_path))
    _reject_offline_authority_flags(
        activity_authorities, "copilot_offline_activity_authority_present"
    )
    if "cn.equity.security_master" not in activity_authorities:
        raise TradingCopilotObservationError(
            "copilot_activity_authority_required:cn.equity.security_master"
        )
    minute_authority = activity_authorities.get(profile.dataset_id)
    if minute_authority is None:
        raise TradingCopilotObservationError(
            f"copilot_activity_authority_required:{profile.dataset_id}"
        )
    _reject_offline_authority_flags(
        minute_authority, "copilot_offline_activity_authority_present"
    )
    if _offline_timestamp(
        minute_authority.get("dataThrough"), "copilot_offline_activity_window_invalid"
    ) != minute_data_through:
        raise TradingCopilotObservationError("copilot_offline_activity_window_invalid")
    for company in company_facts.values():
        source = _mapping(company.get("source"), "copilot_offline_company_source_invalid")
        _bind_activity_authority(source, activity_authorities)

    events: tuple[EventEvidenceSnapshot, ...] = ()
    if event_artifact_path is not None:
        try:
            event_batch = load_event_evidence_batch_artifact(Path(event_artifact_path))
        except AshareEvidenceContractError as exc:
            raise TradingCopilotObservationError(str(exc)) from exc
        events = event_batch.events
        if any(event.symbol is not None and event.symbol not in snapshot_symbols for event in events):
            raise TradingCopilotObservationError("copilot_offline_event_symbol_set_mismatch")
        for event in events:
            _reject_offline_authority_flags(
                event.__dict__, "copilot_offline_event_authority_present"
            )

    try:
        return build_projection_batch(
            snapshot=snapshot,
            company_facts=company_facts,
            events=events,
            activity_authorities=activity_authorities,
            generated_at=generated_at,
            valid_until=valid_until,
        )
    except (TradingCopilotObservationError, TradingCopilotProjectionError) as exc:
        raise TradingCopilotObservationError(str(exc)) from exc


def build_projection_batch(
    *,
    snapshot: MinuteBarSnapshot,
    company_facts: Mapping[str, Mapping[str, Any]],
    events: Sequence[EventEvidenceSnapshot],
    activity_authorities: Mapping[str, Mapping[str, Any]] | None = None,
    generated_at: datetime,
    valid_until: datetime,
) -> dict[str, Any]:
    """Project accepted bars/events without granting model or trade authority."""

    generated_at = _aware(generated_at, "copilot_generated_at_timezone_required")
    valid_until = _aware(valid_until, "copilot_valid_until_timezone_required")
    if valid_until <= generated_at:
        raise TradingCopilotObservationError("copilot_projection_time_invalid")
    if activity_authorities is None:
        activity_authorities = {}
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
            "lineageSha256": latest.source_lineage_sha256,
            "dataThrough": max(bar.data_through for bar in bars).isoformat(),
            "retrievedAt": max(bar.available_at for bar in bars).isoformat(),
            "freshness": (
                "stale"
                if latest.evidence_use is MinuteEvidenceUse.HISTORICAL_DISPLAY
                else "fresh"
            ),
            "adjustment": "none",
        }
        source = _bind_activity_authority(source, activity_authorities)
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
            if (
                projected := _event_for_projection(
                    event, generated_at, activity_authorities
                )
            ) is not None
        ]
        company_source = _bind_activity_authority(
            company.get("source"), activity_authorities
        )
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
                "session": "unknown",
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
    parser.add_argument("--on-demand-event-dataset", action="append", default=[])
    parser.add_argument("--event-evidence-artifact-root", type=Path)
    parser.add_argument("--event-timeline-output-root", type=Path)
    parser.add_argument("--research-snapshot-store-root", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--activity-authorities", type=Path, required=True)
    parser.add_argument("--decision-time", required=True)
    parser.add_argument("--trading-date")
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
    parser.add_argument("--projection-output-root", type=Path)
    parser.add_argument("--result-output", type=Path)
    parser.add_argument(
        "--offline-canary-receipt",
        type=Path,
        help="Build one private v2 batch from an exact full-row canary without TD or publication.",
    )
    parser.add_argument(
        "--offline-event-artifact",
        type=Path,
        help="Optional typed event-evidence artifact for --offline-canary-receipt.",
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.offline_canary_receipt is not None:
            if arguments.observation_manifest or arguments.observation_state_root:
                raise TradingCopilotObservationError(
                    "copilot_offline_company_facts_file_required"
                )
            if arguments.load_current_events or arguments.event_bundle:
                raise TradingCopilotObservationError(
                    "copilot_offline_event_artifact_required"
                )
            if arguments.company_facts is None:
                raise TradingCopilotObservationError(
                    "copilot_offline_company_facts_file_required"
                )
            if not arguments.batch_output.is_absolute() or arguments.batch_output.is_symlink():
                raise TradingCopilotObservationError("copilot_offline_batch_output_invalid")
            decision_time = datetime.fromisoformat(
                arguments.decision_time.replace("Z", "+00:00")
            )
            valid_until = datetime.fromisoformat(
                arguments.valid_until.replace("Z", "+00:00")
            )
            batch = build_offline_projection_batch(
                canary_receipt_path=arguments.offline_canary_receipt,
                minute_manifest_path=arguments.minute_manifest,
                reference_facts_path=arguments.reference_facts,
                company_facts_path=arguments.company_facts,
                activity_authorities_path=arguments.activity_authorities,
                event_artifact_path=arguments.offline_event_artifact,
                generated_at=decision_time,
                valid_until=valid_until,
            )
            _atomic_json(arguments.batch_output, batch)
            result = {
                "status": "pass",
                "mode": "offline_projection_batch",
                "contractId": batch["contractId"],
                "symbolCount": len(batch["items"]),
                "eventCount": sum(len(item["events"]) for item in batch["items"]),
                "publisherInvoked": False,
                "realTradingEnabled": False,
                "batchOutput": str(arguments.batch_output),
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if (
            arguments.token_file is None
            or arguments.projection_output_root is None
            or arguments.result_output is None
            or arguments.trading_date is None
        ):
            raise TradingCopilotObservationError("copilot_worker_runtime_outputs_required")
        if bool(arguments.event_evidence_artifact_root) != bool(arguments.event_timeline_output_root):
            raise TradingCopilotObservationError("copilot_event_retention_roots_required")
        if (arguments.event_evidence_artifact_root or arguments.event_timeline_output_root) and not arguments.load_current_events:
            raise TradingCopilotObservationError("copilot_event_retention_requires_current_td_read")
        if arguments.research_snapshot_store_root:
            if not arguments.observation_manifest or not arguments.observation_state_root:
                raise TradingCopilotObservationError(
                    "copilot_research_snapshot_retention_requires_observation"
                )
            if (
                not arguments.load_current_events
                or not arguments.event_evidence_artifact_root
                or not arguments.event_timeline_output_root
            ):
                raise TradingCopilotObservationError(
                    "copilot_research_snapshot_retention_requires_event_artifacts"
                )
            _retention_root(
                arguments.research_snapshot_store_root,
                "copilot_research_snapshot_store_root_invalid",
            )
            _retention_root(
                arguments.event_evidence_artifact_root,
                "copilot_event_artifact_root_invalid",
            )
            _retention_root(
                arguments.event_timeline_output_root,
                "copilot_event_timeline_root_invalid",
            )
        decision_time = datetime.fromisoformat(arguments.decision_time.replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(arguments.valid_until.replace("Z", "+00:00"))
        config = load_minute_canary_config(arguments.minute_manifest.resolve())
        reference_facts = load_reference_facts(arguments.reference_facts.resolve())
        snapshot_config, snapshot_decision_time = _pinned_snapshot_plan(
            config, reference_facts, decision_time
        )
        _, snapshot, _ = load_minute_snapshot(
            snapshot_config,
            token_file=arguments.token_file.resolve(),
            decision_time=snapshot_decision_time,
            trading_date=date.fromisoformat(arguments.trading_date),
            reference_facts=reference_facts,
            evidence_use=MinuteEvidenceUse(arguments.evidence_use),
        )
        observation_bundle = None
        if arguments.observation_manifest:
            if not arguments.observation_state_root:
                raise TradingCopilotObservationError("copilot_observation_state_root_required")
            observation_bundle = _load_verified_observation_bundle(
                manifest_path=arguments.observation_manifest.resolve(),
                state_root=arguments.observation_state_root.resolve(),
            )
            company_facts = _company_facts_from_bundle(observation_bundle)
        else:
            if arguments.observation_state_root:
                raise TradingCopilotObservationError("copilot_observation_manifest_required")
            company_facts = load_company_facts(arguments.company_facts.resolve())
        if arguments.load_current_events:
            current_events = load_current_event_snapshots(
                minute_config=config,
                token_file=arguments.token_file.resolve(),
                decision_time=decision_time,
                symbols=tuple(bar.symbol for bar in snapshot.bars),
                requested_on_demand_dataset_ids=arguments.on_demand_event_dataset,
                retained_artifact_root=(
                    _retention_root(
                        arguments.event_evidence_artifact_root,
                        "copilot_event_artifact_root_invalid",
                    )
                    if arguments.event_evidence_artifact_root
                    else None
                ),
            )
            if arguments.event_evidence_artifact_root:
                events, blocked_event_datasets, blocked_event_reasons, retained_paths = current_events
                from Ashare.trading_copilot_event_timeline import publish_retained_event_timeline

                publish_retained_event_timeline(
                    artifact_paths=retained_paths,
                    symbols=tuple(bar.symbol for bar in snapshot.bars),
                    blocked_dataset_reasons=blocked_event_reasons,
                    generated_at=decision_time,
                    valid_until=valid_until,
                    output_root=_retention_root(
                        arguments.event_timeline_output_root,
                        "copilot_event_timeline_root_invalid",
                    ),
                )
            else:
                events, blocked_event_datasets, blocked_event_reasons = current_events
        else:
            if arguments.on_demand_event_dataset:
                raise TradingCopilotObservationError(
                    "copilot_event_on_demand_requires_current_td_read"
                )
            events = load_event_bundle(arguments.event_bundle.resolve() if arguments.event_bundle else None)
            blocked_event_datasets = ()
            blocked_event_reasons = {}
        batch = build_projection_batch(
            snapshot=snapshot,
            company_facts=company_facts,
            events=events,
            activity_authorities=load_activity_authorities(
                arguments.activity_authorities.resolve()
            ),
            generated_at=decision_time,
            valid_until=valid_until,
        )
        batch_path = arguments.batch_output.resolve()
        _atomic_json(batch_path, batch)
        research_event_retention = None
        if arguments.research_snapshot_store_root:
            if observation_bundle is None:
                raise TradingCopilotObservationError(
                    "copilot_research_snapshot_retention_requires_observation"
                )
            _, authority_bundle = observation_bundle
            research_event_retention = retain_same_observation_inputs(
                research_snapshot=authority_bundle.research_snapshot,
                event_artifact_paths=retained_paths,
                blocked_dataset_reasons=blocked_event_reasons,
                decision_time=decision_time,
                store_root=arguments.research_snapshot_store_root,
            )
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
        "blockedDatasetReasons": blocked_event_reasons,
        "sentimentLabelsInvented": False,
    }
    if research_event_retention is not None:
        result["researchEventRetention"] = research_event_retention
    result["resultOutput"] = str(arguments.result_output.resolve())
    _atomic_json(arguments.result_output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
