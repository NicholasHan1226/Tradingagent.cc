#!/usr/bin/env python3
"""Isolated real-money signal queue.

This queue is deliberately separate from ``signals/pending`` and
``signals/shadow``. It is a controlled staging area for manually approved real
signals, not an automatic broker execution path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.accounting import daily_reconcile, position_ledger
from shared.markets.safety import SafetyViolation

from .real_trading_gate import (
    GateResult,
    emergency_stop_check,
    require_explicit_approval,
    run_real_order_gates,
    validate_capital_limits,
    validate_market_hours,
    validate_real_trading_enabled,
    validate_t1_settlement,
)

TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
SIGNALS_DIR = Path(os.environ.get("TRADINGAGENT_SIGNALS_DIR", TRADINGAGENT_ROOT / "signals"))
REAL_STATES = ("review", "confirmed", "pending", "claimed", "running", "filled", "failed", "partial", "expired", "cancelled")
RECEIPT_CHECKSUM_KEYS = ("receipt_sha256", "checksum", "sha256")
CHECKSUM_KEYS = {"payload_sha256", "receipt_sha256", "checksum", "sha256"}
_SIGNAL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
SOURCE_PATH_KEYS = ("source_path", "signal_path", "_path", "shadow_signal_path")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_signal_id(signal_id: str) -> str:
    if not signal_id or not _SIGNAL_ID_RE.fullmatch(signal_id):
        raise ValueError(f"Invalid real signal id: {signal_id!r}")
    return signal_id


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"JSON payload must be an object: {path}")
    return data


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def _canonical_json(payload: dict[str, Any], *, drop_checksums: bool = False) -> bytes:
    data = {key: value for key, value in payload.items() if not (drop_checksums and key in CHECKSUM_KEYS)}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload_sha256(payload: dict[str, Any], *, drop_checksums: bool = False) -> str:
    return hashlib.sha256(_canonical_json(payload, drop_checksums=drop_checksums)).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _extract_approval_token(payload: dict[str, Any], explicit: str | None = None) -> str | None:
    return explicit or payload.get("manual_confirmation_token") or payload.get("approval_token")


def _extract_source_path(payload: dict[str, Any]) -> str:
    for key in SOURCE_PATH_KEYS:
        raw = payload.get(key)
        if raw:
            return str(raw)
    return ""


def _require_shadow_source_path(payload: dict[str, Any]) -> str:
    source_path = _extract_source_path(payload)
    normalized = source_path.replace("\\", "/")
    if "/signals/shadow/" not in normalized and not normalized.startswith("signals/shadow/"):
        raise SafetyViolation("real_signal_queue: promotion source must be signals/shadow")
    return source_path


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value or default)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


class RealSignalQueue:
    """Manage ``signals/real/*`` without touching shadow/sim queues."""

    def __init__(
        self,
        signals_root: Path | str | None = None,
        *,
        max_per_order: Any | None = None,
        max_daily: Any | None = None,
    ) -> None:
        root = Path(signals_root) if signals_root is not None else SIGNALS_DIR
        self.queue_root = root if root.name == "real" else root / "real"
        self.max_per_order = max_per_order
        self.max_daily = max_daily

    def ensure_dirs(self) -> None:
        for state in REAL_STATES:
            (self.queue_root / state).mkdir(parents=True, exist_ok=True)
        (self.queue_root / "positions").mkdir(parents=True, exist_ok=True)
        (self.queue_root / "receipts").mkdir(parents=True, exist_ok=True)

    def promote_from_shadow(
        self,
        shadow_signal: dict[str, Any],
        *,
        approval_token: str | None = None,
        max_per_order: Any | None = None,
        max_daily: Any | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Promote a shadow signal into real review only after all gates pass."""

        if not isinstance(shadow_signal, dict):
            raise SafetyViolation("real_signal_queue: shadow_signal must be a dict")
        source_path = _require_shadow_source_path(shadow_signal)
        signal = self._build_real_signal(shadow_signal)
        signal["source_shadow_path"] = source_path
        gate_result = run_real_order_gates(
            signal,
            approval_token=_extract_approval_token(shadow_signal, approval_token),
            max_per_order=max_per_order if max_per_order is not None else self.max_per_order,
            max_daily=max_daily if max_daily is not None else self.max_daily,
            now=now,
            halt_files=self._halt_files(),
        )
        signal["gate_result"] = gate_result.__dict__
        self.ensure_dirs()
        path = self._path("review", signal["order_id"])
        if path.exists():
            raise SafetyViolation(f"real_signal_queue: real review signal already exists: {signal['order_id']}")
        _write_json_atomic(path, signal)
        return {
            "status": "review",
            "queue_scope": "real",
            "order_id": signal["order_id"],
            "signal_path": str(path),
            "signal_card": signal,
            "gate_result": gate_result.__dict__,
        }

    def manual_confirm(self, signal_id: str, token: str) -> dict[str, Any]:
        """Mark a reviewed real signal as manually confirmed."""

        emergency_stop_check(halt_files=self._halt_files())
        validate_real_trading_enabled()
        require_explicit_approval(token)
        self.ensure_dirs()
        path, signal = self._find(signal_id)
        if path is None:
            raise SafetyViolation(f"real_signal_queue: signal not found: {signal_id}")
        status = str(signal.get("status") or path.parent.name)
        if status not in {"review", "confirmed"}:
            raise SafetyViolation(f"real_signal_queue: cannot manually confirm signal in {status}")
        signal["status"] = "confirmed"
        signal["manual_confirm_required"] = True
        signal["manual_confirmed"] = True
        signal["manual_confirmed_at"] = _now_iso()
        signal["manual_confirmation_token_sha256"] = _token_hash(token)
        target = self._path("confirmed", str(signal["order_id"]))
        _write_json_atomic(target, signal)
        if path != target:
            path.unlink(missing_ok=True)
        return {
            "status": "confirmed",
            "queue_scope": "real",
            "order_id": signal["order_id"],
            "signal_path": str(target),
            "signal_card": signal,
        }

    def submit_to_hermes(
        self,
        signal: dict[str, Any] | str,
        *,
        now: datetime | None = None,
        max_per_order: Any | None = None,
        max_daily: Any | None = None,
    ) -> dict[str, Any]:
        """Move a manually confirmed real signal into ``signals/real/pending``."""

        self.ensure_dirs()
        source_path: Path | None = None
        if isinstance(signal, str):
            source_path, payload = self._find(signal)
            if source_path is None:
                raise SafetyViolation(f"real_signal_queue: signal not found: {signal}")
        elif isinstance(signal, dict):
            payload = dict(signal)
            if payload.get("order_id"):
                source_path, existing = self._find(str(payload["order_id"]))
                if existing:
                    payload = {**existing, **payload}
        else:
            raise SafetyViolation("real_signal_queue: signal must be a dict or signal id")

        if payload.get("manual_confirmed") is not True:
            raise SafetyViolation("real_signal_queue: manual confirmation is required before pending submission")
        self._run_submission_gates(
            payload,
            max_per_order=max_per_order if max_per_order is not None else self.max_per_order,
            max_daily=max_daily if max_daily is not None else self.max_daily,
            now=now,
        )
        payload["status"] = "pending"
        payload["submitted_to_hermes_at"] = _now_iso()
        payload["queue_scope"] = "real"
        payload["direct_execution"] = False
        payload["real_auto_order_forbidden"] = True
        target = self._path("pending", str(payload["order_id"]))
        if target.exists() and target != source_path:
            raise SafetyViolation(f"real_signal_queue: pending real signal already exists: {payload['order_id']}")
        _write_json_atomic(target, payload)
        if source_path is not None and source_path != target:
            source_path.unlink(missing_ok=True)
        return {
            "status": "pending",
            "queue_scope": "real",
            "order_id": payload["order_id"],
            "signal_path": str(target),
            "signal_card": payload,
            "direct_execution": False,
            "real_auto_order_forbidden": True,
        }

    def track_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """Verify a signed receipt and persist it under the real queue."""

        if not isinstance(receipt, dict):
            raise SafetyViolation("real_signal_queue: receipt must be a dict")
        embedded = next((str(receipt.get(key) or "") for key in RECEIPT_CHECKSUM_KEYS if receipt.get(key)), "")
        if not embedded:
            raise SafetyViolation("real_signal_queue: receipt_sha256/checksum is required")
        computed = _payload_sha256(receipt, drop_checksums=True)
        if embedded != computed:
            raise SafetyViolation("real_signal_queue: receipt checksum mismatch")
        order_id = _normalize_signal_id(str(receipt.get("order_id") or receipt.get("signal_id") or ""))
        status = str(receipt.get("status") or "").strip().lower()
        target_state = "filled" if status == "filled" else "partial" if status == "partial" else "failed"
        payload = dict(receipt)
        payload.setdefault("tracked_at", _now_iso())
        self.ensure_dirs()
        target = self._path(target_state, order_id)
        _write_json_atomic(target, payload)
        jsonl = self.queue_root / "receipts" / "receipts.jsonl"
        with open(jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return {
            "status": target_state,
            "queue_scope": "real",
            "order_id": order_id,
            "receipt_path": str(target),
            "receipt_sha256": computed,
        }

    def reconcile_ledger(self, date: str) -> dict[str, Any]:
        """Cross-check real position ledger against latest real positions snapshot."""

        self.ensure_dirs()
        try:
            system_positions = position_ledger.get_positions(capital_layer="real")
        except TimeoutError:
            logging.getLogger("tradingagent.real").warning(
                "reconcile_ledger skipped — position_ledger lock timeout"
            )
            return {"date": date, "queue_scope": "real", "status": "degraded",
                    "reason": "position_ledger_lock_timeout"}
        hermes_positions, source_path = self._load_positions_snapshot(date)
        result = daily_reconcile.reconcile(system_positions, hermes_positions, log=False)
        result["date"] = date
        result["queue_scope"] = "real"
        result["positions_source_path"] = str(source_path) if source_path else ""
        result["system_position_count"] = len(system_positions)
        result["hermes_position_count"] = len(hermes_positions)
        target = self.queue_root / "receipts" / f"reconcile_{date}.json"
        _write_json_atomic(target, result)
        return result

    def _run_submission_gates(
        self,
        signal: dict[str, Any],
        *,
        max_per_order: Any | None,
        max_daily: Any | None,
        now: datetime | None,
    ) -> GateResult:
        emergency_stop_check(halt_files=self._halt_files())
        validate_real_trading_enabled()
        validate_capital_limits(
            signal,
            max_per_order if max_per_order is not None else os.environ.get("REAL_TRADING_MAX_PER_ORDER"),
            max_daily if max_daily is not None else os.environ.get("REAL_TRADING_MAX_DAILY"),
        )
        validate_market_hours(now=now)
        validate_t1_settlement(signal, trade_date=now.date().isoformat() if isinstance(now, datetime) else None)
        return GateResult(passed=True, failed=False, reason="submission gates passed", gate="submit_to_hermes")

    def _build_real_signal(self, shadow_signal: dict[str, Any]) -> dict[str, Any]:
        now = _now_iso()
        source_id = str(shadow_signal.get("order_id") or shadow_signal.get("signal_id") or uuid.uuid4().hex[:12])
        side = str(shadow_signal.get("direction") or shadow_signal.get("side") or "buy").strip().lower()
        if side == "reduce":
            side = "sell"
        order_id = str(shadow_signal.get("real_order_id") or f"REAL-{source_id}").replace("/", "-")
        signal = {
            "order_id": _normalize_signal_id(order_id),
            "source_shadow_order_id": source_id,
            "ts_code": str(shadow_signal.get("ts_code") or shadow_signal.get("symbol") or "").strip().upper(),
            "symbol": str(shadow_signal.get("symbol") or shadow_signal.get("ts_code") or "").strip().upper(),
            "market": str(shadow_signal.get("market") or "ashare").strip().lower(),
            "direction": side,
            "quantity": int(_as_float(shadow_signal.get("quantity"), 0.0)),
            "price": _as_float(shadow_signal.get("price", shadow_signal.get("limit_price", shadow_signal.get("execution_price"))), 0.0),
            "stop_loss": shadow_signal.get("stop_loss"),
            "strategy_name": str(shadow_signal.get("strategy_name") or shadow_signal.get("strategy") or "").strip(),
            "timestamp": now,
            "trade_date": str(shadow_signal.get("trade_date") or now[:10]),
            "capital_layer": "real",
            "account_type": "real",
            "manual_confirm_required": True,
            "manual_confirmed": False,
            "direct_execution": False,
            "real_auto_order_forbidden": True,
            "status": "review",
            "valid_until": str(shadow_signal.get("valid_until") or shadow_signal.get("trade_date") or now[:10]),
            "evidence_refs": list(shadow_signal.get("evidence_refs") or []),
            "risk_check": dict(shadow_signal.get("risk_check") or {"passed": False, "checks": ["missing_real_risk_check"]}),
            "idempotency_key": f"REAL:{shadow_signal.get('market', 'ashare')}:{now[:10]}:{shadow_signal.get('ts_code') or shadow_signal.get('symbol')}:{side}",
            "created_by": "RealSignalQueue.promote_from_shadow",
            "created_at": now,
        }
        for field in ("entry_date", "position_open_date", "open_date", "t_plus_1", "daily_notional_used"):
            if field in shadow_signal:
                signal[field] = shadow_signal[field]
        return signal

    def _path(self, state: str, signal_id: str) -> Path:
        if state not in REAL_STATES:
            raise ValueError(f"Invalid real queue state: {state}")
        return self.queue_root / state / f"{_normalize_signal_id(signal_id)}.json"

    def _halt_files(self) -> list[Path]:
        return [
            self.queue_root / "emergency_stop.json",
            self.queue_root / "HALT",
            self.queue_root.parent / "executor_halt.json",
        ]

    def _find(self, signal_id: str) -> tuple[Path | None, dict[str, Any]]:
        signal_id = _normalize_signal_id(signal_id)
        for state in REAL_STATES:
            path = self._path(state, signal_id)
            if path.exists():
                data = _read_json(path)
                data.setdefault("status", state)
                return path, data
        return None, {}

    def _load_positions_snapshot(self, target_date: str) -> tuple[list[dict[str, Any]], Path | None]:
        positions_dir = self.queue_root / "positions"
        candidates = sorted(positions_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        dated = [path for path in candidates if target_date in path.name]
        for path in dated + [path for path in candidates if path not in dated]:
            try:
                data = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            positions = data.get("positions") if isinstance(data, dict) else None
            if isinstance(positions, list):
                return [row for row in positions if isinstance(row, dict)], path
            if data.get("ts_code"):
                return [data], path
        return [], None


__all__ = ["RealSignalQueue"]
