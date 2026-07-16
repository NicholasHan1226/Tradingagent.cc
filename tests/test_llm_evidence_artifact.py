from __future__ import annotations

from dataclasses import replace
import importlib
from typing import Any

import pytest

from shared.llm.schema import LLMEvidenceRequest


DOCUMENT = "公司公告披露新增合同已签署，交付仍取决于客户验收。"
SPAN = "新增合同已签署"
CUTOFF = "2026-07-16T08:30:00+08:00"


def _artifact_module() -> Any:
    try:
        return importlib.import_module("shared.llm.evidence_artifact")
    except ModuleNotFoundError:
        pytest.fail("content-addressed LLM evidence artifact is missing")


def _artifact() -> Any:
    artifact_module = _artifact_module()
    start = DOCUMENT.index(SPAN)
    return artifact_module.EvidenceArtifact.create(
        document_text=DOCUMENT,
        published_at="2026-07-16T08:00:00+08:00",
        available_at="2026-07-16T08:05:00+08:00",
        span_start=start,
        span_end=start + len(SPAN),
        entity_resolution_version="ashare-entity-resolution.v1",
    )


def _request(artifact: Any = None) -> LLMEvidenceRequest:
    artifact = artifact or _artifact()
    return LLMEvidenceRequest.create(
        request_id="REQ-ARTIFACT-001",
        task_type="event_evidence_extraction",
        route="bulk_extraction",
        prompt_template_id="general-evidence-review",
        prompt_version="bull-bear.v1",
        document_cutoff=CUTOFF,
        evidence_refs=(artifact.artifact_id,),
        artifacts=(artifact,),
        payload={"symbol": "600000.SH", "event_type": "contract"},
    )


def test_evidence_artifact_is_content_addressed_and_verifiable() -> None:
    artifact = _artifact()

    artifact.verify(document_cutoff=CUTOFF)
    assert artifact.document_sha256
    assert artifact.source_span == SPAN
    assert artifact.span_sha256
    assert artifact.artifact_id == f"evidence:{artifact.artifact_sha256}"
    assert artifact.verification_status == "integrity_verified"
    assert artifact.source_authority_receipt is None


def test_external_source_authority_is_distinct_from_content_integrity() -> None:
    artifact_module = _artifact_module()
    start = DOCUMENT.index(SPAN)
    receipt = artifact_module.EvidenceSourceAuthorityReceipt.create(
        receipt_id="source-receipt-001",
        source_system="official-disclosure-fixture",
        source_document_id="doc-20260716-001",
        document_sha256=artifact_module.sha256_document(DOCUMENT),
        available_at="2026-07-16T08:05:00+08:00",
        issued_at="2026-07-16T08:06:00+08:00",
    )
    artifact = artifact_module.EvidenceArtifact.create(
        document_text=DOCUMENT,
        published_at="2026-07-16T08:00:00+08:00",
        available_at="2026-07-16T08:05:00+08:00",
        span_start=start,
        span_end=start + len(SPAN),
        entity_resolution_version="ashare-entity-resolution.v1",
        source_authority_receipt=receipt,
    )

    class _Verifier:
        def verify(self, *, artifact: Any, receipt: Any) -> bool:
            return (
                receipt.receipt_id == "source-receipt-001"
                and receipt.document_sha256 == artifact.document_sha256
            )

    artifact.verify(document_cutoff=CUTOFF)
    artifact.verify_source_authority(
        _Verifier(),
        document_cutoff=CUTOFF,
        verified_at="2026-07-16T08:06:30+08:00",
    )
    with pytest.raises(
        artifact_module.EvidenceArtifactError,
        match="external_source_authority_rejected",
    ):
        artifact.verify_source_authority(
            lambda **_: False,
            document_cutoff=CUTOFF,
        )


def test_external_source_receipt_must_already_exist_at_document_cutoff() -> None:
    artifact_module = _artifact_module()
    start = DOCUMENT.index(SPAN)
    receipt = artifact_module.EvidenceSourceAuthorityReceipt.create(
        receipt_id="source-receipt-future",
        source_system="official-disclosure-fixture",
        source_document_id="doc-20260716-future",
        document_sha256=artifact_module.sha256_document(DOCUMENT),
        available_at="2026-07-16T08:05:00+08:00",
        issued_at="2026-07-16T09:00:00+08:00",
    )
    artifact = artifact_module.EvidenceArtifact.create(
        document_text=DOCUMENT,
        published_at="2026-07-16T08:00:00+08:00",
        available_at="2026-07-16T08:05:00+08:00",
        span_start=start,
        span_end=start + len(SPAN),
        entity_resolution_version="ashare-entity-resolution.v1",
        source_authority_receipt=receipt,
    )

    with pytest.raises(
        artifact_module.EvidenceArtifactError,
        match="external_source_receipt_after_document_cutoff",
    ):
        artifact.verify_source_authority(
            lambda **_: True,
            document_cutoff=CUTOFF,
        )


