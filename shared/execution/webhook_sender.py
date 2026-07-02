#!/usr/bin/env python3
"""Webhook sender for Mac Mini simulated-signal ingestion."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import os

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:9865/")
RECEIPTS_PATH = Path(os.environ.get("SIM_RECEIPTS_PATH", "/opt/investment/MarketGraph/outputs/sim_execution_receipts.jsonl"))
TIMEOUT_SECONDS = 10
RETRY_COUNT = 2


def _order_get(order: Any, key: str, default: Any = None) -> Any:
    if isinstance(order, dict):
        return order.get(key, default)
    return getattr(order, key, default)


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _coerce_direction(value: Any) -> str:
    direction = str(value or "").lower().strip()
    if direction == "reduce":
        return "sell"
    return direction


def build_sim_signal(order: dict[str, Any] | Any) -> dict[str, Any]:
    """Build the Mini receiver payload from a tradingagent order."""
    now = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    side = _order_get(order, "direction", _order_get(order, "side", ""))
    price = _order_get(order, "price", _order_get(order, "limit_price", _order_get(order, "execution_price")))
    return {
        "order_id": str(_order_get(order, "order_id", "") or f"SIM-WEBHOOK-{now}-{uuid.uuid4().hex[:8]}"),
        "ts_code": str(_order_get(order, "ts_code", "")).strip(),
        "direction": _coerce_direction(side),
        "quantity": int(_order_get(order, "quantity", 0) or 0),
        "price": _coerce_float(price),
        "strategy_name": str(_order_get(order, "strategy_name", "")).strip(),
        "capital_layer": "simulated",
        "account_type": "simulated",
    }


def encode_signal(signal: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes used for both POST body and HMAC."""
    return json.dumps(signal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_body(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """HMAC-SHA256 signature for the Mini receiver."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_once(url: str, body: bytes, signature: str, timeout: int | float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hermes-Signature": signature,
            "X-Signature": signature,
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status_code = int(getattr(resp, "status", getattr(resp, "code", 0)) or 0)
        raw = resp.read().decode("utf-8", errors="replace")
    if status_code < 200 or status_code >= 300:
        raise urllib.error.HTTPError(url, status_code, raw or "webhook rejected", hdrs=None, fp=None)
    try:
        parsed: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"raw": raw}
    return {
        "status": "sent",
        "success": True,
        "http_status": status_code,
        "response": parsed,
    }


def send_sim_signal_to_mini(
    order: dict[str, Any] | Any,
    url: str = WEBHOOK_URL,
    secret: str = WEBHOOK_SECRET,
    timeout: int | float = TIMEOUT_SECONDS,
    retries: int = RETRY_COUNT,
) -> dict[str, Any]:
    """Send a simulated order to the Mac Mini webhook receiver."""
    signal = build_sim_signal(order)
    body = encode_signal(signal)
    signature = sign_body(body, secret)
    body_sha256 = hashlib.sha256(body).hexdigest()
    attempts = retries + 1
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            result = _post_once(url, body, signature, timeout)
            result.update(
                {
                    "attempts": attempt,
                    "order_id": signal["order_id"],
                    "signal": signal,
                    "signature": signature,
                    "payload_sha256": body_sha256,
                    "webhook_url": url,
                }
            )
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(0)

    return {
        "status": "failed",
        "success": False,
        "attempts": attempts,
        "order_id": signal["order_id"],
        "signal": signal,
        "signature": signature,
        "payload_sha256": body_sha256,
        "webhook_url": url,
        "message": f"Mini webhook send failed after {attempts} attempts: {last_error}",
    }


def read_receipts(path: Path | str = RECEIPTS_PATH) -> list[dict[str, Any]]:
    """Read sim-signal-executor receipts written back by the Mini."""
    receipt_path = Path(path)
    if not receipt_path.exists():
        return []

    receipts: list[dict[str, Any]] = []
    with open(receipt_path, "r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                receipts.append(parsed)
    return receipts
