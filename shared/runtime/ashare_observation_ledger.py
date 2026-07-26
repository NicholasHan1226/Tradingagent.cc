"""Immutable per-symbol A-share observation membership ledger.

The ledger consumes an already validated current-observation research snapshot,
its aggregate observation receipt, and explicit per-symbol membership records.
It performs no transport, ranking, probability, learning, performance,
promotion, capital, order, fill, MarketGraph, or LLM work.  One trade session
can be bound once; exact replays are idempotent and every conflicting rewrite
fails closed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from shared.data.research_snapshot import (
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)
from shared.universe.policy import is_mainboard_tradable


ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID = (
    "tradingagent.ashare.observation-membership-ledger.v1"
)
ASHARE_OBSERVATION_MEMBERSHIP_BINDING_SCHEMA_ID = (
    "tradingagent.ashare.observation-membership-binding.v1"
)
# A post-close daily observation has no trustworthy intraday or next-session
# label anchor by itself.  Horizons remain disabled until a frozen trading
# calendar, paper-trade session, and matching market-truth evidence exist.
LABEL_HORIZONS: tuple[str, ...] = ()
OBSERVED_REASON_CODE = "phase1_mainboard_observed"

_COMPAT_OBSERVATION_RECEIPT_SCHEMA_ID = "tradingagent.ashare.observation-receipt.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_RE = re.compile(r"^[0-9]{8}$")
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_ATOMIC_TMP_RE = re.compile(r"^\.tmp-[0-9a-f]{32}\.json$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_FILE_BYTES = 32 * 1024 * 1024

_ARTIFACT_KEYS = frozenset(
    {
        "schema_id",
        "observation_session",
        "decision_as_of",
        "profile_id",
        "profile_contract_sha256",
        "catalog_version",
        "catalog_version_sha256",
        "snapshot_sha256",
        "probe_receipt_sha256",
        "observation_receipt_sha256",
        "universe_sha256",
        "records",
        "label_horizons",
        "historical_pit_eligible",
        "learning_eligible",
        "performance_eligible",
        "promotion_eligible",
        "real_trading_enabled",
        "content_sha256",
    }
)
_RECORD_KEYS = frozenset({"symbol", "disposition", "reason_code"})
_BINDING_KEYS = frozenset(
    {
        "schema_id",
        "observation_session",
        "decision_as_of",
        "session_identity_sha256",
        "artifact_content_sha256",
        "snapshot_sha256",
        "observation_receipt_sha256",
        "content_sha256",
    }
)


class AshareObservationLedgerContractError(ValueError):
    """Raised when supplied observation evidence is not safely bindable."""


class AshareObservationLedgerCorruption(RuntimeError):
    """Raised when durable ledger bytes or paths cannot be trusted."""


class AshareObservationLedgerConflict(RuntimeError):
    """Raised when immutable/CAS semantics reject a write."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AshareObservationLedgerContractError(
            "observation_ledger_noncanonical_value"
        ) from exc


