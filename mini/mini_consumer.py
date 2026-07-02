#!/usr/bin/env python3
"""Deprecated Mini consumer notes.

The active Mini-side path is now:

    SSH tunnel 9865 -> mini:8654
    sim-signal-receiver -> pending
    sim-signal-executor -> execution receipts

tradingagent should send simulated signals through
``shared.execution.webhook_sender.send_sim_signal_to_mini``. No extra
``mini_consumer`` process is required on the Mini. The legacy classes below are
kept only so older local tests and imports do not break during this migration.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.execution.signal_state_machine import (
    PENDING,
    SignalStateConflict,
    SignalStateMachine,
    now_iso,
    read_json,
    write_json,
)

TRADINGAGENT_ROOT = Path(__file__).resolve().parents[1]
SIGNALS_DIR = TRADINGAGENT_ROOT / "signals"
SIMULATED_EXECUTOR = TRADINGAGENT_ROOT / "Ashare" / "sim_executor.py"


class MiniConsumerRejected(RuntimeError):
    """Raised when a signal violates the Mini execution bridge contract."""


class MiniConsumer:
    """File-backed Mini-side signal consumer."""

    def __init__(
        self,
        signals_dir: Path | str = SIGNALS_DIR,
        executor_path: Path | str = SIMULATED_EXECUTOR,
        worker_id: str = "mac-mini-cron",
        dry_run: bool = False,
    ) -> None:
        self.signals_dir = Path(signals_dir)
        self.executor_path = Path(executor_path)
        self.worker_id = worker_id
        self.dry_run = dry_run
        self.machine = SignalStateMachine(self.signals_dir)

    @property
    def positions_dir(self) -> Path:
        return self.signals_dir / "positions"

    def ensure_dirs(self) -> None:
        self.machine.ensure_dirs()
        self.positions_dir.mkdir(parents=True, exist_ok=True)

    def claim_next_pending(self) -> dict[str, Any] | None:
        """Atomically claim one pending signal card."""
        self.ensure_dirs()
        for pending_path in sorted((self.signals_dir / PENDING).glob("*.json")):
            try:
                order_id = read_json(pending_path).get("order_id", pending_path.stem)
                return self.machine.claim(str(order_id), worker_id=self.worker_id)
            except (SignalStateConflict, FileNotFoundError):
                continue
        return None

    def dispatch(self, claimed: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a claimed signal by capital layer."""
        signal = claimed.get("signal_card", claimed)
        order_id = str(signal.get("order_id", ""))
        try:
            capital_layer = str(signal.get("capital_layer", "")).lower().strip()
            if capital_layer == "simulated":
                result = self.execute_simulated(signal)
                return self.write_filled(signal, result)
            if capital_layer == "real":
                return self.handle_real(signal)
            raise MiniConsumerRejected(f"Unsupported capital_layer for Mini consumer: {capital_layer!r}")
        except MiniConsumerRejected as exc:
            return self.reject_claimed(order_id, str(exc))

    def execute_simulated(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Execute a simulated signal through the local A-share simulated executor."""
        self._require_account(signal, capital_layer="simulated", account_type="simulated")
        self._require_market(signal, market="ashare")
        if str(signal.get("capital_layer")) == "real":
            raise MiniConsumerRejected("real signal must never enter execute_simulated")

        cmd = [
            sys.executable,
            str(self.executor_path),
            "--code",
            str(signal.get("ts_code", "")),
            "--side",
            str(signal.get("direction", signal.get("side", "buy"))),
            "--quantity",
            str(int(signal.get("quantity", 0) or 0)),
            "--tradebook-id",
            str(signal.get("order_id", "")),
        ]
        if self.dry_run or signal.get("dry_run"):
            cmd.append("--dry-run")

        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "simulated executor failed").strip()
            raise MiniConsumerRejected(f"simulated executor failed: {message}")

        parsed = self._parse_executor_stdout(completed.stdout)
        requested_qty = int(signal.get("quantity", 0) or 0)
        requested_price = float(signal.get("price") or signal.get("trigger_price") or 0.0)
        filled_qty = int(parsed.get("filled_qty", parsed.get("filled_quantity", requested_qty)) or 0)
        filled_price = float(parsed.get("avg_price", parsed.get("filled_price", requested_price)) or 0.0)
        status = str(parsed.get("status", "filled"))
        if status not in ("ok", "warning", "dry_run_ok", "filled", "partial"):
            raise MiniConsumerRejected(f"simulated executor returned non-fill status: {status}")

        return {
            "status": "filled",
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_quantity": filled_qty,
            "slippage": float(parsed.get("slippage", 0.0) or 0.0),
            "fee": float(parsed.get("fee", 0.0) or 0.0),
            "executed_at": parsed.get("executed_at") or now_iso(),
            "message": parsed.get("message", ""),
        }

    def handle_real(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Handle a real-money signal without direct execution."""
        self._require_account(signal, capital_layer="real", account_type="real")
        if signal.get("direct_execution") is not False:
            raise MiniConsumerRejected("real signal with direct_execution=true is forbidden")

        email_result = self.send_email(signal)
        snapshot = self.sync_readonly_positions(signal)
        positions_result = self.write_positions(snapshot)
        return {
            "order_id": signal.get("order_id", ""),
            "status": "manual_notified",
            "direct_execution": False,
            "email": email_result,
            "positions": positions_result,
        }

    def write_filled(self, signal: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Write a fill card by moving the claimed signal into filled."""
        self._require_account(signal, capital_layer="simulated", account_type="simulated")
        order_id = str(signal.get("order_id", ""))
        filled_qty = int(result.get("filled_qty", result.get("filled_quantity", signal.get("quantity", 0))) or 0)
        fill_card = {
            "order_id": order_id,
            "status": "filled",
            "filled_price": float(result.get("filled_price", result.get("avg_price", signal.get("price", 0.0))) or 0.0),
            "filled_qty": filled_qty,
            "filled_quantity": filled_qty,
            "slippage": float(result.get("slippage", 0.0) or 0.0),
            "fee": float(result.get("fee", 0.0) or 0.0),
            "executed_at": str(result.get("executed_at") or now_iso()),
            "fill_time": str(result.get("executed_at") or now_iso()),
            "filled_at": str(result.get("executed_at") or now_iso()),
            "account_type": "simulated",
            "capital_layer": "simulated",
            "idempotency_key": str(signal.get("idempotency_key") or order_id),
            "fill": {
                "filled_price": float(result.get("filled_price", result.get("avg_price", signal.get("price", 0.0))) or 0.0),
                "filled_qty": filled_qty,
                "filled_at": str(result.get("executed_at") or now_iso()),
            },
        }
        return self.machine.fill(order_id, fill_info=fill_card)

    def write_positions(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Write a readonly positions snapshot under signals/positions/."""
        payload = dict(snapshot)
        payload.setdefault("ts_code", "")
        payload.setdefault("quantity", 0)
        payload.setdefault("sellable_quantity", 0)
        payload.setdefault("avg_price", 0.0)
        payload.setdefault("cash", 0.0)
        payload.setdefault("account_type", "real")
        payload.setdefault("synced_at", now_iso())
        payload.setdefault("source", "tonghuashun_readonly")

        snapshot_id = str(payload.get("snapshot_id") or f"positions-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}")
        payload["snapshot_id"] = snapshot_id
        path = self.positions_dir / f"{snapshot_id}.json"
        write_json(path, payload)
        return {"status": "ok", "snapshot_path": str(path), "snapshot": payload}

    def reject_claimed(self, order_id: str, reason: str) -> dict[str, Any]:
        """Move a claimed/running signal into failed after a contract rejection."""
        if not order_id:
            return {"order_id": "", "status": "rejected", "message": reason}
        try:
            result = self.machine.fail(order_id, reason=reason)
        except (FileNotFoundError, SignalStateConflict, ValueError) as exc:
            return {"order_id": order_id, "status": "rejected", "message": f"{reason}; fail transition error: {exc}"}
        result["status"] = "rejected"
        result["message"] = reason
        return result

    def send_email(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Stub for Nicholas manual-confirmation email."""
        return {
            "status": "queued_stub",
            "order_id": signal.get("order_id", ""),
            "message": "manual confirmation email stub; no real order submitted",
        }

    def sync_readonly_positions(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Stub for Tonghuashun readonly account snapshot."""
        return {
            "ts_code": str(signal.get("ts_code", "")),
            "quantity": 0,
            "sellable_quantity": 0,
            "avg_price": 0.0,
            "cash": 0.0,
            "account_type": "real",
            "synced_at": now_iso(),
            "source": "tonghuashun_readonly",
        }

    def _require_account(self, signal: dict[str, Any], capital_layer: str, account_type: str) -> None:
        actual_layer = str(signal.get("capital_layer", "")).lower().strip()
        actual_account = str(signal.get("account_type", "")).lower().strip()
        if actual_layer != capital_layer:
            raise MiniConsumerRejected(f"capital_layer must be {capital_layer}, got {actual_layer!r}")
        if actual_account != account_type:
            raise MiniConsumerRejected(f"{capital_layer} signal must use account_type={account_type}, got {actual_account!r}")

    def _require_market(self, signal: dict[str, Any], market: str) -> None:
        actual_market = str(signal.get("market", market)).lower().strip()
        if actual_market != market:
            raise MiniConsumerRejected(f"Mini simulated bridge only supports market={market}, got {actual_market!r}")

    def _parse_executor_stdout(self, stdout: str) -> dict[str, Any]:
        text = (stdout or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"message": text}
        return parsed if isinstance(parsed, dict) else {}


def claim_next_pending(signals_dir: Path | str = SIGNALS_DIR) -> dict[str, Any] | None:
    return MiniConsumer(signals_dir=signals_dir).claim_next_pending()


def dispatch(signal: dict[str, Any], signals_dir: Path | str = SIGNALS_DIR) -> dict[str, Any]:
    return MiniConsumer(signals_dir=signals_dir).dispatch(signal)


def execute_simulated(signal: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    return MiniConsumer(dry_run=dry_run).execute_simulated(signal)


def handle_real(signal: dict[str, Any], signals_dir: Path | str = SIGNALS_DIR) -> dict[str, Any]:
    return MiniConsumer(signals_dir=signals_dir).handle_real(signal)


def write_filled(signal: dict[str, Any], result: dict[str, Any], signals_dir: Path | str = SIGNALS_DIR) -> dict[str, Any]:
    return MiniConsumer(signals_dir=signals_dir).write_filled(signal, result)


def write_positions(snapshot: dict[str, Any], signals_dir: Path | str = SIGNALS_DIR) -> dict[str, Any]:
    return MiniConsumer(signals_dir=signals_dir).write_positions(snapshot)


def main() -> int:
    result = {
        "status": "disabled",
        "message": (
            "mini_consumer is deprecated. Mini-side sim-signal-receiver and "
            "sim-signal-executor already consume webhook signals."
        ),
        "replacement": "shared.execution.webhook_sender.send_sim_signal_to_mini",
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
