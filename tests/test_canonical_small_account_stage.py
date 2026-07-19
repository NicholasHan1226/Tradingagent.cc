from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from shared.portfolio.small_account_optimizer import CandidateAllocationInput
from shared.portfolio.champion import FrozenChampionSpec
from shared.runtime.canonical_small_account_stage import (
    CanonicalSmallAccountDecisionStagePort,
    CanonicalSmallAccountStageError,
)
from shared.runtime.canonical_account_authority import (
    build_canonical_account_authority,
)
from shared.runtime.capital_stages import (
    CapitalBackedPreopenStagePort,
    PaperCapitalAccount,
)
from shared.runtime.day_loop import StageRequest
from shared.runtime.run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunContext,
    RunStage,
    STAGE_ORDER,
    StageReceipt,
)
from tests.test_capital_backed_paper_stages import (
    DECISION_AS_OF,
    LINEAGE,
    TRADE_DATE,
    _bundle as capital_bundle,
    _init_ledger,
    _request as capital_request,
    _StaticPort,
)
from tests._champion_authority_fixture import (
    FrozenChampionSelectionVerifier,
    FrozenNumericPITFeatureSnapshotVerifier,
    build_champion_authority_fixture,
)
from tests._thesis_risk_fixture import build_thesis_risk_fixture


def _account(tmp_path: Path) -> PaperCapitalAccount:
    account = PaperCapitalAccount(
        ledger=_init_ledger(tmp_path),
        artifact_root=tmp_path / "paper-capital-artifacts",
        mark_prices={},
    )
    CapitalBackedPreopenStagePort(
        base_port=_StaticPort(
            RunStage.PREOPEN,
            {
                "market": "ashare",
                "account_type": "simulated",
                "real_trading_enabled": False,
                "account_authority_valid": True,
                "position_authority_valid": True,
            },
        ),
        account=account,
    ).execute(
        capital_request(
            stage=RunStage.PREOPEN,
            bundle=capital_bundle(),
        )
    )
    return account


def _champion() -> FrozenChampionSpec:
    return FrozenChampionSpec(
        champion_id="canonical-stage-test",
        version="1",
        feature_names=(
            "quality_score",
            "value_score",
            "momentum_score",
            "low_volatility_score",
        ),
        feature_weights=(0.25, 0.25, 0.25, 0.25),
        decision_horizon="5d",
        trained_through="2026-06-30",
    )


def _candidate(*, score: float) -> CandidateAllocationInput:
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    authority = build_champion_authority_fixture(
        champion=_champion(),
        symbol="000001.SZ",
        decision_time=decision_time,
        feature_values={
            "quality_score": score,
            "value_score": score,
            "momentum_score": score,
            "low_volatility_score": score,
        },
        source_id="canonical-stage-data-1",
    )
    return CandidateAllocationInput(
        symbol="000001.SZ",
        score_evidence=authority.score_receipt,
        decision_time=decision_time,
        price_observed_at=datetime.fromisoformat("2026-07-16T09:29:00+08:00"),
        decision_reference_price=10.0,
    )


def _authority_kwargs(candidate: CandidateAllocationInput) -> dict[str, object]:
    context = candidate.score_evidence.selection_context
    return {
        "current_champion_selection_context": context,
        "champion_selection_verifier": FrozenChampionSelectionVerifier(context),
        "numeric_feature_snapshot_verifier": (
            FrozenNumericPITFeatureSnapshotVerifier(
                candidate.score_evidence.feature_snapshot
            )
        ),
    }


def _risk_kwargs(
    account: PaperCapitalAccount,
    candidates: tuple[CandidateAllocationInput, ...],
) -> dict[str, object]:
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    snapshot, _ = build_canonical_account_authority(
        account=account,
        decision_time=decision_time,
        trade_date=TRADE_DATE,
        mark_observed_at={},
    )
    return build_thesis_risk_fixture(
        candidates=candidates,
        account_snapshot=snapshot,
        decision_time=decision_time,
    )


