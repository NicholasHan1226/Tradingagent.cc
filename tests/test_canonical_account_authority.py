from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from shared.portfolio.small_account_optimizer import optimize_small_account
from shared.runtime.canonical_account_authority import (
    CanonicalAccountAuthorityError,
    build_canonical_account_authority,
)
from shared.runtime.capital_stages import (
    CapitalBackedPreopenStagePort,
    PaperCapitalAccount,
)
from shared.runtime.run_bundle import RunStage
from tests._thesis_risk_fixture import build_thesis_risk_fixture
from tests.test_capital_backed_paper_stages import (
    DECISION_AS_OF,
    TRADE_DATE,
    _bundle,
    _execute_buy,
    _init_ledger,
    _mark,
    _request,
    _StaticPort,
)


def _preopen_account(tmp_path: Path) -> PaperCapitalAccount:
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
    ).execute(_request(stage=RunStage.PREOPEN, bundle=_bundle()))
    return account


def test_canonical_optimizer_snapshot_is_bound_to_market_capital_head(
    tmp_path: Path,
) -> None:
    account = _preopen_account(tmp_path)
    decision_time = datetime.fromisoformat(DECISION_AS_OF)

    snapshot, verifier = build_canonical_account_authority(
        account=account,
        decision_time=decision_time,
        trade_date=TRADE_DATE,
        mark_observed_at={},
    )

    assert snapshot.capital_authority_id == "ashare-capital-v1"
    assert snapshot.authority_source_class == "canonical_authority"
    assert snapshot.available_cash_cny == 50_000.0
    assert snapshot.current_gross_cny == 0.0
    assert snapshot.positions == ()
    assert account.ledger.snapshot().event_checksum in (
        snapshot.position_snapshot_receipt_id
    )
    plan = optimize_small_account(
        candidates=(),
        account_snapshot=snapshot,
        decision_time=decision_time,
        account_authority_verifier=verifier,
        **build_thesis_risk_fixture(
            candidates=(),
            account_snapshot=snapshot,
            decision_time=decision_time,
        ),
    )
    assert plan.capital_authority_id == "ashare-capital-v1"
    assert plan.target_gross_cny == 0.0


def test_canonical_optimizer_verifier_fails_after_ledger_head_changes(
    tmp_path: Path,
) -> None:
    account = _preopen_account(tmp_path)
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    snapshot, verifier = build_canonical_account_authority(
        account=account,
        decision_time=decision_time,
        trade_date=TRADE_DATE,
        mark_observed_at={},
    )
    thesis_risk_fixture = build_thesis_risk_fixture(
        candidates=(),
        account_snapshot=snapshot,
        decision_time=decision_time,
    )

    changed_bundle = _bundle(run_id="changed-capital-head")
    account.reconcile(
        request=_request(stage=RunStage.PREOPEN, bundle=changed_bundle),
        phase="preopen-refresh",
        pit_timestamp=DECISION_AS_OF,
    )

    with pytest.raises(ValueError, match="account_authority_verification_failed"):
        optimize_small_account(
            candidates=(),
            account_snapshot=snapshot,
            decision_time=decision_time,
            account_authority_verifier=verifier,
            **thesis_risk_fixture,
        )


def test_canonical_optimizer_snapshot_rejects_trade_date_or_mark_time_guessing(
    tmp_path: Path,
) -> None:
    account = _preopen_account(tmp_path)
    decision_time = datetime.fromisoformat(DECISION_AS_OF)

    with pytest.raises(
        CanonicalAccountAuthorityError,
        match="canonical_account_trade_date_mismatch",
    ):
        build_canonical_account_authority(
            account=account,
            decision_time=decision_time,
            trade_date="2026-07-17",
            mark_observed_at={},
        )

    with pytest.raises(
        CanonicalAccountAuthorityError,
        match="canonical_account_unknown_mark_observation",
    ):
        build_canonical_account_authority(
            account=account,
            decision_time=decision_time,
            trade_date=TRADE_DATE,
            mark_observed_at={"000001.SZ": decision_time},
        )


def test_canonical_account_rejects_caller_mark_time_not_bound_to_account_evidence(
    tmp_path: Path,
) -> None:
    account = PaperCapitalAccount(
        ledger=_init_ledger(tmp_path),
        artifact_root=tmp_path / "bound-mark-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _execute_buy(
        account=account,
        order_id="ORDER-BOUND-MARK",
        run_id="RUN-BOUND-MARK",
        trade_date=TRADE_DATE,
        decision_as_of=DECISION_AS_OF,
        execution_time="2026-07-16T09:31:00+08:00",
    )
    reconcile_time = "2026-07-16T09:32:00+08:00"
    reconcile_bundle = _bundle(
        run_id="RUN-BOUND-MARK-RECONCILE",
        decision_as_of=reconcile_time,
    )
    account.reconcile(
        request=_request(stage=RunStage.PREOPEN, bundle=reconcile_bundle),
        phase="intraday-refresh",
        pit_timestamp=reconcile_time,
    )
    decision_time = datetime.fromisoformat("2026-07-16T09:33:00+08:00")

    with pytest.raises(
        CanonicalAccountAuthorityError,
        match="canonical_account_mark_observation_evidence_mismatch",
    ):
        build_canonical_account_authority(
            account=account,
            decision_time=decision_time,
            trade_date=TRADE_DATE,
            mark_observed_at={"000001.SZ": decision_time},
        )
