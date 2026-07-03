#!/usr/bin/env python3
"""Tests for fail-closed real trading safety gates."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.execution.real_trading_gate import (
    emergency_stop_check,
    require_explicit_approval,
    run_real_order_gates,
    validate_capital_limits,
    validate_market_hours,
    validate_real_trading_enabled,
    validate_t1_settlement,
)
from shared.execution.signals_real import RealSignalQueue, _payload_sha256
from shared.markets.safety import SafetyViolation


class RealTradingGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.env_patch.stop()

    def _enable_env(self) -> None:
        os.environ["REAL_TRADING_ENABLED"] = "enabled"
        os.environ["REAL_TRADING_APPROVAL_TOKEN"] = "unit-token"
        os.environ["REAL_TRADING_MAX_PER_ORDER"] = "20000"
        os.environ["REAL_TRADING_MAX_DAILY"] = "50000"

    def _buy_order(self, **overrides: object) -> dict[str, object]:
        order: dict[str, object] = {
            "order_id": "REAL-UNIT-1",
            "ts_code": "600000.SH",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "trade_date": "2026-07-03",
            "daily_notional_used": 0,
        }
        order.update(overrides)
        return order

    def test_real_trading_enabled_rejects_by_default(self) -> None:
        with self.assertRaisesRegex(SafetyViolation, "REAL_TRADING_ENABLED"):
            validate_real_trading_enabled()

    def test_explicit_approval_requires_matching_manual_token(self) -> None:
        os.environ["REAL_TRADING_APPROVAL_TOKEN"] = "unit-token"

        with self.assertRaisesRegex(SafetyViolation, "missing"):
            require_explicit_approval()
        with self.assertRaisesRegex(SafetyViolation, "does not match"):
            require_explicit_approval("wrong")

        result = require_explicit_approval("unit-token")
        self.assertTrue(result.passed)
        self.assertFalse(result.failed)

    def test_capital_limits_are_hard_caps(self) -> None:
        order = self._buy_order(quantity=100, price=10.0, daily_notional_used=400)
        self.assertTrue(validate_capital_limits(order, 2000, 2000).passed)

        with self.assertRaisesRegex(SafetyViolation, "max_per_order"):
            validate_capital_limits(order, 999, 2000)
        with self.assertRaisesRegex(SafetyViolation, "max_daily"):
            validate_capital_limits(order, 2000, 1399)

    def test_market_hours_rejects_outside_session_and_accepts_session(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        with self.assertRaisesRegex(SafetyViolation, "outside real trading session"):
            validate_market_hours(datetime(2026, 7, 3, 8, 59, tzinfo=tz))

        result = validate_market_hours(datetime(2026, 7, 3, 10, 0, tzinfo=tz))
        self.assertTrue(result.passed)

    def test_t1_blocks_same_day_sell_and_allows_next_trading_day(self) -> None:
        same_day_sell = self._buy_order(direction="sell", entry_date="2026-07-03", trade_date="2026-07-03")
        with self.assertRaisesRegex(SafetyViolation, "sellable_date"):
            validate_t1_settlement(same_day_sell)

        next_day_sell = self._buy_order(direction="sell", entry_date="2026-07-02", trade_date="2026-07-03")
        self.assertTrue(validate_t1_settlement(next_day_sell).passed)

    def test_emergency_stop_halt_file_blocks(self) -> None:
        halt = self.root / "HALT"
        halt.write_text("stop", encoding="utf-8")

        with self.assertRaisesRegex(SafetyViolation, "halt file exists"):
            emergency_stop_check(halt_files=[halt])

    def test_all_gates_reject_without_approval_even_when_enabled(self) -> None:
        self._enable_env()
        tz = ZoneInfo("Asia/Shanghai")

        with self.assertRaisesRegex(SafetyViolation, "manual confirmation token"):
            run_real_order_gates(
                self._buy_order(),
                now=datetime(2026, 7, 3, 10, 0, tzinfo=tz),
                halt_files=[self.root / "missing_halt"],
            )

    def test_all_gates_pass_only_when_every_guard_is_explicit(self) -> None:
        self._enable_env()
        tz = ZoneInfo("Asia/Shanghai")

        result = run_real_order_gates(
            self._buy_order(),
            approval_token="unit-token",
            now=datetime(2026, 7, 3, 10, 0, tzinfo=tz),
            halt_files=[self.root / "missing_halt"],
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.gate, "all_gates")

    def test_real_signal_queue_rejects_promotion_by_default(self) -> None:
        queue = RealSignalQueue(self.root / "signals", max_per_order=20000, max_daily=50000)

        with self.assertRaises(SafetyViolation):
            queue.promote_from_shadow(self._buy_order(capital_layer="shadow", account_type="none"))

        self.assertFalse((self.root / "signals" / "real" / "pending").exists())

    def test_real_signal_queue_requires_manual_confirm_before_pending(self) -> None:
        self._enable_env()
        tz = ZoneInfo("Asia/Shanghai")
        queue = RealSignalQueue(self.root / "signals", max_per_order=20000, max_daily=50000)
        promoted = queue.promote_from_shadow(
            self._buy_order(capital_layer="shadow", account_type="none", approval_token="unit-token"),
            now=datetime(2026, 7, 3, 10, 0, tzinfo=tz),
        )

        with self.assertRaisesRegex(SafetyViolation, "manual confirmation"):
            queue.submit_to_hermes(promoted["order_id"], now=datetime(2026, 7, 3, 10, 0, tzinfo=tz))

        confirmed = queue.manual_confirm(promoted["order_id"], "unit-token")
        submitted = queue.submit_to_hermes(confirmed["order_id"], now=datetime(2026, 7, 3, 10, 0, tzinfo=tz))

        self.assertEqual(submitted["status"], "pending")
        self.assertTrue((self.root / "signals" / "real" / "pending" / f"{submitted['order_id']}.json").exists())

    def test_real_signal_queue_verifies_receipt_sha256(self) -> None:
        queue = RealSignalQueue(self.root / "signals")
        receipt = {"order_id": "REAL-RECEIPT-1", "status": "filled", "filled_qty": 100}
        receipt["receipt_sha256"] = _payload_sha256(receipt, drop_checksums=True)

        result = queue.track_receipt(receipt)
        self.assertEqual(result["status"], "filled")

        bad = dict(receipt)
        bad["receipt_sha256"] = hashlib.sha256(b"bad").hexdigest()
        with self.assertRaisesRegex(SafetyViolation, "checksum mismatch"):
            queue.track_receipt(bad)


if __name__ == "__main__":
    unittest.main()
