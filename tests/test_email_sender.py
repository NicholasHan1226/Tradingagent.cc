from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.notify import email_sender


class EmailSenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)

        for name, value in (
            ("EMAIL_LOG", self.tmp_path / "emails_sent.jsonl"),
            ("LOCAL_FALLBACK_DIR", self.tmp_path / "email_fallback"),
        ):
            patcher = patch.object(email_sender, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_send_email_prefers_cloudflare_when_available(self) -> None:
        with (
            patch.object(email_sender, "load_env_from_file", return_value=[]),
            patch.object(email_sender, "_send_via_cloudflare", return_value={
                "provider": "cloudflare",
                "message_id": "cf-123",
                "status_code": 200,
            }) as cloudflare,
            patch.object(email_sender, "_send_via_deadsimple") as deadsimple,
            patch.object(email_sender, "_send_via_smtp") as smtp,
        ):
            result = email_sender.send_email(
                "user@example.com",
                "Trade summary",
                "plain body",
                "<p>html body</p>",
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["provider"], "cloudflare")
        self.assertEqual(result["message_id"], "cf-123")
        self.assertEqual(result["from"], "notice@tradingagent.cc")
        cloudflare.assert_called_once()
        deadsimple.assert_not_called()
        smtp.assert_not_called()

    def test_send_email_falls_back_to_local_save_when_providers_fail(self) -> None:
        with (
            patch.object(email_sender, "load_env_from_file", return_value=[]),
            patch.object(email_sender, "_send_via_cloudflare", side_effect=RuntimeError("cf down")),
            patch.object(email_sender, "_send_via_deadsimple", side_effect=RuntimeError("ds down")),
            patch.object(email_sender, "_send_via_smtp", side_effect=RuntimeError("smtp down")),
        ):
            result = email_sender.send_email(
                "ops@example.com",
                "System alert",
                "fallback body",
                "<p>fallback html</p>",
                channel="system",
            )

        self.assertEqual(result["status"], "saved_local")
        self.assertEqual(result["provider"], "local_file")
        self.assertEqual(result["from"], "notice@tradingagent.cc")
        saved_path = Path(result["saved_to"])
        self.assertTrue(saved_path.exists())
        saved_payload = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_payload["subject"], "System alert")
        self.assertEqual(len(result["errors"]), 3)

        log_rows = [
            json.loads(line)
            for line in email_sender.EMAIL_LOG.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(log_rows), 1)
        self.assertEqual(log_rows[0]["status"], "saved_local")

    def test_send_template_email_renders_html_and_dispatches(self) -> None:
        captured: dict[str, str] = {}

        def _capture(to: str, subject: str, body: str, html_body: str, from_addr: str) -> dict[str, object]:
            captured["to"] = to
            captured["subject"] = subject
            captured["body"] = body
            captured["html_body"] = html_body
            captured["from_addr"] = from_addr
            return {"provider": "cloudflare", "message_id": "tmpl-1", "status_code": 200}

        with (
            patch.object(email_sender, "load_env_from_file", return_value=[]),
            patch.object(email_sender, "_send_via_cloudflare", side_effect=_capture),
            patch.object(email_sender, "_send_via_deadsimple") as deadsimple,
            patch.object(email_sender, "_send_via_smtp") as smtp,
        ):
            result = email_sender.send_template_email(
                "daily_report",
                {
                    "date": "2026-06-30",
                    "total_pnl": 1234.5,
                    "total_pnl_pct": 0.032,
                    "benchmark_pnl_pct": 0.011,
                    "trades": [{"ts_code": "600000", "side": "buy", "quantity": 100, "price": 12.3, "pnl": 45.6}],
                    "attribution": [{"factor": "事件面", "contribution": 0.02}],
                    "holdings": [{"ts_code": "600000", "name": "浦发银行", "pnl_pct": 0.015}],
                    "tomorrow_plan": [{"action": "观察", "target": "600000", "reason": "量价延续"}],
                    "summary": "日报摘要",
                },
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["provider"], "cloudflare")
        self.assertEqual(captured["to"], "tradingadviser@coze.email")
        self.assertEqual(captured["from_addr"], "notice@tradingagent.cc")
        self.assertEqual(captured["subject"], "Tradings 日报 2026-06-30")
        self.assertIn("日报 | 2026-06-30", captured["html_body"])
        self.assertIn("今日盈亏", captured["html_body"])
        self.assertEqual(captured["body"], "日报摘要")
        deadsimple.assert_not_called()
        smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