def _sha256_value(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AshareObservationLedgerContractError(f"{field_name}_invalid")
    return value


def _nonempty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AshareObservationLedgerContractError(f"{field_name}_invalid")
    return value


def _aware_utc(value: object, *, field_name: str) -> datetime:
    text = _nonempty(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AshareObservationLedgerContractError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AshareObservationLedgerContractError(f"{field_name}_invalid")
    return parsed.astimezone(timezone.utc)


def _session(value: object) -> str:
    if not isinstance(value, str) or _SESSION_RE.fullmatch(value) is None:
        raise AshareObservationLedgerContractError("observation_session_invalid")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise AshareObservationLedgerContractError(
            "observation_session_invalid"
        ) from exc
    return value


def _session_is_not_after_decision(session: str, decision: datetime) -> bool:
    return datetime.strptime(session, "%Y%m%d").date() <= decision.astimezone(
        _SHANGHAI
    ).date()


@dataclass(frozen=True)
class AshareObservationMembershipRecord:
    """One stock's explicit Phase-1 membership disposition for one session."""

    symbol: str
    disposition: str
    reason_code: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.symbol, str)
            or _SYMBOL_RE.fullmatch(self.symbol) is None
        ):
            raise AshareObservationLedgerContractError("record_symbol_invalid")
        if self.disposition not in {"observed", "excluded"}:
            raise AshareObservationLedgerContractError("record_disposition_invalid")
        if (
            not isinstance(self.reason_code, str)
            or _REASON_RE.fullmatch(self.reason_code) is None
        ):
            raise AshareObservationLedgerContractError("record_reason_code_invalid")
        if self.disposition == "observed":
            if not is_mainboard_tradable(
                self.symbol,
                instrument_type="common_stock",
            ):
                raise AshareObservationLedgerContractError(
                    "observed_symbol_not_mainboard"
                )
            if self.reason_code != OBSERVED_REASON_CODE:
                raise AshareObservationLedgerContractError(
                    "observed_reason_code_invalid"
                )
        elif self.reason_code == OBSERVED_REASON_CODE:
            raise AshareObservationLedgerContractError("excluded_reason_code_invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "disposition": self.disposition,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class AshareObservationMembershipArtifact:
    """Canonical immutable membership artifact for one A-share session."""

    observation_session: str
    decision_as_of: str
    profile_id: str
    profile_contract_sha256: str
    catalog_version: str
    catalog_version_sha256: str
    snapshot_sha256: str
    probe_receipt_sha256: str
    observation_receipt_sha256: str
    universe_sha256: str
    records: tuple[AshareObservationMembershipRecord, ...]
    label_horizons: tuple[str, ...]
    historical_pit_eligible: bool
    learning_eligible: bool
    performance_eligible: bool
    promotion_eligible: bool
    real_trading_enabled: bool
    content_sha256: str
    schema_id: str = ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID

    def __post_init__(self) -> None:
        session = _session(self.observation_session)
        decision = _aware_utc(self.decision_as_of, field_name="decision_as_of")
        if not _session_is_not_after_decision(session, decision):
            raise AshareObservationLedgerContractError(
                "artifact_decision_session_mismatch"
            )
        if self.schema_id != ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID:
            raise AshareObservationLedgerContractError("artifact_schema_invalid")
        _nonempty(self.profile_id, field_name="profile_id")
        _sha256(
            self.profile_contract_sha256,
            field_name="profile_contract_sha256",
        )
        catalog = _nonempty(self.catalog_version, field_name="catalog_version")
        _sha256(self.catalog_version_sha256, field_name="catalog_version_sha256")
        if self.catalog_version_sha256 != _sha256_value(catalog):
            raise AshareObservationLedgerContractError("catalog_hash_mismatch")
        for name in (
            "snapshot_sha256",
            "probe_receipt_sha256",
            "observation_receipt_sha256",
            "universe_sha256",
            "content_sha256",
        ):
            _sha256(getattr(self, name), field_name=name)
        if self.label_horizons != LABEL_HORIZONS:
            raise AshareObservationLedgerContractError("label_horizons_invalid")
        if (
            self.historical_pit_eligible,
            self.learning_eligible,
            self.performance_eligible,
            self.promotion_eligible,
            self.real_trading_enabled,
        ) != (False, False, False, False, False):
            raise AshareObservationLedgerContractError(
                "artifact_authority_flag_invalid"
            )
        if (
            not isinstance(self.records, tuple)
            or not self.records
            or any(
                not isinstance(item, AshareObservationMembershipRecord)
                for item in self.records
            )
            or self.records != tuple(sorted(self.records, key=lambda item: item.symbol))
            or len({item.symbol for item in self.records}) != len(self.records)
        ):
            raise AshareObservationLedgerContractError("records_not_canonical")
        observed = [
            item.symbol for item in self.records if item.disposition == "observed"
        ]
        if not observed or self.universe_sha256 != _sha256_value(observed):
            raise AshareObservationLedgerContractError("universe_hash_mismatch")
        unsigned = {
            "schema_id": self.schema_id,
            "observation_session": self.observation_session,
            "decision_as_of": self.decision_as_of,
            "profile_id": self.profile_id,
            "profile_contract_sha256": self.profile_contract_sha256,
            "catalog_version": self.catalog_version,
            "catalog_version_sha256": self.catalog_version_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "probe_receipt_sha256": self.probe_receipt_sha256,
            "observation_receipt_sha256": self.observation_receipt_sha256,
            "universe_sha256": self.universe_sha256,
            "records": [item.to_dict() for item in self.records],
            "label_horizons": list(self.label_horizons),
            "historical_pit_eligible": self.historical_pit_eligible,
            "learning_eligible": self.learning_eligible,
            "performance_eligible": self.performance_eligible,
            "promotion_eligible": self.promotion_eligible,
            "real_trading_enabled": self.real_trading_enabled,
        }
        if self.content_sha256 != _sha256_value(unsigned):
            raise AshareObservationLedgerContractError("artifact_hash_mismatch")


def _snapshot_identity(snapshot: ResearchDataSnapshot) -> None:
    if not isinstance(snapshot, ResearchDataSnapshot):
        raise AshareObservationLedgerContractError("research_snapshot_invalid")
    if (
        not snapshot.datasets
        or any(
            not isinstance(dataset, ResearchDatasetSnapshot)
            for dataset in snapshot.datasets
        )
        or len({dataset.dataset_id for dataset in snapshot.datasets})
        != len(snapshot.datasets)
    ):
        raise AshareObservationLedgerContractError("research_snapshot_invalid")
    _nonempty(snapshot.profile_id, field_name="profile_id")
    _sha256(snapshot.profile_contract_sha256, field_name="profile_contract_sha256")
    _nonempty(snapshot.catalog_version, field_name="catalog_version")
    _aware_utc(snapshot.decision_as_of, field_name="decision_as_of")
    if (
        snapshot.execution_eligible is not True
        or snapshot.historical_pit_eligible is not False
        or snapshot.blocking_reasons
        or any(
            dataset.catalog_version != snapshot.catalog_version
            or dataset.evidence_action != "accept"
            or dataset.eligible is not True
            or dataset.source_proof_complete is not True
            or dataset.observation_mode != "current_observation"
            or dataset.historical_pit_eligible is not False
            or dataset.next_cursor is not None
            for dataset in snapshot.datasets
        )
    ):
        raise AshareObservationLedgerContractError(
            "research_snapshot_not_observation_eligible"
        )
    for dataset in snapshot.datasets:
        if (
            not isinstance(dataset.receipt_id, str)
            or not dataset.receipt_id
            or any(
                not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
                for value in (
                    dataset.lineage_sha256,
                    dataset.source_proof_sha256,
                    dataset.identity_sha256,
                    dataset.row_observation_sha256,
                    dataset.pagination_trace_sha256,
                    dataset.pagination_semantic_sha256,
                    dataset.page_request_set_sha256,
                    dataset.page_response_set_sha256,
                    dataset.cursor_chain_sha256,
                    dataset.response_sha256,
                )
            )
        ):
            raise AshareObservationLedgerContractError(
                "research_snapshot_source_identity_invalid"
            )
    expected = _sha256_value(
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
    if snapshot.snapshot_sha256 != expected:
        raise AshareObservationLedgerContractError("snapshot_sha256_mismatch")


def observation_membership_source_symbols(
    snapshot: ResearchDataSnapshot,
    *,
    observation_session: str,
) -> tuple[str, ...]:
    """Return the exact daily/master union represented by one membership ledger.

    The security-master side is optional only for isolated legacy fixtures.  The
    Phase-1 runtime profile requires it, which prevents suspended or otherwise
    missing daily rows from silently disappearing from observation coverage.
    """

    matches = [
        dataset
        for dataset in snapshot.datasets
        if dataset.row_event_time_field == "trade_date"
        and dataset.row_event_time_format == "yyyymmdd"
        and dataset.row_event_time_semantic == "session"
        and {"ts_code", "trade_date"}.issubset(dataset.identity_fields)
    ]
    if len(matches) != 1:
        raise AshareObservationLedgerContractError("daily_dataset_contract_invalid")
    daily = matches[0]
    try:
        rows = daily.decoded_rows()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AshareObservationLedgerContractError(
            "daily_dataset_rows_invalid"
        ) from exc
    if len(rows) != daily.row_count or not rows:
        raise AshareObservationLedgerContractError("daily_dataset_rows_invalid")
    daily_symbols: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("trade_date") != observation_session:
            raise AshareObservationLedgerContractError(
                "daily_observation_session_mismatch"
            )
        symbol = row.get("ts_code")
        if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
            raise AshareObservationLedgerContractError("daily_symbol_invalid")
        daily_symbols.append(symbol)
    if len(set(daily_symbols)) != len(daily_symbols):
        raise AshareObservationLedgerContractError("daily_symbol_duplicate")

    master_matches = [
        dataset
        for dataset in snapshot.datasets
        if dataset is not daily and dataset.identity_fields == ("ts_code",)
    ]
    if len(master_matches) > 1:
        raise AshareObservationLedgerContractError(
            "security_master_dataset_contract_invalid"
        )
    master_symbols: list[str] = []
    if master_matches:
        master = master_matches[0]
        try:
            master_rows = master.decoded_rows()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AshareObservationLedgerContractError(
                "security_master_dataset_rows_invalid"
            ) from exc
        if len(master_rows) != master.row_count or not master_rows:
            raise AshareObservationLedgerContractError(
                "security_master_dataset_rows_invalid"
            )
        for row in master_rows:
            if not isinstance(row, Mapping) or not {
                "ts_code",
                "name",
                "list_status",
                "list_date",
            }.issubset(row):
                raise AshareObservationLedgerContractError(
                    "security_master_dataset_rows_invalid"
                )
            symbol = row.get("ts_code")
            if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
                raise AshareObservationLedgerContractError(
                    "security_master_symbol_invalid"
                )
            master_symbols.append(symbol)
        if len(set(master_symbols)) != len(master_symbols):
            raise AshareObservationLedgerContractError(
                "security_master_symbol_duplicate"
            )
    return tuple(sorted(set(daily_symbols).union(master_symbols)))


def _canonical_records(
    records: Sequence[AshareObservationMembershipRecord],
) -> tuple[AshareObservationMembershipRecord, ...]:
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise AshareObservationLedgerContractError("records_invalid")
    if not records or any(
        not isinstance(item, AshareObservationMembershipRecord) for item in records
    ):
        raise AshareObservationLedgerContractError("records_invalid")
    symbols = [item.symbol for item in records]
    if len(set(symbols)) != len(symbols):
        raise AshareObservationLedgerContractError("duplicate_symbol")
    return tuple(sorted(records, key=lambda item: item.symbol))


def _validate_observation_receipt(
    receipt: Mapping[str, Any],
    *,
    snapshot: ResearchDataSnapshot,
    records: tuple[AshareObservationMembershipRecord, ...],
) -> tuple[str, str, str]:
    if not isinstance(receipt, Mapping):
        raise AshareObservationLedgerContractError("observation_receipt_invalid")
    unsigned = dict(receipt)
    claimed = unsigned.pop("receipt_sha256", None)
    if not isinstance(claimed, str) or claimed != _sha256_value(unsigned):
        raise AshareObservationLedgerContractError(
            "observation_receipt_sha256_mismatch"
        )
    # This is a strict compatibility read of the frozen aggregate receipt.
    # Keeping the wire ID local prevents a runtime import cycle when the
    # observation writer later composes this ledger.
    if receipt.get("schema_id") != _COMPAT_OBSERVATION_RECEIPT_SCHEMA_ID:
        raise AshareObservationLedgerContractError("observation_receipt_schema_invalid")
    if (
        receipt.get("mode") != "observation_only"
        or receipt.get("marketgraph_mode") != "mg_off"
        or receipt.get("real_trading_enabled") is not False
        or receipt.get("historical_pit_eligible") is not False
        or receipt.get("execution_authority") is not False
    ):
        raise AshareObservationLedgerContractError(
            "observation_receipt_authority_flags_invalid"
        )
    if receipt.get("profile_id") != snapshot.profile_id:
        raise AshareObservationLedgerContractError("observation_profile_mismatch")
    if receipt.get("catalog_version") != snapshot.catalog_version:
        raise AshareObservationLedgerContractError("observation_catalog_mismatch")
    if receipt.get("decision_as_of") != snapshot.decision_as_of:
        raise AshareObservationLedgerContractError(
            "observation_decision_as_of_mismatch"
        )
    if receipt.get("snapshot_sha256") != snapshot.snapshot_sha256:
        raise AshareObservationLedgerContractError("observation_snapshot_mismatch")
    probe_sha256 = _sha256(
        receipt.get("probe_receipt_sha256"),
        field_name="probe_receipt_sha256",
    )
    universe_sha256 = _sha256(
        receipt.get("tradable_universe_sha256"),
        field_name="tradable_universe_sha256",
    )
    observed = [item.symbol for item in records if item.disposition == "observed"]
    excluded = [item for item in records if item.disposition == "excluded"]
    universe_count = receipt.get("tradable_universe_count")
    if (
        isinstance(universe_count, bool)
        or not isinstance(universe_count, int)
        or universe_count <= 0
        or not observed
        or universe_count != len(observed)
        or universe_sha256 != _sha256_value(observed)
    ):
        raise AshareObservationLedgerContractError("universe_membership_mismatch")
    expected_excluded = dict(
        sorted(Counter(item.reason_code for item in excluded).items())
    )
    if receipt.get("excluded_reason_counts") != expected_excluded:
        raise AshareObservationLedgerContractError("excluded_reason_counts_mismatch")
    return claimed, probe_sha256, universe_sha256


def _artifact_payload(
    *,
    observation_session: str,
    snapshot: ResearchDataSnapshot,
    observation_receipt: Mapping[str, Any],
    records: Sequence[AshareObservationMembershipRecord],
) -> tuple[AshareObservationMembershipArtifact, dict[str, Any]]:
    session = _session(observation_session)
    _snapshot_identity(snapshot)
    decision = _aware_utc(snapshot.decision_as_of, field_name="decision_as_of")
    if not _session_is_not_after_decision(session, decision):
        raise AshareObservationLedgerContractError(
            "observation_session_decision_mismatch"
        )
    canonical_records = _canonical_records(records)
    if tuple(item.symbol for item in canonical_records) != (
        observation_membership_source_symbols(
            snapshot,
            observation_session=session,
        )
    ):
        raise AshareObservationLedgerContractError("daily_symbol_membership_mismatch")
    receipt_sha256, probe_sha256, universe_sha256 = _validate_observation_receipt(
        observation_receipt,
        snapshot=snapshot,
        records=canonical_records,
    )
    unsigned: dict[str, Any] = {
        "schema_id": ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID,
        "observation_session": session,
        "decision_as_of": snapshot.decision_as_of,
        "profile_id": snapshot.profile_id,
        "profile_contract_sha256": snapshot.profile_contract_sha256,
        "catalog_version": snapshot.catalog_version,
        "catalog_version_sha256": _sha256_value(snapshot.catalog_version),
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe_sha256,
        "observation_receipt_sha256": receipt_sha256,
        "universe_sha256": universe_sha256,
        "records": [item.to_dict() for item in canonical_records],
        "label_horizons": list(LABEL_HORIZONS),
        "historical_pit_eligible": False,
        "learning_eligible": False,
        "performance_eligible": False,
        "promotion_eligible": False,
        "real_trading_enabled": False,
    }
    content_sha256 = _sha256_value(unsigned)
    payload = {**unsigned, "content_sha256": content_sha256}
    artifact = _decode_artifact(payload)
    return artifact, payload


def build_ashare_observation_membership_artifact(
    *,
    observation_session: str,
    research_snapshot: ResearchDataSnapshot,
    observation_receipt: Mapping[str, Any],
    records: Sequence[AshareObservationMembershipRecord],
) -> AshareObservationMembershipArtifact:
    """Build and validate one membership artifact without performing I/O."""

    artifact, _payload = _artifact_payload(
        observation_session=observation_session,
        snapshot=research_snapshot,
        observation_receipt=observation_receipt,
        records=records,
    )
    return artifact


def _decode_record(raw: object) -> AshareObservationMembershipRecord:
    if not isinstance(raw, Mapping) or set(raw) != _RECORD_KEYS:
        raise AshareObservationLedgerCorruption("record_fields_invalid")
    try:
        return AshareObservationMembershipRecord(
            symbol=raw.get("symbol"),  # type: ignore[arg-type]
            disposition=raw.get("disposition"),  # type: ignore[arg-type]
            reason_code=raw.get("reason_code"),  # type: ignore[arg-type]
        )
    except AshareObservationLedgerContractError as exc:
        raise AshareObservationLedgerCorruption("record_contract_invalid") from exc


def _decode_artifact(raw: object) -> AshareObservationMembershipArtifact:
    if not isinstance(raw, Mapping) or set(raw) != _ARTIFACT_KEYS:
        raise AshareObservationLedgerCorruption("artifact_fields_invalid")
    if raw.get("schema_id") != ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID:
        raise AshareObservationLedgerCorruption("artifact_schema_invalid")
    unsigned = dict(raw)
    claimed = unsigned.pop("content_sha256", None)
    if not isinstance(claimed, str) or claimed != _sha256_value(unsigned):
        raise AshareObservationLedgerCorruption("artifact_hash_mismatch")
    try:
        session = _session(raw.get("observation_session"))
        decision = _nonempty(raw.get("decision_as_of"), field_name="decision_as_of")
        profile_id = _nonempty(raw.get("profile_id"), field_name="profile_id")
        profile_sha = _sha256(
            raw.get("profile_contract_sha256"),
            field_name="profile_contract_sha256",
        )
        catalog = _nonempty(raw.get("catalog_version"), field_name="catalog_version")
        catalog_sha = _sha256(
            raw.get("catalog_version_sha256"),
            field_name="catalog_version_sha256",
        )
        snapshot_sha = _sha256(raw.get("snapshot_sha256"), field_name="snapshot_sha256")
        probe_sha = _sha256(
            raw.get("probe_receipt_sha256"),
            field_name="probe_receipt_sha256",
        )
        receipt_sha = _sha256(
            raw.get("observation_receipt_sha256"),
            field_name="observation_receipt_sha256",
        )
        universe_sha = _sha256(raw.get("universe_sha256"), field_name="universe_sha256")
    except AshareObservationLedgerContractError as exc:
        raise AshareObservationLedgerCorruption("artifact_identity_invalid") from exc
    if catalog_sha != _sha256_value(catalog):
        raise AshareObservationLedgerCorruption("catalog_hash_mismatch")
    try:
        decision_instant = _aware_utc(decision, field_name="decision_as_of")
    except AshareObservationLedgerContractError as exc:
        raise AshareObservationLedgerCorruption("decision_as_of_invalid") from exc
    if not _session_is_not_after_decision(session, decision_instant):
        raise AshareObservationLedgerCorruption("decision_session_mismatch")
    flags = (
        raw.get("historical_pit_eligible"),
        raw.get("learning_eligible"),
        raw.get("performance_eligible"),
        raw.get("promotion_eligible"),
        raw.get("real_trading_enabled"),
    )
    if flags != (False, False, False, False, False):
        raise AshareObservationLedgerCorruption("artifact_authority_flag_invalid")
    if raw.get("label_horizons") != list(LABEL_HORIZONS):
        raise AshareObservationLedgerCorruption("label_horizons_invalid")
    raw_records = raw.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise AshareObservationLedgerCorruption("records_invalid")
    records = tuple(_decode_record(item) for item in raw_records)
    if records != tuple(sorted(records, key=lambda item: item.symbol)):
        raise AshareObservationLedgerCorruption("records_not_canonical")
    if len({item.symbol for item in records}) != len(records):
        raise AshareObservationLedgerCorruption("duplicate_symbol")
    observed = [item.symbol for item in records if item.disposition == "observed"]
    if universe_sha != _sha256_value(observed):
        raise AshareObservationLedgerCorruption("universe_hash_mismatch")
    return AshareObservationMembershipArtifact(
        observation_session=session,
        decision_as_of=decision,
        profile_id=profile_id,
        profile_contract_sha256=profile_sha,
        catalog_version=catalog,
        catalog_version_sha256=catalog_sha,
        snapshot_sha256=snapshot_sha,
        probe_receipt_sha256=probe_sha,
        observation_receipt_sha256=receipt_sha,
        universe_sha256=universe_sha,
        records=records,
        label_horizons=LABEL_HORIZONS,
        historical_pit_eligible=False,
        learning_eligible=False,
        performance_eligible=False,
        promotion_eligible=False,
        real_trading_enabled=False,
        content_sha256=claimed,
    )


def _session_identity(observation_session: str) -> str:
    return _sha256_value({"observation_session": observation_session})


def _binding_payload(
    artifact: AshareObservationMembershipArtifact,
) -> dict[str, Any]:
    unsigned = {
        "schema_id": ASHARE_OBSERVATION_MEMBERSHIP_BINDING_SCHEMA_ID,
        "observation_session": artifact.observation_session,
        "decision_as_of": artifact.decision_as_of,
        "session_identity_sha256": _session_identity(artifact.observation_session),
        "artifact_content_sha256": artifact.content_sha256,
        "snapshot_sha256": artifact.snapshot_sha256,
        "observation_receipt_sha256": artifact.observation_receipt_sha256,
    }
    return {**unsigned, "content_sha256": _sha256_value(unsigned)}


def _decode_binding(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _BINDING_KEYS:
        raise AshareObservationLedgerCorruption("binding_fields_invalid")
    if raw.get("schema_id") != ASHARE_OBSERVATION_MEMBERSHIP_BINDING_SCHEMA_ID:
        raise AshareObservationLedgerCorruption("binding_schema_invalid")
    unsigned = dict(raw)
    claimed = unsigned.pop("content_sha256", None)
    if not isinstance(claimed, str) or claimed != _sha256_value(unsigned):
        raise AshareObservationLedgerCorruption("binding_hash_mismatch")
    try:
        session = _session(raw.get("observation_session"))
        decision = _nonempty(raw.get("decision_as_of"), field_name="decision_as_of")
        for name in (
            "session_identity_sha256",
            "artifact_content_sha256",
            "snapshot_sha256",
            "observation_receipt_sha256",
        ):
            _sha256(raw.get(name), field_name=name)
    except AshareObservationLedgerContractError as exc:
        raise AshareObservationLedgerCorruption("binding_identity_invalid") from exc
    if raw.get("session_identity_sha256") != _session_identity(session):
        raise AshareObservationLedgerCorruption("binding_identity_mismatch")
    try:
        decision_instant = _aware_utc(decision, field_name="decision_as_of")
    except AshareObservationLedgerContractError as exc:
        raise AshareObservationLedgerCorruption("binding_identity_invalid") from exc
    if not _session_is_not_after_decision(session, decision_instant):
        raise AshareObservationLedgerCorruption("binding_identity_mismatch")
    return dict(raw)


def _assert_safe_path(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AshareObservationLedgerCorruption("path_unreadable") from exc
        if stat.S_ISLNK(mode):
            raise AshareObservationLedgerCorruption("symlink_forbidden")
        if current != absolute and not stat.S_ISDIR(mode):
            raise AshareObservationLedgerCorruption("parent_not_directory")


def _no_follow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _directory_flag() -> int:
    return getattr(os, "O_DIRECTORY", 0)


def _trusted_file(fd: int, path: Path, *, kind: str) -> os.stat_result:
    try:
        descriptor_stat = os.fstat(fd)
        path_stat = path.lstat()
    except OSError as exc:
        raise AshareObservationLedgerCorruption(f"{kind}_identity_unavailable") from exc
    if not stat.S_ISREG(descriptor_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise AshareObservationLedgerCorruption(f"{kind}_not_regular")
    if descriptor_stat.st_nlink != 1 or path_stat.st_nlink != 1:
        raise AshareObservationLedgerCorruption(f"{kind}_hardlink_forbidden")
    if stat.S_IMODE(descriptor_stat.st_mode) != 0o600:
        raise AshareObservationLedgerCorruption(f"{kind}_mode_invalid")
    if descriptor_stat.st_uid != os.geteuid():
        raise AshareObservationLedgerCorruption(f"{kind}_owner_invalid")
    if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
        path_stat.st_dev,
        path_stat.st_ino,
    ):
        raise AshareObservationLedgerCorruption(f"{kind}_identity_changed")
    return descriptor_stat


def _fsync_directory(path: Path) -> None:
    _assert_safe_path(path)
    try:
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY | _directory_flag() | _no_follow(),
        )
    except OSError as exc:
        raise AshareObservationLedgerCorruption("directory_sync_failed") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise AshareObservationLedgerCorruption("root_not_directory")
        os.fsync(descriptor)
    except OSError as exc:
        raise AshareObservationLedgerCorruption("directory_sync_failed") from exc
    finally:
        os.close(descriptor)


class FileAshareObservationMembershipLedger:
    """CAS-backed immutable membership artifacts and per-session bindings."""

    def __init__(self, root: Path | str) -> None:
        if not isinstance(root, (str, os.PathLike)) or not os.fspath(root):
            raise ValueError("observation ledger root must be explicitly configured")
        raw = Path(os.fspath(root))
        if ".." in raw.parts:
            raise ValueError("observation ledger root path_traversal forbidden")
        self.root = Path(os.path.abspath(os.fspath(raw)))
        _assert_safe_path(self.root)

    def _prepare_root(self) -> None:
        _assert_safe_path(self.root)
        existed = self.root.exists()
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise AshareObservationLedgerCorruption("root_unavailable") from exc
        _assert_safe_path(self.root)
        metadata = self.root.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise AshareObservationLedgerCorruption("root_not_directory")
        if metadata.st_uid != os.geteuid():
            raise AshareObservationLedgerCorruption("root_owner_invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            if existed:
                raise AshareObservationLedgerCorruption("root_mode_invalid")
            os.chmod(self.root, 0o700)
        if not existed:
            _fsync_directory(self.root.parent)
        _fsync_directory(self.root)

    def _artifact_path(self, content_sha256: str) -> Path:
        try:
            value = _sha256(content_sha256, field_name="content_sha256")
        except AshareObservationLedgerContractError as exc:
            raise AshareObservationLedgerCorruption("content_sha256_invalid") from exc
        return self.root / f"artifact-{value}.json"

    def _binding_path(self, observation_session: str) -> Path:
        try:
            identity = _session_identity(_session(observation_session))
        except AshareObservationLedgerContractError as exc:
            raise AshareObservationLedgerCorruption(
                "observation_session_invalid"
            ) from exc
        return self.root / f"session-{identity}.json"

    def _lock_path(self, observation_session: str) -> Path:
        return self.root / f".session-{_session_identity(observation_session)}.lock"

    @contextmanager
    def _locked(self, observation_session: str, *, exclusive: bool) -> Iterator[None]:
        self._prepare_root()
        path = self._lock_path(observation_session)
        _assert_safe_path(path)
        try:
            descriptor = os.open(
                os.fspath(path),
                os.O_RDWR | os.O_CREAT | _no_follow(),
                0o600,
            )
        except OSError as exc:
            raise AshareObservationLedgerCorruption("lock_unavailable") from exc
        try:
            _trusted_file(descriptor, path, kind="lock")
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
            _trusted_file(descriptor, path, kind="lock")
            yield
            _trusted_file(descriptor, path, kind="lock")
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_json(self, path: Path, *, kind: str) -> dict[str, Any]:
        _assert_safe_path(path)
        try:
            descriptor = os.open(os.fspath(path), os.O_RDONLY | _no_follow())
        except OSError as exc:
            raise AshareObservationLedgerCorruption(f"{kind}_unavailable") from exc
        try:
            before = _trusted_file(descriptor, path, kind=kind)
            if before.st_size > _MAX_FILE_BYTES:
                raise AshareObservationLedgerCorruption(f"{kind}_too_large")
            chunks: list[bytes] = []
            remaining = _MAX_FILE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = _trusted_file(descriptor, path, kind=kind)
            if (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise AshareObservationLedgerCorruption(f"{kind}_changed_during_read")
        finally:
            os.close(descriptor)
        try:
            text = b"".join(chunks).decode("utf-8")
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AshareObservationLedgerCorruption(f"{kind}_malformed") from exc
        try:
            canonical = _canonical_json(raw)
        except AshareObservationLedgerContractError as exc:
            raise AshareObservationLedgerCorruption(f"{kind}_not_canonical") from exc
        if not isinstance(raw, dict) or canonical != text:
            raise AshareObservationLedgerCorruption(f"{kind}_not_canonical")
        return raw

    def _recover_atomic_publish_window(
        self,
        path: Path,
        *,
        kind: str,
        observation_session: str,
        expected_payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Finish a narrowly identifiable link-then-unlink publication.

        Callers must hold this session's exclusive ledger lock.  Recovery is
        deliberately limited to the exact state produced by ``_atomic_create``:
        one canonical final file and one canonical UUID-shaped temporary name
        are hard links to the same regular, owner-only inode.  Every ambiguous
        state remains untouched and fails closed.
        """

        _assert_safe_path(path)
        session = _session(observation_session)
        try:
            descriptor = os.open(os.fspath(path), os.O_RDONLY | _no_follow())
        except OSError as exc:
            raise AshareObservationLedgerCorruption(
                f"{kind}_recovery_unavailable"
            ) from exc
        try:
            try:
                descriptor_stat = os.fstat(descriptor)
                path_stat = path.lstat()
            except OSError as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_identity_unavailable"
                ) from exc
            if descriptor_stat.st_nlink == 1 and path_stat.st_nlink == 1:
                return
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_not_regular"
                )
            if (
                descriptor_stat.st_nlink != 2
                or path_stat.st_nlink != 2
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_hardlink_count_invalid"
                )
            if (
                stat.S_IMODE(descriptor_stat.st_mode) != 0o600
                or stat.S_IMODE(path_stat.st_mode) != 0o600
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_mode_invalid"
                )
            if (
                descriptor_stat.st_uid != os.geteuid()
                or path_stat.st_uid != os.geteuid()
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_owner_invalid"
                )
            final_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
            if final_identity != (path_stat.st_dev, path_stat.st_ino):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_identity_changed"
                )
            if (
                descriptor_stat.st_size <= 0
                or descriptor_stat.st_size > _MAX_FILE_BYTES
                or path_stat.st_size != descriptor_stat.st_size
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_size_invalid"
                )

            aliases: list[Path] = []
            try:
                with os.scandir(self.root) as directory_entries:
                    entries = tuple(directory_entries)
            except OSError as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_scan_failed"
                ) from exc
            for entry in entries:
                if entry.name == path.name:
                    continue
                candidate = self.root / entry.name
                try:
                    candidate_stat = candidate.lstat()
                except OSError as exc:
                    raise AshareObservationLedgerCorruption(
                        f"{kind}_recovery_alias_unreadable"
                    ) from exc
                if _ATOMIC_TMP_RE.fullmatch(entry.name) and stat.S_ISLNK(
                    candidate_stat.st_mode
                ):
                    raise AshareObservationLedgerCorruption(
                        f"{kind}_recovery_tmp_symlink_forbidden"
                    )
                if (
                    stat.S_ISREG(candidate_stat.st_mode)
                    and (candidate_stat.st_dev, candidate_stat.st_ino)
                    == final_identity
                ):
                    aliases.append(candidate)
            if len(aliases) != 1:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_alias_count_invalid"
                )
            alias = aliases[0]
            if _ATOMIC_TMP_RE.fullmatch(alias.name) is None:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_alias_name_invalid"
                )
            try:
                alias_stat = alias.lstat()
            except OSError as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_alias_unreadable"
                ) from exc
            if not stat.S_ISREG(alias_stat.st_mode):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_alias_not_regular"
                )
            if (
                alias_stat.st_uid != os.geteuid()
                or stat.S_IMODE(alias_stat.st_mode) != 0o600
                or alias_stat.st_nlink != 2
                or alias_stat.st_size != descriptor_stat.st_size
                or (alias_stat.st_dev, alias_stat.st_ino) != final_identity
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_alias_identity_invalid"
                )

            chunks: list[bytes] = []
            remaining = descriptor_stat.st_size
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            try:
                after_read = os.fstat(descriptor)
            except OSError as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_identity_unavailable"
                ) from exc
            if (
                len(encoded) != descriptor_stat.st_size
                or (
                    after_read.st_size,
                    after_read.st_mtime_ns,
                    after_read.st_ctime_ns,
                    after_read.st_nlink,
                    after_read.st_dev,
                    after_read.st_ino,
                )
                != (
                    descriptor_stat.st_size,
                    descriptor_stat.st_mtime_ns,
                    descriptor_stat.st_ctime_ns,
                    descriptor_stat.st_nlink,
                    descriptor_stat.st_dev,
                    descriptor_stat.st_ino,
                )
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_changed_during_read"
                )
            try:
                text = encoded.decode("utf-8")
                raw = json.loads(text)
                canonical = _canonical_json(raw)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                AshareObservationLedgerContractError,
            ) as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_not_canonical"
                ) from exc
            if not isinstance(raw, dict) or canonical != text:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_not_canonical"
                )
            if expected_payload is not None and raw != dict(expected_payload):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_payload_mismatch"
                )
            if kind == "artifact":
                decoded = _decode_artifact(raw)
                if (
                    decoded.observation_session != session
                    or path != self._artifact_path(decoded.content_sha256)
                ):
                    raise AshareObservationLedgerCorruption(
                        "artifact_recovery_identity_mismatch"
                    )
            elif kind == "binding":
                decoded_binding = _decode_binding(raw)
                if (
                    decoded_binding["observation_session"] != session
                    or path != self._binding_path(session)
                ):
                    raise AshareObservationLedgerCorruption(
                        "binding_recovery_identity_mismatch"
                    )
            else:
                raise AshareObservationLedgerCorruption(
                    "atomic_recovery_kind_invalid"
                )

            try:
                current_final = path.lstat()
                current_alias = alias.lstat()
            except OSError as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_identity_unavailable"
                ) from exc
            if (
                not stat.S_ISREG(current_final.st_mode)
                or not stat.S_ISREG(current_alias.st_mode)
                or current_final.st_uid != os.geteuid()
                or current_alias.st_uid != os.geteuid()
                or stat.S_IMODE(current_final.st_mode) != 0o600
                or stat.S_IMODE(current_alias.st_mode) != 0o600
                or (current_final.st_dev, current_final.st_ino) != final_identity
                or (current_alias.st_dev, current_alias.st_ino) != final_identity
                or current_final.st_nlink != 2
                or current_alias.st_nlink != 2
                or current_final.st_size != descriptor_stat.st_size
                or current_alias.st_size != descriptor_stat.st_size
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_identity_changed"
                )
            try:
                os.unlink(alias)
            except OSError as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_unlink_failed"
                ) from exc
            _fsync_directory(self.root)
            try:
                recovered_stat = path.lstat()
            except OSError as exc:
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_identity_unavailable"
                ) from exc
            if (
                not stat.S_ISREG(recovered_stat.st_mode)
                or (recovered_stat.st_dev, recovered_stat.st_ino) != final_identity
                or recovered_stat.st_nlink != 1
                or recovered_stat.st_uid != os.geteuid()
                or stat.S_IMODE(recovered_stat.st_mode) != 0o600
                or recovered_stat.st_size != descriptor_stat.st_size
            ):
                raise AshareObservationLedgerCorruption(
                    f"{kind}_recovery_postcondition_failed"
                )
        finally:
            os.close(descriptor)

    def _atomic_create(self, path: Path, payload: Mapping[str, Any]) -> None:
        _assert_safe_path(path)
        encoded = _canonical_json(payload).encode("utf-8")
        temporary = self.root / f".tmp-{uuid.uuid4().hex}.json"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                os.fspath(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow(),
                0o600,
            )
            _trusted_file(descriptor, temporary, kind="temporary")
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise AshareObservationLedgerCorruption("short_write")
                offset += written
            os.fsync(descriptor)
            _trusted_file(descriptor, temporary, kind="temporary")
            os.close(descriptor)
            descriptor = None
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
            _fsync_directory(self.root)
            published = os.open(os.fspath(path), os.O_RDONLY | _no_follow())
            try:
                _trusted_file(published, path, kind="published")
                os.fsync(published)
            finally:
                os.close(published)
        except AshareObservationLedgerCorruption:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            if self.root.exists():
                _fsync_directory(self.root)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            if self.root.exists():
                _fsync_directory(self.root)
            raise AshareObservationLedgerCorruption("atomic_write_failed") from exc

    def _read_artifact(
        self,
        content_sha256: str,
        *,
        observation_session: str,
        expected_payload: Mapping[str, Any] | None = None,
    ) -> tuple[AshareObservationMembershipArtifact, dict[str, Any]]:
        path = self._artifact_path(content_sha256)
        self._recover_atomic_publish_window(
            path,
            kind="artifact",
            observation_session=observation_session,
            expected_payload=expected_payload,
        )
        raw = self._read_json(
            path,
            kind="artifact",
        )
        artifact = _decode_artifact(raw)
        if artifact.content_sha256 != content_sha256:
            raise AshareObservationLedgerCorruption("artifact_identity_mismatch")
        return artifact, raw

    def compare_and_swap(
        self,
        *,
        observation_session: str,
        research_snapshot: ResearchDataSnapshot,
        observation_receipt: Mapping[str, Any],
        records: Sequence[AshareObservationMembershipRecord],
        expected_content_sha256: str | None,
    ) -> AshareObservationMembershipArtifact:
        """Publish one immutable session membership or validate exact replay."""

        artifact, payload = _artifact_payload(
            observation_session=observation_session,
            snapshot=research_snapshot,
            observation_receipt=observation_receipt,
            records=records,
        )
        if expected_content_sha256 is not None:
            _sha256(
                expected_content_sha256,
                field_name="expected_content_sha256",
            )
        binding_path = self._binding_path(artifact.observation_session)
        with self._locked(artifact.observation_session, exclusive=True):
            if binding_path.exists() or binding_path.is_symlink():
                self._recover_atomic_publish_window(
                    binding_path,
                    kind="binding",
                    observation_session=artifact.observation_session,
                    expected_payload=_binding_payload(artifact),
                )
                binding = _decode_binding(self._read_json(binding_path, kind="binding"))
                current = str(binding["artifact_content_sha256"])
                if current == artifact.content_sha256:
                    if expected_content_sha256 not in (None, current):
                        raise AshareObservationLedgerConflict(
                            "observation_ledger_compare_and_swap_failed"
                        )
                    recovered, raw = self._read_artifact(
                        current,
                        observation_session=artifact.observation_session,
                        expected_payload=payload,
                    )
                    if recovered != artifact or raw != payload:
                        raise AshareObservationLedgerCorruption(
                            "idempotent_replay_mismatch"
                        )
                    if binding != _binding_payload(artifact):
                        raise AshareObservationLedgerCorruption(
                            "binding_payload_mismatch"
                        )
                    return recovered
                if expected_content_sha256 not in (None, current):
                    raise AshareObservationLedgerConflict(
                        "observation_ledger_compare_and_swap_failed"
                    )
                raise AshareObservationLedgerConflict(
                    "observation_ledger_immutable_session_conflict"
                )
            if expected_content_sha256 is not None:
                raise AshareObservationLedgerConflict(
                    "observation_ledger_compare_and_swap_failed"
                )
            artifact_path = self._artifact_path(artifact.content_sha256)
            if artifact_path.exists() or artifact_path.is_symlink():
                recovered, raw = self._read_artifact(
                    artifact.content_sha256,
                    observation_session=artifact.observation_session,
                    expected_payload=payload,
                )
                if recovered != artifact or raw != payload:
                    raise AshareObservationLedgerCorruption("orphan_artifact_mismatch")
            else:
                self._atomic_create(artifact_path, payload)
                recovered, raw = self._read_artifact(
                    artifact.content_sha256,
                    observation_session=artifact.observation_session,
                    expected_payload=payload,
                )
                if recovered != artifact or raw != payload:
                    raise AshareObservationLedgerCorruption(
                        "published_artifact_mismatch"
                    )
            binding = _binding_payload(artifact)
            self._atomic_create(binding_path, binding)
            if (
                _decode_binding(self._read_json(binding_path, kind="binding"))
                != binding
            ):
                raise AshareObservationLedgerCorruption("published_binding_mismatch")
            return artifact

    def load_bound_session(
        self,
        *,
        observation_session: str,
    ) -> AshareObservationMembershipArtifact | None:
        """Load the immutable artifact bound to one trade session."""

        session = _session(observation_session)
        _assert_safe_path(self.root)
        if not self.root.exists():
            return None
        binding_path = self._binding_path(session)
        with self._locked(session, exclusive=True):
            if not binding_path.exists() and not binding_path.is_symlink():
                return None
            self._recover_atomic_publish_window(
                binding_path,
                kind="binding",
                observation_session=session,
            )
            binding = _decode_binding(self._read_json(binding_path, kind="binding"))
            artifact, _ = self._read_artifact(
                str(binding["artifact_content_sha256"]),
                observation_session=session,
            )
            if (
                artifact.observation_session != session
                or artifact.decision_as_of != binding["decision_as_of"]
                or artifact.snapshot_sha256 != binding["snapshot_sha256"]
                or artifact.observation_receipt_sha256
                != binding["observation_receipt_sha256"]
                or binding != _binding_payload(artifact)
            ):
                raise AshareObservationLedgerCorruption("binding_artifact_mismatch")
            return artifact


__all__ = [
    "ASHARE_OBSERVATION_MEMBERSHIP_BINDING_SCHEMA_ID",
    "ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID",
    "LABEL_HORIZONS",
    "OBSERVED_REASON_CODE",
    "AshareObservationLedgerConflict",
    "AshareObservationLedgerContractError",
    "AshareObservationLedgerCorruption",
    "AshareObservationMembershipArtifact",
    "AshareObservationMembershipRecord",
    "FileAshareObservationMembershipLedger",
    "build_ashare_observation_membership_artifact",
    "observation_membership_source_symbols",
]
