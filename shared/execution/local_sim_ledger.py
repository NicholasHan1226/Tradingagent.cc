#!/usr/bin/env python3
"""Server-local simulated ledger for A-share backup fills."""

from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import os
import re
import time
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from shared.capital.ashare_position_authority import (
    canonical_sha256,
    normalize_ashare_positions,
)
from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
    EXECUTION_LINEAGE_SCHEMA_VERSION,
    ExecutionLineageError,
    build_execution_lineage,
    require_execution_lineage,
)
from shared.execution.execution_reality import ashare_execution_reality

LEGACY_LOCAL_SIM_DIR = Path(__file__).resolve().parent.parent / "logs" / "local_sim"
LOCAL_SIM_DIR = Path(
    os.environ.get(
        "TRADINGAGENT_ASHARE_EXECUTION_ROOT",
        str(
            Path(__file__).resolve().parent.parent
            / "logs"
            / "execution_lineages"
            / ASHARE_EXECUTION_LINEAGE_ID
        ),
    )
).expanduser()
LOCAL_SIM_TRADES = LOCAL_SIM_DIR / "local_sim_trades.jsonl"
LOCAL_SIM_POSITIONS = LOCAL_SIM_DIR / "local_sim_positions.json"
LOCAL_SIM_PNL = LOCAL_SIM_DIR / "local_sim_pnl.json"
LOCAL_SIM_LOCK = LOCAL_SIM_DIR / ".local_sim.lock"
LOCAL_SIM_POSITIONS_SNAPSHOT = LOCAL_SIM_DIR / "simulated_ashare_positions.json"
LOCAL_SIM_RECEIPTS = LOCAL_SIM_DIR / "sim_execution_receipts.jsonl"
DEFAULT_ACCOUNT = "ashare_sim"
ASHARE_SIM_DEFAULT_CASH = 50_000.0
LINEAGE_MANIFEST_FILENAME = "execution_lineage_manifest.json"
MARKET_CAPITAL_OUTBOX_FILENAME = "market_capital_outbox.json"
MARKET_CAPITAL_OUTBOX_SCHEMA_VERSION = "2026-07-12.ashare-market-capital-outbox.v2"
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.1
CHECKSUM_KEYS = {
    "payload_sha256",
    "receipt_sha256",
    "trade_sha256",
    "checksum",
    "sha256",
}
CN_TZ = ZoneInfo("Asia/Shanghai")


class LocalSimLedgerCorruption(RuntimeError):
    """Raised when the append-only A-share authority cannot be replayed."""


def _lineage_manifest_path(root: Path | None = None) -> Path:
    return Path(root or LOCAL_SIM_DIR) / LINEAGE_MANIFEST_FILENAME


def _market_capital_outbox_path(root: Path | None = None) -> Path:
    return Path(root or LOCAL_SIM_DIR) / MARKET_CAPITAL_OUTBOX_FILENAME


