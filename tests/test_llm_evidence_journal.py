from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from shared.llm.evidence_artifact import (
    EvidenceArtifact,
    EvidenceSourceAuthorityReceipt,
    sha256_document,
)
from shared.llm.evidence_journal import (
    EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
    LLMEvidenceEnvelope,
    LLMEvidenceEnvelopeError,
    LLMEvidenceJournal,
    LLMEvidenceJournalError,
)
from shared.llm.gateway import ProviderTransportReceipt
from shared.llm.schema import (
    LLMEvidenceRequest,
    available_observation,
    normalize_observation,
    sha256_text,
)


MODEL = "configured-pro-model"
VERIFIED_AT = "2026-07-16T08:20:00+08:00"
RECEIVED_AT = "2026-07-16T08:20:01+08:00"


def _sha256_json(value: object) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


class _SourceVerifier:
    verifier_id = "fixture-source-authority-verifier"
    verifier_version = "2026-07-16.v1"

    @staticmethod
    def verify(*, artifact: EvidenceArtifact, receipt: object) -> bool:
        return receipt.document_sha256 == artifact.document_sha256


def _request(*, request_id: str = "REQ-JOURNAL-001") -> LLMEvidenceRequest:
    document = "公司公告披露新增合同已签署，交付仍取决于客户验收。"
    span = "新增合同已签署"
    start = document.index(span)
    receipt = EvidenceSourceAuthorityReceipt.create(
        receipt_id=f"source-{request_id}",
        source_system="official-disclosure-fixture",
        source_document_id=f"document-{request_id}",
        document_sha256=sha256_document(document),
        available_at="2026-07-16T08:05:00+08:00",
        issued_at="2026-07-16T08:06:00+08:00",
    )
    artifact = EvidenceArtifact.create(
        document_text=document,
        published_at="2026-07-16T08:00:00+08:00",
        available_at="2026-07-16T08:05:00+08:00",
        span_start=start,
        span_end=start + len(span),
        entity_resolution_version="ashare-entity-resolution.v1",
        source_authority_receipt=receipt,
    )
    return LLMEvidenceRequest.create(
        request_id=request_id,
        task_type="event_evidence_extraction",
        route="slow_research",
        prompt_template_id="general-evidence-review",
        prompt_version="bull-bear.v1",
        document_cutoff="2026-07-16T08:30:00+08:00",
        evidence_refs=(artifact.artifact_id,),
        artifacts=(artifact,),
        payload={"symbol": "600000.SH", "event_type": "contract"},
    )


def _components(*, request_id: str = "REQ-JOURNAL-001"):
    request = _request(request_id=request_id)
    verifier = _SourceVerifier()
    material = request.validate_for_transport(
        MODEL,
        source_authority_verifier=verifier,
        verified_at=VERIFIED_AT,
    )
    observation = available_observation(
        request,
        provider="deepseek",
        model=MODEL,
        raw_evidence={
            "bull_case": "合同事实有已验证来源",
            "bear_case": "客户验收仍有不确定性",
            "key_risk": "收入确认可能延后",
            "evidence_refs": list(request.evidence_refs),
        },
        entity_id="600000.SH",
    )
    receipt = ProviderTransportReceipt.create(
        provider="deepseek",
        model=MODEL,
        transport_id="offline-deepseek-fixture",
        transport_version="offline-fixture-v1",
        verified_at=VERIFIED_AT,
        request_sha256=material["metadata"]["request_sha256"],
        source_authority_proof_set_sha256=material["metadata"][
            "source_authority_proof_set_sha256"
        ],
        transport_material_sha256=_sha256_json(material),
        outbound_sha256="a" * 64,
        response_sha256="b" * 64,
        normalized_evidence_sha256=observation["output_sha256"],
        provider_response_id="fixture-response-001",
        received_at=RECEIVED_AT,
        transport_metadata={
            "kind": "offline_fixture",
            "endpoint": "offline://deepseek-fixture",
            "method": "FIXTURE_RESOLVE",
            "egress_policy_version": "offline-fixture-v1",
            "http_status": 0,
            "content_type": "application/json",
            "request_bytes": 128,
            "response_bytes": 256,
            "attempt_count": 1,
            "retry_disposition": "not_applicable",
        },
    )
    return request, verifier, receipt, observation


def _envelope(*, request_id: str = "REQ-JOURNAL-001") -> LLMEvidenceEnvelope:
    request, verifier, receipt, observation = _components(request_id=request_id)
    return LLMEvidenceEnvelope.create(
        run_id=f"llm-run-{receipt.receipt_sha256}",
        request=request,
        source_authority_verifier=verifier,
        transport_receipt=receipt,
        observation=observation,
    )