def test_external_source_authority_has_no_self_certifying_default() -> None:
    artifact_module = _artifact_module()

    with pytest.raises(
        artifact_module.EvidenceArtifactError,
        match="external_source_authority_receipt_required",
    ):
        _artifact().verify_source_authority(
            None,
            document_cutoff=CUTOFF,
        )


@pytest.mark.parametrize(
    "tamper",
    [
        lambda row: replace(row, document_text=row.document_text + "篡改"),
        lambda row: replace(row, source_span=row.source_span + "篡改"),
        lambda row: replace(row, span_sha256="0" * 64),
        lambda row: replace(row, published_at="2026-07-16T08:01:00+08:00"),
        lambda row: replace(row, available_at="2026-07-16T08:06:00+08:00"),
        lambda row: replace(row, entity_resolution_version="forged.v2"),
        lambda row: replace(row, verification_status="unverified"),
    ],
)
def test_evidence_artifact_tampering_is_rejected(tamper: object) -> None:
    artifact_module = _artifact_module()
    artifact = tamper(_artifact())  # type: ignore[operator]

    with pytest.raises(artifact_module.EvidenceArtifactError):
        artifact.verify(document_cutoff=CUTOFF)


def test_artifact_time_order_and_cutoff_fail_closed() -> None:
    artifact_module = _artifact_module()
    start = DOCUMENT.index(SPAN)
    with pytest.raises(
        artifact_module.EvidenceArtifactError,
        match="artifact_time_order_invalid",
    ):
        artifact_module.EvidenceArtifact.create(
            document_text=DOCUMENT,
            published_at="2026-07-16T08:10:00+08:00",
            available_at="2026-07-16T08:05:00+08:00",
            span_start=start,
            span_end=start + len(SPAN),
            entity_resolution_version="ashare-entity-resolution.v1",
        )

    with pytest.raises(
        artifact_module.EvidenceArtifactError,
        match="artifact_after_document_cutoff",
    ):
        _artifact().verify(document_cutoff="2026-07-16T08:04:59+08:00")


def test_request_sha_binds_model_template_payload_and_artifact_set() -> None:
    request = _request()

    baseline = request.request_sha256("deepseek-chat-fixture")
    assert baseline != request.request_sha256("deepseek-reasoner-fixture")
    assert request.prompt_template_id == "general-evidence-review"
    assert request.artifact_set_sha256
    assert request.request_content_sha256

    payload_tampered = replace(
        request,
        payload={"symbol": "600001.SH", "event_type": "contract"},
    )
    with pytest.raises(ValueError, match="request_payload_sha256_mismatch"):
        payload_tampered.request_sha256("deepseek-chat-fixture")

    reference_tampered = replace(request, evidence_refs=("evidence:forged",))
    with pytest.raises(ValueError):
        reference_tampered.request_sha256("deepseek-chat-fixture")

    cutoff_tampered = replace(
        request,
        document_cutoff="2026-07-16T08:10:00+08:00",
    )
    with pytest.raises(ValueError, match="request_content_sha256_mismatch"):
        cutoff_tampered.request_sha256("deepseek-chat-fixture")


def test_unverified_or_unbound_artifacts_cannot_enter_a_cloud_request() -> None:
    artifact_module = _artifact_module()
    artifact = _artifact()
    unverified = replace(artifact, verification_status="unverified")
    with pytest.raises(artifact_module.EvidenceArtifactError):
        _request(unverified)

    with pytest.raises(ValueError, match="evidence_refs_artifacts_mismatch"):
        LLMEvidenceRequest.create(
            request_id="REQ-REF-MISMATCH",
            task_type="event_evidence_extraction",
            route="bulk_extraction",
            prompt_template_id="general-evidence-review",
            prompt_version="bull-bear.v1",
            document_cutoff=CUTOFF,
            evidence_refs=("evidence:forged",),
            artifacts=(artifact,),
            payload={"symbol": "600000.SH"},
        )
