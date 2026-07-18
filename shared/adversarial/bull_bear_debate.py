#!/usr/bin/env python3
"""Evidence-only bull/bear review.

The former implementation returned an LLM ``belief_score`` and let that
value influence portfolio sizing.  This module now emits only a versioned
``LLMEvidenceObservation``.  It cannot approve a decision, alter risk, create
a TradeIntent, size a position, or enable real trading.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Iterable

from shared.llm.gateway import LLMEvidenceGateway
from shared.llm.evidence_artifact import (
    EvidenceArtifact,
    EvidenceArtifactError,
    EvidenceSourceAuthorityVerifier,
)
from shared.llm.schema import (
    LLMEvidenceRequest,
    SensitivePayloadError,
    available_observation,
    invalid_observation,
    unavailable_observation,
)

_DIMENSIONS = ["macro", "event", "fundamental", "capital", "technical", "sentiment"]
_PROMPT_TEMPLATE_ID = "ashare-bull-bear-evidence"
_PROMPT_VERSION = "bull-bear-evidence.v2"


def _call_deepseek(*_: Any, **__: Any) -> dict[str, Any]:
    """Retired compatibility seam; direct provider/network calls are forbidden."""

    raise RuntimeError("direct DeepSeek calls are retired; use shared.llm gateway")


def _safe_unit_score(value: Any, default: float = 0.5) -> float:
    if isinstance(value, dict):
        value = value.get("score", value.get("value"))
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:
        return default
    return max(0.0, min(1.0, result))


def _fast_evidence(
    scores: dict[str, Any],
    *,
    evidence_refs: Iterable[str],
) -> dict[str, Any]:
    positives = [dim for dim in _DIMENSIONS if _safe_unit_score(scores.get(dim)) >= 0.6]
    risks = [dim for dim in _DIMENSIONS if _safe_unit_score(scores.get(dim)) <= 0.4]
    bull = "正向维度: " + (", ".join(positives) if positives else "暂无明显优势")
    bear = "风险维度: " + (", ".join(risks) if risks else "暂无明显短板")
    return {
        "bull_case": bull,
        "bear_case": bear,
        "key_risk": bear,
        "contradictions": [],
        "material_facts": [],
        "evidence_refs": list(evidence_refs),
        "confidence_note": "deterministic dimension summary; not a calibrated probability",
    }


def _request(
    ts_code: str,
    scores: dict[str, Any],
    *,
    route: str,
    artifacts: tuple[EvidenceArtifact, ...],
) -> LLMEvidenceRequest:
    recorded_times = [artifact.available_at for artifact in artifacts]
    recorded_times.extend(
        artifact.source_authority_receipt.issued_at
        for artifact in artifacts
        if artifact.source_authority_receipt is not None
    )
    cutoff = str(
        scores.get("document_cutoff")
        or scores.get("available_time")
        or max(recorded_times)
    )
    payload_scores = {
        key: value
        for key, value in scores.items()
        if key not in {"evidence_refs", "document_cutoff", "available_time"}
    }
    return LLMEvidenceRequest.create(
        request_id=f"LLM-{uuid.uuid4().hex[:16]}",
        task_type="adversarial_review",
        route=route,
        prompt_template_id=_PROMPT_TEMPLATE_ID,
        prompt_version=_PROMPT_VERSION,
        document_cutoff=cutoff,
        evidence_refs=tuple(artifact.artifact_id for artifact in artifacts),
        artifacts=artifacts,
        payload={"symbol": ts_code, "research_scores": payload_scores},
    )


def _debate_mode() -> str:
    # Network-free deterministic evidence is the safe default.
    return os.environ.get("TRADINGS_DEBATE_MODE", "fast").strip().lower()


def debate(
    ts_code: str,
    scores: dict[str, Any],
    *,
    gateway: LLMEvidenceGateway | None = None,
    artifacts: Iterable[EvidenceArtifact] = (),
    source_authority_verifier: EvidenceSourceAuthorityVerifier | Any | None = None,
) -> dict[str, Any]:
    """Return research evidence without any trading authority."""

    if not ts_code:
        raise ValueError("ts_code is required")
    if not isinstance(scores, dict):
        scores = {}
    artifact_rows = tuple(artifacts)
    if not artifact_rows:
        return unavailable_observation(
            None,
            reason_code="verified_evidence_artifact_required",
            entity_id=ts_code,
        )
    if any(not isinstance(artifact, EvidenceArtifact) for artifact in artifact_rows):
        return invalid_observation(
            None,
            reason_code="invalid_evidence_artifact",
            entity_id=ts_code,
        )
    mode = _debate_mode()
    route = "slow_research"
    try:
        request = _request(
            ts_code,
            scores,
            route=route,
            artifacts=artifact_rows,
        )
    except (EvidenceArtifactError, SensitivePayloadError, ValueError):
        return invalid_observation(
            None,
            reason_code="invalid_evidence_artifact_or_payload",
            entity_id=ts_code,
        )

    if mode in {"fast", "heuristic", "deterministic", "off", "disabled"}:
        try:
            for artifact in artifact_rows:
                artifact.verify_source_authority(
                    source_authority_verifier,
                    document_cutoff=request.document_cutoff,
                )
        except EvidenceArtifactError:
            return invalid_observation(
                request,
                reason_code="external_source_authority_invalid",
                entity_id=ts_code,
            )
        return available_observation(
            request,
            provider="deterministic",
            model="evidence-summary-v1",
            raw_evidence=_fast_evidence(
                scores,
                evidence_refs=request.evidence_refs,
            ),
            entity_id=ts_code,
        )

    sidecar = gateway or LLMEvidenceGateway()
    return sidecar.analyze(request, entity_id=ts_code)


if __name__ == "__main__":
    import json

    sample = {
        "macro": {"score": 0.7, "note": "growth"},
        "event": {"score": 0.3, "note": "event uncertainty"},
        "combined": 0.68,
    }
    print(json.dumps(debate("600519.SH", sample), ensure_ascii=False, indent=2))