def test_envelope_binds_request_source_proofs_transport_and_observation() -> None:
    envelope = _envelope()

    envelope.verify_integrity()
    assert envelope.request_sha256 == envelope.transport_receipt["request_sha256"]
    assert (
        envelope.source_authority_proof_set_sha256
        == envelope.transport_receipt["source_authority_proof_set_sha256"]
    )
    assert (
        envelope.transport_material_sha256
        == envelope.transport_receipt["transport_material_sha256"]
    )
    assert envelope.observation["status"] == "available"
    assert envelope.transport_receipt["transport_metadata"] == {
        "kind": "offline_fixture",
        "endpoint": "offline://deepseek-fixture",
        "method": "FIXTURE_RESOLVE",
        "egress_policy_version": "offline-fixture-v1",
        "http_status": 0,
        "content_type": "application/json",
        "request_bytes": 128,
        "response_bytes": 256,
        "attempt_count": 1,
        "retry_disposition": "not_applicable",
    }
    assert envelope.shadow_only is True
    assert envelope.production_eligible is False
    assert all(value is False for value in envelope.authority.values())


def test_envelope_rejects_swapped_receipt_and_mutated_observation() -> None:
    request, verifier, receipt, observation = _components()
    other_request, _, other_receipt, _ = _components(request_id="REQ-JOURNAL-OTHER")
    assert other_request.request_id != request.request_id

    with pytest.raises(LLMEvidenceEnvelopeError, match="request_sha256_mismatch"):
        LLMEvidenceEnvelope.create(
            run_id="llm-run-swapped",
            request=request,
            source_authority_verifier=verifier,
            transport_receipt=other_receipt,
            observation=observation,
        )

    mutated = {**observation, "entity_id": "600001.SH"}
    with pytest.raises(LLMEvidenceEnvelopeError, match="observation_binding_invalid"):
        LLMEvidenceEnvelope.create(
            run_id="llm-run-mutated",
            request=request,
            source_authority_verifier=verifier,
            transport_receipt=receipt,
            observation=mutated,
        )


def test_envelope_rejects_same_request_different_response_receipt_swap() -> None:
    request, verifier, receipt, observation = _components()
    alternate_observation = available_observation(
        request,
        provider="deepseek",
        model=MODEL,
        raw_evidence={
            "bull_case": "替代响应声称合同影响更强",
            "bear_case": "客户验收仍有不确定性",
            "key_risk": "收入确认可能延后",
            "evidence_refs": list(request.evidence_refs),
        },
        entity_id="600000.SH",
    )
    alternate_receipt = ProviderTransportReceipt.create(
        provider=receipt.provider,
        model=receipt.model,
        transport_id=receipt.transport_id,
        transport_version=receipt.transport_version,
        verified_at=receipt.verified_at,
        request_sha256=receipt.request_sha256,
        source_authority_proof_set_sha256=(receipt.source_authority_proof_set_sha256),
        transport_material_sha256=receipt.transport_material_sha256,
        outbound_sha256=receipt.outbound_sha256,
        response_sha256="c" * 64,
        normalized_evidence_sha256=alternate_observation["output_sha256"],
        provider_response_id="fixture-response-002",
        received_at=receipt.received_at,
        transport_metadata=receipt.transport_metadata,
    )

    with pytest.raises(
        LLMEvidenceEnvelopeError,
        match="observation_receipt_binding_invalid",
    ):
        LLMEvidenceEnvelope.create(
            run_id=f"llm-run-{alternate_receipt.receipt_sha256}",
            request=request,
            source_authority_verifier=verifier,
            transport_receipt=alternate_receipt,
            observation=observation,
        )


def test_envelope_run_id_is_derived_from_transport_receipt() -> None:
    request, verifier, receipt, observation = _components()
    with pytest.raises(
        LLMEvidenceEnvelopeError,
        match="run_id_receipt_binding_invalid",
    ):
        LLMEvidenceEnvelope.create(
            run_id="llm-run-caller-selected",
            request=request,
            source_authority_verifier=verifier,
            transport_receipt=receipt,
            observation=observation,
        )


def test_envelope_integrity_rejects_post_creation_mutation() -> None:
    envelope = _envelope()
    forged = replace(envelope, envelope_sha256="0" * 64)

    with pytest.raises(LLMEvidenceEnvelopeError, match="envelope_sha256_mismatch"):
        forged.verify_integrity()


