from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest.mock import patch

from shared.adversarial import bull_bear_debate
from shared.llm.evidence_artifact import (
    EvidenceArtifact,
    EvidenceSourceAuthorityReceipt,
    sha256_document,
)
from shared.llm.gateway import (
    DeepSeekAdapter,
    LLMEvidenceGateway,
    OfflineDeepSeekFixtureTransport,
)
from shared.llm.router import LLMRouter


def _artifact() -> EvidenceArtifact:
    document = "公司公告披露新增合同已签署，交付仍取决于客户验收。"
    span = "新增合同已签署"
    start = document.index(span)
    receipt = EvidenceSourceAuthorityReceipt.create(
        receipt_id="source-receipt-debate-001",
        source_system="official-disclosure-fixture",
        source_document_id="doc-debate-001",
        document_sha256=sha256_document(document),
        available_at="2026-07-15T08:05:00+08:00",
        issued_at="2026-07-15T08:06:00+08:00",
    )
    return EvidenceArtifact.create(
        document_text=document,
        published_at="2026-07-15T08:00:00+08:00",
        available_at="2026-07-15T08:05:00+08:00",
        span_start=start,
        span_end=start + len(span),
        entity_resolution_version="ashare-entity-resolution.v1",
        source_authority_receipt=receipt,
    )


def _source_verifier(**kwargs: object) -> bool:
    artifact = kwargs["artifact"]
    receipt = kwargs["receipt"]
    return getattr(receipt, "document_sha256") == getattr(artifact, "document_sha256")


class BullBearDebateFastModeTest(unittest.TestCase):
    def test_missing_evidence_artifacts_never_becomes_available(self) -> None:
        scores = {"combined": 0.68, "macro": 0.7, "event": 0.3, "technical": 0.61}
        with patch.dict(os.environ, {"TRADINGS_DEBATE_MODE": "fast"}, clear=False):
            with patch.object(
                bull_bear_debate,
                "_call_deepseek",
                side_effect=AssertionError("should not call llm"),
            ):
                result = bull_bear_debate.debate("600519.SH", scores)
        self.assertEqual(result["record_type"], "llm_evidence_observation")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "verified_evidence_artifact_required")
        self.assertNotIn("belief_score", result)
        self.assertEqual(result["evidence"], {})
        for authority_flag in (
            "decision_eligible",
            "risk_eligible",
            "trade_intent_eligible",
            "order_eligible",
            "position_eligible",
            "real_trading_enabled",
        ):
            self.assertIs(result["authority"][authority_flag], False)

    def test_fast_mode_requires_external_source_verification(self) -> None:
        scores = {"combined": 0.68, "macro": 0.7, "event": 0.3}
        with patch.dict(os.environ, {"TRADINGS_DEBATE_MODE": "fast"}, clear=False):
            result = bull_bear_debate.debate(
                "600519.SH",
                scores,
                artifacts=(_artifact(),),
                source_authority_verifier=_source_verifier,
            )

        self.assertEqual(result["status"], "available")
        self.assertIn("macro", result["evidence"]["bull_case"])
        self.assertEqual(result["evidence_refs"], result["evidence"]["evidence_refs"])

    def test_cloud_mode_reaches_deepseek_only_with_verified_artifacts(self) -> None:
        artifact = _artifact()
        scores = {"macro": 0.7, "event": 0.3}
        request = bull_bear_debate._request(
            "600519.SH",
            scores,
            route="slow_research",
            artifacts=(artifact,),
        )
        outbound = {
            "model": "configured-pro-model",
            "messages": [
                {"role": "system", "content": request.prompt_text},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "payload": request.payload,
                            "untrusted_artifact_data": [
                                item.to_request_descriptor()
                                for item in request.artifacts
                            ],
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": 8192,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        outbound_sha256 = hashlib.sha256(
            json.dumps(
                outbound,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipts = []
        fixture = OfflineDeepSeekFixtureTransport.from_response(
            request_sha256=request.request_sha256("configured-pro-model"),
            outbound_sha256=outbound_sha256,
            response={
                "bull_case": "公开证据支持合同增量",
                "bear_case": "验收时间仍不确定",
                "key_risk": "收入兑现可能延迟",
                "evidence_refs": [artifact.artifact_id],
            },
        )

        gateway = LLMEvidenceGateway(
            router=LLMRouter.from_offline_fixture_mapping(
                {
                    "slow_research": {
                        "provider": "deepseek",
                        "model": "configured-pro-model",
                    }
                }
            ),
            adapters={
                "deepseek": DeepSeekAdapter(
                    transport=fixture,
                    source_authority_verifier=_source_verifier,
                    receipt_sink=receipts.append,
                )
            },
        )
        with patch.dict(os.environ, {"TRADINGS_DEBATE_MODE": "live"}, clear=False):
            with patch.object(bull_bear_debate, "_request", return_value=request):
                result = bull_bear_debate.debate(
                    "600519.SH",
                    scores,
                    artifacts=(artifact,),
                    gateway=gateway,
                )

        self.assertEqual(result["status"], "available")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].provider, "deepseek")
        self.assertEqual(receipts[0].model, "configured-pro-model")
        self.assertEqual(receipts[0].request_sha256, fixture.request_sha256)


if __name__ == "__main__":
    unittest.main()
