#!/usr/bin/env python3
"""Helpers for non-executable shadow signal lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .signal_state_machine import SignalStateConflict, SignalStateMachine

FILLED_SHADOW_STATUSES = {"filled", "partial"}


def write_shadow_signal(card: dict[str, Any], signals_root: Path | str) -> dict[str, Any]:
    """Write a shadow card and settle local mock fills immediately.

    Shadow cards are not broker execution tasks. When a market runner already
    has a local simulated fill, leaving the card in pending creates permanent
    operational noise, so the card is atomically advanced to shadow/filled.
    """
    state = SignalStateMachine(Path(signals_root) / "shadow")
    try:
        pending = state.write_pending(card)
    except SignalStateConflict as exc:
        return {
            "order_id": card.get("order_id", ""),
            "status": "duplicate",
            "queue_scope": "shadow",
            "message": str(exc),
        }

    simulated_fill = card.get("simulated_fill") if isinstance(card.get("simulated_fill"), dict) else {}
    fill_status = str(simulated_fill.get("status") or "").strip().lower()
    if fill_status not in FILLED_SHADOW_STATUSES:
        pending["queue_scope"] = "shadow"
        pending["capital_layer"] = "shadow"
        return pending

    order_id = str(pending["order_id"])
    fill_info = {
        "simulated_fill": simulated_fill,
        "shadow_settled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settlement_source": "local_shadow_simulator",
    }
    try:
        state.claim(order_id, worker_id="local_shadow_simulator")
        state.mark_running(order_id, worker_id="local_shadow_simulator")
        settled = state.fill(order_id, fill_info, partial=fill_status == "partial")
    except Exception as exc:  # noqa: BLE001
        reason = f"local shadow settlement failed: {exc.__class__.__name__}: {exc}"
        try:
            failed = state.fail(order_id, reason=reason)
        except Exception:
            pending["queue_scope"] = "shadow"
            pending["capital_layer"] = "shadow"
            pending["settlement_warning"] = reason
            return pending
        failed["queue_scope"] = "shadow"
        failed["capital_layer"] = "shadow"
        failed["settlement_warning"] = reason
        return failed
    settled["queue_scope"] = "shadow"
    settled["capital_layer"] = "shadow"
    return settled


__all__ = ["write_shadow_signal"]