def test_envelope_keeps_defensive_nested_json_snapshots() -> None:
    envelope = _envelope()

    transport_metadata = envelope.transport_receipt["transport_metadata"]
    transport_metadata["request_bytes"] = 999
    evidence = envelope.observation["evidence"]
    evidence["bull_case"] = "tampered"

    assert envelope.transport_receipt["transport_metadata"]["request_bytes"] == 128
    assert envelope.observation["evidence"]["bull_case"] != "tampered"
    envelope.verify_integrity()

    reloaded = LLMEvidenceEnvelope.from_payload(envelope.canonical_payload())
    reloaded_metadata = reloaded.transport_receipt["transport_metadata"]
    reloaded_metadata["request_bytes"] = 777

    assert reloaded.transport_receipt["transport_metadata"]["request_bytes"] == 128
    reloaded.verify_integrity()


def test_persisted_observation_cannot_drop_bound_citation_and_rehash() -> None:
    request, verifier, _, observation = _components()
    forged_evidence = {**observation["evidence"], "evidence_refs": []}
    forged = {
        **observation,
        "evidence": forged_evidence,
        "output_sha256": sha256_text(
            __import__("json").dumps(
                forged_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    }

    normalized = normalize_observation(
        forged,
        request=request,
        source_authority_verifier=verifier,
    )

    assert normalized["status"] == "invalid"
    assert normalized["reason_code"] == "invalid_llm_evidence_schema"


def test_journal_is_append_only_idempotent_and_checksum_chained(tmp_path: Path) -> None:
    journal = LLMEvidenceJournal(tmp_path / "llm_evidence.jsonl")
    first = _envelope()
    second = _envelope(request_id="REQ-JOURNAL-002")

    assert journal.read().head_sha256 == EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256
    assert (
        journal.append(
            first,
            expected_head_sha256=EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
        )
        is True
    )
    first_head = journal.read().head_sha256
    assert journal.append(first, expected_head_sha256=first_head) is False
    assert journal.append(second, expected_head_sha256=first_head) is True

    readback = journal.read()
    assert len(readback.events) == 2
    assert readback.latest_by_run[first.run_id] == first
    assert readback.latest_by_run[second.run_id] == second
    assert readback.events[1].previous_event_sha256 == first_head


def test_journal_fails_closed_on_tamper_partial_write_and_symlink(
    tmp_path: Path,
) -> None:
    path = tmp_path / "llm_evidence.jsonl"
    journal = LLMEvidenceJournal(path)
    assert (
        journal.append(
            _envelope(),
            expected_head_sha256=EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
        )
        is True
    )

    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("600000.SH", "600001.SH"), encoding="utf-8")
    with pytest.raises(LLMEvidenceJournalError):
        journal.read()

    path.write_text(original.rstrip("\n"), encoding="utf-8")
    with pytest.raises(LLMEvidenceJournalError, match="journal_partial_line"):
        journal.read()

    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    symlink = tmp_path / "link.jsonl"
    symlink.symlink_to(target)
    with pytest.raises(LLMEvidenceJournalError, match="journal_symlink_forbidden"):
        LLMEvidenceJournal(symlink)


def test_journal_requires_cas_and_rejects_alternate_run_replay(tmp_path: Path) -> None:
    journal = LLMEvidenceJournal(tmp_path / "llm_evidence.jsonl")
    envelope = _envelope()

    with pytest.raises(
        TypeError,
        match="expected_head_sha256",
    ):
        journal.append(envelope)  # type: ignore[call-arg]
    with pytest.raises(LLMEvidenceJournalError, match="journal_head_cas_mismatch"):
        journal.append(envelope, expected_head_sha256="f" * 64)
    assert (
        journal.append(
            envelope,
            expected_head_sha256=EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
        )
        is True
    )

    replay = replace(envelope, run_id="llm-run-caller-selected")
    with pytest.raises(LLMEvidenceJournalError, match="journal_envelope_invalid"):
        journal.append(
            replay,
            expected_head_sha256=journal.read().head_sha256,
        )


def test_journal_head_anchor_detects_deleted_or_replaced_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "llm_evidence.jsonl"
    journal = LLMEvidenceJournal(path)
    envelope = _envelope()
    assert (
        journal.append(
            envelope,
            expected_head_sha256=EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
        )
        is True
    )
    assert journal.head_path.exists()

    path.unlink()
    with pytest.raises(
        LLMEvidenceJournalError,
        match="journal_missing_with_head_anchor",
    ):
        journal.read()
