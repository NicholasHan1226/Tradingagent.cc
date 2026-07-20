from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.execution.shadow_signal import write_shadow_signal
from shared.execution.signal_state_machine import SignalStateMachine, read_json
from shared.governance.retirement import RetiredRuntimeError


class ShadowSignalTest(unittest.TestCase):
    def _card(self, order_id: str = "SHADOW-1") -> dict[str, object]:
        return {
            "order_id": order_id,
            "market": "us",
            "symbol": "AAPL",
            "status": "pending",
            "queue_scope": "shadow",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "direct_execution": False,
            "real_execution": False,
            "simulated_fill": {
                "status": "filled",
                "symbol": "AAPL",
                "quantity": 1,
                "avg_price": 100.0,
            },
        }

    def test_filled_simulated_shadow_card_settles_to_shadow_filled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = write_shadow_signal(self._card(), Path(tmp))
            filled = Path(tmp) / "shadow" / "filled" / "SHADOW-1.json"

            self.assertEqual(result["status"], "filled")
            self.assertTrue(filled.exists())
            self.assertFalse(
                (Path(tmp) / "shadow" / "pending" / "SHADOW-1.json").exists()
            )

    def test_shadow_settlement_failure_moves_card_to_failed_not_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                SignalStateMachine, "fill", side_effect=RuntimeError("boom")
            ):
                result = write_shadow_signal(self._card("SHADOW-FAIL"), Path(tmp))

            failed = Path(tmp) / "shadow" / "failed" / "SHADOW-FAIL.json"
            pending = Path(tmp) / "shadow" / "pending" / "SHADOW-FAIL.json"
            card = read_json(failed)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(failed.exists())
            self.assertFalse(pending.exists())
            self.assertIn("boom", card["failure_reason"])

    def test_crypto_identity_is_rejected_before_any_signal_write(self) -> None:
        cases = (
            {"market": "crypto", "symbol": "BTCUSDT"},
            {"symbol": "BTCUSD"},
            {"market": "ashare", "ts_code": "BTC/USDT"},
            {"base_asset": "BTC", "quote_asset": "USDT"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "signals"
            for index, identity in enumerate(cases):
                card = self._card(f"CRYPTO-{index}")
                card.update(identity)
                with self.subTest(identity=identity):
                    with self.assertRaisesRegex(
                        RetiredRuntimeError, "legacy_runtime_retired"
                    ):
                        write_shadow_signal(card, root)

            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
