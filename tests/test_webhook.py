#!/usr/bin/env python3
"""Tests for the simulated Mini webhook sender."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.execution import hermes_bridge, webhook_sender


class _HTTPResponse:
    status = 200

    def __enter__(self) -> "_HTTPResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"accepted":true}'


class WebhookSenderTest(unittest.TestCase):
    def test_send_sim_signal_to_mini_posts_signed_payload(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(req: object, timeout: object = None) -> _HTTPResponse:
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data
            captured["headers"] = dict(req.header_items())
            captured["timeout"] = timeout
            return _HTTPResponse()

        order = {
            "order_id": "SIM-WEBHOOK-1",
            "ts_code": "600000.SH",
            "direction": "buy",
            "quantity": 100,
            "price": 10.5,
            "strategy_name": "webhook_unit_test",
            "capital_layer": "simulated",
            "account_type": "simulated",
        }

        with patch("shared.execution.webhook_sender.urllib.request.urlopen", side_effect=fake_urlopen):
            result = webhook_sender.send_sim_signal_to_mini(order)

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(captured["url"], "http://localhost:9865/")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 10)

        body = captured["body"]
        self.assertIsInstance(body, bytes)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "order_id": "SIM-WEBHOOK-1",
                "ts_code": "600000.SH",
                "direction": "buy",
                "quantity": 100,
                "price": 10.5,
                "strategy_name": "webhook_unit_test",
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        )

        expected_signature = hmac.new(
            webhook_sender.WEBHOOK_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers = captured["headers"]
        self.assertEqual(headers["X-hermes-signature"], expected_signature)
        self.assertEqual(headers["X-signature"], expected_signature)
        self.assertEqual(headers["X-hub-signature-256"], f"sha256={expected_signature}")
        self.assertEqual(result["signature"], expected_signature)
        self.assertEqual(result["payload_sha256"], hashlib.sha256(body).hexdigest())

    def test_hermes_bridge_simulated_order_uses_webhook_sender(self) -> None:
        order = {
            "order_id": "SIM-BRIDGE-WEBHOOK",
            "ts_code": "600000.SH",
            "direction": "buy",
            "quantity": 100,
            "price": 10.5,
            "stop_loss": 9.8,
            "strategy_name": "webhook_bridge_test",
            "timestamp": "2026-06-30T09:30:00+08:00",
            "status": "pending",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "manual_confirm_required": False,
            "direct_execution": False,
            "trigger": {
                "condition_id": "COND-SIM-BRIDGE-WEBHOOK",
                "triggered_at": "2026-06-30T09:30:00+08:00",
                "trigger_price": 10.5,
            },
            "evidence_refs": ["unit-test"],
            "valid_until": "2026-06-30",
            "risk_check": {
                "passed": True,
                "checks": ["unit_test"],
            },
            "source_condition_id": "COND-SIM-BRIDGE-WEBHOOK",
            "idempotency_key": "SIM-BRIDGE-WEBHOOK",
            "t_plus_1": {
                "sellable_from": "2026-06-30",
                "sellable_date": "2026-06-30",
            },
        }

        with patch(
            "shared.execution.hermes_bridge.send_sim_signal_to_mini",
            return_value={"status": "sent", "success": True, "order_id": "SIM-BRIDGE-WEBHOOK"},
        ) as send_mock:
            result = hermes_bridge.send_order(order)

        self.assertEqual(result["status"], "sent")
        self.assertTrue(result["success"])
        self.assertEqual(result["capital_layer"], "simulated")
        self.assertEqual(result["account_type"], "simulated")
        send_mock.assert_called_once()

    def test_read_receipts_parses_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sim_execution_receipts.jsonl"
            rows = [
                {"order_id": "SIM-1", "status": "filled", "filled_qty": 100},
                {"order_id": "SIM-2", "status": "rejected", "message": "bad account"},
            ]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n")
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.write("{bad-json}\n")

            self.assertEqual(webhook_sender.read_receipts(path), rows)
            self.assertEqual(webhook_sender.read_receipts(Path(tmp) / "missing.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