def _decision_bundle(port: CanonicalSmallAccountDecisionStagePort) -> RunBundle:
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    context = RunContext(
        trade_date=TRADE_DATE,
        decision_as_of=decision_time,
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage=LINEAGE,
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256="c" * 64,
    )
    components = tuple(
        port.identity
        if stage is RunStage.DECISION_READY
        else ComponentIdentity(
            stage=stage,
            component_id=f"canonical-stage-{stage.value}",
            version="1",
            artifact_sha256=f"{index + 1:x}" * 64,
        )
        for index, stage in enumerate(STAGE_ORDER)
    )
    bundle = RunBundle(context=context, components=components)
    payloads = {
        RunStage.PREOPEN: {
            "market": "ashare",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "account_authority_valid": True,
            "position_authority_valid": True,
        },
        RunStage.EVIDENCE_READY: {"decision_as_of": DECISION_AS_OF},
        RunStage.UNIVERSE_READY: {
            "tradable_symbols": ["000001.SZ"],
            "feasible_symbols": ["000001.SZ"],
        },
    }
    for index, stage in enumerate(
        (RunStage.PREOPEN, RunStage.EVIDENCE_READY, RunStage.UNIVERSE_READY)
    ):
        receipt = StageReceipt.create(
            stage=stage,
            status="completed",
            idempotency_key=f"{index + 1:x}" * 64,
            component=bundle.component_for(stage),
            input_bundle_sha256=bundle.bundle_sha256,
            payload=payloads[stage],
            reason_codes=(),
        )
        bundle = bundle.append(
            receipt,
            stop_new_risk=False,
            position_authority_valid=True if stage is RunStage.PREOPEN else None,
            block_reasons=(),
            permitted_order_ids=None,
        )
    return bundle


def test_canonical_decision_stage_builds_optimizer_input_at_execution_time(
    tmp_path: Path,
) -> None:
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    candidate = _candidate(score=0.28)
    account = _account(tmp_path)
    port = CanonicalSmallAccountDecisionStagePort(
        account=account,
        candidates=(candidate,),
        reduction_intents=(),
        decision_time=decision_time,
        trade_date=TRADE_DATE,
        mark_observed_at={},
        **_risk_kwargs(account, (candidate,)),
        **_authority_kwargs(candidate),
    )
    bundle = _decision_bundle(port)
    request = StageRequest(
        run_id=bundle.run_id,
        stage=RunStage.DECISION_READY,
        idempotency_key="d" * 64,
        input_bundle_sha256=bundle.bundle_sha256,
        bundle=bundle,
        allowed_actions=("open", "increase", "reduce", "exit", "hold"),
        permitted_order_ids=(),
    )

    payload = port.execute(request).payload

    assert port.account_authority_source_class == "canonical_authority"
    assert port.runtime_environment == "canonical_simulated"
    assert port.promotion_eligible is False
    assert payload["small_account_plan"]["capital_authority_id"] == (
        "ashare-capital-v1"
    )
    assert payload["small_account_plan"]["starting_available_cash_cny"] == (50_000.0)
    assert payload["decisions"][0]["action"] == "open"


def test_canonical_decision_stage_identity_binds_candidates_and_marks(
    tmp_path: Path,
) -> None:
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    account = _account(tmp_path)
    first_candidate = _candidate(score=0.28)
    second_candidate = _candidate(score=0.29)
    first = CanonicalSmallAccountDecisionStagePort(
        account=account,
        candidates=(first_candidate,),
        decision_time=decision_time,
        trade_date=TRADE_DATE,
        mark_observed_at={},
        **_risk_kwargs(account, (first_candidate,)),
        **_authority_kwargs(first_candidate),
    )
    second = CanonicalSmallAccountDecisionStagePort(
        account=account,
        candidates=(second_candidate,),
        decision_time=decision_time,
        trade_date=TRADE_DATE,
        mark_observed_at={},
        **_risk_kwargs(account, (second_candidate,)),
        **_authority_kwargs(second_candidate),
    )

    assert first.identity.artifact_sha256 != second.identity.artifact_sha256


def test_canonical_decision_stage_requires_external_champion_authority(
    tmp_path: Path,
) -> None:
    account = _account(tmp_path)
    candidate = _candidate(score=0.28)
    with pytest.raises(
        CanonicalSmallAccountStageError,
        match="current_champion_selection_authority_required",
    ):
        CanonicalSmallAccountDecisionStagePort(
            account=account,
            candidates=(candidate,),
            decision_time=datetime.fromisoformat(DECISION_AS_OF),
            trade_date=TRADE_DATE,
            mark_observed_at={},
            **_risk_kwargs(account, (candidate,)),
        )
