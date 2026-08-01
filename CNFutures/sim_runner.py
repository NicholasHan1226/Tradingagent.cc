#!/usr/bin/env python3
"""Automated multi-style simulation runner for China futures."""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from shared.execution.signal_state_machine import (
    SignalStateConflict,
    SignalStateMachine,
)
from shared.execution.sim_broker import execute_sim_order
from shared.markets.sim_capital import default_sim_capital
from shared.review.forward_labels import canonicalize_evidence_record

from . import MARKET
from .adapter import CNFuturesAdapter, READER_MARKET
from .contract_rules import (
    get_contract_rule,
    night_session_end_minute,
    normalize_product,
)
from .execution_evidence import (
    build_execution_evidence,
    build_round_trip_evidence,
)
from .order_events import (
    record_local_sim_order_lifecycle,
    startup_reconcile_order_projection,
)
from .margin_model import estimate_order_cost
from .review import DEFAULT_REVIEW_PATH, append_review, load_review_cluster_state
from .session import parse_cn_datetime, session_bar_age_minutes
from .signal_engine import generate_style_signal
from . import sim_executor as _sim_executor  # noqa: F401  # Ensure registry side effect.


INTRADAY_INTERVAL = "5min"
DEFAULT_MAX_INTRADAY_BAR_AGE_MINUTES = 10.0
CN_TZ = timezone(timedelta(hours=8))
POSITIONS_FILENAME = "cn_futures_sim_positions.json"
POSITION_SNAPSHOT_SCHEMA_VERSION = "2026-07-12.cn-futures-position-snapshot.v2"
ACCOUNT_STATE_FILENAME = "cn_futures_account_state.json"
AFFORDABILITY_FILENAME = "cn_futures_affordability_latest.json"
CAPITAL_OUTBOX_FILENAME = "cn_futures_capital_outbox.json"
CAPITAL_OUTBOX_LOCK_FILENAME = ".cn_futures_capital_outbox.lock"
CAPITAL_OUTBOX_SCHEMA_VERSION = "2026-07-12.cn-futures-capital-outbox.v3"
DEFAULT_DAILY_LOSS_LIMIT_PCT = 0.03
DEFAULT_DRAWDOWN_TIGHTEN_PCT = 0.05
DEFAULT_DRAWDOWN_TIGHTEN_RISK_MULTIPLIER = 0.75
DEFAULT_MAX_DRAWDOWN_PCT = 0.07
DEFAULT_MAX_CONSECUTIVE_LOSSES = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (OverflowError, TypeError, ValueError):
        return default


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _runtime_payload_sha256(payload: dict[str, Any]) -> str:
    canonical = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_cn_futures_capital_provider_state(*, trade_date: str) -> dict[str, Any] | None:
    """Return the cn_futures MarketCapitalLedger provider state, if available."""

    from shared.capital import load_market_capital_provider_state

    return load_market_capital_provider_state("cn_futures", trade_date)


def _strict_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _strict_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_market_capital_provider_state(
    state: Any,
    *,
    trade_date: str,
) -> tuple[dict[str, Any] | None, str]:
    """Validate the market-capital-ledger provider contract fail closed.

    Requires: source=market_capital_ledger, market=cn_futures,
    authority_id=cn-futures-capital-v1, a positive current generation,
    sim-only, reconciled, fresh, trade_date match,
    execution_lineage present, margin_utilization_limit=25000.
    Bootstrap without reconcile fails closed.
    """

    if state is None:
        return None, "market_capital_state_unavailable"
    if not isinstance(state, dict):
        return None, "market_capital_state_invalid_type"
    if str(state.get("source") or "") != "market_capital_ledger":
        return None, "market_capital_state_invalid_source"
    if state.get("reconciled") is not True:
        return None, "market_capital_state_not_reconciled"
    if state.get("fresh") is not True:
        return None, "market_capital_state_not_fresh"
    if str(state.get("market") or "") != "cn_futures":
        return None, "market_capital_state_wrong_market"
    authority_id = state.get("authority_id")
    if authority_id != "cn-futures-capital-v1":
        return None, "market_capital_state_invalid_authority"
    authority_generation = state.get("authority_generation")
    if (
        not isinstance(authority_generation, int)
        or isinstance(authority_generation, bool)
        or authority_generation <= 0
    ):
        return None, "market_capital_state_invalid_generation"
    if state.get("real_trading_enabled") is not False:
        return None, "market_capital_state_not_sim_only"
    execution_lineage_id = state.get("execution_lineage_id")
    if not isinstance(execution_lineage_id, str) or not execution_lineage_id.strip():
        return None, "market_capital_state_missing_execution_lineage"
    if _normalize_trade_date(state.get("trade_date")) != _normalize_trade_date(
        trade_date
    ):
        return None, "market_capital_state_wrong_trade_date"
    if float(state.get("margin_utilization_limit_cny") or 0) != 25_000.0:
        return None, "market_capital_state_invalid_margin_limit"

    required_numbers = (
        "initial_equity_cny",
        "equity_cny",
        "available_margin",
        "margin_utilization_limit_cny",
        "margin_used_cny",
        "unrealized_pnl_cny",
        "cumulative_pnl",
        "daily_realized_pnl",
        "max_daily_loss",
        "consecutive_losses",
        "max_consecutive_losses",
        "high_water_equity",
        "max_drawdown",
    )
    if any(
        key not in state or not _strict_finite_number(state.get(key))
        for key in required_numbers
    ):
        return None, "market_capital_state_missing_or_non_finite_fields"
    if not _strict_nonnegative_integer(state.get("consecutive_losses")):
        return None, "market_capital_state_invalid_consecutive_losses"
    if not _strict_nonnegative_integer(state.get("max_consecutive_losses")):
        return None, "market_capital_state_invalid_max_consecutive_losses"

    event_id = state.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None, "market_capital_state_missing_event_id"
    event_checksum = state.get("event_checksum")
    if (
        not isinstance(event_checksum, str)
        or len(event_checksum) != 64
        or any(ch not in "0123456789abcdef" for ch in event_checksum)
    ):
        return None, "market_capital_state_missing_event_checksum"

    nonnegative_fields = (
        "initial_equity_cny",
        "equity_cny",
        "available_margin",
        "margin_utilization_limit_cny",
        "margin_used_cny",
        "max_daily_loss",
        "consecutive_losses",
        "max_consecutive_losses",
        "high_water_equity",
        "max_drawdown",
    )
    if any(float(state[key]) < 0 for key in nonnegative_fields):
        return None, "market_capital_state_negative_fields"
    initial_equity = float(state["initial_equity_cny"])
    equity = float(state["equity_cny"])
    cumulative_pnl = float(state["cumulative_pnl"])
    unrealized_pnl = float(state["unrealized_pnl_cny"])
    if (
        initial_equity != 50_000.0
        or float(state["margin_utilization_limit_cny"]) != 25_000.0
        or not math.isclose(
            float(state["max_daily_loss"]), initial_equity * 0.03, abs_tol=0.01
        )
        or int(state["max_consecutive_losses"]) != 3
        or not math.isclose(
            float(state["max_drawdown"]), initial_equity * 0.07, abs_tol=0.01
        )
        or not math.isclose(
            equity,
            initial_equity + cumulative_pnl + unrealized_pnl,
            abs_tol=0.01,
        )
        or float(state["available_margin"]) > float(state["equity_cny"])
        or float(state["available_margin"]) + float(state["margin_used_cny"])
        > float(state["margin_utilization_limit_cny"])
        or float(state["high_water_equity"]) < max(initial_equity, equity)
    ):
        return None, "market_capital_state_inconsistent_fields"

    normalized = dict(state)
    normalized["trade_date"] = _normalize_trade_date(trade_date)
    return normalized, "market_capital_state_reconciled"


def _reserve_cn_futures_market_margin(
    *,
    reference_id: str,
    risk_unit_key: str,
    worst_case_amount_cny: float,
    trade_date: str,
    point_in_time_as_of: str,
    lineage_sha256: str,
    execution_lineage_id: str,
    authority_id: str,
    authority_generation: int,
    worst_case_fee_cash_cny: float = 0.0,
) -> dict[str, Any]:
    """Reserve simulated capacity via cn_futures MarketCapitalLedger before a new futures fill."""

    from shared.capital import MarketCapitalReservationRequest, reserve_market_capital

    if authority_id != "cn-futures-capital-v1":
        return {
            "approved": False,
            "reason": "market_capital_reservation_invalid_authority",
            "real_trading_enabled": False,
        }
    if (
        isinstance(authority_generation, bool)
        or not isinstance(authority_generation, int)
        or authority_generation <= 0
    ):
        return {
            "approved": False,
            "reason": "market_capital_reservation_invalid_generation",
            "real_trading_enabled": False,
        }
    try:
        decision = reserve_market_capital(
            "cn_futures",
            MarketCapitalReservationRequest(
                market="cn_futures",
                reference_id=reference_id,
                risk_unit_key=risk_unit_key,
                worst_case_amount_cny=worst_case_amount_cny,
                authority_id=authority_id,
                authority_generation=authority_generation,
                trade_date=trade_date,
                point_in_time_as_of=point_in_time_as_of,
                lineage_sha256=lineage_sha256,
                execution_lineage_id=execution_lineage_id,
                worst_case_cash_cny=worst_case_fee_cash_cny,
                worst_case_exposure_cny=0.0,
                worst_case_margin_cny=worst_case_amount_cny,
            ),
        )
    except Exception as exc:
        return {
            "approved": False,
            "reason": "market_capital_reservation_error",
            "error_type": type(exc).__name__,
            "real_trading_enabled": False,
        }
    snapshot = decision.snapshot
    return {
        "approved": bool(decision.approved),
        "reason": str(decision.reason or "market_capital_unavailable"),
        "reservation_id": str(decision.reservation_id or ""),
        "event_id": str(decision.event_id or ""),
        "amount_cny": round(float(worst_case_amount_cny), 6),
        "reference_id": reference_id,
        "risk_unit_key": risk_unit_key,
        "authority_id": authority_id,
        "authority_generation": authority_generation,
        "trade_date": _normalize_trade_date(trade_date),
        "point_in_time_as_of": point_in_time_as_of,
        "lineage_sha256": lineage_sha256,
        "execution_lineage_id": execution_lineage_id,
        "event_checksum": str(
            getattr(snapshot, "event_checksum", "") if snapshot is not None else ""
        ),
        "fee_cash_cny": round(float(worst_case_fee_cash_cny), 6),
        "real_trading_enabled": False,
    }


def _release_cn_futures_market_margin(
    *,
    reservation_id: str,
    amount_cny: float,
    reason: str,
    reference_id: str,
) -> dict[str, Any]:
    """Release capacity idempotently; failures remain visible and over-reserved."""

    from shared.capital import release_market_capital

    try:
        return dict(
            release_market_capital(
                "cn_futures",
                reservation_id,
                amount_cny,
                reason,
                reference_id=reference_id,
            )
        )
    except Exception as exc:
        return {
            "status": "market_capital_release_error",
            "reservation_id": reservation_id,
            "amount_cny": round(float(amount_cny), 6),
            "reference_id": reference_id,
            "error_type": type(exc).__name__,
            "real_trading_enabled": False,
        }


def _record_cn_futures_market_pnl(
    *,
    reference_id: str,
    amount_cny: float,
    trade_date: str,
    affects_loss_streak: bool,
) -> dict[str, Any]:
    from shared.capital import record_market_capital_realized_pnl

    try:
        return dict(
            record_market_capital_realized_pnl(
                market="cn_futures",
                reference_id=reference_id,
                amount_cny=amount_cny,
                trade_date=trade_date,
                affects_loss_streak=affects_loss_streak,
            )
        )
    except Exception as exc:
        return {
            "status": "market_capital_pnl_error",
            "reference_id": reference_id,
            "amount_cny": round(float(amount_cny), 6),
            "error_type": type(exc).__name__,
            "real_trading_enabled": False,
        }


def _capital_outbox_path(signals_dir: Path) -> Path:
    return Path(signals_dir) / "capital" / CAPITAL_OUTBOX_FILENAME


def _capital_outbox_lock_path(signals_dir: Path) -> Path:
    return Path(signals_dir) / "capital" / CAPITAL_OUTBOX_LOCK_FILENAME


