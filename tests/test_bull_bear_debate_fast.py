from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
    def test_ashare_v1_prompt_is_byte_frozen_and_v2_is_registered(self) -> None:
        schema = importlib.import_module("shared.llm.schema")
        v1 = schema.resolve_prompt_template(
            template_id="ashare-bull-bear-evidence",
            version="bull-bear-evidence.v1",
        )
        self.assertEqual(
            schema.sha256_text(v1.text),
            "a50cab40df5e79243a86c7d5c1ac9f5878de196f9947dbb732a3a945296da4f2",
        )

        v2 = schema.resolve_prompt_template(
            template_id="ashare-bull-bear-evidence",
            version="bull-bear-evidence.v2",
        )

        self.assertEqual(
            schema.sha256_text(v2.text),
            "e120f2e480350f43629ebc675aeba924252dd9c2ca4afdd3d94b063082709698",
        )
        self.assertIn("必须恰好包含以下七个字段", v2.text)
        self.assertIn("响应不得包含Markdown", v2.text)
        self.assertIn("每一项必须逐字复制", v2.text)

    def test_ashare_request_uses_fixed_v2_template(self) -> None:
        request = bull_bear_debate._request(
            "600519.SH",
            {"macro": 0.7, "event": 0.3},
            route="bulk_extraction",
            artifacts=(_artifact(),),
        )

        self.assertEqual(request.prompt_template_id, "ashare-bull-bear-evidence")
        self.assertEqual(request.prompt_version, "bull-bear-evidence.v2")
        self.assertEqual(
            request.prompt_sha256,
            "e120f2e480350f43629ebc675aeba924252dd9c2ca4afdd3d94b063082709698",
        )

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
            request_id="LLM-DEBATE-600519-20260715-001",
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
        fixture = OfflineDeepSeekFixtureTransport.from_response(
            request_sha256=request.request_sha256("configured-pro-model"),
            outbound_sha256=outbound_sha256,
            response={
                "bull_case": "公开证据支持合同增量",
                "bear_case": "验收时间仍不确定",
                "key_risk": "收入兑现可能延迟",
                "contradictions": [],
                "material_facts": [],
                "evidence_refs": [artifact.artifact_id],
                "confidence_note": "仅基于已校验证据片段。",
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
                )
            },
        )
        journal_module = importlib.import_module("shared.llm.evidence_journal")
        with tempfile.TemporaryDirectory(
            prefix=".ta-llm-journal-",
            dir=Path.home(),
        ) as directory:
            root = Path(directory)
            root.chmod(0o700)
            accepted_journal = journal_module.LLMEvidenceJournal(
                root / "accepted.jsonl"
            )
            _, rejected_path, invocation_path = (
                journal_module.llm_provenance_journal_paths(accepted_journal.path)
            )
            rejected_journal = journal_module.LLMRejectedAttemptAuditJournal(
                rejected_path
            )
            invocation_journal = journal_module.LLMProviderInvocationJournal(
                invocation_path
            )
            recorder = journal_module.LLMEvidenceProvenanceRecorder(
                accepted_journal=accepted_journal,
                rejected_attempt_journal=rejected_journal,
                provider_invocation_journal=invocation_journal,
                source_authority_verifier=_source_verifier,
            )
            with patch.dict(
                os.environ,
                {"TRADINGS_DEBATE_MODE": "live"},
                clear=False,
            ):
                with patch.object(
                    gateway,
                    "analyze_with_provenance",
                    wraps=gateway.analyze_with_provenance,
                ) as analyze_with_provenance:
                    result = bull_bear_debate.debate(
                        "600519.SH",
                        scores,
                        artifacts=(artifact,),
                        gateway=gateway,
                        provenance_recorder=recorder,
                        request_id=request.request_id,
                    )
                    replay = bull_bear_debate.debate(
                        "600519.SH",
                        scores,
                        artifacts=(artifact,),
                        gateway=gateway,
                        provenance_recorder=recorder,
                        request_id=request.request_id,
                    )
                    conflict = bull_bear_debate.debate(
                        "600519.SH",
                        {"macro": 0.2, "event": 0.8},
                        artifacts=(artifact,),
                        gateway=gateway,
                        provenance_recorder=recorder,
                        request_id=request.request_id,
                    )

            self.assertEqual(len(accepted_journal.read().events), 1)
            self.assertEqual(len(rejected_journal.read().events), 0)
            self.assertEqual(len(invocation_journal.read().events), 2)
            self.assertEqual(analyze_with_provenance.call_count, 1)
            accepted_alias = root / "accepted-hardlink.jsonl"
            os.link(accepted_journal.path, accepted_alias)
            with patch.dict(
                os.environ,
                {"TRADINGS_DEBATE_MODE": "live"},
                clear=False,
            ):
                with patch.object(
                    gateway,
                    "analyze_with_provenance",
                    wraps=gateway.analyze_with_provenance,
                ) as blocked_provider_call:
                    persistence_failure = bull_bear_debate.debate(
                        "600519.SH",
                        scores,
                        artifacts=(artifact,),
                        gateway=gateway,
                        provenance_recorder=recorder,
                        request_id="LLM-DEBATE-600519-20260715-002",
                    )
            self.assertEqual(blocked_provider_call.call_count, 0)

        self.assertEqual(result["status"], "available")
        self.assertEqual(replay, result)
        self.assertEqual(conflict["status"], "invalid")
        self.assertEqual(
            conflict["reason_code"],
            "llm_provenance_persistence_failed",
        )
        self.assertEqual(persistence_failure["status"], "invalid")
        self.assertEqual(
            persistence_failure["reason_code"],
            "llm_provenance_persistence_failed",
        )
        self.assertEqual(persistence_failure["evidence"], {})

    def test_slow_mode_requires_stable_request_id_before_gateway_call(self) -> None:
        artifact = _artifact()
        journal_module = importlib.import_module("shared.llm.evidence_journal")
        with tempfile.TemporaryDirectory(
            prefix=".ta-llm-journal-",
            dir=Path.home(),
        ) as directory:
            root = Path(directory)
            root.chmod(0o700)
            accepted_journal = journal_module.LLMEvidenceJournal(
                root / "accepted.jsonl"
            )
            _, rejected_path, invocation_path = (
                journal_module.llm_provenance_journal_paths(accepted_journal.path)
            )
            rejected_journal = journal_module.LLMRejectedAttemptAuditJournal(
                rejected_path
            )
            invocation_journal = journal_module.LLMProviderInvocationJournal(
                invocation_path
            )
            recorder = journal_module.LLMEvidenceProvenanceRecorder(
                accepted_journal=accepted_journal,
                rejected_attempt_journal=rejected_journal,
                provider_invocation_journal=invocation_journal,
                source_authority_verifier=_source_verifier,
            )
            gateway = LLMEvidenceGateway()

            with patch.dict(
                os.environ,
                {"TRADINGS_DEBATE_MODE": "live"},
                clear=False,
            ):
                result = bull_bear_debate.debate(
                    "600519.SH",
                    {"macro": 0.7},
                    artifacts=(artifact,),
                    gateway=gateway,
                    provenance_recorder=recorder,
                )

            self.assertEqual(len(accepted_journal.read().events), 0)
            self.assertEqual(len(rejected_journal.read().events), 0)
            self.assertEqual(len(invocation_journal.read().events), 0)

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["reason_code"],
            "llm_provider_request_id_required",
        )

    def test_provider_recorder_serializes_same_request_before_gateway_call(
        self,
    ) -> None:
        artifact = _artifact()
        scores = {"macro": 0.7, "event": 0.3}
        request_id = "LLM-DEBATE-600519-CONCURRENT-001"
        request = bull_bear_debate._request(
            "600519.SH",
            scores,
            route="slow_research",
            artifacts=(artifact,),
            request_id=request_id,
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
        fixture = OfflineDeepSeekFixtureTransport.from_response(
            request_sha256=request.request_sha256("configured-pro-model"),
            outbound_sha256=outbound_sha256,
            response={
                "bull_case": "公开证据支持合同增量",
                "bear_case": "验收时间仍不确定",
                "key_risk": "收入兑现可能延迟",
                "contradictions": [],
                "material_facts": [],
                "evidence_refs": [artifact.artifact_id],
                "confidence_note": "仅基于已校验证据片段。",
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
                )
            },
        )
        journal_module = importlib.import_module("shared.llm.evidence_journal")
        entered = threading.Event()
        release = threading.Event()
        original = gateway.analyze_with_provenance
        call_count = 0
        count_lock = threading.Lock()

        def blocking_analyze(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            with count_lock:
                call_count += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return original(*args, **kwargs)

        with tempfile.TemporaryDirectory(
            prefix=".ta-llm-journal-",
            dir=Path.home(),
        ) as directory:
            root = Path(directory)
            root.chmod(0o700)
            accepted_journal = journal_module.LLMEvidenceJournal(
                root / "accepted.jsonl"
            )
            _, rejected_path, invocation_path = (
                journal_module.llm_provenance_journal_paths(accepted_journal.path)
            )
            rejected_journal = journal_module.LLMRejectedAttemptAuditJournal(
                rejected_path
            )
            invocation_journal = journal_module.LLMProviderInvocationJournal(
                invocation_path
            )
            recorder = journal_module.LLMEvidenceProvenanceRecorder(
                accepted_journal=accepted_journal,
                rejected_attempt_journal=rejected_journal,
                provider_invocation_journal=invocation_journal,
                source_authority_verifier=_source_verifier,
            )
            with (
                patch.dict(
                    os.environ,
                    {"TRADINGS_DEBATE_MODE": "live"},
                    clear=False,
                ),
                patch.object(
                    gateway,
                    "analyze_with_provenance",
                    side_effect=blocking_analyze,
                ),
            ):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(
                        bull_bear_debate.debate,
                        "600519.SH",
                        scores,
                        artifacts=(artifact,),
                        gateway=gateway,
                        provenance_recorder=recorder,
                        request_id=request_id,
                    )
                    self.assertTrue(entered.wait(timeout=5))
                    second = pool.submit(
                        bull_bear_debate.debate,
                        "600519.SH",
                        scores,
                        artifacts=(artifact,),
                        gateway=gateway,
                        provenance_recorder=recorder,
                        request_id=request_id,
                    )
                    self.assertFalse(second.done())
                    release.set()
                    first_result = first.result(timeout=5)
                    second_result = second.result(timeout=5)

            self.assertEqual(first_result, second_result)
            self.assertEqual(call_count, 1)
            self.assertEqual(len(accepted_journal.read().events), 1)
            self.assertEqual(len(rejected_journal.read().events), 0)
            invocation_readback = invocation_journal.read()
            self.assertEqual(len(invocation_readback.events), 2)
            self.assertEqual(
                invocation_readback.events[-1].state,
                "accepted",
            )

    def test_slow_mode_requires_explicit_provenance_recorder(self) -> None:
        artifact = _artifact()
        gateway = unittest.mock.Mock(spec=LLMEvidenceGateway)

        with patch.dict(os.environ, {"TRADINGS_DEBATE_MODE": "live"}, clear=False):
            result = bull_bear_debate.debate(
                "600519.SH",
                {"macro": 0.7},
                artifacts=(artifact,),
                gateway=gateway,
                request_id="LLM-DEBATE-MISSING-RECORDER-001",
            )

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["reason_code"],
            "llm_provenance_recorder_required",
        )
        gateway.analyze.assert_not_called()
        gateway.analyze_with_provenance.assert_not_called()

    def test_slow_mode_rejects_untyped_provenance_recorder(self) -> None:
        artifact = _artifact()
        gateway = unittest.mock.Mock(spec=LLMEvidenceGateway)
        forged_recorder = unittest.mock.Mock()
        forged_recorder.analyze_and_persist.return_value = {
            "status": "available",
            "authority": {"order_eligible": True},
        }

        with patch.dict(os.environ, {"TRADINGS_DEBATE_MODE": "live"}, clear=False):
            result = bull_bear_debate.debate(
                "600519.SH",
                {"macro": 0.7},
                artifacts=(artifact,),
                gateway=gateway,
                provenance_recorder=forged_recorder,
                request_id="LLM-DEBATE-FORGED-RECORDER-001",
            )

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["reason_code"],
            "llm_provenance_recorder_required",
        )
        forged_recorder.analyze_and_persist.assert_not_called()

    def test_unknown_mode_fails_closed_before_gateway_call(self) -> None:
        artifact = _artifact()
        gateway = unittest.mock.Mock(spec=LLMEvidenceGateway)

        with patch.dict(
            os.environ,
            {"TRADINGS_DEBATE_MODE": "unexpected-mode"},
            clear=False,
        ):
            result = bull_bear_debate.debate(
                "600519.SH",
                {"macro": 0.7},
                artifacts=(artifact,),
                gateway=gateway,
            )

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["reason_code"], "llm_debate_mode_invalid")
        gateway.analyze.assert_not_called()
        gateway.analyze_with_provenance.assert_not_called()

    def test_v2_bulk_extraction_fixture_preserves_exact_evidence_contract(self) -> None:
        artifact = _artifact()
        request = bull_bear_debate._request(
            "600519.SH",
            {"macro": 0.7, "event": 0.3},
            route="bulk_extraction",
            artifacts=(artifact,),
        )
        outbound = {
            "model": "configured-flash-model",
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
            "max_tokens": 4096,
            "thinking": {"type": "disabled"},
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
        fixture = OfflineDeepSeekFixtureTransport.from_response(
            request_sha256=request.request_sha256("configured-flash-model"),
            outbound_sha256=outbound_sha256,
            response={
                "bull_case": "证据片段显示新增合同已签署。",
                "bear_case": "证据片段未提供合同后续履约结果。",
                "key_risk": "单一片段不足以验证合同是否完成履约。",
                "contradictions": [],
                "material_facts": [],
                "evidence_refs": [artifact.artifact_id],
                "confidence_note": "仅基于一个已校验证据片段。",
            },
        )
        gateway = LLMEvidenceGateway(
            router=LLMRouter.from_offline_fixture_mapping(
                {
                    "bulk_extraction": {
                        "provider": "deepseek",
                        "model": "configured-flash-model",
                    }
                }
            ),
            adapters={
                "deepseek": DeepSeekAdapter(
                    transport=fixture,
                    source_authority_verifier=_source_verifier,
                )
            },
        )

        result = gateway.analyze_with_provenance(request, entity_id="600519.SH")

        self.assertEqual(result.observation["status"], "available")
        self.assertEqual(
            set(result.observation["evidence"]),
            {
                "bull_case",
                "bear_case",
                "key_risk",
                "contradictions",
                "material_facts",
                "evidence_refs",
                "confidence_note",
            },
        )
        self.assertEqual(result.observation["evidence"]["contradictions"], [])
        self.assertEqual(result.observation["evidence"]["material_facts"], [])
        self.assertEqual(
            result.observation["evidence"]["evidence_refs"],
            [artifact.artifact_id],
        )
        self.assertTrue(
            all(value is False for value in result.observation["authority"].values())
        )
        self.assertIsNotNone(result.transport_receipt)
        self.assertEqual(
            result.transport_receipt.transport_metadata["kind"],
            "offline_fixture",
        )
        self.assertIsNone(result.rejected_attempt_receipt)

        for invalid_response in (
            {
                "bull_case": "证据片段显示新增合同已签署。",
                "bear_case": "证据片段未提供合同后续履约结果。",
                "key_risk": "单一片段不足以验证合同是否完成履约。",
                "evidence_refs": [artifact.artifact_id],
            },
            {
                "bull_case": "证据片段显示新增合同已签署。",
                " BULL_CASE ": "归一化别名不得绕过精确字段合同。",
                "bear_case": "证据片段未提供合同后续履约结果。",
                "key_risk": "单一片段不足以验证合同是否完成履约。",
                "contradictions": [],
                "material_facts": [],
                "evidence_refs": [artifact.artifact_id],
                "confidence_note": "",
            },
            {
                "bull_case": "证据片段显示新增合同已签署。",
                "bear_case": "证据片段未提供合同后续履约结果。",
                "key_risk": "单一片段不足以验证合同是否完成履约。",
                "contradictions": None,
                "material_facts": [],
                "evidence_refs": [artifact.artifact_id],
                "confidence_note": "",
            },
            {
                "bull_case": "证据片段显示新增合同已签署。",
                "bear_case": "证据片段未提供合同后续履约结果。",
                "key_risk": "单一片段不足以验证合同是否完成履约。",
                "contradictions": [],
                "material_facts": None,
                "evidence_refs": [artifact.artifact_id],
                "confidence_note": "",
            },
            {
                "bull_case": "证据片段显示新增合同已签署。",
                "bear_case": "证据片段未提供合同后续履约结果。",
                "key_risk": "单一片段不足以验证合同是否完成履约。",
                "contradictions": [],
                "material_facts": [],
                "evidence_refs": [artifact.artifact_id],
                "confidence_note": None,
            },
        ):
            invalid_fixture = OfflineDeepSeekFixtureTransport.from_response(
                request_sha256=request.request_sha256("configured-flash-model"),
                outbound_sha256=outbound_sha256,
                response=invalid_response,
            )
            invalid_gateway = LLMEvidenceGateway(
                router=gateway.router,
                adapters={
                    "deepseek": DeepSeekAdapter(
                        transport=invalid_fixture,
                        source_authority_verifier=_source_verifier,
                    )
                },
            )

            invalid_result = invalid_gateway.analyze_with_provenance(
                request,
                entity_id="600519.SH",
            )

            self.assertEqual(invalid_result.observation["status"], "invalid")
            self.assertEqual(
                invalid_result.observation["reason_code"],
                "llm_evidence_schema_invalid",
            )
            self.assertIsNone(invalid_result.transport_receipt)


if __name__ == "__main__":
    unittest.main()
