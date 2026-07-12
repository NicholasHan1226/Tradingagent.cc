#!/usr/bin/env python3
"""Fail-closed server-local MTM reconcile writer for both fresh 50k markets.

The writer never invents account state.  A-share accounting is reconstructed
from its fresh execution lineage, durable capital outbox, and strategy position
projection.  CNFutures accounting is reconstructed from the position snapshot
and the matching durable commit outbox/history.  The resulting immutable
canonical JSON is the only source passed to ``MarketCapitalLedger.mtm_reconcile``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.capital.market_ledger import (
    RECONCILE_SOURCE_SCHEMA_VERSION,
    MarketCapitalLedger,
    MarketCapitalLedgerError,
    ReconcileManifest,
    _reconcile_trade_date_for_pit,
    market_capital_root,
)
from shared.capital.market_policy import MarketPolicy
from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
    ExecutionLineageError,
    require_execution_lineage,
)
from shared.review.sample_journal import JournalError, SampleJournal


CN_TZ = timezone(timedelta(hours=8))
ALLOWED_PHASES = frozenset({"opening", "preopen", "ops"})
ASHARE_POSITION_FILENAME = "simulated_ashare_positions.json"
ASHARE_MANIFEST_FILENAME = "execution_lineage_manifest.json"
ASHARE_OUTBOX_FILENAME = "market_capital_outbox.json"
ASHARE_OUTBOX_SCHEMA = "2026-07-12.ashare-market-capital-outbox.v2"
CN_POSITION_FILENAME = "cn_futures_sim_positions.json"
CN_OUTBOX_FILENAME = "cn_futures_capital_outbox.json"
CN_OUTBOX_SCHEMA = "2026-07-12.cn-futures-capital-outbox.v3"
CN_POSITION_SCHEMA = "2026-07-12.cn-futures-position-snapshot.v2"
ASHARE_DAILY_MTM_EVIDENCE_SOURCE = "ashare_market_capital_reconcile"
ASHARE_DAILY_MTM_CLOSE_TIME = (15, 31)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ASHARE_CHECKSUM_KEYS = {
    "payload_sha256",
    "receipt_sha256",
    "trade_sha256",
    "checksum",
    "sha256",
}
LEGACY_AUTHORITY_KEYS = {
    "account_epoch",
    "capital_epoch",
    "epoch_id",
    "current_epoch_id",
    "previous_epoch_id",
    "master_capital_required",
    "master_capital_reference_id",
    "master_capital_reservation_id",
    "master_capital_event_id",
}


class MarketCapitalReconcileError(RuntimeError):
    """Raised before a reconcile event can be written."""


@dataclass(frozen=True)
class _FileRead:
    path: Path
    payload: bytes
    sha256: str
    fingerprint: tuple[int, int, int, int]


@dataclass(frozen=True)
class _JsonRead:
    file: _FileRead
    value: dict[str, Any]


@dataclass(frozen=True)
class _ExecutionState:
    market: str
    execution_lineage_id: str
    cash_balance_cny: float
    positions_market_value: dict[str, float]
    unrealized_pnl_cny: float
    position_margin_by_risk_unit: dict[str, float]
    positions_quantity_by_risk_unit: dict[str, int]
    positions_cost_basis_cny_by_risk_unit: dict[str, float]
    positions_entry_fee_cny_by_risk_unit: dict[str, float]
    position_entry_price_by_risk_unit: dict[str, float]
    position_side_by_risk_unit: dict[str, str]
    position_contract_multiplier_by_risk_unit: dict[str, float]
    position_contract_spec_sha256_by_risk_unit: dict[str, str]
    position_mark_price_by_risk_unit: dict[str, float]
    committed_event_ids: frozenset[str]
    commit_requests_by_event_id: dict[str, tuple[str, dict[str, Any]]]
    proven_active_reservation_ids: frozenset[str]
    source_reads: tuple[_FileRead, ...]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha(
    payload: Mapping[str, Any],
    *,
    excluded: set[str] | None = None,
) -> str:
    body = {
        key: value for key, value in payload.items() if key not in (excluded or set())
    }
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _read_regular_file_once(path: Path) -> _FileRead:
    candidate = Path(path).expanduser()
    try:
        before_path = candidate.lstat()
    except OSError as exc:
        raise MarketCapitalReconcileError(
            f"reconcile_source_unavailable:{candidate}"
        ) from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise MarketCapitalReconcileError(
            f"reconcile_source_not_regular_file:{candidate}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise MarketCapitalReconcileError(
            f"reconcile_source_open_failed:{candidate}"
        ) from exc
    try:
        before = os.fstat(fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        after_path = candidate.lstat()
    except OSError as exc:
        raise MarketCapitalReconcileError(
            f"reconcile_source_torn_read:{candidate}"
        ) from exc
    fingerprints = {
        (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
        for row in (before_path, before, after, after_path)
    }
    if len(fingerprints) != 1:
        raise MarketCapitalReconcileError(f"reconcile_source_torn_read:{candidate}")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise MarketCapitalReconcileError(f"reconcile_source_torn_read:{candidate}")
    fingerprint = next(iter(fingerprints))
    return _FileRead(
        path=candidate.resolve(strict=True),
        payload=payload,
        sha256=_sha256_bytes(payload),
        fingerprint=fingerprint,
    )


def _stable_read_bytes(path: Path) -> _FileRead:
    first = _read_regular_file_once(path)
    second = _read_regular_file_once(path)
    if (
        first.fingerprint != second.fingerprint
        or first.sha256 != second.sha256
        or first.payload != second.payload
    ):
        raise MarketCapitalReconcileError(f"reconcile_source_torn_read:{Path(path)}")
    return second


def _stable_read_json(path: Path) -> _JsonRead:
    source = _stable_read_bytes(path)
    try:
        value = json.loads(source.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketCapitalReconcileError(
            f"reconcile_source_invalid_json:{source.path}"
        ) from exc
    if not isinstance(value, dict):
        raise MarketCapitalReconcileError(
            f"reconcile_source_mapping_required:{source.path}"
        )
    return _JsonRead(file=source, value=value)


def _reject_legacy_authority(payload: Any, *, location: str = "source") -> None:
    if isinstance(payload, Mapping):
        for raw_key, value in payload.items():
            key = str(raw_key).strip().lower()
            if key in LEGACY_AUTHORITY_KEYS or "account_epoch" in key:
                raise MarketCapitalReconcileError(
                    f"legacy_numeric_epoch_forbidden:{location}:{key}"
                )
            _reject_legacy_authority(value, location=f"{location}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            _reject_legacy_authority(value, location=f"{location}[{index}]")


def _strict_number(
    value: Any,
    *,
    field: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MarketCapitalReconcileError(f"invalid_{field}")
    number = float(value)
    if not math.isfinite(number):
        raise MarketCapitalReconcileError(f"invalid_{field}")
    if positive and number <= 0.0:
        raise MarketCapitalReconcileError(f"invalid_{field}")
    if nonnegative and number < 0.0:
        raise MarketCapitalReconcileError(f"invalid_{field}")
    return number


def _strict_int(value: Any, *, field: str, nonzero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MarketCapitalReconcileError(f"invalid_{field}")
    if nonzero and value == 0:
        raise MarketCapitalReconcileError(f"invalid_{field}")
    return int(value)


def _trade_date(value: Any) -> str:
    normalized = str(value or "").strip().replace("-", "")
    if len(normalized) != 8 or not normalized.isdigit():
        raise MarketCapitalReconcileError("invalid_trade_date")
    return normalized


def _aware_time(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketCapitalReconcileError(f"invalid_{field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketCapitalReconcileError(f"timezone_required_{field}")
    return parsed


def _validate_source_time(
    value: Any,
    *,
    trade_date: str,
    pit: datetime,
    field: str,
    market: str = "ashare",
) -> None:
    source_time = _aware_time(value, field=field)
    if _reconcile_trade_date_for_pit(market, source_time) != trade_date:
        raise MarketCapitalReconcileError(f"{field}_trade_date_mismatch")
    if source_time > pit:
        raise MarketCapitalReconcileError(f"{field}_after_reconcile_pit")


def _validate_immutable_source_time(
    value: Any,
    *,
    pit: datetime,
    field: str,
) -> None:
    """Validate an append-only sidecar without requiring a daily rewrite."""

    source_time = _aware_time(value, field=field)
    if source_time > pit:
        raise MarketCapitalReconcileError(f"{field}_after_reconcile_pit")


def _validate_commit_times(
    request: Mapping[str, Any],
    *,
    pit: datetime,
    source: str,
) -> None:
    point_in_time = _aware_time(
        request.get("point_in_time_as_of"),
        field=f"{source}_point_in_time_as_of",
    )
    filled_at = _aware_time(request.get("filled_at"), field=f"{source}_filled_at")
    if point_in_time > filled_at:
        raise MarketCapitalReconcileError(f"{source}_point_in_time_after_fill")
    if point_in_time.astimezone(CN_TZ).strftime("%Y%m%d") != filled_at.astimezone(
        CN_TZ
    ).strftime("%Y%m%d"):
        raise MarketCapitalReconcileError(f"{source}_fill_trade_date_mismatch")
    if filled_at > pit:
        raise MarketCapitalReconcileError(f"{source}_after_reconcile_pit")


def _validate_commit_identity(
    action_type: str,
    *,
    request: Mapping[str, Any],
    event: Mapping[str, Any],
) -> None:
    if action_type == "release":
        keys = (
            "reference_id",
            "reservation_id",
            "amount_cny",
            "reason",
        )
        if event.get("event_type") != "release" or {
            key: request.get(key) for key in keys
        } != {key: event.get(key) for key in keys}:
            raise MarketCapitalReconcileError("execution_commit_fact_mismatch")
        return
    identity_builders = {
        "fill_commit": MarketCapitalLedger._fill_identity,
        "ashare_sell_commit": MarketCapitalLedger._ashare_sell_identity,
        "position_close_commit": MarketCapitalLedger._position_close_identity,
    }
    builder = identity_builders.get(action_type)
    if builder is None or event.get("event_type") != action_type:
        raise MarketCapitalReconcileError("execution_commit_event_type_mismatch")
    if builder(request) != builder(event):
        raise MarketCapitalReconcileError("execution_commit_fact_mismatch")


def _validate_commit_events_against_ledger(
    ledger: MarketCapitalLedger,
    execution: _ExecutionState,
    *,
    expected_head_event_id: str,
    expected_head_checksum: str,
) -> None:
    source = _stable_read_bytes(ledger.events_path)
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(source.payload.splitlines(), start=1):
        if not raw_line.strip():
            raise MarketCapitalReconcileError(
                f"capital_event_log_blank_line:{line_number}"
            )
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketCapitalReconcileError(
                f"capital_event_log_invalid_json:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise MarketCapitalReconcileError(
                f"capital_event_log_invalid_row:{line_number}"
            )
        rows.append(row)
    if not rows:
        raise MarketCapitalReconcileError("capital_event_log_empty")
    if (
        str(rows[-1].get("event_id") or "") != expected_head_event_id
        or str(rows[-1].get("checksum") or "") != expected_head_checksum
    ):
        raise MarketCapitalReconcileError("capital_head_changed_during_source_read")
    by_event_id = {
        str(row.get("event_id") or ""): row
        for row in rows
        if str(row.get("event_id") or "")
    }
    for event_id, (action_type, request) in sorted(
        execution.commit_requests_by_event_id.items()
    ):
        event = by_event_id.get(event_id)
        if event is None:
            raise MarketCapitalReconcileError(
                f"execution_commit_event_missing:{event_id}"
            )
        _validate_commit_identity(action_type, request=request, event=event)


def _ensure_sim_only() -> None:
    raw = str(os.environ.get("REAL_TRADING_ENABLED", "false")).strip().lower()
    if raw not in {"", "0", "false", "no", "off"}:
        raise MarketCapitalReconcileError("sim_only_real_trading_environment_rejected")


def _validate_sim_markers(payload: Mapping[str, Any], *, source: str) -> None:
    if payload.get("capital_layer") != "simulated":
        raise MarketCapitalReconcileError(f"{source}_capital_layer_not_simulated")
    if payload.get("account_type") != "simulated":
        raise MarketCapitalReconcileError(f"{source}_account_type_not_simulated")
    if payload.get("real_trading_enabled") is not False:
        raise MarketCapitalReconcileError(f"{source}_real_trading_enabled_rejected")


def _validate_mark_evidence(
    payload: Mapping[str, Any],
    *,
    expected_marks: Mapping[str, float],
    pit: datetime,
    market: str,
) -> None:
    evidence_by_symbol = payload.get("mark_evidence_by_symbol")
    if not isinstance(evidence_by_symbol, Mapping):
        raise MarketCapitalReconcileError(f"{market}_mark_evidence_missing")
    normalized_evidence = {
        str(symbol).strip().upper(): evidence
        for symbol, evidence in evidence_by_symbol.items()
    }
    if set(normalized_evidence) != set(expected_marks):
        raise MarketCapitalReconcileError(f"{market}_mark_evidence_set_mismatch")
    for symbol, expected_price in expected_marks.items():
        evidence = normalized_evidence.get(symbol)
        if not isinstance(evidence, Mapping):
            raise MarketCapitalReconcileError(f"{market}_mark_evidence_invalid")
        price = _strict_number(
            evidence.get("price"),
            field=f"{market}_mark_evidence_price:{symbol}",
            positive=True,
        )
        if not math.isclose(price, float(expected_price), abs_tol=1e-9):
            raise MarketCapitalReconcileError(f"{market}_mark_evidence_price_mismatch")
        observed = _aware_time(
            evidence.get("observed_at"),
            field=f"{market}_mark_observed_at:{symbol}",
        )
        point_in_time = _aware_time(
            evidence.get("point_in_time_as_of"),
            field=f"{market}_mark_point_in_time:{symbol}",
        )
        if observed > point_in_time or point_in_time > pit:
            raise MarketCapitalReconcileError(f"{market}_mark_evidence_after_pit")
        source = str(evidence.get("source") or "").strip()
        if (
            evidence.get("source_owner") != "SharedSignals"
            or "sharedsignals" not in source.lower()
            or evidence.get("real_trading_enabled") is not False
        ):
            raise MarketCapitalReconcileError(f"{market}_mark_evidence_source_invalid")
        source_row = evidence.get("source_row")
        source_row_sha = str(evidence.get("source_row_sha256") or "")
        if (
            not isinstance(source_row, Mapping)
            or not SHA256_RE.fullmatch(source_row_sha)
            or _canonical_sha(source_row) != source_row_sha
        ):
            raise MarketCapitalReconcileError(f"{market}_mark_evidence_lineage_invalid")


def _maps_close(left: Mapping[str, float], right: Mapping[str, float]) -> bool:
    return set(left) == set(right) and all(
        math.isclose(float(left[key]), float(right[key]), abs_tol=0.01) for key in left
    )


def _validate_ashare_source(
    root: Path,
    *,
    account: str,
    trade_date: str,
    pit: datetime,
) -> _ExecutionState:
    source_root = Path(root).expanduser()
    if source_root.name != ASHARE_EXECUTION_LINEAGE_ID:
        raise MarketCapitalReconcileError("ashare_execution_root_not_fresh_lineage")
    manifest_read = _stable_read_json(source_root / ASHARE_MANIFEST_FILENAME)
    snapshot_read = _stable_read_json(source_root / ASHARE_POSITION_FILENAME)
    outbox_read = _stable_read_json(source_root / ASHARE_OUTBOX_FILENAME)
    manifest = manifest_read.value
    snapshot = snapshot_read.value
    outbox = outbox_read.value
    for name, payload in (
        ("ashare_manifest", manifest),
        ("ashare_snapshot", snapshot),
        ("ashare_outbox", outbox),
    ):
        _reject_legacy_authority(payload, location=name)

    try:
        manifest_lineage = require_execution_lineage(manifest)
        snapshot_lineage = require_execution_lineage(snapshot)
        # The durable outbox owns its schema_version field, while retaining
        # the remaining immutable lineage fields.  Validate the lineage with
        # the manifest's execution schema restored for this check only.
        outbox_lineage = require_execution_lineage(
            {**outbox, "schema_version": manifest_lineage["schema_version"]}
        )
    except ExecutionLineageError as exc:
        raise MarketCapitalReconcileError(
            f"ashare_execution_lineage_invalid:{exc}"
        ) from exc
    if not (
        manifest_lineage["execution_lineage_id"]
        == snapshot_lineage["execution_lineage_id"]
        == outbox_lineage["execution_lineage_id"]
        == ASHARE_EXECUTION_LINEAGE_ID
    ):
        raise MarketCapitalReconcileError("ashare_execution_lineage_mismatch")
    if (
        _aware_time(
            snapshot_lineage["point_in_time_as_of"],
            field="ashare_snapshot_point_in_time_as_of",
        )
        > pit
    ):
        raise MarketCapitalReconcileError(
            "ashare_snapshot_point_in_time_after_reconcile_pit"
        )
    if (
        manifest.get("source") != "fresh_zero_import_bootstrap"
        or manifest.get("initial_cash_cny") != 50_000.0
        or manifest.get("imported_legacy_record_count") != 0
        or manifest.get("legacy_roots_read") != []
        or manifest.get("real_trading_enabled") is not False
    ):
        raise MarketCapitalReconcileError("ashare_fresh_execution_manifest_invalid")
    _validate_sim_markers(snapshot, source="ashare_source")
    if snapshot.get("market") != "ashare":
        raise MarketCapitalReconcileError("ashare_source_wrong_market")
    if snapshot.get("source") != "server_local_sim_backup":
        raise MarketCapitalReconcileError("ashare_source_invalid_source")
    if snapshot.get("account_view") != "strategy_samples_only":
        raise MarketCapitalReconcileError("ashare_source_invalid_account_view")
    _validate_source_time(
        snapshot.get("synced_at"),
        trade_date=trade_date,
        pit=pit,
        field="ashare_source_synced_at",
    )

    positions_by_account = snapshot.get("positions_by_account")
    pnl_by_account = snapshot.get("pnl")
    if not isinstance(positions_by_account, Mapping) or not isinstance(
        pnl_by_account, Mapping
    ):
        raise MarketCapitalReconcileError("ashare_source_account_projection_missing")
    if set(positions_by_account) != {account} or set(pnl_by_account) != {account}:
        raise MarketCapitalReconcileError("ashare_source_single_account_required")
    raw_positions = positions_by_account.get(account)
    pnl = pnl_by_account.get(account)
    audit_positions_by_account = snapshot.get("audit_positions_by_account")
    if not isinstance(raw_positions, Mapping) or not isinstance(pnl, Mapping):
        raise MarketCapitalReconcileError("ashare_source_account_projection_invalid")
    if pnl.get("positions") != raw_positions:
        raise MarketCapitalReconcileError("ashare_source_position_projection_conflict")
    if pnl.get("real_trading_enabled") is not False:
        raise MarketCapitalReconcileError("ashare_source_account_real_trading_rejected")
    ashare_marks: dict[str, float] = {}
    mark_views: list[Mapping[str, Any]] = [raw_positions]
    if isinstance(audit_positions_by_account, Mapping):
        audit_positions = audit_positions_by_account.get(account)
        if not isinstance(audit_positions, Mapping):
            raise MarketCapitalReconcileError(
                "ashare_source_account_projection_invalid"
            )
        mark_views.append(audit_positions)
    for mark_view in mark_views:
        for raw_symbol, raw_position in mark_view.items():
            symbol = str(raw_symbol or "").strip().upper()
            if not symbol or not isinstance(raw_position, Mapping):
                raise MarketCapitalReconcileError("ashare_source_inventory_invalid")
            mark = _strict_number(
                raw_position.get("mark_price"),
                field=f"ashare_mark_price:{symbol}",
                positive=True,
            )
            previous_mark = ashare_marks.get(symbol)
            if previous_mark is not None and not math.isclose(
                previous_mark,
                mark,
                abs_tol=1e-9,
            ):
                raise MarketCapitalReconcileError("ashare_mark_projection_conflict")
            ashare_marks[symbol] = mark
    _validate_mark_evidence(
        snapshot,
        expected_marks=ashare_marks,
        pit=pit,
        market="ashare",
    )

    if (
        outbox.get("schema_version") != ASHARE_OUTBOX_SCHEMA
        or not isinstance(outbox.get("actions"), list)
        or outbox.get("real_trading_enabled") is not False
    ):
        raise MarketCapitalReconcileError("ashare_outbox_invalid")
    if outbox.get("payload_sha256") != _canonical_sha(
        outbox, excluded=ASHARE_CHECKSUM_KEYS
    ):
        raise MarketCapitalReconcileError("ashare_outbox_checksum_mismatch")
    _validate_immutable_source_time(
        outbox.get("updated_at"),
        pit=pit,
        field="ashare_outbox_updated_at",
    )

    replay: dict[str, dict[str, float | int]] = {}
    cash = 50_000.0
    committed_event_ids: set[str] = set()
    commit_requests: dict[str, tuple[str, dict[str, Any]]] = {}
    action_ids: set[str] = set()
    for index, raw_action in enumerate(outbox["actions"]):
        if not isinstance(raw_action, Mapping):
            raise MarketCapitalReconcileError("ashare_outbox_action_invalid")
        action = dict(raw_action)
        _reject_legacy_authority(action, location=f"ashare_outbox.actions[{index}]")
        action_id = str(action.get("action_id") or "").strip()
        if not action_id or action_id in action_ids:
            raise MarketCapitalReconcileError("ashare_outbox_action_identity_invalid")
        action_ids.add(action_id)
        if action.get("status") != "completed":
            raise MarketCapitalReconcileError("ashare_outbox_pending_commit")
        if action.get("real_trading_enabled") is not False:
            raise MarketCapitalReconcileError("ashare_outbox_real_action_rejected")
        try:
            action_lineage = require_execution_lineage(action)
        except ExecutionLineageError as exc:
            raise MarketCapitalReconcileError(
                f"ashare_outbox_action_lineage_invalid:{action_id}:{exc}"
            ) from exc
        if action_lineage["execution_lineage_id"] != ASHARE_EXECUTION_LINEAGE_ID:
            raise MarketCapitalReconcileError("ashare_outbox_action_lineage_mismatch")
        result = action.get("last_result")
        result_snapshot = (
            result.get("snapshot") if isinstance(result, Mapping) else None
        )
        result_is_simulated = isinstance(result, Mapping) and (
            result.get("real_trading_enabled") is False
            or (
                result.get("real_trading_enabled") is None
                and isinstance(result_snapshot, Mapping)
                and result_snapshot.get("real_trading_enabled") is False
            )
        )
        if (
            not isinstance(result, Mapping)
            or result.get("committed") is not True
            or not str(result.get("event_id") or "").strip()
            or not result_is_simulated
        ):
            raise MarketCapitalReconcileError("ashare_outbox_commit_result_invalid")
        event_id = str(result["event_id"])
        if event_id in committed_event_ids:
            raise MarketCapitalReconcileError("ashare_outbox_duplicate_commit_event")
        committed_event_ids.add(event_id)

        action_type = str(action.get("action") or "")
        request_field = (
            "fill_commit_request"
            if action_type == "fill_commit"
            else "ashare_sell_commit_request"
            if action_type == "ashare_sell_commit"
            else ""
        )
        checksum_field = f"{request_field}_sha256" if request_field else ""
        request = action.get(request_field) if request_field else None
        if (
            not request_field
            or not isinstance(request, Mapping)
            or action.get(checksum_field) != _canonical_sha(request)
        ):
            raise MarketCapitalReconcileError("ashare_outbox_commit_request_invalid")
        request = dict(request)
        _validate_commit_times(
            request,
            pit=pit,
            source=f"ashare_commit:{action_id}",
        )
        if (
            request.get("market") != "ashare"
            or request.get("authority_id") != "ashare-capital-v1"
            or request.get("authority_generation") != 1
            or request.get("execution_lineage_id") != ASHARE_EXECUTION_LINEAGE_ID
        ):
            raise MarketCapitalReconcileError("ashare_outbox_commit_authority_invalid")
        commit_requests[event_id] = (action_type, dict(request))
        risk_unit = str(request.get("risk_unit_key") or "").strip().upper()
        if not risk_unit:
            raise MarketCapitalReconcileError("ashare_outbox_commit_risk_unit_missing")
        state = replay.setdefault(
            risk_unit,
            {"quantity": 0, "principal": 0.0, "entry_fee": 0.0},
        )
        if action_type == "fill_commit":
            if str(request.get("side") or "").lower() != "buy":
                raise MarketCapitalReconcileError("ashare_fill_side_invalid")
            quantity = _strict_int(
                request.get("actual_filled_quantity"),
                field="ashare_actual_filled_quantity",
                nonzero=True,
            )
            exposure = _strict_number(
                request.get("actual_exposure_cny"),
                field="ashare_actual_exposure_cny",
                positive=True,
            )
            fee = _strict_number(
                request.get("actual_fee_cash_cny"),
                field="ashare_actual_fee_cash_cny",
                nonnegative=True,
            )
            debit = _strict_number(
                request.get("actual_cash_debit_cny"),
                field="ashare_actual_cash_debit_cny",
                positive=True,
            )
            if not math.isclose(debit, exposure + fee, abs_tol=0.01):
                raise MarketCapitalReconcileError(
                    "ashare_fill_cash_components_mismatch"
                )
            state["quantity"] = int(state["quantity"]) + quantity
            state["principal"] = round(float(state["principal"]) + exposure, 6)
            state["entry_fee"] = round(float(state["entry_fee"]) + fee, 6)
            cash = round(cash - debit, 6)
        else:
            quantity = _strict_int(
                request.get("actual_closed_quantity"),
                field="ashare_actual_closed_quantity",
                nonzero=True,
            )
            current_quantity = int(state["quantity"])
            if quantity <= 0 or quantity > current_quantity:
                raise MarketCapitalReconcileError("ashare_sell_inventory_exceeds")
            proceeds = _strict_number(
                request.get("actual_gross_proceeds_cny"),
                field="ashare_actual_gross_proceeds_cny",
                positive=True,
            )
            fee = _strict_number(
                request.get("actual_fee_cash_cny"),
                field="ashare_sell_fee_cny",
                nonnegative=True,
            )
            net_credit = _strict_number(
                request.get("actual_net_cash_credit_cny"),
                field="ashare_net_cash_credit_cny",
                positive=True,
            )
            if not math.isclose(net_credit, proceeds - fee, abs_tol=0.01):
                raise MarketCapitalReconcileError(
                    "ashare_sell_cash_components_mismatch"
                )
            ratio = quantity / current_quantity
            state["principal"] = round(float(state["principal"]) * (1.0 - ratio), 6)
            state["entry_fee"] = round(float(state["entry_fee"]) * (1.0 - ratio), 6)
            state["quantity"] = current_quantity - quantity
            cash = round(cash + net_credit, 6)
            if int(state["quantity"]) == 0:
                replay.pop(risk_unit, None)

    positions_market_value: dict[str, float] = {}
    quantities: dict[str, int] = {}
    cost_basis: dict[str, float] = {}
    entry_fees: dict[str, float] = {}
    unrealized = 0.0
    if set(raw_positions) != set(replay):
        raise MarketCapitalReconcileError("ashare_source_inventory_set_mismatch")
    for raw_symbol, raw_position in raw_positions.items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or not isinstance(raw_position, Mapping):
            raise MarketCapitalReconcileError("ashare_source_inventory_invalid")
        position = dict(raw_position)
        expected = replay[symbol]
        quantity = _strict_int(
            position.get("quantity"), field=f"ashare_quantity:{symbol}", nonzero=True
        )
        principal = _strict_number(
            position.get("principal_cost_basis"),
            field=f"ashare_principal_cost_basis:{symbol}",
            positive=True,
        )
        entry_fee = _strict_number(
            position.get("entry_fee_cost_basis"),
            field=f"ashare_entry_fee_cost_basis:{symbol}",
            nonnegative=True,
        )
        total_cost = _strict_number(
            position.get("cost_basis"),
            field=f"ashare_cost_basis:{symbol}",
            positive=True,
        )
        mark = _strict_number(
            position.get("mark_price"),
            field=f"ashare_mark_price:{symbol}",
            positive=True,
        )
        market_value = _strict_number(
            position.get("market_value"),
            field=f"ashare_market_value:{symbol}",
            positive=True,
        )
        if (
            quantity != int(expected["quantity"])
            or not math.isclose(principal, float(expected["principal"]), abs_tol=0.01)
            or not math.isclose(entry_fee, float(expected["entry_fee"]), abs_tol=0.01)
            or not math.isclose(total_cost, principal + entry_fee, abs_tol=0.01)
            or not math.isclose(market_value, quantity * mark, abs_tol=0.01)
        ):
            raise MarketCapitalReconcileError("ashare_source_inventory_mismatch")
        row_unrealized = market_value - principal - entry_fee
        if not math.isclose(
            _strict_number(
                position.get("unrealized_pnl"),
                field=f"ashare_unrealized_pnl:{symbol}",
            ),
            row_unrealized,
            abs_tol=0.01,
        ):
            raise MarketCapitalReconcileError("ashare_source_unrealized_mismatch")
        positions_market_value[symbol] = round(market_value, 6)
        quantities[symbol] = quantity
        cost_basis[symbol] = round(principal, 6)
        entry_fees[symbol] = round(entry_fee, 6)
        unrealized += row_unrealized
    source_cash = _strict_number(
        pnl.get("cash_available"),
        field="ashare_source_cash",
        nonnegative=True,
    )
    if not math.isclose(source_cash, cash, abs_tol=0.01):
        raise MarketCapitalReconcileError("ashare_source_cash_mismatch")
    if not math.isclose(
        _strict_number(pnl.get("market_value"), field="ashare_source_market_value"),
        sum(positions_market_value.values()),
        abs_tol=0.01,
    ):
        raise MarketCapitalReconcileError("ashare_source_market_value_mismatch")
    if not math.isclose(
        _strict_number(pnl.get("unrealized_pnl"), field="ashare_source_unrealized"),
        unrealized,
        abs_tol=0.01,
    ):
        raise MarketCapitalReconcileError("ashare_source_unrealized_mismatch")
    return _ExecutionState(
        market="ashare",
        execution_lineage_id=ASHARE_EXECUTION_LINEAGE_ID,
        cash_balance_cny=round(cash, 6),
        positions_market_value=positions_market_value,
        unrealized_pnl_cny=round(unrealized, 6),
        position_margin_by_risk_unit={},
        positions_quantity_by_risk_unit=quantities,
        positions_cost_basis_cny_by_risk_unit=cost_basis,
        positions_entry_fee_cny_by_risk_unit=entry_fees,
        position_entry_price_by_risk_unit={},
        position_side_by_risk_unit={},
        position_contract_multiplier_by_risk_unit={},
        position_contract_spec_sha256_by_risk_unit={},
        position_mark_price_by_risk_unit={},
        committed_event_ids=frozenset(committed_event_ids),
        commit_requests_by_event_id=commit_requests,
        # The current A-share writer persists completed atomic commits only.
        # An in-flight ledger reservation has no durable execution-side fact
        # and therefore cannot be admitted into a reconcile checkpoint.
        proven_active_reservation_ids=frozenset(),
        source_reads=(manifest_read.file, snapshot_read.file, outbox_read.file),
    )


def _validate_cn_source(
    root: Path,
    *,
    trade_date: str,
    pit: datetime,
    execution_lineage_id: str,
) -> _ExecutionState:
    signals_root = Path(root).expanduser()
    snapshot_read = _stable_read_json(signals_root / "positions" / CN_POSITION_FILENAME)
    outbox_read = _stable_read_json(signals_root / "capital" / CN_OUTBOX_FILENAME)
    snapshot = snapshot_read.value
    outbox = outbox_read.value
    _reject_legacy_authority(snapshot, location="cn_futures_snapshot")
    _reject_legacy_authority(outbox, location="cn_futures_outbox")
    _validate_sim_markers(snapshot, source="cn_futures_source")
    _validate_sim_markers(outbox, source="cn_futures_outbox")
    if snapshot.get("market") != "cn_futures" or outbox.get("market") != "cn_futures":
        raise MarketCapitalReconcileError("cn_futures_source_wrong_market")
    if snapshot.get("real_trading_enabled") is not False:
        raise MarketCapitalReconcileError("cn_futures_source_real_trading_rejected")
    if (
        snapshot.get("schema_version") != CN_POSITION_SCHEMA
        or _trade_date(snapshot.get("trade_date")) != trade_date
    ):
        raise MarketCapitalReconcileError("cn_futures_position_schema_invalid")
    if outbox.get("schema_version") != CN_OUTBOX_SCHEMA:
        raise MarketCapitalReconcileError("cn_futures_outbox_schema_invalid")
    if snapshot.get("payload_sha256") != _canonical_sha(
        snapshot, excluded={"payload_sha256"}
    ):
        raise MarketCapitalReconcileError("cn_futures_position_checksum_mismatch")
    if outbox.get("payload_sha256") != _canonical_sha(
        outbox, excluded={"payload_sha256"}
    ):
        raise MarketCapitalReconcileError("cn_futures_outbox_checksum_mismatch")
    _validate_source_time(
        snapshot.get("updated_at"),
        trade_date=trade_date,
        pit=pit,
        field="cn_futures_source_updated_at",
        market="cn_futures",
    )
    _validate_immutable_source_time(
        outbox.get("updated_at"),
        pit=pit,
        field="cn_futures_outbox_updated_at",
    )
    if snapshot.get("pending_capital_releases") != []:
        raise MarketCapitalReconcileError("cn_futures_legacy_pending_release")
    if snapshot.get("pending_capital_commits") != []:
        raise MarketCapitalReconcileError("cn_futures_pending_commit")
    history = snapshot.get("capital_commit_history")
    actions = outbox.get("actions")
    positions = snapshot.get("positions")
    if (
        not isinstance(history, list)
        or not isinstance(actions, list)
        or not isinstance(positions, list)
    ):
        raise MarketCapitalReconcileError("cn_futures_source_shape_invalid")
    if snapshot.get("position_count") != len(positions):
        raise MarketCapitalReconcileError("cn_futures_position_count_mismatch")
    cn_marks: dict[str, float] = {}
    for raw_position in positions:
        if not isinstance(raw_position, Mapping):
            raise MarketCapitalReconcileError("cn_futures_source_inventory_invalid")
        symbol = str(raw_position.get("symbol") or "").strip().upper()
        if not symbol:
            raise MarketCapitalReconcileError("cn_futures_source_inventory_invalid")
        mark = _strict_number(
            raw_position.get("mark_price"),
            field=f"cn_futures_mark:{symbol}",
            positive=True,
        )
        previous_mark = cn_marks.get(symbol)
        if previous_mark is not None and not math.isclose(
            previous_mark,
            mark,
            abs_tol=1e-9,
        ):
            raise MarketCapitalReconcileError(
                "cn_futures_mixed_style_inventory_conflict"
            )
        cn_marks[symbol] = mark
    _validate_mark_evidence(
        snapshot,
        expected_marks=cn_marks,
        pit=pit,
        market="cn_futures",
    )

    action_by_id: dict[str, dict[str, Any]] = {}
    all_action_ids: set[str] = set()
    committed_event_ids: set[str] = set()
    commit_requests: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, Mapping):
            raise MarketCapitalReconcileError("cn_futures_outbox_action_invalid")
        action = dict(raw_action)
        action_id = str(action.get("action_id") or "").strip()
        if not action_id or action_id in all_action_ids:
            raise MarketCapitalReconcileError(
                "cn_futures_outbox_action_identity_invalid"
            )
        all_action_ids.add(action_id)
        action_type = str(action.get("action") or "")
        if action_type not in {"release", "fill_commit", "position_close_commit"}:
            raise MarketCapitalReconcileError(
                "cn_futures_legacy_outbox_action_forbidden"
            )
        if action.get("status") != "completed":
            raise MarketCapitalReconcileError("cn_futures_outbox_pending_commit")
        if action.get("real_trading_enabled") is not False:
            raise MarketCapitalReconcileError("cn_futures_outbox_real_action_rejected")
        if action_type == "release":
            result = action.get("result")
            event_id = (
                str(result.get("event_id") or "").strip()
                if isinstance(result, Mapping)
                else ""
            )
            if (
                not isinstance(result, Mapping)
                or result.get("status") not in {"released", "idempotent_release"}
                or result.get("real_trading_enabled") is not False
                or not event_id
                or not str(action.get("reference_id") or "").strip()
                or not str(action.get("reservation_id") or "").strip()
                or not str(action.get("reason") or "").strip()
                or _strict_number(
                    action.get("amount_cny"),
                    field="cn_futures_release_amount",
                    positive=True,
                )
                <= 0.0
            ):
                raise MarketCapitalReconcileError("cn_futures_outbox_release_invalid")
            if event_id in committed_event_ids:
                raise MarketCapitalReconcileError("cn_futures_duplicate_capital_event")
            committed_event_ids.add(event_id)
            commit_requests[event_id] = ("release", dict(action))
            continue
        if not isinstance(action.get("request"), Mapping):
            raise MarketCapitalReconcileError(
                "cn_futures_outbox_commit_request_invalid"
            )
        action_by_id[action_id] = action

    replay: dict[str, dict[str, Any]] = {}
    cash = 50_000.0
    history_ids: set[str] = set()
    for index, raw_history in enumerate(history):
        if not isinstance(raw_history, Mapping):
            raise MarketCapitalReconcileError("cn_futures_commit_history_invalid")
        row = dict(raw_history)
        action_id = str(row.get("action_id") or "").strip()
        if not action_id or action_id in history_ids:
            raise MarketCapitalReconcileError(
                "cn_futures_commit_history_identity_invalid"
            )
        history_ids.add(action_id)
        action = action_by_id.get(action_id)
        if action is None:
            raise MarketCapitalReconcileError(
                "cn_futures_commit_history_outbox_mismatch"
            )
        if row.get("status") != "committed" or row.get("action") != action.get(
            "action"
        ):
            raise MarketCapitalReconcileError(
                "cn_futures_commit_history_status_invalid"
            )
        request = row.get("request")
        if request != action.get("request") or not isinstance(request, Mapping):
            raise MarketCapitalReconcileError(
                "cn_futures_commit_history_request_mismatch"
            )
        result = row.get("result")
        action_result = action.get("result")
        if (
            not isinstance(result, Mapping)
            or not isinstance(action_result, Mapping)
            or result != action_result
            or result.get("committed") is not True
            or result.get("real_trading_enabled") is not False
            or not str(result.get("event_id") or "").strip()
        ):
            raise MarketCapitalReconcileError(
                "cn_futures_commit_history_result_invalid"
            )
        event_id = str(result["event_id"])
        if event_id in committed_event_ids:
            raise MarketCapitalReconcileError("cn_futures_duplicate_commit_event")
        committed_event_ids.add(event_id)
        request = dict(request)
        _validate_commit_times(
            request,
            pit=pit,
            source=f"cn_futures_commit:{action_id}",
        )
        if (
            request.get("market") != "cn_futures"
            or request.get("authority_id") != "cn-futures-capital-v1"
            or request.get("authority_generation") != 1
            or request.get("execution_lineage_id") != execution_lineage_id
        ):
            raise MarketCapitalReconcileError("cn_futures_commit_authority_invalid")
        action_type = str(row.get("action") or "")
        commit_requests[event_id] = (action_type, dict(request))
        risk_unit = str(request.get("risk_unit_key") or "").strip().upper()
        if not risk_unit:
            raise MarketCapitalReconcileError("cn_futures_commit_risk_unit_missing")
        if action_type == "fill_commit":
            quantity = _strict_int(
                request.get("actual_filled_quantity"),
                field="cn_futures_actual_filled_quantity",
                nonzero=True,
            )
            side = str(request.get("side") or "").strip().lower()
            sign = (
                1 if side in {"buy", "long"} else -1 if side in {"sell", "short"} else 0
            )
            if sign == 0:
                raise MarketCapitalReconcileError("cn_futures_fill_side_invalid")
            price = _strict_number(
                request.get("actual_fill_price"),
                field="cn_futures_fill_price",
                positive=True,
            )
            margin = _strict_number(
                request.get("actual_margin_cny"),
                field="cn_futures_fill_margin",
                positive=True,
            )
            multiplier = _strict_number(
                request.get("contract_multiplier"),
                field="cn_futures_contract_multiplier",
                positive=True,
            )
            spec_sha = str(request.get("contract_spec_sha256") or "")
            if not SHA256_RE.fullmatch(spec_sha):
                raise MarketCapitalReconcileError(
                    "cn_futures_contract_spec_sha_invalid"
                )
            debit = _strict_number(
                request.get("actual_cash_debit_cny"),
                field="cn_futures_open_cash_debit",
                nonnegative=True,
            )
            fee = _strict_number(
                request.get("actual_fee_cash_cny"),
                field="cn_futures_open_fee",
                nonnegative=True,
            )
            if not math.isclose(debit, fee, abs_tol=0.01):
                raise MarketCapitalReconcileError(
                    "cn_futures_open_cash_components_mismatch"
                )
            current = replay.get(risk_unit)
            signed_quantity = sign * quantity
            if current and int(current["quantity"]) * signed_quantity < 0:
                raise MarketCapitalReconcileError(
                    "cn_futures_mixed_direction_inventory"
                )
            previous_qty = abs(int(current["quantity"])) if current else 0
            new_abs = previous_qty + quantity
            entry = (
                (float(current["entry_price"]) * previous_qty + price * quantity)
                / new_abs
                if current
                else price
            )
            if current and (
                not math.isclose(float(current["multiplier"]), multiplier, abs_tol=1e-9)
                or current["spec_sha"] != spec_sha
            ):
                raise MarketCapitalReconcileError(
                    "cn_futures_contract_identity_conflict"
                )
            replay[risk_unit] = {
                "quantity": (int(current["quantity"]) if current else 0)
                + signed_quantity,
                "entry_price": round(entry, 10),
                "side": "long" if sign > 0 else "short",
                "multiplier": multiplier,
                "spec_sha": spec_sha,
                "margin": round(
                    (float(current["margin"]) if current else 0.0) + margin, 6
                ),
            }
            cash = round(cash - debit, 6)
        elif action_type == "position_close_commit":
            current = replay.get(risk_unit)
            if current is None:
                raise MarketCapitalReconcileError("cn_futures_close_inventory_missing")
            quantity = _strict_int(
                request.get("actual_closed_quantity"),
                field="cn_futures_actual_closed_quantity",
                nonzero=True,
            )
            current_abs = abs(int(current["quantity"]))
            if quantity <= 0 or quantity > current_abs:
                raise MarketCapitalReconcileError("cn_futures_close_inventory_exceeds")
            margin_release = _strict_number(
                request.get("actual_margin_released_cny"),
                field="cn_futures_margin_release",
                positive=True,
            )
            if margin_release > float(current["margin"]) + 0.01:
                raise MarketCapitalReconcileError("cn_futures_margin_release_exceeds")
            gross = _strict_number(
                request.get("actual_gross_realized_pnl_cny"),
                field="cn_futures_gross_realized_pnl",
            )
            fee = _strict_number(
                request.get("actual_fee_cash_cny"),
                field="cn_futures_close_fee",
                nonnegative=True,
            )
            cash = round(cash + gross - fee, 6)
            remaining = current_abs - quantity
            if remaining == 0:
                replay.pop(risk_unit)
            else:
                current["quantity"] = (
                    remaining if int(current["quantity"]) > 0 else -remaining
                )
                current["margin"] = round(float(current["margin"]) - margin_release, 6)
        else:
            raise MarketCapitalReconcileError(
                "cn_futures_commit_history_action_invalid"
            )
    if set(action_by_id) != history_ids:
        raise MarketCapitalReconcileError("cn_futures_commit_history_outbox_mismatch")

    aggregated: dict[str, dict[str, Any]] = {}
    position_action_ids: set[str] = set()
    for raw_position in positions:
        if not isinstance(raw_position, Mapping):
            raise MarketCapitalReconcileError("cn_futures_source_inventory_invalid")
        position = dict(raw_position)
        symbol = str(position.get("symbol") or "").strip().upper()
        quantity = _strict_int(
            position.get("net_qty"),
            field=f"cn_futures_quantity:{symbol or 'missing'}",
            nonzero=True,
        )
        side = str(position.get("side") or "").strip().lower()
        if not symbol or side != ("long" if quantity > 0 else "short"):
            raise MarketCapitalReconcileError(
                "cn_futures_source_inventory_side_mismatch"
            )
        if _trade_date(position.get("updated_trade_date")) > trade_date:
            raise MarketCapitalReconcileError("cn_futures_position_trade_date_mismatch")
        if position.get("capital_commit_status") != "committed":
            raise MarketCapitalReconcileError(
                "cn_futures_position_commit_not_committed"
            )
        position_action_id = str(position.get("capital_commit_action_id") or "").strip()
        if not position_action_id or position_action_id not in history_ids:
            raise MarketCapitalReconcileError(
                "cn_futures_position_commit_history_missing"
            )
        position_action_ids.add(position_action_id)
        entry = _strict_number(
            position.get("avg_price"), field=f"cn_futures_entry:{symbol}", positive=True
        )
        mark = _strict_number(
            position.get("mark_price"), field=f"cn_futures_mark:{symbol}", positive=True
        )
        multiplier = _strict_number(
            position.get("contract_multiplier"),
            field=f"cn_futures_multiplier:{symbol}",
            positive=True,
        )
        margin = _strict_number(
            position.get("margin_required"),
            field=f"cn_futures_margin:{symbol}",
            positive=True,
        )
        current = aggregated.get(symbol)
        if current is None:
            aggregated[symbol] = {
                "quantity": quantity,
                "entry_price": entry,
                "mark_price": mark,
                "side": side,
                "multiplier": multiplier,
                "margin": margin,
            }
        else:
            if (
                current["side"] != side
                or not math.isclose(float(current["mark_price"]), mark, abs_tol=1e-9)
                or not math.isclose(
                    float(current["multiplier"]), multiplier, abs_tol=1e-9
                )
            ):
                raise MarketCapitalReconcileError(
                    "cn_futures_mixed_style_inventory_conflict"
                )
            old_abs = abs(int(current["quantity"]))
            new_abs = abs(quantity)
            current["entry_price"] = (
                float(current["entry_price"]) * old_abs + entry * new_abs
            ) / (old_abs + new_abs)
            current["quantity"] = int(current["quantity"]) + quantity
            current["margin"] = float(current["margin"]) + margin

    if set(aggregated) != set(replay):
        raise MarketCapitalReconcileError("cn_futures_source_inventory_set_mismatch")
    quantities: dict[str, int] = {}
    margins: dict[str, float] = {}
    entries: dict[str, float] = {}
    sides: dict[str, str] = {}
    multipliers: dict[str, float] = {}
    specs: dict[str, str] = {}
    marks: dict[str, float] = {}
    unrealized = 0.0
    for symbol, actual in aggregated.items():
        expected = replay[symbol]
        if (
            int(actual["quantity"]) != int(expected["quantity"])
            or not math.isclose(
                float(actual["entry_price"]),
                float(expected["entry_price"]),
                abs_tol=0.01,
            )
            or actual["side"] != expected["side"]
            or not math.isclose(
                float(actual["multiplier"]), float(expected["multiplier"]), abs_tol=1e-9
            )
            or not math.isclose(
                float(actual["margin"]), float(expected["margin"]), abs_tol=0.01
            )
        ):
            raise MarketCapitalReconcileError("cn_futures_source_inventory_mismatch")
        quantity = int(actual["quantity"])
        quantities[symbol] = quantity
        margins[symbol] = round(float(actual["margin"]), 6)
        entries[symbol] = round(float(actual["entry_price"]), 10)
        sides[symbol] = str(actual["side"])
        multipliers[symbol] = round(float(actual["multiplier"]), 10)
        specs[symbol] = str(expected["spec_sha"])
        marks[symbol] = round(float(actual["mark_price"]), 10)
        unrealized += (
            (marks[symbol] - entries[symbol])
            * abs(quantity)
            * multipliers[symbol]
            * (1 if quantity > 0 else -1)
        )
    total_margin = _strict_number(
        snapshot.get("total_margin_required"),
        field="cn_futures_total_margin_required",
        nonnegative=True,
    )
    if not math.isclose(total_margin, sum(margins.values()), abs_tol=0.01):
        raise MarketCapitalReconcileError("cn_futures_total_margin_mismatch")
    return _ExecutionState(
        market="cn_futures",
        execution_lineage_id=execution_lineage_id,
        cash_balance_cny=round(cash, 6),
        positions_market_value={},
        unrealized_pnl_cny=round(unrealized, 6),
        position_margin_by_risk_unit=margins,
        positions_quantity_by_risk_unit=quantities,
        positions_cost_basis_cny_by_risk_unit={},
        positions_entry_fee_cny_by_risk_unit={},
        position_entry_price_by_risk_unit=entries,
        position_side_by_risk_unit=sides,
        position_contract_multiplier_by_risk_unit=multipliers,
        position_contract_spec_sha256_by_risk_unit=specs,
        position_mark_price_by_risk_unit=marks,
        committed_event_ids=frozenset(committed_event_ids),
        commit_requests_by_event_id=commit_requests,
        # CN simulation orders are IOC/terminal and completed commits clear
        # their reservations.  Any remaining ledger reservation is transient
        # and has no exact persisted pending-order proof in this schema.
        proven_active_reservation_ids=frozenset(),
        source_reads=(snapshot_read.file, outbox_read.file),
    )


def _assert_source_reads_unchanged(source_reads: Sequence[_FileRead]) -> None:
    for prior in source_reads:
        current = _stable_read_bytes(prior.path)
        if current.fingerprint != prior.fingerprint or current.sha256 != prior.sha256:
            raise MarketCapitalReconcileError(
                f"reconcile_source_torn_read:{prior.path}"
            )


def _write_canonical_snapshot(
    capital_root: Path,
    payload: Mapping[str, Any],
) -> tuple[Path, str]:
    directory = Path(capital_root) / "reconcile_sources"
    if directory.exists() and directory.is_symlink():
        raise MarketCapitalReconcileError(
            "canonical_reconcile_directory_symlink_rejected"
        )
    directory.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    sha = _sha256_bytes(encoded)
    path = directory / f"{payload['market']}-{payload['trade_date']}-{sha}.json"
    if path.exists():
        existing = _stable_read_bytes(path)
        if existing.payload != encoded:
            raise MarketCapitalReconcileError("canonical_reconcile_snapshot_conflict")
        return existing.path, sha
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise MarketCapitalReconcileError(
            "canonical_reconcile_snapshot_write_failed"
        ) from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    verified = _stable_read_bytes(path)
    if verified.sha256 != sha or verified.payload != encoded:
        raise MarketCapitalReconcileError("canonical_reconcile_snapshot_verify_failed")
    return verified.path, sha


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_symlink():
        raise MarketCapitalReconcileError(f"reconcile_source_symlink_rejected:{target}")
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = ""
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise MarketCapitalReconcileError(
            f"reconcile_source_write_failed:{target}"
        ) from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _prepare_empty_cn_source(
    source_root: Path,
    *,
    trade_date: str,
    pit: datetime,
) -> dict[str, Any]:
    position_path = source_root / "positions" / CN_POSITION_FILENAME
    outbox_path = source_root / "capital" / CN_OUTBOX_FILENAME
    if not position_path.exists():
        position_payload: dict[str, Any] = {
            "schema_version": CN_POSITION_SCHEMA,
            "market": "cn_futures",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "trade_date": trade_date,
            "position_count": 0,
            "total_margin_required": 0.0,
            "positions": [],
            "pending_capital_releases": [],
            "pending_capital_commits": [],
            "capital_commit_history": [],
            "mark_evidence_by_symbol": {},
            "updated_at": pit.isoformat(),
            "real_trading_enabled": False,
        }
        position_payload["payload_sha256"] = _canonical_sha(
            position_payload, excluded={"payload_sha256"}
        )
        _write_json_atomic(position_path, position_payload)
    if not outbox_path.exists():
        outbox_payload: dict[str, Any] = {
            "schema_version": CN_OUTBOX_SCHEMA,
            "market": "cn_futures",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "actions": [],
            "updated_at": pit.isoformat(),
            "real_trading_enabled": False,
        }
        outbox_payload["payload_sha256"] = _canonical_sha(
            outbox_payload, excluded={"payload_sha256"}
        )
        _write_json_atomic(outbox_path, outbox_payload)
    return {
        "status": "prepared",
        "market": "cn_futures",
        "trade_date": trade_date,
        "position_path": str(position_path.resolve()),
        "outbox_path": str(outbox_path.resolve()),
        "mark_count": 0,
        "real_trading_enabled": False,
    }


def _sharedsignals_mark(
    reader: Any,
    *,
    market: str,
    symbol: str,
    trade_date: str,
    pit: datetime,
) -> tuple[float, dict[str, Any]]:
    reader_market = "Ashare" if market == "ashare" else "Futures"
    reader_type = type(reader)
    trusted_reader_source = (
        "SharedSignals/TradingagentDataReader"
        if reader_type.__module__ == "shared.data.reader"
        and reader_type.__name__ in {"TradingagentDataReader", "SharedSignalsReader"}
        else ""
    )
    intraday_query_date = pit.astimezone(CN_TZ).strftime("%Y%m%d")
    candidates: list[tuple[datetime, dict[str, Any], str]] = []
    intraday = getattr(reader, "get_bars_intraday", None)
    if callable(intraday):
        try:
            rows = intraday(
                reader_market,
                symbol,
                "5min",
                intraday_query_date,
                intraday_query_date,
            )
        except TypeError:
            try:
                rows = intraday(
                    market=reader_market,
                    symbol=symbol,
                    interval="5min",
                    start=intraday_query_date,
                    end=intraday_query_date,
                )
            except Exception:
                rows = []
        except Exception:
            rows = []
        for raw in rows or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            observed_value = (
                row.get("bar_time")
                or row.get("trade_time")
                or row.get("time")
                or row.get("datetime")
                or row.get("timestamp")
            )
            try:
                observed = _aware_time(
                    observed_value,
                    field=f"sharedsignals_mark:{symbol}",
                )
            except MarketCapitalReconcileError:
                continue
            if observed <= pit:
                candidates.append((observed, row, "intraday_5min"))

    if not candidates:
        daily = getattr(reader, "get_bars_daily", None)
        if callable(daily):
            daily_end = pit.astimezone(CN_TZ).strftime("%Y%m%d")
            daily_start = (pit.astimezone(CN_TZ) - timedelta(days=10)).strftime(
                "%Y%m%d"
            )
            try:
                rows = daily(
                    reader_market,
                    symbol,
                    daily_start,
                    daily_end,
                )
            except TypeError:
                try:
                    rows = daily(
                        market=reader_market,
                        symbol=symbol,
                        start=daily_start,
                        end=daily_end,
                    )
                except Exception:
                    rows = []
            except Exception:
                rows = []
            for raw in rows or []:
                if not isinstance(raw, Mapping):
                    continue
                row = dict(raw)
                try:
                    row_date = _trade_date(row.get("trade_date") or row.get("date"))
                    observed = datetime.strptime(row_date, "%Y%m%d").replace(
                        hour=15,
                        tzinfo=CN_TZ,
                    )
                except (MarketCapitalReconcileError, ValueError):
                    continue
                if observed <= pit:
                    candidates.append((observed, row, "daily_close"))

    valid: list[tuple[datetime, dict[str, Any], str, float]] = []
    for observed, row, cadence in candidates:
        source = str(
            row.get("source") or row.get("data_source") or trusted_reader_source
        ).strip()
        if "sharedsignals" not in source.lower():
            continue
        row_symbol = (
            str(row.get("symbol") or row.get("ts_code") or row.get("contract") or "")
            .strip()
            .upper()
        )
        if row_symbol and row_symbol != symbol.upper():
            continue
        raw_price = (
            row.get("close")
            if row.get("close") is not None
            else row.get("last_price")
            if row.get("last_price") is not None
            else row.get("price")
        )
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0.0:
            continue
        valid.append((observed, row, cadence, price))
    if not valid:
        raise MarketCapitalReconcileError(
            f"sharedsignals_pit_mark_unavailable:{market}:{symbol}"
        )
    observed, row, cadence, price = max(valid, key=lambda item: item[0])
    evidence = {
        "price": round(price, 10),
        "observed_at": observed.isoformat(),
        "source": str(
            row.get("source") or row.get("data_source") or trusted_reader_source
        ),
        "source_owner": "SharedSignals",
        "source_row": row,
        "source_row_sha256": _canonical_sha(row),
        "cadence": cadence,
        "point_in_time_as_of": pit.isoformat(),
        "real_trading_enabled": False,
    }
    return round(price, 10), evidence


def _prepare_ashare_source(
    source_root: Path,
    *,
    trade_date: str,
    pit: datetime,
    reader: Any | None,
) -> dict[str, Any]:
    snapshot_path = source_root / ASHARE_POSITION_FILENAME
    snapshot = _stable_read_json(snapshot_path).value
    _validate_sim_markers(snapshot, source="ashare_prepare_source")
    positions_by_account = snapshot.get("positions_by_account")
    pnl_by_account = snapshot.get("pnl")
    audit_positions_by_account = snapshot.get("audit_positions_by_account")
    audit_pnl_by_account = snapshot.get("audit_pnl")
    if (
        not isinstance(positions_by_account, Mapping)
        or not isinstance(pnl_by_account, Mapping)
        or not isinstance(audit_positions_by_account, Mapping)
        or not isinstance(audit_pnl_by_account, Mapping)
    ):
        raise MarketCapitalReconcileError("ashare_source_account_projection_missing")
    symbols = sorted(
        {
            str(symbol).strip().upper()
            for account_views in (
                positions_by_account,
                audit_positions_by_account,
            )
            for positions in account_views.values()
            if isinstance(positions, Mapping)
            for symbol in positions
            if str(symbol).strip()
        }
    )
    if symbols and reader is None:
        raise MarketCapitalReconcileError("sharedsignals_mark_reader_required")
    marks: dict[str, float] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        marks[symbol], evidence[symbol] = _sharedsignals_mark(
            reader,
            market="ashare",
            symbol=symbol,
            trade_date=trade_date,
            pit=pit,
        )

    def _refresh_views(
        raw_positions_by_account: Mapping[str, Any],
        raw_pnl_by_account: Mapping[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        refreshed_positions: dict[str, dict[str, Any]] = {}
        refreshed_pnl: dict[str, dict[str, Any]] = {}
        for raw_account, raw_positions in raw_positions_by_account.items():
            account = str(raw_account)
            if not isinstance(raw_positions, Mapping):
                raise MarketCapitalReconcileError(
                    "ashare_source_account_projection_invalid"
                )
            account_positions: dict[str, Any] = {}
            for raw_symbol, raw_position in raw_positions.items():
                symbol = str(raw_symbol).strip().upper()
                if not isinstance(raw_position, Mapping):
                    raise MarketCapitalReconcileError("ashare_source_inventory_invalid")
                position = dict(raw_position)
                quantity = _strict_int(
                    position.get("quantity"),
                    field=f"ashare_quantity:{symbol}",
                    nonzero=True,
                )
                cost = _strict_number(
                    position.get("cost_basis"),
                    field=f"ashare_cost_basis:{symbol}",
                    positive=True,
                )
                mark = marks[symbol]
                market_value = round(quantity * mark, 2)
                position.update(
                    {
                        "mark_price": mark,
                        "market_value": market_value,
                        "unrealized_pnl": round(market_value - cost, 2),
                    }
                )
                account_positions[symbol] = position
            raw_pnl = raw_pnl_by_account.get(raw_account)
            if not isinstance(raw_pnl, Mapping):
                raise MarketCapitalReconcileError(
                    "ashare_source_account_projection_invalid"
                )
            pnl = dict(raw_pnl)
            market_value = round(
                sum(float(row["market_value"]) for row in account_positions.values()),
                2,
            )
            unrealized = round(
                sum(float(row["unrealized_pnl"]) for row in account_positions.values()),
                2,
            )
            pnl.update(
                {
                    "positions": account_positions,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized,
                    "total_pnl": round(
                        float(pnl.get("realized_pnl", 0.0)) + unrealized,
                        2,
                    ),
                    "real_trading_enabled": False,
                }
            )
            refreshed_positions[account] = account_positions
            refreshed_pnl[account] = pnl
        return refreshed_positions, refreshed_pnl

    refreshed_positions, refreshed_pnl = _refresh_views(
        positions_by_account,
        pnl_by_account,
    )
    refreshed_audit_positions, refreshed_audit_pnl = _refresh_views(
        audit_positions_by_account,
        audit_pnl_by_account,
    )

    flat_positions: list[dict[str, Any]] = []
    for account, account_positions in refreshed_positions.items():
        for symbol, position in account_positions.items():
            flat_positions.append(
                {
                    "account": account,
                    "ts_code": symbol,
                    "quantity": position["quantity"],
                    "avg_price": position.get("avg_cost", 0.0),
                    "last_price": position.get("last_price", 0.0),
                    "mark_price": position["mark_price"],
                    "market_value": position["market_value"],
                    "unrealized_pnl": position["unrealized_pnl"],
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    "source": "server_local_sim_backup",
                    "real_trading_enabled": False,
                }
            )
    updated = dict(snapshot)
    updated.update(
        {
            "synced_at": pit.isoformat(),
            "trade_date": trade_date,
            "positions": flat_positions,
            "positions_by_account": refreshed_positions,
            "pnl": refreshed_pnl,
            "audit_positions_by_account": refreshed_audit_positions,
            "audit_pnl": refreshed_audit_pnl,
            "mark_evidence_by_symbol": evidence,
            "real_trading_enabled": False,
        }
    )
    _write_json_atomic(snapshot_path, updated)
    return {
        "status": "prepared",
        "market": "ashare",
        "trade_date": trade_date,
        "position_path": str(snapshot_path.resolve()),
        "mark_count": len(evidence),
        "real_trading_enabled": False,
    }


def _refresh_cn_source(
    source_root: Path,
    *,
    trade_date: str,
    pit: datetime,
    reader: Any | None,
) -> dict[str, Any]:
    position_path = source_root / "positions" / CN_POSITION_FILENAME
    snapshot = _stable_read_json(position_path).value
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        raise MarketCapitalReconcileError("cn_futures_source_inventory_invalid")
    symbols = sorted(
        {
            str(position.get("symbol") or "").strip().upper()
            for position in positions
            if isinstance(position, Mapping)
            and str(position.get("symbol") or "").strip()
        }
    )
    if symbols and reader is None:
        raise MarketCapitalReconcileError("sharedsignals_mark_reader_required")
    marks: dict[str, float] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        marks[symbol], evidence[symbol] = _sharedsignals_mark(
            reader,
            market="cn_futures",
            symbol=symbol,
            trade_date=trade_date,
            pit=pit,
        )
    refreshed_positions = []
    for raw_position in positions:
        if not isinstance(raw_position, Mapping):
            raise MarketCapitalReconcileError("cn_futures_source_inventory_invalid")
        position = dict(raw_position)
        symbol = str(position.get("symbol") or "").strip().upper()
        if not symbol or symbol not in marks:
            raise MarketCapitalReconcileError("cn_futures_source_inventory_invalid")
        position["mark_price"] = marks[symbol]
        refreshed_positions.append(position)
    updated = dict(snapshot)
    updated.update(
        {
            "schema_version": CN_POSITION_SCHEMA,
            "trade_date": trade_date,
            "position_count": len(refreshed_positions),
            "total_margin_required": round(
                sum(
                    _strict_number(
                        position.get("margin_required"),
                        field="cn_futures_margin_required",
                        nonnegative=True,
                    )
                    for position in refreshed_positions
                ),
                6,
            ),
            "positions": refreshed_positions,
            "mark_evidence_by_symbol": evidence,
            "updated_at": pit.isoformat(),
            "real_trading_enabled": False,
        }
    )
    updated["payload_sha256"] = _canonical_sha(
        updated,
        excluded={"payload_sha256"},
    )
    _write_json_atomic(position_path, updated)
    return {
        "status": "prepared",
        "market": "cn_futures",
        "trade_date": trade_date,
        "position_path": str(position_path.resolve()),
        "outbox_path": str((source_root / "capital" / CN_OUTBOX_FILENAME).resolve()),
        "mark_count": len(evidence),
        "real_trading_enabled": False,
    }


def prepare_reconcile_source(
    *,
    market: str,
    source_root: Path | str,
    trade_date: str,
    pit_timestamp: str,
    reader: Any | None = None,
) -> dict[str, Any]:
    """Persist a complete sim-only source before stable reconcile reads."""

    _ensure_sim_only()
    normalized_market = str(market or "").strip().lower().replace("-", "_")
    normalized_date = _trade_date(trade_date)
    pit = _aware_time(pit_timestamp, field="prepare_pit_timestamp")
    if _reconcile_trade_date_for_pit(normalized_market, pit) != normalized_date:
        raise MarketCapitalReconcileError("prepare_pit_trade_date_mismatch")
    if normalized_market == "cn_futures":
        _prepare_empty_cn_source(
            Path(source_root).expanduser(),
            trade_date=normalized_date,
            pit=pit,
        )
        return _refresh_cn_source(
            Path(source_root).expanduser(),
            trade_date=normalized_date,
            pit=pit,
            reader=reader,
        )
    if normalized_market == "ashare":
        return _prepare_ashare_source(
            Path(source_root).expanduser(),
            trade_date=normalized_date,
            pit=pit,
            reader=reader,
        )
    raise MarketCapitalReconcileError("unsupported_reconcile_market")


def _assert_capital_matches_execution(
    market: str,
    capital: Any,
    execution: _ExecutionState,
) -> None:
    if capital.execution_lineage_id != execution.execution_lineage_id:
        raise MarketCapitalReconcileError("execution_lineage_capital_mismatch")
    if not math.isclose(
        capital.cash_balance_cny, execution.cash_balance_cny, abs_tol=0.01
    ):
        raise MarketCapitalReconcileError(f"{market}_source_cash_mismatch")
    if (
        capital.positions_quantity_by_risk_unit
        != execution.positions_quantity_by_risk_unit
    ):
        raise MarketCapitalReconcileError(f"{market}_source_inventory_mismatch")
    if market == "ashare":
        if not _maps_close(
            capital.positions_cost_basis_cny_by_risk_unit,
            execution.positions_cost_basis_cny_by_risk_unit,
        ) or not _maps_close(
            capital.positions_entry_fee_cny_by_risk_unit,
            execution.positions_entry_fee_cny_by_risk_unit,
        ):
            raise MarketCapitalReconcileError("ashare_source_inventory_mismatch")
    else:
        if (
            not math.isclose(
                capital.margin_used_cny,
                sum(execution.position_margin_by_risk_unit.values()),
                abs_tol=0.01,
            )
            or not _maps_close(
                capital.position_entry_price_by_risk_unit,
                execution.position_entry_price_by_risk_unit,
            )
            or capital.position_side_by_risk_unit
            != execution.position_side_by_risk_unit
            or not _maps_close(
                capital.position_contract_multiplier_by_risk_unit,
                execution.position_contract_multiplier_by_risk_unit,
            )
            or capital.position_contract_spec_sha256_by_risk_unit
            != execution.position_contract_spec_sha256_by_risk_unit
        ):
            raise MarketCapitalReconcileError("cn_futures_source_inventory_mismatch")


def reconcile_market_capital(
    *,
    market: str,
    capital_root: Path | str,
    source_root: Path | str,
    trade_date: str,
    pit_timestamp: str,
    phase: str,
    ashare_account: str = "ashare_sim",
    ashare_sample_journal_path: Path | str | None = None,
) -> dict[str, Any]:
    """Write one immutable, execution-backed reconcile checkpoint."""

    _ensure_sim_only()
    normalized_market = str(market or "").strip().lower().replace("-", "_")
    if normalized_market not in {"ashare", "cn_futures"}:
        raise MarketCapitalReconcileError("unsupported_reconcile_market")
    normalized_phase = str(phase or "").strip().lower()
    if normalized_phase not in ALLOWED_PHASES:
        raise MarketCapitalReconcileError("unsupported_reconcile_phase")
    normalized_date = _trade_date(trade_date)
    pit = _aware_time(pit_timestamp, field="reconcile_pit_timestamp")
    if _reconcile_trade_date_for_pit(normalized_market, pit) != normalized_date:
        raise MarketCapitalReconcileError("reconcile_pit_trade_date_mismatch")
    policy = MarketPolicy.load(normalized_market)
    root = Path(capital_root).expanduser()
    ledger = MarketCapitalLedger(root, policy=policy)
    first = ledger.snapshot()
    reservations = ledger.active_reservation_manifest()
    second = ledger.snapshot()
    if (
        first.event_id != second.event_id
        or first.event_checksum != second.event_checksum
        or first.unreconciled_fill_commit_ids != second.unreconciled_fill_commit_ids
    ):
        raise MarketCapitalReconcileError("capital_head_changed_during_source_read")
    if first.authority_generation != 1 or first.real_trading_enabled is not False:
        raise MarketCapitalReconcileError("capital_authority_not_fresh_sim_only")
    if first.frozen_order_cash_cny != 0.0 or first.frozen_order_margin_cny != 0.0:
        raise MarketCapitalReconcileError("frozen_orders_missing_execution_source")

    if normalized_market == "ashare":
        execution = _validate_ashare_source(
            Path(source_root),
            account=ashare_account,
            trade_date=normalized_date,
            pit=pit,
        )
    else:
        execution = _validate_cn_source(
            Path(source_root),
            trade_date=normalized_date,
            pit=pit,
            execution_lineage_id=first.execution_lineage_id,
        )
    if set(reservations) != set(execution.proven_active_reservation_ids):
        raise MarketCapitalReconcileError("active_reservation_execution_fact_mismatch")
    _assert_capital_matches_execution(normalized_market, first, execution)
    _validate_commit_events_against_ledger(
        ledger,
        execution,
        expected_head_event_id=first.event_id,
        expected_head_checksum=first.event_checksum,
    )
    included_ids = tuple(first.unreconciled_fill_commit_ids)
    if any(event_id not in execution.committed_event_ids for event_id in included_ids):
        raise MarketCapitalReconcileError("execution_commit_watermark_mismatch")
    active_total = round(
        sum(
            float(row["remaining_cash_cny"])
            if normalized_market == "ashare"
            else float(row["remaining_margin_cny"])
            for row in reservations.values()
        ),
        6,
    )
    if not math.isclose(active_total, first.active_reservations_cny, abs_tol=1e-9):
        raise MarketCapitalReconcileError("exact_reservation_manifest_total_mismatch")

    canonical_payload = {
        "schema_version": RECONCILE_SOURCE_SCHEMA_VERSION,
        "market": normalized_market,
        "trade_date": normalized_date,
        "pit_timestamp": pit.isoformat(),
        "execution_lineage_id": execution.execution_lineage_id,
        "cash_balance_cny": execution.cash_balance_cny,
        "positions_market_value": execution.positions_market_value,
        "unrealized_pnl_cny": execution.unrealized_pnl_cny,
        "position_margin_by_risk_unit": execution.position_margin_by_risk_unit,
        "active_reservations_cny": first.active_reservations_cny,
        "active_reservations": reservations,
        "frozen_order_cash_cny": first.frozen_order_cash_cny,
        "frozen_order_margin_cny": first.frozen_order_margin_cny,
        "positions_quantity_by_risk_unit": execution.positions_quantity_by_risk_unit,
        "positions_cost_basis_cny_by_risk_unit": execution.positions_cost_basis_cny_by_risk_unit,
        "positions_entry_fee_cny_by_risk_unit": execution.positions_entry_fee_cny_by_risk_unit,
        "position_entry_price_by_risk_unit": execution.position_entry_price_by_risk_unit,
        "position_side_by_risk_unit": execution.position_side_by_risk_unit,
        "position_contract_multiplier_by_risk_unit": execution.position_contract_multiplier_by_risk_unit,
        "position_contract_spec_sha256_by_risk_unit": execution.position_contract_spec_sha256_by_risk_unit,
        "position_mark_price_by_risk_unit": execution.position_mark_price_by_risk_unit,
        "expected_ledger_event_id": first.event_id,
        "expected_ledger_checksum": first.event_checksum,
        "included_fill_commit_ids": list(included_ids),
        "real_trading_enabled": False,
    }
    _assert_source_reads_unchanged(execution.source_reads)
    canonical_path, canonical_sha = _write_canonical_snapshot(root, canonical_payload)
    _assert_source_reads_unchanged(execution.source_reads)
    source_fingerprints = ",".join(
        f"{item.path.name}:{item.sha256}" for item in execution.source_reads
    )
    manifest = ReconcileManifest(
        market=normalized_market,
        authority_id=policy.capital_authority_id,
        as_of=normalized_date,
        cash_balance_cny=execution.cash_balance_cny,
        positions_market_value=execution.positions_market_value,
        unrealized_pnl_cny=execution.unrealized_pnl_cny,
        position_margin_by_risk_unit=execution.position_margin_by_risk_unit,
        active_reservations_cny=first.active_reservations_cny,
        frozen_order_cash_cny=first.frozen_order_cash_cny,
        frozen_order_margin_cny=first.frozen_order_margin_cny,
        authority_generation=1,
        execution_lineage_id=execution.execution_lineage_id,
        pit_timestamp=pit.isoformat(),
        source=f"market_capital_reconcile_ops:{normalized_phase}:{source_fingerprints}",
        source_sha256=canonical_sha,
        active_reservations=reservations,
        expected_ledger_event_id=first.event_id,
        expected_ledger_checksum=first.event_checksum,
        included_fill_commit_ids=included_ids,
        positions_quantity_by_risk_unit=execution.positions_quantity_by_risk_unit,
        positions_cost_basis_cny_by_risk_unit=execution.positions_cost_basis_cny_by_risk_unit,
        positions_entry_fee_cny_by_risk_unit=execution.positions_entry_fee_cny_by_risk_unit,
        position_entry_price_by_risk_unit=execution.position_entry_price_by_risk_unit,
        position_side_by_risk_unit=execution.position_side_by_risk_unit,
        position_contract_multiplier_by_risk_unit=execution.position_contract_multiplier_by_risk_unit,
        position_contract_spec_sha256_by_risk_unit=execution.position_contract_spec_sha256_by_risk_unit,
        position_mark_price_by_risk_unit=execution.position_mark_price_by_risk_unit,
        canonical_snapshot_path=str(canonical_path),
        canonical_snapshot_sha256=canonical_sha,
    )
    try:
        result = ledger.mtm_reconcile(manifest)
    except MarketCapitalLedgerError as exc:
        raise MarketCapitalReconcileError(
            f"market_capital_reconcile_rejected:{exc}"
        ) from exc
    provider = ledger.provider_state(normalized_date)
    sample_journal_mtm_evidence: dict[str, Any] = {
        "status": "not_applicable",
        "reason": "cn_futures_has_independent_maturity_authority",
    }
    if normalized_market == "ashare":
        sample_journal_mtm_evidence = _append_ashare_daily_mtm_evidence(
            journal_path=ashare_sample_journal_path,
            trade_date=normalized_date,
            pit=pit,
            phase=normalized_phase,
            account=ashare_account,
            equity_cny=result.get("equity_cny"),
            capital_reconcile_event_id=result.get("event_id"),
            canonical_snapshot_path=canonical_path,
            canonical_snapshot_sha256=canonical_sha,
            execution_lineage_id=execution.execution_lineage_id,
        )
    return {
        **result,
        "market": normalized_market,
        "phase": normalized_phase,
        "trade_date": normalized_date,
        "fresh": provider.get("fresh") is True,
        "reconciled": provider.get("reconciled") is True,
        "authority_id": policy.capital_authority_id,
        "authority_generation": 1,
        "execution_lineage_id": execution.execution_lineage_id,
        "cash_balance_cny": execution.cash_balance_cny,
        "positions_quantity_by_risk_unit": execution.positions_quantity_by_risk_unit,
        "positions_cost_basis_cny_by_risk_unit": execution.positions_cost_basis_cny_by_risk_unit,
        "positions_entry_fee_cny_by_risk_unit": execution.positions_entry_fee_cny_by_risk_unit,
        "position_margin_by_risk_unit": execution.position_margin_by_risk_unit,
        "included_fill_commit_ids": list(included_ids),
        "active_reservation_count": len(reservations),
        "canonical_snapshot_path": str(canonical_path),
        "canonical_snapshot_sha256": canonical_sha,
        "execution_source_files": [
            {"path": str(item.path), "sha256": item.sha256}
            for item in execution.source_reads
        ],
        "sample_journal_mtm_evidence": sample_journal_mtm_evidence,
        "real_trading_enabled": False,
    }


def _append_ashare_daily_mtm_evidence(
    *,
    journal_path: Path | str | None,
    trade_date: str,
    pit: datetime,
    phase: str,
    account: str,
    equity_cny: Any,
    capital_reconcile_event_id: Any,
    canonical_snapshot_path: Path,
    canonical_snapshot_sha256: str,
    execution_lineage_id: str,
) -> dict[str, Any]:
    """Append one immutable close-of-day MTM fact without blocking sampling.

    The 15:31 threshold deliberately follows the separate 15:05-15:30 fixed-
    price session.  Earlier reconciles remain valid capital checkpoints, but
    cannot be presented as the account's daily closing equity.
    """

    if journal_path is None:
        return {"status": "disabled", "reason": "sample_journal_path_not_configured"}
    if phase != "ops" or (pit.hour, pit.minute) < ASHARE_DAILY_MTM_CLOSE_TIME:
        return {"status": "not_due", "reason": "close_of_day_mtm_not_due"}
    if not isinstance(equity_cny, (int, float)) or isinstance(equity_cny, bool):
        return {"status": "rejected", "reason": "invalid_account_equity_cny"}
    equity = float(equity_cny)
    if not math.isfinite(equity) or equity <= 0.0:
        return {"status": "rejected", "reason": "invalid_account_equity_cny"}
    if execution_lineage_id != ASHARE_EXECUTION_LINEAGE_ID:
        return {"status": "rejected", "reason": "invalid_execution_lineage_id"}

    timestamp = pit.isoformat()
    event_id = f"account-daily-mtm:ashare:{trade_date}"
    journal = SampleJournal(journal_path)
    try:
        existing = next(
            (
                row
                for row in journal.read_events()
                if row.get("journal_event_id") == f"sample:{event_id}"
            ),
            None,
        )
        if existing is not None:
            same_fact = (
                existing.get("trade_date") == trade_date
                and existing.get("account_equity_cny") == round(equity, 6)
                and existing.get("equity_source") == ASHARE_DAILY_MTM_EVIDENCE_SOURCE
                and existing.get("capital_authority_id") == ASHARE_CAPITAL_AUTHORITY_ID
                and existing.get("authority_generation") == ASHARE_AUTHORITY_GENERATION
                and existing.get("execution_lineage_id") == execution_lineage_id
            )
            if not same_fact:
                return {
                    "status": "rejected",
                    "reason": "daily_mtm_evidence_conflict",
                    "event_id": event_id,
                }
            return {
                "status": "idempotent",
                "event_id": event_id,
                "journal_path": str(Path(journal_path).absolute()),
                "account_equity_cny": round(equity, 6),
                "equity_source": ASHARE_DAILY_MTM_EVIDENCE_SOURCE,
            }
        appended = journal.append_sample(
            {
                "event_id": event_id,
                "record_type": "chain_validation",
                "sample_layer": "chain_validation",
                "evidence_type": "account_daily_mtm_equity",
                "market": "ashare",
                "account": account,
                "trade_date": trade_date,
                "account_equity_cny": round(equity, 6),
                "equity_source": ASHARE_DAILY_MTM_EVIDENCE_SOURCE,
                "capital_reconcile_event_id": str(capital_reconcile_event_id or ""),
                "event_time": timestamp,
                "available_at": timestamp,
                "ingested_at": timestamp,
                "retrieved_as_of": timestamp,
                "point_in_time_as_of": timestamp,
                "source_snapshot_path": str(canonical_snapshot_path),
                "source_snapshot_sha256": canonical_snapshot_sha256,
                "capital_authority_id": ASHARE_CAPITAL_AUTHORITY_ID,
                "authority_generation": ASHARE_AUTHORITY_GENERATION,
                "execution_lineage_id": execution_lineage_id,
                "real_trading_enabled": False,
            }
        )
    except JournalError as exc:
        return {
            "status": "rejected",
            "reason": f"sample_journal_rejected:{exc}",
            "event_id": event_id,
        }
    return {
        "status": str(appended.get("status") or "unknown"),
        "event_id": event_id,
        "journal_path": str(Path(journal_path).absolute()),
        "account_equity_cny": round(equity, 6),
        "equity_source": ASHARE_DAILY_MTM_EVIDENCE_SOURCE,
    }


def _default_source_root(market: str) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    if market == "ashare":
        return Path(
            os.environ.get(
                "TRADINGAGENT_ASHARE_EXECUTION_ROOT",
                project_root
                / "shared"
                / "logs"
                / "execution_lineages"
                / ASHARE_EXECUTION_LINEAGE_ID,
            )
        )
    return Path(os.environ.get("TRADINGAGENT_SIGNALS_ROOT", project_root / "signals"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", required=True, choices=("ashare", "cn_futures"))
    parser.add_argument("--capital-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--pit-timestamp", required=True)
    parser.add_argument("--phase", required=True, choices=sorted(ALLOWED_PHASES))
    parser.add_argument("--ashare-account", default="ashare_sim")
    parser.add_argument(
        "--ashare-sample-journal-path",
        type=Path,
        default=(
            Path(__file__).resolve().parents[2]
            / "shared"
            / "review"
            / "ashare"
            / "sample_journal.jsonl"
        ),
    )
    parser.add_argument(
        "--prepare-source",
        action="store_true",
        help="Refresh execution marks from SharedSignals before reconcile.",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    capital_root_value = args.capital_root or market_capital_root(args.market)
    source_root_value = args.source_root or _default_source_root(args.market)
    try:
        if args.prepare_source:
            from shared.data.reader import TradingagentDataReader

            prepare_reconcile_source(
                market=args.market,
                source_root=source_root_value,
                trade_date=args.trade_date,
                pit_timestamp=args.pit_timestamp,
                reader=TradingagentDataReader(),
            )
        result = reconcile_market_capital(
            market=args.market,
            capital_root=capital_root_value,
            source_root=source_root_value,
            trade_date=args.trade_date,
            pit_timestamp=args.pit_timestamp,
            phase=args.phase,
            ashare_account=args.ashare_account,
            ashare_sample_journal_path=args.ashare_sample_journal_path,
        )
    except Exception as exc:  # noqa: BLE001 - operational JSON boundary
        output = {
            "status": "blocked",
            "market": args.market,
            "phase": args.phase,
            "trade_date": str(args.trade_date),
            "reason": str(exc),
            "error_type": exc.__class__.__name__,
            "real_trading_enabled": False,
        }
        print(
            json.dumps(
                output,
                ensure_ascii=False,
                sort_keys=True,
                indent=2 if args.pretty else None,
            )
        )
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MarketCapitalReconcileError",
    "prepare_reconcile_source",
    "reconcile_market_capital",
]