@contextmanager
def _capital_outbox_lock(signals_dir: Path) -> Iterator[None]:
    lock_path = _capital_outbox_lock_path(signals_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists() and lock_path.is_symlink():
        raise RuntimeError("cn_futures_capital_outbox_lock_symlink_not_allowed")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _empty_capital_outbox() -> dict[str, Any]:
    return {
        "schema_version": CAPITAL_OUTBOX_SCHEMA_VERSION,
        "market": MARKET,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "actions": [],
        "real_trading_enabled": False,
    }


def _read_capital_outbox_unlocked(signals_dir: Path) -> dict[str, Any]:
    path = _capital_outbox_path(signals_dir)
    if not path.exists():
        return _empty_capital_outbox()
    if path.is_symlink():
        raise RuntimeError("cn_futures_capital_outbox_symlink_not_allowed")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("cn_futures_capital_outbox_unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != CAPITAL_OUTBOX_SCHEMA_VERSION
        or payload.get("market") != MARKET
        or payload.get("capital_layer") != "simulated"
        or payload.get("account_type") != "simulated"
        or payload.get("real_trading_enabled") is not False
        or not isinstance(payload.get("actions"), list)
    ):
        raise RuntimeError("cn_futures_capital_outbox_invalid")
    checksum = str(payload.get("payload_sha256") or "").strip()
    if checksum and checksum != _runtime_payload_sha256(payload):
        raise RuntimeError("cn_futures_capital_outbox_checksum_mismatch")
    seen: set[str] = set()
    for raw in payload["actions"]:
        if not isinstance(raw, dict):
            raise RuntimeError("cn_futures_capital_outbox_invalid_action")
        action_id = str(raw.get("action_id") or "").strip()
        if not action_id or action_id in seen:
            raise RuntimeError("cn_futures_capital_outbox_duplicate_action")
        seen.add(action_id)
        if raw.get("action") not in {
            "release",
            "realized_pnl",
            "fill_commit",
            "position_close_commit",
        }:
            raise RuntimeError("cn_futures_capital_outbox_invalid_action")
        if raw.get("status") not in {"pending", "completed"}:
            raise RuntimeError("cn_futures_capital_outbox_invalid_status")
        if raw.get("real_trading_enabled") is not False:
            raise RuntimeError("cn_futures_capital_outbox_real_action_rejected")
        if not str(raw.get("reference_id") or "").strip() or not _is_finite_number(
            raw.get("amount_cny")
        ):
            raise RuntimeError("cn_futures_capital_outbox_invalid_action")
        if raw.get("action") == "release" and (
            not str(raw.get("reservation_id") or "").strip()
            or not str(raw.get("reason") or "").strip()
            or float(raw.get("amount_cny", 0.0)) <= 0
        ):
            raise RuntimeError("cn_futures_capital_outbox_invalid_release")
        if raw.get("action") == "realized_pnl" and (
            len(_normalize_trade_date(raw.get("trade_date"))) != 8
            or not _normalize_trade_date(raw.get("trade_date")).isdigit()
            or not isinstance(raw.get("affects_loss_streak"), bool)
        ):
            raise RuntimeError("cn_futures_capital_outbox_invalid_pnl")
        if raw.get("action") in {"fill_commit", "position_close_commit"}:
            request_payload = raw.get("request")
            if not isinstance(request_payload, dict) or not request_payload:
                raise RuntimeError("cn_futures_capital_outbox_invalid_commit")
    return payload


def _write_capital_outbox_unlocked(
    signals_dir: Path,
    payload: dict[str, Any],
) -> None:
    path = _capital_outbox_path(signals_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError("cn_futures_capital_outbox_symlink_not_allowed")
    persisted_payload = dict(payload)
    persisted_payload["payload_sha256"] = _runtime_payload_sha256(persisted_payload)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(
                json.dumps(
                    persisted_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError("cn_futures_capital_outbox_write_failed") from exc


def _capital_action_identity(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "action",
        "reference_id",
        "reservation_id",
        "amount_cny",
        "reason",
        "trade_date",
        "affects_loss_streak",
        "request",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def _capital_action_id(action: str, reference_id: str) -> str:
    return (
        "CNF-CAP-"
        + hashlib.sha256(f"{action}:{reference_id}".encode("utf-8")).hexdigest()[:24]
    )


def _queue_cn_futures_capital_action(
    signals_dir: Path,
    *,
    action: str,
    reference_id: str,
    amount_cny: float,
    reservation_id: str = "",
    reason: str = "",
    trade_date: str = "",
    affects_loss_streak: bool = True,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one stable capital action before any market-capital-ledger call."""

    action_type = str(action or "").strip()
    reference = str(reference_id or "").strip()
    if action_type not in {
        "release",
        "realized_pnl",
        "fill_commit",
        "position_close_commit",
    }:
        raise ValueError("unsupported_cn_futures_capital_action")
    if not reference or not _is_finite_number(amount_cny):
        raise ValueError("invalid_cn_futures_capital_action")
    amount = round(float(amount_cny), 6)
    candidate: dict[str, Any] = {
        "action": action_type,
        "reference_id": reference,
        "amount_cny": amount,
        "status": "pending",
        "created_at": _now_iso(),
        "real_trading_enabled": False,
    }
    if action_type == "release":
        reservation = str(reservation_id or "").strip()
        release_reason = str(reason or "").strip()
        if not reservation or not release_reason or amount <= 0:
            raise ValueError("invalid_cn_futures_release_action")
        candidate.update(
            {
                "reservation_id": reservation,
                "reason": release_reason,
            }
        )
    elif action_type == "realized_pnl":
        if not isinstance(affects_loss_streak, bool):
            raise ValueError("invalid_affects_loss_streak")
        normalized_date = _normalize_trade_date(trade_date)
        if len(normalized_date) != 8 or not normalized_date.isdigit():
            raise ValueError("invalid_cn_futures_pnl_trade_date")
        candidate.update(
            {
                "trade_date": normalized_date,
                "affects_loss_streak": affects_loss_streak,
            }
        )
    else:
        request_payload = dict(request or {})
        if not request_payload:
            raise ValueError("invalid_cn_futures_commit_action")
        candidate["request"] = request_payload
    action_id = _capital_action_id(action_type, reference)
    candidate["action_id"] = action_id
    with _capital_outbox_lock(signals_dir):
        outbox = _read_capital_outbox_unlocked(signals_dir)
        for existing in outbox["actions"]:
            if str(existing.get("action_id") or "") != action_id:
                continue
            if _capital_action_identity(existing) != _capital_action_identity(
                candidate
            ):
                raise RuntimeError("cn_futures_capital_action_conflict")
            return dict(existing)
        outbox["actions"].append(candidate)
        outbox["updated_at"] = _now_iso()
        _write_capital_outbox_unlocked(signals_dir, outbox)
    return dict(candidate)


def _dispatch_cn_futures_capital_outbox(signals_dir: Path) -> dict[str, Any]:
    """Replay pending actions idempotently and mark only confirmed successes."""

    with _capital_outbox_lock(signals_dir):
        outbox = _read_capital_outbox_unlocked(signals_dir)
        for row in outbox["actions"]:
            if row.get("status") == "completed":
                continue
            try:
                if row.get("action") == "release":
                    result = _release_cn_futures_market_margin(
                        reservation_id=str(row.get("reservation_id") or ""),
                        amount_cny=float(row.get("amount_cny", 0.0)),
                        reason=str(row.get("reason") or ""),
                        reference_id=str(row.get("reference_id") or ""),
                    )
                    successful = str(result.get("status") or "") in {
                        "released",
                        "idempotent_release",
                    }
                elif row.get("action") == "realized_pnl":
                    result = _record_cn_futures_market_pnl(
                        reference_id=str(row.get("reference_id") or ""),
                        amount_cny=float(row.get("amount_cny", 0.0)),
                        trade_date=str(row.get("trade_date") or ""),
                        affects_loss_streak=bool(row.get("affects_loss_streak", True)),
                    )
                    successful = str(result.get("status") or "") in {
                        "recorded",
                        "idempotent_realized_pnl",
                    }
                elif row.get("action") == "fill_commit":
                    from shared.capital import (
                        MarketCapitalFillCommitRequest,
                        commit_market_capital_fill,
                    )

                    decision = commit_market_capital_fill(
                        "cn_futures",
                        MarketCapitalFillCommitRequest(**dict(row["request"])),
                    )
                    result = {
                        "status": str(decision.status),
                        "reason": str(decision.reason),
                        "committed": bool(decision.committed),
                        "event_id": str(decision.event_id),
                        "event_checksum": str(
                            decision.snapshot.event_checksum
                            if decision.snapshot is not None
                            else ""
                        ),
                        "idempotent": bool(decision.idempotent),
                        "real_trading_enabled": False,
                    }
                    successful = bool(decision.committed)
                else:
                    from shared.capital import (
                        MarketCapitalPositionCloseCommitRequest,
                        commit_market_capital_position_close,
                    )

                    decision = commit_market_capital_position_close(
                        "cn_futures",
                        MarketCapitalPositionCloseCommitRequest(**dict(row["request"])),
                    )
                    result = {
                        "status": str(decision.status),
                        "reason": str(decision.reason),
                        "committed": bool(decision.committed),
                        "event_id": str(decision.event_id),
                        "event_checksum": str(
                            decision.snapshot.event_checksum
                            if decision.snapshot is not None
                            else ""
                        ),
                        "idempotent": bool(decision.idempotent),
                        "real_trading_enabled": False,
                    }
                    successful = bool(decision.committed)
            except Exception as exc:
                result = {
                    "status": "capital_outbox_dispatch_error",
                    "error_type": type(exc).__name__,
                    "real_trading_enabled": False,
                }
                successful = False
            row["attempt_count"] = _safe_int(row.get("attempt_count"), 0) + 1
            row["last_attempt_at"] = _now_iso()
            row["result"] = dict(result)
            if successful:
                row["status"] = "completed"
                row["completed_at"] = _now_iso()
            outbox["updated_at"] = _now_iso()
            _write_capital_outbox_unlocked(signals_dir, outbox)
        pending_count = sum(
            1 for row in outbox["actions"] if row.get("status") != "completed"
        )
        return {
            "status": "pending" if pending_count else "replayed",
            "pending_count": pending_count,
            "action_count": len(outbox["actions"]),
            "actions": [dict(row) for row in outbox["actions"]],
            "real_trading_enabled": False,
        }


def _release_via_capital_outbox(
    signals_dir: Path,
    *,
    reservation_id: str,
    amount_cny: float,
    reason: str,
    reference_id: str,
) -> dict[str, Any]:
    queued = _queue_cn_futures_capital_action(
        signals_dir,
        action="release",
        reservation_id=reservation_id,
        amount_cny=amount_cny,
        reason=reason,
        reference_id=reference_id,
    )
    replay = _dispatch_cn_futures_capital_outbox(signals_dir)
    for row in replay["actions"]:
        if row.get("action_id") == queued.get("action_id"):
            return dict(row.get("result") or {"status": "capital_outbox_pending"})
    return {"status": "capital_outbox_pending", "real_trading_enabled": False}


def _aware_cn_timestamp(value: Any) -> str:
    parsed = parse_cn_datetime(value)
    if parsed is None:
        raise ValueError("cn_futures_capital_timestamp_invalid")
    return parsed.isoformat(timespec="seconds")


def _execution_fill_id(order_id: str, receipt: dict[str, Any]) -> str:
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    explicit = str(raw.get("execution_fill_id") or raw.get("fill_id") or "").strip()
    if explicit:
        return explicit
    identity = {
        "order_id": order_id,
        "status": str(receipt.get("status") or ""),
        "filled_qty": _safe_int(receipt.get("filled_qty"), 0),
        "avg_price": _safe_float(receipt.get("avg_price"), 0.0),
        "fee": _safe_float(receipt.get("fee"), 0.0),
        "raw_response": raw,
    }
    return "CNF-FILL-" + _runtime_payload_sha256(identity)[:24]


def _immutable_trade_sha256(
    *,
    order: dict[str, Any],
    receipt: dict[str, Any],
    position_snapshot: dict[str, Any],
) -> str:
    return _runtime_payload_sha256(
        {
            "order": dict(order),
            "receipt": dict(receipt),
            "position_snapshot": dict(position_snapshot),
        }
    )


def _build_open_fill_commit_request(
    *,
    order: dict[str, Any],
    receipt: dict[str, Any],
    capital_reservation: dict[str, Any],
    prediction_snapshot: dict[str, Any],
    position_snapshot: dict[str, Any],
) -> dict[str, Any]:
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    order_id = str(order.get("order_id") or receipt.get("order_id") or "").strip()
    reservation_id = str(capital_reservation.get("reservation_id") or "").strip()
    execution_lineage_id = str(
        capital_reservation.get("execution_lineage_id") or ""
    ).strip()
    fill_id = _execution_fill_id(order_id, receipt)
    authority_id = str(capital_reservation.get("authority_id") or "").strip()
    authority_generation = capital_reservation.get("authority_generation")
    if authority_id != "cn-futures-capital-v1" or (
        isinstance(authority_generation, bool)
        or not isinstance(authority_generation, int)
        or authority_generation <= 0
    ):
        raise ValueError("capital_reservation_authority_binding_invalid")
    point_in_time = _aware_cn_timestamp(
        capital_reservation.get("point_in_time_as_of")
        or prediction_snapshot.get("point_in_time_as_of")
    )
    fee_cash = _safe_float(raw.get("open_fee"), -1.0)
    if fee_cash < 0.0:
        fee_cash = _safe_float(receipt.get("fee"), 0.0)
    filled_quantity = _safe_int(receipt.get("filled_qty"), 0)
    filled_price = _safe_float(receipt.get("avg_price"), 0.0)
    contract_cost = estimate_order_cost(
        symbol=str(order.get("symbol") or ""),
        side=str(order.get("side") or ""),
        quantity=filled_quantity,
        price=filled_price,
    )
    contract_margin_per_lot = round(
        float(contract_cost.margin_required) / filled_quantity,
        6,
    )
    from shared.capital import (
        CN_FUTURES_CONTRACT_SPEC_VERSION,
        cn_futures_contract_spec_sha256,
    )

    contract_spec_sha = cn_futures_contract_spec_sha256(
        str(order.get("symbol") or ""),
        float(contract_cost.rule.contract_multiplier),
        contract_margin_per_lot,
    )
    return {
        "market": "cn_futures",
        "reference_id": (
            f"MCAPFILL:{authority_generation}:{execution_lineage_id}:"
            f"{reservation_id}:{fill_id}"
        ),
        "reservation_id": reservation_id,
        "reservation_event_id": str(capital_reservation.get("event_id") or ""),
        "reservation_reference_id": str(
            capital_reservation.get("reference_id") or order_id
        ),
        "risk_unit_key": str(order.get("symbol") or ""),
        "authority_id": authority_id,
        "authority_generation": authority_generation,
        "execution_lineage_id": execution_lineage_id,
        "lineage_sha256": str(capital_reservation.get("lineage_sha256") or ""),
        "order_id": order_id,
        "idempotency_key": str(order.get("idempotency_key") or order_id),
        "execution_fill_id": fill_id,
        "fill_sequence": 1,
        "side": str(order.get("side") or ""),
        "status": str(receipt.get("status") or "").lower(),
        # The local simulation executor is IOC-like. A partial receipt has no
        # later continuation, so unused reservation legs are cancelled here.
        "terminal": True,
        "actual_filled_quantity": filled_quantity,
        "actual_fill_price": filled_price,
        "actual_cash_debit_cny": fee_cash,
        "actual_exposure_cny": 0.0,
        "actual_margin_cny": _safe_float(raw.get("margin_required"), 0.0),
        "actual_fee_cash_cny": fee_cash,
        "contract_multiplier": float(contract_cost.rule.contract_multiplier),
        "contract_margin_per_lot_cny": contract_margin_per_lot,
        "contract_spec_version": CN_FUTURES_CONTRACT_SPEC_VERSION,
        "contract_spec_sha256": contract_spec_sha,
        "filled_at": _now_iso(),
        "point_in_time_as_of": point_in_time,
        "source": "cn_futures_sim_fill_outbox",
        "source_sha256": str(prediction_snapshot.get("source_snapshot_sha256") or ""),
        "receipt_sha256": _runtime_payload_sha256(dict(receipt)),
        "local_trade_sha256": _immutable_trade_sha256(
            order=order,
            receipt=receipt,
            position_snapshot=position_snapshot,
        ),
        "expected_ledger_event_id": str(capital_reservation.get("event_id") or ""),
        "expected_ledger_checksum": str(
            capital_reservation.get("event_checksum") or ""
        ),
    }


def _build_position_close_commit_request(
    *,
    order: dict[str, Any],
    receipt: dict[str, Any],
    prediction_snapshot: dict[str, Any],
    position_snapshot: dict[str, Any],
    previous_position: dict[str, Any],
    performance: dict[str, Any],
    capital_state: dict[str, Any],
) -> dict[str, Any]:
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    order_id = str(order.get("order_id") or receipt.get("order_id") or "").strip()
    fill_id = _execution_fill_id(order_id, receipt)
    risk_unit_key = str(order.get("symbol") or "")
    execution_lineage_id = str(capital_state.get("execution_lineage_id") or "")
    authority_id = str(capital_state.get("authority_id") or "").strip()
    authority_generation = capital_state.get("authority_generation")
    if authority_id != "cn-futures-capital-v1" or (
        isinstance(authority_generation, bool)
        or not isinstance(authority_generation, int)
        or authority_generation <= 0
    ):
        raise ValueError("capital_state_authority_binding_invalid")
    previous_qty = abs(_safe_int(previous_position.get("net_qty"), 0))
    closed_qty = _safe_int(performance.get("closed_quantity"), 0)
    previous_margin = _safe_float(previous_position.get("margin_required"), 0.0)
    margin_released = (
        round(previous_margin * min(1.0, closed_qty / previous_qty), 6)
        if previous_qty > 0 and closed_qty > 0
        else 0.0
    )
    fee_cash = _safe_float(raw.get("estimated_close_fee"), -1.0)
    if fee_cash < 0.0:
        fee_cash = _safe_float(receipt.get("fee"), 0.0)
    point_in_time = _aware_cn_timestamp(prediction_snapshot.get("point_in_time_as_of"))
    return {
        "market": "cn_futures",
        "reference_id": (
            f"MCAPCLOSE:{authority_generation}:{execution_lineage_id}:"
            f"{risk_unit_key}:{fill_id}"
        ),
        "risk_unit_key": risk_unit_key,
        "authority_id": authority_id,
        "authority_generation": authority_generation,
        "execution_lineage_id": execution_lineage_id,
        "lineage_sha256": str(prediction_snapshot.get("source_snapshot_sha256") or ""),
        "order_id": order_id,
        "idempotency_key": str(order.get("idempotency_key") or order_id),
        "execution_fill_id": fill_id,
        "fill_sequence": 1,
        "side": str(order.get("side") or ""),
        "status": str(receipt.get("status") or "").lower(),
        "terminal": True,
        "actual_closed_quantity": closed_qty,
        "actual_fill_price": _safe_float(receipt.get("avg_price"), 0.0),
        "actual_margin_released_cny": margin_released,
        "actual_fee_cash_cny": fee_cash,
        "actual_gross_realized_pnl_cny": _safe_float(performance.get("gross_pnl"), 0.0),
        "filled_at": _now_iso(),
        "point_in_time_as_of": point_in_time,
        "source": "cn_futures_sim_close_outbox",
        "source_sha256": str(prediction_snapshot.get("source_snapshot_sha256") or ""),
        "receipt_sha256": _runtime_payload_sha256(dict(receipt)),
        "local_position_sha256": _immutable_trade_sha256(
            order=order,
            receipt=receipt,
            position_snapshot=position_snapshot,
        ),
        "expected_ledger_event_id": str(capital_state.get("event_id") or ""),
        "expected_ledger_checksum": str(capital_state.get("event_checksum") or ""),
    }


def _distinct_products(symbols: list[str]) -> list[str]:
    products: set[str] = set()
    for symbol in symbols:
        try:
            products.add(normalize_product(symbol))
        except ValueError:
            continue
    return sorted(products)


def _product_or_empty(symbol: str) -> str:
    try:
        return normalize_product(symbol)
    except ValueError:
        return ""


def _style_is_active(style: dict[str, Any]) -> bool:
    status = str(style.get("status") or "").strip().lower()
    if status in {"paused", "deprecated"}:
        return False
    return bool(style.get("enabled", True))


def _inactive_style_reason(style: dict[str, Any]) -> str:
    status = str(style.get("status") or "").strip().lower()
    if status == "deprecated":
        return "style_deprecated"
    if status == "paused":
        return "style_paused"
    if not bool(style.get("enabled", True)):
        return "style_disabled"
    return ""


def _style_allows_symbol(style: dict[str, Any], symbol: str) -> bool:
    raw_products = style.get("products") or style.get("target_products")
    if not raw_products:
        return True
    allowed = {str(item).strip().lower() for item in raw_products if str(item).strip()}
    if not allowed:
        return True
    try:
        return normalize_product(symbol) in allowed
    except ValueError:
        return False


def _cn_local_datetime(now: datetime | None) -> datetime | None:
    if now is None:
        return None
    if now.tzinfo is not None:
        return now.astimezone(CN_TZ)
    return now.replace(tzinfo=CN_TZ)


def _cn_local_time(now: datetime | None) -> time | None:
    current = _cn_local_datetime(now)
    return current.time() if current is not None else None


def _style_allows_session(style: dict[str, Any], now: datetime | None) -> bool:
    if str(style.get("style_family") or "").strip().lower() != "commodity_intraday_trend":
        return True
    if not bool(style.get("no_overnight", True)):
        return True
    current = _cn_local_time(now)
    if current is None:
        return True
    return (time(9, 30) <= current <= time(11, 30)) or (
        time(13, 0) <= current <= time(15, 0)
    )


def _session_bucket(now: datetime | None, *, symbol: str = "") -> str:
    current_dt = _cn_local_datetime(now)
    if current_dt is None:
        return "unknown"
    if current_dt.weekday() >= 5:
        return "closed"
    current = current_dt.time()
    if time(9, 0) <= current <= time(11, 30):
        return "day_morning"
    if time(13, 0) <= current <= time(15, 0):
        return "day_afternoon"
    current_minute = current_dt.hour * 60 + current_dt.minute
    is_night_clock = current_minute >= 21 * 60 or current_minute <= 2 * 60 + 30
    if not is_night_clock or not str(symbol or "").strip():
        return "closed"
    try:
        rule = get_contract_rule(symbol)
    except ValueError:
        return "closed"
    close_minute = rule.night_session_end_minute
    if not rule.night_session or close_minute is None:
        return "closed"
    if current_minute >= 21 * 60:
        if close_minute <= 3 * 60 or current_minute <= close_minute:
            return "night"
        return "closed"
    # Early-morning trading can only continue a Monday-Thursday night
    # session. Weekend dates and Monday morning have no preceding session.
    if current_dt.weekday() not in {1, 2, 3, 4}:
        return "closed"
    if close_minute <= 3 * 60 and current_minute <= close_minute:
        return "night"
    return "closed"


def _aggregate_session_bucket(now: datetime | None, symbols: list[str]) -> str:
    day_bucket = _session_bucket(now)
    if day_bucket in {"day_morning", "day_afternoon", "unknown"}:
        return day_bucket
    for symbol in symbols:
        if _session_bucket(now, symbol=str(symbol or "")) == "night":
            return "night"
    return "closed"


def _exchange_trade_date(
    now: datetime | None,
    *,
    requested_date: str = "",
) -> str:
    """Resolve the exchange attribution date for an intraday session.

    A valid caller-supplied future weekday remains authoritative so an
    exchange calendar can skip holidays. If the legacy caller supplied the
    next *calendar* date and it lands on a weekend, the conservative fallback
    advances to the next weekday. Weekend runtime itself remains closed.
    """

    current = _cn_local_datetime(now)
    if current is None or current.weekday() >= 5:
        return ""
    current_minute = current.hour * 60 + current.minute
    if current_minute < 21 * 60:
        return current.strftime("%Y%m%d")
    requested = _parse_trade_date(requested_date)
    if (
        requested is not None
        and requested.date() > current.date()
        and requested.weekday() < 5
    ):
        return requested.strftime("%Y%m%d")
    candidate = current + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.strftime("%Y%m%d")


def _minutes_until_day_session_close(now: datetime | None) -> float | None:
    current_time = _cn_local_time(now)
    if current_time is None:
        return None
    close_time: time | None = None
    if time(9, 30) <= current_time <= time(11, 30):
        close_time = time(11, 30)
    elif time(13, 0) <= current_time <= time(15, 0):
        close_time = time(15, 0)
    if close_time is None:
        return None
    today = datetime(2000, 1, 1)
    current_dt = datetime.combine(today, current_time)
    close_dt = datetime.combine(today, close_time)
    return (close_dt - current_dt).total_seconds() / 60.0


def _should_flatten_no_overnight(style: dict[str, Any], now: datetime | None) -> bool:
    resolved_exit_plan = _exit_plan_for_signal({}, style)
    if not bool(resolved_exit_plan.get("no_overnight")):
        return False
    minutes_left = _minutes_until_day_session_close(now)
    if minutes_left is None:
        return False
    threshold = max(
        1, _safe_int(resolved_exit_plan.get("flatten_before_session_close_minutes"), 10)
    )
    return 0 <= minutes_left <= threshold


def _parse_dt(value: Any) -> datetime | None:
    parsed = parse_cn_datetime(value)
    return parsed.replace(tzinfo=None) if parsed is not None else None


def _bar_age_minutes(latest_bar_time: str, now: datetime | None) -> float | None:
    if now is None:
        return None
    return session_bar_age_minutes(latest_bar_time, now)


def _is_intraday_bar_fresh(
    latest_bar_time: str, *, now: datetime | None, max_age_minutes: float
) -> tuple[bool, float | None]:
    age = _bar_age_minutes(latest_bar_time, now)
    if age is None:
        return False, None
    return -5.0 <= age <= max_age_minutes, age


def _local_naive_dt(value: datetime) -> datetime:
    return (
        value.astimezone(CN_TZ).replace(tzinfo=None)
        if value.tzinfo is not None
        else value
    )


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _is_after_product_night_close(
    symbol: str, latest_bar_time: str, now: datetime | None
) -> bool:
    """Return true when a stale-looking night bar is actually the product close."""

    if now is None:
        return False
    close_minute = night_session_end_minute(symbol)
    if close_minute is None:
        return False
    bar_dt = _parse_dt(latest_bar_time)
    if bar_dt is None:
        return False
    now_dt = _local_naive_dt(now)
    bar_minute = _minute_of_day(bar_dt)
    now_minute = _minute_of_day(now_dt)
    bar_at_close = close_minute - 5 <= bar_minute <= close_minute
    if not bar_at_close:
        return False
    if close_minute <= 3 * 60:
        return now_dt.date() == bar_dt.date() and now_minute > close_minute
    return (
        now_dt.date() == bar_dt.date() and now_minute > close_minute
    ) or now_dt.date() > bar_dt.date()


def _read_daily_bars(reader: Any, symbol: str, date: str) -> list[dict[str, Any]]:
    get_bars = getattr(reader, "get_bars_daily", None)
    if not callable(get_bars):
        return []
    try:
        rows = get_bars(READER_MARKET, symbol, None, date)
    except TypeError:
        rows = get_bars(market=READER_MARKET, symbol=symbol, end=date)
    except Exception:
        return []
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _normalize_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    first_part = raw[:10]
    digits = "".join(ch for ch in first_part if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _parse_trade_date(value: Any) -> datetime | None:
    normalized = _normalize_trade_date(value)
    if len(normalized) != 8:
        return None
    try:
        return datetime.strptime(normalized, "%Y%m%d")
    except ValueError:
        return None


def _contract_month_start(symbol: str) -> datetime | None:
    value = str(symbol or "").strip().lower().split(".", 1)[0]
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 4:
        return None
    year = 2000 + int(digits[:2])
    month = int(digits[2:4])
    if month < 1 or month > 12:
        return None
    return datetime(year, month, 1)


def _contract_inside_rollover_guard(
    symbol: str, date: str, style: dict[str, Any]
) -> tuple[bool, int | None]:
    min_days = _safe_int(style.get("rollover_min_days_to_contract_month_start"), 0)
    if min_days <= 0:
        return False, None
    trade_dt = _parse_trade_date(date)
    contract_start = _contract_month_start(symbol)
    if trade_dt is None or contract_start is None:
        return False, None
    days_to_month = (contract_start - trade_dt).days
    return -min_days <= days_to_month <= min_days, days_to_month


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _enrich_order_from_bar(order: dict[str, Any], bar: dict[str, Any]) -> None:
    mapped = {
        "bid_price": _first_present(bar, "bid_price", "bid1", "best_bid"),
        "ask_price": _first_present(bar, "ask_price", "ask1", "best_ask"),
        "bid_size": _first_present(bar, "bid_size", "bid_volume", "bid1_volume"),
        "ask_size": _first_present(bar, "ask_size", "ask_volume", "ask1_volume"),
        "last_trade_date": _first_present(bar, "last_trade_date"),
        "expiry_date": _first_present(
            bar, "expiry_date", "expiration_date", "delivery_date"
        ),
    }
    for key, value in mapped.items():
        if value not in (None, ""):
            order[key] = value


def _scenario_tags(
    symbol: str, signal: dict[str, Any], now: datetime | None
) -> dict[str, Any]:
    tags = (
        signal.get("scenario_tags")
        if isinstance(signal.get("scenario_tags"), dict)
        else {}
    )
    product = "unknown"
    try:
        product = normalize_product(symbol)
    except ValueError:
        pass
    return {
        "product": product,
        "session": _session_bucket(now, symbol=symbol),
        "time_bucket": tags.get("time_bucket", "unknown"),
        "direction": signal.get("side") or signal.get("action") or "unknown",
        "volatility_bucket": tags.get("volatility_bucket", "unknown"),
        "volume_bucket": tags.get("volume_bucket", "unknown"),
        "signal_strength_bucket": tags.get("signal_strength_bucket", "unknown"),
    }


def _exit_plan_for_signal(
    signal: dict[str, Any], style: dict[str, Any]
) -> dict[str, Any]:
    plan = signal.get("exit_plan") if isinstance(signal.get("exit_plan"), dict) else {}
    horizon = max(
        1,
        _safe_int(
            plan.get("prediction_horizon_bars")
            or signal.get("prediction_horizon_bars")
            or style.get("prediction_horizon_bars"),
            3,
        ),
    )
    time_stop_bars = max(
        1, _safe_int(plan.get("time_stop_bars") or style.get("time_stop_bars"), horizon)
    )
    max_hold_bars = max(
        time_stop_bars,
        _safe_int(
            plan.get("max_hold_bars") or style.get("max_hold_bars"),
            max(horizon, time_stop_bars),
        ),
    )
    return {
        "prediction_horizon_bars": horizon,
        "time_stop_bars": time_stop_bars,
        "max_hold_bars": max_hold_bars,
        "stop_loss_pct": max(
            0.0,
            _safe_float(
                plan.get("stop_loss_pct")
                if "stop_loss_pct" in plan
                else style.get("stop_loss_pct"),
                0.004,
            ),
        ),
        "take_profit_pct": max(
            0.0,
            _safe_float(
                plan.get("take_profit_pct")
                if "take_profit_pct" in plan
                else style.get("take_profit_pct"),
                0.006,
            ),
        ),
        "flatten_before_session_close_minutes": max(
            0,
            _safe_int(
                plan.get("flatten_before_session_close_minutes")
                or style.get("flatten_before_session_close_minutes"),
                10,
            ),
        ),
        "no_overnight": bool(plan.get("no_overnight", style.get("no_overnight", True))),
    }


def _forward_outcome_label(
    bars: list[dict[str, Any]],
    signal: dict[str, Any],
    exit_plan: dict[str, Any],
    *,
    point_in_time_as_of: str = "",
) -> dict[str, Any]:
    side = str(signal.get("side") or signal.get("action") or "").lower().strip()
    direction = 1 if side == "buy" else (-1 if side == "sell" else 0)
    entry_price = _safe_float(signal.get("price"), 0.0)
    horizon = max(1, _safe_int(exit_plan.get("prediction_horizon_bars"), 3))
    if direction == 0 or entry_price <= 0:
        return {
            "status": "unscored",
            "reason": "not_directional_signal",
            "prediction_horizon_bars": horizon,
            "point_in_time_as_of": point_in_time_as_of,
        }

    # --- PIT guard: find the entry bar at or before as_of --------------------
    if point_in_time_as_of:
        visible_bars = [
            row
            for row in bars
            if str(row.get("bar_time") or row.get("time") or "") <= point_in_time_as_of
        ]
        if not visible_bars:
            return {
                "status": "pending_future_bars",
                "prediction_horizon_bars": horizon,
                "entry_price": entry_price,
                "direction": side,
                "point_in_time_as_of": point_in_time_as_of,
            }
        entry_index = len(visible_bars) - 1
        # Future bars are strictly after as_of — none are visible
        future_rows: list[dict[str, Any]] = []
    else:
        entry_index = len(bars) - 1
        future_rows = bars[entry_index + 1 : entry_index + 1 + horizon]

    if not future_rows:
        return {
            "status": "pending_future_bars",
            "prediction_horizon_bars": horizon,
            "entry_price": entry_price,
            "direction": side,
            "point_in_time_as_of": point_in_time_as_of,
        }
    closes = [_safe_float(row.get("close"), 0.0) for row in future_rows]
    closes = [value for value in closes if value > 0]
    if not closes:
        return {
            "status": "unscored",
            "reason": "missing_future_close",
            "prediction_horizon_bars": horizon,
            "entry_price": entry_price,
            "direction": side,
            "point_in_time_as_of": point_in_time_as_of,
        }
    directional_returns = [
        direction * ((close / entry_price) - 1.0) for close in closes
    ]
    horizon_return = directional_returns[min(len(directional_returns), horizon) - 1]
    max_favorable = max(directional_returns)
    max_adverse = min(directional_returns)
    stop_loss_pct = max(0.0, _safe_float(exit_plan.get("stop_loss_pct"), 0.0))
    take_profit_pct = max(0.0, _safe_float(exit_plan.get("take_profit_pct"), 0.0))
    time_stop_bars = max(1, _safe_int(exit_plan.get("time_stop_bars"), horizon))
    time_stop_index = min(len(directional_returns), time_stop_bars) - 1
    time_stop_return = directional_returns[time_stop_index]
    return {
        "status": "labeled",
        "prediction_horizon_bars": horizon,
        "entry_price": entry_price,
        "direction": side,
        "future_bar_count": len(future_rows),
        "horizon_return_pct": round(horizon_return, 8),
        "time_stop_return_pct": round(time_stop_return, 8),
        "max_favorable_excursion_pct": round(max_favorable, 8),
        "max_adverse_excursion_pct": round(max_adverse, 8),
        "direction_correct": horizon_return > 0,
        "time_stop_positive": time_stop_return > 0,
        "take_profit_hit": bool(take_profit_pct and max_favorable >= take_profit_pct),
        "stop_loss_hit": bool(stop_loss_pct and abs(max_adverse) >= stop_loss_pct),
        "point_in_time_as_of": point_in_time_as_of,
    }


def _first_finite_signal_value(signal: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in signal and _is_finite_number(signal.get(key)):
            return float(signal[key])
    return None


def _style_version(
    style_name: str, style: dict[str, Any], signal: dict[str, Any]
) -> str:
    explicit = str(
        signal.get("style_version")
        or signal.get("strategy_version")
        or style.get("style_version")
        or style.get("strategy_version")
        or style.get("version")
        or ""
    ).strip()
    if explicit:
        return explicit
    encoded = json.dumps(
        {"style": style_name, "config": style},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"config-sha256:{hashlib.sha256(encoded).hexdigest()[:16]}"


def _explicit_mg_state(
    signal: dict[str, Any], style: dict[str, Any]
) -> tuple[bool, str]:
    for source_name, source in (("signal", signal), ("style", style)):
        for key in ("mg_on", "mg_enabled", "marketgraph_enabled"):
            if key not in source:
                continue
            value = source.get(key)
            if isinstance(value, str):
                enabled = value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
            else:
                enabled = bool(value)
            return enabled, f"{source_name}.{key}"
    # This runner does not query MarketGraph implicitly.  Absence of an
    # explicit enhancement flag therefore means the decision was MG-off.
    return False, "runner_default_off"


def _market_regime_evidence(
    signal: dict[str, Any],
    scenario_tags: dict[str, Any],
    style: dict[str, Any],
) -> tuple[str, str]:
    for source_name, source in (
        ("signal", signal),
        ("scenario_tags", scenario_tags),
        ("style", style),
    ):
        value = str(source.get("market_regime") or "").strip()
        if value and value.lower() not in {"unknown", "unavailable"}:
            return value, f"{source_name}.market_regime"
    volatility_bucket = (
        str(scenario_tags.get("volatility_bucket") or "").strip().lower()
    )
    if volatility_bucket and volatility_bucket not in {"unknown", "unavailable"}:
        return f"volatility_{volatility_bucket}", "scenario_tags.volatility_bucket"
    momentum = _first_finite_signal_value(signal, "momentum")
    if momentum is not None:
        if momentum > 0:
            return "directional_up", "signal.momentum"
        if momentum < 0:
            return "directional_down", "signal.momentum"
        return "flat", "signal.momentum"
    return "", "missing"


def _prediction_snapshot_before_risk(
    *,
    style_name: str,
    style: dict[str, Any],
    signal: dict[str, Any],
    scenario_tags: dict[str, Any],
    exit_plan: dict[str, Any],
    forward_outcome: dict[str, Any],
    bar_time: str,
    authority: str = "",
    symbol: str = "",
    source_name: str = "",
    source_cadence: str = "",
    source_bars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    side = str(signal.get("side") or signal.get("action") or "").strip().lower()
    score = _first_finite_signal_value(
        signal,
        "score",
        "directional_score",
        "signal_score",
        "momentum",
    )
    uncalibrated_confidence_prior = _first_finite_signal_value(
        signal,
        "probability",
        "confidence",
    )
    market_regime, market_regime_source = _market_regime_evidence(
        signal,
        scenario_tags,
        style,
    )
    mg_on, mg_evidence_source = _explicit_mg_state(signal, style)
    holding_horizon = {
        "unit": "bars",
        "prediction_horizon_bars": max(
            0, _safe_int(exit_plan.get("prediction_horizon_bars"), 0)
        ),
        "time_stop_bars": max(0, _safe_int(exit_plan.get("time_stop_bars"), 0)),
        "max_hold_bars": max(0, _safe_int(exit_plan.get("max_hold_bars"), 0)),
        "no_overnight": bool(exit_plan.get("no_overnight", True)),
    }
    missing_fields: list[str] = []
    if side not in {"buy", "sell"}:
        missing_fields.append("direction")
    if _safe_float(signal.get("price"), 0.0) <= 0:
        missing_fields.append("price")
    if score is None:
        missing_fields.append("raw_heuristic_score")
    if not market_regime or market_regime.lower() in {"unknown", "unavailable"}:
        missing_fields.append("market_regime")
    if not bar_time:
        missing_fields.append("bar_time")
    if holding_horizon["prediction_horizon_bars"] <= 0:
        missing_fields.append("holding_horizon")

    style_version = _style_version(style_name, style, signal)

    # --- PIT lineage --------------------------------------------------------
    pit_as_of = _aware_cn_timestamp(bar_time)
    source_event_time = pit_as_of
    resolved_authority = str(authority or "").strip()
    resolved_symbol = str(symbol or "").strip()
    resolved_source_name = str(source_name or "").strip()
    resolved_source_cadence = str(source_cadence or "").strip()
    immutable_source_bars = [
        dict(row) for row in (source_bars or []) if isinstance(row, dict)
    ]
    source_evidence: dict[str, Any] = {}
    if immutable_source_bars:
        source_evidence = canonicalize_evidence_record(
            immutable_source_bars[-1],
            boundary=datetime.now(timezone.utc),
            extra_event_fields={"bar_time": bar_time},
        )
        source_validation = source_evidence.get("evidence_envelope_validation")
        if (
            isinstance(source_validation, dict)
            and source_validation.get("complete") is True
            and source_validation.get("status") == "valid"
        ):
            canonical = source_validation.get("canonical_timestamps")
            if isinstance(canonical, dict):
                source_event_time = str(canonical.get("event_time") or pit_as_of)
                pit_as_of = str(
                    source_validation.get("max_evidence_receipt_at") or pit_as_of
                )
    source_rule: dict[str, Any] = {}
    source_rule_version = ""
    if resolved_symbol:
        try:
            rule = get_contract_rule(resolved_symbol)
        except ValueError:
            pass
        else:
            from shared.capital import CN_FUTURES_CONTRACT_SPEC_VERSION

            source_rule_version = CN_FUTURES_CONTRACT_SPEC_VERSION
            source_rule = {
                "product": rule.product,
                "exchange": rule.exchange,
                "contract_multiplier": rule.contract_multiplier,
                "tick_size": rule.tick_size,
                "margin_rate": rule.margin_rate,
                "open_fee_rate": rule.open_fee_rate,
                "close_fee_rate": rule.close_fee_rate,
                "price_limit_rate": rule.price_limit_rate,
                "modeled_overnight_gap_pct": rule.modeled_overnight_gap_pct,
                "modeled_slippage_bps": rule.modeled_slippage_bps,
                "open_fee_type": rule.open_fee_type,
                "close_fee_type": rule.close_fee_type,
                "night_session": rule.night_session,
                "night_session_end_minute": rule.night_session_end_minute,
            }
    source_rule_sha256 = (
        hashlib.sha256(
            json.dumps(
                source_rule,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if source_rule
        else ""
    )
    lineage_missing_fields: list[str] = []
    for field_name, value in (
        ("authority", resolved_authority),
        ("point_in_time_as_of", pit_as_of),
        ("symbol", resolved_symbol),
        ("source_name", resolved_source_name),
        ("source_cadence", resolved_source_cadence),
        ("source_bars", immutable_source_bars),
        ("style_version", style_version),
        ("source_rule_version", source_rule_version),
        ("source_rule", source_rule),
    ):
        if value in (None, "", [], {}):
            lineage_missing_fields.append(field_name)
    lineage_status = "complete" if not lineage_missing_fields else "incomplete"

    source_snapshot_manifest = {
        "authority": resolved_authority,
        "point_in_time_as_of": pit_as_of,
        "source_event_time": source_event_time,
        "source_evidence": source_evidence,
        "source": {
            "name": resolved_source_name,
            "market": READER_MARKET,
            "cadence": resolved_source_cadence,
            "symbol": resolved_symbol,
            "bars": immutable_source_bars,
        },
        "signal": dict(signal),
        "direction": side,
        "style": {
            "name": style_name,
            "version": style_version,
            "config": dict(style),
        },
        "scenario_tags": dict(scenario_tags),
        "exit_plan": dict(exit_plan),
        "contract_rule": {
            "version": source_rule_version,
            "sha256": source_rule_sha256,
            "config": source_rule,
        },
    }
    source_snapshot_sha256 = hashlib.sha256(
        json.dumps(
            source_snapshot_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    source_snapshot_id = f"CNF-SNAP-{source_snapshot_sha256[:16]}"
    source_latest_bar_sha256 = (
        hashlib.sha256(
            json.dumps(
                immutable_source_bars[-1],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if immutable_source_bars
        else ""
    )

    evidence_complete = not missing_fields
    if evidence_complete:
        resolved_forward_outcome = dict(forward_outcome)
        forward_label_status = str(
            resolved_forward_outcome.get("status") or "pending_future_bars"
        )
    else:
        resolved_forward_outcome = {
            "status": "prediction_evidence_incomplete",
            "reason": "prediction_evidence_incomplete",
        }
        forward_label_status = "prediction_evidence_incomplete"
    return {
        "raw_signal": dict(signal),
        "direction": side,
        "side": side,
        "style": style_name,
        "style_version": style_version,
        "raw_heuristic_score": score,
        "uncalibrated_confidence_prior": (
            uncalibrated_confidence_prior
            if uncalibrated_confidence_prior is not None
            and 0.0 <= uncalibrated_confidence_prior <= 1.0
            else None
        ),
        "calibrated_probability": None,
        "probability_model_state": "not_calibrated",
        "market_regime": market_regime,
        "market_regime_source": market_regime_source,
        "mg_on": mg_on,
        "mg_enabled": mg_on,
        "mg_evidence_source": mg_evidence_source,
        "holding_horizon": holding_horizon,
        "scenario_tags": dict(scenario_tags),
        "bar_time": bar_time,
        "entry_price": _safe_float(signal.get("price"), 0.0),
        "evidence_status": "complete" if evidence_complete else "incomplete",
        "evidence_reason": "complete"
        if evidence_complete
        else "prediction_evidence_incomplete",
        "missing_fields": missing_fields,
        "forward_label_status": forward_label_status,
        "forward_outcome": resolved_forward_outcome,
        # PIT lineage fields
        "point_in_time_as_of": pit_as_of,
        "source_event_time": source_event_time,
        "evidence_envelope": dict(source_evidence.get("evidence_envelope") or {}),
        "evidence_envelope_validation": dict(
            source_evidence.get("evidence_envelope_validation") or {}
        ),
        "point_in_time_lineage": dict(
            source_evidence.get("point_in_time_lineage") or {}
        ),
        "source_snapshot_id": source_snapshot_id,
        "source_snapshot_sha256": source_snapshot_sha256,
        "source_name": resolved_source_name,
        "source_market": READER_MARKET,
        "source_symbol": resolved_symbol,
        "source_cadence": resolved_source_cadence,
        "source_bar_count": len(immutable_source_bars),
        "source_latest_bar_sha256": source_latest_bar_sha256,
        "source_rule_version": source_rule_version,
        "source_rule_sha256": source_rule_sha256,
        "authority": resolved_authority,
        "lineage_status": lineage_status,
        "lineage_missing_fields": lineage_missing_fields,
    }


def _latest_hold_bar_time(holds: list[dict[str, Any]]) -> str:
    for hold in reversed(holds):
        if isinstance(hold, dict) and hold.get("bar_time"):
            return str(hold.get("bar_time"))
    return ""


def _positions_path(signals_dir: Path) -> Path:
    return signals_dir / "positions" / POSITIONS_FILENAME


def _read_position_snapshot(signals_dir: Path) -> dict[str, Any]:
    path = _positions_path(signals_dir)
    if not path.exists():
        return {
            "market": MARKET,
            "positions": [],
            "position_count": 0,
            "total_margin_required": 0.0,
        }
    if path.is_symlink():
        raise RuntimeError("cn_futures_position_snapshot_symlink_not_allowed")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("cn_futures_position_snapshot_unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("cn_futures_position_snapshot_invalid")
    checksum = str(payload.get("payload_sha256") or "").strip()
    if checksum and checksum != _runtime_payload_sha256(payload):
        raise RuntimeError("cn_futures_position_snapshot_checksum_mismatch")
    if payload.get("real_trading_enabled") not in {None, False}:
        raise RuntimeError("cn_futures_position_snapshot_real_rejected")
    if payload.get("capital_layer") not in {None, "simulated"}:
        raise RuntimeError("cn_futures_position_snapshot_invalid_capital_layer")
    if payload.get("account_type") not in {None, "simulated"}:
        raise RuntimeError("cn_futures_position_snapshot_invalid_account_type")
    positions = payload.get("positions")
    if not isinstance(positions, list):
        raise RuntimeError("cn_futures_position_snapshot_invalid_positions")
    seen_keys: set[str] = set()
    for position in positions:
        if not isinstance(position, dict):
            raise RuntimeError("cn_futures_position_snapshot_invalid_position")
        style = str(
            position.get("style") or position.get("strategy_name") or ""
        ).strip()
        symbol = str(position.get("symbol") or "").strip()
        net_qty = position.get("net_qty")
        if (
            not style
            or not symbol
            or not isinstance(net_qty, int)
            or isinstance(net_qty, bool)
            or net_qty == 0
        ):
            raise RuntimeError("cn_futures_position_snapshot_invalid_position")
        key = _position_key(style, symbol)
        if key in seen_keys:
            raise RuntimeError("cn_futures_position_snapshot_duplicate_position")
        seen_keys.add(key)
        raw_reservations = position.get("cn_futures_capital_reservations")
        if raw_reservations is not None:
            if not isinstance(raw_reservations, list):
                raise RuntimeError("cn_futures_position_snapshot_invalid_reservations")
            normalized = _capital_reservations(position)
            if len(normalized) != len(raw_reservations):
                raise RuntimeError("cn_futures_position_snapshot_invalid_reservations")
    pending = payload.get("pending_capital_releases", [])
    if not isinstance(pending, list):
        raise RuntimeError("cn_futures_position_snapshot_invalid_pending_releases")
    for row in pending:
        if not isinstance(row, dict) or not isinstance(row.get("reservations"), list):
            raise RuntimeError("cn_futures_position_snapshot_invalid_pending_releases")
        normalized = _capital_reservations(
            {"cn_futures_capital_reservations": row.get("reservations")}
        )
        if len(normalized) != len(row["reservations"]):
            raise RuntimeError("cn_futures_position_snapshot_invalid_pending_releases")
    pending_commits = payload.get("pending_capital_commits", [])
    commit_history = payload.get("capital_commit_history", [])
    if not isinstance(pending_commits, list) or not isinstance(commit_history, list):
        raise RuntimeError("cn_futures_position_snapshot_invalid_capital_commits")
    for row in [*pending_commits, *commit_history]:
        if (
            not isinstance(row, dict)
            or not str(row.get("action_id") or "").strip()
            or row.get("action") not in {"fill_commit", "position_close_commit"}
        ):
            raise RuntimeError("cn_futures_position_snapshot_invalid_capital_commits")
    for row in pending_commits:
        if (
            not isinstance(row.get("request"), dict)
            or not row.get("request")
            or not str(row.get("reference_id") or "").strip()
            or not _is_finite_number(row.get("amount_cny"))
        ):
            raise RuntimeError("cn_futures_position_snapshot_invalid_pending_commit")
    return payload


def _write_position_snapshot(signals_dir: Path, snapshot: dict[str, Any]) -> None:
    positions = [
        position
        for position in snapshot.get("positions", [])
        if isinstance(position, dict) and _safe_int(position.get("net_qty"), 0) != 0
    ]
    total_margin = round(
        sum(
            _safe_float(position.get("margin_required"), 0.0) for position in positions
        ),
        6,
    )
    pending_capital_releases = [
        row
        for row in snapshot.get("pending_capital_releases", [])
        if isinstance(row, dict)
    ]
    pending_capital_commits = [
        row
        for row in snapshot.get("pending_capital_commits", [])
        if isinstance(row, dict)
    ]
    capital_commit_history = [
        row
        for row in snapshot.get("capital_commit_history", [])
        if isinstance(row, dict)
    ]
    normalized_trade_date = _normalize_trade_date(snapshot.get("trade_date"))
    if not normalized_trade_date:
        position_dates = sorted(
            {
                _normalize_trade_date(position.get("updated_trade_date"))
                for position in positions
                if _normalize_trade_date(position.get("updated_trade_date"))
            }
        )
        normalized_trade_date = (
            position_dates[-1] if position_dates else _normalize_trade_date(_now_iso())
        )
    mark_evidence = snapshot.get("mark_evidence_by_symbol")
    payload = {
        "schema_version": POSITION_SNAPSHOT_SCHEMA_VERSION,
        "market": MARKET,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "trade_date": normalized_trade_date,
        "position_count": len(positions),
        "total_margin_required": total_margin,
        "positions": positions,
        "pending_capital_releases": pending_capital_releases,
        "pending_capital_commits": pending_capital_commits,
        "capital_commit_history": capital_commit_history,
        "mark_evidence_by_symbol": (
            dict(mark_evidence) if isinstance(mark_evidence, dict) else {}
        ),
        "updated_at": _now_iso(),
        "real_trading_enabled": False,
    }
    payload["payload_sha256"] = _runtime_payload_sha256(payload)
    path = _positions_path(signals_dir)
    _write_json_atomic(path, payload)


def _runtime_output_path(review_path: Path | None, filename: str) -> Path:
    target = review_path or DEFAULT_REVIEW_PATH
    return target.with_name(filename)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError("cn_futures_runtime_output_symlink_not_allowed")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and path.is_symlink():
            raise RuntimeError("cn_futures_runtime_output_symlink_not_allowed")
        os.replace(temporary_name, path)
        temporary_name = ""
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise RuntimeError("cn_futures_runtime_output_write_failed") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _position_account_totals(snapshot: dict[str, Any]) -> tuple[float, float]:
    unrealized_pnl = 0.0
    margin_required = 0.0
    for position in snapshot.get("positions", []):
        if not isinstance(position, dict):
            continue
        net_qty = _safe_int(position.get("net_qty"), 0)
        avg_price = _safe_float(position.get("avg_price"), 0.0)
        mark_price = _safe_float(position.get("mark_price"), avg_price)
        multiplier = _safe_int(position.get("contract_multiplier"), 0)
        unrealized_pnl += (mark_price - avg_price) * net_qty * multiplier
        margin_required += _safe_float(position.get("margin_required"), 0.0)
    return round(unrealized_pnl, 6), round(margin_required, 6)


def _load_sim_account_ledger(
    *,
    account: dict[str, Any],
    capital: float,
    date: str,
    review_path: Path | None,
    authoritative_state: dict[str, Any] | None = None,
    authority_status: str = "market_capital_state_unavailable",
) -> tuple[dict[str, Any], Path]:
    path = _runtime_output_path(review_path, ACCOUNT_STATE_FILENAME)
    history_exists = (review_path or DEFAULT_REVIEW_PATH).exists()
    ledger: dict[str, Any] | None = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                ledger = loaded
        except (OSError, ValueError):
            ledger = None
    if ledger is None:
        ledger = {
            "schema_version": "2026-07-11.cn-futures-account-state.v1",
            "market": MARKET,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "base_capital": capital,
            "cumulative_pnl": 0.0,
            "daily_realized_pnl": 0.0,
            "daily_trade_date": _normalize_trade_date(date),
            "consecutive_losses": 0,
            "high_water_equity": capital,
            "history_complete": False,
            "history_status": "market_capital_state_unavailable"
            if not history_exists
            else "legacy_review_without_account_state",
            "authority": "none",
            "real_trading_enabled": False,
        }
    else:
        stored_capital = _safe_float(ledger.get("base_capital"), 0.0)
        if stored_capital <= 0 or abs(stored_capital - capital) > 0.01:
            ledger["history_complete"] = False
            ledger["history_status"] = "capital_base_mismatch"
        if _normalize_trade_date(
            ledger.get("daily_trade_date")
        ) != _normalize_trade_date(date):
            ledger["daily_trade_date"] = _normalize_trade_date(date)
            ledger["daily_realized_pnl"] = 0.0

    del account  # Adapter-owned account dictionaries are never authority.
    ledger["history_complete"] = False
    ledger["authority"] = "none"
    ledger.pop("authoritative_account_state", None)
    if authoritative_state is not None:
        base_capital = float(authoritative_state["initial_equity_cny"])
        ledger["base_capital"] = base_capital
        ledger["history_complete"] = True
        ledger["history_status"] = "market_capital_state_reconciled"
        ledger["authority"] = "market_capital_ledger"
        ledger["authoritative_account_state"] = dict(authoritative_state)
        for key in (
            "cumulative_pnl",
            "daily_realized_pnl",
            "consecutive_losses",
            "high_water_equity",
        ):
            if key in authoritative_state:
                ledger[key] = authoritative_state[key]
    else:
        ledger["history_status"] = str(
            authority_status or "market_capital_state_unavailable"
        )
    _write_json_atomic(path, ledger)
    return ledger, path


def _current_account_state(
    *,
    account: dict[str, Any],
    ledger: dict[str, Any],
    position_snapshot: dict[str, Any],
) -> dict[str, Any]:
    del (
        account
    )  # Only the validated provider state persisted in the sidecar is trusted.
    base_capital = _safe_float(ledger.get("base_capital"), 0.0)
    cumulative_pnl = _safe_float(ledger.get("cumulative_pnl"), 0.0)
    unrealized_pnl, reserved_margin = _position_account_totals(position_snapshot)
    derived_equity = base_capital + cumulative_pnl + unrealized_pnl
    authoritative = (
        bool(ledger.get("history_complete"))
        and str(ledger.get("authority") or "none") == "market_capital_ledger"
    )
    explicit_state = (
        ledger.get("authoritative_account_state")
        if authoritative and isinstance(ledger.get("authoritative_account_state"), dict)
        else {}
    )
    external_equity = _safe_float(explicit_state.get("equity_cny"), derived_equity)
    equity = min(external_equity, derived_equity)
    external_available_margin = _safe_float(
        explicit_state.get("available_margin"),
        max(0.0, equity - reserved_margin),
    )
    market_margin_limit = _safe_float(
        explicit_state.get("margin_utilization_limit_cny"),
        external_available_margin,
    )
    market_margin_used = _safe_float(
        explicit_state.get("margin_used_cny"),
        0.0,
    )
    allocation_remaining_margin = max(
        0.0,
        market_margin_limit - max(market_margin_used, reserved_margin),
    )
    available_margin = min(
        external_available_margin,
        allocation_remaining_margin,
        max(0.0, equity - reserved_margin),
    )
    high_water_equity = max(
        _safe_float(ledger.get("high_water_equity"), base_capital),
        equity,
    )
    ledger["high_water_equity"] = round(high_water_equity, 6)
    drawdown = max(0.0, high_water_equity - equity)
    max_daily_loss = _safe_float(
        explicit_state.get("max_daily_loss"),
        base_capital * DEFAULT_DAILY_LOSS_LIMIT_PCT,
    )
    max_drawdown = _safe_float(
        explicit_state.get("max_drawdown"),
        base_capital * DEFAULT_MAX_DRAWDOWN_PCT,
    )
    drawdown_tighten = _safe_float(
        explicit_state.get("drawdown_tighten"),
        base_capital * DEFAULT_DRAWDOWN_TIGHTEN_PCT,
    )
    drawdown_tightened = drawdown >= drawdown_tighten and (
        max_drawdown <= 0 or drawdown < max_drawdown
    )
    risk_multiplier = (
        DEFAULT_DRAWDOWN_TIGHTEN_RISK_MULTIPLIER if drawdown_tightened else 1.0
    )
    max_consecutive_losses = _safe_int(
        explicit_state.get("max_consecutive_losses"),
        DEFAULT_MAX_CONSECUTIVE_LOSSES,
    )
    return {
        "equity": round(equity, 6),
        "available_margin": round(max(0.0, available_margin), 6),
        "reserved_margin": reserved_margin,
        "market_margin_used": round(market_margin_used, 6),
        "margin_utilization_limit_cny": round(market_margin_limit, 6),
        "allocation_remaining_margin": round(allocation_remaining_margin, 6),
        "unrealized_pnl": unrealized_pnl,
        "daily_realized_pnl": round(
            _safe_float(ledger.get("daily_realized_pnl"), 0.0), 6
        ),
        "max_daily_loss": round(max_daily_loss, 6),
        "consecutive_losses": _safe_int(ledger.get("consecutive_losses"), 0),
        "max_consecutive_losses": max_consecutive_losses,
        "drawdown": round(drawdown, 6),
        "max_drawdown": round(max_drawdown, 6),
        "drawdown_tighten": round(drawdown_tighten, 6),
        "drawdown_tightened": drawdown_tightened,
        "risk_multiplier": risk_multiplier,
        "high_water_equity": round(high_water_equity, 6),
        "authoritative": authoritative,
        "counterfactual_only": not authoritative,
        "history_status": str(ledger.get("history_status") or "unknown"),
        "source": "market_capital_ledger"
        if authoritative
        else "cn_futures_local_sim_account_state",
    }


def _execution_pnl_delta(
    *,
    intent: str,
    receipt: dict[str, Any],
    performance: dict[str, Any],
) -> float | None:
    if str(receipt.get("status") or "").lower() not in {"filled", "partial"}:
        return None
    raw_response = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    fee_field = (
        "estimated_close_fee"
        if str(intent or "").strip().lower()
        in {"reduce_only", "flatten_no_overnight", "close"}
        else "open_fee"
    )
    fee = _safe_float(raw_response.get(fee_field), -1.0)
    if fee < 0:
        fee = _safe_float(receipt.get("fee"), 0.0)
    gross_realized = _safe_float(performance.get("gross_pnl"), 0.0)
    return round(gross_realized - fee, 6)


def _apply_execution_to_account_ledger(
    ledger: dict[str, Any],
    *,
    date: str,
    intent: str,
    receipt: dict[str, Any],
    performance: dict[str, Any],
    position_snapshot: dict[str, Any],
    path: Path,
) -> float | None:
    pnl_delta = _execution_pnl_delta(
        intent=intent,
        receipt=receipt,
        performance=performance,
    )
    if pnl_delta is None:
        return None
    ledger["cumulative_pnl"] = round(
        _safe_float(ledger.get("cumulative_pnl"), 0.0) + pnl_delta,
        6,
    )
    if _normalize_trade_date(ledger.get("daily_trade_date")) != _normalize_trade_date(
        date
    ):
        ledger["daily_trade_date"] = _normalize_trade_date(date)
        ledger["daily_realized_pnl"] = 0.0
    ledger["daily_realized_pnl"] = round(
        _safe_float(ledger.get("daily_realized_pnl"), 0.0) + pnl_delta,
        6,
    )
    if performance:
        if pnl_delta < 0:
            ledger["consecutive_losses"] = (
                _safe_int(ledger.get("consecutive_losses"), 0) + 1
            )
        elif pnl_delta > 0:
            ledger["consecutive_losses"] = 0
    unrealized_pnl, _ = _position_account_totals(position_snapshot)
    current_equity = (
        _safe_float(ledger.get("base_capital"), 0.0)
        + _safe_float(ledger.get("cumulative_pnl"), 0.0)
        + unrealized_pnl
    )
    ledger["high_water_equity"] = round(
        max(
            _safe_float(ledger.get("high_water_equity"), current_equity), current_equity
        ),
        6,
    )
    ledger["updated_at"] = _now_iso()
    _write_json_atomic(path, ledger)
    return round(pnl_delta, 6)


def _receipt_execution_eligible(receipt: dict[str, Any]) -> bool:
    """Classify only evidence-backed, real-spec simulated futures fills."""

    if str(receipt.get("status") or "").lower() not in {"filled", "partial"}:
        return False
    if _safe_int(receipt.get("filled_qty"), 0) <= 0:
        return False
    if _safe_float(receipt.get("avg_price"), 0.0) <= 0.0:
        return False
    if str(receipt.get("capital_layer") or "").lower() != "simulated":
        return False
    if str(receipt.get("account_type") or "").lower() != "simulated":
        return False
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    if raw.get("real_trading_enabled") is not False:
        return False
    if str(raw.get("fill_evidence_type") or "") not in {
        "bar_volume_participation",
        "order_book_ask",
        "order_book_bid",
    }:
        return False
    if parse_cn_datetime(raw.get("evidence_timestamp")) is None:
        return False
    if _safe_float(raw.get("margin_required"), 0.0) <= 0.0:
        return False
    if _safe_float(raw.get("contract_multiplier"), 0.0) <= 0.0:
        return False
    # Missing proof is not backward-compatible evidence.  Only an explicit
    # complete PIT lineage may enter the execution-eligible sample layer.
    if receipt.get("pit_lineage_complete") is not True:
        return False
    return True


def _compute_cluster_id(
    *,
    authority: str,
    symbol: str,
    style_version: str,
    side: str,
    bar_time: str,
) -> str:
    """Compute a stable cluster_id for 5-minute bucket dedup.

    Same authority+symbol+style_version+side+5min_bucket → identical id.
    Different buckets → different ids.
    """
    bar_dt = parse_cn_datetime(bar_time)
    if bar_dt is None:
        # Fallback: use raw bar_time truncated to 12-char precision
        raw = "".join(ch for ch in str(bar_time) if ch.isdigit())
        bucket = raw[:12] if len(raw) >= 12 else raw
    else:
        minute_of_day = bar_dt.hour * 60 + bar_dt.minute
        bucket_minute = (minute_of_day // 5) * 5
        bucket = (
            bar_dt.strftime("%Y%m%d")
            + f"T{bucket_minute // 60:02d}:{bucket_minute % 60:02d}"
        )

    identity = f"{authority}|{symbol}|{style_version}|{side}|{bucket}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"CNF-CLUST-{digest}"


def _classify_cluster_occurrence(
    cluster_state: dict[str, dict[str, Any]],
    cluster_id: str,
    *,
    is_execution_eligible: bool = False,
) -> dict[str, Any]:
    """Classify a cluster occurrence as origin or duplicate with weight reduction.

    First occurrence in a cluster → origin (weight_multiplier=1.0).
    Subsequent occurrences → duplicate (weight_multiplier < 1.0).

    Execution-eligible fills only count as origin if they are the first
    execution-eligible occurrence in the cluster.
    """
    if cluster_id not in cluster_state:
        cluster_state[cluster_id] = {
            "occurrence_count": 0,
            "execution_eligible_count": 0,
            "first_bar_time": "",
        }
    state = cluster_state[cluster_id]
    occurrence_index = state["occurrence_count"]
    state["occurrence_count"] += 1

    if occurrence_index == 0:
        state["first_bar_time"] = ""  # set by caller
        return {
            "cluster_role": "origin",
            "occurrence_index": 0,
            "weight_multiplier": 1.0,
        }

    if is_execution_eligible:
        state["execution_eligible_count"] += 1

    return {
        "cluster_role": "duplicate",
        "occurrence_index": occurrence_index,
        # Raw facts remain append-only; statistical weight is zero so cron
        # frequency cannot inflate maturity or KPI evidence.
        "weight_multiplier": 0.0,
    }


def _build_affordability_report(
    *,
    date: str,
    raw_distinct_products: list[str],
    contracts: list[dict[str, Any]],
    account_state: dict[str, Any],
) -> dict[str, Any]:
    affordable_products = sorted(
        {
            str(row.get("product") or "")
            for row in contracts
            if row.get("eligible") is True
            and not bool(row.get("counterfactual_only"))
            and not bool(row.get("reduce_only"))
            and str(row.get("product") or "")
        }
    )
    return {
        "schema_version": "2026-07-11.cn-futures-affordability.v1",
        "market": MARKET,
        "date": date,
        "raw_distinct_products": list(raw_distinct_products),
        "raw_distinct_product_count": len(raw_distinct_products),
        "affordable_distinct_products": affordable_products,
        "affordable_distinct_product_count": len(affordable_products),
        "contracts": contracts,
        "account_state": account_state,
        "counterfactual_only": not bool(account_state.get("authoritative")),
        "real_trading_enabled": False,
        "generated_at": _now_iso(),
    }


def _pre_sizing_affordability_rejection(
    *,
    style_name: str,
    symbol: str,
    reason: str,
    cadence: str,
    bar_time: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    size_decision: dict[str, Any] = {
        "symbol": symbol,
        "quantity": 0,
        "eligible": False,
        "reason": reason,
        "counterfactual_only": True,
    }
    if details:
        size_decision.update(details)
    return {
        "style": style_name,
        "symbol": symbol,
        "product": _product_or_empty(symbol),
        "cadence": cadence,
        "bar_time": bar_time,
        "eligible": False,
        "quantity": 0,
        "reason": reason,
        "counterfactual_only": True,
        "reduce_only": False,
        "execution_class": "counterfactual_only",
        "assessment_status": "rejected_before_sizing",
        "size_decision": size_decision,
    }


def _position_key(style_name: str, symbol: str) -> str:
    return f"{style_name}|{symbol}"


def _positions_by_key(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for position in snapshot.get("positions", []):
        if not isinstance(position, dict):
            continue
        style_name = str(
            position.get("style") or position.get("strategy_name") or ""
        ).strip()
        symbol = str(position.get("symbol") or "").strip()
        if style_name and symbol and _safe_int(position.get("net_qty"), 0) != 0:
            positions[_position_key(style_name, symbol)] = position
    return positions


def _style_margin_used(snapshot: dict[str, Any], style_name: str) -> float:
    return round(
        sum(
            _safe_float(position.get("margin_required"), 0.0)
            for position in snapshot.get("positions", [])
            if isinstance(position, dict)
            and str(position.get("style") or "") == style_name
        ),
        6,
    )


def _side_sign(side: str) -> int:
    return 1 if str(side or "").lower().strip() in {"buy", "long"} else -1


def _position_side(net_qty: int) -> str:
    return "long" if net_qty > 0 else "short"


def _opposite_side_for_position(position: dict[str, Any]) -> str:
    return "sell" if _safe_int(position.get("net_qty"), 0) > 0 else "buy"


def _capital_reservations(position: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (
        position.get("cn_futures_capital_reservations")
        if isinstance(position, dict)
        else []
    )
    output: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return output
    for row in rows:
        if not isinstance(row, dict):
            continue
        reservation_id = str(row.get("reservation_id") or "").strip()
        amount = _safe_float(row.get("amount_cny"), 0.0)
        if not reservation_id or amount <= 0:
            continue
        output.append(
            {
                "reservation_id": reservation_id,
                "event_id": str(row.get("event_id") or ""),
                "amount_cny": round(amount, 6),
            }
        )
    return output


def _release_position_capital(
    *,
    signals_dir: Path,
    position: dict[str, Any],
    closed_quantity: int,
    order_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    previous_quantity = abs(_safe_int(position.get("net_qty"), 0))
    reservations = _capital_reservations(position)
    if previous_quantity <= 0 or closed_quantity <= 0 or not reservations:
        return reservations, []
    release_ratio = min(1.0, closed_quantity / previous_quantity)
    remaining: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for row in reservations:
        amount = _safe_float(row.get("amount_cny"), 0.0)
        release_amount = (
            amount if release_ratio >= 1.0 else round(amount * release_ratio, 6)
        )
        if release_amount <= 0:
            remaining.append(row)
            continue
        result = _release_via_capital_outbox(
            signals_dir,
            reservation_id=str(row["reservation_id"]),
            amount_cny=release_amount,
            reason="filled_futures_reduce",
            reference_id=f"{order_id}:release:{row['reservation_id']}",
        )
        results.append(result)
        if str(result.get("status") or "") in {"released", "idempotent_release"}:
            remaining_amount = round(amount - release_amount, 6)
            if remaining_amount > 0:
                remaining.append({**row, "amount_cny": remaining_amount})
        else:
            # Over-reservation is safer than silently minting capacity.  Keep
            # the full row so an operator/reconciliation retry can release it.
            remaining.append(row)
    return remaining, results


def _update_position_snapshot(
    signals_dir: Path,
    *,
    date: str,
    style_name: str,
    symbol: str,
    order: dict[str, Any],
    receipt: dict[str, Any],
    performance: dict[str, Any],
) -> dict[str, Any]:
    snapshot = _read_position_snapshot(signals_dir)
    positions = _positions_by_key(snapshot)
    key = _position_key(style_name, symbol)
    previous = dict(positions.get(key, {}))
    previous_qty = _safe_int(previous.get("net_qty"), 0)
    filled_qty = _safe_int(receipt.get("filled_qty"), 0)
    if filled_qty <= 0:
        return snapshot
    snapshot["trade_date"] = _normalize_trade_date(date)
    fill_sign = _side_sign(str(order.get("side") or "buy"))
    new_qty = previous_qty + (fill_sign * filled_qty)
    reducing = previous_qty != 0 and (previous_qty > 0) != (fill_sign > 0)
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    previous_capital_reservations = _capital_reservations(previous)
    explicit_after = order.get("cn_futures_capital_reservations_after")
    if isinstance(explicit_after, list):
        capital_reservations_after = _capital_reservations(
            {"cn_futures_capital_reservations": explicit_after}
        )
    elif str(order.get("capital_commit_mode") or "") == "atomic":
        capital_reservations_after = []
    else:
        capital_reservations_after = list(previous_capital_reservations)
        new_reservation = order.get("cn_futures_capital_reservation")
        if isinstance(new_reservation, dict):
            capital_reservations_after.extend(
                _capital_reservations(
                    {"cn_futures_capital_reservations": [new_reservation]}
                )
            )
    positions.pop(key, None)
    if new_qty != 0:
        avg_price = _safe_float(
            receipt.get("avg_price"), _safe_float(order.get("price"), 0.0)
        )
        if (
            previous_qty
            and (previous_qty > 0) == (new_qty > 0)
            and (previous_qty > 0) == (fill_sign > 0)
        ):
            previous_abs = abs(previous_qty)
            previous_price = _safe_float(previous.get("avg_price"), avg_price)
            avg_price = round(
                ((previous_price * previous_abs) + (avg_price * filled_qty))
                / max(previous_abs + filled_qty, 1),
                8,
            )
        mark_price = _safe_float(
            order.get("price"), _safe_float(receipt.get("avg_price"), avg_price)
        )
        contract_multiplier = _safe_int(
            order.get("contract_multiplier") or raw.get("contract_multiplier"), 1
        )
        previous_margin = _safe_float(previous.get("margin_required"), 0.0)
        filled_margin = _safe_float(raw.get("margin_required"), 0.0)
        if reducing and previous_qty != 0:
            margin_required = round(
                previous_margin * abs(new_qty) / abs(previous_qty), 6
            )
        elif previous_qty == 0 or (previous_qty > 0) != (new_qty > 0):
            margin_required = filled_margin
        else:
            margin_required = round(previous_margin + filled_margin, 6)
        positions[key] = {
            "style": style_name,
            "strategy_name": style_name,
            "symbol": symbol,
            "net_qty": new_qty,
            "side": _position_side(new_qty),
            "avg_price": avg_price,
            "last_price": _safe_float(
                receipt.get("avg_price"), _safe_float(order.get("price"), 0.0)
            ),
            "mark_price": mark_price,
            "contract_multiplier": contract_multiplier,
            "margin_required": margin_required,
            "notional": _safe_float(raw.get("notional"), 0.0),
            "updated_trade_date": _normalize_trade_date(date),
            "updated_at": _now_iso(),
            "last_order_id": order.get("order_id"),
            "last_bar_time": order.get("bar_time"),
            "realized_pnl": _safe_float(previous.get("realized_pnl"), 0.0)
            + _safe_float(performance.get("realized_pnl"), 0.0),
            "cn_futures_capital_reservations": capital_reservations_after,
            "capital_commit_status": str(
                order.get("capital_commit_status") or "pending"
            ),
            "capital_commit_action_id": str(
                order.get("capital_commit_action_id") or ""
            ),
        }
    elif capital_reservations_after:
        pending = snapshot.get("pending_capital_releases")
        if not isinstance(pending, list):
            pending = []
        pending.append(
            {
                "order_id": str(order.get("order_id") or ""),
                "symbol": symbol,
                "style": style_name,
                "reservations": capital_reservations_after,
                "release_results": order.get("cn_futures_capital_release_results", []),
                "reason": "closed_position_capital_release_pending",
                "updated_at": _now_iso(),
            }
        )
        snapshot["pending_capital_releases"] = pending
    snapshot["positions"] = sorted(
        positions.values(),
        key=lambda item: (str(item.get("style")), str(item.get("symbol"))),
    )
    action_id = str(order.get("capital_commit_action_id") or "").strip()
    action_type = str(order.get("capital_commit_action") or "").strip()
    if action_id and action_type in {"fill_commit", "position_close_commit"}:
        pending_commits = [
            dict(row)
            for row in snapshot.get("pending_capital_commits", [])
            if isinstance(row, dict) and str(row.get("action_id") or "") != action_id
        ]
        pending_commits.append(
            {
                "action_id": action_id,
                "action": action_type,
                "order_id": str(order.get("order_id") or ""),
                "style": style_name,
                "symbol": symbol,
                "status": "pending",
                "reference_id": str(order.get("capital_commit_reference_id") or ""),
                "amount_cny": _safe_float(order.get("capital_commit_amount_cny"), 0.0),
                "request": dict(order.get("capital_commit_request") or {}),
                "updated_at": _now_iso(),
            }
        )
        snapshot["pending_capital_commits"] = pending_commits
    _write_position_snapshot(signals_dir, snapshot)
    return _read_position_snapshot(signals_dir)


def _persist_position_execution_evidence(
    signals_dir: Path,
    *,
    style_name: str,
    symbol: str,
    action: str,
    execution_evidence: dict[str, Any],
    previous_position: dict[str, Any] | None = None,
    closed_quantity: int = 0,
) -> dict[str, Any]:
    """Attach immutable open evidence to the remaining simulated position."""

    snapshot = _read_position_snapshot(signals_dir)
    positions: list[dict[str, Any]] = []
    changed = False
    for raw in snapshot.get("positions", []):
        if not isinstance(raw, dict):
            continue
        position = dict(raw)
        if (
            str(position.get("style") or "") == style_name
            and str(position.get("symbol") or "") == symbol
        ):
            if action == "fill_commit":
                position["entry_execution_evidence"] = dict(execution_evidence)
                position["entry_evidence_quantity_remaining"] = _safe_int(
                    execution_evidence.get("filled_quantity"), 0
                )
                changed = True
            elif action == "position_close_commit" and previous_position:
                entry = previous_position.get("entry_execution_evidence")
                if isinstance(entry, dict) and entry:
                    previous_remaining = _safe_int(
                        previous_position.get("entry_evidence_quantity_remaining"),
                        _safe_int(entry.get("filled_quantity"), 0),
                    )
                    remaining = max(0, previous_remaining - max(0, closed_quantity))
                    if remaining > 0:
                        position["entry_execution_evidence"] = dict(entry)
                        position["entry_evidence_quantity_remaining"] = remaining
                    changed = True
        positions.append(position)
    if changed:
        snapshot["positions"] = positions
        _write_position_snapshot(signals_dir, snapshot)
    return _read_position_snapshot(signals_dir)


def _update_position_capital_metadata(
    signals_dir: Path,
    *,
    style_name: str,
    symbol: str,
    order_id: str,
    reservations_after: list[dict[str, Any]],
    release_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Close the durable release outbox without applying the fill twice."""

    snapshot = _read_position_snapshot(signals_dir)
    normalized_reservations = _capital_reservations(
        {"cn_futures_capital_reservations": reservations_after}
    )
    active_position = False
    positions: list[dict[str, Any]] = []
    for raw in snapshot.get("positions", []):
        if not isinstance(raw, dict):
            continue
        position = dict(raw)
        if (
            str(position.get("style") or position.get("strategy_name") or "")
            == style_name
            and str(position.get("symbol") or "") == symbol
        ):
            position["cn_futures_capital_reservations"] = normalized_reservations
            active_position = _safe_int(position.get("net_qty"), 0) != 0
        positions.append(position)
    snapshot["positions"] = positions
    pending = [
        dict(row)
        for row in snapshot.get("pending_capital_releases", [])
        if isinstance(row, dict) and str(row.get("order_id") or "") != order_id
    ]
    if normalized_reservations and not active_position:
        pending.append(
            {
                "order_id": order_id,
                "symbol": symbol,
                "style": style_name,
                "reservations": normalized_reservations,
                "release_results": release_results,
                "reason": "closed_position_capital_release_pending",
                "updated_at": _now_iso(),
            }
        )
    snapshot["pending_capital_releases"] = pending
    _write_position_snapshot(signals_dir, snapshot)
    return _read_position_snapshot(signals_dir)


def _sync_position_capital_commits(
    signals_dir: Path,
    replay: dict[str, Any],
) -> dict[str, Any]:
    """Fold completed durable capital actions into position metadata only."""

    snapshot = _read_position_snapshot(signals_dir)
    by_action_id = {
        str(row.get("action_id") or ""): dict(row)
        for row in replay.get("actions", [])
        if isinstance(row, dict) and str(row.get("action_id") or "")
    }
    changed = False
    positions: list[dict[str, Any]] = []
    for raw in snapshot.get("positions", []):
        if not isinstance(raw, dict):
            continue
        position = dict(raw)
        action_id = str(position.get("capital_commit_action_id") or "")
        action = by_action_id.get(action_id)
        if action and action.get("status") == "completed":
            result = dict(action.get("result") or {})
            position["capital_commit_status"] = "committed"
            position["capital_commit_event_id"] = str(result.get("event_id") or "")
            position["cn_futures_capital_reservations"] = []
            changed = True
        positions.append(position)
    snapshot["positions"] = positions

    pending: list[dict[str, Any]] = []
    history = [
        dict(row)
        for row in snapshot.get("capital_commit_history", [])
        if isinstance(row, dict)
    ]
    history_ids = {str(row.get("action_id") or "") for row in history}
    for raw in snapshot.get("pending_capital_commits", []):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        action_id = str(row.get("action_id") or "")
        action = by_action_id.get(action_id)
        if action and action.get("status") == "completed":
            if action_id not in history_ids:
                history.append(
                    {
                        **row,
                        "status": "committed",
                        "result": dict(action.get("result") or {}),
                        "completed_at": str(action.get("completed_at") or _now_iso()),
                    }
                )
                history_ids.add(action_id)
            changed = True
        else:
            pending.append(row)
    snapshot["pending_capital_commits"] = pending
    snapshot["capital_commit_history"] = history
    if changed:
        _write_position_snapshot(signals_dir, snapshot)
    return _read_position_snapshot(signals_dir)


def _replay_cn_futures_capital_actions(signals_dir: Path) -> dict[str, Any]:
    """Replay immutable atomic commit actions and materialize their status."""

    snapshot = _read_position_snapshot(signals_dir)
    legacy_pending = [
        row
        for row in snapshot.get("pending_capital_releases", [])
        if isinstance(row, dict)
    ]
    materialized = 0
    for row in snapshot.get("pending_capital_commits", []):
        if not isinstance(row, dict):
            continue
        _queue_cn_futures_capital_action(
            signals_dir,
            action=str(row.get("action") or ""),
            reference_id=str(row.get("reference_id") or ""),
            amount_cny=_safe_float(row.get("amount_cny"), 0.0),
            request=dict(row.get("request") or {}),
        )
        materialized += 1
    replay = _dispatch_cn_futures_capital_outbox(signals_dir)
    _sync_position_capital_commits(signals_dir, replay)
    pending_count = int(replay.get("pending_count", 0)) + len(legacy_pending)
    return {
        **replay,
        "status": "pending" if pending_count else "replayed",
        "pending_count": pending_count,
        "materialized_pending_commit_count": materialized,
        "legacy_pending_release_count": len(legacy_pending),
        "fresh_atomic_commits_only": not bool(legacy_pending),
    }


def _read_intraday_bars(reader: Any, symbol: str, date: str) -> list[dict[str, Any]]:
    get_bars = getattr(reader, "get_bars_intraday", None)
    if not callable(get_bars):
        return []
    rows: Any
    try:
        rows = get_bars(READER_MARKET, symbol, INTRADAY_INTERVAL, date, date)
    except TypeError:
        try:
            rows = get_bars(
                market=READER_MARKET,
                symbol=symbol,
                interval=INTRADAY_INTERVAL,
                start=date,
                end=date,
            )
        except TypeError:
            rows = get_bars(READER_MARKET, symbol, INTRADAY_INTERVAL)
        except Exception:
            return []
    except Exception:
        return []
    trade_date = _normalize_trade_date(date)
    normalized = [dict(row) for row in rows or [] if isinstance(row, dict)]
    filtered = [
        row
        for row in normalized
        if not trade_date
        or _normalize_trade_date(
            row.get("trade_date") or row.get("bar_time") or row.get("time")
        )
        == trade_date
    ]
    filtered.sort(
        key=lambda row: str(
            row.get("bar_time") or row.get("time") or row.get("trade_time") or ""
        )
    )
    return filtered


def _bars_for_cadence(
    reader: Any, symbol: str, date: str, cadence: str
) -> tuple[list[dict[str, Any]], str, str]:
    cadence_value = str(cadence or INTRADAY_INTERVAL).lower()
    if cadence_value in {"daily", "1d", "day"}:
        return _read_daily_bars(reader, symbol, date), "daily", ""
    bars = _read_intraday_bars(reader, symbol, date)
    latest_bar_time = str(
        (bars[-1] if bars else {}).get("bar_time")
        or (bars[-1] if bars else {}).get("time")
        or ""
    )
    return bars, INTRADAY_INTERVAL, latest_bar_time


def _explicit_source_identity(
    reader: Any,
    bars: list[dict[str, Any]],
) -> str:
    """Return only an identity explicitly supplied by the injected data port.

    A reader implementation name is not data lineage, and TradingDatas dataset
    IDs are not known before the fresh handoff.  Fixture ports therefore expose
    an explicit ``source_identity``; future V1 ports may propagate a dataset ID
    on the reader or row envelope.  Missing or conflicting identities remain
    empty so the simulated receipt fails closed as lineage-incomplete.
    """

    candidates: set[str] = set()

    reader_identity = getattr(reader, "source_identity", None)
    if callable(reader_identity):
        try:
            reader_identity = reader_identity()
        except Exception:
            reader_identity = None
    if isinstance(reader_identity, Mapping):
        reader_identity = (
            reader_identity.get("dataset_id")
            or reader_identity.get("source_name")
            or reader_identity.get("identity")
        )
    if isinstance(reader_identity, str) and reader_identity.strip():
        candidates.add(reader_identity.strip())

    for bar in bars:
        metadata = bar.get("metadata") if isinstance(bar.get("metadata"), dict) else {}
        identity = (
            bar.get("source_dataset_id")
            or bar.get("dataset_id")
            or metadata.get("dataset_id")
            or metadata.get("source_name")
        )
        if isinstance(identity, str) and identity.strip():
            candidates.add(identity.strip())

    return next(iter(candidates)) if len(candidates) == 1 else ""


def _order_period_key(date: str, cadence: str, latest_bar_time: str) -> str:
    if cadence == "daily":
        return str(date)
    raw = latest_bar_time or str(date)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 12:
        return digits[:12]
    if len(digits) >= 8:
        return digits[:8]
    return str(date)


def _same_day_filled_signals(
    signals_dir: Path, *, date: str, style_name: str, symbol: str
) -> list[dict[str, Any]]:
    filled_dir = signals_dir / "filled"
    if not filled_dir.exists():
        return []
    trade_date = _normalize_trade_date(date)
    rows: list[dict[str, Any]] = []
    for path in sorted(filled_dir.glob("SIM-CNF-*.json")):
        try:
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(payload.get("strategy_name") or "") != style_name:
            continue
        if str(payload.get("symbol") or payload.get("ts_code") or "") != symbol:
            continue
        payload_date = _normalize_trade_date(
            payload.get("trade_date")
            or payload.get("valid_until")
            or payload.get("bar_time")
        )
        if trade_date and payload_date != trade_date:
            continue
        rows.append(payload)
    rows.sort(
        key=lambda item: str(
            item.get("bar_time") or item.get("timestamp") or item.get("order_id") or ""
        )
    )
    return rows


def _has_repeated_same_side_exposure(
    signals_dir: Path,
    *,
    date: str,
    style_name: str,
    symbol: str,
    side: str,
) -> bool:
    rows = _same_day_filled_signals(
        signals_dir, date=date, style_name=style_name, symbol=symbol
    )
    if not rows:
        return False
    latest = rows[-1]
    latest_side = (
        str(latest.get("side") or latest.get("direction") or "").lower().strip()
    )
    return bool(latest_side and latest_side == str(side or "").lower().strip())


def _latest_opposite_fill(
    signals_dir: Path,
    *,
    date: str,
    style_name: str,
    symbol: str,
    side: str,
) -> dict[str, Any] | None:
    rows = _same_day_filled_signals(
        signals_dir, date=date, style_name=style_name, symbol=symbol
    )
    for row in reversed(rows):
        previous_side = (
            str(row.get("side") or row.get("direction") or "").lower().strip()
        )
        if previous_side and previous_side != str(side or "").lower().strip():
            return row
    return None


def _realized_pnl_from_reversal(
    *,
    previous: dict[str, Any] | None,
    side: str,
    receipt: dict[str, Any],
    rule_multiplier: int,
) -> dict[str, Any]:
    if not previous:
        return {}
    previous_price = _safe_float(
        previous.get("filled_price") or previous.get("price"), 0.0
    )
    exit_price = _safe_float(receipt.get("avg_price"), 0.0)
    qty = min(
        _safe_int(
            previous.get("filled_qty")
            or previous.get("filled_quantity")
            or previous.get("quantity"),
            0,
        ),
        _safe_int(receipt.get("filled_qty"), 0),
    )
    if previous_price <= 0 or exit_price <= 0 or qty <= 0:
        return {}
    previous_side = (
        str(previous.get("side") or previous.get("direction") or "").lower().strip()
    )
    current_side = str(side or "").lower().strip()
    gross = 0.0
    if previous_side in {"buy", "long"} and current_side in {"sell", "short"}:
        gross = (exit_price - previous_price) * qty * rule_multiplier
    elif previous_side in {"sell", "short"} and current_side in {"buy", "long"}:
        gross = (previous_price - exit_price) * qty * rule_multiplier
    else:
        return {}
    previous_raw = (
        previous.get("raw_response")
        if isinstance(previous.get("raw_response"), dict)
        else {}
    )
    receipt_raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    previous_fee = _safe_float(previous.get("fee"), 0.0)
    previous_round_trip_fee = _safe_float(previous_raw.get("total_estimated_fee"), 0.0)
    if previous_round_trip_fee > 0 and previous_fee >= previous_round_trip_fee:
        fee = previous_fee
    else:
        close_fee = _safe_float(receipt_raw.get("estimated_close_fee"), 0.0)
        fee = previous_fee + (
            close_fee if close_fee > 0 else _safe_float(receipt.get("fee"), 0.0)
        )
    return {
        "realized_pnl": round(gross - fee, 6),
        "gross_pnl": round(gross, 6),
        "round_trip_fee": round(fee, 6),
        "closed_quantity": qty,
        "entry_price": previous_price,
        "exit_price": exit_price,
        "entry_side": previous_side,
        "exit_side": current_side,
        "method": "same_day_reversal_estimate",
    }


def _realized_pnl_from_position_close(
    *,
    position: dict[str, Any] | None,
    side: str,
    receipt: dict[str, Any],
    rule_multiplier: int,
) -> dict[str, Any]:
    if not position:
        return {}
    net_qty = _safe_int(position.get("net_qty"), 0)
    entry_price = _safe_float(position.get("avg_price"), 0.0)
    exit_price = _safe_float(receipt.get("avg_price"), 0.0)
    closed_qty = min(abs(net_qty), _safe_int(receipt.get("filled_qty"), 0))
    if net_qty == 0 or entry_price <= 0 or exit_price <= 0 or closed_qty <= 0:
        return {}
    current_side = str(side or "").lower().strip()
    gross = 0.0
    if net_qty > 0 and current_side in {"sell", "short"}:
        gross = (exit_price - entry_price) * closed_qty * rule_multiplier
    elif net_qty < 0 and current_side in {"buy", "long"}:
        gross = (entry_price - exit_price) * closed_qty * rule_multiplier
    else:
        return {}
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    fee = _safe_float(raw.get("estimated_close_fee"), 0.0)
    if fee <= 0:
        fee = _safe_float(receipt.get("fee"), 0.0)
    return {
        "realized_pnl": round(gross - fee, 6),
        "gross_pnl": round(gross, 6),
        "round_trip_fee": round(fee, 6),
        "closed_quantity": closed_qty,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_side": _position_side(net_qty),
        "exit_side": current_side,
        "method": "force_flatten_position_close",
    }


def _affordability_rejection(
    *,
    symbol: str,
    reason: str,
    counterfactual_only: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision = {
        "symbol": symbol,
        "quantity": 0,
        "margin_per_lot": 0.0,
        "margin_budget": 0.0,
        "modeled_loss_per_lot": 0.0,
        "loss_budget": 0.0,
        "eligible": False,
        "reason": reason,
        "counterfactual_only": counterfactual_only,
    }
    if details:
        decision.update(details)
    return decision


def quantity_for_style_decision(
    *,
    symbol: str,
    price: float,
    account_state: dict[str, Any],
    style: dict[str, Any],
    exit_plan: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    """Return a zero-safe one-contract affordability decision."""

    raw_price = price
    raw_equity = account_state.get("equity")
    raw_available_margin = account_state.get("available_margin")
    raw_stop_loss_pct = exit_plan.get("stop_loss_pct")
    raw_max_margin_usage = style.get("max_margin_usage", 0.20)
    raw_risk_per_trade = style.get("risk_per_trade", 0.02)
    raw_weight = style.get("weight", 1.0)
    price = _safe_float(raw_price, 0.0)
    equity = _safe_float(account_state.get("equity"), 0.0)
    available_margin = _safe_float(account_state.get("available_margin"), -1.0)
    stop_loss_pct = _safe_float(exit_plan.get("stop_loss_pct"), 0.0)
    max_margin_usage_input = _safe_float(style.get("max_margin_usage"), 0.20)
    risk_per_trade_input = _safe_float(style.get("risk_per_trade"), 0.02)
    weight_input = _safe_float(style.get("weight"), 1.0)
    if (
        price <= 0
        or not all(
            _is_finite_number(value)
            for value in (
                raw_price,
                raw_equity,
                raw_available_margin,
                raw_stop_loss_pct,
                raw_max_margin_usage,
                raw_risk_per_trade,
                raw_weight,
            )
        )
        or equity <= 0
        or available_margin < 0
        or stop_loss_pct <= 0
        or max_margin_usage_input <= 0
        or risk_per_trade_input <= 0
        or weight_input <= 0
    ):
        return _affordability_rejection(
            symbol=symbol,
            reason="missing_contract_risk_inputs",
            counterfactual_only=True,
        )

    if bool(account_state.get("authoritative")):
        has_daily_pnl = (
            "daily_realized_pnl" in account_state or "daily_pnl" in account_state
        )
        required_gate_fields = (
            has_daily_pnl
            and "max_daily_loss" in account_state
            and "consecutive_losses" in account_state
            and "max_consecutive_losses" in account_state
            and "drawdown" in account_state
            and "max_drawdown" in account_state
        )
        if (
            not required_gate_fields
            or _safe_float(account_state.get("max_daily_loss"), 0.0) <= 0
            or _safe_int(account_state.get("max_consecutive_losses"), 0) <= 0
            or _safe_float(account_state.get("max_drawdown"), 0.0) <= 0
        ):
            return _affordability_rejection(
                symbol=symbol,
                reason="missing_account_risk_gates",
                counterfactual_only=True,
            )

    daily_realized_pnl = _safe_float(
        account_state.get("daily_realized_pnl", account_state.get("daily_pnl")),
        0.0,
    )
    max_daily_loss = _safe_float(account_state.get("max_daily_loss"), 0.0)
    gate_numeric_values = [
        account_state.get("daily_realized_pnl", account_state.get("daily_pnl", 0.0)),
        account_state.get("max_daily_loss", 0.0),
        account_state.get("consecutive_losses", 0),
        account_state.get("max_consecutive_losses", 0),
        account_state.get("drawdown", 0.0),
        account_state.get("max_drawdown", 0.0),
    ]
    if any(not _is_finite_number(value) for value in gate_numeric_values):
        return _affordability_rejection(
            symbol=symbol,
            reason="missing_account_risk_gates",
            counterfactual_only=True,
        )
    if max_daily_loss > 0 and daily_realized_pnl <= -abs(max_daily_loss):
        return _affordability_rejection(
            symbol=symbol,
            reason="daily_loss_limit",
            details={
                "daily_realized_pnl": round(daily_realized_pnl, 6),
                "max_daily_loss": round(max_daily_loss, 6),
            },
        )

    consecutive_losses = _safe_int(account_state.get("consecutive_losses"), 0)
    max_consecutive_losses = _safe_int(account_state.get("max_consecutive_losses"), 0)
    if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
        return _affordability_rejection(
            symbol=symbol,
            reason="consecutive_loss_limit",
            details={
                "consecutive_losses": consecutive_losses,
                "max_consecutive_losses": max_consecutive_losses,
            },
        )

    drawdown = _safe_float(account_state.get("drawdown"), 0.0)
    max_drawdown = _safe_float(account_state.get("max_drawdown"), 0.0)
    if max_drawdown > 0 and drawdown >= max_drawdown:
        return _affordability_rejection(
            symbol=symbol,
            reason="maximum_drawdown_limit",
            details={
                "drawdown": round(drawdown, 6),
                "max_drawdown": round(max_drawdown, 6),
            },
        )

    drawdown_tighten = _safe_float(
        account_state.get("drawdown_tighten"),
        max_drawdown * DEFAULT_DRAWDOWN_TIGHTEN_PCT / DEFAULT_MAX_DRAWDOWN_PCT
        if max_drawdown > 0
        else 0.0,
    )
    drawdown_tightened = (
        drawdown_tighten > 0
        and drawdown >= drawdown_tighten
        and (max_drawdown <= 0 or drawdown < max_drawdown)
    )
    risk_multiplier = (
        DEFAULT_DRAWDOWN_TIGHTEN_RISK_MULTIPLIER if drawdown_tightened else 1.0
    )

    can_hold_overnight = session.get("can_hold_overnight")
    if not isinstance(can_hold_overnight, bool):
        return _affordability_rejection(
            symbol=symbol,
            reason="missing_contract_risk_inputs",
            counterfactual_only=True,
        )

    try:
        cost = estimate_order_cost(symbol=symbol, side="buy", quantity=1, price=price)
    except (
        Exception
    ) as exc:  # Contract metadata is an observation hold, not a runner fault.
        return _affordability_rejection(
            symbol=symbol,
            reason="missing_contract_risk_inputs",
            counterfactual_only=True,
            details={"contract_input_error": exc.__class__.__name__},
        )
    rule = cost.rule
    contract_multiplier = _safe_int(getattr(rule, "contract_multiplier", None), 0)
    margin_rate = _safe_float(getattr(rule, "margin_rate", None), -1.0)
    open_fee_rate = _safe_float(getattr(rule, "open_fee_rate", None), -1.0)
    close_fee_rate = _safe_float(getattr(rule, "close_fee_rate", None), -1.0)
    margin_per_lot = _safe_float(getattr(cost, "margin_required", None), -1.0)
    round_trip_fees = _safe_float(getattr(cost, "total_estimated_fee", None), -1.0)
    modeled_gap_pct = _safe_float(
        getattr(rule, "modeled_overnight_gap_pct", None), -1.0
    )
    modeled_slippage_bps = _safe_float(
        getattr(rule, "modeled_slippage_bps", None), -1.0
    )
    if (
        contract_multiplier <= 0
        or margin_rate <= 0
        or margin_per_lot <= 0
        or round_trip_fees < 0
        or open_fee_rate < 0
        or close_fee_rate < 0
        or modeled_slippage_bps < 0
        or (can_hold_overnight and modeled_gap_pct <= 0)
    ):
        return _affordability_rejection(
            symbol=symbol,
            reason="missing_contract_risk_inputs",
            counterfactual_only=True,
        )
    base_max_margin_usage = min(max_margin_usage_input, 0.80)
    max_margin_usage = base_max_margin_usage * risk_multiplier
    risk_per_trade = min(risk_per_trade_input, base_max_margin_usage) * risk_multiplier
    weight = min(weight_input, 1.0)
    stop_loss_pct = max(0.0, stop_loss_pct)
    requested_slippage_bps = _safe_float(
        style.get("slippage_bps", modeled_slippage_bps),
        -1.0,
    )
    if requested_slippage_bps < 0:
        return _affordability_rejection(
            symbol=symbol,
            reason="missing_contract_risk_inputs",
            counterfactual_only=True,
        )
    slippage_bps = max(modeled_slippage_bps, requested_slippage_bps)
    directional_loss_rate = (
        max(stop_loss_pct, modeled_gap_pct) if can_hold_overnight else stop_loss_pct
    )
    modeled_round_trip_slippage = (
        price * contract_multiplier * slippage_bps * 2.0 / 10_000.0
    )
    modeled_loss_per_lot = (
        price * contract_multiplier * directional_loss_rate
        + round_trip_fees
        + modeled_round_trip_slippage
    )
    current_style_margin = max(
        0.0, _safe_float(account_state.get("current_style_margin"), 0.0)
    )
    remaining_style_margin_cap = max(
        0.0, (equity * max_margin_usage) - current_style_margin
    )
    margin_budget = min(
        available_margin,
        equity * risk_per_trade * weight,
        remaining_style_margin_cap,
    )
    loss_budget = equity * risk_per_trade * weight
    quantity_by_margin = (
        int(margin_budget // margin_per_lot) if margin_per_lot > 0 else 0
    )
    quantity_by_loss = (
        int(loss_budget // modeled_loss_per_lot) if modeled_loss_per_lot > 0 else 0
    )
    quantity = min(quantity_by_margin, quantity_by_loss)
    eligible = quantity >= 1
    return {
        "symbol": symbol,
        "quantity": quantity,
        "margin_per_lot": round(margin_per_lot, 6),
        "margin_budget": round(margin_budget, 6),
        "current_style_margin": round(current_style_margin, 6),
        "remaining_style_margin_cap": round(remaining_style_margin_cap, 6),
        "modeled_loss_per_lot": round(modeled_loss_per_lot, 6),
        "directional_loss_rate": round(directional_loss_rate, 8),
        "modeled_overnight_gap_pct": round(modeled_gap_pct, 8)
        if can_hold_overnight
        else None,
        "modeled_slippage_bps": round(slippage_bps, 6),
        "loss_budget": round(loss_budget, 6),
        "eligible": eligible,
        "reason": "eligible" if eligible else "minimum_contract_exceeds_risk_budget",
        "counterfactual_only": False,
        "drawdown_tightened": drawdown_tightened,
        "risk_multiplier": risk_multiplier,
    }


def build_affordability_hold(
    *,
    symbol: str,
    style_name: str,
    size_decision: dict[str, Any],
    cadence: str,
    bar_time: str,
    session: str,
    prediction_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a reviewable risk hold for a rejected size decision."""

    snapshot = dict(prediction_snapshot or {})
    direction = str(snapshot.get("direction") or snapshot.get("side") or "")
    evidence_status = str(snapshot.get("evidence_status") or "incomplete")
    evidence_complete = evidence_status == "complete"
    forward_label_status = str(
        snapshot.get("forward_label_status") or "prediction_evidence_incomplete"
    )
    forward_outcome = (
        dict(snapshot.get("forward_outcome"))
        if isinstance(snapshot.get("forward_outcome"), dict)
        else {
            "status": "prediction_evidence_incomplete",
            "reason": "prediction_evidence_incomplete",
        }
    )
    counterfactual_size_decision = {
        **size_decision,
        "quantity": 0,
        "eligible": False,
        "counterfactual_only": True,
    }
    return {
        "stage": "risk",
        "style": style_name,
        "style_version": str(snapshot.get("style_version") or ""),
        "symbol": symbol,
        "product": _product_or_empty(symbol),
        "cadence": cadence,
        "bar_time": bar_time,
        "session": session,
        "reason": str(
            size_decision.get("reason") or "minimum_contract_exceeds_risk_budget"
        ),
        "action": direction,
        "direction": direction,
        "side": direction,
        "raw_heuristic_score": snapshot.get("raw_heuristic_score"),
        "uncalibrated_confidence_prior": snapshot.get("uncalibrated_confidence_prior"),
        "calibrated_probability": None,
        "probability_model_state": "not_calibrated",
        "entry_price": _safe_float(snapshot.get("entry_price"), 0.0),
        "cluster_id": str(snapshot.get("cluster_id") or ""),
        "cluster_role": str(snapshot.get("cluster_role") or ""),
        "occurrence_index": _safe_int(snapshot.get("occurrence_index"), -1),
        "weight_multiplier": _safe_float(snapshot.get("weight_multiplier"), 0.0),
        "point_in_time_as_of": str(snapshot.get("point_in_time_as_of") or ""),
        "source_event_time": str(snapshot.get("source_event_time") or ""),
        "evidence_envelope": dict(snapshot.get("evidence_envelope") or {}),
        "evidence_envelope_validation": dict(
            snapshot.get("evidence_envelope_validation") or {}
        ),
        "point_in_time_lineage": dict(snapshot.get("point_in_time_lineage") or {}),
        "source_snapshot_id": str(snapshot.get("source_snapshot_id") or ""),
        "source_snapshot_sha256": str(snapshot.get("source_snapshot_sha256") or ""),
        "authority": str(snapshot.get("authority") or ""),
        "lineage_status": str(snapshot.get("lineage_status") or "incomplete"),
        "capital_authority_id": str(snapshot.get("capital_authority_id") or ""),
        "authority_generation": snapshot.get("authority_generation"),
        "execution_lineage_id": str(snapshot.get("execution_lineage_id") or ""),
        "market_regime": str(snapshot.get("market_regime") or ""),
        "mg_on": bool(snapshot.get("mg_on")),
        "mg_enabled": bool(snapshot.get("mg_on")),
        "holding_horizon": dict(snapshot.get("holding_horizon") or {}),
        "prediction": snapshot if evidence_complete else {},
        "prediction_snapshot": snapshot,
        "prediction_evidence_status": evidence_status,
        "prediction_evidence_reason": str(
            snapshot.get("evidence_reason") or "prediction_evidence_incomplete"
        ),
        "label_eligible": evidence_complete,
        "label_status": forward_label_status,
        "forward_label_status": forward_label_status,
        "forward_outcome": forward_outcome,
        "scenario_tags": dict(snapshot.get("scenario_tags") or {}),
        "execution_class": "counterfactual_only",
        "execution_eligible": False,
        "counterfactual_only": True,
        "sample_intent": "counterfactual",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real": False,
        "real_trading_enabled": False,
        "size_decision": counterfactual_size_decision,
    }


def _compute_position_pnl_summary(signals_dir: Path) -> dict[str, dict[str, Any]]:
    """Compute realized + mark-to-market unrealized PnL per style from the position snapshot."""
    snapshot = _read_position_snapshot(signals_dir)
    summary: dict[str, dict[str, Any]] = {}
    for position in snapshot.get("positions", []):
        if not isinstance(position, dict):
            continue
        style_name = str(
            position.get("style") or position.get("strategy_name") or "unknown"
        )
        net_qty = _safe_int(position.get("net_qty"), 0)
        avg_price = _safe_float(position.get("avg_price"), 0.0)
        mark_price = _safe_float(position.get("mark_price"), avg_price)
        multiplier = _safe_int(position.get("contract_multiplier"), 1)
        realized = _safe_float(position.get("realized_pnl"), 0.0)
        unrealized = round((mark_price - avg_price) * net_qty * multiplier, 6)
        item = summary.setdefault(
            style_name,
            {
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_pnl": 0.0,
                "open_position_count": 0,
            },
        )
        item["realized_pnl"] = round(item["realized_pnl"] + realized, 6)
        item["unrealized_pnl"] = round(item["unrealized_pnl"] + unrealized, 6)
        item["total_pnl"] = round(item["realized_pnl"] + item["unrealized_pnl"], 6)
        item["open_position_count"] += 1
    return summary


def _signal_card(
    *,
    date: str,
    cadence: str,
    latest_bar_time: str,
    style_name: str,
    symbol: str,
    order: dict[str, Any],
    receipt: dict[str, Any],
    signal: dict[str, Any],
) -> dict[str, Any]:
    order_id = str(order["order_id"])
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    return {
        "order_id": order_id,
        "ts_code": symbol,
        "symbol": symbol,
        "market": MARKET,
        "reader_market": READER_MARKET,
        "direction": order["side"],
        "side": order["side"],
        "quantity": _safe_int(order.get("quantity"), 0),
        "price": _safe_float(order.get("price"), 0.0),
        "trigger_price": _safe_float(order.get("price"), 0.0),
        "status": "pending",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "account": f"cn_futures_sim_{style_name}",
        "strategy_name": style_name,
        "manual_confirm_required": False,
        "direct_execution": False,
        "real_trading_enabled": False,
        "valid_until": date,
        "timestamp": _now_iso(),
        "idempotency_key": order_id,
        "order_intent": str(order.get("intent") or "open"),
        "position_effect": str(order.get("position_effect") or ""),
        "source": "cn_futures_multi_style_simulation",
        "broker_contract": str(
            receipt.get("broker_contract")
            or _sim_executor.PAPER_BROKER_CONTRACT
        ),
        "authority_id": str(
            receipt.get("authority_id") or _sim_executor.SIM_AUTHORITY_ID
        ),
        "cadence": cadence,
        "bar_time": latest_bar_time,
        "signal": signal,
        "margin_required": raw.get("margin_required"),
        "fee": receipt.get("fee"),
    }


def _write_filled_signal(
    signals_dir: Path, card: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    machine = SignalStateMachine(signals_dir)
    try:
        machine.write_pending(card)
    except SignalStateConflict:
        return {"status": "duplicate", "order_id": card.get("order_id", "")}
    machine.claim(str(card["order_id"]), worker_id="cn_futures_sim")
    machine.mark_running(str(card["order_id"]), worker_id="cn_futures_sim")
    fill_info = {
        "filled_price": receipt.get("avg_price", card.get("price", 0.0)),
        "filled_quantity": receipt.get("filled_qty", card.get("quantity", 0)),
        "filled_qty": receipt.get("filled_qty", card.get("quantity", 0)),
        "fee": receipt.get("fee", 0.0),
        "fill_time": _now_iso(),
        "raw_response": receipt.get("raw_response", {}),
    }
    partial = str(receipt.get("status") or "").lower().strip() == "partial"
    filled = machine.fill(str(card["order_id"]), fill_info, partial=partial)
    order_event_result = record_local_sim_order_lifecycle(
        signals_dir,
        card=card,
        receipt=receipt,
        final_card=filled.get("signal_card") or {},
    )
    return {
        "status": "partial" if partial else "filled",
        "order_id": card.get("order_id", ""),
        "filled_signal": filled,
        "order_event_result": order_event_result,
    }


def run_multi_style_simulation(
    adapter: CNFuturesAdapter,
    date: str,
    reader: Any,
    *,
    signals_dir: Path,
    review_path: Path | None = None,
    cadence: str = INTRADAY_INTERVAL,
    now: datetime | None = None,
    max_intraday_bar_age_minutes: float = DEFAULT_MAX_INTRADAY_BAR_AGE_MINUTES,
) -> dict[str, Any]:
    """Run all configured futures styles through simulated execution."""

    cadence_value = (
        "daily"
        if str(cadence or "").lower() in {"daily", "1d", "day"}
        else INTRADAY_INTERVAL
    )
    if now is None:
        now = datetime.now()
    cn_now = (
        now.astimezone(CN_TZ) if now.tzinfo is not None else now.replace(tzinfo=CN_TZ)
    )
    if cadence_value != "daily":
        resolved_trade_date = _exchange_trade_date(
            cn_now,
            requested_date=date,
        )
        if resolved_trade_date:
            date = resolved_trade_date
    market_data_date = cn_now.strftime("%Y%m%d")
    order_projection_reconcile = startup_reconcile_order_projection(signals_dir)
    capital_outbox_replay = _replay_cn_futures_capital_actions(signals_dir)
    config = adapter.get_strategy_config()
    styles = config.get("styles") if isinstance(config.get("styles"), dict) else {}
    try:
        provider_state = get_cn_futures_capital_provider_state(trade_date=date)
    except Exception:
        provider_state = None
        authoritative_state = None
        authority_status = "market_capital_provider_error"
    else:
        authoritative_state, authority_status = _validate_market_capital_provider_state(
            provider_state,
            trade_date=date,
        )
    if int(capital_outbox_replay.get("pending_count", 0)) > 0:
        authoritative_state = None
        authority_status = "capital_outbox_pending"
    # Adapter account dictionaries are strategy compatibility inputs, never
    # capital authority.  Even counterfactual sizing must remain anchored to
    # the canonical 50k policy instead of reflecting a forged adapter balance.
    fallback_capital = default_sim_capital("cn_futures")
    capital = (
        float(authoritative_state["initial_equity_cny"])
        if authoritative_state is not None
        else fallback_capital
    )
    account = {"sim_capital": capital}
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    cluster_state = load_review_cluster_state(review_path or DEFAULT_REVIEW_PATH)
    position_snapshot = _read_position_snapshot(signals_dir)
    account_ledger, account_ledger_path = _load_sim_account_ledger(
        account=account,
        capital=capital,
        date=date,
        review_path=review_path,
        authoritative_state=authoritative_state,
        authority_status=authority_status,
    )
    account_state = _current_account_state(
        account=account,
        ledger=account_ledger,
        position_snapshot=position_snapshot,
    )
    contract_affordability: list[dict[str, Any]] = []
    session_bucket = _session_bucket(now)
    if cadence_value == "daily":
        universe = adapter.get_universe(date)
    else:
        get_intraday_universe = getattr(adapter, "get_intraday_universe", None)
        universe = (
            get_intraday_universe(market_data_date, interval=INTRADAY_INTERVAL)
            if callable(get_intraday_universe)
            else adapter.get_universe(date)
        )
        session_bucket = _aggregate_session_bucket(now, list(universe or []))
    if not universe:
        errors.append(
            {"stage": "universe", "market": MARKET, "error": "empty_futures_universe"}
        )
    if not styles:
        errors.append(
            {"stage": "strategy", "market": MARKET, "error": "empty_strategy_styles"}
        )
    if cadence_value != "daily" and session_bucket == "closed":
        position_pnl_summary = _compute_position_pnl_summary(signals_dir)
        return {
            "market": MARKET,
            "reader_market": READER_MARKET,
            "date": date,
            "cadence": cadence_value,
            "session": session_bucket,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "state": "market_closed",
            "universe_count": len(universe),
            "style_count": len(styles),
            "record_count": 0,
            "filled_count": 0,
            "records": [],
            "errors": [],
            "holds": [],
            "hold_count": 0,
            "hold_reason_summary": {},
            "review": {
                "state": "market_closed",
                "append_skipped": True,
                "reason": "closed_session_empty_review_not_persisted",
                "position_pnl_summary": position_pnl_summary,
            },
            "real_trading_enabled": False,
            "generated_at": _now_iso(),
            "max_intraday_bar_age_minutes": max_intraday_bar_age_minutes,
            "capital_outbox": capital_outbox_replay,
        }

    configured_min_products = max(
        1,
        _safe_int(
            getattr(adapter, "universe_filter", {}).get("min_distinct_products"), 3
        ),
    )
    max_symbols = max(
        1, _safe_int(getattr(adapter, "universe_filter", {}).get("max_symbols"), 30)
    )
    required_min_products = min(configured_min_products, max_symbols)
    distinct_products = _distinct_products(universe)
    if len(distinct_products) < required_min_products:
        holds.append(
            {
                "stage": "universe",
                "style": "",
                "symbol": "",
                "product": "",
                "cadence": cadence_value,
                "bar_time": "",
                "session": session_bucket,
                "reason": "insufficient_distinct_product_coverage",
                "distinct_products": distinct_products,
                "required_min_distinct_products": required_min_products,
            }
        )
        position_pnl_summary = _compute_position_pnl_summary(signals_dir)
        affordability = _build_affordability_report(
            date=date,
            raw_distinct_products=distinct_products,
            contracts=[],
            account_state=account_state,
        )
        review = append_review(
            date=date,
            market=MARKET,
            records=[],
            errors=[],
            holds=holds,
            path=review_path,
            position_pnl_summary=position_pnl_summary,
            affordability=affordability,
        )
        _write_json_atomic(
            _runtime_output_path(review_path, AFFORDABILITY_FILENAME),
            affordability,
        )
        return {
            "market": MARKET,
            "reader_market": READER_MARKET,
            "date": date,
            "cadence": cadence_value,
            "session": session_bucket,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "state": "observation_only",
            "universe_count": len(universe),
            "distinct_products": distinct_products,
            "distinct_product_count": len(distinct_products),
            "required_min_distinct_products": required_min_products,
            "style_count": len(styles),
            "record_count": 0,
            "filled_count": 0,
            "records": [],
            "errors": [],
            "holds": holds,
            "hold_count": len(holds),
            "latest_hold_bar_time": "",
            "hold_reason_summary": review.get("hold_reason_summary", {}),
            "review": review,
            "affordability": affordability,
            "real_trading_enabled": False,
            "generated_at": _now_iso(),
            "max_intraday_bar_age_minutes": max_intraday_bar_age_minutes,
            "capital_outbox": capital_outbox_replay,
        }

    for style_name, style_config in styles.items():
        style = dict(style_config or {})
        style.setdefault("name", style_name)
        if not _style_is_active(style):
            reason = _inactive_style_reason(style) or "style_inactive"
            holds.append(
                {
                    "stage": "style",
                    "style": style_name,
                    "symbol": "",
                    "product": "",
                    "cadence": cadence_value,
                    "bar_time": "",
                    "session": session_bucket,
                    "reason": reason,
                    "confidence": 0.0,
                    "evolution_action": style.get("evolution_action", ""),
                    "evolution_reason": style.get("evolution_reason", ""),
                }
            )
            continue
        if not _style_allows_session(style, now):
            holds.append(
                {
                    "stage": "style",
                    "style": style_name,
                    "symbol": "",
                    "product": "",
                    "cadence": cadence_value,
                    "bar_time": "",
                    "session": session_bucket,
                    "reason": "style_session_not_allowed",
                    "confidence": 0.0,
                }
            )
            continue
        for symbol in universe:
            if not _style_allows_symbol(style, symbol):
                continue
            contract_session_bucket = _session_bucket(now, symbol=symbol)
            if cadence_value != "daily" and contract_session_bucket == "closed":
                holds.append(
                    {
                        "record_type": "risk_reject",
                        "stage": "session",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": cadence_value,
                        "bar_time": "",
                        "session": session_bucket,
                        "contract_session": contract_session_bucket,
                        "reason": "contract_session_closed",
                        "execution_class": "counterfactual_only",
                        "counterfactual_only": True,
                        "sample_intent": "counterfactual",
                        "label_status": "not_due",
                        "real_trading_enabled": False,
                    }
                )
                continue
            bars, bar_cadence, latest_bar_time = _bars_for_cadence(
                reader, symbol, market_data_date, cadence_value
            )
            if not bars:
                missing_reason = (
                    "missing_intraday_bars"
                    if cadence_value != "daily"
                    else "missing_daily_bars"
                )
                contract_affordability.append(
                    _pre_sizing_affordability_rejection(
                        style_name=style_name,
                        symbol=symbol,
                        reason=missing_reason,
                        cadence=bar_cadence,
                    )
                )
                errors.append(
                    {
                        "stage": "data",
                        "symbol": symbol,
                        "style": style_name,
                        "cadence": cadence_value,
                        "error": missing_reason,
                    }
                )
                holds.append(
                    {
                        "record_type": "risk_reject",
                        "stage": "data",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": "",
                        "session": session_bucket,
                        "reason": missing_reason,
                        "execution_class": "counterfactual_only",
                        "counterfactual_only": True,
                        "sample_intent": "counterfactual",
                        "label_status": "rejected_data_unreliable",
                        "real_trading_enabled": False,
                    }
                )
                continue
            positions = _positions_by_key(position_snapshot)
            existing_position = positions.get(_position_key(style_name, symbol))
            force_flatten = bool(existing_position) and _should_flatten_no_overnight(
                style, now
            )
            if cadence_value != "daily":
                fresh, age_minutes = _is_intraday_bar_fresh(
                    latest_bar_time,
                    now=now,
                    max_age_minutes=max_intraday_bar_age_minutes,
                )
                if not fresh:
                    if _is_after_product_night_close(symbol, latest_bar_time, now):
                        contract_affordability.append(
                            _pre_sizing_affordability_rejection(
                                style_name=style_name,
                                symbol=symbol,
                                reason="product_night_session_closed",
                                cadence=bar_cadence,
                                bar_time=latest_bar_time,
                                details={"bar_age_minutes": age_minutes},
                            )
                        )
                        holds.append(
                            {
                                "stage": "data",
                                "symbol": symbol,
                                "product": _product_or_empty(symbol),
                                "style": style_name,
                                "cadence": cadence_value,
                                "bar_time": latest_bar_time,
                                "bar_age_minutes": age_minutes,
                                "max_age_minutes": max_intraday_bar_age_minutes,
                                "session": session_bucket,
                                "reason": "product_night_session_closed",
                            }
                        )
                        continue
                    contract_affordability.append(
                        _pre_sizing_affordability_rejection(
                            style_name=style_name,
                            symbol=symbol,
                            reason="stale_intraday_bar",
                            cadence=bar_cadence,
                            bar_time=latest_bar_time,
                            details={
                                "bar_age_minutes": age_minutes,
                                "max_age_minutes": max_intraday_bar_age_minutes,
                            },
                        )
                    )
                    errors.append(
                        {
                            "stage": "data",
                            "symbol": symbol,
                            "style": style_name,
                            "cadence": cadence_value,
                            "bar_time": latest_bar_time,
                            "bar_age_minutes": age_minutes,
                            "max_age_minutes": max_intraday_bar_age_minutes,
                            "error": "stale_intraday_bar",
                        }
                    )
                    holds.append(
                        {
                            "record_type": "risk_reject",
                            "stage": "data",
                            "symbol": symbol,
                            "product": _product_or_empty(symbol),
                            "style": style_name,
                            "cadence": bar_cadence,
                            "bar_time": latest_bar_time,
                            "bar_age_minutes": age_minutes,
                            "max_age_minutes": max_intraday_bar_age_minutes,
                            "session": session_bucket,
                            "reason": "stale_intraday_bar",
                            "execution_class": "counterfactual_only",
                            "counterfactual_only": True,
                            "sample_intent": "counterfactual",
                            "label_status": "rejected_data_unreliable",
                            "real_trading_enabled": False,
                        }
                    )
                    continue
            rollover_blocked, days_to_contract_month = _contract_inside_rollover_guard(
                symbol, date, style
            )
            if rollover_blocked and not existing_position:
                contract_affordability.append(
                    _pre_sizing_affordability_rejection(
                        style_name=style_name,
                        symbol=symbol,
                        reason="contract_rollover_guard",
                        cadence=bar_cadence,
                        bar_time=latest_bar_time,
                        details={
                            "days_to_contract_month_start": days_to_contract_month
                        },
                    )
                )
                holds.append(
                    {
                        "stage": "risk",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": cadence_value,
                        "days_to_contract_month_start": days_to_contract_month,
                        "session": session_bucket,
                        "reason": "contract_rollover_guard",
                    }
                )
                continue
            if force_flatten and existing_position:
                flatten_side = _opposite_side_for_position(existing_position)
                signal = {
                    "action": flatten_side,
                    "side": flatten_side,
                    "price": _safe_float((bars[-1] if bars else {}).get("close"), 0.0),
                    "reason": "flatten_no_overnight",
                }
            else:
                signal = generate_style_signal(symbol, bars, style)
            if signal.get("action") == "hold":
                contract_affordability.append(
                    {
                        "style": style_name,
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "eligible": False,
                        "quantity": 0,
                        "reason": str(signal.get("reason") or "signal_hold"),
                        "counterfactual_only": False,
                        "reduce_only": False,
                        "execution_class": "not_assessed",
                        "assessment_status": "not_assessed",
                        "size_decision": {},
                    }
                )
                holds.append(
                    {
                        "stage": "signal",
                        "style": style_name,
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "session": session_bucket,
                        "reason": str(signal.get("reason") or "hold"),
                        "confidence": signal.get("confidence", 0.0),
                    }
                )
                continue
            exit_plan = _exit_plan_for_signal(signal, style)
            scenario_tags = _scenario_tags(symbol, signal, now)

            # --- PIT authority -------------------------------------------------
            resolved_authority = (
                "market_capital_ledger" if authoritative_state is not None else ""
            )

            forward_outcome = _forward_outcome_label(
                bars,
                signal,
                exit_plan,
                point_in_time_as_of=latest_bar_time,
            )
            source_identity = _explicit_source_identity(reader, bars)
            prediction_snapshot = _prediction_snapshot_before_risk(
                style_name=style_name,
                style=style,
                signal=signal,
                scenario_tags=scenario_tags,
                exit_plan=exit_plan,
                forward_outcome=forward_outcome,
                bar_time=latest_bar_time,
                authority=resolved_authority,
                symbol=symbol,
                source_name=source_identity,
                source_cadence=bar_cadence,
                source_bars=bars,
            )
            prediction_snapshot.update(
                {
                    "capital_authority_id": str(
                        (authoritative_state or {}).get("authority_id") or ""
                    ),
                    "authority_generation": (
                        (authoritative_state or {}).get("authority_generation")
                    ),
                    "execution_lineage_id": str(
                        (authoritative_state or {}).get("execution_lineage_id") or ""
                    ),
                }
            )
            cluster_authority = (
                "cn-futures-capital-v1:"
                + str((authoritative_state or {}).get("authority_generation") or "")
                + ":"
                + str((authoritative_state or {}).get("execution_lineage_id") or "")
                if authoritative_state is not None
                else "unverified:"
                + str(prediction_snapshot.get("source_snapshot_sha256") or "")
            )
            cluster_id = _compute_cluster_id(
                authority=cluster_authority,
                symbol=symbol,
                style_version=prediction_snapshot["style_version"],
                side=prediction_snapshot["side"],
                bar_time=latest_bar_time,
            )
            cluster_info = _classify_cluster_occurrence(
                cluster_state,
                cluster_id,
                is_execution_eligible=False,
            )
            cluster_fields = {
                "cluster_id": cluster_id,
                "cluster_role": cluster_info["cluster_role"],
                "occurrence_index": cluster_info["occurrence_index"],
                "weight_multiplier": cluster_info["weight_multiplier"],
            }
            prediction_snapshot.update(cluster_fields)
            price = _safe_float(signal.get("price"), 0.0)
            if price <= 0:
                contract_affordability.append(
                    _pre_sizing_affordability_rejection(
                        style_name=style_name,
                        symbol=symbol,
                        reason="invalid_price",
                        cadence=bar_cadence,
                        bar_time=latest_bar_time,
                    )
                )
                errors.append(
                    {
                        "stage": "signal",
                        "symbol": symbol,
                        "style": style_name,
                        "error": "invalid_price",
                    }
                )
                holds.append(
                    {
                        "record_type": "risk_reject",
                        "stage": "data",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "session": session_bucket,
                        "reason": "invalid_price",
                        "execution_class": "counterfactual_only",
                        "counterfactual_only": True,
                        "sample_intent": "counterfactual",
                        "label_status": "rejected_data_unreliable",
                        "real_trading_enabled": False,
                    }
                )
                continue
            side = str(signal.get("side", signal.get("action", "buy")))
            existing_qty = _safe_int((existing_position or {}).get("net_qty"), 0)
            opposite_existing = existing_qty != 0 and (existing_qty > 0) != (
                _side_sign(side) > 0
            )
            reduce_only = bool(existing_position) and (
                force_flatten or opposite_existing
            )
            try:
                if reduce_only:
                    rule = get_contract_rule(symbol)
                    if authoritative_state is None:
                        quantity = 0
                        size_decision = {
                            "symbol": symbol,
                            "quantity": 0,
                            "margin_per_lot": 0.0,
                            "margin_budget": 0.0,
                            "modeled_loss_per_lot": 0.0,
                            "loss_budget": 0.0,
                            "eligible": False,
                            "reason": "account_state_unavailable",
                            "authority_status": authority_status,
                            "counterfactual_only": True,
                            "reduce_only": True,
                        }
                        capital_error = {
                            "stage": "capital",
                            "market": MARKET,
                            "symbol": symbol,
                            "style": style_name,
                            "error": (
                                "capital_provider_unavailable:"
                                f"{authority_status}"
                            ),
                        }
                        if capital_error not in errors:
                            errors.append(capital_error)
                    else:
                        quantity = abs(existing_qty)
                        size_decision = {
                            "symbol": symbol,
                            "quantity": quantity,
                            "margin_per_lot": 0.0,
                            "margin_budget": 0.0,
                            "modeled_loss_per_lot": 0.0,
                            "loss_budget": 0.0,
                            "eligible": quantity > 0,
                            "reason": "reduce_only_existing_position",
                            "counterfactual_only": False,
                            "reduce_only": True,
                        }
                else:
                    account_state = _current_account_state(
                        account=account,
                        ledger=account_ledger,
                        position_snapshot=position_snapshot,
                    )
                    sizing_account_state = {
                        **account_state,
                        "current_style_margin": _style_margin_used(
                            position_snapshot, style_name
                        ),
                    }
                    size_decision = quantity_for_style_decision(
                        symbol=symbol,
                        price=price,
                        account_state=sizing_account_state,
                        style=style,
                        exit_plan=exit_plan,
                        session={
                            "can_hold_overnight": not bool(
                                exit_plan.get("no_overnight")
                            )
                        },
                    )
                    if not bool(account_state.get("authoritative")):
                        size_decision = {
                            **size_decision,
                            "quantity": 0,
                            "eligible": False,
                            "reason": "account_state_unavailable",
                            "counterfactual_only": True,
                            "counterfactual_eligible": bool(
                                size_decision.get("eligible")
                            ),
                            "counterfactual_reason": size_decision.get("reason"),
                            "account_history_status": account_state.get(
                                "history_status"
                            ),
                        }
                    quantity = _safe_int(size_decision.get("quantity"), 0)
            except Exception as exc:
                errors.append(
                    {
                        "stage": "risk",
                        "symbol": symbol,
                        "style": style_name,
                        "error": str(exc),
                    }
                )
                holds.append(
                    {
                        "record_type": "risk_reject",
                        "stage": "risk",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "session": session_bucket,
                        "reason": "risk_sizing_error",
                        "error_type": type(exc).__name__,
                        "execution_class": "counterfactual_only",
                        "counterfactual_only": True,
                        "sample_intent": "counterfactual",
                        "real_trading_enabled": False,
                    }
                )
                continue
            if not bool(size_decision.get("eligible")):
                size_decision = {
                    **size_decision,
                    "quantity": 0,
                    "eligible": False,
                    "counterfactual_only": True,
                }
                quantity = 0
            affordability_row = {
                "style": style_name,
                "style_version": prediction_snapshot["style_version"],
                "symbol": symbol,
                "product": _product_or_empty(symbol),
                "eligible": bool(size_decision.get("eligible")),
                "quantity": quantity,
                "reason": str(size_decision.get("reason") or "unknown"),
                "counterfactual_only": bool(
                    size_decision.get("counterfactual_only")
                    or not size_decision.get("eligible")
                ),
                "reduce_only": reduce_only,
                "execution_class": "reduce_only"
                if reduce_only
                else (
                    "counterfactual_only"
                    if size_decision.get("counterfactual_only")
                    or not size_decision.get("eligible")
                    else "new_position"
                ),
                "direction": prediction_snapshot["direction"],
                "side": prediction_snapshot["side"],
                "raw_heuristic_score": prediction_snapshot["raw_heuristic_score"],
                "uncalibrated_confidence_prior": prediction_snapshot[
                    "uncalibrated_confidence_prior"
                ],
                "calibrated_probability": None,
                "probability_model_state": "not_calibrated",
                "market_regime": prediction_snapshot["market_regime"],
                "mg_on": prediction_snapshot["mg_on"],
                "holding_horizon": prediction_snapshot["holding_horizon"],
                "prediction_evidence_status": prediction_snapshot["evidence_status"],
                "forward_label_status": prediction_snapshot["forward_label_status"],
                "prediction_snapshot": prediction_snapshot,
                **cluster_fields,
                "real": False,
                "real_trading_enabled": False,
                "size_decision": size_decision,
            }
            if not bool(size_decision.get("eligible")):
                contract_affordability.append(affordability_row)
                holds.append(
                    build_affordability_hold(
                        symbol=symbol,
                        style_name=style_name,
                        size_decision=size_decision,
                        cadence=bar_cadence,
                        bar_time=latest_bar_time,
                        session=session_bucket,
                        prediction_snapshot=prediction_snapshot,
                    )
                )
                continue
            if not reduce_only:
                rule = get_contract_rule(symbol)
            period_key = _order_period_key(date, bar_cadence, latest_bar_time)
            intent = (
                "flatten_no_overnight"
                if force_flatten
                else ("reduce_only" if reduce_only else "open")
            )
            suffix = "-flatten" if force_flatten else ""
            order_id = f"SIM-CNF-{style_name}-{symbol}-{period_key}{suffix}".replace(
                "/", "-"
            )
            order = {
                "order_id": order_id,
                "symbol": symbol,
                "ts_code": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "strategy_name": style_name,
                "market": MARKET,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "account_id": "cn_futures_sim",
                "broker_contract": _sim_executor.PAPER_BROKER_CONTRACT,
                "authority_id": str(
                    (authoritative_state or {}).get("authority_id") or ""
                ),
                "authority_generation": (
                    (authoritative_state or {}).get("authority_generation")
                ),
                "decision_time": cn_now.isoformat(timespec="seconds"),
                "contract_multiplier": rule.contract_multiplier,
                "cadence": bar_cadence,
                "bar_time": latest_bar_time,
                "intent": intent,
                "position_effect": "close" if reduce_only else "open",
                "bar_volume": _safe_float(
                    (bars[-1] if bars else {}).get("volume"), 0.0
                ),
                "previous_close": _safe_float(
                    (bars[-2] if len(bars) >= 2 else {}).get("close"), price
                ),
                "trade_date": date,
                "scenario_tags": scenario_tags,
                "exit_plan": exit_plan,
                "size_decision": size_decision,
            }
            _enrich_order_from_bar(order, bars[-1] if bars else {})
            if not bool(order_projection_reconcile.get("ready")):
                halted_decision = {
                    **size_decision,
                    "quantity": 0,
                    "eligible": False,
                    "reason": "order_projection_reconcile_halted",
                    "counterfactual_only": True,
                    "counterfactual_eligible": True,
                    "order_projection_reconcile": order_projection_reconcile,
                }
                contract_affordability.append(
                    {
                        **affordability_row,
                        "eligible": False,
                        "quantity": 0,
                        "reason": "order_projection_reconcile_halted",
                        "counterfactual_only": True,
                        "execution_class": "counterfactual_only",
                        "size_decision": halted_decision,
                    }
                )
                holds.append(
                    build_affordability_hold(
                        symbol=symbol,
                        style_name=style_name,
                        size_decision=halted_decision,
                        cadence=bar_cadence,
                        bar_time=latest_bar_time,
                        session=session_bucket,
                        prediction_snapshot=prediction_snapshot,
                    )
                )
                continue
            if not reduce_only and _has_repeated_same_side_exposure(
                signals_dir,
                date=date,
                style_name=style_name,
                symbol=symbol,
                side=str(order["side"]),
            ):
                repeated_decision = {
                    **size_decision,
                    "quantity": 0,
                    "eligible": False,
                    "reason": "repeated_same_side_exposure",
                }
                contract_affordability.append(
                    {
                        **affordability_row,
                        "eligible": False,
                        "quantity": 0,
                        "reason": "repeated_same_side_exposure",
                        "size_decision": repeated_decision,
                    }
                )
                holds.append(
                    {
                        "stage": "risk",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "side": order["side"],
                        "reason": "repeated_same_side_exposure",
                    }
                )
                continue
            capital_reservation: dict[str, Any] = {}
            if not reduce_only:
                reservation_base_price = max(
                    price,
                    _safe_float(order.get("ask_price"), 0.0),
                    _safe_float(order.get("bid_price"), 0.0),
                )
                reservation_slippage_bps = max(
                    _safe_float(getattr(rule, "modeled_slippage_bps", 0.0), 0.0),
                    _safe_float(style.get("slippage_bps"), 2.0),
                )
                reservation_tick = max(
                    _safe_float(getattr(rule, "tick_size", 0.0), 0.0),
                    0.000001,
                )
                reservation_price = reservation_base_price + max(
                    reservation_tick,
                    reservation_base_price * reservation_slippage_bps / 10_000.0,
                )
                projected_cost = estimate_order_cost(
                    symbol=symbol,
                    side=str(order["side"]),
                    quantity=quantity,
                    price=reservation_price,
                )
                capital_reservation = _reserve_cn_futures_market_margin(
                    reference_id=order_id,
                    risk_unit_key=symbol,
                    worst_case_amount_cny=float(projected_cost.margin_required),
                    trade_date=date,
                    point_in_time_as_of=_aware_cn_timestamp(latest_bar_time),
                    lineage_sha256=str(
                        prediction_snapshot.get("source_snapshot_sha256") or ""
                    ),
                    execution_lineage_id=str(
                        (authoritative_state or {}).get("execution_lineage_id") or ""
                    ),
                    authority_id=str(
                        (authoritative_state or {}).get("authority_id") or ""
                    ),
                    authority_generation=(authoritative_state or {}).get(
                        "authority_generation"
                    ),
                    worst_case_fee_cash_cny=float(projected_cost.open_fee),
                )
                if not bool(capital_reservation.get("approved")):
                    reservation_reason = str(
                        capital_reservation.get("reason")
                        or "market_capital_unavailable"
                    )
                    rejected_decision = {
                        **size_decision,
                        "quantity": 0,
                        "eligible": False,
                        "reason": reservation_reason,
                        "cn_futures_capital_reservation": capital_reservation,
                    }
                    contract_affordability.append(
                        {
                            **affordability_row,
                            "eligible": False,
                            "quantity": 0,
                            "reason": reservation_reason,
                            "size_decision": rejected_decision,
                        }
                    )
                    holds.append(
                        {
                            "stage": "capital",
                            "symbol": symbol,
                            "product": _product_or_empty(symbol),
                            "style": style_name,
                            "cadence": bar_cadence,
                            "bar_time": latest_bar_time,
                            "session": session_bucket,
                            "reason": reservation_reason,
                            "cn_futures_capital_reservation": capital_reservation,
                        }
                    )
                    continue
                order["cn_futures_capital_reservation"] = capital_reservation
                order["cn_futures_capital_reservation_id"] = capital_reservation[
                    "reservation_id"
                ]
                order["cn_futures_capital_event_id"] = capital_reservation["event_id"]
                order["capital_commit_mode"] = "atomic"
            previous_opposite = _latest_opposite_fill(
                signals_dir,
                date=date,
                style_name=style_name,
                symbol=symbol,
                side=str(order["side"]),
            )
            sim_account: dict[str, Any] = {
                "account_id": "cn_futures_sim",
                "market": MARKET,
                "broker_contract": _sim_executor.PAPER_BROKER_CONTRACT,
                "authority_id": str(
                    (authoritative_state or {}).get("authority_id") or ""
                ),
                "authority_generation": (
                    (authoritative_state or {}).get("authority_generation")
                ),
                "capital_layer": "simulated",
                "account_type": "simulated",
            }
            if reduce_only:
                sim_account["position_snapshot"] = {
                    "snapshot_id": str(
                        position_snapshot.get("payload_sha256") or ""
                    ),
                    "as_of": str(
                        position_snapshot.get("updated_at")
                        or position_snapshot.get("trade_date")
                        or ""
                    ),
                    "authority_id": _sim_executor.SIM_AUTHORITY_ID,
                    "authority_generation": (
                        (authoritative_state or {}).get("authority_generation")
                    ),
                    "broker_contract": _sim_executor.PAPER_BROKER_CONTRACT,
                    "positions": [
                        {
                            "symbol": symbol,
                            "position_side": _position_side(existing_qty),
                            "total_qty": abs(existing_qty),
                        }
                    ],
                }
            try:
                receipt_obj = execute_sim_order(
                    order=order,
                    market=MARKET,
                    account=sim_account,
                    config={
                        "fee_mode": "round_trip_estimate",
                        "style": style_name,
                        "slippage_bps": _safe_float(style.get("slippage_bps"), 2.0),
                        "volume_participation": _safe_float(
                            style.get("volume_participation"), 0.05
                        ),
                        "rollover_min_days_to_expiry": _safe_int(
                            style.get("rollover_min_days_to_expiry"), 0
                        ),
                    },
                )
            except Exception as exc:
                capital_release: dict[str, Any] = {}
                if capital_reservation:
                    capital_release = _release_via_capital_outbox(
                        signals_dir,
                        reservation_id=str(
                            capital_reservation.get("reservation_id") or ""
                        ),
                        amount_cny=_safe_float(
                            capital_reservation.get("amount_cny"), 0.0
                        ),
                        reason="sim_executor_exception",
                        reference_id=f"{order_id}:executor-exception",
                    )
                errors.append(
                    {
                        "stage": "execution",
                        "symbol": symbol,
                        "style": style_name,
                        "error": "sim_executor_exception",
                        "error_type": type(exc).__name__,
                    }
                )
                holds.append(
                    {
                        "stage": "execution",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "session": session_bucket,
                        "reason": "sim_executor_exception",
                        "error_type": type(exc).__name__,
                        "cn_futures_capital_release": capital_release,
                    }
                )
                contract_affordability.append(
                    {
                        **affordability_row,
                        "eligible": False,
                        "quantity": 0,
                        "reason": "sim_executor_exception",
                    }
                )
                continue
            receipt = {
                "status": receipt_obj.status,
                "filled_qty": receipt_obj.filled_qty,
                "avg_price": receipt_obj.avg_price,
                "fee": receipt_obj.fee,
                "message": receipt_obj.message,
                "capital_layer": receipt_obj.capital_layer,
                "account_type": receipt_obj.account_type,
                "real_trading_enabled": False,
                "order_id": receipt_obj.order_id,
                "market": receipt_obj.market,
                "broker_contract": receipt_obj.broker_contract,
                "authority_id": receipt_obj.authority_id,
                "authority_generation": receipt_obj.authority_generation,
                "raw_response": receipt_obj.raw_response,
            }
            # PIT lineage flag on the receipt itself
            receipt["pit_lineage_complete"] = bool(
                prediction_snapshot.get("lineage_status") == "complete"
                and prediction_snapshot.get("authority") == "market_capital_ledger"
                and prediction_snapshot.get("source_snapshot_sha256")
            )
            receipt["execution_eligible"] = _receipt_execution_eligible(receipt)
            receipt["execution_class"] = (
                "execution_eligible"
                if receipt["execution_eligible"]
                else "unverified_simulated_fill"
            )
            receipt["counterfactual_only"] = False
            if str(receipt.get("status") or "").lower() not in {"filled", "partial"}:
                raw_response = (
                    receipt.get("raw_response")
                    if isinstance(receipt.get("raw_response"), dict)
                    else {}
                )
                rejection_reason = str(
                    raw_response.get("reason") or "sim_execution_rejected"
                )
                capital_release: dict[str, Any] = {}
                if capital_reservation:
                    capital_release = _release_via_capital_outbox(
                        signals_dir,
                        reservation_id=str(
                            capital_reservation.get("reservation_id") or ""
                        ),
                        amount_cny=_safe_float(
                            capital_reservation.get("amount_cny"), 0.0
                        ),
                        reason="sim_executor_rejected",
                        reference_id=f"{order_id}:executor-rejected",
                    )
                    order["cn_futures_capital_release"] = capital_release
                    receipt["cn_futures_capital_release"] = capital_release
                rejected_decision = {
                    **size_decision,
                    "quantity": 0,
                    "eligible": False,
                    "reason": rejection_reason,
                    "cn_futures_capital_release": capital_release,
                }
                contract_affordability.append(
                    {
                        **affordability_row,
                        "eligible": False,
                        "quantity": 0,
                        "reason": rejection_reason,
                        "size_decision": rejected_decision,
                    }
                )
                holds.append(
                    {
                        "stage": "execution",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "session": session_bucket,
                        "reason": rejection_reason,
                        "receipt": receipt,
                    }
                )
                continue
            contract_affordability.append(affordability_row)
            if reduce_only and existing_position:
                performance = _realized_pnl_from_position_close(
                    position=existing_position,
                    side=str(order["side"]),
                    receipt=receipt,
                    rule_multiplier=rule.contract_multiplier,
                )
            else:
                performance = _realized_pnl_from_reversal(
                    previous=previous_opposite,
                    side=str(order["side"]),
                    receipt=receipt,
                    rule_multiplier=rule.contract_multiplier,
                )
            planned_pnl_delta = _execution_pnl_delta(
                intent=intent,
                receipt=receipt,
                performance=performance,
            )
            if reduce_only and existing_position:
                try:
                    latest_capital_state = (
                        get_cn_futures_capital_provider_state(trade_date=date) or {}
                    )
                except Exception:
                    latest_capital_state = {}
                capital_commit_request = _build_position_close_commit_request(
                    order=order,
                    receipt=receipt,
                    prediction_snapshot=prediction_snapshot,
                    position_snapshot=position_snapshot,
                    previous_position=existing_position,
                    performance=performance,
                    capital_state=latest_capital_state,
                )
                capital_commit_action = "position_close_commit"
                capital_commit_amount = _safe_float(planned_pnl_delta, 0.0)
                order["cn_futures_capital_reservations_after"] = []
            else:
                capital_commit_request = _build_open_fill_commit_request(
                    order=order,
                    receipt=receipt,
                    capital_reservation=capital_reservation,
                    prediction_snapshot=prediction_snapshot,
                    position_snapshot=position_snapshot,
                )
                capital_commit_action = "fill_commit"
                capital_commit_amount = _safe_float(
                    capital_commit_request.get("actual_margin_cny"), 0.0
                )
            capital_commit_reference = str(
                capital_commit_request.get("reference_id") or ""
            )
            capital_commit_action_id = _capital_action_id(
                capital_commit_action,
                capital_commit_reference,
            )
            order.update(
                {
                    "capital_commit_mode": "atomic",
                    "capital_commit_status": "pending",
                    "capital_commit_action": capital_commit_action,
                    "capital_commit_action_id": capital_commit_action_id,
                    "capital_commit_reference_id": capital_commit_reference,
                    "capital_commit_amount_cny": capital_commit_amount,
                    "capital_commit_request": capital_commit_request,
                }
            )
            position_snapshot = _update_position_snapshot(
                signals_dir,
                date=date,
                style_name=style_name,
                symbol=symbol,
                order=order,
                receipt=receipt,
                performance=performance,
            )
            capital_outbox_action = _queue_cn_futures_capital_action(
                signals_dir,
                action=capital_commit_action,
                reference_id=capital_commit_reference,
                amount_cny=capital_commit_amount,
                request=capital_commit_request,
            )
            capital_replay = _dispatch_cn_futures_capital_outbox(signals_dir)
            position_snapshot = _sync_position_capital_commits(
                signals_dir,
                capital_replay,
            )
            committed_capital_action = next(
                (
                    row
                    for row in capital_replay.get("actions", [])
                    if row.get("action_id") == capital_outbox_action.get("action_id")
                ),
                {},
            )
            capital_commit_completed = (
                committed_capital_action.get("status") == "completed"
            )
            capital_commit_result = dict(committed_capital_action.get("result") or {})
            receipt["cn_futures_capital_commit"] = capital_commit_result
            receipt["cn_futures_capital_commit_action_id"] = capital_commit_action_id
            receipt["capital_commit_status"] = (
                "committed" if capital_commit_completed else "pending"
            )
            receipt["execution_eligible"] = bool(
                receipt.get("execution_eligible") and capital_commit_completed
            )
            receipt["execution_class"] = (
                "execution_eligible"
                if receipt["execution_eligible"]
                else "capital_commit_pending"
            )
            execution_evidence: dict[str, Any] = {}
            round_trip_evidence: dict[str, Any] = {}
            if receipt["execution_eligible"]:
                try:
                    execution_evidence = build_execution_evidence(
                        order=order,
                        receipt=receipt,
                        capital_commit_request=capital_commit_request,
                        capital_commit_result=capital_commit_result,
                        source_snapshot_sha256=str(
                            prediction_snapshot.get("source_snapshot_sha256") or ""
                        ),
                    )
                except ValueError as exc:
                    receipt["execution_eligible"] = False
                    receipt["execution_class"] = "execution_evidence_invalid"
                    receipt["execution_evidence_error"] = str(exc)
                    errors.append(
                        {
                            "stage": "execution_evidence",
                            "symbol": symbol,
                            "style": style_name,
                            "error": str(exc),
                        }
                    )
                else:
                    receipt["execution_evidence"] = execution_evidence
                    if capital_commit_action == "position_close_commit":
                        entry_execution_evidence = (
                            existing_position.get("entry_execution_evidence")
                            if isinstance(existing_position, dict)
                            else None
                        )
                        if isinstance(entry_execution_evidence, dict):
                            try:
                                round_trip_evidence = build_round_trip_evidence(
                                    entry_execution_evidence=entry_execution_evidence,
                                    exit_execution_evidence=execution_evidence,
                                    closed_quantity=_safe_int(
                                        performance.get("closed_quantity"), 0
                                    ),
                                    actual_fill_gross_pnl_cny=_safe_float(
                                        performance.get("gross_pnl"), 0.0
                                    ),
                                )
                            except ValueError as exc:
                                receipt["round_trip_evidence_error"] = str(exc)
                            else:
                                receipt["round_trip_evidence"] = round_trip_evidence
                        else:
                            receipt["round_trip_evidence_error"] = (
                                "entry_execution_evidence_missing"
                            )
                    position_snapshot = _persist_position_execution_evidence(
                        signals_dir,
                        style_name=style_name,
                        symbol=symbol,
                        action=capital_commit_action,
                        execution_evidence=execution_evidence,
                        previous_position=existing_position,
                        closed_quantity=_safe_int(
                            performance.get("closed_quantity"), 0
                        ),
                    )
            if not capital_commit_completed:
                errors.append(
                    {
                        "stage": "capital",
                        "symbol": symbol,
                        "style": style_name,
                        "error": "capital_commit_pending",
                        "capital_commit_reason": capital_commit_result.get("reason"),
                    }
                )
                holds.append(
                    {
                        "stage": "capital",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "session": session_bucket,
                        "reason": "capital_commit_pending",
                        "capital_commit": capital_commit_result,
                    }
                )
            card = _signal_card(
                date=date,
                cadence=bar_cadence,
                latest_bar_time=latest_bar_time,
                style_name=style_name,
                symbol=symbol,
                order=order,
                receipt=receipt,
                signal=signal,
            )
            signal_result = _write_filled_signal(signals_dir, card, receipt)
            account_pnl_delta = _apply_execution_to_account_ledger(
                account_ledger,
                date=date,
                intent=intent,
                receipt=receipt,
                performance=performance,
                position_snapshot=position_snapshot,
                path=account_ledger_path,
            )
            if planned_pnl_delta is not None:
                if not math.isclose(
                    float(account_pnl_delta), planned_pnl_delta, abs_tol=1e-9
                ):
                    raise RuntimeError("cn_futures_account_pnl_delta_mismatch")
            if int(capital_replay.get("pending_count", 0)) > 0:
                account_ledger["history_complete"] = False
                account_ledger["history_status"] = "capital_outbox_pending"
                account_ledger["authority"] = "none"
                account_ledger.pop("authoritative_account_state", None)
                _write_json_atomic(account_ledger_path, account_ledger)
            account_state = _current_account_state(
                account=account,
                ledger=account_ledger,
                position_snapshot=position_snapshot,
            )
            records.append(
                {
                    "date": date,
                    "session": session_bucket,
                    "record_type": "simulated_fill",
                    "market": MARKET,
                    "cadence": bar_cadence,
                    "bar_time": latest_bar_time,
                    "style": style_name,
                    "symbol": symbol,
                    "signal": signal,
                    "scenario_tags": scenario_tags,
                    "exit_plan": exit_plan,
                    "size_decision": size_decision,
                    "forward_outcome": forward_outcome,
                    "order": order,
                    "receipt": receipt,
                    "performance": performance,
                    "signal_card": card,
                    "signal_result": signal_result,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    # PIT lineage
                    "point_in_time_as_of": prediction_snapshot["point_in_time_as_of"],
                    "source_event_time": prediction_snapshot["source_event_time"],
                    "evidence_envelope": dict(
                        prediction_snapshot.get("evidence_envelope") or {}
                    ),
                    "evidence_envelope_validation": dict(
                        prediction_snapshot.get("evidence_envelope_validation") or {}
                    ),
                    "point_in_time_lineage": dict(
                        prediction_snapshot.get("point_in_time_lineage") or {}
                    ),
                    "source_snapshot_id": prediction_snapshot["source_snapshot_id"],
                    "source_snapshot_sha256": prediction_snapshot[
                        "source_snapshot_sha256"
                    ],
                    "authority": prediction_snapshot["authority"],
                    "lineage_status": prediction_snapshot["lineage_status"],
                    "capital_authority_id": prediction_snapshot["capital_authority_id"],
                    "authority_generation": prediction_snapshot["authority_generation"],
                    "execution_lineage_id": prediction_snapshot["execution_lineage_id"],
                    "execution_evidence": execution_evidence,
                    "round_trip_evidence": round_trip_evidence,
                    "entry_price": prediction_snapshot["entry_price"],
                    # Cluster dedup
                    "cluster_id": cluster_id,
                    "cluster_role": cluster_info["cluster_role"],
                    "occurrence_index": cluster_info["occurrence_index"],
                    "weight_multiplier": cluster_info["weight_multiplier"],
                }
            )

    position_pnl_summary = _compute_position_pnl_summary(signals_dir)
    order_projection_reconcile = startup_reconcile_order_projection(signals_dir)
    account_state = _current_account_state(
        account=account,
        ledger=account_ledger,
        position_snapshot=position_snapshot,
    )
    affordability = _build_affordability_report(
        date=date,
        raw_distinct_products=distinct_products,
        contracts=contract_affordability,
        account_state=account_state,
    )
    affordability["order_projection_reconcile"] = dict(order_projection_reconcile)
    review = append_review(
        date=date,
        market=MARKET,
        records=records,
        errors=errors,
        holds=holds,
        path=review_path,
        position_pnl_summary=position_pnl_summary,
        affordability=affordability,
        authority_scope=(
            {
                "capital_authority_id": str(
                    (authoritative_state or {}).get("authority_id") or ""
                ),
                "authority_generation": (
                    (authoritative_state or {}).get("authority_generation")
                ),
                "execution_lineage_id": str(
                    (authoritative_state or {}).get("execution_lineage_id") or ""
                ),
            }
            if authoritative_state is not None
            else None
        ),
    )
    _write_json_atomic(
        _runtime_output_path(review_path, AFFORDABILITY_FILENAME),
        affordability,
    )
    return {
        "market": MARKET,
        "reader_market": READER_MARKET,
        "date": date,
        "cadence": cadence_value,
        "session": session_bucket,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "state": (
            "degraded"
            if errors or not bool(order_projection_reconcile.get("ready"))
            else "ok"
        ),
        "universe_count": len(universe),
        "distinct_products": distinct_products,
        "distinct_product_count": len(distinct_products),
        "required_min_distinct_products": required_min_products,
        "style_count": len(styles),
        "record_count": len(records),
        "filled_count": sum(
            1 for record in records if record["receipt"].get("status") == "filled"
        ),
        "records": records,
        "errors": errors,
        "holds": holds,
        "hold_count": len(holds),
        "latest_hold_bar_time": _latest_hold_bar_time(holds),
        "hold_reason_summary": review.get("hold_reason_summary", {}),
        "review": review,
        "account_state": account_state,
        "affordability": affordability,
        "real_trading_enabled": False,
        "generated_at": _now_iso(),
        "max_intraday_bar_age_minutes": max_intraday_bar_age_minutes,
        "capital_outbox": _dispatch_cn_futures_capital_outbox(signals_dir),
        "order_projection_reconcile": order_projection_reconcile,
    }


__all__ = [
    "build_affordability_hold",
    "get_cn_futures_capital_provider_state",
    "quantity_for_style_decision",
    "run_multi_style_simulation",
    "_compute_cluster_id",
    "_classify_cluster_occurrence",
    "_forward_outcome_label",
    "_prediction_snapshot_before_risk",
    "_receipt_execution_eligible",
]
