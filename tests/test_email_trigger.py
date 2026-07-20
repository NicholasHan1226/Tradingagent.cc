from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.markets.base import MarketAdapter
from shared.governance.retirement import RetiredRuntimeError
from shared.notify import email_sender
from shared.orchestrator import OrchestratorDeps, run_shadow_loop, run_sim_loop


class TriggerAdapter(MarketAdapter):
    def get_universe(self, date: str) -> list[str]:
        return ["AAA"]

    def get_market(self) -> str:
        return "cn_futures"

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return "cn_futures", symbol

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "shadow_capital": 10000.0,
            "sim_capital": 10000.0,
            "portfolio_method": "conviction_weighted",
            "regime": "growth",
            "max_candidates": 1,
            "default_price": 10.0,
            "default_volatility": 0.20,
        }

    def get_shadow_account(self) -> str:
        return "cn_futures_shadow"

    def get_sim_account(self) -> dict[str, object]:
        return {
            "account": "cn_futures_sim",
            "sim_capital": 10000.0,
            "positions": [],
        }


class CryptoTriggerAdapter(TriggerAdapter):
    def get_market(self) -> str:
        return "crypto"

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return "crypto", "BTCUSDT"


class TriggerReader:
    def get_bars_daily(
        self, market: str, symbol: str, start: object = None, end: object = None
    ) -> list[dict[str, float]]:
        return [{"close": 10.0}, {"close": 10.2}]


def _audit_event(**kwargs: object) -> dict[str, object]:
    return {
        "audit_id": f"audit-{kwargs.get('stage')}-{kwargs.get('ts_code')}",
        "stage": kwargs.get("stage"),
        "ts_code": kwargs.get("ts_code"),
    }


class EmailTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        self.sent: list[dict[str, object]] = []

    def _send_email(
        self, to: str, subject: str, body: str, html_body: str, **kwargs: object
    ) -> dict[str, object]:
        record = {
            "to": to,
            "subject": subject,
            "body": body,
            "html_body": html_body,
            **kwargs,
        }
        self.sent.append(record)
        return {
            "status": "sent",
            "provider": "mock",
            "message_id": f"mock-{len(self.sent)}",
        }

    def _deps(self, *, sim: bool = False) -> OrchestratorDeps:
        def score_stock(
            market: str, symbol: str, reader: object = None, date: str | None = None
        ) -> dict[str, object]:
            return {"combined": 0.8, "technical": 0.75, "sector": "unit"}

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {
                "candidate": list(universe),
                "watch": [],
                "holdings": [],
                "universe": list(universe),
            }

        def debate(symbol: str, scores: dict[str, object]) -> dict[str, object]:
            return {"ts_code": symbol, "belief_score": 0.7}

        def risk_check(
            order: dict[str, object], portfolio: dict[str, object]
        ) -> dict[str, object]:
            return {
                "approved": True,
                "adjusted_weight": 0.05,
                "adjustments": [],
                "reasons": ["unit trigger"],
            }

        def construct(
            orders: list[dict[str, object]], capital: float, method: str, regime: str
        ) -> dict[str, object]:
            return {
                "positions": [
                    {
                        "ts_code": "AAA",
                        "shares": 10,
                        "price": 10.0,
                        "weight": 0.05,
                        "sector": "unit",
                    }
                ],
                "total_weight": 0.05,
                "cash_weight": 0.95,
            }

        def size_position(belief_score: float, volatility: float, regime: str) -> float:
            return 0.05

        def record_shadow(order: dict[str, object], account: str) -> dict[str, object]:
            return {"recorded": True, "status": "recorded", "trade_id": "shadow-1"}

        def review(
            date: str, session: str = "close", capital_layer: str = "shadow"
        ) -> dict[str, object]:
            return {
                "trade_date": date,
                "session": session,
                "capital_layer": capital_layer,
            }

        def execute_sim_order(
            order: dict[str, object], account: object = None
        ) -> dict[str, object]:
            return {
                "order_id": order["order_id"],
                "status": "filled",
                "filled_price": 10.1,
                "filled_quantity": order["quantity"],
                "fill_time": "2026-06-30T10:00:00",
            }

        return OrchestratorDeps(
            score_stock=score_stock,
            build_pool=build_pool,
            debate=debate,
            risk_check=risk_check,
            construct=construct,
            size_position=size_position,
            record_shadow=record_shadow,
            run_review=review,
            record_audit_event=_audit_event,
            execute_sim_order=execute_sim_order if sim else None,
            send_email=self._send_email,
        )

    def test_shadow_signal_pushes_trading_signal_email_immediately(self) -> None:
        result = run_shadow_loop(
            TriggerAdapter(),
            "20260630",
            TriggerReader(),
            deps=self._deps(),
            signals_dir=self.tmp_path / "shadow_signals",
        )

        self.assertEqual(result["recorded_count"], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["rate_limit_type"], "trading_signal")
        self.assertEqual(self.sent[0]["channel"], "trading")
        self.assertIn("影子盘新信号", str(self.sent[0]["subject"]))
        self.assertIn("交易信号", str(self.sent[0]["html_body"]))
        self.assertEqual(result["records"][0]["email_notification"]["status"], "sent")

    def test_sim_fill_pushes_trade_receipt_email_immediately(self) -> None:
        result = run_sim_loop(
            TriggerAdapter(),
            "20260630",
            TriggerReader(),
            deps=self._deps(sim=True),
            signals_dir=self.tmp_path / "sim_signals",
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["rate_limit_type"], "trade_receipt")
        self.assertEqual(self.sent[0]["channel"], "trading")
        self.assertIn("模拟盘成交回执", str(self.sent[0]["subject"]))
        self.assertIn("交易回执", str(self.sent[0]["html_body"]))
        self.assertEqual(result["records"][0]["email_notification"]["status"], "sent")

    def test_crypto_shared_orchestrators_retire_before_email_or_signal_write(
        self,
    ) -> None:
        for runner, sim in ((run_shadow_loop, False), (run_sim_loop, True)):
            with self.subTest(runner=runner.__name__):
                signals_dir = self.tmp_path / runner.__name__
                with self.assertRaisesRegex(
                    RetiredRuntimeError, "legacy_runtime_retired"
                ):
                    runner(
                        CryptoTriggerAdapter(),
                        "20260630",
                        TriggerReader(),
                        deps=self._deps(sim=sim),
                        signals_dir=signals_dir,
                    )
                self.assertFalse(signals_dir.exists())

        self.assertEqual(self.sent, [])

    def test_send_email_rate_limit_blocks_same_type_within_five_minutes(self) -> None:
        log_path = self.tmp_path / "emails_sent.jsonl"
        fallback_dir = self.tmp_path / "fallback"
        state_path = self.tmp_path / "email_rate_limit.json"

        with (
            patch.object(email_sender, "EMAIL_LOG", log_path),
            patch.object(email_sender, "LOCAL_FALLBACK_DIR", fallback_dir),
            patch.object(email_sender, "RATE_LIMIT_STATE", state_path),
            patch.object(email_sender, "load_env_from_file", return_value=[]),
            patch.object(
                email_sender,
                "_send_via_cloudflare",
                return_value={
                    "provider": "cloudflare",
                    "message_id": "cf-1",
                    "status_code": 200,
                },
            ) as cloudflare,
        ):
            first = email_sender.send_email(
                "user@example.com",
                "tradingagent 交易信号",
                "first",
                "<p>first</p>",
                rate_limit_type="trading_signal",
            )
            second = email_sender.send_email(
                "user@example.com",
                "tradingagent 交易信号",
                "second",
                "<p>second</p>",
                rate_limit_type="trading_signal",
            )

        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "rate_limited")
        cloudflare.assert_called_once()
        self.assertFalse(fallback_dir.exists())


if __name__ == "__main__":
    unittest.main()