def _path_or_ancestor_is_symlink(path: Path) -> bool:
    current = path.absolute()
    system_root_aliases = {Path("/var"), Path("/tmp"), Path("/etc")}
    while True:
        if current.is_symlink() and current not in system_root_aliases:
            return True
        if current == current.parent:
            return False
        current = current.parent


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    if path.exists() and path.is_symlink():
        raise LocalSimLedgerCorruption(
            f"fresh_execution_symlink_not_allowed:{path.name}"
        )
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise LocalSimLedgerCorruption(
            f"fresh_execution_write_failed:{path.name}"
        ) from exc


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(
        path,
        (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8"),
    )


def _empty_account_projection(
    account: str,
    *,
    point_in_time_as_of: str,
    lineage: dict[str, Any],
) -> dict[str, Any]:
    return {
        **lineage,
        "account": account,
        "cash_available": ASHARE_SIM_DEFAULT_CASH,
        "market_value": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": 0.0,
        "positions": {},
        "total_trades": 0,
        "point_in_time_as_of": point_in_time_as_of,
        "real_trading_enabled": False,
    }


def _read_fresh_lineage_manifest(root: Path | None = None) -> dict[str, Any] | None:
    execution_root = Path(root or LOCAL_SIM_DIR)
    if execution_root.name != ASHARE_EXECUTION_LINEAGE_ID:
        raise LocalSimLedgerCorruption(
            "fresh_execution_root_must_be_lineage_namespaced"
        )
    if execution_root.absolute() == LEGACY_LOCAL_SIM_DIR.absolute():
        raise LocalSimLedgerCorruption("legacy_local_sim_root_forbidden")
    manifest_path = _lineage_manifest_path(execution_root)
    if _path_or_ancestor_is_symlink(execution_root) or manifest_path.is_symlink():
        raise LocalSimLedgerCorruption("fresh_execution_symlink_not_allowed")
    if not execution_root.exists() or not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalSimLedgerCorruption("fresh_execution_manifest_unreadable") from exc
    try:
        require_execution_lineage(payload)
    except ExecutionLineageError as exc:
        raise LocalSimLedgerCorruption(str(exc)) from exc
    if payload.get("source") != "fresh_zero_import_bootstrap":
        raise LocalSimLedgerCorruption("fresh_execution_manifest_source_invalid")
    if (
        type(payload.get("imported_legacy_record_count")) is not int
        or payload.get("imported_legacy_record_count") != 0
    ):
        raise LocalSimLedgerCorruption("legacy_import_detected")
    if payload.get("legacy_roots_read") != []:
        raise LocalSimLedgerCorruption("fresh_bootstrap_read_legacy_roots")
    initial_cash = payload.get("initial_cash_cny")
    if (
        isinstance(initial_cash, bool)
        or not isinstance(initial_cash, (int, float))
        or float(initial_cash) != ASHARE_SIM_DEFAULT_CASH
    ):
        raise LocalSimLedgerCorruption("fresh_execution_initial_cash_invalid")
    if payload.get("real_trading_enabled") is not False:
        raise LocalSimLedgerCorruption("fresh_execution_real_trading_forbidden")
    return payload


def get_local_sim_execution_lineage_manifest(
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Return the validated fresh manifest without creating any state."""

    manifest = _read_fresh_lineage_manifest(Path(root) if root is not None else None)
    if manifest is None:
        return {
            "status": "execution_lineage_unavailable",
            "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
            "real_trading_enabled": False,
        }
    return {**dict(manifest), "status": "ready", "real_trading_enabled": False}


def build_local_sim_order_lineage(*, point_in_time_as_of: str) -> dict[str, Any]:
    """Build an order lineage from the validated manifest authority."""

    manifest = _read_fresh_lineage_manifest()
    if manifest is None:
        raise ExecutionLineageError("execution_lineage_unavailable")
    return build_execution_lineage(
        lineage_started_at=manifest["lineage_started_at"],
        point_in_time_as_of=point_in_time_as_of,
    )


LINEAGE_PROJECTION_FIELDS = (
    "schema_version",
    "capital_authority_id",
    "authority_generation",
    "execution_lineage_id",
    "lineage_started_at",
    "point_in_time_as_of",
    "execution_lineage_sha256",
)


def _latest_lineage_projection(
    trades: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    authority = manifest or _read_fresh_lineage_manifest()
    if authority is None:
        raise LocalSimLedgerCorruption("execution_lineage_unavailable")
    latest = require_execution_lineage(authority)
    latest_timestamp = datetime.fromisoformat(latest["point_in_time_as_of"])
    for trade in trades:
        candidate = require_execution_lineage(trade)
        candidate_timestamp = datetime.fromisoformat(candidate["point_in_time_as_of"])
        if candidate_timestamp >= latest_timestamp:
            latest = candidate
            latest_timestamp = candidate_timestamp
    return {field: latest[field] for field in LINEAGE_PROJECTION_FIELDS}


def bootstrap_fresh_local_sim(
    *,
    root: Path | str,
    lineage_started_at: str,
    point_in_time_as_of: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """Explicitly create a zero-import fresh execution authority.

    This is the only function allowed to create an execution root.  It never
    reads, copies, migrates, or scans the retired ``shared/logs/local_sim``
    tree.
    """

    account_name = _require_authoritative_account(account)
    execution_root = Path(root).expanduser()
    if execution_root.name != ASHARE_EXECUTION_LINEAGE_ID:
        raise LocalSimLedgerCorruption(
            "fresh_execution_root_must_be_lineage_namespaced"
        )
    if _path_or_ancestor_is_symlink(execution_root):
        raise LocalSimLedgerCorruption("fresh_execution_symlink_not_allowed")
    if execution_root.absolute() == LEGACY_LOCAL_SIM_DIR.absolute():
        raise LocalSimLedgerCorruption("legacy_local_sim_root_forbidden")
    if execution_root.exists() and any(execution_root.iterdir()):
        raise LocalSimLedgerCorruption("fresh_execution_root_not_empty")

    lineage = build_execution_lineage(
        lineage_started_at=lineage_started_at,
        point_in_time_as_of=point_in_time_as_of,
    )
    execution_root.parent.mkdir(parents=True, exist_ok=True)
    execution_root.mkdir(mode=0o700, exist_ok=True)
    projection = _empty_account_projection(
        account_name,
        point_in_time_as_of=lineage["point_in_time_as_of"],
        lineage=lineage,
    )
    manifest = {
        **lineage,
        "source": "fresh_zero_import_bootstrap",
        "initial_cash_cny": ASHARE_SIM_DEFAULT_CASH,
        "imported_legacy_record_count": 0,
        "legacy_roots_read": [],
        "created_at": lineage["point_in_time_as_of"],
        "real_trading_enabled": False,
    }
    outbox = {
        **lineage,
        "schema_version": MARKET_CAPITAL_OUTBOX_SCHEMA_VERSION,
        "actions": [],
        "updated_at": lineage["point_in_time_as_of"],
        "real_trading_enabled": False,
    }
    outbox["payload_sha256"] = _payload_sha256(outbox, drop_checksums=True)
    _atomic_write_json(_lineage_manifest_path(execution_root), manifest)
    _atomic_write_bytes(execution_root / LOCAL_SIM_TRADES.name, b"")
    _atomic_write_bytes(execution_root / LOCAL_SIM_RECEIPTS.name, b"")
    _atomic_write_json(
        execution_root / LOCAL_SIM_POSITIONS.name, projection["positions"]
    )
    _atomic_write_json(execution_root / LOCAL_SIM_PNL.name, projection)
    position_snapshot = {
        **lineage,
        "snapshot_id": "simulated_ashare_positions",
        "market": "ashare",
        "account_type": "simulated",
        "capital_layer": "simulated",
        "source": "server_local_sim_backup",
        "synced_at": lineage["point_in_time_as_of"],
        "trade_date": _trade_date(lineage["point_in_time_as_of"]),
        "positions": [],
        "positions_by_account": {account_name: {}},
        "pnl": {account_name: projection},
        "account_view": "strategy_samples_only",
        "audit_positions_by_account": {account_name: {}},
        "audit_pnl": {account_name: projection},
        "bootstrap_state": "no_trades_yet",
        "cash_available": ASHARE_SIM_DEFAULT_CASH,
        "mark_evidence_by_symbol": {},
        "real_trading_enabled": False,
    }
    _atomic_write_json(
        execution_root / LOCAL_SIM_POSITIONS_SNAPSHOT.name,
        position_snapshot,
    )
    _atomic_write_json(_market_capital_outbox_path(execution_root), outbox)
    _atomic_write_bytes(execution_root / LOCAL_SIM_LOCK.name, b"")
    return {
        "status": "bootstrapped",
        **lineage,
        "source": manifest["source"],
        "imported_legacy_record_count": 0,
        "cash_available": ASHARE_SIM_DEFAULT_CASH,
        "positions": {},
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "root": str(execution_root),
        "real_trading_enabled": False,
    }


def _bj_today() -> date:
    """Return today's date in Beijing time (UTC+8)."""
    from datetime import timedelta as _td

    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = timezone(_td(hours=8))
    return datetime.now(tz).date()


@dataclass
class LocalSimTrade:
    schema_version: str = EXECUTION_LINEAGE_SCHEMA_VERSION
    trade_id: str = field(default_factory=lambda: f"LSIM-{uuid.uuid4().hex[:12]}")
    order_id: str = ""
    idempotency_key: str = ""
    market: str = "ashare"
    account: str = DEFAULT_ACCOUNT
    trade_date: str = field(default_factory=lambda: _bj_today().isoformat())
    ts_code: str = ""
    side: str = ""
    quantity: int = 0
    requested_price: float = 0.0
    filled_price: float = 0.0
    slippage_bps: float = 0.0
    amount: float = 0.0
    commission: float = 0.0
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0
    total_fee: float = 0.0
    execution_reality_model_version: str = ""
    commission_schedule_status: str = ""
    commission_schedule_version: str = ""
    net_amount: float = 0.0
    status: str = "filled"
    source: str = "server_local_sim_backup"
    candidate_pool_layer: str = ""
    execution_source: str = ""
    sample_intent: str = ""
    sample_layer: str = ""
    execution_eligible: bool = False
    primary_style: str = ""
    fill_price_source: str = ""
    fill_price_source_class: str = ""
    fill_evidence: dict[str, Any] = field(default_factory=dict)
    capital_scope: str = ""
    retry_of: str = ""
    retry_attempt: int = 0
    hypothesis_id: str = ""
    research_hypothesis: dict[str, Any] = field(default_factory=dict)
    factor_snapshot: dict[str, Any] = field(default_factory=dict)
    capital_authority_id: str = ASHARE_CAPITAL_AUTHORITY_ID
    authority_generation: int = ASHARE_AUTHORITY_GENERATION
    execution_lineage_id: str = ASHARE_EXECUTION_LINEAGE_ID
    lineage_started_at: str = ""
    point_in_time_as_of: str = ""
    execution_lineage_sha256: str = ""
    capital_cny: float = ASHARE_SIM_DEFAULT_CASH
    market_capital_required: bool = False
    market_capital_reference_id: str = ""
    market_capital_reservation_id: str = ""
    market_capital_event_id: str = ""
    market_capital_risk_unit_key: str = ""
    market_capital_expected_head_event_id: str = ""
    market_capital_expected_head_checksum: str = ""
    market_capital_fill_sequence: int = 1
    market_capital_source_sha256: str = ""
    market_capital_receipt_sha256: str = ""
    market_reserved_gross_cny: float = 0.0
    market_retained_gross_cny: float = 0.0
    market_release_allocations: list[dict[str, Any]] = field(default_factory=list)
    released_principal_cost_basis_cny: float = 0.0
    released_entry_fee_cny: float = 0.0
    gross_realized_pnl_cny: float = 0.0
    realized_pnl_cny: float = 0.0
    partial_terminal: bool = False
    real_trading_enabled: bool = False
    trade_sha256: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    trade_timestamp_bj: str = ""
    ashare_session_valid: bool = True
    ashare_session_rejection: str = ""
    linked_execution_status: str = ""
    note: str = ""


@contextmanager
def _lock() -> Iterator[None]:
    manifest = _read_fresh_lineage_manifest()
    if manifest is None:
        raise LocalSimLedgerCorruption("execution_lineage_unavailable")
    if LOCAL_SIM_LOCK.is_symlink() or not LOCAL_SIM_LOCK.is_file():
        raise LocalSimLedgerCorruption("fresh_execution_lock_unavailable")
    with LOCAL_SIM_LOCK.open("r+", encoding="utf-8") as handle:
        _acquire_exclusive_lock(handle.fileno(), LOCAL_SIM_LOCK)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_exclusive_lock(fd: int, lock_path: Path) -> None:
    last_error: OSError | None = None
    retry_errnos = {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }
    for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in retry_errnos:
                raise
            last_error = exc
            if attempt < LOCK_RETRY_ATTEMPTS:
                time.sleep(LOCK_RETRY_DELAY_SECONDS * attempt)
    raise TimeoutError(
        f"Could not acquire local sim lock {lock_path} after {LOCK_RETRY_ATTEMPTS} attempts"
    ) from last_error


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _verified_ashare_execution_evidence(
    evidence: Any,
    source_class: Any = "",
) -> bool:
    if not isinstance(evidence, dict):
        return False
    evidence_class = str(evidence.get("execution_evidence_class") or "").strip()
    normalized_source_class = str(
        evidence.get("fill_price_source_class") or source_class or ""
    ).strip()
    return (
        evidence_class == "verified_5min_market_data"
        and normalized_source_class in {"market_data", "verified_5min_market_data"}
        and bool(str(evidence.get("fill_price_source") or "").strip())
        and bool(str(evidence.get("bar_time") or "").strip())
        and _safe_float(evidence.get("bar_volume"), 0.0) > 0
    )


def _snapshot_writer_authority_error() -> str | None:
    """Validate the fresh lineage manifest before a snapshot writer touches disk."""

    try:
        manifest = _read_fresh_lineage_manifest()
    except LocalSimLedgerCorruption as exc:
        return str(exc)
    return None if manifest is not None else "execution_lineage_unavailable"


def _account_name(account: Any) -> str:
    if isinstance(account, dict):
        for key in ("account", "account_id", "account_name", "name"):
            value = str(account.get(key) or "").strip()
            if value:
                return value
    value = str(account or "").strip()
    return value or DEFAULT_ACCOUNT


def _require_authoritative_account(account: Any) -> str:
    account_name = _account_name(account)
    if account_name != DEFAULT_ACCOUNT:
        raise LocalSimLedgerCorruption(
            "ashare_authoritative_account_must_be_ashare_sim"
        )
    return account_name


def _is_regular_ashare_symbol(symbol: Any) -> bool:
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        digits, exchange = raw.split(".", 1)
    else:
        digits, exchange = raw, ""
    if not re.fullmatch(r"\d{6}", digits):
        return False
    if exchange == "SZ":
        return digits.startswith(("000", "001", "002", "003", "300", "301"))
    if exchange == "SH":
        return digits.startswith(("600", "601", "603", "605", "688", "689"))
    return digits.startswith(
        (
            "000",
            "001",
            "002",
            "003",
            "300",
            "301",
            "600",
            "601",
            "603",
            "605",
            "688",
            "689",
        )
    )


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _is_ashare_regular_session(ts: datetime) -> bool:
    try:
        from Ashare.t_plus_1 import is_trading_day

        if not is_trading_day(ts.date()):
            return False
    except Exception:
        if ts.weekday() >= 5:
            return False
    current = ts.time()
    return (dt_time(9, 30) <= current <= dt_time(11, 30)) or (
        dt_time(13, 0) <= current <= dt_time(14, 57)
    )


def _ashare_session_metadata(
    market: Any, symbol: Any, created_at: str
) -> dict[str, Any]:
    if str(market or "").strip().lower() != "ashare" or not _is_regular_ashare_symbol(
        symbol
    ):
        return {
            "trade_timestamp_bj": "",
            "ashare_session_valid": True,
            "ashare_session_rejection": "",
        }
    ts = _parse_timestamp(created_at) or datetime.now(CN_TZ)
    session_valid = _is_ashare_regular_session(ts)
    return {
        "trade_timestamp_bj": ts.isoformat(timespec="seconds"),
        "ashare_session_valid": session_valid,
        "ashare_session_rejection": ""
        if session_valid
        else "outside_regular_session_09:30-11:30_13:00-14:57",
    }


def _ashare_provenance_error(
    side: str,
    candidate_pool_layer: str,
    execution_source: str,
    sample_intent: str = "",
) -> str:
    side_key = str(side or "").lower().strip()
    layer = str(candidate_pool_layer or "").lower().strip()
    source = str(execution_source or "").lower().strip()
    intent = str(sample_intent or "").lower().strip()
    if side_key == "buy":
        valid_candidate = (
            layer == "candidate"
            and source == "ashare_candidate_layer"
            and intent in {"", "exploitation"}
        )
        valid_exploration = (
            layer == "exploration"
            and source == "ashare_candidate_layer"
            and intent == "exploration"
        )
        if not (valid_candidate or valid_exploration):
            return (
                "A-share simulated buy requires candidate_pool_layer=candidate "
                "with sample_intent=exploitation, or "
                "candidate_pool_layer=exploration with sample_intent=exploration; "
                "execution_source=ashare_candidate_layer"
            )
    if side_key == "sell" and source != "ashare_rebalance_sell":
        return "A-share simulated sell requires execution_source=ashare_rebalance_sell"
    return ""


def _canonical_json(payload: dict[str, Any], *, drop_checksums: bool = False) -> bytes:
    data = {
        key: value
        for key, value in payload.items()
        if not (drop_checksums and key in CHECKSUM_KEYS)
    }
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _payload_sha256(payload: dict[str, Any], *, drop_checksums: bool = False) -> str:
    return hashlib.sha256(
        _canonical_json(payload, drop_checksums=drop_checksums)
    ).hexdigest()


def _append_receipt_unlocked(receipt: dict[str, Any]) -> None:
    if LOCAL_SIM_RECEIPTS.is_symlink():
        raise LocalSimLedgerCorruption("local_sim_receipt_symlink_not_allowed")
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(LOCAL_SIM_RECEIPTS, flags)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=True) as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise LocalSimLedgerCorruption("local_sim_receipt_append_failed") from exc


def _build_signed_receipt(
    *,
    order: dict[str, Any],
    trade: LocalSimTrade | None,
    market: str,
    account: str,
    status: str,
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "receipt_type": "server_local_sim",
        "source": "server_local_sim_backup",
        "market": market,
        "account": account,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "order_id": str(order.get("order_id") or (trade.order_id if trade else "")),
        "idempotency_key": str(
            order.get("idempotency_key") or (trade.idempotency_key if trade else "")
        ),
        "symbol": str(
            order.get("ts_code")
            or order.get("symbol")
            or (trade.ts_code if trade else "")
        ),
        "side": str(
            order.get("side") or order.get("direction") or (trade.side if trade else "")
        ),
        "status": status,
        "success": status in {"filled", "partial"},
        "message": message,
        "receipt_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload_sha256": _payload_sha256(order),
    }
    if trade is not None:
        payload.update(
            {
                "trade_id": trade.trade_id,
                "trade_date": trade.trade_date,
                "filled_qty": trade.quantity,
                "avg_price": trade.filled_price,
                "commission": trade.commission,
                "stamp_duty": trade.stamp_duty,
                "transfer_fee": trade.transfer_fee,
                "total_fee": trade.total_fee,
                "execution_reality_model_version": (
                    trade.execution_reality_model_version
                ),
                "commission_schedule_status": trade.commission_schedule_status,
                "commission_schedule_version": trade.commission_schedule_version,
                "net_amount": trade.net_amount,
                "fill_price_source": trade.fill_price_source,
                "fill_price_source_class": trade.fill_price_source_class,
                "fill_evidence": trade.fill_evidence,
                "sample_intent": trade.sample_intent,
                "primary_style": trade.primary_style,
                "real_trading_enabled": False,
            }
        )
    if extra:
        payload.update(extra)
    payload["receipt_sha256"] = _payload_sha256(payload, drop_checksums=True)
    return payload


def _trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 10:
        return raw[:10].replace("/", "-")
    return date.today().isoformat()


def _order_trade_date(order: dict[str, Any], idempotency_key: str) -> str:
    explicit = order.get("trade_date") or order.get("valid_until") or order.get("date")
    if explicit not in (None, ""):
        return _trade_date(explicit)
    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", str(idempotency_key or ""))
    if match:
        return _trade_date(match.group(1))
    return _trade_date(None)


def _load_trades_unlocked() -> list[dict[str, Any]]:
    if not LOCAL_SIM_TRADES.exists():
        raise LocalSimLedgerCorruption("local_sim_trade_log_unavailable")
    manifest = _read_fresh_lineage_manifest()
    if manifest is None:
        raise LocalSimLedgerCorruption("execution_lineage_unavailable")
    rows: list[dict[str, Any]] = []
    if LOCAL_SIM_TRADES.is_symlink():
        raise LocalSimLedgerCorruption("local_sim_trade_log_symlink_not_allowed")
    with LOCAL_SIM_TRADES.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise LocalSimLedgerCorruption(
                    f"corrupt_local_sim_trade:{line_number}:blank_line"
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LocalSimLedgerCorruption(
                    f"corrupt_local_sim_trade:{line_number}:invalid_json"
                ) from exc
            if not isinstance(row, dict):
                raise LocalSimLedgerCorruption(
                    f"corrupt_local_sim_trade:{line_number}:invalid_shape"
                )
            checksum = str(row.get("trade_sha256") or "").strip()
            if checksum and checksum != _payload_sha256(row, drop_checksums=True):
                raise LocalSimLedgerCorruption(
                    f"corrupt_local_sim_trade:{line_number}:checksum_mismatch"
                )
            try:
                lineage = require_execution_lineage(row)
            except ExecutionLineageError as exc:
                raise LocalSimLedgerCorruption(
                    f"invalid_execution_lineage:{line_number}:{exc}"
                ) from exc
            for field in (
                "capital_authority_id",
                "authority_generation",
                "execution_lineage_id",
                "lineage_started_at",
            ):
                if lineage[field] != manifest[field]:
                    raise LocalSimLedgerCorruption(
                        f"invalid_execution_lineage:{line_number}:{field}_manifest_mismatch"
                    )
            try:
                _require_authoritative_account(row.get("account"))
            except LocalSimLedgerCorruption as exc:
                raise LocalSimLedgerCorruption(
                    f"invalid_account_authority:{line_number}:{exc}"
                ) from exc
            rows.append(row)
    return rows


def _can_sell_on(entry_date: Any, trade_date: Any) -> bool:
    try:
        from Ashare.t_plus_1 import can_sell

        return bool(can_sell(entry_date, trade_date))
    except Exception:
        try:
            return _trade_date(entry_date) < _trade_date(trade_date)
        except Exception:
            return False


def _starting_cash(value: Any) -> float:
    if value in (None, ""):
        return ASHARE_SIM_DEFAULT_CASH
    cash = _safe_float(value, float("nan"))
    if cash != ASHARE_SIM_DEFAULT_CASH:
        raise LocalSimLedgerCorruption("fresh_initial_cash_mismatch")
    return ASHARE_SIM_DEFAULT_CASH


def _starting_cash_for_bootstrap(value: Any = None) -> float:
    return _starting_cash(value)


def _position_source_envelope(
    positions: Any,
    *,
    trade_date: str,
    position_authority: Any,
    source: str,
) -> dict[str, Any]:
    """Build a source-owned envelope from a pre-read current authority view."""

    blocked = {
        "source": source,
        "position_source_status": "blocked",
        "position_source_reason": "position_authority_context_invalid",
    }
    if not isinstance(position_authority, dict):
        return blocked
    if position_authority.get("status") != "verified":
        return blocked
    authority_id = str(position_authority.get("authority_id") or "")
    generation = position_authority.get("authority_generation")
    lineage_id = str(position_authority.get("execution_lineage_id") or "")
    authority_checksum = str(position_authority.get("authority_checksum") or "").lower()
    requested_date = _trade_date(trade_date).replace("-", "")
    authority_date = _trade_date(position_authority.get("trade_date")).replace("-", "")
    if (
        authority_id != ASHARE_CAPITAL_AUTHORITY_ID
        or isinstance(generation, bool)
        or generation != ASHARE_AUTHORITY_GENERATION
        or lineage_id != ASHARE_EXECUTION_LINEAGE_ID
        or len(authority_checksum) != 64
        or any(ch not in "0123456789abcdef" for ch in authority_checksum)
        or authority_date != requested_date
    ):
        return blocked
    normalized, _, reason = normalize_ashare_positions(positions)
    if normalized is None:
        return {
            **blocked,
            "position_source_reason": f"position_source_invalid:{reason}",
        }
    return {
        "source": source,
        "position_source_status": "ready",
        "authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
        "authority_checksum": authority_checksum,
        "trade_date": requested_date,
        "position_count": len(normalized),
        "positions_fingerprint": canonical_sha256(normalized),
    }


def _sim_account_snapshot_unlocked(
    trades: list[dict[str, Any]],
    *,
    account: str,
    symbol: str = "",
    trade_date: str = "",
    starting_cash: float = ASHARE_SIM_DEFAULT_CASH,
) -> dict[str, Any]:
    lots_by_symbol: dict[str, list[dict[str, Any]]] = {}
    cash_available = float(starting_cash)
    as_of = _trade_date(trade_date)
    for trade in trades:
        if str(trade.get("account") or "") != account:
            continue
        if str(trade.get("status") or "") not in {"filled", "partial"}:
            continue
        code = str(trade.get("ts_code") or "").strip().upper()
        if not code:
            continue
        side = str(trade.get("side") or "").lower()
        qty = _safe_float(trade.get("quantity"), 0.0)
        net_amount = _safe_float(trade.get("net_amount"), 0.0)
        if qty <= 0:
            continue
        if side == "buy":
            cash_available -= net_amount
            lots_by_symbol.setdefault(code, []).append(
                {
                    "quantity": qty,
                    "trade_date": _trade_date(trade.get("trade_date")),
                    "cost_basis": net_amount,
                    "sample_intent": str(trade.get("sample_intent") or "exploitation")
                    .strip()
                    .lower(),
                }
            )
            continue
        if side != "sell":
            continue
        cash_available += net_amount
        remaining = qty
        for lot in lots_by_symbol.get(code, []):
            if remaining <= 0:
                break
            lot_qty = _safe_float(lot.get("quantity"), 0.0)
            used = min(lot_qty, remaining)
            lot_cost = _safe_float(lot.get("cost_basis"), 0.0)
            released_cost = lot_cost * used / lot_qty if lot_qty > 0 else 0.0
            lot["quantity"] = round(lot_qty - used, 8)
            lot["cost_basis"] = round(max(0.0, lot_cost - released_cost), 2)
            remaining -= used
    positions: dict[str, dict[str, Any]] = {}
    for code, lots in lots_by_symbol.items():
        open_lots = [lot for lot in lots if _safe_float(lot.get("quantity"), 0.0) > 0]
        quantity = sum(_safe_float(lot.get("quantity"), 0.0) for lot in open_lots)
        sellable_quantity = sum(
            _safe_float(lot.get("quantity"), 0.0)
            for lot in open_lots
            if _can_sell_on(lot.get("trade_date"), as_of)
        )
        if quantity <= 0:
            continue
        exploration_quantity = sum(
            _safe_float(lot.get("quantity"), 0.0)
            for lot in open_lots
            if str(lot.get("sample_intent") or "").strip().lower() == "exploration"
        )
        exploration_exposure = sum(
            _safe_float(lot.get("cost_basis"), 0.0)
            for lot in open_lots
            if str(lot.get("sample_intent") or "").strip().lower() == "exploration"
        )
        if exploration_quantity <= 1e-9:
            sample_intent = "exploitation"
        elif abs(exploration_quantity - quantity) <= 1e-9:
            sample_intent = "exploration"
        else:
            sample_intent = "mixed"
        oldest_open_date = min(
            str(lot.get("trade_date") or "")
            for lot in open_lots
            if lot.get("trade_date")
        )
        positions[code] = {
            "quantity": int(quantity)
            if abs(quantity - round(quantity)) < 1e-12
            else round(quantity, 6),
            "sellable_quantity": int(sellable_quantity)
            if abs(sellable_quantity - round(sellable_quantity)) < 1e-12
            else round(sellable_quantity, 6),
            "oldest_open_date": oldest_open_date,
            "entry_date": oldest_open_date,
            "sample_intent": sample_intent,
            "exploration_quantity": int(exploration_quantity)
            if abs(exploration_quantity - round(exploration_quantity)) < 1e-12
            else round(exploration_quantity, 6),
            "exploration_exposure_cny": round(exploration_exposure, 2),
        }
    selected = positions.get(str(symbol or "").strip().upper(), {}) if symbol else {}
    return {
        "account": account,
        "trade_date": as_of,
        "cash_available": round(cash_available, 2),
        "sellable_qty": selected.get("sellable_quantity", 0 if symbol else None),
        "position_qty": selected.get("quantity", 0 if symbol else None),
        "positions": positions,
    }


def get_local_sim_account_snapshot(
    account: dict[str, Any] | str | None = None,
    *,
    symbol: str = "",
    trade_date: str = "",
    starting_cash: Any = ASHARE_SIM_DEFAULT_CASH,
    include_validation_samples: bool = False,
    position_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return server-local simulated cash and T+1 sellable quantity snapshot."""

    try:
        account_name = _require_authoritative_account(account or DEFAULT_ACCOUNT)
    except LocalSimLedgerCorruption as exc:
        return {
            "status": "rejected",
            "reason": str(exc),
            "account": _account_name(account),
            "cash_available": None,
            "positions": {},
            "real_trading_enabled": False,
        }
    manifest = _read_fresh_lineage_manifest()
    if manifest is None:
        return {
            "status": "execution_lineage_unavailable",
            "account": account_name,
            "cash_available": None,
            "positions": {},
            "real_trading_enabled": False,
        }
    with _lock():
        trades = _load_trades_unlocked()
        if not include_validation_samples:
            trades = _strategy_trades_only(trades)
        try:
            cash = _starting_cash(starting_cash)
        except LocalSimLedgerCorruption as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "account": account_name,
                "cash_available": None,
                "positions": {},
                "real_trading_enabled": False,
            }
        snapshot = _sim_account_snapshot_unlocked(
            trades,
            account=account_name,
            symbol=symbol,
            trade_date=trade_date,
            starting_cash=cash,
        )
        snapshot.update(
            {
                "status": "ready",
                **_latest_lineage_projection(trades, manifest),
                **_position_source_envelope(
                    snapshot.get("positions"),
                    trade_date=trade_date,
                    position_authority=position_authority,
                    source="server_local_sim_account_snapshot",
                ),
                "real_trading_enabled": False,
            }
        )
        return snapshot


def get_local_sim_exploration_state(
    account: dict[str, Any] | str | None = None,
    *,
    trade_date: str = "",
    starting_cash: Any = ASHARE_SIM_DEFAULT_CASH,
) -> dict[str, Any]:
    """Project conservative exploration limits from the authoritative trade log.

    Partial buys count immediately.  Mixed positions are treated as exploration
    exposure so attribution ambiguity can only reduce, never expand, risk.
    """

    account_name = _require_authoritative_account(account or DEFAULT_ACCOUNT)
    as_of = _trade_date(trade_date)
    with _lock():
        trades = _strategy_trades_only(_load_trades_unlocked())
        account_trades = [
            row
            for row in trades
            if str(row.get("account") or "") == account_name
            and str(row.get("status") or "") in {"filled", "partial"}
        ]
        projection = _replay_account(
            account_trades,
            account_name,
            starting_cash=_starting_cash(starting_cash),
        )

    opened_symbols = {
        str(row.get("ts_code") or "").strip().upper()
        for row in account_trades
        if str(row.get("side") or "").strip().lower() == "buy"
        and str(row.get("sample_intent") or "").strip().lower() == "exploration"
        and _trade_date(row.get("trade_date")) == as_of
        and _safe_float(row.get("quantity"), 0.0) > 0
    }
    open_exposure = round(
        sum(
            _safe_float(position.get("exploration_exposure_cny"), 0.0)
            for position in projection.get("positions", {}).values()
            if isinstance(position, dict)
        ),
        2,
    )
    daily_realized = round(
        sum(
            _safe_float(row.get("realized_pnl_cny"), 0.0)
            for row in account_trades
            if str(row.get("side") or "").strip().lower() == "sell"
            and str(row.get("sample_intent") or "").strip().lower()
            in {"exploration", "mixed"}
            and _trade_date(row.get("trade_date")) == as_of
        ),
        2,
    )
    return {
        "account": account_name,
        "trade_date": as_of,
        "new_position_count": len(opened_symbols),
        "new_position_symbols": sorted(symbol for symbol in opened_symbols if symbol),
        "open_exposure_cny": open_exposure,
        "daily_realized_pnl_cny": daily_realized,
        "daily_loss_cny": round(max(0.0, -daily_realized), 2),
        "real_trading_enabled": False,
    }


def _append_trade_unlocked(trade: LocalSimTrade) -> None:
    if LOCAL_SIM_TRADES.exists() and LOCAL_SIM_TRADES.is_symlink():
        raise LocalSimLedgerCorruption("local_sim_trade_log_symlink_not_allowed")
    payload = asdict(trade)
    payload["real_trading_enabled"] = False
    payload["trade_sha256"] = _payload_sha256(payload, drop_checksums=True)
    trade.trade_sha256 = payload["trade_sha256"]
    if LOCAL_SIM_TRADES.is_symlink() or not LOCAL_SIM_TRADES.is_file():
        raise LocalSimLedgerCorruption("local_sim_trade_log_unavailable")
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(LOCAL_SIM_TRADES, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=True) as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise LocalSimLedgerCorruption("local_sim_trade_append_failed") from exc


def _is_strategy_sample_trade(trade: dict[str, Any]) -> bool:
    """Return whether a trade may consume active A-share strategy capital."""

    try:
        from shared.review.sample_quality import classify_trade_sample
    except Exception:
        return True
    try:
        return bool(classify_trade_sample(trade).get("strategy_sample_valid"))
    except Exception:
        return True


def _strategy_trades_only(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [trade for trade in trades if _is_strategy_sample_trade(trade)]


def _capital_scope(trade: dict[str, Any]) -> str:
    scope = str(trade.get("capital_scope") or "").strip().lower()
    if scope in {"strategy", "validation"}:
        return scope
    return "strategy" if _is_strategy_sample_trade(trade) else "validation"


def _trades_for_capital_scope(
    trades: list[dict[str, Any]], capital_scope: str
) -> list[dict[str, Any]]:
    return [trade for trade in trades if _capital_scope(trade) == capital_scope]


def _reservation_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        reservation_id = str(raw.get("reservation_id") or "").strip()
        remaining = _safe_float(
            raw.get("remaining_cost_basis_cny", raw.get("amount_cny")), 0.0
        )
        if not reservation_id or remaining <= 0:
            continue
        rows.append(
            {
                "reservation_id": reservation_id,
                "event_id": str(raw.get("event_id") or ""),
                "reference_id": str(raw.get("reference_id") or ""),
                "remaining_cost_basis_cny": round(remaining, 2),
                "capital_authority_id": str(
                    raw.get("capital_authority_id") or ASHARE_CAPITAL_AUTHORITY_ID
                ),
                "authority_generation": _safe_int(
                    raw.get("authority_generation"), ASHARE_AUTHORITY_GENERATION
                ),
                "risk_unit_key": str(raw.get("risk_unit_key") or "").strip(),
            }
        )
    return rows


def _allocate_reservation_cost_basis(
    reservations: list[dict[str, Any]], released_cost_basis_cny: float
) -> list[dict[str, Any]]:
    rows = _reservation_rows(reservations)
    target = round(max(0.0, released_cost_basis_cny), 2)
    total = round(
        sum(_safe_float(row.get("remaining_cost_basis_cny"), 0.0) for row in rows),
        2,
    )
    if not rows or target <= 0 or total <= 0:
        return []
    target = min(target, total)
    allocations: list[dict[str, Any]] = []
    allocated = 0.0
    for index, row in enumerate(rows):
        remaining = _safe_float(row.get("remaining_cost_basis_cny"), 0.0)
        if index == len(rows) - 1:
            amount = round(target - allocated, 2)
        else:
            amount = round(target * remaining / total, 2)
        amount = min(max(0.0, amount), remaining)
        allocated = round(allocated + amount, 2)
        if amount <= 0:
            continue
        allocations.append(
            {
                "reservation_id": row["reservation_id"],
                "event_id": row.get("event_id", ""),
                "reference_id": row.get("reference_id", ""),
                "amount_cny": amount,
                "capital_authority_id": row.get(
                    "capital_authority_id", ASHARE_CAPITAL_AUTHORITY_ID
                ),
                "authority_generation": row.get(
                    "authority_generation", ASHARE_AUTHORITY_GENERATION
                ),
                "risk_unit_key": row.get("risk_unit_key", ""),
            }
        )
    return allocations


def _replay_account(
    trades: list[dict[str, Any]],
    account: str | None = None,
    mark_prices: dict[str, float] | None = None,
    starting_cash: float = ASHARE_SIM_DEFAULT_CASH,
) -> dict[str, Any]:
    positions: dict[str, dict[str, Any]] = {}
    realized_pnl = 0.0
    total_trades = 0
    buys = 0
    sells = 0
    cash_available = float(starting_cash)
    for trade in trades:
        if account is not None and str(trade.get("account") or "") != account:
            continue
        if str(trade.get("status") or "") not in {"filled", "partial"}:
            continue
        code = str(trade.get("ts_code") or "").upper()
        if not code:
            continue
        qty = _safe_float(trade.get("quantity"), 0.0)
        net_amount = _safe_float(trade.get("net_amount"), 0.0)
        filled_price = _safe_float(trade.get("filled_price"), 0.0)
        side = str(trade.get("side") or "").lower()
        pos = positions.setdefault(
            code,
            {
                "quantity": 0.0,
                "cost_basis": 0.0,
                "principal_cost_basis": 0.0,
                "entry_fee_cost_basis": 0.0,
                "last_price": 0.0,
                "trades": 0,
                "order_ids": set(),
                "market_reservations": [],
                "exploration_quantity": 0.0,
                "exploration_cost_basis": 0.0,
            },
        )
        total_trades += 1
        if side == "buy":
            principal_amount = _safe_float(trade.get("amount"), 0.0)
            entry_fee_amount = round(
                _safe_float(trade.get("commission"), 0.0)
                + _safe_float(trade.get("stamp_duty"), 0.0)
                + _safe_float(trade.get("transfer_fee"), 0.0),
                6,
            )
            if principal_amount <= 0.0:
                principal_amount = max(0.0, net_amount - entry_fee_amount)
            cash_available -= net_amount
            pos["quantity"] += qty
            pos["cost_basis"] += net_amount
            pos["principal_cost_basis"] += principal_amount
            pos["entry_fee_cost_basis"] += entry_fee_amount
            pos["last_price"] = filled_price or pos["last_price"]
            pos["trades"] += 1
            if str(trade.get("sample_intent") or "").strip().lower() == "exploration":
                pos["exploration_quantity"] += qty
                pos["exploration_cost_basis"] += net_amount
            order_id = str(trade.get("order_id") or "").strip()
            if order_id:
                pos["order_ids"].add(order_id)
            reservation_id = str(
                trade.get("market_capital_reservation_id") or ""
            ).strip()
            retained = _safe_float(trade.get("market_retained_gross_cny"), 0.0)
            if reservation_id and retained > 0:
                pos["market_reservations"].append(
                    {
                        "reservation_id": reservation_id,
                        "event_id": str(trade.get("market_capital_event_id") or ""),
                        "reference_id": str(
                            trade.get("market_capital_reference_id") or ""
                        ),
                        "remaining_cost_basis_cny": round(retained, 2),
                        "capital_authority_id": str(
                            trade.get("capital_authority_id")
                            or ASHARE_CAPITAL_AUTHORITY_ID
                        ),
                        "authority_generation": _safe_int(
                            trade.get("authority_generation"),
                            ASHARE_AUTHORITY_GENERATION,
                        ),
                        "risk_unit_key": str(
                            trade.get("market_capital_risk_unit_key") or code
                        ).strip(),
                    }
                )
            buys += 1
            continue
        if side != "sell" or qty <= 0 or pos["quantity"] <= 0:
            continue
        cash_available += net_amount
        sell_qty = min(qty, pos["quantity"])
        avg_cost = pos["cost_basis"] / pos["quantity"] if pos["quantity"] else 0.0
        released_cost = round(avg_cost * sell_qty, 2)
        principal_before = _safe_float(pos.get("principal_cost_basis"), 0.0)
        entry_fee_before = _safe_float(pos.get("entry_fee_cost_basis"), 0.0)
        released_principal = _safe_float(
            trade.get("released_principal_cost_basis_cny"),
            principal_before * sell_qty / pos["quantity"] if pos["quantity"] else 0.0,
        )
        released_entry_fee = _safe_float(
            trade.get("released_entry_fee_cny"),
            entry_fee_before * sell_qty / pos["quantity"] if pos["quantity"] else 0.0,
        )
        exploration_quantity_before = _safe_float(pos.get("exploration_quantity"), 0.0)
        exploration_cost_before = _safe_float(pos.get("exploration_cost_basis"), 0.0)
        exploration_sell_quantity = (
            sell_qty * exploration_quantity_before / pos["quantity"]
            if pos["quantity"] > 0
            else 0.0
        )
        exploration_released_cost = (
            exploration_cost_before
            * exploration_sell_quantity
            / exploration_quantity_before
            if exploration_quantity_before > 0
            else 0.0
        )
        pos["quantity"] -= sell_qty
        pos["cost_basis"] = round(pos["cost_basis"] - released_cost, 2)
        pos["principal_cost_basis"] = round(
            max(0.0, principal_before - released_principal), 6
        )
        pos["entry_fee_cost_basis"] = round(
            max(0.0, entry_fee_before - released_entry_fee), 6
        )
        pos["exploration_quantity"] = round(
            max(0.0, exploration_quantity_before - exploration_sell_quantity), 8
        )
        pos["exploration_cost_basis"] = round(
            max(0.0, exploration_cost_before - exploration_released_cost), 2
        )
        release_allocations = _reservation_rows(
            [
                {
                    **row,
                    "remaining_cost_basis_cny": row.get("amount_cny"),
                }
                for row in (
                    trade.get("market_release_allocations")
                    if isinstance(trade.get("market_release_allocations"), list)
                    else []
                )
                if isinstance(row, dict)
            ]
        )
        if not release_allocations:
            release_allocations = _allocate_reservation_cost_basis(
                pos.get("market_reservations", []), released_cost
            )
        release_by_id = {
            str(row.get("reservation_id") or ""): _safe_float(
                row.get("remaining_cost_basis_cny"), 0.0
            )
            for row in release_allocations
        }
        remaining_reservations: list[dict[str, Any]] = []
        for reservation in _reservation_rows(pos.get("market_reservations", [])):
            released = release_by_id.get(str(reservation["reservation_id"]), 0.0)
            remaining_amount = round(
                _safe_float(reservation.get("remaining_cost_basis_cny"), 0.0)
                - released,
                2,
            )
            if remaining_amount > 0:
                remaining_reservations.append(
                    {**reservation, "remaining_cost_basis_cny": remaining_amount}
                )
        pos["market_reservations"] = remaining_reservations
        pos["last_price"] = filled_price or pos["last_price"]
        pos["trades"] += 1
        realized_pnl += net_amount - released_cost
        sells += 1
        if pos["quantity"] <= 0:
            pos["quantity"] = 0.0
            pos["cost_basis"] = 0.0
            pos["principal_cost_basis"] = 0.0
            pos["entry_fee_cost_basis"] = 0.0
            pos["exploration_quantity"] = 0.0
            pos["exploration_cost_basis"] = 0.0
    clean_positions: dict[str, dict[str, Any]] = {}
    market_value = 0.0
    unrealized = 0.0
    for code, pos in positions.items():
        qty = float(pos.get("quantity") or 0.0)
        if qty <= 0:
            continue
        cost = round(float(pos.get("cost_basis") or 0.0), 2)
        last_price = round(float(pos.get("last_price") or 0.0), 6)
        mark_price = round(
            float(mark_prices.get(code, last_price)) if mark_prices else last_price, 6
        )
        value = round(qty * mark_price, 2)
        row_unrealized = round(value - cost, 2)
        row = {
            "quantity": int(qty) if abs(qty - round(qty)) < 1e-12 else round(qty, 6),
            "cost_basis": cost,
            "principal_cost_basis": round(
                _safe_float(pos.get("principal_cost_basis"), 0.0), 6
            ),
            "entry_fee_cost_basis": round(
                _safe_float(pos.get("entry_fee_cost_basis"), 0.0), 6
            ),
            "avg_cost": round(cost / qty, 4) if qty else 0.0,
            "last_price": last_price,
            "mark_price": mark_price,
            "market_value": value,
            "unrealized_pnl": row_unrealized,
            "trades": int(pos.get("trades") or 0),
            "market_reservations": _reservation_rows(
                pos.get("market_reservations", [])
            ),
        }
        exploration_quantity = min(
            qty, _safe_float(pos.get("exploration_quantity"), 0.0)
        )
        exploration_exposure = min(
            cost, _safe_float(pos.get("exploration_cost_basis"), 0.0)
        )
        if exploration_quantity <= 1e-9:
            row["sample_intent"] = "exploitation"
        elif abs(exploration_quantity - qty) <= 1e-9:
            row["sample_intent"] = "exploration"
        else:
            row["sample_intent"] = "mixed"
        row["exploration_quantity"] = (
            int(exploration_quantity)
            if abs(exploration_quantity - round(exploration_quantity)) < 1e-12
            else round(exploration_quantity, 6)
        )
        row["exploration_exposure_cny"] = round(exploration_exposure, 2)
        row["market_reserved_cost_basis_cny"] = round(
            sum(
                _safe_float(item.get("remaining_cost_basis_cny"), 0.0)
                for item in row["market_reservations"]
            ),
            2,
        )
        order_ids = pos.get("order_ids") or set()
        if len(order_ids) == 1:
            row["order_id"] = next(iter(order_ids))
        clean_positions[code] = row
        market_value += value
        unrealized += row_unrealized
    return {
        "account": account or "all",
        "total_trades": total_trades,
        "buys": buys,
        "sells": sells,
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "market_value": round(market_value, 2),
        "total_pnl": round(realized_pnl + unrealized, 2),
        "cash_available": round(cash_available, 2),
        "positions": clean_positions,
    }


def _persist_unlocked(trades: list[dict[str, Any]]) -> None:
    accounts = sorted(
        {str(t.get("account") or DEFAULT_ACCOUNT) for t in trades if t.get("account")}
    )
    strategy_trades = _strategy_trades_only(trades)
    lineage = _latest_lineage_projection(trades)
    positions = {
        account: _replay_account(strategy_trades, account)["positions"]
        for account in accounts
    }
    pnl = {
        account: {
            **_replay_account(strategy_trades, account),
            **lineage,
            "real_trading_enabled": False,
        }
        for account in accounts
    }
    audit_positions = {
        account: _replay_account(trades, account)["positions"] for account in accounts
    }
    audit_pnl = {
        account: {
            **_replay_account(trades, account),
            **lineage,
            "real_trading_enabled": False,
        }
        for account in accounts
    }
    _atomic_write_json(LOCAL_SIM_POSITIONS, positions)
    _atomic_write_json(LOCAL_SIM_PNL, pnl)
    _write_positions_snapshot(
        positions,
        pnl,
        audit_positions=audit_positions,
        audit_pnl=audit_pnl,
        lineage_metadata=lineage,
    )


def refresh_local_sim_snapshot(
    mark_prices: dict[str, float] | None = None,
    *,
    local_trades_path: Path | str | None = None,
) -> dict[str, Any]:
    """Rewrite reporting PnL/position snapshots with current mark prices.

    Trade facts remain append-only in local_sim_trades.jsonl. This function is
    for review/evolution jobs that need local_sim_pnl.json to match the same
    mark-to-market price source they use for portfolio evidence.
    """
    authority_error = _snapshot_writer_authority_error()
    if authority_error:
        return {"status": "rejected", "written": False, "reason": authority_error}
    original_local_sim_trades = LOCAL_SIM_TRADES
    if local_trades_path is not None:
        globals()["LOCAL_SIM_TRADES"] = Path(local_trades_path)
    try:
        with _lock():
            trades = _load_trades_unlocked()
            accounts = sorted(
                {
                    str(t.get("account") or DEFAULT_ACCOUNT)
                    for t in trades
                    if t.get("account")
                }
            )
            if not accounts:
                try:
                    existing_snapshot = json.loads(
                        LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    existing_snapshot = {}
                existing_accounts = (
                    existing_snapshot.get("positions_by_account")
                    if isinstance(existing_snapshot, dict)
                    else None
                )
                accounts = (
                    sorted(str(name) for name in existing_accounts)
                    if isinstance(existing_accounts, dict) and existing_accounts
                    else [DEFAULT_ACCOUNT]
                )
            strategy_trades = _strategy_trades_only(trades)
            lineage = _latest_lineage_projection(trades)
            positions = {
                account: _replay_account(
                    strategy_trades, account, mark_prices=mark_prices
                )["positions"]
                for account in accounts
            }
            pnl = {
                account: {
                    **_replay_account(
                        strategy_trades, account, mark_prices=mark_prices
                    ),
                    **lineage,
                    "real_trading_enabled": False,
                }
                for account in accounts
            }
            audit_positions = {
                account: _replay_account(trades, account, mark_prices=mark_prices)[
                    "positions"
                ]
                for account in accounts
            }
            audit_pnl = {
                account: {
                    **_replay_account(trades, account, mark_prices=mark_prices),
                    **lineage,
                    "real_trading_enabled": False,
                }
                for account in accounts
            }
            _atomic_write_json(LOCAL_SIM_POSITIONS, positions)
            _atomic_write_json(LOCAL_SIM_PNL, pnl)
            _write_positions_snapshot(
                positions,
                pnl,
                audit_positions=audit_positions,
                audit_pnl=audit_pnl,
                lineage_metadata=lineage,
            )
    finally:
        if local_trades_path is not None:
            globals()["LOCAL_SIM_TRADES"] = original_local_sim_trades
    return {
        "status": "refreshed",
        "trade_count": len(trades),
        "account_count": len(accounts),
        "mark_price_count": len(mark_prices or {}),
    }


def _write_positions_snapshot(
    positions: dict[str, dict[str, Any]],
    pnl: dict[str, dict[str, Any]],
    *,
    bootstrap: dict[str, Any] | None = None,
    audit_positions: dict[str, dict[str, Any]] | None = None,
    audit_pnl: dict[str, dict[str, Any]] | None = None,
    lineage_metadata: dict[str, Any] | None = None,
    mark_evidence_by_symbol: dict[str, Any] | None = None,
    synced_at: str | None = None,
    trade_date: str | None = None,
) -> None:
    normalized_pnl = {
        account: {**row, "real_trading_enabled": False} for account, row in pnl.items()
    }
    normalized_audit_pnl = {
        account: {**row, "real_trading_enabled": False}
        for account, row in (audit_pnl or normalized_pnl).items()
    }
    flat_positions: list[dict[str, Any]] = []
    for account, account_positions in positions.items():
        for ts_code, position in account_positions.items():
            flat_positions.append(
                {
                    "account": account,
                    "ts_code": ts_code,
                    "quantity": position.get("quantity", 0),
                    "avg_price": position.get("avg_cost", 0.0),
                    "last_price": position.get("last_price", 0.0),
                    "mark_price": position.get(
                        "mark_price", position.get("last_price", 0.0)
                    ),
                    "market_value": position.get("market_value", 0.0),
                    "unrealized_pnl": position.get("unrealized_pnl", 0.0),
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    "source": "server_local_sim_backup",
                    "real_trading_enabled": False,
                }
            )
    effective_synced_at = synced_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    if mark_evidence_by_symbol is None:
        try:
            existing_snapshot = json.loads(
                LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            existing_snapshot = {}
        existing_marks = (
            existing_snapshot.get("mark_evidence_by_symbol")
            if isinstance(existing_snapshot, dict)
            else None
        )
        mark_evidence_by_symbol = (
            dict(existing_marks) if isinstance(existing_marks, dict) else {}
        )
    payload = {
        "snapshot_id": "simulated_ashare_positions",
        "market": "ashare",
        "account_type": "simulated",
        "capital_layer": "simulated",
        "source": "server_local_sim_backup",
        "synced_at": effective_synced_at,
        "trade_date": trade_date or _trade_date(effective_synced_at),
        "positions": flat_positions,
        "positions_by_account": positions,
        "pnl": normalized_pnl,
        "account_view": "strategy_samples_only",
        "audit_positions_by_account": audit_positions or positions,
        "audit_pnl": normalized_audit_pnl,
        "mark_evidence_by_symbol": mark_evidence_by_symbol,
        "real_trading_enabled": False,
    }
    manifest = _read_fresh_lineage_manifest()
    if manifest is None:
        raise LocalSimLedgerCorruption("execution_lineage_unavailable")
    payload.update(lineage_metadata or _latest_lineage_projection([], manifest))
    if bootstrap:
        payload.update(bootstrap)
    payload["real_trading_enabled"] = False
    _atomic_write_json(LOCAL_SIM_POSITIONS_SNAPSHOT, payload)


def ensure_local_sim_bootstrap_snapshot(
    account: dict[str, Any] | str | None = None,
    *,
    starting_cash: Any = None,
    trade_date: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Create an empty A-share simulated snapshot before the first local fill."""

    authority_error = _snapshot_writer_authority_error()
    if authority_error:
        return {"status": "rejected", "written": False, "reason": authority_error}

    try:
        account_name = _require_authoritative_account(account or DEFAULT_ACCOUNT)
    except LocalSimLedgerCorruption as exc:
        return {"status": "rejected", "written": False, "reason": str(exc)}
    try:
        cash = _starting_cash_for_bootstrap(starting_cash)
    except LocalSimLedgerCorruption as exc:
        return {"status": "rejected", "written": False, "reason": str(exc)}
    with _lock():
        trades = _load_trades_unlocked()
        if trades:
            required_snapshots = (
                LOCAL_SIM_POSITIONS,
                LOCAL_SIM_PNL,
                LOCAL_SIM_POSITIONS_SNAPSHOT,
            )
            if not all(path.exists() for path in required_snapshots):
                _persist_unlocked(trades)
            return {
                "status": "existing_trades",
                "written": False,
                "trade_count": len(trades),
                "account": account_name,
            }
        if LOCAL_SIM_POSITIONS_SNAPSHOT.exists() and not force:
            try:
                existing = json.loads(
                    LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
                )
            except Exception:
                existing = {}
            existing_cash = (
                _safe_float(existing.get("cash_available"), -1.0)
                if isinstance(existing, dict)
                else -1.0
            )
            existing_bootstrap = (
                str(existing.get("bootstrap_state") or "")
                if isinstance(existing, dict)
                else ""
            )
            if (
                existing_bootstrap != "no_trades_yet"
                or abs(existing_cash - cash) < 0.01
            ):
                return {
                    "status": "snapshot_exists",
                    "written": False,
                    "trade_count": 0,
                    "account": account_name,
                }

        positions = {account_name: {}}
        pnl = {
            account_name: {
                "account": account_name,
                "total_trades": 0,
                "buys": 0,
                "sells": 0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "market_value": 0.0,
                "total_pnl": 0.0,
                "cash_available": round(cash, 2),
                "positions": {},
            }
        }
        _atomic_write_json(LOCAL_SIM_POSITIONS, positions)
        _atomic_write_json(LOCAL_SIM_PNL, pnl)
        _write_positions_snapshot(
            positions,
            pnl,
            bootstrap={
                "bootstrap_state": "no_trades_yet",
                "cash_available": round(cash, 2),
                "trade_date": _trade_date(trade_date),
            },
        )
    return {
        "status": "bootstrapped",
        "written": True,
        "trade_count": 0,
        "account": account_name,
        "cash_available": round(cash, 2),
    }


def record_local_sim_order(
    order: dict[str, Any],
    market: str,
    account: dict[str, Any] | str | None = None,
    config: dict[str, Any] | None = None,
    receipt: Any | None = None,
) -> dict[str, Any]:
    market_key = str(market or "").lower().strip()
    if market_key != "ashare":
        return {
            "status": "skipped",
            "recorded": False,
            "reason": f"unsupported market={market_key}",
        }
    config = dict(config or {})
    from shared.markets.safety import reject_real_execution_payload

    try:
        reject_real_execution_payload(
            order,
            context="record_local_sim_order.order",
        )
        reject_real_execution_payload(
            account if isinstance(account, dict) else {},
            context="record_local_sim_order.account",
        )
        reject_real_execution_payload(
            config,
            context="record_local_sim_order.config",
        )
    except RuntimeError as exc:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": str(exc),
        }
    code = str(order.get("ts_code") or order.get("symbol") or "").strip().upper()
    if not _is_regular_ashare_symbol(code):
        return {
            "status": "rejected",
            "recorded": False,
            "reason": f"unsupported or non-A-share code: {code}",
        }
    side = str(order.get("side") or order.get("direction") or "buy").lower().strip()
    if side not in {"buy", "sell"}:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": f"invalid side: {side}",
        }
    quantity = _safe_int(
        order.get("quantity") or order.get("qty") or order.get("filled_qty"), 0
    )
    requested_price = _safe_float(
        order.get("price") or order.get("limit_price") or order.get("mid_price"), 0.0
    )
    if quantity <= 0 or requested_price <= 0:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": "non-positive quantity or price",
        }
    if quantity % 100 != 0:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": "ashare_lot_size_invalid",
            "requested_quantity": quantity,
        }
    order_id = str(
        order.get("order_id")
        or f"LSIM-ASHARE-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    idempotency_key = str(order.get("idempotency_key") or order_id)
    try:
        account_name = _require_authoritative_account(
            account or order.get("account") or DEFAULT_ACCOUNT
        )
    except LocalSimLedgerCorruption as exc:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": str(exc),
        }
    linked_status = str(
        getattr(receipt, "status", "")
        or (receipt.get("status") if isinstance(receipt, dict) else "")
        or ""
    )
    linked_filled_qty = _safe_int(
        getattr(receipt, "filled_qty", None)
        or (receipt.get("filled_qty") if isinstance(receipt, dict) else None),
        0,
    )
    if linked_status in {"filled", "partial"}:
        if linked_status == "filled" and linked_filled_qty <= 0:
            linked_filled_qty = quantity
        if linked_filled_qty <= 0:
            return {
                "status": "rejected",
                "recorded": False,
                "reason": "filled receipt has non-positive filled quantity",
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "account": account_name,
            }
        quantity = min(quantity, linked_filled_qty)
        if quantity % 100 != 0:
            return {
                "status": "rejected",
                "recorded": False,
                "reason": "ashare_lot_size_invalid",
                "filled_quantity": quantity,
                "order_id": order_id,
                "idempotency_key": idempotency_key,
            }
    linked_avg_price = _safe_float(
        getattr(receipt, "avg_price", None)
        or (receipt.get("avg_price") if isinstance(receipt, dict) else None),
        0.0,
    )
    raw_response = getattr(receipt, "raw_response", None)
    if raw_response is None and isinstance(receipt, dict):
        raw_response = receipt.get("raw_response")
    if not isinstance(raw_response, dict):
        raw_response = {}
    partial_terminal = bool(
        getattr(receipt, "partial_terminal", False)
        or (receipt.get("partial_terminal") if isinstance(receipt, dict) else False)
        or raw_response.get("partial_terminal")
        or raw_response.get("residual_cancelled")
    )
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    research_hypothesis = (
        order.get("research_hypothesis")
        if isinstance(order.get("research_hypothesis"), dict)
        else {}
    )
    factor_snapshot = (
        order.get("factor_snapshot")
        if isinstance(order.get("factor_snapshot"), dict)
        else research_hypothesis.get("factor_snapshot")
    )
    if not isinstance(factor_snapshot, dict):
        factor_snapshot = {}
    evidence_candidates = [
        candidate
        for candidate in (
            raw_response.get("fill_evidence"),
            order.get("fill_evidence"),
            metadata.get("fill_evidence"),
        )
        if isinstance(candidate, dict)
    ]
    fill_evidence = next(
        (
            candidate
            for candidate in evidence_candidates
            if _verified_ashare_execution_evidence(candidate)
        ),
        evidence_candidates[0] if evidence_candidates else {},
    )
    if not isinstance(fill_evidence, dict):
        fill_evidence = {}
    fill_price_source = str(
        order.get("fill_price_source")
        or metadata.get("fill_price_source")
        or fill_evidence.get("fill_price_source")
        or ""
    )
    fill_price_source_class = str(
        order.get("fill_price_source_class")
        or metadata.get("fill_price_source_class")
        or fill_evidence.get("fill_price_source_class")
        or ""
    )
    if "local_sim_slippage_bps" in config:
        slippage_bps = _safe_float(config.get("local_sim_slippage_bps"), 5.0)
    else:
        slippage_bps = _safe_float(os.environ.get("ASHARE_LOCAL_SIM_SLIPPAGE_BPS"), 5.0)
    if linked_avg_price > 0:
        filled_price = linked_avg_price
        if requested_price > 0:
            direction = 1.0 if side == "buy" else -1.0
            slippage_bps = round(
                ((filled_price / requested_price) - 1.0) * 10000.0 * direction, 6
            )
    else:
        filled_price = (
            requested_price * (1.0 + slippage_bps / 10000.0)
            if side == "buy"
            else requested_price * (1.0 - slippage_bps / 10000.0)
        )
    filled_price = round(filled_price, 4)
    amount = round(quantity * filled_price, 2)
    execution_reality = ashare_execution_reality()
    fee_breakdown = execution_reality.calculate_fees(side, amount)
    commission = round(float(fee_breakdown["commission"]), 2)
    stamp_duty = round(float(fee_breakdown["stamp_duty"]), 2)
    transfer_fee = round(float(fee_breakdown["transfer_fee"]), 2)
    total_fee = round(commission + stamp_duty + transfer_fee, 2)
    net_amount = (
        round(amount + total_fee, 2) if side == "buy" else round(amount - total_fee, 2)
    )
    candidate_pool_layer = str(
        order.get("candidate_pool_layer") or metadata.get("candidate_pool_layer") or ""
    )
    execution_source = str(
        order.get("execution_source") or metadata.get("execution_source") or ""
    )
    sample_intent = (
        str(order.get("sample_intent") or metadata.get("sample_intent") or "")
        .strip()
        .lower()
    )
    if side == "buy" and not sample_intent and candidate_pool_layer == "candidate":
        sample_intent = "exploitation"
    primary_style = str(
        order.get("primary_style") or metadata.get("primary_style") or ""
    ).strip()
    if linked_status and linked_status not in {"filled", "partial"}:
        return {
            "status": linked_status,
            "recorded": False,
            "reason": "server-local A-share ledger records filled/partial receipts only",
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    provenance_error = _ashare_provenance_error(
        side,
        candidate_pool_layer,
        execution_source,
        sample_intent,
    )
    if provenance_error:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": provenance_error,
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        manifest = _read_fresh_lineage_manifest()
        if manifest is None:
            raise ExecutionLineageError("execution_lineage_unavailable")
        lineage_metadata = require_execution_lineage(order)
        for field in (
            "capital_authority_id",
            "authority_generation",
            "execution_lineage_id",
            "lineage_started_at",
        ):
            if lineage_metadata[field] != manifest[field]:
                raise ExecutionLineageError(f"{field}_manifest_mismatch")
    except (ExecutionLineageError, LocalSimLedgerCorruption) as exc:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": str(exc),
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    capital_cny = ASHARE_SIM_DEFAULT_CASH
    explicit_capital_scope = str(order.get("capital_scope") or "").strip().lower()
    requested_capital_scope = (
        explicit_capital_scope
        if explicit_capital_scope in {"strategy", "validation"}
        else _capital_scope(
            {
                **order,
                "market": market_key,
                "capital_layer": "simulated",
                "account_type": "simulated",
            }
        )
    )
    market_capital_required = order.get("market_capital_required") is True
    market_capital_reference_id = str(
        order.get("market_capital_reference_id") or ""
    ).strip()
    market_capital_reservation_id = str(
        order.get("market_capital_reservation_id") or ""
    ).strip()
    market_capital_event_id = str(order.get("market_capital_event_id") or "").strip()
    market_capital_expected_head_event_id = str(
        order.get("market_capital_expected_head_event_id") or ""
    ).strip()
    market_capital_expected_head_checksum = str(
        order.get("market_capital_expected_head_checksum") or ""
    ).strip()
    market_capital_risk_unit_key = (
        str(
            order.get("market_capital_risk_unit_key")
            or order.get("risk_unit_key")
            or code
        )
        .strip()
        .upper()
    )
    if market_capital_risk_unit_key != code:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": "market_capital_risk_unit_mismatch",
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    market_reserved_gross_cny = _safe_float(order.get("market_reserved_gross_cny"), 0.0)
    session_metadata = _ashare_session_metadata(market_key, code, created_at)
    if (
        market_key == "ashare"
        and requested_capital_scope == "strategy"
        and session_metadata.get("ashare_session_valid") is not True
    ):
        return {
            "status": "rejected",
            "recorded": False,
            "reason": "outside_ashare_regular_session",
            "session_rejection": str(
                session_metadata.get("ashare_session_rejection") or ""
            ),
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
            "execution_eligible": False,
            "real_trading_enabled": False,
        }
    execution_trade_date = _order_trade_date(order, idempotency_key)
    lineage_started = datetime.fromisoformat(lineage_metadata["lineage_started_at"])
    point_in_time = datetime.fromisoformat(lineage_metadata["point_in_time_as_of"])
    lineage_start_date = lineage_started.astimezone(CN_TZ).date().isoformat()
    point_in_time_date = point_in_time.astimezone(CN_TZ).date().isoformat()
    if execution_trade_date < lineage_start_date:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": "trade_date_before_execution_lineage",
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    if execution_trade_date > point_in_time_date:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": "trade_date_after_point_in_time",
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    evidence_timestamp = _parse_timestamp(fill_evidence.get("bar_time"))
    if (
        _verified_ashare_execution_evidence(fill_evidence, fill_price_source_class)
        and evidence_timestamp is not None
        and evidence_timestamp > point_in_time.astimezone(CN_TZ)
    ):
        return {
            "status": "rejected",
            "recorded": False,
            "reason": "execution_evidence_after_point_in_time",
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    trade = LocalSimTrade(
        order_id=order_id,
        idempotency_key=idempotency_key,
        market=market_key,
        account=account_name,
        trade_date=execution_trade_date,
        ts_code=code,
        side=side,
        quantity=quantity,
        requested_price=round(requested_price, 6),
        filled_price=filled_price,
        slippage_bps=slippage_bps,
        amount=amount,
        commission=commission,
        stamp_duty=stamp_duty,
        transfer_fee=transfer_fee,
        total_fee=total_fee,
        execution_reality_model_version=execution_reality.model_version,
        commission_schedule_status=execution_reality.commission_schedule_status,
        commission_schedule_version=execution_reality.commission_schedule_version,
        net_amount=net_amount,
        candidate_pool_layer=candidate_pool_layer,
        execution_source=execution_source,
        sample_intent=sample_intent,
        sample_layer=(
            "chain_validation"
            if requested_capital_scope == "validation"
            else "execution_pending_capital_commit"
        ),
        execution_eligible=False,
        primary_style=primary_style,
        fill_price_source=fill_price_source,
        fill_price_source_class=fill_price_source_class,
        fill_evidence=fill_evidence,
        retry_of=str(order.get("retry_of") or ""),
        retry_attempt=_safe_int(order.get("retry_attempt"), 0),
        hypothesis_id=str(
            order.get("hypothesis_id") or research_hypothesis.get("hypothesis_id") or ""
        ),
        research_hypothesis=research_hypothesis,
        factor_snapshot=factor_snapshot,
        capital_authority_id=lineage_metadata["capital_authority_id"],
        authority_generation=lineage_metadata["authority_generation"],
        execution_lineage_id=lineage_metadata["execution_lineage_id"],
        lineage_started_at=lineage_metadata["lineage_started_at"],
        point_in_time_as_of=lineage_metadata["point_in_time_as_of"],
        execution_lineage_sha256=lineage_metadata["execution_lineage_sha256"],
        capital_cny=capital_cny,
        market_capital_required=market_capital_required,
        market_capital_reference_id=market_capital_reference_id,
        market_capital_reservation_id=market_capital_reservation_id,
        market_capital_event_id=market_capital_event_id,
        market_capital_risk_unit_key=market_capital_risk_unit_key,
        market_capital_expected_head_event_id=market_capital_expected_head_event_id,
        market_capital_expected_head_checksum=market_capital_expected_head_checksum,
        market_capital_fill_sequence=max(
            1,
            _safe_int(order.get("market_capital_fill_sequence"), 1),
        ),
        market_capital_source_sha256=_payload_sha256(fill_evidence),
        market_capital_receipt_sha256=_payload_sha256(
            {
                "status": linked_status or "filled",
                "filled_quantity": quantity,
                "filled_price": filled_price,
                "partial_terminal": partial_terminal,
                "raw_response": raw_response,
            }
        ),
        market_reserved_gross_cny=round(market_reserved_gross_cny, 2),
        partial_terminal=partial_terminal,
        created_at=created_at,
        trade_timestamp_bj=str(session_metadata["trade_timestamp_bj"]),
        ashare_session_valid=bool(session_metadata["ashare_session_valid"]),
        ashare_session_rejection=str(session_metadata["ashare_session_rejection"]),
        linked_execution_status=linked_status,
        status=linked_status or "filled",
        note=str(
            order.get("note") or "server backup fill for A-share simulated signal"
        ),
    )
    trade.capital_scope = (
        explicit_capital_scope
        if explicit_capital_scope in {"strategy", "validation"}
        else _capital_scope(asdict(trade))
    )
    with _lock():
        trades = _load_trades_unlocked()
        for existing in trades:
            if str(existing.get("idempotency_key") or "") == idempotency_key:
                return {
                    "status": "duplicate",
                    "recorded": False,
                    "trade_id": existing.get("trade_id", ""),
                    "idempotency_key": idempotency_key,
                    "account": account_name,
                    "capital_scope": existing.get("capital_scope", ""),
                    "capital_authority_id": existing.get("capital_authority_id", ""),
                    "authority_generation": existing.get("authority_generation", 0),
                    "execution_lineage_id": existing.get("execution_lineage_id", ""),
                    "lineage_started_at": existing.get("lineage_started_at", ""),
                    "point_in_time_as_of": existing.get("point_in_time_as_of", ""),
                    "sample_intent": existing.get("sample_intent", ""),
                    "sample_layer": existing.get("sample_layer", ""),
                    "execution_eligible": existing.get("execution_eligible", False),
                    "primary_style": existing.get("primary_style", ""),
                    "real_trading_enabled": False,
                    "filled_qty": existing.get("quantity", 0),
                    "avg_price": existing.get("filled_price", 0.0),
                    "fill_price_source": existing.get("fill_price_source", ""),
                    "fill_price_source_class": existing.get(
                        "fill_price_source_class", ""
                    ),
                    "fill_evidence": existing.get("fill_evidence", {}),
                    "net_amount": existing.get("net_amount", 0.0),
                    "market_capital_required": existing.get(
                        "market_capital_required", False
                    ),
                    "market_capital_reference_id": existing.get(
                        "market_capital_reference_id", ""
                    ),
                    "market_capital_reservation_id": existing.get(
                        "market_capital_reservation_id", ""
                    ),
                    "market_capital_event_id": existing.get(
                        "market_capital_event_id", ""
                    ),
                    "market_capital_risk_unit_key": existing.get(
                        "market_capital_risk_unit_key", ""
                    ),
                    "market_reserved_gross_cny": existing.get(
                        "market_reserved_gross_cny", 0.0
                    ),
                    "market_retained_gross_cny": existing.get(
                        "market_retained_gross_cny", 0.0
                    ),
                    "market_release_allocations": existing.get(
                        "market_release_allocations", []
                    ),
                    "released_principal_cost_basis_cny": existing.get(
                        "released_principal_cost_basis_cny", 0.0
                    ),
                    "released_entry_fee_cny": existing.get(
                        "released_entry_fee_cny", 0.0
                    ),
                    "gross_realized_pnl_cny": existing.get(
                        "gross_realized_pnl_cny", 0.0
                    ),
                    "realized_pnl_cny": existing.get("realized_pnl_cny", 0.0),
                    "partial_terminal": existing.get("partial_terminal", False),
                }
        latest_point_in_time = datetime.fromisoformat(
            _latest_lineage_projection(trades, manifest)["point_in_time_as_of"]
        )
        if datetime.fromisoformat(trade.point_in_time_as_of) < latest_point_in_time:
            return {
                "status": "rejected",
                "recorded": False,
                "reason": "point_in_time_regression",
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "account": account_name,
            }
        try:
            starting_cash = _starting_cash(
                config.get("starting_cash")
                or config.get("initial_capital")
                or (
                    account.get("initial_capital")
                    if isinstance(account, dict)
                    else None
                )
                or (account.get("sim_capital") if isinstance(account, dict) else None)
                or ASHARE_SIM_DEFAULT_CASH
            )
        except LocalSimLedgerCorruption as exc:
            return {
                "status": "rejected",
                "recorded": False,
                "reason": str(exc),
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "account": account_name,
            }
        # Validation fills are independently capitalized chain checks. Replay the
        # same logical scope as this order so they cannot consume strategy cash.
        scoped_trades = _trades_for_capital_scope(trades, trade.capital_scope)
        capital_account = _replay_account(
            scoped_trades,
            account_name,
            starting_cash=starting_cash,
        )
        if side == "sell" and explicit_capital_scope not in {"strategy", "validation"}:
            matching_scopes: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
            for candidate_scope in ("strategy", "validation"):
                candidate_trades = _trades_for_capital_scope(trades, candidate_scope)
                candidate_account = _replay_account(
                    candidate_trades,
                    account_name,
                    starting_cash=starting_cash,
                )
                candidate_position = candidate_account.get("positions", {}).get(
                    code, {}
                )
                if _safe_int(candidate_position.get("quantity"), 0) >= quantity:
                    matching_scopes.append(
                        (candidate_scope, candidate_trades, candidate_account)
                    )
            if len(matching_scopes) != 1:
                return {
                    "status": "rejected",
                    "recorded": False,
                    "reason": (
                        "capital_scope_ambiguous"
                        if len(matching_scopes) > 1
                        else "local_simulated_position_unavailable"
                    ),
                    "order_id": order_id,
                    "idempotency_key": idempotency_key,
                    "account": account_name,
                }
            trade.capital_scope, scoped_trades, capital_account = matching_scopes[0]
        if side == "buy":
            cash_available = _safe_float(capital_account.get("cash_available"), 0.0)
            if cash_available + 1e-9 < net_amount:
                return {
                    "status": "rejected",
                    "recorded": False,
                    "reason": "insufficient_cash",
                    "order_id": order_id,
                    "idempotency_key": idempotency_key,
                    "account": account_name,
                    "cash_available": round(cash_available, 2),
                    "required_cash": round(net_amount, 2),
                }
            strategy_market_capital_required = explicit_capital_scope != "validation"
            if strategy_market_capital_required:
                if (
                    not market_capital_required
                    or trade.capital_scope != "strategy"
                    or not market_capital_reference_id
                    or not market_capital_reservation_id
                    or not market_capital_event_id
                    or not market_capital_expected_head_event_id
                    or not re.fullmatch(
                        r"[a-f0-9]{64}",
                        market_capital_expected_head_checksum,
                    )
                ):
                    return {
                        "status": "rejected",
                        "recorded": False,
                        "reason": "market_capital_lineage_missing",
                        "order_id": order_id,
                        "idempotency_key": idempotency_key,
                        "account": account_name,
                    }
                if market_reserved_gross_cny + 1e-9 < net_amount:
                    return {
                        "status": "rejected",
                        "recorded": False,
                        "reason": "market_reservation_underfunded",
                        "order_id": order_id,
                        "idempotency_key": idempotency_key,
                        "account": account_name,
                        "market_reserved_gross_cny": round(
                            market_reserved_gross_cny, 2
                        ),
                        "required_cash": round(net_amount, 2),
                    }
                import shared.capital as market_capital

                verification = market_capital.verify_market_capital_reservation(
                    "ashare",
                    reservation_id=market_capital_reservation_id,
                    reference_id=market_capital_reference_id,
                    authority_id=trade.capital_authority_id,
                    authority_generation=trade.authority_generation,
                    execution_lineage_id=trade.execution_lineage_id,
                    risk_unit_key=trade.market_capital_risk_unit_key,
                    expected_event_id=market_capital_event_id,
                    retained_amount_cny=net_amount,
                )
                if verification.get("verified") is not True:
                    return {
                        "status": "rejected",
                        "recorded": False,
                        "reason": "market_reservation_verification_failed",
                        "verification_reason": str(
                            verification.get("reason") or "unknown"
                        ),
                        "order_id": order_id,
                        "idempotency_key": idempotency_key,
                        "account": account_name,
                    }
                expected_reservation_lineage = {
                    "reservation_id": market_capital_reservation_id,
                    "reference_id": market_capital_reference_id,
                    "market": market_key,
                    "authority_id": trade.capital_authority_id,
                    "authority_generation": trade.authority_generation,
                    "execution_lineage_id": trade.execution_lineage_id,
                    "risk_unit_key": trade.market_capital_risk_unit_key,
                }
                if any(
                    verification.get(field) != expected
                    for field, expected in expected_reservation_lineage.items()
                ):
                    return {
                        "status": "rejected",
                        "recorded": False,
                        "reason": "market_reservation_lineage_mismatch",
                        "order_id": order_id,
                        "idempotency_key": idempotency_key,
                        "account": account_name,
                    }
                if str(verification.get("event_id") or "") != market_capital_event_id:
                    return {
                        "status": "rejected",
                        "recorded": False,
                        "reason": "market_reservation_event_mismatch",
                        "order_id": order_id,
                        "idempotency_key": idempotency_key,
                        "account": account_name,
                    }
                trade.market_retained_gross_cny = 0.0
        if (
            trade.capital_scope == "strategy"
            and side != "sell"
            and not _verified_ashare_execution_evidence(
                fill_evidence,
                fill_price_source_class,
            )
        ):
            return {
                "status": "rejected",
                "recorded": False,
                "reason": "execution_evidence_unverified",
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "account": account_name,
            }
        if side == "sell":
            if trade.capital_scope == "strategy" and (
                not market_capital_required
                or not market_capital_expected_head_event_id
                or not re.fullmatch(
                    r"[a-f0-9]{64}",
                    market_capital_expected_head_checksum,
                )
            ):
                return {
                    "status": "rejected",
                    "recorded": False,
                    "reason": "market_capital_sell_lineage_missing",
                    "order_id": order_id,
                    "idempotency_key": idempotency_key,
                    "account": account_name,
                }
            current = capital_account["positions"].get(code, {})
            if quantity > _safe_int(current.get("quantity"), 0):
                return {
                    "status": "rejected",
                    "recorded": False,
                    "reason": f"sell quantity {quantity} exceeds local simulated position {current.get('quantity', 0)} for {code}",
                    "account": account_name,
                }
            sellable_snapshot = _sim_account_snapshot_unlocked(
                scoped_trades,
                account=account_name,
                symbol=code,
                trade_date=trade.trade_date,
                starting_cash=starting_cash,
            )
            sellable_quantity = _safe_int(sellable_snapshot.get("sellable_qty"), 0)
            if quantity > sellable_quantity:
                return {
                    "status": "rejected",
                    "recorded": False,
                    "reason": "t_plus_one_sell_quantity_unavailable",
                    "order_id": order_id,
                    "idempotency_key": idempotency_key,
                    "account": account_name,
                    "requested_quantity": quantity,
                    "sellable_quantity": sellable_quantity,
                }
            if (
                trade.capital_scope == "strategy"
                and not _verified_ashare_execution_evidence(
                    fill_evidence,
                    fill_price_source_class,
                )
            ):
                return {
                    "status": "rejected",
                    "recorded": False,
                    "reason": "execution_evidence_unverified",
                    "order_id": order_id,
                    "idempotency_key": idempotency_key,
                    "account": account_name,
                }
            current_intent = str(current.get("sample_intent") or "").strip().lower()
            if _safe_float(current.get("exploration_exposure_cny"), 0.0) > 0:
                trade.sample_intent = (
                    "exploration" if current_intent == "exploration" else "mixed"
                )
            elif not trade.sample_intent:
                trade.sample_intent = "exploitation"
            current_quantity = _safe_float(current.get("quantity"), 0.0)
            current_principal = _safe_float(current.get("principal_cost_basis"), 0.0)
            current_entry_fee = _safe_float(current.get("entry_fee_cost_basis"), 0.0)
            released_principal = round(
                (current_principal / current_quantity) * quantity
                if current_quantity > 0
                else 0.0,
                6,
            )
            released_entry_fee = round(
                (current_entry_fee / current_quantity) * quantity
                if current_quantity > 0
                else 0.0,
                6,
            )
            trade.market_release_allocations = []
            trade.released_principal_cost_basis_cny = released_principal
            trade.released_entry_fee_cny = released_entry_fee
            trade.gross_realized_pnl_cny = round(
                amount - released_principal,
                6,
            )
            trade.realized_pnl_cny = round(
                trade.gross_realized_pnl_cny
                - released_entry_fee
                - commission
                - stamp_duty
                - transfer_fee,
                2,
            )
        _append_trade_unlocked(trade)
        trades.append(asdict(trade))
        _reconcile_market_capital_outbox_unlocked(trades)
        _persist_unlocked(trades)
        _append_receipt_unlocked(
            _build_signed_receipt(
                order=order,
                trade=trade,
                market=market_key,
                account=account_name,
                status=trade.status,
                extra={
                    "candidate_pool_layer": candidate_pool_layer,
                    "execution_source": execution_source,
                    "sample_intent": trade.sample_intent,
                    "sample_layer": trade.sample_layer,
                    "execution_eligible": trade.execution_eligible,
                    "primary_style": trade.primary_style,
                    "real_trading_enabled": False,
                    "fill_price_source": fill_price_source,
                    "fill_price_source_class": fill_price_source_class,
                    "fill_evidence": fill_evidence,
                    "capital_scope": trade.capital_scope,
                    "capital_authority_id": trade.capital_authority_id,
                    "authority_generation": trade.authority_generation,
                    "execution_lineage_id": trade.execution_lineage_id,
                    "lineage_started_at": trade.lineage_started_at,
                    "point_in_time_as_of": trade.point_in_time_as_of,
                    "execution_lineage_sha256": trade.execution_lineage_sha256,
                    "capital_cny": trade.capital_cny,
                    "market_capital_required": trade.market_capital_required,
                    "market_capital_reference_id": trade.market_capital_reference_id,
                    "market_capital_reservation_id": trade.market_capital_reservation_id,
                    "market_capital_event_id": trade.market_capital_event_id,
                    "market_capital_risk_unit_key": trade.market_capital_risk_unit_key,
                    "market_reserved_gross_cny": trade.market_reserved_gross_cny,
                    "market_retained_gross_cny": trade.market_retained_gross_cny,
                    "market_release_allocations": trade.market_release_allocations,
                    "released_principal_cost_basis_cny": (
                        trade.released_principal_cost_basis_cny
                    ),
                    "released_entry_fee_cny": trade.released_entry_fee_cny,
                    "gross_realized_pnl_cny": trade.gross_realized_pnl_cny,
                    "realized_pnl_cny": trade.realized_pnl_cny,
                    "partial_terminal": trade.partial_terminal,
                    "retry_of": trade.retry_of,
                    "retry_attempt": trade.retry_attempt,
                    "hypothesis_id": trade.hypothesis_id,
                    "research_hypothesis": research_hypothesis,
                },
            )
        )
    return {
        "status": trade.status,
        "recorded": True,
        "trade_id": trade.trade_id,
        "order_id": order_id,
        "idempotency_key": idempotency_key,
        "account": account_name,
        "filled_qty": quantity,
        "avg_price": filled_price,
        "fill_price_source": fill_price_source,
        "fill_price_source_class": fill_price_source_class,
        "fill_evidence": fill_evidence,
        "amount": amount,
        "commission": commission,
        "stamp_duty": stamp_duty,
        "transfer_fee": transfer_fee,
        "total_fee": total_fee,
        "execution_reality_model_version": trade.execution_reality_model_version,
        "commission_schedule_status": trade.commission_schedule_status,
        "commission_schedule_version": trade.commission_schedule_version,
        "slippage_bps": slippage_bps,
        "net_amount": net_amount,
        "created_at": trade.created_at,
        "trade_sha256": trade.trade_sha256,
        "capital_authority_id": trade.capital_authority_id,
        "authority_generation": trade.authority_generation,
        "execution_lineage_id": trade.execution_lineage_id,
        "lineage_started_at": trade.lineage_started_at,
        "point_in_time_as_of": trade.point_in_time_as_of,
        "execution_lineage_sha256": trade.execution_lineage_sha256,
        "capital_cny": trade.capital_cny,
        "capital_scope": trade.capital_scope,
        "sample_intent": trade.sample_intent,
        "sample_layer": trade.sample_layer,
        "execution_eligible": trade.execution_eligible,
        "primary_style": trade.primary_style,
        "real_trading_enabled": False,
        "market_capital_required": trade.market_capital_required,
        "market_capital_reference_id": trade.market_capital_reference_id,
        "market_capital_reservation_id": trade.market_capital_reservation_id,
        "market_capital_event_id": trade.market_capital_event_id,
        "market_capital_risk_unit_key": trade.market_capital_risk_unit_key,
        "market_capital_expected_head_event_id": (
            trade.market_capital_expected_head_event_id
        ),
        "market_capital_expected_head_checksum": (
            trade.market_capital_expected_head_checksum
        ),
        "market_capital_source_sha256": trade.market_capital_source_sha256,
        "market_capital_receipt_sha256": trade.market_capital_receipt_sha256,
        "market_reserved_gross_cny": trade.market_reserved_gross_cny,
        "market_retained_gross_cny": trade.market_retained_gross_cny,
        "market_release_allocations": trade.market_release_allocations,
        "released_principal_cost_basis_cny": (trade.released_principal_cost_basis_cny),
        "released_entry_fee_cny": trade.released_entry_fee_cny,
        "gross_realized_pnl_cny": trade.gross_realized_pnl_cny,
        "realized_pnl_cny": trade.realized_pnl_cny,
        "partial_terminal": trade.partial_terminal,
        "ledger": "server_local_sim_backup",
        "receipt_path": str(LOCAL_SIM_RECEIPTS),
    }


def get_local_sim_trade_by_idempotency(
    idempotency_key: str,
    *,
    account: str | None = None,
) -> dict[str, Any] | None:
    """Return the immutable local fill fact for crash-safe orchestration recovery."""

    identity = str(idempotency_key or "").strip()
    if not identity:
        return None
    with _lock():
        for trade in reversed(_load_trades_unlocked()):
            if str(trade.get("idempotency_key") or "") != identity:
                continue
            if account is not None and str(trade.get("account") or "") != str(account):
                continue
            return dict(trade)
    return None


def _project_market_capital_actions(
    trades: list[dict[str, Any]],
    *,
    account: str | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for trade in trades:
        if account is not None and str(trade.get("account") or "") != str(account):
            continue
        if _capital_scope(trade) != "strategy":
            continue
        if (
            trade.get("capital_authority_id") != ASHARE_CAPITAL_AUTHORITY_ID
            or trade.get("authority_generation") != ASHARE_AUTHORITY_GENERATION
            or trade.get("execution_lineage_id") != ASHARE_EXECUTION_LINEAGE_ID
        ):
            continue
        idempotency_key = str(trade.get("idempotency_key") or "").strip()
        if not idempotency_key:
            continue
        side = str(trade.get("side") or "").strip().lower()
        lineage = {
            key: trade.get(key)
            for key in (
                "schema_version",
                "capital_authority_id",
                "authority_generation",
                "execution_lineage_id",
                "lineage_started_at",
                "point_in_time_as_of",
                "execution_lineage_sha256",
            )
        }
        if side == "buy" and trade.get("market_capital_required") is True:
            reservation_id = str(
                trade.get("market_capital_reservation_id") or ""
            ).strip()
            trade_id = str(trade.get("trade_id") or "").strip()
            status = str(trade.get("status") or "").strip().lower()
            terminal_fill = status == "filled" or (
                status == "partial" and trade.get("partial_terminal") is True
            )
            fill_reference = (
                f"MCAPFILL:{ASHARE_AUTHORITY_GENERATION}:"
                f"{ASHARE_EXECUTION_LINEAGE_ID}:{reservation_id}:{trade_id}"
            )
            fill_request = {
                "market": "ashare",
                "reference_id": fill_reference,
                "reservation_id": reservation_id,
                "reservation_event_id": str(trade.get("market_capital_event_id") or ""),
                "reservation_reference_id": str(
                    trade.get("market_capital_reference_id") or ""
                ),
                "risk_unit_key": str(trade.get("market_capital_risk_unit_key") or ""),
                "authority_id": trade.get("capital_authority_id"),
                "authority_generation": trade.get("authority_generation"),
                "execution_lineage_id": trade.get("execution_lineage_id"),
                "lineage_sha256": trade.get("execution_lineage_sha256"),
                "order_id": str(trade.get("order_id") or ""),
                "idempotency_key": idempotency_key,
                "execution_fill_id": trade_id,
                "fill_sequence": _safe_int(
                    trade.get("market_capital_fill_sequence"),
                    1,
                ),
                "side": "buy",
                "status": status,
                "terminal": terminal_fill,
                "actual_filled_quantity": _safe_int(trade.get("quantity"), 0),
                "actual_fill_price": _safe_float(
                    trade.get("filled_price"),
                    0.0,
                ),
                "actual_cash_debit_cny": _safe_float(
                    trade.get("net_amount"),
                    0.0,
                ),
                "actual_exposure_cny": _safe_float(
                    trade.get("amount"),
                    0.0,
                ),
                "actual_margin_cny": 0.0,
                "actual_fee_cash_cny": round(
                    _safe_float(trade.get("commission"), 0.0)
                    + _safe_float(trade.get("stamp_duty"), 0.0)
                    + _safe_float(trade.get("transfer_fee"), 0.0),
                    6,
                ),
                "filled_at": str(trade.get("created_at") or ""),
                "point_in_time_as_of": str(trade.get("point_in_time_as_of") or ""),
                "source": str(trade.get("source") or "local_sim_trade"),
                "source_sha256": str(trade.get("market_capital_source_sha256") or ""),
                "receipt_sha256": str(trade.get("market_capital_receipt_sha256") or ""),
                "local_trade_sha256": str(trade.get("trade_sha256") or ""),
                "expected_ledger_event_id": str(
                    trade.get("market_capital_expected_head_event_id") or ""
                ),
                "expected_ledger_checksum": str(
                    trade.get("market_capital_expected_head_checksum") or ""
                ),
            }
            if not reservation_id or not trade_id:
                raise LocalSimLedgerCorruption(
                    "market_capital_fill_commit_identity_missing"
                )
            request_sha256 = _payload_sha256(fill_request)
            actions.append(
                {
                    "action": "fill_commit",
                    "reference_id": fill_reference,
                    "reservation_id": reservation_id,
                    "amount_cny": fill_request["actual_cash_debit_cny"],
                    "trade_id": trade_id,
                    "idempotency_key": idempotency_key,
                    "risk_unit_key": fill_request["risk_unit_key"],
                    "fill_commit_request": fill_request,
                    "fill_commit_request_sha256": request_sha256,
                    **lineage,
                }
            )
        if side != "sell":
            continue
        trade_id = str(trade.get("trade_id") or "").strip()
        risk_unit_key = (
            str(trade.get("market_capital_risk_unit_key") or trade.get("ts_code") or "")
            .strip()
            .upper()
        )
        expected_head_event_id = str(
            trade.get("market_capital_expected_head_event_id") or ""
        ).strip()
        expected_head_checksum = str(
            trade.get("market_capital_expected_head_checksum") or ""
        ).strip()
        if (
            trade.get("market_capital_required") is not True
            or not trade_id
            or not risk_unit_key
            or not expected_head_event_id
            or not re.fullmatch(r"[a-f0-9]{64}", expected_head_checksum)
        ):
            raise LocalSimLedgerCorruption(
                "market_capital_ashare_sell_commit_identity_missing"
            )
        status = str(trade.get("status") or "").strip().lower()
        terminal_fill = status == "filled" or (
            status == "partial" and trade.get("partial_terminal") is True
        )
        sell_reference = (
            f"MCAPSELL:{ASHARE_AUTHORITY_GENERATION}:"
            f"{ASHARE_EXECUTION_LINEAGE_ID}:{risk_unit_key}:{trade_id}"
        )
        sell_request = {
            "market": "ashare",
            "reference_id": sell_reference,
            "risk_unit_key": risk_unit_key,
            "authority_id": trade.get("capital_authority_id"),
            "authority_generation": trade.get("authority_generation"),
            "execution_lineage_id": trade.get("execution_lineage_id"),
            "lineage_sha256": trade.get("execution_lineage_sha256"),
            "order_id": str(trade.get("order_id") or ""),
            "idempotency_key": idempotency_key,
            "execution_fill_id": trade_id,
            "fill_sequence": _safe_int(trade.get("market_capital_fill_sequence"), 1),
            "side": "sell",
            "status": status,
            "terminal": terminal_fill,
            "actual_closed_quantity": _safe_int(trade.get("quantity"), 0),
            "actual_fill_price": _safe_float(trade.get("filled_price"), 0.0),
            "actual_gross_proceeds_cny": _safe_float(trade.get("amount"), 0.0),
            "actual_fee_cash_cny": round(
                _safe_float(trade.get("commission"), 0.0)
                + _safe_float(trade.get("stamp_duty"), 0.0)
                + _safe_float(trade.get("transfer_fee"), 0.0),
                6,
            ),
            "actual_net_cash_credit_cny": _safe_float(trade.get("net_amount"), 0.0),
            "actual_gross_realized_pnl_cny": _safe_float(
                trade.get("gross_realized_pnl_cny"), 0.0
            ),
            "filled_at": str(trade.get("created_at") or ""),
            "point_in_time_as_of": str(trade.get("point_in_time_as_of") or ""),
            "source": str(trade.get("source") or "local_sim_trade"),
            "source_sha256": str(trade.get("market_capital_source_sha256") or ""),
            "receipt_sha256": str(trade.get("market_capital_receipt_sha256") or ""),
            "local_position_sha256": str(trade.get("trade_sha256") or ""),
            "expected_ledger_event_id": expected_head_event_id,
            "expected_ledger_checksum": expected_head_checksum,
        }
        sell_request_sha256 = _payload_sha256(sell_request)
        actions.append(
            {
                "action": "ashare_sell_commit",
                "reference_id": sell_reference,
                "amount_cny": round(_safe_float(trade.get("realized_pnl_cny"), 0.0), 2),
                "trade_id": trade_id,
                "idempotency_key": idempotency_key,
                "risk_unit_key": risk_unit_key,
                "ashare_sell_commit_request": sell_request,
                "ashare_sell_commit_request_sha256": sell_request_sha256,
                **lineage,
            }
        )
    return actions


def _outbox_action_id(action: dict[str, Any]) -> str:
    identity = f"{action.get('action')}:{action.get('reference_id')}"
    return "ASH-CAP-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _read_market_capital_outbox_unlocked() -> dict[str, Any]:
    path = _market_capital_outbox_path()
    if path.is_symlink() or not path.is_file():
        raise LocalSimLedgerCorruption("market_capital_outbox_unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalSimLedgerCorruption("market_capital_outbox_unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != MARKET_CAPITAL_OUTBOX_SCHEMA_VERSION
        or payload.get("capital_authority_id") != ASHARE_CAPITAL_AUTHORITY_ID
        or payload.get("authority_generation") != ASHARE_AUTHORITY_GENERATION
        or payload.get("execution_lineage_id") != ASHARE_EXECUTION_LINEAGE_ID
        or not isinstance(payload.get("actions"), list)
        or payload.get("real_trading_enabled") is not False
    ):
        raise LocalSimLedgerCorruption("market_capital_outbox_invalid")
    checksum = str(payload.get("payload_sha256") or "")
    if checksum and checksum != _payload_sha256(payload, drop_checksums=True):
        raise LocalSimLedgerCorruption("market_capital_outbox_checksum_mismatch")
    seen: set[str] = set()
    for action in payload["actions"]:
        action_id = (
            str(action.get("action_id") or "") if isinstance(action, dict) else ""
        )
        if not action_id or action_id in seen:
            raise LocalSimLedgerCorruption("market_capital_outbox_duplicate_action")
        seen.add(action_id)
        if action.get("status") not in {"pending", "error", "completed"}:
            raise LocalSimLedgerCorruption("market_capital_outbox_invalid_status")
        if action.get("action") == "fill_commit":
            request = action.get("fill_commit_request")
            request_sha256 = str(action.get("fill_commit_request_sha256") or "")
            if not isinstance(request, dict) or request_sha256 != _payload_sha256(
                request
            ):
                raise LocalSimLedgerCorruption(
                    "market_capital_outbox_fill_commit_checksum_mismatch"
                )
        elif action.get("action") == "ashare_sell_commit":
            request = action.get("ashare_sell_commit_request")
            request_sha256 = str(action.get("ashare_sell_commit_request_sha256") or "")
            if not isinstance(request, dict) or request_sha256 != _payload_sha256(
                request
            ):
                raise LocalSimLedgerCorruption(
                    "market_capital_outbox_ashare_sell_commit_checksum_mismatch"
                )
        else:
            raise LocalSimLedgerCorruption(
                "market_capital_outbox_legacy_action_forbidden"
            )
        try:
            require_execution_lineage(action)
        except ExecutionLineageError as exc:
            raise LocalSimLedgerCorruption(
                f"market_capital_outbox_invalid_lineage:{action_id}:{exc}"
            ) from exc
    return payload


def _write_market_capital_outbox_unlocked(payload: dict[str, Any]) -> None:
    persisted = dict(payload)
    persisted["payload_sha256"] = _payload_sha256(persisted, drop_checksums=True)
    _atomic_write_json(_market_capital_outbox_path(), persisted)


def _reconcile_market_capital_outbox_unlocked(
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    outbox = _read_market_capital_outbox_unlocked()
    by_id = {str(row.get("action_id") or ""): row for row in outbox["actions"]}
    changed = False
    for action in _project_market_capital_actions(trades):
        action_id = _outbox_action_id(action)
        candidate = {
            **action,
            "action_id": action_id,
            "status": "pending",
            "attempt_count": 0,
            "created_at": action.get("point_in_time_as_of"),
            "real_trading_enabled": False,
        }
        existing = by_id.get(action_id)
        if existing is None:
            outbox["actions"].append(candidate)
            by_id[action_id] = candidate
            changed = True
            continue
        identity_fields = (
            "action",
            "reference_id",
            "reservation_id",
            "amount_cny",
            "reason",
            "trade_date",
            "capital_authority_id",
            "authority_generation",
            "execution_lineage_id",
            "execution_lineage_sha256",
            "point_in_time_as_of",
            "risk_unit_key",
            "fill_commit_request_sha256",
            "ashare_sell_commit_request_sha256",
        )
        if any(existing.get(key) != candidate.get(key) for key in identity_fields):
            raise LocalSimLedgerCorruption("market_capital_outbox_action_conflict")
    if changed:
        outbox["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_market_capital_outbox_unlocked(outbox)
    return outbox


def get_local_sim_market_capital_outbox() -> dict[str, Any]:
    """Return durable replay evidence without dispatching any capital action."""

    with _lock():
        return _read_market_capital_outbox_unlocked()


def list_local_sim_market_capital_actions(
    *, account: str | None = None
) -> list[dict[str, Any]]:
    with _lock():
        trades = _load_trades_unlocked()
        _reconcile_market_capital_outbox_unlocked(trades)
        return _project_market_capital_actions(trades, account=account)


def replay_local_sim_market_capital_outbox() -> dict[str, Any]:
    """Replay pending/error actions and persist every attempt and result."""

    import shared.capital as market_capital

    with _lock():
        trades = _load_trades_unlocked()
        outbox = _reconcile_market_capital_outbox_unlocked(trades)
        for action in outbox["actions"]:
            if action.get("status") == "completed":
                continue
            try:
                if action.get("action") == "fill_commit":
                    request_payload = action.get("fill_commit_request")
                    if not isinstance(request_payload, dict):
                        raise LocalSimLedgerCorruption(
                            "market_capital_fill_commit_request_missing"
                        )
                    request_sha256 = str(action.get("fill_commit_request_sha256") or "")
                    if request_sha256 != _payload_sha256(request_payload):
                        raise LocalSimLedgerCorruption(
                            "market_capital_fill_commit_request_checksum_mismatch"
                        )
                    decision = market_capital.commit_market_capital_fill(
                        "ashare",
                        market_capital.MarketCapitalFillCommitRequest(
                            **request_payload
                        ),
                    )
                    result = asdict(decision)
                    success = bool(decision.committed) and decision.status in {
                        "committed",
                        "idempotent",
                    }
                elif action.get("action") == "ashare_sell_commit":
                    request_payload = action.get("ashare_sell_commit_request")
                    if not isinstance(request_payload, dict):
                        raise LocalSimLedgerCorruption(
                            "market_capital_ashare_sell_commit_request_missing"
                        )
                    request_sha256 = str(
                        action.get("ashare_sell_commit_request_sha256") or ""
                    )
                    if request_sha256 != _payload_sha256(request_payload):
                        raise LocalSimLedgerCorruption(
                            "market_capital_ashare_sell_commit_request_checksum_mismatch"
                        )
                    decision = market_capital.commit_market_capital_ashare_sell(
                        "ashare",
                        market_capital.MarketCapitalAshareSellCommitRequest(
                            **request_payload
                        ),
                    )
                    result = asdict(decision)
                    success = bool(decision.committed) and decision.status in {
                        "committed",
                        "idempotent",
                    }
                else:
                    result = {"status": "unsupported_market_capital_action"}
                    success = False
            except Exception as exc:  # noqa: BLE001 - persisted operational evidence
                result = {
                    "status": "market_capital_dispatch_error",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                    "real_trading_enabled": False,
                }
                success = False
            action["attempt_count"] = _safe_int(action.get("attempt_count"), 0) + 1
            action["last_attempt_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            action["last_result"] = dict(result)
            if success:
                action["status"] = "completed"
                action["completed_at"] = action["last_attempt_at"]
                action.pop("last_error", None)
            else:
                action["status"] = "error"
                action["last_error"] = str(
                    result.get("error") or result.get("status") or "unknown"
                )
            outbox["updated_at"] = action["last_attempt_at"]
            _write_market_capital_outbox_unlocked(outbox)
        pending = sum(
            1 for row in outbox["actions"] if row.get("status") != "completed"
        )
        return {
            "status": "pending" if pending else "replayed",
            "pending_count": pending,
            "action_count": len(outbox["actions"]),
            "actions": [dict(row) for row in outbox["actions"]],
            "real_trading_enabled": False,
        }


def get_local_sim_pnl(
    account: str | None = None,
    mark_prices: dict[str, float] | None = None,
    trade_filter: Any | None = None,
    include_validation_samples: bool = False,
    *,
    trade_date: str = "",
    position_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if account is not None:
        try:
            _require_authoritative_account(account)
        except LocalSimLedgerCorruption as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "account": str(account),
                "cash_available": None,
                "positions": {},
                "real_trading_enabled": False,
            }
    manifest = _read_fresh_lineage_manifest()
    if manifest is None:
        return {
            "status": "execution_lineage_unavailable",
            "account": account or "all",
            "cash_available": None,
            "positions": {},
            "real_trading_enabled": False,
        }
    with _lock():
        trades = _load_trades_unlocked()
        if callable(trade_filter):
            trades = [trade for trade in trades if trade_filter(trade)]
        elif not include_validation_samples:
            trades = _strategy_trades_only(trades)
        projection = _replay_account(trades, account, mark_prices=mark_prices)
        return {
            **projection,
            **_latest_lineage_projection(trades, manifest),
            "status": "ready",
            **_position_source_envelope(
                projection.get("positions"),
                trade_date=trade_date,
                position_authority=position_authority,
                source="server_local_sim_pnl",
            ),
            "real_trading_enabled": False,
        }
