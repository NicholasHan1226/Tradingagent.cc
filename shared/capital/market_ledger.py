"""Append-only, lock-protected authority for a single-market simulated capital account.

v2 Nicholas fresh-start approved (nicholas-fresh-start-019f5040-20260712).
- fail-before-write: invalid manifest → root nonexistent
- real legacy freeze verification (file SHA, row count, dir exists)
- pinned decision IDs matched against policy
- reservation lineage (authority_generation, execution_lineage_id)
- reconcile conservation (active reservations match, conflicting payload)
- snapshot/provider detailed capacities, available_to_reserve min constraint
- fsync parent directory
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .market_policy import (
    MarketPolicy,
    REQUIRED_CUTOVER_STATE,
    PINNED_CUTOVER_DECISION_ID,
    PINNED_SOURCE_THREAD_ID,
)

ALLOWED_EVENT_TYPES = {
    "bootstrap",
    "reserve",
    "fill_commit",
    "ashare_sell_commit",
    "position_close_commit",
    "release",
    "mark",
    "realized_pnl",
    "reconcile",
}
GENESIS_PREVIOUS_CHECKSUM = "genesis"
SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
CN_TZ = timezone(timedelta(hours=8))
CN_FUTURES_CONTRACT_SPEC_VERSION = "cn-futures-contract-spec.v1"
RECONCILE_SOURCE_SCHEMA_VERSION = "market-capital-reconcile-source.v1"


class MarketCapitalLedgerError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compute_event_checksum(row: Mapping[str, Any]) -> str:
    content = dict(row)
    content.pop("checksum", None)
    # previous_checksum stays in canonical payload
    return _sha256_hex(json.dumps(content, ensure_ascii=False, sort_keys=True))


def cn_futures_contract_spec_sha256(
    risk_unit_key: str,
    contract_multiplier: float,
    margin_per_lot_cny: float,
    *,
    version: str = CN_FUTURES_CONTRACT_SPEC_VERSION,
) -> str:
    """Bind the immutable contract identity used by capital accounting."""

    payload = {
        "contract_multiplier": float(contract_multiplier),
        "margin_per_lot_cny": float(margin_per_lot_cny),
        "risk_unit_key": str(risk_unit_key or "").strip(),
        "version": str(version or "").strip(),
    }
    return _sha256_hex(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _is_64hex(s: str) -> bool:
    return bool(SHA256_HEX_RE.match(s))


def _strict_number(v: object, *, field: str, positive: bool = False) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise MarketCapitalLedgerError(f"invalid_{field}")
    n = float(v)
    if not math.isfinite(n) or (n <= 0.0 if positive else False):
        raise MarketCapitalLedgerError(f"invalid_{field}")
    return n


def _normalize_market(v: str) -> str:
    return str(v or "").strip().lower().replace("-", "_")


def _validate_trade_date(v: object) -> str:
    n = str(v or "").strip().replace("-", "")
    if len(n) != 8 or not n.isdigit():
        raise MarketCapitalLedgerError("invalid_trade_date")
    return n


def _tz_aware(ts: str) -> bool:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.tzinfo is not None
    except ValueError:
        return False


def _parse_timestamp(ts: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketCapitalLedgerError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise MarketCapitalLedgerError(f"{field}_timezone_required")
    return parsed


def _reconcile_trade_date_for_pit(market: str, pit: datetime) -> str:
    """Resolve the market attribution date without weakening PIT validation.

    A shares remain calendar-date attributed.  CN futures night sessions use
    the following exchange weekday, including Friday night / Saturday-early
    attribution to Monday.  A holiday-aware runtime may still supply the PIT
    and trade date only when its exchange calendar agrees with this fail-closed
    ledger boundary.
    """

    current = pit.astimezone(CN_TZ)
    if _normalize_market(market) != "cn_futures":
        return current.strftime("%Y%m%d")
    rolls_to_next_session = (current.weekday() < 5 and current.hour >= 21) or (
        current.weekday() == 5 and current.hour < 3
    )
    if not rolls_to_next_session:
        return current.strftime("%Y%m%d")
    candidate = current + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.strftime("%Y%m%d")


# ---- data classes ----


@dataclass(frozen=True)
class OpeningStateManifest:
    market: str
    authority_id: str
    cutover_decision_id: str
    mode: str
    as_of: str
    cash_balance_cny: float
    opening_equity_cny: float
    active_reservations_cny: float
    consecutive_losses: int
    inherited_high_water_equity_cny: float
    positions_by_risk_unit: dict[str, float]
    position_margin_by_risk_unit: dict[str, float]
    frozen_order_cash_cny: float
    realized_pnl_cny: float
    unrealized_pnl_cny: float
    source: str
    source_sha256: str
    execution_lineage_id: str
    real: bool = False


@dataclass(frozen=True)
class ReconcileManifest:
    market: str
    authority_id: str
    as_of: str
    cash_balance_cny: float
    positions_market_value: dict[str, float]
    unrealized_pnl_cny: float
    position_margin_by_risk_unit: dict[str, float]
    active_reservations_cny: float
    frozen_order_cash_cny: float
    frozen_order_margin_cny: float
    authority_generation: int
    execution_lineage_id: str
    pit_timestamp: str
    source: str
    source_sha256: str
    active_reservations: dict[str, dict[str, Any]] | None = None
    expected_ledger_event_id: str = ""
    expected_ledger_checksum: str = ""
    included_fill_commit_ids: tuple[str, ...] = ()
    positions_quantity_by_risk_unit: dict[str, int] | None = None
    positions_cost_basis_cny_by_risk_unit: dict[str, float] | None = None
    positions_entry_fee_cny_by_risk_unit: dict[str, float] | None = None
    position_entry_price_by_risk_unit: dict[str, float] | None = None
    position_side_by_risk_unit: dict[str, str] | None = None
    position_contract_multiplier_by_risk_unit: dict[str, float] | None = None
    position_contract_spec_sha256_by_risk_unit: dict[str, str] | None = None
    position_mark_price_by_risk_unit: dict[str, float] | None = None
    canonical_snapshot_path: str = ""
    canonical_snapshot_sha256: str = ""


@dataclass(frozen=True)
class MarketCapitalReservationRequest:
    market: str
    reference_id: str
    risk_unit_key: str
    worst_case_amount_cny: float
    authority_id: str
    trade_date: str
    point_in_time_as_of: str
    lineage_sha256: str
    authority_generation: int
    execution_lineage_id: str
    worst_case_cash_cny: float | None = None
    worst_case_exposure_cny: float | None = None
    worst_case_margin_cny: float | None = None


@dataclass(frozen=True)
class MarketCapitalFillCommitRequest:
    market: str
    reference_id: str
    reservation_id: str
    reservation_event_id: str
    reservation_reference_id: str
    risk_unit_key: str
    authority_id: str
    authority_generation: int
    execution_lineage_id: str
    lineage_sha256: str
    order_id: str
    idempotency_key: str
    execution_fill_id: str
    fill_sequence: int
    side: str
    status: str
    terminal: bool
    actual_filled_quantity: int
    actual_fill_price: float
    actual_cash_debit_cny: float
    actual_exposure_cny: float
    actual_margin_cny: float
    actual_fee_cash_cny: float
    filled_at: str
    point_in_time_as_of: str
    source: str
    source_sha256: str
    receipt_sha256: str
    local_trade_sha256: str
    expected_ledger_event_id: str
    expected_ledger_checksum: str
    contract_multiplier: float = 0.0
    contract_margin_per_lot_cny: float = 0.0
    contract_spec_version: str = ""
    contract_spec_sha256: str = ""


@dataclass(frozen=True)
class MarketCapitalPositionCloseCommitRequest:
    market: str
    reference_id: str
    risk_unit_key: str
    authority_id: str
    authority_generation: int
    execution_lineage_id: str
    lineage_sha256: str
    order_id: str
    idempotency_key: str
    execution_fill_id: str
    fill_sequence: int
    side: str
    status: str
    terminal: bool
    actual_closed_quantity: int
    actual_fill_price: float
    actual_margin_released_cny: float
    actual_fee_cash_cny: float
    actual_gross_realized_pnl_cny: float
    filled_at: str
    point_in_time_as_of: str
    source: str
    source_sha256: str
    receipt_sha256: str
    local_position_sha256: str
    expected_ledger_event_id: str
    expected_ledger_checksum: str


@dataclass(frozen=True)
class MarketCapitalAshareSellCommitRequest:
    market: str
    reference_id: str
    risk_unit_key: str
    authority_id: str
    authority_generation: int
    execution_lineage_id: str
    lineage_sha256: str
    order_id: str
    idempotency_key: str
    execution_fill_id: str
    fill_sequence: int
    side: str
    status: str
    terminal: bool
    actual_closed_quantity: int
    actual_fill_price: float
    actual_gross_proceeds_cny: float
    actual_fee_cash_cny: float
    actual_net_cash_credit_cny: float
    actual_gross_realized_pnl_cny: float
    filled_at: str
    point_in_time_as_of: str
    source: str
    source_sha256: str
    receipt_sha256: str
    local_position_sha256: str
    expected_ledger_event_id: str
    expected_ledger_checksum: str


@dataclass(frozen=True)
class MarketCapitalSnapshot:
    source: str
    schema_version: str
    authority_id: str
    authority_generation: int
    account_name: str
    market: str
    currency: str
    initial_equity_cny: float
    equity_cny: float
    cash_balance_cny: float
    positions_market_value_cny: float
    margin_used_cny: float
    frozen_order_cash_cny: float
    frozen_order_margin_cny: float
    realized_pnl_cny: float
    unrealized_pnl_cny: float
    reserved_capital_cny: float
    active_reservations_cny: float
    available_to_reserve_cny: float
    capital_utilization_rate: float
    stock_gross_exposure_limit_cny: float
    margin_utilization_limit_cny: float
    reconciled: bool
    event_id: str
    event_checksum: str
    updated_at: str
    execution_lineage_id: str
    real_trading_enabled: bool = False
    reserved_cash_cny: float = 0.0
    reserved_exposure_cny: float = 0.0
    reserved_margin_cny: float = 0.0
    unreconciled_fill_commit_ids: tuple[str, ...] = ()
    positions_quantity_by_risk_unit: dict[str, int] = field(default_factory=dict)
    positions_cost_basis_cny_by_risk_unit: dict[str, float] = field(
        default_factory=dict
    )
    positions_entry_fee_cny_by_risk_unit: dict[str, float] = field(default_factory=dict)
    position_entry_price_by_risk_unit: dict[str, float] = field(default_factory=dict)
    position_side_by_risk_unit: dict[str, str] = field(default_factory=dict)
    position_contract_multiplier_by_risk_unit: dict[str, float] = field(
        default_factory=dict
    )
    position_contract_spec_sha256_by_risk_unit: dict[str, str] = field(
        default_factory=dict
    )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MarketCapitalReservationDecision:
    approved: bool
    reason: str
    reservation_id: str = ""
    event_id: str = ""
    snapshot: MarketCapitalSnapshot | None = None
    risk_tightened: bool = False
    risk_multiplier: float = 1.0
    reservation_cap_cny: float = 0.0


@dataclass(frozen=True)
class MarketCapitalFillCommitDecision:
    committed: bool
    reason: str
    status: str = "rejected"
    event_id: str = ""
    reservation_id: str = ""
    snapshot: MarketCapitalSnapshot | None = None
    idempotent: bool = False


@dataclass(frozen=True)
class MarketCapitalPositionCloseCommitDecision:
    committed: bool
    reason: str
    status: str = "rejected"
    event_id: str = ""
    snapshot: MarketCapitalSnapshot | None = None
    idempotent: bool = False


@dataclass(frozen=True)
class MarketCapitalAshareSellCommitDecision:
    committed: bool
    reason: str
    status: str = "rejected"
    event_id: str = ""
    snapshot: MarketCapitalSnapshot | None = None
    idempotent: bool = False


@dataclass
class _ReservationState:
    reservation_id: str
    market: str
    reference_id: str
    risk_unit_key: str
    original_amount_cny: float
    remaining_amount_cny: float
    authority_id: str
    authority_generation: int
    execution_lineage_id: str
    lineage_sha256: str
    point_in_time_as_of: str
    event_id: str
    trade_date: str
    original_cash_cny: float = 0.0
    remaining_cash_cny: float = 0.0
    original_exposure_cny: float = 0.0
    remaining_exposure_cny: float = 0.0
    original_margin_cny: float = 0.0
    remaining_margin_cny: float = 0.0
    terminal: bool = False
    last_fill_sequence: int = 0


@dataclass
class _ReplayState:
    snapshot: MarketCapitalSnapshot
    reservations: dict[str, _ReservationState]
    mtm_equity_history: list[dict[str, Any]] = field(default_factory=list)
    peak_mtm_equity: float = 50_000.0
    latest_mtm_equity: float = 50_000.0
    latest_positions_mv: dict[str, float] = field(default_factory=dict)
    latest_position_margin: dict[str, float] = field(default_factory=dict)
    latest_position_quantity: dict[str, int] = field(default_factory=dict)
    latest_position_cost_basis: dict[str, float] = field(default_factory=dict)
    latest_position_entry_fee: dict[str, float] = field(default_factory=dict)
    latest_position_entry_price: dict[str, float] = field(default_factory=dict)
    latest_position_side: dict[str, str] = field(default_factory=dict)
    latest_position_contract_multiplier: dict[str, float] = field(default_factory=dict)
    latest_position_contract_spec_sha256: dict[str, str] = field(default_factory=dict)
    unreconciled_fill_commit_ids: list[str] = field(default_factory=list)


@dataclass
class _AshareQuantityLot:
    acquired_on: str
    remaining_quantity: int


# ---- legacy freeze verification ----


def _verify_legacy_freeze(mf: dict[str, Any]) -> dict[str, Any]:
    """Real verification — file must exist, sha must match, row count correct."""
    errors = []
    if mf.get("imported") is not False:
        errors.append("imported_must_be_false")

    events_path = str(mf.get("events_path") or "")
    if not events_path or not os.path.isabs(events_path):
        errors.append("events_path_not_absolute")
        return _lf_fail(errors)
    ep = Path(events_path)
    if not ep.exists() or not ep.is_file() or ep.is_symlink():
        errors.append("events_path_not_regular_file")
        return _lf_fail(errors)

    declared_sha = str(mf.get("sha256") or "")
    if not _is_64hex(declared_sha):
        errors.append("sha256_not_64hex")
    actual_sha = _sha256_file(ep)
    if actual_sha != declared_sha:
        errors.append("sha256_mismatch")

    declared_count = mf.get("row_count")
    declared_last = str(mf.get("last_event_id") or "")
    if (
        not isinstance(declared_count, int)
        or isinstance(declared_count, bool)
        or declared_count < 0
    ):
        errors.append("row_count_invalid")
    if (
        isinstance(declared_count, int)
        and not isinstance(declared_count, bool)
        and declared_count > 0
        and not declared_last
    ):
        errors.append("last_event_id_missing")
    if (
        isinstance(declared_count, int)
        and not isinstance(declared_count, bool)
        and declared_count >= 0
        and not errors
    ):
        try:
            lines = [
                line for line in ep.read_text("utf-8").splitlines() if line.strip()
            ]
            actual_count = len(lines)
            if actual_count != declared_count:
                errors.append("row_count_mismatch")
            if lines:
                last_row = json.loads(lines[-1])
                if str(last_row.get("event_id") or "") != declared_last:
                    errors.append("last_event_id_mismatch")
        except (OSError, json.JSONDecodeError):
            errors.append("events_file_unreadable")

    archive_path = str(mf.get("archive_path") or "")
    if not archive_path or not os.path.isabs(archive_path):
        errors.append("archive_path_not_absolute")
        return _lf_fail(errors)
    ap = Path(archive_path)
    if not ap.exists() or not ap.is_dir() or ap.is_symlink():
        errors.append("archive_path_not_directory")

    frozen_at = str(mf.get("frozen_at") or "")
    if not _tz_aware(frozen_at):
        errors.append("frozen_at_not_timezone_aware")

    if errors:
        return _lf_fail(errors)
    return {"status": "verified", "actual_sha256": actual_sha}


def _lf_fail(errors: list[str]) -> dict:
    raise MarketCapitalLedgerError(
        f"legacy_freeze_verification_failed:{','.join(errors)}"
    )


# ---- ledger ----


class MarketCapitalLedger:
    def __init__(self, root: str | Path, *, policy: MarketPolicy | None = None):
        r = Path(root).expanduser()
        if r.exists() and r.is_symlink():
            raise MarketCapitalLedgerError("root_symlink")
        self.root = r
        if policy is None:
            raise MarketCapitalLedgerError("policy_required")
        self.policy = policy
        a = policy.account_name
        self.events_filename = f"{a}_capital_events.jsonl"
        self.latest_filename = f"{a}_capital_latest.json"
        self.lock_filename = f".{a}_capital.lock"
        self.events_path = self.root / self.events_filename
        self.latest_path = self.root / self.latest_filename
        self.lock_path = self.root / self.lock_filename
        self._initialized = self.events_path.exists()

    @staticmethod
    def _reject_symlink(p: Path) -> None:
        if p.exists() and p.is_symlink():
            raise MarketCapitalLedgerError(f"symlink:{p.name}")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as e:
            raise MarketCapitalLedgerError("lock_unavailable") from e
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ---- initialize (fail-before-write) ----

    def initialize(
        self,
        manifest: OpeningStateManifest,
        *,
        cutover_manifest: dict[str, Any] | None = None,
        legacy_freeze_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # --- Phase 1: Validate ALL inputs BEFORE any filesystem mutation ---
        if cutover_manifest is None:
            raise MarketCapitalLedgerError("cutover_manifest_required")
        if legacy_freeze_manifest is None:
            raise MarketCapitalLedgerError("legacy_freeze_manifest_required")

        # Validate cutover manifest
        cdi = str(cutover_manifest.get("cutover_decision_id") or "").strip()
        if cdi != PINNED_CUTOVER_DECISION_ID:
            raise MarketCapitalLedgerError("pinned_decision_id_mismatch")
        sti = str(cutover_manifest.get("source_thread_id") or "").strip()
        if sti != PINNED_SOURCE_THREAD_ID:
            raise MarketCapitalLedgerError("pinned_source_thread_id_mismatch")
        cs = str(cutover_manifest.get("cutover_state") or "").strip()
        if cs != REQUIRED_CUTOVER_STATE:
            raise MarketCapitalLedgerError(
                f"cutover_state_must_be_{REQUIRED_CUTOVER_STATE}"
            )
        ag = cutover_manifest.get("authority_generation")
        if (
            not isinstance(ag, int)
            or isinstance(ag, bool)
            or ag != self.policy.authority_generation
        ):
            raise MarketCapitalLedgerError("authority_generation_mismatch")

        # Validate legacy freeze (real file verification)
        _verify_legacy_freeze(legacy_freeze_manifest)

        # Validate opening manifest
        mk = _normalize_market(manifest.market)
        if mk != self.policy.market:
            raise MarketCapitalLedgerError("market_mismatch")
        if manifest.authority_id != self.policy.capital_authority_id:
            raise MarketCapitalLedgerError("authority_id_mismatch")
        if manifest.cutover_decision_id != PINNED_CUTOVER_DECISION_ID:
            raise MarketCapitalLedgerError("decision_id_mismatch")
        if manifest.real is not False:
            raise MarketCapitalLedgerError("real_must_be_false")

        mode = str(manifest.mode or "").strip()
        if mode != "fresh_start":
            raise MarketCapitalLedgerError(f"only_fresh_start_allowed:{mode}")

        # fresh_start constraints
        if not math.isclose(manifest.cash_balance_cny, 50_000.0, abs_tol=1e-9):
            raise MarketCapitalLedgerError("fresh_start_cash_50000")
        if not math.isclose(manifest.opening_equity_cny, 50_000.0, abs_tol=1e-9):
            raise MarketCapitalLedgerError("fresh_start_opening_equity_50000")
        if manifest.active_reservations_cny != 0.0:
            raise MarketCapitalLedgerError("fresh_start_reservations_zero")
        if manifest.consecutive_losses != 0:
            raise MarketCapitalLedgerError("fresh_start_consecutive_losses_zero")
        if manifest.inherited_high_water_equity_cny != 0.0:
            raise MarketCapitalLedgerError("fresh_start_high_water_zero")
        if manifest.positions_by_risk_unit:
            raise MarketCapitalLedgerError("fresh_start_positions_zero")
        if manifest.position_margin_by_risk_unit:
            raise MarketCapitalLedgerError("fresh_start_margin_zero")
        if manifest.frozen_order_cash_cny != 0.0:
            raise MarketCapitalLedgerError("fresh_start_frozen_zero")
        if manifest.realized_pnl_cny != 0.0:
            raise MarketCapitalLedgerError("fresh_start_realized_pnl_zero")
        if manifest.unrealized_pnl_cny != 0.0:
            raise MarketCapitalLedgerError("fresh_start_unrealized_pnl_zero")
        if not manifest.execution_lineage_id:
            raise MarketCapitalLedgerError("execution_lineage_id_required")
        if not _is_64hex(manifest.source_sha256):
            raise MarketCapitalLedgerError("source_sha256_not_64hex")
        if not manifest.source:
            raise MarketCapitalLedgerError("source_required")
        # Validate as_of is parseable
        try:
            _validate_trade_date(manifest.as_of)
        except MarketCapitalLedgerError:
            raise MarketCapitalLedgerError("invalid_as_of")

        # --- Phase 2: All validations passed, now create root ---
        self.root.mkdir(parents=True, exist_ok=True)
        self._reject_symlink(self.root)

        with self._lock():
            if self.events_path.exists():
                return self._replay_for_idempotency(manifest)

            opening = MarketCapitalSnapshot(
                source="opening_state_manifest",
                schema_version="market-capital-snapshot.v2",
                authority_id=self.policy.capital_authority_id,
                authority_generation=self.policy.authority_generation,
                account_name=self.policy.account_name,
                market=self.policy.market,
                currency=self.policy.currency,
                initial_equity_cny=self.policy.initial_equity_cny,
                equity_cny=50_000.0,
                cash_balance_cny=50_000.0,
                positions_market_value_cny=0.0,
                margin_used_cny=0.0,
                frozen_order_cash_cny=0.0,
                frozen_order_margin_cny=0.0,
                realized_pnl_cny=0.0,
                unrealized_pnl_cny=0.0,
                reserved_capital_cny=0.0,
                active_reservations_cny=0.0,
                available_to_reserve_cny=(
                    self.policy.stock_gross_exposure_limit_cny
                    if self.policy.market == "ashare"
                    else self.policy.margin_utilization_limit_cny
                ),
                capital_utilization_rate=0.0,
                stock_gross_exposure_limit_cny=self.policy.stock_gross_exposure_limit_cny,
                margin_utilization_limit_cny=self.policy.margin_utilization_limit_cny,
                reconciled=False,
                event_id="",
                event_checksum="",
                updated_at=_now_iso(),
                execution_lineage_id=manifest.execution_lineage_id,
                real_trading_enabled=False,
            )

            evt = {
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "bootstrap",
                "authority_id": self.policy.capital_authority_id,
                "authority_generation": self.policy.authority_generation,
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "cutover_decision_id": cdi,
                "source_thread_id": sti,
                "cutover_state": cs,
                "mode": mode,
                "as_of": str(manifest.as_of),
                "cash_balance_cny": 50_000.0,
                "opening_equity_cny": 50_000.0,
                "active_reservations_cny": 0.0,
                "consecutive_losses": 0,
                "inherited_high_water_equity_cny": 0.0,
                "positions_by_risk_unit": {},
                "position_margin_by_risk_unit": {},
                "frozen_order_cash_cny": 0.0,
                "realized_pnl_cny": 0.0,
                "unrealized_pnl_cny": 0.0,
                "source": manifest.source,
                "source_sha256": manifest.source_sha256,
                "execution_lineage_id": manifest.execution_lineage_id,
                "legacy_freeze": legacy_freeze_manifest,
                "reference_id": f"opening-{self.policy.market}-v{self.policy.authority_generation}",
                "amount_cny": self.policy.initial_equity_cny,
                "currency": self.policy.currency,
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "previous_checksum": _sha256_hex(GENESIS_PREVIOUS_CHECKSUM),
            }
            evt["checksum"] = _compute_event_checksum(evt)
            opening = dc_replace(
                opening,
                event_id=str(evt["event_id"]),
                event_checksum=str(evt["checksum"]),
            )

            self._append_event_unlocked(evt)
            self._write_projection_unlocked(opening)
            self._fsync_parent()
            self._initialized = True

            return {
                "status": "initialized",
                "mode": mode,
                "event_id": evt["event_id"],
                "snapshot": opening.as_dict(),
                "real_trading_enabled": False,
            }

    def _replay_for_idempotency(self, manifest: OpeningStateManifest) -> dict:
        events = self._load_events_unlocked()
        if not events:
            raise MarketCapitalLedgerError("empty_event_log")
        b = events[0]
        if b.get("event_type") != "bootstrap":
            raise MarketCapitalLedgerError("missing_bootstrap")
        if (
            not math.isclose(
                float(b.get("cash_balance_cny", 0)),
                manifest.cash_balance_cny,
                abs_tol=1e-9,
            )
            or b.get("mode") != manifest.mode
            or b.get("cutover_decision_id") != manifest.cutover_decision_id
        ):
            raise MarketCapitalLedgerError("initialization_conflict")
        replay = self._replay(events)
        self._write_projection_unlocked(replay.snapshot)
        self._initialized = True
        return {
            "status": "already_initialized",
            "mode": manifest.mode,
            "event_id": str(b["event_id"]),
            "snapshot": replay.snapshot.as_dict(),
            "real_trading_enabled": False,
        }

    # ---- filesystem ----

    def _fsync_parent(self) -> None:
        try:
            fd = os.open(str(self.root), os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass

    def _load_events_unlocked(self) -> list[dict[str, Any]]:
        self._reject_symlink(self.events_path)
        try:
            lines = self.events_path.read_text("utf-8").splitlines()
        except OSError as e:
            raise MarketCapitalLedgerError("unreadable") from e
        if not lines:
            raise MarketCapitalLedgerError("empty_log")
        out = []
        for i, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise MarketCapitalLedgerError(f"corrupt:{i}") from e
            if not isinstance(row, dict):
                raise MarketCapitalLedgerError(f"invalid:{i}")
            out.append(row)
        return out

    def _append_event_unlocked(self, event: Mapping[str, Any]) -> None:
        self._reject_symlink(self.events_path)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(self.events_path, flags, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8", closefd=True) as h:
                h.write(
                    json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n"
                )
                h.flush()
                os.fsync(h.fileno())
            self._fsync_parent()
        except OSError as e:
            raise MarketCapitalLedgerError("append_failed") from e

    def _write_projection_unlocked(self, snapshot: MarketCapitalSnapshot) -> None:
        self._reject_symlink(self.latest_path)
        payload = (
            json.dumps(snapshot.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{self.latest_filename}.",
                suffix=".tmp",
                delete=False,
            ) as h:
                tmp = h.name
                h.write(payload)
                h.flush()
                os.fsync(h.fileno())
            os.replace(tmp, self.latest_path)
            self._fsync_parent()
        except OSError as e:
            if tmp:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except OSError:
                    pass
            raise MarketCapitalLedgerError("projection_failed") from e

    # ---- replay + validation ----

    def _validate_event(self, row: Mapping[str, Any], *, index: int) -> str:
        for k in (
            "event_id",
            "event_type",
            "market",
            "reference_id",
            "currency",
            "created_at",
        ):
            if not isinstance(row.get(k), str) or not str(row.get(k)).strip():
                raise MarketCapitalLedgerError(f"invalid_event:{index}")
        if row["event_type"] not in ALLOWED_EVENT_TYPES:
            raise MarketCapitalLedgerError(f"unsupported:{index}")
        if str(row.get("authority_id") or "") != self.policy.capital_authority_id:
            raise MarketCapitalLedgerError(f"aid:{index}")
        g = row.get("authority_generation")
        if (
            not isinstance(g, int)
            or isinstance(g, bool)
            or g != self.policy.authority_generation
        ):
            raise MarketCapitalLedgerError(f"gen:{index}")
        if _normalize_market(row["market"]) != self.policy.market:
            raise MarketCapitalLedgerError(f"mkt:{index}")
        if row.get("account_name") != self.policy.account_name:
            raise MarketCapitalLedgerError(f"acct:{index}")
        if row.get("currency") != self.policy.currency:
            raise MarketCapitalLedgerError(f"ccy:{index}")
        if row.get("real_trading_enabled") is not False:
            raise MarketCapitalLedgerError(f"real:{index}")
        ck = str(row.get("checksum") or "")
        if not ck:
            raise MarketCapitalLedgerError(f"no_cksum:{index}")
        if ck != _compute_event_checksum(row):
            raise MarketCapitalLedgerError(f"cksum:{index}")
        return str(row.get("previous_checksum") or "")

    def _replay(self, events: list[dict[str, Any]]) -> _ReplayState:
        seen = set()
        resv: dict[str, _ReservationState] = {}
        rpnl = 0.0
        bcnt = 0
        last_eid = ""
        upd = ""
        mh: list[dict] = []
        peak = self.policy.initial_equity_cny
        leq = self.policy.initial_equity_cny
        lcash = self.policy.initial_equity_cny
        lupnl = 0.0
        lpmv = 0.0
        lmgn = 0.0
        lfrozen_cash = 0.0
        lfrozen_margin = 0.0
        lpmv_d: dict[str, float] = {}
        lpm_d: dict[str, float] = {}
        lqty_d: dict[str, int] = {}
        lcost_d: dict[str, float] = {}
        lentry_fee_d: dict[str, float] = {}
        lentry_price_d: dict[str, float] = {}
        lside_d: dict[str, str] = {}
        lmultiplier_d: dict[str, float] = {}
        lcontract_spec_sha_d: dict[str, str] = {}
        pending_fill_commit_ids: list[str] = []
        has_reconcile = False
        exec_lineage = str(events[0].get("execution_lineage_id", "")) if events else ""
        prev_expected: str | None = None

        for i, row in enumerate(events, 1):
            pv = self._validate_event(row, index=i)
            if i == 1:
                if pv != _sha256_hex(GENESIS_PREVIOUS_CHECKSUM):
                    raise MarketCapitalLedgerError(f"genesis_cksum:{i}")
            elif prev_expected is not None and pv != prev_expected:
                raise MarketCapitalLedgerError(f"chain:{i}")
            prev_expected = str(row.get("checksum") or "")

            eid = str(row["event_id"])
            if eid in seen:
                raise MarketCapitalLedgerError(f"dup:{i}")
            seen.add(eid)
            et = str(row["event_type"])
            mk = _normalize_market(row["market"])
            amt = float(row["amount_cny"])

            if et == "bootstrap":
                bcnt += 1
                if i != 1:
                    raise MarketCapitalLedgerError("bootstrap_position")
                lcash = float(
                    row.get("cash_balance_cny", self.policy.initial_equity_cny)
                )
                lupnl = 0.0
                lpmv = 0.0
                lmgn = 0.0
                lfrozen_cash = 0.0
                lfrozen_margin = 0.0
                lpmv_d = {}
                lpm_d = {}
                lqty_d = {}
                lcost_d = {}
                lentry_fee_d = {}
                lentry_price_d = {}
                lside_d = {}
                lmultiplier_d = {}
                lcontract_spec_sha_d = {}
                leq = lcash
                peak = leq

            elif et == "reserve":
                rid = str(row.get("reservation_id") or "").strip()
                if not rid or rid in resv:
                    raise MarketCapitalLedgerError(f"resv_id:{i}")
                if mk == "ashare":
                    cash_leg = float(row.get("cash_reservation_cny", amt))
                    exposure_leg = float(row.get("exposure_reservation_cny", amt))
                    margin_leg = 0.0
                else:
                    cash_leg = float(row.get("cash_reservation_cny", 0.0))
                    exposure_leg = 0.0
                    margin_leg = float(row.get("margin_reservation_cny", amt))
                resv[rid] = _ReservationState(
                    reservation_id=rid,
                    market=mk,
                    reference_id=str(row["reference_id"]),
                    risk_unit_key=str(row.get("risk_unit_key") or ""),
                    original_amount_cny=amt,
                    remaining_amount_cny=amt,
                    authority_id=str(row.get("authority_id") or ""),
                    authority_generation=int(row.get("authority_generation", 1)),
                    execution_lineage_id=str(row.get("execution_lineage_id") or ""),
                    lineage_sha256=str(row.get("lineage_sha256") or ""),
                    point_in_time_as_of=str(row.get("point_in_time_as_of") or ""),
                    event_id=eid,
                    trade_date=str(row.get("trade_date") or "").replace("-", ""),
                    original_cash_cny=cash_leg,
                    remaining_cash_cny=cash_leg,
                    original_exposure_cny=exposure_leg,
                    remaining_exposure_cny=exposure_leg,
                    original_margin_cny=margin_leg,
                    remaining_margin_cny=margin_leg,
                )
                exec_lineage = str(row.get("execution_lineage_id") or exec_lineage)

            elif et == "fill_commit":
                rid = str(row.get("reservation_id") or "").strip()
                r = resv.get(rid)
                if r is None:
                    raise MarketCapitalLedgerError(f"unknown_fill_reservation:{i}")
                if r.terminal:
                    raise MarketCapitalLedgerError(f"fill_after_terminal:{i}")
                cash_consumed = float(row.get("cash_reservation_consumed_cny", 0.0))
                exposure_consumed = float(
                    row.get("exposure_reservation_consumed_cny", 0.0)
                )
                margin_consumed = float(row.get("margin_reservation_consumed_cny", 0.0))
                if cash_consumed > r.remaining_cash_cny + 1e-9:
                    raise MarketCapitalLedgerError(f"fill_cash_exceeds:{i}")
                if exposure_consumed > r.remaining_exposure_cny + 1e-9:
                    raise MarketCapitalLedgerError(f"fill_exposure_exceeds:{i}")
                if margin_consumed > r.remaining_margin_cny + 1e-9:
                    raise MarketCapitalLedgerError(f"fill_margin_exceeds:{i}")
                r.remaining_cash_cny = round(r.remaining_cash_cny - cash_consumed, 6)
                r.remaining_exposure_cny = round(
                    r.remaining_exposure_cny - exposure_consumed, 6
                )
                r.remaining_margin_cny = round(
                    r.remaining_margin_cny - margin_consumed, 6
                )
                if bool(row.get("terminal")):
                    r.remaining_cash_cny = 0.0
                    r.remaining_exposure_cny = 0.0
                    r.remaining_margin_cny = 0.0
                    r.terminal = True
                r.last_fill_sequence = int(row.get("fill_sequence", 0))
                r.remaining_amount_cny = (
                    r.remaining_cash_cny if mk == "ashare" else r.remaining_margin_cny
                )
                risk_unit = str(row.get("risk_unit_key") or r.risk_unit_key)
                cash_debit = float(row.get("actual_cash_debit_cny", 0.0))
                lcash = round(lcash - cash_debit, 6)
                if mk == "ashare":
                    exposure = float(row.get("actual_exposure_cny", 0.0))
                    lpmv_d[risk_unit] = round(
                        float(lpmv_d.get(risk_unit, 0.0)) + exposure, 6
                    )
                    lqty_d[risk_unit] = int(lqty_d.get(risk_unit, 0)) + int(
                        row.get("actual_filled_quantity", 0)
                    )
                    lcost_d[risk_unit] = round(
                        float(lcost_d.get(risk_unit, 0.0)) + exposure,
                        6,
                    )
                    lentry_fee_d[risk_unit] = round(
                        float(lentry_fee_d.get(risk_unit, 0.0))
                        + float(row.get("actual_fee_cash_cny", 0.0)),
                        6,
                    )
                    lpmv = round(sum(lpmv_d.values()), 6)
                    leq = lcash + lpmv
                else:
                    margin = float(row.get("actual_margin_cny", 0.0))
                    lpm_d[risk_unit] = round(
                        float(lpm_d.get(risk_unit, 0.0)) + margin, 6
                    )
                    fill_quantity = int(row.get("actual_filled_quantity", 0))
                    fill_side = str(row.get("side") or "").strip().lower()
                    fill_sign = 1 if fill_side in {"buy", "long"} else -1
                    signed_fill_quantity = fill_sign * fill_quantity
                    existing_quantity = int(lqty_d.get(risk_unit, 0))
                    if (
                        existing_quantity
                        and existing_quantity * signed_fill_quantity < 0
                    ):
                        raise MarketCapitalLedgerError(f"fill_direction_conflict:{i}")
                    existing_abs_quantity = abs(existing_quantity)
                    combined_abs_quantity = existing_abs_quantity + fill_quantity
                    fill_price = float(row.get("actual_fill_price", 0.0))
                    existing_entry = float(lentry_price_d.get(risk_unit, 0.0))
                    weighted_entry = (
                        (
                            existing_entry * existing_abs_quantity
                            + fill_price * fill_quantity
                        )
                        / combined_abs_quantity
                        if combined_abs_quantity > 0
                        else 0.0
                    )
                    multiplier = float(row.get("contract_multiplier", 0.0))
                    spec_sha = str(row.get("contract_spec_sha256") or "")
                    if risk_unit in lmultiplier_d and not math.isclose(
                        lmultiplier_d[risk_unit], multiplier, abs_tol=1e-12
                    ):
                        raise MarketCapitalLedgerError(f"fill_multiplier_conflict:{i}")
                    if (
                        risk_unit in lcontract_spec_sha_d
                        and lcontract_spec_sha_d[risk_unit] != spec_sha
                    ):
                        raise MarketCapitalLedgerError(
                            f"fill_contract_spec_conflict:{i}"
                        )
                    lqty_d[risk_unit] = existing_quantity + signed_fill_quantity
                    lentry_price_d[risk_unit] = round(weighted_entry, 10)
                    lside_d[risk_unit] = "long" if lqty_d[risk_unit] > 0 else "short"
                    lmultiplier_d[risk_unit] = multiplier
                    lcontract_spec_sha_d[risk_unit] = spec_sha
                    lmgn = round(sum(lpm_d.values()), 6)
                    fee_cash = float(row.get("actual_fee_cash_cny", 0.0))
                    rpnl = round(rpnl - fee_cash, 6)
                    leq = lcash + lupnl
                pending_fill_commit_ids.append(eid)
                exec_lineage = str(row.get("execution_lineage_id") or exec_lineage)

            elif et == "ashare_sell_commit":
                if mk != "ashare":
                    raise MarketCapitalLedgerError(f"ashare_sell_market:{i}")
                risk_unit = str(row.get("risk_unit_key") or "").strip()
                if not risk_unit:
                    raise MarketCapitalLedgerError(f"ashare_sell_risk_unit:{i}")
                closed_quantity = int(row.get("actual_closed_quantity", 0))
                current_quantity = int(lqty_d.get(risk_unit, 0))
                exposure_released = float(row.get("actual_exposure_released_cny", 0.0))
                current_exposure = float(lpmv_d.get(risk_unit, 0.0))
                current_cost_basis = float(lcost_d.get(risk_unit, 0.0))
                cost_basis_released = float(
                    row.get("actual_cost_basis_released_cny", 0.0)
                )
                entry_fee_released = float(
                    row.get("actual_entry_fee_released_cny", 0.0)
                )
                current_entry_fee = float(lentry_fee_d.get(risk_unit, 0.0))
                if (
                    closed_quantity <= 0
                    or closed_quantity > current_quantity
                    or exposure_released <= 0.0
                    or exposure_released > current_exposure + 1e-9
                    or cost_basis_released <= 0.0
                    or cost_basis_released > current_cost_basis + 1e-9
                    or entry_fee_released < 0.0
                    or entry_fee_released > current_entry_fee + 1e-9
                ):
                    raise MarketCapitalLedgerError(f"ashare_sell_exceeds:{i}")
                remaining_quantity = current_quantity - closed_quantity
                remaining_exposure = round(current_exposure - exposure_released, 6)
                if remaining_quantity == 0:
                    lqty_d.pop(risk_unit, None)
                    lpmv_d.pop(risk_unit, None)
                    lcost_d.pop(risk_unit, None)
                    lentry_fee_d.pop(risk_unit, None)
                else:
                    lqty_d[risk_unit] = remaining_quantity
                    lpmv_d[risk_unit] = remaining_exposure
                    lcost_d[risk_unit] = round(
                        current_cost_basis - cost_basis_released,
                        6,
                    )
                    lentry_fee_d[risk_unit] = round(
                        current_entry_fee - entry_fee_released,
                        6,
                    )
                lpmv = round(sum(lpmv_d.values()), 6)
                net_cash_credit = float(row.get("actual_net_cash_credit_cny", 0.0))
                net_realized = float(row.get("amount_cny", 0.0))
                lcash = round(lcash + net_cash_credit, 6)
                rpnl = round(rpnl + net_realized, 6)
                leq = lcash + lpmv
                peak = max(peak, leq)
                pending_fill_commit_ids.append(eid)
                exec_lineage = str(row.get("execution_lineage_id") or exec_lineage)

            elif et == "position_close_commit":
                if mk != "cn_futures":
                    raise MarketCapitalLedgerError(f"close_commit_market:{i}")
                risk_unit = str(row.get("risk_unit_key") or "").strip()
                if not risk_unit:
                    raise MarketCapitalLedgerError(f"close_commit_risk_unit:{i}")
                margin_released = float(row.get("actual_margin_released_cny", 0.0))
                current_margin = float(lpm_d.get(risk_unit, 0.0))
                if margin_released <= 0.0 or margin_released > current_margin + 1e-9:
                    raise MarketCapitalLedgerError(f"close_margin_exceeds:{i}")
                remaining_margin = round(current_margin - margin_released, 6)
                if remaining_margin <= 1e-9:
                    lpm_d.pop(risk_unit, None)
                else:
                    lpm_d[risk_unit] = remaining_margin
                current_quantity = int(lqty_d.get(risk_unit, 0))
                closed_quantity = int(row.get("actual_closed_quantity", 0))
                remaining_quantity = max(0, abs(current_quantity) - closed_quantity)
                if remaining_quantity == 0:
                    lqty_d.pop(risk_unit, None)
                    lentry_price_d.pop(risk_unit, None)
                    lside_d.pop(risk_unit, None)
                    lmultiplier_d.pop(risk_unit, None)
                    lcontract_spec_sha_d.pop(risk_unit, None)
                else:
                    lqty_d[risk_unit] = (
                        remaining_quantity
                        if current_quantity > 0
                        else -remaining_quantity
                    )
                lmgn = round(sum(lpm_d.values()), 6)
                gross_realized = float(row.get("actual_gross_realized_pnl_cny", 0.0))
                fee_cash = float(row.get("actual_fee_cash_cny", 0.0))
                net_realized = round(gross_realized - fee_cash, 6)
                lcash = round(lcash + net_realized, 6)
                rpnl = round(rpnl + net_realized, 6)
                leq = lcash + lupnl
                peak = max(peak, leq)
                pending_fill_commit_ids.append(eid)
                exec_lineage = str(row.get("execution_lineage_id") or exec_lineage)

            elif et == "release":
                rid = str(row.get("reservation_id") or "").strip()
                r = resv.get(rid)
                if r is None:
                    raise MarketCapitalLedgerError(f"unknown_rel:{i}")
                if mk != r.market:
                    raise MarketCapitalLedgerError(f"rel_mkt:{i}")
                if amt > r.remaining_amount_cny + 1e-9:
                    raise MarketCapitalLedgerError(f"rel_exceeds:{i}")
                r.remaining_amount_cny = round(r.remaining_amount_cny - amt, 6)
                cash_release = min(
                    r.remaining_cash_cny,
                    float(row.get("cash_release_cny", amt if mk == "ashare" else 0.0)),
                )
                exposure_release = min(
                    r.remaining_exposure_cny,
                    float(
                        row.get("exposure_release_cny", amt if mk == "ashare" else 0.0)
                    ),
                )
                margin_release = min(
                    r.remaining_margin_cny,
                    float(
                        row.get(
                            "margin_release_cny", amt if mk == "cn_futures" else 0.0
                        )
                    ),
                )
                r.remaining_cash_cny = round(r.remaining_cash_cny - cash_release, 6)
                r.remaining_exposure_cny = round(
                    r.remaining_exposure_cny - exposure_release, 6
                )
                r.remaining_margin_cny = round(
                    r.remaining_margin_cny - margin_release, 6
                )
                r.remaining_amount_cny = (
                    r.remaining_cash_cny if mk == "ashare" else r.remaining_margin_cny
                )
                if (
                    r.remaining_cash_cny <= 1e-9
                    and r.remaining_exposure_cny <= 1e-9
                    and r.remaining_margin_cny <= 1e-9
                ):
                    r.remaining_cash_cny = 0.0
                    r.remaining_exposure_cny = 0.0
                    r.remaining_margin_cny = 0.0
                    r.remaining_amount_cny = 0.0
                    r.terminal = True

            elif et == "realized_pnl":
                rpnl += amt
                lcash += amt
                leq = lcash + lupnl
                peak = max(peak, leq)

            elif et == "reconcile":
                has_reconcile = True
                lcash = float(row.get("cash_balance_cny", lcash))
                lupnl = float(row.get("unrealized_pnl_cny", lupnl))
                lpmv = float(
                    row.get(
                        "positions_market_value_cny",
                        sum(
                            float(v)
                            for v in (row.get("positions_market_value") or {}).values()
                        ),
                    )
                )
                lmgn = float(
                    row.get(
                        "margin_used_cny",
                        sum(
                            float(v)
                            for v in (
                                row.get("position_margin_by_risk_unit") or {}
                            ).values()
                        ),
                    )
                )
                lfrozen_cash = float(row.get("frozen_order_cash_cny", 0.0))
                lfrozen_margin = float(row.get("frozen_order_margin_cny", 0.0))
                lpmv_d = dict(row.get("positions_market_value") or {})
                lpm_d = dict(row.get("position_margin_by_risk_unit") or {})
                incoming_quantities = row.get("positions_quantity_by_risk_unit")
                if isinstance(incoming_quantities, Mapping):
                    lqty_d = {
                        str(key): int(value)
                        for key, value in incoming_quantities.items()
                    }
                else:
                    lqty_d = {
                        key: quantity
                        for key, quantity in lqty_d.items()
                        if key in (lpmv_d if self.policy.market == "ashare" else lpm_d)
                    }
                incoming_cost_basis = row.get("positions_cost_basis_cny_by_risk_unit")
                if isinstance(incoming_cost_basis, Mapping):
                    lcost_d = {
                        str(key): float(value)
                        for key, value in incoming_cost_basis.items()
                    }
                else:
                    lcost_d = {
                        key: cost_basis
                        for key, cost_basis in lcost_d.items()
                        if key in lpmv_d
                    }
                incoming_entry_fees = row.get("positions_entry_fee_cny_by_risk_unit")
                if isinstance(incoming_entry_fees, Mapping):
                    lentry_fee_d = {
                        str(key): float(value)
                        for key, value in incoming_entry_fees.items()
                    }
                else:
                    lentry_fee_d = {
                        key: entry_fee
                        for key, entry_fee in lentry_fee_d.items()
                        if key in lpmv_d
                    }
                if self.policy.market == "cn_futures":
                    incoming_entry_prices = row.get("position_entry_price_by_risk_unit")
                    incoming_sides = row.get("position_side_by_risk_unit")
                    incoming_multipliers = row.get(
                        "position_contract_multiplier_by_risk_unit"
                    )
                    incoming_spec_shas = row.get(
                        "position_contract_spec_sha256_by_risk_unit"
                    )
                    if isinstance(incoming_entry_prices, Mapping):
                        lentry_price_d = {
                            str(key): float(value)
                            for key, value in incoming_entry_prices.items()
                        }
                    else:
                        lentry_price_d = {
                            key: value
                            for key, value in lentry_price_d.items()
                            if key in lpm_d
                        }
                    if isinstance(incoming_sides, Mapping):
                        lside_d = {
                            str(key): str(value)
                            for key, value in incoming_sides.items()
                        }
                    else:
                        lside_d = {
                            key: value for key, value in lside_d.items() if key in lpm_d
                        }
                    if isinstance(incoming_multipliers, Mapping):
                        lmultiplier_d = {
                            str(key): float(value)
                            for key, value in incoming_multipliers.items()
                        }
                    else:
                        lmultiplier_d = {
                            key: value
                            for key, value in lmultiplier_d.items()
                            if key in lpm_d
                        }
                    if isinstance(incoming_spec_shas, Mapping):
                        lcontract_spec_sha_d = {
                            str(key): str(value)
                            for key, value in incoming_spec_shas.items()
                        }
                    else:
                        lcontract_spec_sha_d = {
                            key: value
                            for key, value in lcontract_spec_sha_d.items()
                            if key in lpm_d
                        }
                leq = (
                    (lcash + lpmv)
                    if self.policy.market == "ashare"
                    else (lcash + lupnl)
                )
                peak = max(peak, leq)
                exec_lineage = str(row.get("execution_lineage_id") or exec_lineage)
                pending_fill_commit_ids = []
                mh.append(
                    {
                        "as_of": str(
                            row.get("as_of") or str(row.get("trade_date") or "")
                        ),
                        "equity_cny": leq,
                        "cash_balance_cny": lcash,
                        "positions_market_value_cny": lpmv,
                        "unrealized_pnl_cny": lupnl,
                    }
                )

            last_eid = eid
            upd = str(row["created_at"])

        if bcnt != 1:
            raise MarketCapitalLedgerError("bootstrap_count")

        reserved_cash = round(sum(v.remaining_cash_cny for v in resv.values()), 6)
        reserved_exposure = round(
            sum(v.remaining_exposure_cny for v in resv.values()), 6
        )
        reserved_margin = round(sum(v.remaining_margin_cny for v in resv.values()), 6)
        reserved = reserved_cash if self.policy.market == "ashare" else reserved_margin

        # available_to_reserve: min(cash constraint, remaining market cap)
        if self.policy.market == "ashare":
            gross_used = lpmv + lfrozen_cash + reserved_exposure
            cash_available = lcash - lfrozen_cash - reserved_cash
            avail = max(
                0.0,
                min(
                    cash_available,
                    self.policy.stock_gross_exposure_limit_cny - gross_used,
                ),
            )
            total_deployed = gross_used
        else:
            margin_used_total = lmgn + lfrozen_margin + reserved_margin
            cash_available = (
                lcash - lfrozen_cash - lfrozen_margin - reserved_margin - reserved_cash
            )
            avail = max(
                0.0,
                min(
                    cash_available,
                    self.policy.margin_utilization_limit_cny - margin_used_total,
                ),
            )
            total_deployed = margin_used_total

        util = (
            round(total_deployed / self.policy.initial_equity_cny, 6)
            if self.policy.initial_equity_cny > 0
            else 0.0
        )

        snap = MarketCapitalSnapshot(
            source="market_capital_ledger",
            schema_version="market-capital-snapshot.v2",
            authority_id=self.policy.capital_authority_id,
            authority_generation=self.policy.authority_generation,
            account_name=self.policy.account_name,
            market=self.policy.market,
            currency=self.policy.currency,
            initial_equity_cny=self.policy.initial_equity_cny,
            equity_cny=round(leq, 6),
            cash_balance_cny=round(lcash, 6),
            positions_market_value_cny=round(lpmv, 6),
            margin_used_cny=round(lmgn, 6),
            frozen_order_cash_cny=round(lfrozen_cash, 6),
            frozen_order_margin_cny=round(lfrozen_margin, 6),
            realized_pnl_cny=round(rpnl, 6),
            unrealized_pnl_cny=round(lupnl, 6),
            reserved_capital_cny=reserved,
            active_reservations_cny=reserved,
            available_to_reserve_cny=round(avail, 6),
            capital_utilization_rate=util,
            stock_gross_exposure_limit_cny=self.policy.stock_gross_exposure_limit_cny,
            margin_utilization_limit_cny=self.policy.margin_utilization_limit_cny,
            reconciled=has_reconcile,
            event_id=last_eid,
            event_checksum=str(prev_expected or ""),
            updated_at=upd,
            execution_lineage_id=exec_lineage,
            real_trading_enabled=False,
            reserved_cash_cny=reserved_cash,
            reserved_exposure_cny=reserved_exposure,
            reserved_margin_cny=reserved_margin,
            unreconciled_fill_commit_ids=tuple(pending_fill_commit_ids),
            positions_quantity_by_risk_unit=dict(lqty_d),
            positions_cost_basis_cny_by_risk_unit=dict(lcost_d),
            positions_entry_fee_cny_by_risk_unit=dict(lentry_fee_d),
            position_entry_price_by_risk_unit=dict(lentry_price_d),
            position_side_by_risk_unit=dict(lside_d),
            position_contract_multiplier_by_risk_unit=dict(lmultiplier_d),
            position_contract_spec_sha256_by_risk_unit=dict(lcontract_spec_sha_d),
        )
        return _ReplayState(
            snapshot=snap,
            reservations=resv,
            mtm_equity_history=mh,
            peak_mtm_equity=peak,
            latest_mtm_equity=leq,
            latest_positions_mv=lpmv_d,
            latest_position_margin=lpm_d,
            latest_position_quantity=lqty_d,
            latest_position_cost_basis=lcost_d,
            latest_position_entry_fee=lentry_fee_d,
            latest_position_entry_price=lentry_price_d,
            latest_position_side=lside_d,
            latest_position_contract_multiplier=lmultiplier_d,
            latest_position_contract_spec_sha256=lcontract_spec_sha_d,
            unreconciled_fill_commit_ids=pending_fill_commit_ids,
        )

    def _project_ashare_sellable_quantity_unlocked(
        self,
        events: list[dict[str, Any]],
        replay: _ReplayState,
        *,
        as_of: datetime,
    ) -> dict[str, int]:
        """Rebuild A-share T+1 inventory from immutable fill/sell events."""

        if self.policy.market != "ashare":
            raise MarketCapitalLedgerError("ashare_sellable_projection_only_ashare")
        as_of_cn = as_of.astimezone(CN_TZ)
        lots_by_risk_unit: dict[str, list[_AshareQuantityLot]] = {}
        previous_filled_at: datetime | None = None

        for index, row in enumerate(events, 1):
            event_type = str(row.get("event_type") or "")
            if event_type not in {"fill_commit", "ashare_sell_commit"}:
                continue
            risk_unit = str(row.get("risk_unit_key") or "").strip().upper()
            if not risk_unit:
                raise MarketCapitalLedgerError(
                    f"ashare_sellable_risk_unit_missing:{index}"
                )
            quantity_field = (
                "actual_filled_quantity"
                if event_type == "fill_commit"
                else "actual_closed_quantity"
            )
            quantity = row.get(quantity_field)
            if (
                not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity <= 0
            ):
                raise MarketCapitalLedgerError(
                    f"ashare_sellable_quantity_invalid:{index}"
                )
            side = str(row.get("side") or "").strip().lower()
            expected_side = "buy" if event_type == "fill_commit" else "sell"
            if side != expected_side:
                raise MarketCapitalLedgerError(f"ashare_sellable_side_invalid:{index}")
            filled_at = _parse_timestamp(
                str(row.get("filled_at") or ""),
                field="ashare_sellable_filled_at",
            ).astimezone(CN_TZ)
            if filled_at > as_of_cn:
                raise MarketCapitalLedgerError(
                    "ashare_sellable_as_of_before_ledger_fill"
                )
            if previous_filled_at is not None and filled_at < previous_filled_at:
                raise MarketCapitalLedgerError(
                    f"ashare_sellable_filled_at_regression:{index}"
                )
            previous_filled_at = filled_at
            event_trade_date = filled_at.strftime("%Y%m%d")
            lots = lots_by_risk_unit.setdefault(risk_unit, [])

            if event_type == "fill_commit":
                lots.append(
                    _AshareQuantityLot(
                        acquired_on=event_trade_date,
                        remaining_quantity=quantity,
                    )
                )
                continue

            remaining_to_close = quantity
            for lot in lots:
                if remaining_to_close <= 0:
                    break
                if lot.acquired_on >= event_trade_date:
                    continue
                consumed = min(lot.remaining_quantity, remaining_to_close)
                lot.remaining_quantity -= consumed
                remaining_to_close -= consumed
            if remaining_to_close:
                raise MarketCapitalLedgerError(
                    f"ashare_sellable_history_t1_violation:{index}"
                )

        projected_positions = {
            risk_unit: sum(lot.remaining_quantity for lot in lots)
            for risk_unit, lots in lots_by_risk_unit.items()
            if sum(lot.remaining_quantity for lot in lots) > 0
        }
        latest_positions: dict[str, int] = {}
        for raw_risk_unit, quantity in replay.latest_position_quantity.items():
            risk_unit = str(raw_risk_unit or "").strip().upper()
            if (
                not risk_unit
                or risk_unit in latest_positions
                or not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity <= 0
            ):
                raise MarketCapitalLedgerError(
                    "ashare_sellable_latest_position_invalid"
                )
            latest_positions[risk_unit] = quantity
        if projected_positions != latest_positions:
            raise MarketCapitalLedgerError(
                "ashare_sellable_projection_position_mismatch"
            )

        query_trade_date = as_of_cn.strftime("%Y%m%d")
        return {
            risk_unit: sum(
                lot.remaining_quantity
                for lot in lots_by_risk_unit.get(risk_unit, [])
                if lot.acquired_on < query_trade_date
            )
            for risk_unit in latest_positions
        }

    # ---- public API ----

    def _dec(
        self, *, approved: bool, reason: str, **kw
    ) -> MarketCapitalReservationDecision:
        return MarketCapitalReservationDecision(approved=approved, reason=reason, **kw)

    def _ensure_init(self) -> None:
        if not self._initialized and not self.events_path.exists():
            raise MarketCapitalLedgerError("not_initialized")

    def snapshot(self) -> MarketCapitalSnapshot:
        self._ensure_init()
        with self._lock():
            r = self._replay(self._load_events_unlocked())
            return r.snapshot

    def ashare_sellable_quantities(self, trade_date: str) -> dict[str, int]:
        """Return T+1 sellable quantities for every currently held A-share.

        ``trade_date`` is the exchange-local date (``YYYYMMDD`` or
        ``YYYY-MM-DD``).  The projection remains derived exclusively from the
        immutable fill/sell event stream and is cross-checked against the
        ledger's current position quantities before it is returned.
        """

        self._ensure_init()
        normalized_trade_date = _validate_trade_date(trade_date)
        try:
            query_date = datetime.strptime(normalized_trade_date, "%Y%m%d")
        except ValueError as exc:
            raise MarketCapitalLedgerError("invalid_trade_date") from exc
        end_of_trade_date = query_date.replace(
            hour=23,
            minute=59,
            second=59,
            microsecond=999999,
            tzinfo=CN_TZ,
        )
        with self._lock():
            events = self._load_events_unlocked()
            replay = self._replay(events)
            return self._project_ashare_sellable_quantity_unlocked(
                events,
                replay,
                as_of=end_of_trade_date,
            )

    def _risk_metrics(
        self, events: list[dict], replay: _ReplayState, *, trade_date: str
    ) -> dict:
        daily_rpnl = 0.0
        cons = 0
        mh = replay.mtm_equity_history
        dse: float | None = None
        for e in reversed(mh):
            if str(e.get("as_of") or "").replace("-", "") < trade_date:
                dse = float(e["equity_cny"])
                break
        if dse is None:
            dse = self.policy.initial_equity_cny
        cur = replay.latest_mtm_equity
        dmtm = cur - dse
        dd = max(0.0, replay.peak_mtm_equity - cur)

        for row in events:
            event_type = str(row.get("event_type") or "")
            if event_type == "realized_pnl":
                a = float(row["amount_cny"])
                affects_loss_streak = row.get("affects_loss_streak", True) is True
            elif event_type == "fill_commit" and self.policy.market == "cn_futures":
                a = -float(row.get("actual_fee_cash_cny", 0.0))
                affects_loss_streak = False
            elif event_type == "position_close_commit":
                a = float(row.get("amount_cny", 0.0))
                affects_loss_streak = True
            elif event_type == "ashare_sell_commit":
                a = float(row.get("amount_cny", 0.0))
                affects_loss_streak = True
            else:
                continue
            etd = str(row.get("trade_date") or "").replace("-", "")
            if not etd:
                event_time = str(row.get("filled_at") or row["created_at"])
                etd = datetime.fromisoformat(
                    event_time.replace("Z", "+00:00")
                ).strftime("%Y%m%d")
            if etd == trade_date:
                daily_rpnl += a
            if not affects_loss_streak:
                continue
            if a < 0:
                cons += 1
            elif a > 0:
                cons = 0
        return {
            "daily_mtm_change": round(dmtm, 6),
            "daily_realized_pnl": round(daily_rpnl, 6),
            "consecutive_losses": cons,
            "high_water_equity": round(replay.peak_mtm_equity, 6),
            "current_equity": round(cur, 6),
            "drawdown": round(dd, 6),
        }

    def provider_state(self, trade_date: str) -> dict:
        self._ensure_init()
        nd = _validate_trade_date(trade_date)
        with self._lock():
            ev = self._load_events_unlocked()
            r = self._replay(ev)
        risk = self._risk_metrics(ev, r, trade_date=nd)
        s = r.snapshot
        # fresh only after actual current-day reconcile
        is_fresh = any(
            e.get("event_type") == "reconcile"
            and str(e.get("as_of") or str(e.get("trade_date") or "")).replace("-", "")
            == nd
            for e in ev
        )
        state = {
            **s.as_dict(),
            "trade_date": nd,
            "fresh": is_fresh,
            # The replay above validates every event checksum and previous-head
            # link under the same ledger lock used to build this state.  These
            # fields are mandatory inputs to the A-share position authority.
            "checksum_status": "valid",
            "checksum_event_count": len(ev),
            "checksum_last": s.event_checksum,
            "last_reconciled_trade_date": (
                r.mtm_equity_history[-1]["as_of"] if r.mtm_equity_history else ""
            ),
            "cumulative_pnl": round(s.realized_pnl_cny, 6),
            "daily_mtm_change": risk["daily_mtm_change"],
            "daily_realized_pnl": risk["daily_realized_pnl"],
            "max_daily_loss": round(
                self.policy.initial_equity_cny * self.policy.daily_loss_pause_pct, 6
            ),
            "consecutive_losses": risk["consecutive_losses"],
            "max_consecutive_losses": self.policy.max_consecutive_losses,
            "high_water_equity": risk["high_water_equity"],
            "max_drawdown": round(
                self.policy.initial_equity_cny * self.policy.drawdown_halt_pct, 6
            ),
            "real_trading_enabled": False,
        }
        if self.policy.market == "ashare":
            from .ashare_position_authority import normalize_ashare_positions

            normalized_positions, _, normalization_reason = normalize_ashare_positions(
                s.positions_quantity_by_risk_unit
            )
            if normalized_positions is None:
                raise MarketCapitalLedgerError(
                    f"ashare_position_projection_invalid:{normalization_reason}"
                )
            state["single_name_cap_cny"] = self.policy.single_name_cap_cny
            state["stock_gross_exposure_limit_cny"] = (
                self.policy.stock_gross_exposure_limit_cny
            )
            state["position_count"] = len(normalized_positions)
            state["positions_fingerprint"] = _sha256_hex(
                json.dumps(
                    normalized_positions,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            state["margin_utilization_limit_cny"] = (
                self.policy.margin_utilization_limit_cny
            )
            state["available_margin"] = s.available_to_reserve_cny
        return state

    @staticmethod
    def _active_reservation_manifest(
        reservations: Mapping[str, _ReservationState],
    ) -> dict[str, dict[str, Any]]:
        manifest: dict[str, dict[str, Any]] = {}
        for reservation_id, reservation in sorted(reservations.items()):
            if (
                reservation.remaining_cash_cny <= 0.0
                and reservation.remaining_exposure_cny <= 0.0
                and reservation.remaining_margin_cny <= 0.0
            ):
                continue
            manifest[reservation_id] = {
                "reservation_event_id": reservation.event_id,
                "reference_id": reservation.reference_id,
                "risk_unit_key": reservation.risk_unit_key,
                "remaining_cash_cny": round(reservation.remaining_cash_cny, 6),
                "remaining_exposure_cny": round(
                    reservation.remaining_exposure_cny,
                    6,
                ),
                "remaining_margin_cny": round(
                    reservation.remaining_margin_cny,
                    6,
                ),
                "terminal": reservation.terminal,
                "last_fill_sequence": reservation.last_fill_sequence,
            }
        return manifest

    def active_reservation_manifest(self) -> dict[str, dict[str, Any]]:
        self._ensure_init()
        with self._lock():
            replay = self._replay(self._load_events_unlocked())
            return self._active_reservation_manifest(replay.reservations)

    # ---- MTM reconcile (conservation) ----

    def mtm_reconcile(self, manifest: ReconcileManifest) -> dict:
        self._ensure_init()
        if _normalize_market(manifest.market) != self.policy.market:
            raise MarketCapitalLedgerError("reconcile_market_mismatch")
        if manifest.authority_id != self.policy.capital_authority_id:
            raise MarketCapitalLedgerError("reconcile_aid_mismatch")
        if manifest.authority_generation != self.policy.authority_generation:
            raise MarketCapitalLedgerError("reconcile_gen_mismatch")
        if not _is_64hex(manifest.source_sha256):
            raise MarketCapitalLedgerError("source_sha256_not_64hex")
        if not str(manifest.source or "").strip():
            raise MarketCapitalLedgerError("reconcile_source_required")
        if not str(manifest.execution_lineage_id or "").strip():
            raise MarketCapitalLedgerError("reconcile_execution_lineage_required")
        if not _tz_aware(str(manifest.pit_timestamp or "")):
            raise MarketCapitalLedgerError("reconcile_pit_timestamp_timezone_required")
        if not isinstance(manifest.positions_market_value, Mapping):
            raise MarketCapitalLedgerError("invalid_positions_market_value")
        if not isinstance(manifest.position_margin_by_risk_unit, Mapping):
            raise MarketCapitalLedgerError("invalid_position_margin")

        as_of = _validate_trade_date(manifest.as_of)
        reconcile_pit_value = _parse_timestamp(
            manifest.pit_timestamp,
            field="reconcile_pit_timestamp",
        )
        if (
            _reconcile_trade_date_for_pit(
                self.policy.market,
                reconcile_pit_value,
            )
            != as_of
        ):
            raise MarketCapitalLedgerError("reconcile_pit_trade_date_mismatch")
        cash_balance = _strict_number(
            manifest.cash_balance_cny, field="cash_balance_cny"
        )
        unrealized_pnl = _strict_number(
            manifest.unrealized_pnl_cny, field="unrealized_pnl_cny"
        )
        active_reservations = _strict_number(
            manifest.active_reservations_cny, field="active_reservations_cny"
        )
        frozen_order_cash = _strict_number(
            manifest.frozen_order_cash_cny, field="frozen_order_cash_cny"
        )
        frozen_order_margin = _strict_number(
            manifest.frozen_order_margin_cny, field="frozen_order_margin_cny"
        )
        for field_name, value in (
            ("cash_balance_cny", cash_balance),
            ("active_reservations_cny", active_reservations),
            ("frozen_order_cash_cny", frozen_order_cash),
            ("frozen_order_margin_cny", frozen_order_margin),
        ):
            if value < 0.0:
                raise MarketCapitalLedgerError(f"negative_{field_name}")

        positions_market_value: dict[str, float] = {}
        for key, raw_value in manifest.positions_market_value.items():
            risk_unit = str(key or "").strip()
            if not risk_unit:
                raise MarketCapitalLedgerError("empty_position_risk_unit")
            value = _strict_number(raw_value, field=f"position_mv:{risk_unit}")
            if value < 0.0:
                raise MarketCapitalLedgerError(f"negative_position_mv:{risk_unit}")
            positions_market_value[risk_unit] = value

        position_margin: dict[str, float] = {}
        for key, raw_value in manifest.position_margin_by_risk_unit.items():
            risk_unit = str(key or "").strip()
            if not risk_unit:
                raise MarketCapitalLedgerError("empty_margin_risk_unit")
            value = _strict_number(raw_value, field=f"margin:{risk_unit}")
            if value < 0.0:
                raise MarketCapitalLedgerError(f"negative_margin:{risk_unit}")
            position_margin[risk_unit] = value

        def _numeric_inventory(
            raw: Mapping[str, Any] | None,
            *,
            field_name: str,
            integer: bool = False,
            positive: bool = False,
        ) -> dict[str, float] | dict[str, int]:
            if raw is None:
                return {}
            if not isinstance(raw, Mapping):
                raise MarketCapitalLedgerError(f"invalid_{field_name}")
            normalized: dict[str, float] | dict[str, int] = {}
            for raw_key, raw_value in raw.items():
                key = str(raw_key or "").strip()
                if not key:
                    raise MarketCapitalLedgerError(f"empty_{field_name}_risk_unit")
                if integer:
                    if (
                        not isinstance(raw_value, int)
                        or isinstance(raw_value, bool)
                        or raw_value == 0
                    ):
                        raise MarketCapitalLedgerError(f"invalid_{field_name}:{key}")
                    normalized[key] = int(raw_value)
                    continue
                value = _strict_number(raw_value, field=f"{field_name}:{key}")
                if positive and value <= 0.0:
                    raise MarketCapitalLedgerError(f"invalid_{field_name}:{key}")
                if not positive and value < 0.0:
                    raise MarketCapitalLedgerError(f"negative_{field_name}:{key}")
                normalized[key] = value
            return normalized

        positions_quantity = _numeric_inventory(
            manifest.positions_quantity_by_risk_unit,
            field_name="positions_quantity",
            integer=True,
        )
        positions_cost_basis = _numeric_inventory(
            manifest.positions_cost_basis_cny_by_risk_unit,
            field_name="positions_cost_basis",
        )
        positions_entry_fee = _numeric_inventory(
            manifest.positions_entry_fee_cny_by_risk_unit,
            field_name="positions_entry_fee",
        )
        position_entry_price = _numeric_inventory(
            manifest.position_entry_price_by_risk_unit,
            field_name="position_entry_price",
            positive=True,
        )
        position_multiplier = _numeric_inventory(
            manifest.position_contract_multiplier_by_risk_unit,
            field_name="position_contract_multiplier",
            positive=True,
        )
        position_mark_price = _numeric_inventory(
            manifest.position_mark_price_by_risk_unit,
            field_name="position_mark_price",
            positive=True,
        )

        position_side_raw = manifest.position_side_by_risk_unit or {}
        if not isinstance(position_side_raw, Mapping):
            raise MarketCapitalLedgerError("invalid_position_side")
        position_side = {
            str(key or "").strip(): str(value or "").strip().lower()
            for key, value in position_side_raw.items()
        }
        if any(
            not key or value not in {"long", "short"}
            for key, value in position_side.items()
        ):
            raise MarketCapitalLedgerError("invalid_position_side")

        position_spec_raw = manifest.position_contract_spec_sha256_by_risk_unit or {}
        if not isinstance(position_spec_raw, Mapping):
            raise MarketCapitalLedgerError("invalid_position_contract_spec_sha256")
        position_contract_spec_sha = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in position_spec_raw.items()
        }
        if any(
            not key or not _is_64hex(value)
            for key, value in position_contract_spec_sha.items()
        ):
            raise MarketCapitalLedgerError("invalid_position_contract_spec_sha256")

        active_reservation_map: dict[str, dict[str, Any]] | None = None
        if manifest.active_reservations is not None:
            if not isinstance(manifest.active_reservations, Mapping):
                raise MarketCapitalLedgerError("invalid_active_reservation_map")
            active_reservation_map = {}
            required_reservation_fields = {
                "reservation_event_id",
                "reference_id",
                "risk_unit_key",
                "remaining_cash_cny",
                "remaining_exposure_cny",
                "remaining_margin_cny",
                "terminal",
                "last_fill_sequence",
            }
            for raw_reservation_id, raw_state in manifest.active_reservations.items():
                reservation_id = str(raw_reservation_id or "").strip()
                if not reservation_id or not isinstance(raw_state, Mapping):
                    raise MarketCapitalLedgerError("invalid_active_reservation_map")
                if set(raw_state) != required_reservation_fields:
                    raise MarketCapitalLedgerError(
                        "invalid_active_reservation_map_fields"
                    )
                remaining_cash = _strict_number(
                    raw_state.get("remaining_cash_cny"),
                    field="remaining_cash_cny",
                )
                remaining_exposure = _strict_number(
                    raw_state.get("remaining_exposure_cny"),
                    field="remaining_exposure_cny",
                )
                remaining_margin = _strict_number(
                    raw_state.get("remaining_margin_cny"),
                    field="remaining_margin_cny",
                )
                if min(remaining_cash, remaining_exposure, remaining_margin) < 0.0:
                    raise MarketCapitalLedgerError("negative_active_reservation_leg")
                terminal = raw_state.get("terminal")
                last_fill_sequence = raw_state.get("last_fill_sequence")
                if not isinstance(terminal, bool):
                    raise MarketCapitalLedgerError(
                        "invalid_active_reservation_terminal"
                    )
                if (
                    not isinstance(last_fill_sequence, int)
                    or isinstance(last_fill_sequence, bool)
                    or last_fill_sequence < 0
                ):
                    raise MarketCapitalLedgerError(
                        "invalid_active_reservation_fill_sequence"
                    )
                string_fields = {
                    key: str(raw_state.get(key) or "").strip()
                    for key in (
                        "reservation_event_id",
                        "reference_id",
                        "risk_unit_key",
                    )
                }
                if any(not value for value in string_fields.values()):
                    raise MarketCapitalLedgerError(
                        "invalid_active_reservation_identity"
                    )
                active_reservation_map[reservation_id] = {
                    **string_fields,
                    "remaining_cash_cny": round(remaining_cash, 6),
                    "remaining_exposure_cny": round(remaining_exposure, 6),
                    "remaining_margin_cny": round(remaining_margin, 6),
                    "terminal": terminal,
                    "last_fill_sequence": last_fill_sequence,
                }

        included_fill_commit_ids = tuple(manifest.included_fill_commit_ids)
        if any(
            not isinstance(value, str) or not value.strip()
            for value in included_fill_commit_ids
        ) or len(set(included_fill_commit_ids)) != len(included_fill_commit_ids):
            raise MarketCapitalLedgerError("invalid_included_fill_commit_ids")

        ref_id = f"mtm-reconcile:{self.policy.account_name}:{as_of}"
        if str(manifest.expected_ledger_event_id or "").strip():
            # A fill-aware reconcile is a new same-day checkpoint, not a
            # conflicting replay of the opening/pre-open checkpoint.
            ref_id = f"{ref_id}:{manifest.expected_ledger_event_id}"

        if self.policy.market == "ashare":
            if position_margin or frozen_order_margin > 0.0:
                raise MarketCapitalLedgerError("ashare_margin_not_allowed")
            equity = cash_balance + sum(positions_market_value.values())
            margin_used = 0.0
        else:
            equity = cash_balance + unrealized_pnl
            margin_used = sum(position_margin.values())

        incoming_payload = {
            "cash_balance_cny": cash_balance,
            "positions_market_value": positions_market_value,
            "unrealized_pnl_cny": unrealized_pnl,
            "position_margin_by_risk_unit": position_margin,
            "active_reservations_cny": active_reservations,
            "frozen_order_cash_cny": frozen_order_cash,
            "frozen_order_margin_cny": frozen_order_margin,
            "execution_lineage_id": str(manifest.execution_lineage_id),
            "source": str(manifest.source),
            "source_sha256": str(manifest.source_sha256),
            "equity_cny": round(equity, 6),
            "margin_used_cny": round(margin_used, 6),
            "active_reservations": active_reservation_map,
            "expected_ledger_event_id": str(manifest.expected_ledger_event_id),
            "expected_ledger_checksum": str(manifest.expected_ledger_checksum),
            "included_fill_commit_ids": list(included_fill_commit_ids),
            "positions_quantity_by_risk_unit": dict(positions_quantity),
            "positions_cost_basis_cny_by_risk_unit": dict(positions_cost_basis),
            "positions_entry_fee_cny_by_risk_unit": dict(positions_entry_fee),
            "position_entry_price_by_risk_unit": dict(position_entry_price),
            "position_side_by_risk_unit": dict(position_side),
            "position_contract_multiplier_by_risk_unit": dict(position_multiplier),
            "position_contract_spec_sha256_by_risk_unit": dict(
                position_contract_spec_sha
            ),
            "position_mark_price_by_risk_unit": dict(position_mark_price),
            "canonical_snapshot_sha256": str(manifest.canonical_snapshot_sha256),
        }

        canonical_path_raw = str(manifest.canonical_snapshot_path or "").strip()
        canonical_sha = str(manifest.canonical_snapshot_sha256 or "").strip()
        if not canonical_path_raw or not Path(canonical_path_raw).is_absolute():
            raise MarketCapitalLedgerError(
                "reconcile_canonical_snapshot_absolute_path_required"
            )
        canonical_path = Path(canonical_path_raw)
        if canonical_path.is_symlink() or not canonical_path.is_file():
            raise MarketCapitalLedgerError("reconcile_canonical_snapshot_unavailable")
        if not _is_64hex(canonical_sha):
            raise MarketCapitalLedgerError(
                "reconcile_canonical_snapshot_sha256_invalid"
            )
        actual_canonical_sha = _sha256_file(canonical_path)
        if (
            actual_canonical_sha != canonical_sha
            or manifest.source_sha256 != canonical_sha
        ):
            raise MarketCapitalLedgerError(
                "reconcile_canonical_snapshot_sha256_mismatch"
            )
        try:
            canonical_source = json.loads(canonical_path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketCapitalLedgerError(
                "reconcile_canonical_snapshot_unreadable"
            ) from exc
        expected_canonical_source = {
            "schema_version": RECONCILE_SOURCE_SCHEMA_VERSION,
            "market": self.policy.market,
            "trade_date": as_of,
            "pit_timestamp": str(manifest.pit_timestamp),
            "execution_lineage_id": str(manifest.execution_lineage_id),
            "cash_balance_cny": cash_balance,
            "positions_market_value": positions_market_value,
            "unrealized_pnl_cny": unrealized_pnl,
            "position_margin_by_risk_unit": position_margin,
            "active_reservations_cny": active_reservations,
            "active_reservations": active_reservation_map,
            "frozen_order_cash_cny": frozen_order_cash,
            "frozen_order_margin_cny": frozen_order_margin,
            "positions_quantity_by_risk_unit": dict(positions_quantity),
            "positions_cost_basis_cny_by_risk_unit": dict(positions_cost_basis),
            "positions_entry_fee_cny_by_risk_unit": dict(positions_entry_fee),
            "position_entry_price_by_risk_unit": dict(position_entry_price),
            "position_side_by_risk_unit": dict(position_side),
            "position_contract_multiplier_by_risk_unit": dict(position_multiplier),
            "position_contract_spec_sha256_by_risk_unit": dict(
                position_contract_spec_sha
            ),
            "position_mark_price_by_risk_unit": dict(position_mark_price),
            "expected_ledger_event_id": str(manifest.expected_ledger_event_id),
            "expected_ledger_checksum": str(manifest.expected_ledger_checksum),
            "included_fill_commit_ids": list(included_fill_commit_ids),
            "real_trading_enabled": False,
        }
        if canonical_source != expected_canonical_source:
            raise MarketCapitalLedgerError(
                "reconcile_canonical_snapshot_content_mismatch"
            )

        with self._lock():
            ev = self._load_events_unlocked()
            r = self._replay(ev)

            def _float_maps_close(
                actual: Mapping[str, float],
                expected: Mapping[str, float],
            ) -> bool:
                return set(actual) == set(expected) and all(
                    math.isclose(
                        float(actual[key]),
                        float(expected[key]),
                        abs_tol=0.01,
                    )
                    for key in actual
                )

            if manifest.execution_lineage_id != r.snapshot.execution_lineage_id:
                raise MarketCapitalLedgerError("reconcile_execution_lineage_mismatch")
            if not math.isclose(
                cash_balance,
                r.snapshot.cash_balance_cny,
                abs_tol=0.01,
            ):
                raise MarketCapitalLedgerError("reconcile_cash_conservation_mismatch")
            if not math.isclose(
                frozen_order_cash,
                r.snapshot.frozen_order_cash_cny,
                abs_tol=0.01,
            ):
                raise MarketCapitalLedgerError(
                    "reconcile_frozen_cash_conservation_mismatch"
                )
            if not math.isclose(
                frozen_order_margin,
                r.snapshot.frozen_order_margin_cny,
                abs_tol=0.01,
            ):
                raise MarketCapitalLedgerError(
                    "reconcile_frozen_margin_conservation_mismatch"
                )

            if self.policy.market == "ashare":
                expected_quantities = r.latest_position_quantity
                expected_cost_basis = r.latest_position_cost_basis
                expected_entry_fees = r.latest_position_entry_fee
                if set(positions_market_value) != set(expected_quantities):
                    raise MarketCapitalLedgerError(
                        "reconcile_ashare_position_set_mismatch"
                    )
                if dict(positions_quantity) != expected_quantities:
                    raise MarketCapitalLedgerError(
                        "reconcile_ashare_position_quantity_mismatch"
                    )
                if not _float_maps_close(
                    positions_cost_basis,
                    expected_cost_basis,
                ):
                    raise MarketCapitalLedgerError(
                        "reconcile_ashare_cost_basis_mismatch"
                    )
                if not _float_maps_close(
                    positions_entry_fee,
                    expected_entry_fees,
                ):
                    raise MarketCapitalLedgerError(
                        "reconcile_ashare_entry_fee_mismatch"
                    )
                if any(
                    (
                        position_entry_price,
                        position_side,
                        position_multiplier,
                        position_contract_spec_sha,
                        position_mark_price,
                    )
                ):
                    raise MarketCapitalLedgerError(
                        "reconcile_ashare_futures_inventory_forbidden"
                    )
                derived_unrealized = round(
                    sum(positions_market_value.values())
                    - sum(expected_cost_basis.values())
                    - sum(expected_entry_fees.values()),
                    6,
                )
            else:
                expected_quantities = r.latest_position_quantity
                if positions_market_value:
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_position_value_forbidden"
                    )
                if dict(positions_quantity) != expected_quantities:
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_position_quantity_mismatch"
                    )
                if positions_cost_basis or positions_entry_fee:
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_ashare_inventory_forbidden"
                    )
                if not _float_maps_close(position_margin, r.latest_position_margin):
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_margin_mismatch"
                    )
                if not _float_maps_close(
                    position_entry_price,
                    r.latest_position_entry_price,
                ):
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_entry_price_mismatch"
                    )
                if position_side != r.latest_position_side:
                    raise MarketCapitalLedgerError("reconcile_cn_futures_side_mismatch")
                if not _float_maps_close(
                    position_multiplier,
                    r.latest_position_contract_multiplier,
                ):
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_multiplier_mismatch"
                    )
                if position_contract_spec_sha != r.latest_position_contract_spec_sha256:
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_contract_spec_mismatch"
                    )
                if set(position_mark_price) != set(expected_quantities):
                    raise MarketCapitalLedgerError(
                        "reconcile_cn_futures_mark_price_mismatch"
                    )
                derived_unrealized = 0.0
                for risk_unit, signed_quantity in expected_quantities.items():
                    entry = r.latest_position_entry_price[risk_unit]
                    mark = float(position_mark_price[risk_unit])
                    multiplier = r.latest_position_contract_multiplier[risk_unit]
                    direction = 1.0 if signed_quantity > 0 else -1.0
                    derived_unrealized += (
                        (mark - entry) * abs(signed_quantity) * multiplier * direction
                    )
                derived_unrealized = round(derived_unrealized, 6)
            if not math.isclose(
                unrealized_pnl,
                derived_unrealized,
                abs_tol=0.01,
            ):
                raise MarketCapitalLedgerError("reconcile_unrealized_pnl_mismatch")

            # Check idempotency: same as_of, same reference
            priors = [
                row
                for row in ev
                if row.get("event_type") == "reconcile"
                and str(row.get("reference_id") or "") == ref_id
            ]
            if priors:
                prior = priors[0]
                prior_payload = {key: prior.get(key) for key in incoming_payload}
                if prior_payload != incoming_payload:
                    raise MarketCapitalLedgerError("reconcile_reference_conflict")
                return {
                    "status": "idempotent_reconcile",
                    "event_id": prior["event_id"],
                    "equity_cny": round(equity, 6),
                    "cash_balance_cny": round(manifest.cash_balance_cny, 6),
                    "positions_market_value_cny": round(
                        sum(float(v) for v in manifest.positions_market_value.values()),
                        6,
                    ),
                    "margin_used_cny": round(margin_used, 6),
                    "active_reservations_cny": r.snapshot.active_reservations_cny,
                    "available_to_reserve_cny": r.snapshot.available_to_reserve_cny,
                    "capital_utilization_rate": r.snapshot.capital_utilization_rate,
                    "as_of": as_of,
                    "lineage_sha256": str(prior.get("checksum") or ""),
                    "reconciled": True,
                    "real_trading_enabled": False,
                }

            pending_fill_commit_ids = tuple(r.unreconciled_fill_commit_ids)
            current_reservation_map = self._active_reservation_manifest(r.reservations)
            has_expected_event = bool(
                str(manifest.expected_ledger_event_id or "").strip()
            )
            has_expected_checksum = bool(
                str(manifest.expected_ledger_checksum or "").strip()
            )
            if has_expected_event != has_expected_checksum:
                raise MarketCapitalLedgerError(
                    "reconcile_ledger_head_cas_pair_required"
                )
            if has_expected_event:
                if not _is_64hex(str(manifest.expected_ledger_checksum or "")):
                    raise MarketCapitalLedgerError("reconcile_ledger_head_cas_required")
                head = ev[-1]
                if str(head.get("event_id") or "") != str(
                    manifest.expected_ledger_event_id
                ) or str(head.get("checksum") or "") != str(
                    manifest.expected_ledger_checksum
                ):
                    raise MarketCapitalLedgerError("reconcile_ledger_head_cas_mismatch")
            exact_state_required = bool(
                pending_fill_commit_ids or current_reservation_map
            )
            if exact_state_required:
                if active_reservation_map is None:
                    raise MarketCapitalLedgerError(
                        "reconcile_active_reservation_map_required"
                    )
                if not str(
                    manifest.expected_ledger_event_id or ""
                ).strip() or not _is_64hex(
                    str(manifest.expected_ledger_checksum or "")
                ):
                    raise MarketCapitalLedgerError("reconcile_ledger_head_cas_required")
            if pending_fill_commit_ids:
                if included_fill_commit_ids != pending_fill_commit_ids:
                    raise MarketCapitalLedgerError("reconcile_fill_watermark_mismatch")
                reconcile_pit = _parse_timestamp(
                    manifest.pit_timestamp,
                    field="reconcile_pit_timestamp",
                )
                fill_times = [
                    _parse_timestamp(
                        str(row.get("filled_at") or ""),
                        field="fill_commit_filled_at",
                    )
                    for row in ev
                    if row.get("event_type")
                    in {
                        "fill_commit",
                        "ashare_sell_commit",
                        "position_close_commit",
                    }
                    and str(row.get("event_id") or "") in pending_fill_commit_ids
                ]
                if any(reconcile_pit < fill_time for fill_time in fill_times):
                    raise MarketCapitalLedgerError("reconcile_pit_before_fill_commit")
            elif included_fill_commit_ids:
                raise MarketCapitalLedgerError("reconcile_fill_watermark_mismatch")
            if (
                active_reservation_map is not None
                and active_reservation_map != current_reservation_map
            ):
                raise MarketCapitalLedgerError("active_reservation_map_mismatch")

            # Conservation: active_reservations must match
            if not math.isclose(
                active_reservations, r.snapshot.active_reservations_cny, abs_tol=1e-9
            ):
                raise MarketCapitalLedgerError("active_reservations_mismatch")

            last_ck = ev[-1].get("checksum", "") if ev else ""
            rec_evt = {
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "reconcile",
                "authority_id": self.policy.capital_authority_id,
                "authority_generation": self.policy.authority_generation,
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "reference_id": ref_id,
                "amount_cny": 0.0,
                "currency": self.policy.currency,
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "trade_date": as_of,
                "as_of": as_of,
                "cash_balance_cny": cash_balance,
                "positions_market_value": positions_market_value,
                "unrealized_pnl_cny": unrealized_pnl,
                "position_margin_by_risk_unit": position_margin,
                "active_reservations_cny": active_reservations,
                "frozen_order_cash_cny": frozen_order_cash,
                "frozen_order_margin_cny": frozen_order_margin,
                "execution_lineage_id": manifest.execution_lineage_id,
                "pit_timestamp": manifest.pit_timestamp,
                "positions_market_value_cny": sum(positions_market_value.values()),
                "margin_used_cny": margin_used,
                "equity_cny": round(equity, 6),
                "source": manifest.source,
                "source_sha256": manifest.source_sha256,
                "active_reservations": active_reservation_map,
                "expected_ledger_event_id": manifest.expected_ledger_event_id,
                "expected_ledger_checksum": manifest.expected_ledger_checksum,
                "included_fill_commit_ids": list(included_fill_commit_ids),
                "positions_quantity_by_risk_unit": dict(positions_quantity),
                "positions_cost_basis_cny_by_risk_unit": dict(positions_cost_basis),
                "positions_entry_fee_cny_by_risk_unit": dict(positions_entry_fee),
                "position_entry_price_by_risk_unit": dict(position_entry_price),
                "position_side_by_risk_unit": dict(position_side),
                "position_contract_multiplier_by_risk_unit": dict(position_multiplier),
                "position_contract_spec_sha256_by_risk_unit": dict(
                    position_contract_spec_sha
                ),
                "position_mark_price_by_risk_unit": dict(position_mark_price),
                "canonical_snapshot_path": str(manifest.canonical_snapshot_path),
                "canonical_snapshot_sha256": canonical_sha,
                "previous_checksum": last_ck,
            }
            rec_evt["checksum"] = _compute_event_checksum(rec_evt)
            self._append_event_unlocked(rec_evt)
            upd = self._replay([*ev, rec_evt])
            self._write_projection_unlocked(upd.snapshot)
            return {
                "status": "reconciled",
                "event_id": rec_evt["event_id"],
                "equity_cny": round(equity, 6),
                "cash_balance_cny": round(manifest.cash_balance_cny, 6),
                "positions_market_value_cny": round(
                    sum(float(v) for v in manifest.positions_market_value.values()), 6
                ),
                "margin_used_cny": round(margin_used, 6),
                "active_reservations_cny": upd.snapshot.active_reservations_cny,
                "available_to_reserve_cny": upd.snapshot.available_to_reserve_cny,
                "capital_utilization_rate": upd.snapshot.capital_utilization_rate,
                "as_of": as_of,
                "lineage_sha256": rec_evt["checksum"],
                "reconciled": True,
                "real_trading_enabled": False,
            }

    # ---- reserve ----

    def _reservation_legs(
        self,
        request: MarketCapitalReservationRequest,
        compatibility_amount: float,
    ) -> tuple[float, float, float]:
        if self.policy.market == "ashare":
            cash = _strict_number(
                compatibility_amount
                if request.worst_case_cash_cny is None
                else request.worst_case_cash_cny,
                field="worst_case_cash_cny",
                positive=True,
            )
            exposure = _strict_number(
                compatibility_amount
                if request.worst_case_exposure_cny is None
                else request.worst_case_exposure_cny,
                field="worst_case_exposure_cny",
                positive=True,
            )
            margin = _strict_number(
                0.0
                if request.worst_case_margin_cny is None
                else request.worst_case_margin_cny,
                field="worst_case_margin_cny",
            )
            if margin != 0.0 or cash + 1e-9 < exposure:
                raise MarketCapitalLedgerError("invalid_ashare_reservation_legs")
            if not math.isclose(compatibility_amount, cash, abs_tol=1e-9):
                raise MarketCapitalLedgerError("ashare_compatibility_amount_mismatch")
            return cash, exposure, 0.0

        cash = _strict_number(
            0.0 if request.worst_case_cash_cny is None else request.worst_case_cash_cny,
            field="worst_case_cash_cny",
        )
        exposure = _strict_number(
            0.0
            if request.worst_case_exposure_cny is None
            else request.worst_case_exposure_cny,
            field="worst_case_exposure_cny",
        )
        margin = _strict_number(
            compatibility_amount
            if request.worst_case_margin_cny is None
            else request.worst_case_margin_cny,
            field="worst_case_margin_cny",
            positive=True,
        )
        if exposure != 0.0:
            raise MarketCapitalLedgerError("invalid_cn_futures_reservation_legs")
        if not math.isclose(compatibility_amount, margin, abs_tol=1e-9):
            raise MarketCapitalLedgerError("cn_futures_compatibility_amount_mismatch")
        return cash, 0.0, margin

    def reserve(
        self, request: MarketCapitalReservationRequest
    ) -> MarketCapitalReservationDecision:
        self._ensure_init()
        if _normalize_market(request.market) != self.policy.market:
            return self._dec(approved=False, reason="market_mismatch")
        if request.authority_id != self.policy.capital_authority_id:
            return self._dec(approved=False, reason="aid_mismatch")
        if request.authority_generation != self.policy.authority_generation:
            return self._dec(approved=False, reason="gen_mismatch")
        if not str(request.reference_id or "").strip():
            return self._dec(approved=False, reason="missing_ref")
        if not str(request.risk_unit_key or "").strip():
            return self._dec(approved=False, reason="missing_risk_unit")
        if not str(request.point_in_time_as_of or "").strip():
            return self._dec(approved=False, reason="missing_pit_as_of")
        if not _tz_aware(str(request.point_in_time_as_of or "")):
            return self._dec(approved=False, reason="invalid_pit_as_of")
        if not _is_64hex(request.lineage_sha256):
            return self._dec(approved=False, reason="invalid_lineage_sha256")
        if not request.execution_lineage_id:
            return self._dec(approved=False, reason="missing_execution_lineage")
        try:
            amt = _strict_number(
                request.worst_case_amount_cny, field="amount", positive=True
            )
        except MarketCapitalLedgerError:
            return self._dec(approved=False, reason="invalid_amount")
        try:
            cash_leg, exposure_leg, margin_leg = self._reservation_legs(request, amt)
        except MarketCapitalLedgerError as exc:
            return self._dec(approved=False, reason=str(exc))
        try:
            nd = _validate_trade_date(request.trade_date)
        except MarketCapitalLedgerError:
            return self._dec(approved=False, reason="invalid_trade_date")

        with self._lock():
            ev = self._load_events_unlocked()
            r = self._replay(ev)
            # Idempotency: match reference_id + risk_unit_key
            matching = [
                v
                for v in r.reservations.values()
                if v.reference_id == request.reference_id
            ]
            if matching:
                ex = matching[0]
                if (
                    not math.isclose(ex.original_amount_cny, amt, abs_tol=1e-9)
                    or not math.isclose(ex.original_cash_cny, cash_leg, abs_tol=1e-9)
                    or not math.isclose(
                        ex.original_exposure_cny, exposure_leg, abs_tol=1e-9
                    )
                    or not math.isclose(
                        ex.original_margin_cny, margin_leg, abs_tol=1e-9
                    )
                    or ex.risk_unit_key != request.risk_unit_key
                    or ex.authority_generation != request.authority_generation
                    or ex.execution_lineage_id != request.execution_lineage_id
                    or ex.lineage_sha256 != request.lineage_sha256
                    or ex.point_in_time_as_of != request.point_in_time_as_of
                    or (bool(ex.trade_date) and ex.trade_date != nd)
                ):
                    return self._dec(
                        approved=False,
                        reason="reservation_conflict",
                        reservation_id=ex.reservation_id,
                        event_id=ex.event_id,
                        snapshot=r.snapshot,
                    )
                if not math.isclose(
                    ex.remaining_amount_cny, ex.original_amount_cny, abs_tol=1e-9
                ):
                    return self._dec(
                        approved=False,
                        reason="reservation_closed",
                        reservation_id=ex.reservation_id,
                        event_id=ex.event_id,
                        snapshot=r.snapshot,
                    )
                return self._dec(
                    approved=True,
                    reason="idempotent_reservation",
                    reservation_id=ex.reservation_id,
                    event_id=ex.event_id,
                    snapshot=r.snapshot,
                )

            same_day_reconciles = [
                row
                for row in ev
                if row.get("event_type") == "reconcile"
                and str(row.get("as_of") or row.get("trade_date") or "").replace(
                    "-", ""
                )
                == nd
                and str(row.get("execution_lineage_id") or "")
                == request.execution_lineage_id
            ]
            if not same_day_reconciles:
                return self._dec(
                    approved=False,
                    reason="current_trade_date_reconcile_required",
                    snapshot=r.snapshot,
                )
            latest_reconcile = same_day_reconciles[-1]
            try:
                request_pit = _parse_timestamp(
                    request.point_in_time_as_of,
                    field="reservation_point_in_time_as_of",
                )
                reconcile_pit = _parse_timestamp(
                    str(latest_reconcile.get("pit_timestamp") or ""),
                    field="latest_reconcile_pit_timestamp",
                )
            except MarketCapitalLedgerError as exc:
                return self._dec(approved=False, reason=str(exc), snapshot=r.snapshot)
            if request_pit < reconcile_pit:
                return self._dec(
                    approved=False,
                    reason="reservation_point_in_time_before_reconcile",
                    snapshot=r.snapshot,
                )

            snap = r.snapshot
            risk = self._risk_metrics(ev, r, trade_date=nd)
            dll = self.policy.initial_equity_cny * self.policy.daily_loss_pause_pct
            if float(risk["daily_mtm_change"]) <= -dll + 1e-9:
                return self._dec(
                    approved=False, reason="daily_loss_limit", snapshot=snap
                )
            if int(risk["consecutive_losses"]) >= self.policy.max_consecutive_losses:
                return self._dec(
                    approved=False, reason="consecutive_loss_limit", snapshot=snap
                )
            ddh = self.policy.initial_equity_cny * self.policy.drawdown_halt_pct
            if float(risk["drawdown"]) >= ddh - 1e-9:
                return self._dec(
                    approved=False, reason="maximum_drawdown_limit", snapshot=snap
                )
            ddt = self.policy.initial_equity_cny * self.policy.drawdown_tighten_pct
            rt = float(risk["drawdown"]) >= ddt - 1e-9
            rm = self.policy.drawdown_tighten_risk_multiplier if rt else 1.0

            if self.policy.market == "ashare":
                # Replay includes every committed fill since the last MTM
                # checkpoint.  Reading only the last reconcile would omit the
                # just-filled position and transiently mint capacity.
                lp = dict(r.latest_positions_mv)
                smv = lp.get(request.risk_unit_key, 0.0)
                sres = sum(
                    v.remaining_exposure_cny
                    for v in r.reservations.values()
                    if v.risk_unit_key == request.risk_unit_key
                )
                stot = smv + sres + exposure_leg
                sc = round(self.policy.single_name_cap_cny * rm, 6)
                if stot > sc + 1e-9 or exposure_leg > sc + 1e-9:
                    return self._dec(
                        approved=False,
                        reason="drawdown_tighten_reservation_cap"
                        if rt
                        else "single_name_cap_exceeded",
                        snapshot=snap,
                        risk_tightened=rt,
                        risk_multiplier=rm,
                        reservation_cap_cny=sc,
                    )
                tmv = sum(float(v) for v in lp.values())
                texp = tmv + snap.reserved_exposure_cny + exposure_leg
                gl = round(self.policy.stock_gross_exposure_limit_cny * rm, 6)
                if texp > gl + 1e-9:
                    return self._dec(
                        approved=False,
                        reason="gross_exposure_limit_exceeded",
                        snapshot=snap,
                        risk_tightened=rt,
                        risk_multiplier=rm,
                        reservation_cap_cny=gl,
                    )
            else:
                texp = snap.margin_used_cny + snap.reserved_margin_cny + margin_leg
                ml = self.policy.margin_utilization_limit_cny
                el = round(ml * rm, 6) if rt else ml
                if texp > el + 1e-9:
                    return self._dec(
                        approved=False,
                        reason="margin_limit_exhausted",
                        snapshot=snap,
                        risk_tightened=rt,
                        risk_multiplier=rm,
                        reservation_cap_cny=el,
                    )

            cash_encumbered = (
                snap.reserved_cash_cny + cash_leg
                if self.policy.market == "ashare"
                else snap.margin_used_cny
                + snap.frozen_order_margin_cny
                + snap.reserved_margin_cny
                + margin_leg
                + snap.reserved_cash_cny
                + cash_leg
            )
            if (
                cash_encumbered
                > snap.cash_balance_cny - snap.frozen_order_cash_cny + 1e-9
            ):
                return self._dec(
                    approved=False, reason="equity_insufficient", snapshot=snap
                )

            rid = f"MCAP-RES-{uuid.uuid4().hex}"
            ec = (
                round(self.policy.single_name_cap_cny * rm, 6)
                if self.policy.market == "ashare"
                else round(self.policy.margin_utilization_limit_cny * rm, 6)
            )
            evt = {
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "reserve",
                "authority_id": self.policy.capital_authority_id,
                "authority_generation": self.policy.authority_generation,
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "reference_id": request.reference_id,
                "amount_cny": amt,
                "currency": self.policy.currency,
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "reservation_id": rid,
                "risk_unit_key": request.risk_unit_key,
                "trade_date": nd,
                "risk_tightened": rt,
                "risk_multiplier": rm,
                "reservation_cap_cny": ec,
                "cash_reservation_cny": cash_leg,
                "exposure_reservation_cny": exposure_leg,
                "margin_reservation_cny": margin_leg,
                "execution_lineage_id": request.execution_lineage_id,
                "lineage_sha256": request.lineage_sha256,
                "point_in_time_as_of": request.point_in_time_as_of,
                "previous_checksum": ev[-1].get("checksum", "")
                if ev
                else _sha256_hex(GENESIS_PREVIOUS_CHECKSUM),
            }
            evt["checksum"] = _compute_event_checksum(evt)
            self._append_event_unlocked(evt)
            upd = self._replay([*ev, evt])
            self._write_projection_unlocked(upd.snapshot)
            return self._dec(
                approved=True,
                reason="reserved_drawdown_tightened" if rt else "reserved",
                reservation_id=rid,
                event_id=str(evt["event_id"]),
                snapshot=upd.snapshot,
                risk_tightened=rt,
                risk_multiplier=rm,
                reservation_cap_cny=ec,
            )

    @staticmethod
    def _latest_positions_from_events(ev: list[dict]) -> dict[str, float]:
        for row in reversed(ev):
            if row.get("event_type") == "bootstrap":
                return {
                    k: float(v)
                    for k, v in (row.get("positions_by_risk_unit") or {}).items()
                }
            if row.get("event_type") == "reconcile":
                return {
                    k: float(v)
                    for k, v in (row.get("positions_market_value") or {}).items()
                }
        return {}

    # ---- fill commit ----

    @staticmethod
    def _fill_decision(
        *,
        committed: bool,
        reason: str,
        **kwargs: Any,
    ) -> MarketCapitalFillCommitDecision:
        return MarketCapitalFillCommitDecision(
            committed=committed,
            reason=reason,
            **kwargs,
        )

    @staticmethod
    def _fill_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "reference_id",
            "reservation_id",
            "reservation_event_id",
            "reservation_reference_id",
            "risk_unit_key",
            "authority_id",
            "authority_generation",
            "execution_lineage_id",
            "lineage_sha256",
            "order_id",
            "idempotency_key",
            "execution_fill_id",
            "fill_sequence",
            "side",
            "status",
            "terminal",
            "actual_filled_quantity",
            "actual_fill_price",
            "actual_cash_debit_cny",
            "actual_exposure_cny",
            "actual_margin_cny",
            "actual_fee_cash_cny",
            "contract_multiplier",
            "contract_margin_per_lot_cny",
            "contract_spec_version",
            "contract_spec_sha256",
            "filled_at",
            "point_in_time_as_of",
            "source",
            "source_sha256",
            "receipt_sha256",
            "local_trade_sha256",
        )
        return {key: payload.get(key) for key in keys}

    def commit_fill(
        self,
        request: MarketCapitalFillCommitRequest,
    ) -> MarketCapitalFillCommitDecision:
        self._ensure_init()
        market = _normalize_market(request.market)
        if market != self.policy.market:
            return self._fill_decision(committed=False, reason="market_mismatch")
        if request.authority_id != self.policy.capital_authority_id:
            return self._fill_decision(committed=False, reason="aid_mismatch")
        if request.authority_generation != self.policy.authority_generation:
            return self._fill_decision(committed=False, reason="gen_mismatch")
        required_strings = (
            "reference_id",
            "reservation_id",
            "reservation_event_id",
            "reservation_reference_id",
            "risk_unit_key",
            "execution_lineage_id",
            "order_id",
            "idempotency_key",
            "execution_fill_id",
            "source",
            "expected_ledger_event_id",
            "expected_ledger_checksum",
        )
        for field_name in required_strings:
            if not str(getattr(request, field_name) or "").strip():
                return self._fill_decision(
                    committed=False,
                    reason=f"missing_{field_name}",
                )
        for field_name in (
            "lineage_sha256",
            "source_sha256",
            "receipt_sha256",
            "local_trade_sha256",
            "expected_ledger_checksum",
        ):
            if not _is_64hex(str(getattr(request, field_name) or "")):
                return self._fill_decision(
                    committed=False,
                    reason=f"invalid_{field_name}",
                )
        expected_reference = (
            f"MCAPFILL:{request.authority_generation}:"
            f"{request.execution_lineage_id}:{request.reservation_id}:"
            f"{request.execution_fill_id}"
        )
        if request.reference_id != expected_reference:
            return self._fill_decision(
                committed=False,
                reason="fill_reference_mismatch",
            )
        if (
            not isinstance(request.fill_sequence, int)
            or isinstance(request.fill_sequence, bool)
            or request.fill_sequence <= 0
        ):
            return self._fill_decision(committed=False, reason="invalid_fill_sequence")
        if (
            not isinstance(request.actual_filled_quantity, int)
            or isinstance(request.actual_filled_quantity, bool)
            or request.actual_filled_quantity <= 0
        ):
            return self._fill_decision(
                committed=False,
                reason="invalid_actual_filled_quantity",
            )
        try:
            fill_price = _strict_number(
                request.actual_fill_price,
                field="actual_fill_price",
                positive=True,
            )
            cash_debit = _strict_number(
                request.actual_cash_debit_cny,
                field="actual_cash_debit_cny",
            )
            exposure = _strict_number(
                request.actual_exposure_cny,
                field="actual_exposure_cny",
            )
            margin = _strict_number(
                request.actual_margin_cny,
                field="actual_margin_cny",
            )
            fee_cash = _strict_number(
                request.actual_fee_cash_cny,
                field="actual_fee_cash_cny",
            )
        except MarketCapitalLedgerError as exc:
            return self._fill_decision(committed=False, reason=str(exc))
        if any(value < 0.0 for value in (cash_debit, exposure, margin, fee_cash)):
            return self._fill_decision(
                committed=False, reason="negative_fill_accounting"
            )
        status = str(request.status or "").strip().lower()
        if status not in {"partial", "filled"}:
            return self._fill_decision(committed=False, reason="invalid_fill_status")
        if not isinstance(request.terminal, bool):
            return self._fill_decision(committed=False, reason="invalid_terminal")
        if status == "filled" and request.terminal is not True:
            return self._fill_decision(
                committed=False, reason="filled_must_be_terminal"
            )
        side = str(request.side or "").strip().lower()
        if market == "ashare":
            if side != "buy" or exposure <= 0.0 or margin != 0.0:
                return self._fill_decision(
                    committed=False,
                    reason="invalid_ashare_fill_accounting",
                )
            if request.actual_filled_quantity % 100 != 0:
                return self._fill_decision(
                    committed=False,
                    reason="ashare_lot_size_invalid",
                )
            expected_notional = round(
                request.actual_filled_quantity * fill_price,
                6,
            )
            if not math.isclose(exposure, expected_notional, abs_tol=0.01):
                return self._fill_decision(
                    committed=False,
                    reason="ashare_fill_notional_mismatch",
                )
            if cash_debit + 1e-9 < exposure or not math.isclose(
                cash_debit - exposure, fee_cash, abs_tol=1e-6
            ):
                return self._fill_decision(
                    committed=False,
                    reason="ashare_cash_exposure_fee_mismatch",
                )
        else:
            if (
                side not in {"buy", "sell", "long", "short"}
                or margin <= 0.0
                or exposure != 0.0
            ):
                return self._fill_decision(
                    committed=False,
                    reason="invalid_cn_futures_fill_accounting",
                )
            if not math.isclose(cash_debit, fee_cash, abs_tol=1e-6):
                return self._fill_decision(
                    committed=False,
                    reason="cn_futures_cash_fee_mismatch",
                )
            try:
                contract_multiplier = _strict_number(
                    request.contract_multiplier,
                    field="contract_multiplier",
                    positive=True,
                )
                margin_per_lot = _strict_number(
                    request.contract_margin_per_lot_cny,
                    field="contract_margin_per_lot_cny",
                    positive=True,
                )
            except MarketCapitalLedgerError as exc:
                return self._fill_decision(committed=False, reason=str(exc))
            if request.contract_spec_version != CN_FUTURES_CONTRACT_SPEC_VERSION:
                return self._fill_decision(
                    committed=False,
                    reason="contract_spec_version_mismatch",
                )
            expected_spec_sha = cn_futures_contract_spec_sha256(
                request.risk_unit_key,
                contract_multiplier,
                margin_per_lot,
                version=request.contract_spec_version,
            )
            if request.contract_spec_sha256 != expected_spec_sha:
                return self._fill_decision(
                    committed=False,
                    reason="contract_spec_sha256_mismatch",
                )
            if not math.isclose(
                margin,
                request.actual_filled_quantity * margin_per_lot,
                abs_tol=0.01,
            ):
                return self._fill_decision(
                    committed=False,
                    reason="cn_futures_margin_quantity_mismatch",
                )
        try:
            filled_at = _parse_timestamp(request.filled_at, field="filled_at")
            pit = _parse_timestamp(
                request.point_in_time_as_of,
                field="point_in_time_as_of",
            )
        except MarketCapitalLedgerError as exc:
            return self._fill_decision(committed=False, reason=str(exc))
        if filled_at < pit:
            return self._fill_decision(committed=False, reason="fill_pit_regression")

        incoming = self._fill_identity(asdict(request))
        with self._lock():
            events = self._load_events_unlocked()
            replay = self._replay(events)
            priors = [
                row
                for row in events
                if row.get("event_type") == "fill_commit"
                and str(row.get("reference_id") or "") == request.reference_id
            ]
            if priors:
                prior = priors[0]
                if self._fill_identity(prior) != incoming:
                    raise MarketCapitalLedgerError("fill_commit_conflict")
                return self._fill_decision(
                    committed=True,
                    reason="idempotent_fill_commit",
                    status="idempotent",
                    event_id=str(prior["event_id"]),
                    reservation_id=request.reservation_id,
                    snapshot=replay.snapshot,
                    idempotent=True,
                )

            head = events[-1]
            if (
                str(head.get("event_id") or "") != request.expected_ledger_event_id
                or str(head.get("checksum") or "") != request.expected_ledger_checksum
            ):
                return self._fill_decision(
                    committed=False,
                    reason="ledger_head_cas_mismatch",
                )
            reservation = replay.reservations.get(request.reservation_id)
            if reservation is None:
                return self._fill_decision(
                    committed=False,
                    reason="unknown_reservation",
                )
            if reservation.terminal:
                return self._fill_decision(
                    committed=False,
                    reason="reservation_terminal",
                )
            lineage_checks = {
                "reservation_event_id": (
                    reservation.event_id,
                    request.reservation_event_id,
                ),
                "reservation_reference_id": (
                    reservation.reference_id,
                    request.reservation_reference_id,
                ),
                "risk_unit_key": (reservation.risk_unit_key, request.risk_unit_key),
                "authority_id": (reservation.authority_id, request.authority_id),
                "authority_generation": (
                    reservation.authority_generation,
                    request.authority_generation,
                ),
                "execution_lineage_id": (
                    reservation.execution_lineage_id,
                    request.execution_lineage_id,
                ),
                "lineage_sha256": (
                    reservation.lineage_sha256,
                    request.lineage_sha256,
                ),
                "point_in_time_as_of": (
                    reservation.point_in_time_as_of,
                    request.point_in_time_as_of,
                ),
            }
            for field_name, (expected, actual) in lineage_checks.items():
                if expected != actual:
                    return self._fill_decision(
                        committed=False,
                        reason=f"fill_{field_name}_mismatch",
                    )
            try:
                reservation_pit = _parse_timestamp(
                    reservation.point_in_time_as_of,
                    field="reservation_point_in_time_as_of",
                )
            except MarketCapitalLedgerError as exc:
                return self._fill_decision(committed=False, reason=str(exc))
            if filled_at < reservation_pit:
                return self._fill_decision(
                    committed=False,
                    reason="fill_before_reservation_pit",
                )
            prior_reservation_fills = [
                row
                for row in events
                if row.get("event_type") == "fill_commit"
                and str(row.get("reservation_id") or "") == request.reservation_id
            ]
            expected_sequence = 1 + max(
                (int(row.get("fill_sequence", 0)) for row in prior_reservation_fills),
                default=0,
            )
            if request.fill_sequence != expected_sequence:
                return self._fill_decision(
                    committed=False,
                    reason="fill_sequence_mismatch",
                )
            if market == "cn_futures":
                existing_quantity = int(
                    replay.latest_position_quantity.get(request.risk_unit_key, 0)
                )
                fill_sign = 1 if side in {"buy", "long"} else -1
                if existing_quantity and existing_quantity * fill_sign < 0:
                    return self._fill_decision(
                        committed=False,
                        reason="cn_futures_open_direction_conflict",
                    )
                existing_multiplier = replay.latest_position_contract_multiplier.get(
                    request.risk_unit_key
                )
                if existing_multiplier is not None and not math.isclose(
                    float(existing_multiplier),
                    float(request.contract_multiplier),
                    abs_tol=1e-12,
                ):
                    return self._fill_decision(
                        committed=False,
                        reason="cn_futures_contract_multiplier_conflict",
                    )
                existing_spec_sha = replay.latest_position_contract_spec_sha256.get(
                    request.risk_unit_key
                )
                if (
                    existing_spec_sha is not None
                    and existing_spec_sha != request.contract_spec_sha256
                ):
                    return self._fill_decision(
                        committed=False,
                        reason="cn_futures_contract_spec_conflict",
                    )

            cash_consumed = cash_debit if market == "ashare" else fee_cash
            exposure_consumed = exposure if market == "ashare" else 0.0
            margin_consumed = margin if market == "cn_futures" else 0.0
            if cash_consumed > reservation.remaining_cash_cny + 1e-9:
                return self._fill_decision(
                    committed=False,
                    reason="fill_cash_exceeds_reservation",
                )
            if exposure_consumed > reservation.remaining_exposure_cny + 1e-9:
                return self._fill_decision(
                    committed=False,
                    reason="fill_exposure_exceeds_reservation",
                )
            if margin_consumed > reservation.remaining_margin_cny + 1e-9:
                return self._fill_decision(
                    committed=False,
                    reason="fill_margin_exceeds_reservation",
                )

            remaining_cash = round(reservation.remaining_cash_cny - cash_consumed, 6)
            remaining_exposure = round(
                reservation.remaining_exposure_cny - exposure_consumed,
                6,
            )
            remaining_margin = round(
                reservation.remaining_margin_cny - margin_consumed,
                6,
            )
            if status == "partial" and request.terminal is False:
                has_remaining_order_capacity = (
                    remaining_cash > 0.0 and remaining_exposure > 0.0
                    if market == "ashare"
                    else remaining_margin > 0.0
                )
                if not has_remaining_order_capacity:
                    return self._fill_decision(
                        committed=False,
                        reason="partial_open_without_remaining_reservation",
                    )
            released_cash = remaining_cash if request.terminal else 0.0
            released_exposure = remaining_exposure if request.terminal else 0.0
            released_margin = remaining_margin if request.terminal else 0.0
            post_cash = 0.0 if request.terminal else remaining_cash
            post_exposure = 0.0 if request.terminal else remaining_exposure
            post_margin = 0.0 if request.terminal else remaining_margin
            event = {
                **incoming,
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "fill_commit",
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "currency": self.policy.currency,
                "amount_cny": cash_consumed if market == "ashare" else margin_consumed,
                "cash_reservation_consumed_cny": round(cash_consumed, 6),
                "exposure_reservation_consumed_cny": round(exposure_consumed, 6),
                "margin_reservation_consumed_cny": round(margin_consumed, 6),
                "cash_reservation_released_cny": released_cash,
                "exposure_reservation_released_cny": released_exposure,
                "margin_reservation_released_cny": released_margin,
                "remaining_cash_reservation_cny": post_cash,
                "remaining_exposure_reservation_cny": post_exposure,
                "remaining_margin_reservation_cny": post_margin,
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "previous_checksum": str(head.get("checksum") or ""),
            }
            event["checksum"] = _compute_event_checksum(event)
            updated = self._replay([*events, event])
            self._append_event_unlocked(event)
            self._write_projection_unlocked(updated.snapshot)
            return self._fill_decision(
                committed=True,
                reason="fill_committed",
                status="committed",
                event_id=str(event["event_id"]),
                reservation_id=request.reservation_id,
                snapshot=updated.snapshot,
            )

    # ---- A-share position sell commit ----

    @staticmethod
    def _ashare_sell_decision(
        *,
        committed: bool,
        reason: str,
        **kwargs: Any,
    ) -> MarketCapitalAshareSellCommitDecision:
        return MarketCapitalAshareSellCommitDecision(
            committed=committed,
            reason=reason,
            **kwargs,
        )

    @staticmethod
    def _ashare_sell_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "reference_id",
            "risk_unit_key",
            "authority_id",
            "authority_generation",
            "execution_lineage_id",
            "lineage_sha256",
            "order_id",
            "idempotency_key",
            "execution_fill_id",
            "fill_sequence",
            "side",
            "status",
            "terminal",
            "actual_closed_quantity",
            "actual_fill_price",
            "actual_gross_proceeds_cny",
            "actual_fee_cash_cny",
            "actual_net_cash_credit_cny",
            "actual_gross_realized_pnl_cny",
            "filled_at",
            "point_in_time_as_of",
            "source",
            "source_sha256",
            "receipt_sha256",
            "local_position_sha256",
        )
        return {key: payload.get(key) for key in keys}

    def commit_ashare_sell(
        self,
        request: MarketCapitalAshareSellCommitRequest,
    ) -> MarketCapitalAshareSellCommitDecision:
        """Atomically fold an actual A-share sell into cash and exposure."""

        self._ensure_init()
        market = _normalize_market(request.market)
        if market != self.policy.market:
            return self._ashare_sell_decision(committed=False, reason="market_mismatch")
        if market != "ashare":
            return self._ashare_sell_decision(
                committed=False, reason="ashare_sell_only_ashare"
            )
        if request.authority_id != self.policy.capital_authority_id:
            return self._ashare_sell_decision(committed=False, reason="aid_mismatch")
        if request.authority_generation != self.policy.authority_generation:
            return self._ashare_sell_decision(committed=False, reason="gen_mismatch")
        required_strings = (
            "reference_id",
            "risk_unit_key",
            "execution_lineage_id",
            "order_id",
            "idempotency_key",
            "execution_fill_id",
            "source",
            "expected_ledger_event_id",
            "expected_ledger_checksum",
        )
        for field_name in required_strings:
            if not str(getattr(request, field_name) or "").strip():
                return self._ashare_sell_decision(
                    committed=False, reason=f"missing_{field_name}"
                )
        for field_name in (
            "lineage_sha256",
            "source_sha256",
            "receipt_sha256",
            "local_position_sha256",
            "expected_ledger_checksum",
        ):
            if not _is_64hex(str(getattr(request, field_name) or "")):
                return self._ashare_sell_decision(
                    committed=False, reason=f"invalid_{field_name}"
                )
        expected_reference = (
            f"MCAPSELL:{request.authority_generation}:"
            f"{request.execution_lineage_id}:{request.risk_unit_key}:"
            f"{request.execution_fill_id}"
        )
        if request.reference_id != expected_reference:
            return self._ashare_sell_decision(
                committed=False, reason="ashare_sell_reference_mismatch"
            )
        if (
            not isinstance(request.fill_sequence, int)
            or isinstance(request.fill_sequence, bool)
            or request.fill_sequence <= 0
        ):
            return self._ashare_sell_decision(
                committed=False, reason="invalid_fill_sequence"
            )
        if (
            not isinstance(request.actual_closed_quantity, int)
            or isinstance(request.actual_closed_quantity, bool)
            or request.actual_closed_quantity <= 0
        ):
            return self._ashare_sell_decision(
                committed=False, reason="invalid_actual_closed_quantity"
            )
        if request.actual_closed_quantity % 100 != 0:
            return self._ashare_sell_decision(
                committed=False, reason="ashare_lot_size_invalid"
            )
        try:
            fill_price = _strict_number(
                request.actual_fill_price,
                field="actual_fill_price",
                positive=True,
            )
            gross_proceeds = _strict_number(
                request.actual_gross_proceeds_cny,
                field="actual_gross_proceeds_cny",
                positive=True,
            )
            fee_cash = _strict_number(
                request.actual_fee_cash_cny,
                field="actual_fee_cash_cny",
            )
            net_cash_credit = _strict_number(
                request.actual_net_cash_credit_cny,
                field="actual_net_cash_credit_cny",
                positive=True,
            )
            reported_gross_realized = _strict_number(
                request.actual_gross_realized_pnl_cny,
                field="actual_gross_realized_pnl_cny",
            )
        except MarketCapitalLedgerError as exc:
            return self._ashare_sell_decision(committed=False, reason=str(exc))
        if fee_cash < 0.0:
            return self._ashare_sell_decision(
                committed=False, reason="negative_sell_fee"
            )
        expected_proceeds = round(
            request.actual_closed_quantity * fill_price,
            6,
        )
        if not math.isclose(gross_proceeds, expected_proceeds, abs_tol=0.01):
            return self._ashare_sell_decision(
                committed=False, reason="ashare_sell_proceeds_mismatch"
            )
        if not math.isclose(
            net_cash_credit,
            gross_proceeds - fee_cash,
            abs_tol=0.01,
        ):
            return self._ashare_sell_decision(
                committed=False, reason="ashare_sell_net_cash_mismatch"
            )
        status = str(request.status or "").strip().lower()
        if status not in {"partial", "filled"}:
            return self._ashare_sell_decision(
                committed=False, reason="invalid_fill_status"
            )
        if not isinstance(request.terminal, bool):
            return self._ashare_sell_decision(
                committed=False, reason="invalid_terminal"
            )
        if status == "filled" and request.terminal is not True:
            return self._ashare_sell_decision(
                committed=False, reason="filled_must_be_terminal"
            )
        if str(request.side or "").strip().lower() != "sell":
            return self._ashare_sell_decision(
                committed=False, reason="invalid_ashare_sell_side"
            )
        try:
            filled_at = _parse_timestamp(request.filled_at, field="filled_at")
            pit = _parse_timestamp(
                request.point_in_time_as_of,
                field="point_in_time_as_of",
            )
        except MarketCapitalLedgerError as exc:
            return self._ashare_sell_decision(committed=False, reason=str(exc))
        if filled_at < pit:
            return self._ashare_sell_decision(
                committed=False, reason="fill_pit_regression"
            )

        incoming = self._ashare_sell_identity(asdict(request))
        with self._lock():
            events = self._load_events_unlocked()
            replay = self._replay(events)
            priors = [
                row
                for row in events
                if row.get("event_type") == "ashare_sell_commit"
                and str(row.get("reference_id") or "") == request.reference_id
            ]
            if priors:
                prior = priors[0]
                if self._ashare_sell_identity(prior) != incoming:
                    raise MarketCapitalLedgerError("ashare_sell_commit_conflict")
                return self._ashare_sell_decision(
                    committed=True,
                    reason="idempotent_ashare_sell_commit",
                    status="idempotent",
                    event_id=str(prior["event_id"]),
                    snapshot=replay.snapshot,
                    idempotent=True,
                )
            head = events[-1]
            if (
                str(head.get("event_id") or "") != request.expected_ledger_event_id
                or str(head.get("checksum") or "") != request.expected_ledger_checksum
            ):
                return self._ashare_sell_decision(
                    committed=False, reason="ledger_head_cas_mismatch"
                )
            if replay.snapshot.execution_lineage_id != request.execution_lineage_id:
                return self._ashare_sell_decision(
                    committed=False,
                    reason="ashare_sell_execution_lineage_mismatch",
                )
            prior_order_sells = [
                row
                for row in events
                if row.get("event_type") == "ashare_sell_commit"
                and str(row.get("order_id") or "") == request.order_id
            ]
            expected_sequence = 1 + max(
                (int(row.get("fill_sequence", 0)) for row in prior_order_sells),
                default=0,
            )
            if request.fill_sequence != expected_sequence:
                return self._ashare_sell_decision(
                    committed=False, reason="fill_sequence_mismatch"
                )
            current_quantity = int(
                replay.latest_position_quantity.get(request.risk_unit_key, 0)
            )
            if request.actual_closed_quantity > current_quantity:
                return self._ashare_sell_decision(
                    committed=False, reason="sell_quantity_exceeds_position"
                )
            sellable_quantity = self._project_ashare_sellable_quantity_unlocked(
                events,
                replay,
                as_of=filled_at,
            ).get(request.risk_unit_key.strip().upper(), 0)
            if request.actual_closed_quantity > sellable_quantity:
                return self._ashare_sell_decision(
                    committed=False,
                    reason="ashare_sell_quantity_exceeds_t1_sellable",
                )
            current_exposure = float(
                replay.latest_positions_mv.get(request.risk_unit_key, 0.0)
            )
            current_cost_basis = float(
                replay.latest_position_cost_basis.get(request.risk_unit_key, 0.0)
            )
            current_entry_fee = float(
                replay.latest_position_entry_fee.get(request.risk_unit_key, 0.0)
            )
            if (
                current_quantity <= 0
                or current_exposure <= 0.0
                or current_cost_basis <= 0.0
            ):
                return self._ashare_sell_decision(
                    committed=False, reason="ashare_position_accounting_unavailable"
                )
            close_ratio = request.actual_closed_quantity / current_quantity
            exposure_released = round(current_exposure * close_ratio, 6)
            cost_basis_released = round(current_cost_basis * close_ratio, 6)
            entry_fee_released = round(current_entry_fee * close_ratio, 6)
            derived_gross_realized = round(
                gross_proceeds - cost_basis_released,
                6,
            )
            if not math.isclose(
                reported_gross_realized,
                derived_gross_realized,
                abs_tol=0.01,
            ):
                return self._ashare_sell_decision(
                    committed=False,
                    reason="ashare_sell_realized_pnl_mismatch",
                )
            net_realized = round(
                derived_gross_realized - entry_fee_released - fee_cash,
                6,
            )
            event = {
                **incoming,
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "ashare_sell_commit",
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "currency": self.policy.currency,
                "amount_cny": net_realized,
                "actual_exposure_released_cny": exposure_released,
                "actual_cost_basis_released_cny": cost_basis_released,
                "actual_entry_fee_released_cny": entry_fee_released,
                "net_realized_pnl_cny": net_realized,
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "previous_checksum": str(head.get("checksum") or ""),
            }
            event["checksum"] = _compute_event_checksum(event)
            updated = self._replay([*events, event])
            self._project_ashare_sellable_quantity_unlocked(
                [*events, event],
                updated,
                as_of=filled_at,
            )
            self._append_event_unlocked(event)
            self._write_projection_unlocked(updated.snapshot)
            return self._ashare_sell_decision(
                committed=True,
                reason="ashare_sell_committed",
                status="committed",
                event_id=str(event["event_id"]),
                snapshot=updated.snapshot,
            )

    # ---- futures position close commit ----

    @staticmethod
    def _position_close_decision(
        *,
        committed: bool,
        reason: str,
        **kwargs: Any,
    ) -> MarketCapitalPositionCloseCommitDecision:
        return MarketCapitalPositionCloseCommitDecision(
            committed=committed,
            reason=reason,
            **kwargs,
        )

    @staticmethod
    def _position_close_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "reference_id",
            "risk_unit_key",
            "authority_id",
            "authority_generation",
            "execution_lineage_id",
            "lineage_sha256",
            "order_id",
            "idempotency_key",
            "execution_fill_id",
            "fill_sequence",
            "side",
            "status",
            "terminal",
            "actual_closed_quantity",
            "actual_fill_price",
            "actual_margin_released_cny",
            "actual_fee_cash_cny",
            "actual_gross_realized_pnl_cny",
            "filled_at",
            "point_in_time_as_of",
            "source",
            "source_sha256",
            "receipt_sha256",
            "local_position_sha256",
        )
        return {key: payload.get(key) for key in keys}

    def commit_position_close(
        self,
        request: MarketCapitalPositionCloseCommitRequest,
    ) -> MarketCapitalPositionCloseCommitDecision:
        """Atomically fold an actual futures close fill into cash and margin.

        The position snapshot must already be durable before this method is
        called.  A failed or interrupted call therefore leaves margin
        conservatively occupied; exact outbox replay is idempotent.
        """

        self._ensure_init()
        market = _normalize_market(request.market)
        if market != self.policy.market:
            return self._position_close_decision(
                committed=False, reason="market_mismatch"
            )
        if market != "cn_futures":
            return self._position_close_decision(
                committed=False, reason="position_close_only_cn_futures"
            )
        if request.authority_id != self.policy.capital_authority_id:
            return self._position_close_decision(committed=False, reason="aid_mismatch")
        if request.authority_generation != self.policy.authority_generation:
            return self._position_close_decision(committed=False, reason="gen_mismatch")
        required_strings = (
            "reference_id",
            "risk_unit_key",
            "execution_lineage_id",
            "order_id",
            "idempotency_key",
            "execution_fill_id",
            "source",
            "expected_ledger_event_id",
            "expected_ledger_checksum",
        )
        for field_name in required_strings:
            if not str(getattr(request, field_name) or "").strip():
                return self._position_close_decision(
                    committed=False, reason=f"missing_{field_name}"
                )
        for field_name in (
            "lineage_sha256",
            "source_sha256",
            "receipt_sha256",
            "local_position_sha256",
            "expected_ledger_checksum",
        ):
            if not _is_64hex(str(getattr(request, field_name) or "")):
                return self._position_close_decision(
                    committed=False, reason=f"invalid_{field_name}"
                )
        expected_reference = (
            f"MCAPCLOSE:{request.authority_generation}:"
            f"{request.execution_lineage_id}:{request.risk_unit_key}:"
            f"{request.execution_fill_id}"
        )
        if request.reference_id != expected_reference:
            return self._position_close_decision(
                committed=False, reason="position_close_reference_mismatch"
            )
        if (
            not isinstance(request.fill_sequence, int)
            or isinstance(request.fill_sequence, bool)
            or request.fill_sequence <= 0
        ):
            return self._position_close_decision(
                committed=False, reason="invalid_fill_sequence"
            )
        if (
            not isinstance(request.actual_closed_quantity, int)
            or isinstance(request.actual_closed_quantity, bool)
            or request.actual_closed_quantity <= 0
        ):
            return self._position_close_decision(
                committed=False, reason="invalid_actual_closed_quantity"
            )
        try:
            fill_price = _strict_number(
                request.actual_fill_price,
                field="actual_fill_price",
                positive=True,
            )
            margin_released = _strict_number(
                request.actual_margin_released_cny,
                field="actual_margin_released_cny",
                positive=True,
            )
            fee_cash = _strict_number(
                request.actual_fee_cash_cny,
                field="actual_fee_cash_cny",
            )
            gross_realized = _strict_number(
                request.actual_gross_realized_pnl_cny,
                field="actual_gross_realized_pnl_cny",
            )
        except MarketCapitalLedgerError as exc:
            return self._position_close_decision(committed=False, reason=str(exc))
        if fee_cash < 0.0:
            return self._position_close_decision(
                committed=False, reason="negative_close_fee"
            )
        status = str(request.status or "").strip().lower()
        if status not in {"partial", "filled"}:
            return self._position_close_decision(
                committed=False, reason="invalid_fill_status"
            )
        if not isinstance(request.terminal, bool):
            return self._position_close_decision(
                committed=False, reason="invalid_terminal"
            )
        if status == "filled" and request.terminal is not True:
            return self._position_close_decision(
                committed=False, reason="filled_must_be_terminal"
            )
        side = str(request.side or "").strip().lower()
        if side not in {"buy", "sell", "long", "short"}:
            return self._position_close_decision(
                committed=False, reason="invalid_cn_futures_close_side"
            )
        try:
            filled_at = _parse_timestamp(request.filled_at, field="filled_at")
            pit = _parse_timestamp(
                request.point_in_time_as_of,
                field="point_in_time_as_of",
            )
        except MarketCapitalLedgerError as exc:
            return self._position_close_decision(committed=False, reason=str(exc))
        if filled_at < pit:
            return self._position_close_decision(
                committed=False, reason="fill_pit_regression"
            )

        incoming = self._position_close_identity(asdict(request))
        with self._lock():
            events = self._load_events_unlocked()
            replay = self._replay(events)
            priors = [
                row
                for row in events
                if row.get("event_type") == "position_close_commit"
                and str(row.get("reference_id") or "") == request.reference_id
            ]
            if priors:
                prior = priors[0]
                if self._position_close_identity(prior) != incoming:
                    raise MarketCapitalLedgerError("position_close_commit_conflict")
                return self._position_close_decision(
                    committed=True,
                    reason="idempotent_position_close_commit",
                    status="idempotent",
                    event_id=str(prior["event_id"]),
                    snapshot=replay.snapshot,
                    idempotent=True,
                )

            head = events[-1]
            if (
                str(head.get("event_id") or "") != request.expected_ledger_event_id
                or str(head.get("checksum") or "") != request.expected_ledger_checksum
            ):
                return self._position_close_decision(
                    committed=False, reason="ledger_head_cas_mismatch"
                )
            if replay.snapshot.execution_lineage_id != request.execution_lineage_id:
                return self._position_close_decision(
                    committed=False,
                    reason="position_close_execution_lineage_mismatch",
                )
            prior_order_closes = [
                row
                for row in events
                if row.get("event_type") == "position_close_commit"
                and str(row.get("order_id") or "") == request.order_id
            ]
            expected_sequence = 1 + max(
                (int(row.get("fill_sequence", 0)) for row in prior_order_closes),
                default=0,
            )
            if request.fill_sequence != expected_sequence:
                return self._position_close_decision(
                    committed=False, reason="fill_sequence_mismatch"
                )
            position_margin = float(
                replay.latest_position_margin.get(request.risk_unit_key, 0.0)
            )
            position_quantity = int(
                replay.latest_position_quantity.get(request.risk_unit_key, 0)
            )
            if position_quantity == 0:
                return self._position_close_decision(
                    committed=False,
                    reason="cn_futures_position_inventory_unavailable",
                )
            if request.actual_closed_quantity > abs(position_quantity):
                return self._position_close_decision(
                    committed=False,
                    reason="close_quantity_exceeds_position",
                )
            expected_close_sides = (
                {"sell", "short"} if position_quantity > 0 else {"buy", "long"}
            )
            if side not in expected_close_sides:
                return self._position_close_decision(
                    committed=False,
                    reason="close_side_mismatch",
                )
            if margin_released > position_margin + 1e-9:
                return self._position_close_decision(
                    committed=False, reason="close_margin_exceeds_position"
                )
            expected_margin_release = round(
                position_margin
                * request.actual_closed_quantity
                / abs(position_quantity),
                6,
            )
            if not math.isclose(
                margin_released,
                expected_margin_release,
                abs_tol=0.01,
            ):
                return self._position_close_decision(
                    committed=False,
                    reason="close_margin_release_mismatch",
                )
            entry_price = float(
                replay.latest_position_entry_price.get(request.risk_unit_key, 0.0)
            )
            multiplier = float(
                replay.latest_position_contract_multiplier.get(
                    request.risk_unit_key, 0.0
                )
            )
            if entry_price <= 0.0 or multiplier <= 0.0:
                return self._position_close_decision(
                    committed=False,
                    reason="cn_futures_position_pricing_unavailable",
                )
            direction_sign = 1.0 if position_quantity > 0 else -1.0
            derived_gross_realized = round(
                (fill_price - entry_price)
                * request.actual_closed_quantity
                * multiplier
                * direction_sign,
                6,
            )
            if not math.isclose(
                gross_realized,
                derived_gross_realized,
                abs_tol=0.01,
            ):
                return self._position_close_decision(
                    committed=False,
                    reason="close_realized_pnl_mismatch",
                )
            event = {
                **incoming,
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "position_close_commit",
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "currency": self.policy.currency,
                "amount_cny": round(derived_gross_realized - fee_cash, 6),
                "net_realized_pnl_cny": round(derived_gross_realized - fee_cash, 6),
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "previous_checksum": str(head.get("checksum") or ""),
            }
            event["checksum"] = _compute_event_checksum(event)
            updated = self._replay([*events, event])
            self._append_event_unlocked(event)
            self._write_projection_unlocked(updated.snapshot)
            return self._position_close_decision(
                committed=True,
                reason="position_close_committed",
                status="committed",
                event_id=str(event["event_id"]),
                snapshot=updated.snapshot,
            )

    # ---- release ----

    def release(
        self, reservation_id: str, amount: float, reason: str, *, reference_id: str = ""
    ) -> dict:
        self._ensure_init()
        rid = str(reservation_id or "").strip()
        rr = str(reason or "").strip()
        if not rid:
            raise MarketCapitalLedgerError("missing_rid")
        if not rr:
            raise MarketCapitalLedgerError("missing_reason")
        amt = _strict_number(amount, field="amount", positive=True)
        rref = str(reference_id or "").strip() or f"release:{rid}:{amt:.6f}:{rr}"
        with self._lock():
            ev = self._load_events_unlocked()
            r = self._replay(ev)
            resv = r.reservations.get(rid)
            if resv is None:
                raise MarketCapitalLedgerError("unknown_reservation")
            priors = [
                row
                for row in ev
                if row.get("event_type") == "release"
                and str(row.get("reference_id") or "") == rref
            ]
            if priors:
                pr = priors[0]
                if (
                    str(pr.get("reservation_id") or "") != rid
                    or not math.isclose(
                        float(pr.get("amount_cny", 0.0)), amt, abs_tol=1e-9
                    )
                    or str(pr.get("reason") or "") != rr
                ):
                    raise MarketCapitalLedgerError("release_conflict")
                return {
                    "status": "idempotent_release",
                    "event_id": pr["event_id"],
                    "reservation_id": rid,
                    "amount_cny": amt,
                    "remaining_amount_cny": resv.remaining_amount_cny,
                    "snapshot": r.snapshot.as_dict(),
                    "real_trading_enabled": False,
                }
            if amt > resv.remaining_amount_cny + 1e-9:
                raise MarketCapitalLedgerError("release_exceeds")
            if self.policy.market == "ashare":
                release_ratio = amt / max(resv.remaining_cash_cny, 1e-12)
                cash_release = amt
                exposure_release = round(
                    resv.remaining_exposure_cny * release_ratio,
                    6,
                )
                margin_release = 0.0
            else:
                release_ratio = amt / max(resv.remaining_margin_cny, 1e-12)
                cash_release = round(
                    resv.remaining_cash_cny * release_ratio,
                    6,
                )
                exposure_release = 0.0
                margin_release = amt
            evt = {
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "release",
                "authority_id": self.policy.capital_authority_id,
                "authority_generation": self.policy.authority_generation,
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "reference_id": rref,
                "amount_cny": amt,
                "currency": self.policy.currency,
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "reservation_id": rid,
                "reservation_reference_id": resv.reference_id,
                "reason": rr,
                "risk_unit_key": resv.risk_unit_key,
                "cash_release_cny": cash_release,
                "exposure_release_cny": exposure_release,
                "margin_release_cny": margin_release,
                "execution_lineage_id": resv.execution_lineage_id,
                "lineage_sha256": resv.lineage_sha256,
                "point_in_time_as_of": resv.point_in_time_as_of,
                "previous_checksum": ev[-1].get("checksum", "")
                if ev
                else _sha256_hex(GENESIS_PREVIOUS_CHECKSUM),
            }
            evt["checksum"] = _compute_event_checksum(evt)
            self._append_event_unlocked(evt)
            upd = self._replay([*ev, evt])
            self._write_projection_unlocked(upd.snapshot)
            return {
                "status": "released",
                "event_id": evt["event_id"],
                "reservation_id": rid,
                "amount_cny": amt,
                "remaining_amount_cny": upd.reservations[rid].remaining_amount_cny,
                "snapshot": upd.snapshot.as_dict(),
                "real_trading_enabled": False,
            }

    def verify_release(
        self,
        *,
        reservation_id: str,
        amount_cny: float,
        reason: str,
        reference_id: str,
        expected_event_id: str,
        authority_id: str,
        authority_generation: int,
        execution_lineage_id: str,
        risk_unit_key: str,
        require_terminal: bool = False,
    ) -> dict:
        """Verify one exact release event and its immediate post-event state."""

        self._ensure_init()
        reservation = str(reservation_id or "").strip()
        release_reason = str(reason or "").strip()
        reference = str(reference_id or "").strip()
        event_id = str(expected_event_id or "").strip()
        lineage = str(execution_lineage_id or "").strip()
        risk_unit = str(risk_unit_key or "").strip()
        try:
            amount = _strict_number(amount_cny, field="amount", positive=True)
        except MarketCapitalLedgerError:
            return self._vf("invalid_amount")
        if not all(
            (reservation, release_reason, reference, event_id, lineage, risk_unit)
        ):
            return self._vf("release_identity_required")
        if authority_id != self.policy.capital_authority_id:
            return self._vf("aid_mismatch")
        if authority_generation != self.policy.authority_generation:
            return self._vf("gen_mismatch")
        if not isinstance(require_terminal, bool):
            return self._vf("invalid_terminal_requirement")
        with self._lock():
            events = self._load_events_unlocked()
            self._replay(events)
            matches = [
                (index, row)
                for index, row in enumerate(events)
                if row.get("event_type") == "release"
                and str(row.get("event_id") or "") == event_id
            ]
        if len(matches) != 1:
            return self._vf("release_event_not_found")
        event_index, row = matches[0]
        if (
            str(row.get("reservation_id") or "") != reservation
            or str(row.get("reference_id") or "") != reference
            or str(row.get("reason") or "") != release_reason
            or not math.isclose(float(row.get("amount_cny", 0.0)), amount, abs_tol=1e-9)
            or str(row.get("authority_id") or "") != authority_id
            or row.get("authority_generation") != authority_generation
            or str(row.get("execution_lineage_id") or "") != lineage
            or str(row.get("risk_unit_key") or "") != risk_unit
            or row.get("real_trading_enabled") is not False
        ):
            return self._vf("release_event_mismatch")
        post_release = self._replay(events[: event_index + 1])
        reservation_state = post_release.reservations.get(reservation)
        if reservation_state is None or (
            reservation_state.authority_id != authority_id
            or reservation_state.authority_generation != authority_generation
            or reservation_state.execution_lineage_id != lineage
            or reservation_state.risk_unit_key != risk_unit
        ):
            return self._vf("release_reservation_identity_mismatch")
        terminal = bool(
            reservation_state.terminal
            and reservation_state.remaining_cash_cny <= 1e-9
            and reservation_state.remaining_exposure_cny <= 1e-9
            and reservation_state.remaining_margin_cny <= 1e-9
        )
        if require_terminal and not terminal:
            return self._vf("release_not_terminal")
        return {
            "verified": True,
            "reason": "verified",
            "event_id": event_id,
            "reservation_id": reservation,
            "reference_id": reference,
            "amount_cny": round(amount, 6),
            "release_reason": release_reason,
            "authority_id": authority_id,
            "authority_generation": authority_generation,
            "execution_lineage_id": lineage,
            "risk_unit_key": risk_unit,
            "terminal_after_event": terminal,
            "remaining_cash_cny_after_event": round(
                reservation_state.remaining_cash_cny,
                6,
            ),
            "remaining_exposure_cny_after_event": round(
                reservation_state.remaining_exposure_cny,
                6,
            ),
            "remaining_margin_cny_after_event": round(
                reservation_state.remaining_margin_cny,
                6,
            ),
            "real_trading_enabled": False,
        }

    # ---- PnL ----

    def record_realized_pnl(
        self,
        *,
        reference_id: str,
        amount_cny: float,
        trade_date: str,
        affects_loss_streak: bool = True,
    ) -> dict:
        self._ensure_init()
        rid = str(reference_id or "").strip()
        if not rid:
            raise MarketCapitalLedgerError("missing_ref")
        nd = _validate_trade_date(trade_date)
        amt = _strict_number(amount_cny, field="pnl")
        if not isinstance(affects_loss_streak, bool):
            raise MarketCapitalLedgerError("invalid_affects")
        with self._lock():
            ev = self._load_events_unlocked()
            r = self._replay(ev)
            priors = [
                row
                for row in ev
                if row.get("event_type") == "realized_pnl"
                and str(row.get("reference_id") or "") == rid
            ]
            if priors:
                pr = priors[0]
                if (
                    not math.isclose(
                        float(pr.get("amount_cny", 0.0)), amt, abs_tol=1e-9
                    )
                    or str(pr.get("trade_date") or "") != nd
                    or bool(pr.get("affects_loss_streak", True))
                    is not affects_loss_streak
                ):
                    raise MarketCapitalLedgerError("pnl_conflict")
                return {
                    "status": "idempotent_realized_pnl",
                    "event_id": pr["event_id"],
                    "reference_id": rid,
                    "amount_cny": amt,
                    "affects_loss_streak": affects_loss_streak,
                    "snapshot": r.snapshot.as_dict(),
                    "real_trading_enabled": False,
                }
            evt = {
                "event_id": f"MCAP-{uuid.uuid4().hex}",
                "event_type": "realized_pnl",
                "authority_id": self.policy.capital_authority_id,
                "authority_generation": self.policy.authority_generation,
                "account_name": self.policy.account_name,
                "market": self.policy.market,
                "reference_id": rid,
                "amount_cny": amt,
                "currency": self.policy.currency,
                "created_at": _now_iso(),
                "real_trading_enabled": False,
                "trade_date": nd,
                "affects_loss_streak": affects_loss_streak,
                "previous_checksum": ev[-1].get("checksum", "")
                if ev
                else _sha256_hex(GENESIS_PREVIOUS_CHECKSUM),
            }
            evt["checksum"] = _compute_event_checksum(evt)
            upd = self._replay([*ev, evt])
            self._append_event_unlocked(evt)
            self._write_projection_unlocked(upd.snapshot)
            return {
                "status": "recorded",
                "event_id": evt["event_id"],
                "reference_id": rid,
                "amount_cny": amt,
                "affects_loss_streak": affects_loss_streak,
                "snapshot": upd.snapshot.as_dict(),
                "real_trading_enabled": False,
            }

    # ---- verify reservation ----

    def verify_reservation_identity(
        self,
        *,
        reservation_id: str,
        reference_id: str,
        market: str,
        authority_id: str,
        authority_generation: int,
        execution_lineage_id: str,
        risk_unit_key: str,
        expected_event_id: str,
    ) -> dict:
        """Verify immutable reservation identity even after full release."""

        self._ensure_init()
        rid = str(reservation_id or "").strip()
        ref = str(reference_id or "").strip()
        mk = _normalize_market(market)
        lineage = str(execution_lineage_id or "").strip()
        risk_unit = str(risk_unit_key or "").strip()
        event_id = str(expected_event_id or "").strip()
        if not all((rid, ref, lineage, risk_unit, event_id)):
            return self._vf("reservation_identity_required")
        if mk != self.policy.market:
            return self._vf("market_mismatch")
        if authority_id != self.policy.capital_authority_id:
            return self._vf("aid_mismatch")
        if authority_generation != self.policy.authority_generation:
            return self._vf("gen_mismatch")
        with self._lock():
            replay = self._replay(self._load_events_unlocked())
            reservation = replay.reservations.get(rid)
        if reservation is None:
            return self._vf("unknown")
        if (
            reservation.reference_id != ref
            or reservation.market != mk
            or reservation.authority_id != authority_id
            or reservation.authority_generation != authority_generation
            or reservation.execution_lineage_id != lineage
            or reservation.risk_unit_key != risk_unit
            or reservation.event_id != event_id
        ):
            return self._vf("reservation_identity_mismatch")
        return {
            "verified": True,
            "reason": "verified",
            "reservation_id": rid,
            "reference_id": ref,
            "market": mk,
            "authority_id": authority_id,
            "authority_generation": authority_generation,
            "execution_lineage_id": lineage,
            "lineage_sha256": reservation.lineage_sha256,
            "risk_unit_key": risk_unit,
            "event_id": event_id,
            "original_cash_cny": round(reservation.original_cash_cny, 6),
            "remaining_cash_cny": round(reservation.remaining_cash_cny, 6),
            "original_exposure_cny": round(
                reservation.original_exposure_cny,
                6,
            ),
            "remaining_exposure_cny": round(
                reservation.remaining_exposure_cny,
                6,
            ),
            "original_margin_cny": round(reservation.original_margin_cny, 6),
            "remaining_margin_cny": round(reservation.remaining_margin_cny, 6),
            "terminal": reservation.terminal,
            "real_trading_enabled": False,
        }

    def verify_reservation(
        self,
        *,
        reservation_id: str,
        reference_id: str,
        market: str,
        authority_id: str,
        retained_amount_cny: float,
        authority_generation: int | None = None,
        execution_lineage_id: str = "",
        risk_unit_key: str = "",
        expected_event_id: str = "",
    ) -> dict:
        self._ensure_init()
        rid = str(reservation_id or "").strip()
        ref = str(reference_id or "").strip()
        mk = _normalize_market(market)
        if not rid:
            return self._vf("missing_rid")
        if not ref:
            return self._vf("missing_ref")
        if mk != self.policy.market:
            return self._vf("market_mismatch")
        if authority_id != self.policy.capital_authority_id:
            return self._vf("aid_mismatch")
        if authority_generation is None:
            return self._vf("gen_required")
        if authority_generation != self.policy.authority_generation:
            return self._vf("gen_mismatch")
        if not str(execution_lineage_id or "").strip():
            return self._vf("execution_lineage_required")
        if not str(risk_unit_key or "").strip():
            return self._vf("risk_unit_required")
        try:
            ra = _strict_number(retained_amount_cny, field="retained", positive=True)
        except MarketCapitalLedgerError:
            return self._vf("invalid_retained")
        with self._lock():
            r = self._replay(self._load_events_unlocked())
            resv = r.reservations.get(rid)
            if resv is None:
                return self._vf("unknown")
            if resv.reference_id != ref:
                return self._vf("ref_mismatch")
            if resv.market != mk:
                return self._vf("mkt_mismatch")
            if resv.authority_id != authority_id:
                return self._vf("aid_mismatch")
            if resv.authority_generation != authority_generation:
                return self._vf("gen_mismatch")
            if resv.execution_lineage_id != execution_lineage_id:
                return self._vf("execution_lineage_mismatch")
            if resv.risk_unit_key != risk_unit_key:
                return self._vf("risk_unit_mismatch")
            if resv.remaining_amount_cny + 1e-9 < ra:
                return self._vf("insufficient")
            if expected_event_id and resv.event_id != expected_event_id:
                return self._vf("event_id_mismatch")
            return {
                "verified": True,
                "reason": "verified",
                "reservation_id": rid,
                "reference_id": ref,
                "market": mk,
                "authority_id": authority_id,
                "authority_generation": resv.authority_generation,
                "execution_lineage_id": resv.execution_lineage_id,
                "lineage_sha256": resv.lineage_sha256,
                "point_in_time_as_of": resv.point_in_time_as_of,
                "risk_unit_key": resv.risk_unit_key,
                "remaining_amount_cny": round(resv.remaining_amount_cny, 6),
                "retained_amount_cny": round(ra, 6),
                "event_id": resv.event_id,
                "real_trading_enabled": False,
            }

    @staticmethod
    def _vf(reason: str) -> dict:
        return {"verified": False, "reason": reason, "real_trading_enabled": False}

    # ---- checksum validation ----

    def validate_checksum_chain(self) -> dict:
        self._ensure_init()
        with self._lock():
            ev = self._load_events_unlocked()
        issues = []
        prev_exp = None
        for i, row in enumerate(ev, 1):
            comp = _compute_event_checksum(row)
            if comp != str(row.get("checksum") or ""):
                issues.append(f"cksum:{i}")
            pv = str(row.get("previous_checksum") or "")
            if i == 1:
                if pv != _sha256_hex(GENESIS_PREVIOUS_CHECKSUM):
                    issues.append(f"genesis_prev:{i}")
            elif prev_exp is not None and pv != prev_exp:
                issues.append(f"chain:{i}")
            prev_exp = str(row.get("checksum") or "")
        if issues:
            return {
                "status": "invalid",
                "issues": issues,
                "event_count": len(ev),
                "real_trading_enabled": False,
            }
        return {
            "status": "valid",
            "event_count": len(ev),
            "last_checksum": prev_exp or "",
            "real_trading_enabled": False,
        }


# ---- module-level wrappers ----


def market_capital_root(market: str) -> Path:
    mk = _normalize_market(market)
    if mk == "ashare":
        ev = "TRADINGAGENT_ASHARE_CAPITAL_ROOT"
        sd = "ashare"
    elif mk == "cn_futures":
        ev = "TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT"
        sd = "cn_futures"
    else:
        raise MarketCapitalLedgerError(f"unsupported:{mk}")
    cfg = os.environ.get(ev)
    return (
        Path(cfg).expanduser()
        if cfg
        else Path(__file__).resolve().parents[1] / "logs" / "capital" / sd
    )


def _is_default_production_root(market: str, root: Path) -> bool:
    """Check if root is the resolved default production root."""
    return root.resolve() == market_capital_root(market).resolve()


def load_market_capital_provider_state(
    market: str, trade_date: str, *, root=None, policy=None
):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return None
    return MarketCapitalLedger(rp, policy=pol).provider_state(trade_date)


def reserve_market_capital(market: str, request, *, root=None, policy=None):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return MarketCapitalReservationDecision(
            approved=False, reason="market_capital_unavailable"
        )
    return MarketCapitalLedger(rp, policy=pol).reserve(request)


def commit_market_capital_fill(market: str, request, *, root=None, policy=None):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return MarketCapitalFillCommitDecision(
            committed=False,
            reason="market_capital_unavailable",
        )
    return MarketCapitalLedger(rp, policy=pol).commit_fill(request)


def commit_market_capital_ashare_sell(market: str, request, *, root=None, policy=None):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return MarketCapitalAshareSellCommitDecision(
            committed=False,
            reason="market_capital_unavailable",
        )
    return MarketCapitalLedger(rp, policy=pol).commit_ashare_sell(request)


def commit_market_capital_position_close(
    market: str, request, *, root=None, policy=None
):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return MarketCapitalPositionCloseCommitDecision(
            committed=False,
            reason="market_capital_unavailable",
        )
    return MarketCapitalLedger(rp, policy=pol).commit_position_close(request)


def release_market_capital(
    market: str,
    reservation_id: str,
    amount: float,
    reason: str,
    *,
    reference_id: str,
    root=None,
    policy=None,
):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return {
            "status": "market_capital_unavailable",
            "reservation_id": str(reservation_id or ""),
            "amount_cny": float(amount),
            "real_trading_enabled": False,
        }
    return MarketCapitalLedger(rp, policy=pol).release(
        reservation_id, amount, reason, reference_id=reference_id
    )


def record_market_capital_realized_pnl(
    market: str,
    *,
    reference_id: str,
    amount_cny: float,
    trade_date: str,
    affects_loss_streak: bool = True,
    root=None,
    policy=None,
):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return {
            "status": "market_capital_unavailable",
            "reference_id": str(reference_id or ""),
            "amount_cny": float(amount_cny),
            "real_trading_enabled": False,
        }
    return MarketCapitalLedger(rp, policy=pol).record_realized_pnl(
        reference_id=reference_id,
        amount_cny=amount_cny,
        trade_date=trade_date,
        affects_loss_streak=affects_loss_streak,
    )


def verify_market_capital_reservation(
    market: str,
    *,
    reservation_id: str,
    reference_id: str,
    authority_id: str,
    retained_amount_cny: float,
    root=None,
    policy=None,
    **kw,
):
    mk = _normalize_market(market)
    rp = Path(root).expanduser() if root else market_capital_root(mk)
    pol = policy or MarketPolicy.load(mk)
    ef = rp / f"{pol.account_name}_capital_events.jsonl"
    if not ef.exists():
        return {
            "verified": False,
            "reason": "market_capital_unavailable",
            "real_trading_enabled": False,
        }
    return MarketCapitalLedger(rp, policy=pol).verify_reservation(
        reservation_id=reservation_id,
        reference_id=reference_id,
        market=mk,
        authority_id=authority_id,
        retained_amount_cny=retained_amount_cny,
        **kw,
    )


__all__ = [
    "MarketCapitalLedger",
    "MarketCapitalLedgerError",
    "MarketCapitalAshareSellCommitDecision",
    "MarketCapitalAshareSellCommitRequest",
    "MarketCapitalFillCommitDecision",
    "MarketCapitalFillCommitRequest",
    "MarketCapitalPositionCloseCommitDecision",
    "MarketCapitalPositionCloseCommitRequest",
    "MarketCapitalReservationDecision",
    "MarketCapitalReservationRequest",
    "MarketCapitalSnapshot",
    "OpeningStateManifest",
    "ReconcileManifest",
    "_compute_event_checksum",
    "GENESIS_PREVIOUS_CHECKSUM",
    "commit_market_capital_ashare_sell",
    "commit_market_capital_fill",
    "commit_market_capital_position_close",
    "load_market_capital_provider_state",
    "market_capital_root",
    "_is_default_production_root",
    "record_market_capital_realized_pnl",
    "release_market_capital",
    "reserve_market_capital",
    "verify_market_capital_reservation",
]
