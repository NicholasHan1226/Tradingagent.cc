from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from shared.capital.market_ledger import MarketCapitalLedger, OpeningStateManifest
from shared.capital.market_policy import (
    PINNED_CUTOVER_DECISION_ID,
    PINNED_SOURCE_THREAD_ID,
    MarketPolicy,
)
from shared.models.lifecycle import (
    TradingSessionCalendarAuthority,
    TradingSessionCalendarAuthorityVerification,
)
from shared.runtime.capital_stages import (
    CapitalBackedPreopenStagePort,
    CapitalBackedReconcileStagePort,
    CapitalBackedRiskStagePort,
    CapitalBackedSimulationExecutionStagePort,
    CapitalEffectAuthorization,
    CapitalEffectGuard,
    PaperCapitalAccount,
    PaperCapitalStageError,
)
from shared.runtime.day_loop import StageRequest, StageResult
from shared.runtime.execution_receipt_contract import (
    is_reconcilable_not_committed_market_failure,
)
from shared.runtime.run_bundle import ComponentIdentity, RunStage
from shared.runtime.trusted_clock import (
    NonProductionFixtureExecutionClock,
    TrustedExecutionClockError,
)
from tests._ashare_validation_plan_fixture import (
    build_non_production_ashare_validation_plan,
)
from tests._market_evidence_fixture import (
    attach_mark_authority,
    attach_quote_authority,
)


TRADE_DATE = "2026-07-16"
DECISION_AS_OF = "2026-07-16T09:30:00+08:00"
LINEAGE = "paper-capital-safety-lineage-20260716"


def _uncommitted_market_failure_receipt(
    *,
    intent: str = "open",
    reason: str = "paper_market_snapshot_stale_before_capital_commit",
) -> dict[str, Any]:
    is_buy = intent in {"open", "increase"}
    return {
        "status": "not_filled",
        "intent": intent,
        "requested_quantity": 100,
        "filled_quantity": 0,
        "residual_quantity": 100,
        "capital_commit_status": "not_committed",
        "capital_commit_receipt_id": None,
        "capital_release_status": "released" if is_buy else "not_applicable",
        "capital_release_receipt_id": ("MCAP-" + "1" * 32) if is_buy else None,
        "simulated_fill_id": None,
        "filled_at": None,
        "fill_fingerprint": None,
        "execution_reason": reason,
        "market_session": "continuous_auction_am",
        "market_execution_time": "2026-07-16T09:31:00+08:00",
        "market_available_at": "2026-07-16T09:31:00+08:00",
        "market_data_through": "2026-07-16T09:31:00+08:00",
        "sim_submit_checked_at": "2026-07-16T09:31:10+08:00",
        "capital_commit_checked_at": "2026-07-16T09:32:00+08:00",
        "terminal_at": "2026-07-16T09:32:00+08:00",
    }


def test_uncommitted_market_failure_requires_reason_specific_evidence() -> None:
    stale = _uncommitted_market_failure_receipt()
    assert is_reconcilable_not_committed_market_failure(
        stale,
        expected_trade_date=TRADE_DATE,
    )

    stale_within_ttl = {
        **stale,
        "capital_commit_checked_at": "2026-07-16T09:31:20+08:00",
        "terminal_at": "2026-07-16T09:31:20+08:00",
    }
    assert not is_reconcilable_not_committed_market_failure(
        stale_within_ttl,
        expected_trade_date=TRADE_DATE,
    )

    stale_before_submit = {
        **stale,
        "market_execution_time": "2026-07-16T09:30:00+08:00",
        "market_available_at": "2026-07-16T09:30:00+08:00",
        "market_data_through": "2026-07-16T09:30:00+08:00",
        "sim_submit_checked_at": "2026-07-16T09:31:10+08:00",
    }
    assert not is_reconcilable_not_committed_market_failure(
        stale_before_submit,
        expected_trade_date=TRADE_DATE,
    )

    inverted_market_times = {
        **stale,
        "market_available_at": "2026-07-16T09:30:59+08:00",
        "market_data_through": "2026-07-16T09:31:00+08:00",
    }
    assert not is_reconcilable_not_committed_market_failure(
        inverted_market_times,
        expected_trade_date=TRADE_DATE,
    )

    invalid_execution_session = {
        **stale,
        "market_execution_time": "2026-07-16T09:00:00+08:00",
        "market_available_at": "2026-07-16T09:00:00+08:00",
        "market_data_through": "2026-07-16T09:00:00+08:00",
    }
    assert not is_reconcilable_not_committed_market_failure(
        invalid_execution_session,
        expected_trade_date=TRADE_DATE,
    )

    session_mismatch = {
        **_uncommitted_market_failure_receipt(
            reason="paper_market_clock_session_mismatch_before_capital_commit"
        ),
        "market_execution_time": "2026-07-16T11:29:40+08:00",
        "market_available_at": "2026-07-16T11:29:40+08:00",
        "market_data_through": "2026-07-16T11:29:40+08:00",
        "sim_submit_checked_at": "2026-07-16T11:30:00+08:00",
        "capital_commit_checked_at": "2026-07-16T13:00:00+08:00",
        "terminal_at": "2026-07-16T13:00:00+08:00",
    }
    assert is_reconcilable_not_committed_market_failure(
        session_mismatch,
        expected_trade_date=TRADE_DATE,
    )

    forged_same_session = {
        **session_mismatch,
        "market_execution_time": "2026-07-16T11:29:20+08:00",
        "market_available_at": "2026-07-16T11:29:20+08:00",
        "market_data_through": "2026-07-16T11:29:20+08:00",
        "sim_submit_checked_at": "2026-07-16T11:29:40+08:00",
        "capital_commit_checked_at": "2026-07-16T11:29:50+08:00",
        "terminal_at": "2026-07-16T11:29:50+08:00",
    }
    assert not is_reconcilable_not_committed_market_failure(
        forged_same_session,
        expected_trade_date=TRADE_DATE,
    )

    impossible_future = {
        **stale,
        "execution_reason": "paper_market_snapshot_future_before_capital_commit",
    }
    assert not is_reconcilable_not_committed_market_failure(
        impossible_future,
        expected_trade_date=TRADE_DATE,
    )


def test_uncommitted_market_failure_enforces_priority_terminal_and_release() -> None:
    stale = _uncommitted_market_failure_receipt()
    assert not is_reconcilable_not_committed_market_failure(
        {**stale, "capital_release_receipt_id": "FORGED-RELEASE"},
        expected_trade_date=TRADE_DATE,
    )
    assert not is_reconcilable_not_committed_market_failure(
        {**stale, "terminal_at": "2026-07-16T09:32:01+08:00"},
        expected_trade_date=TRADE_DATE,
    )

    forged_trade_date_priority = {
        **stale,
        "execution_reason": (
            "paper_market_clock_trade_date_mismatch_before_capital_commit"
        ),
        "capital_commit_checked_at": "2026-07-15T09:30:00+08:00",
        "terminal_at": stale["sim_submit_checked_at"],
    }
    assert not is_reconcilable_not_committed_market_failure(
        forged_trade_date_priority,
        expected_trade_date=TRADE_DATE,
    )

    reduce_receipt = _uncommitted_market_failure_receipt(intent="reduce")
    assert is_reconcilable_not_committed_market_failure(
        reduce_receipt,
        expected_trade_date=TRADE_DATE,
    )
    assert not is_reconcilable_not_committed_market_failure(
        {**reduce_receipt, "capital_release_receipt_id": "FORGED-RELEASE"},
        expected_trade_date=TRADE_DATE,
    )


def _clock(
    instant: str = "2026-07-16T09:31:00+08:00",
    *,
    effect_overrides: Mapping[str, str] | None = None,
) -> NonProductionFixtureExecutionClock:
    return NonProductionFixtureExecutionClock.from_isoformat(
        default_instant=instant,
        effect_overrides=effect_overrides or {},
    )


def test_fixture_execution_clock_is_explicit_frozen_and_effect_scoped() -> None:
    clock = _clock(
        effect_overrides={
            "capital_commit:ORDER-1": "2026-07-16T09:31:20+08:00",
        }
    )

    assert clock.now(effect="sim_submit", order_id="ORDER-1").isoformat() == (
        "2026-07-16T09:31:00+08:00"
    )
    assert clock.now(effect="capital_commit", order_id="ORDER-1").isoformat() == (
        "2026-07-16T09:31:20+08:00"
    )
    assert clock.production_eligible is False
    with pytest.raises(AttributeError):
        clock._default_instant = clock.now(  # type: ignore[misc]
            effect="sim_submit",
            order_id="ORDER-1",
        )


def test_fixture_execution_clock_rejects_naive_or_unknown_effect() -> None:
    with pytest.raises(TrustedExecutionClockError, match="timezone_required"):
        NonProductionFixtureExecutionClock.from_isoformat(
            default_instant="2026-07-16T09:31:00",
            effect_overrides={},
        )
    with pytest.raises(TrustedExecutionClockError, match="effect_invalid"):
        _clock().now(effect="unknown", order_id="ORDER-1")


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _non_production_session_calendar_receipt() -> dict[str, Any]:
    plan = build_non_production_ashare_validation_plan()
    calendar = plan.trading_session_calendar
    verification = plan.trading_session_calendar_verification
    assert calendar is not None
    assert verification is not None
    return {
        "authority_tier": "non_production_fixture",
        "production_eligible": False,
        "calendar": calendar.canonical_payload(),
        "verification": verification.canonical_payload(),
    }


def _single_session_calendar_receipt() -> dict[str, Any]:
    from datetime import date, datetime, timezone

    calendar = TradingSessionCalendarAuthority(
        market="ashare",
        calendar_id="single-session-boundary-fixture",
        calendar_version="1",
        source_dataset_id="fixture.ashare.trade_calendar",
        source_receipt_id="single-session-receipt",
        source_receipt_sha256="e" * 64,
        available_at=datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc),
        sessions=(date(2026, 7, 16),),
    )
    verification = TradingSessionCalendarAuthorityVerification(
        accepted=True,
        verifier_id="single-session-fixture-verifier",
        verifier_version="1",
        proof_sha256="d" * 64,
        verified_at=datetime(2026, 7, 16, 8, 1, tzinfo=timezone.utc),
        frozen_at=datetime(2026, 7, 16, 8, 1, tzinfo=timezone.utc),
        calendar_sha256=calendar.calendar_sha256,
        source_receipt_id=calendar.source_receipt_id,
        source_receipt_sha256=calendar.source_receipt_sha256,
    )
    return {
        "authority_tier": "non_production_fixture",
        "production_eligible": False,
        "calendar": calendar.canonical_payload(),
        "verification": verification.canonical_payload(),
    }


def _init_ledger(tmp_path: Path) -> MarketCapitalLedger:
    legacy_archive = tmp_path / "legacy-archive"
    legacy_archive.mkdir(parents=True)
    legacy_events = tmp_path / "legacy-events.jsonl"
    legacy_events.write_text(
        json.dumps({"event_id": "LEGACY-1"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    policy = MarketPolicy.load("ashare")
    ledger = MarketCapitalLedger(tmp_path / "capital", policy=policy)
    ledger.initialize(
        OpeningStateManifest(
            market="ashare",
            authority_id=policy.capital_authority_id,
            cutover_decision_id=PINNED_CUTOVER_DECISION_ID,
            mode="fresh_start",
            as_of=TRADE_DATE.replace("-", ""),
            cash_balance_cny=50_000.0,
            opening_equity_cny=50_000.0,
            active_reservations_cny=0.0,
            consecutive_losses=0,
            inherited_high_water_equity_cny=0.0,
            positions_by_risk_unit={},
            position_margin_by_risk_unit={},
            frozen_order_cash_cny=0.0,
            realized_pnl_cny=0.0,
            unrealized_pnl_cny=0.0,
            source="capital-stage-safety-bootstrap",
            source_sha256=_sha_text("capital-stage-safety-bootstrap"),
            execution_lineage_id=LINEAGE,
            real=False,
        ),
        cutover_manifest={
            "cutover_decision_id": PINNED_CUTOVER_DECISION_ID,
            "source_thread_id": PINNED_SOURCE_THREAD_ID,
            "cutover_state": "fresh_start_approved",
            "authority_generation": 1,
        },
        legacy_freeze_manifest={
            "events_path": str(legacy_events),
            "sha256": hashlib.sha256(legacy_events.read_bytes()).hexdigest(),
            "last_event_id": "LEGACY-1",
            "row_count": 1,
            "frozen_at": "2026-07-16T00:00:00+08:00",
            "archive_path": str(legacy_archive),
            "imported": False,
        },
    )
    return ledger


@dataclass(frozen=True)
class _Bundle:
    context: Any
    run_id: str = "ashare-paper-safety-run"
    bundle_sha256: str = "b" * 64
    stop_new_risk: bool = False
    position_authority_valid: bool = True
    permitted_order_ids: tuple[str, ...] = ()
    stage_payloads: Mapping[RunStage, Mapping[str, Any]] = field(default_factory=dict)

    def receipt_for(self, stage: RunStage) -> SimpleNamespace:
        return SimpleNamespace(payload=dict(self.stage_payloads[stage]))


class _StaticPort:
    def __init__(self, stage: RunStage, payload: Mapping[str, Any]) -> None:
        self.identity = ComponentIdentity(
            stage=stage,
            component_id=f"safety-{stage.value}",
            version="1",
            artifact_sha256=_sha_text(stage.value),
        )
        self._payload = dict(payload)

    def execute(self, request: StageRequest) -> StageResult:
        return StageResult(payload=self._payload)


def _bundle(
    *,
    run_id: str = "ashare-paper-safety-run",
    permitted_order_ids: tuple[str, ...] = (),
    stage_payloads: Mapping[RunStage, Mapping[str, Any]] | None = None,
) -> _Bundle:
    return _Bundle(
        run_id=run_id,
        context=SimpleNamespace(
            authority_id="ashare-capital-v1",
            authority_generation=1,
            execution_lineage=LINEAGE,
            trade_date=TRADE_DATE,
            decision_as_of=DECISION_AS_OF,
            account_type="simulated",
            real_trading_enabled=False,
        ),
        permitted_order_ids=permitted_order_ids,
        stage_payloads=dict(stage_payloads or {}),
    )


def _request(
    *,
    stage: RunStage,
    bundle: _Bundle,
    idempotency_key: str | None = None,
    permitted_order_ids: tuple[str, ...] = (),
) -> StageRequest:
    return StageRequest(
        run_id=bundle.run_id,
        stage=stage,
        idempotency_key=idempotency_key or _sha_text(f"{bundle.run_id}:{stage.value}"),
        input_bundle_sha256=bundle.bundle_sha256,
        bundle=bundle,  # type: ignore[arg-type]
        allowed_actions=("open", "increase", "reduce", "exit", "hold"),
        permitted_order_ids=permitted_order_ids,
    )


def _preopen(
    account: PaperCapitalAccount,
    *,
    run_id: str = "ashare-paper-safety-run",
) -> None:
    port = CapitalBackedPreopenStagePort(
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
    )
    port.execute(_request(stage=RunStage.PREOPEN, bundle=_bundle(run_id=run_id)))


def _mark(
    price: float,
    *,
    trade_date: str = "2026-07-15",
    observed_at: str | None = None,
    available_at: str = "2026-07-15T15:00:00+08:00",
    decision_as_of: str = DECISION_AS_OF,
    session_calendar_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    observed = observed_at or f"{trade_date}T15:00:00+08:00"
    raw = {
        "price_cny": price,
        "market": "ashare",
        "trade_date": trade_date,
        "observed_at": observed,
        "available_at": available_at,
        "data_through": observed,
        "market_session": "close",
        "source_receipt_id": f"mark-{price}",
        "source_sha256": _sha_text(f"mark-{price}-{available_at}"),
        "data_authority_id": "frozen-paper-market-fixture-v1",
        "session_calendar_receipt": (
            session_calendar_receipt or _non_production_session_calendar_receipt()
        ),
        "real_trading_enabled": False,
    }
    return attach_mark_authority(
        raw,
        symbol="000001.SZ",
        decision_as_of=decision_as_of,
        capital_authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage=LINEAGE,
    )


def _snapshot(
    order_id: str,
    *,
    execution_time: str = "2026-07-16T09:31:00+08:00",
    decision_as_of: str = DECISION_AS_OF,
    attach_authority: bool = True,
    **updates: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "snapshot_id": f"snapshot-{order_id}",
        "source_receipt_id": f"quote-receipt-{order_id}",
        "source_sha256": _sha_text(f"snapshot-{order_id}"),
        "market": "ashare",
        "trade_date": TRADE_DATE,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
        "account_type": "simulated",
        "real_trading_enabled": False,
        "observed_at": execution_time,
        "available_at": execution_time,
        "data_through": execution_time,
        "execution_time": execution_time,
        "ask_price": 10.0,
        "ask_size": 1_000,
        "previous_close": 9.9,
        "market_session": "continuous_auction_am",
        "session_calendar_receipt": _non_production_session_calendar_receipt(),
        "cash_available": 50_000.0,
    }
    value.update(updates)
    if not attach_authority:
        return value
    return attach_quote_authority(
        value,
        symbol=str(value.get("symbol") or "000001.SZ"),
        decision_as_of=decision_as_of,
    )


def _buy_order(order_id: str = "ORDER-1") -> dict[str, Any]:
    return {
        "order_id": order_id,
        "decision_id": f"DECISION-{order_id}",
        "symbol": "000001.SZ",
        "intent": "open",
        "side": "buy",
        "quantity": 100,
        "reservation_price_cny": 10.5,
        "expected_fee_cny": 6.0,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
    }


class _TightensOnFinalCommitCheck(CapitalEffectGuard):
    identity_sha256 = "c" * 64

    def __init__(self) -> None:
        self.commit_checks = 0

    def authorize(
        self,
        *,
        effect: str,
        request: StageRequest,
        order: Mapping[str, Any],
    ) -> CapitalEffectAuthorization:
        del request, order
        if effect == "capital_commit":
            self.commit_checks += 1
            if self.commit_checks > 1:
                return CapitalEffectAuthorization(
                    allowed=False,
                    reason="drift_stop_new_risk:final-reread",
                )
        return CapitalEffectAuthorization(allowed=True, reason="authorized")


class _StopsNewRiskAfterCrash(CapitalEffectGuard):
    identity_sha256 = "d" * 64

    def __init__(self) -> None:
        self.stop_new_risk = False
        self.release_checks = 0

    def authorize(
        self,
        *,
        effect: str,
        request: StageRequest,
        order: Mapping[str, Any],
    ) -> CapitalEffectAuthorization:
        del request, order
        if effect == "reservation_release":
            self.release_checks += 1
            return CapitalEffectAuthorization(
                allowed=True,
                reason="cleanup_authorized",
            )
        if self.stop_new_risk and effect in {"sim_submit", "capital_commit"}:
            return CapitalEffectAuthorization(
                allowed=False,
                reason="drift_stop_new_risk:after-crash",
            )
        return CapitalEffectAuthorization(allowed=True, reason="authorized")


class _RejectsReservationRelease(CapitalEffectGuard):
    identity_sha256 = "e" * 64

    def authorize(
        self,
        *,
        effect: str,
        request: StageRequest,
        order: Mapping[str, Any],
    ) -> CapitalEffectAuthorization:
        del request, order
        if effect == "reservation_release":
            return CapitalEffectAuthorization(
                allowed=False,
                reason="manual_release_hold",
            )
        return CapitalEffectAuthorization(allowed=True, reason="authorized")


def _reserve(account: PaperCapitalAccount, order: Mapping[str, Any]) -> dict[str, Any]:
    return dict(
        CapitalBackedRiskStagePort(
            base_port=_StaticPort(
                RunStage.RISK_CHECKED,
                {
                    "risk_policy_version": "safety-risk-v1",
                    "oms_plan_id": "safety-plan",
                    "approved_orders": [dict(order)],
                    "rejected_decisions": [],
                },
            ),
            account=account,
        )
        .execute(_request(stage=RunStage.RISK_CHECKED, bundle=_bundle()))
        .payload
    )


def test_reconcile_intent_uses_derived_filename_and_cannot_escape_root(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    artifact_root = tmp_path / "artifacts"
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={},
    )
    port = CapitalBackedPreopenStagePort(
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
    )

    port.execute(
        _request(
            stage=RunStage.PREOPEN,
            bundle=_bundle(),
            idempotency_key="../../escaped",
        )
    )

    assert not (tmp_path / "escaped.json").exists()
    [intent] = (artifact_root / "intents").iterdir()
    assert intent.name.endswith(".json")
    assert "escaped" not in intent.name


def test_nested_artifact_symlink_fails_before_ledger_mutation(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    before = ledger.validate_checksum_chain()["event_count"]
    artifact_root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifact_root.mkdir()
    outside.mkdir()
    (artifact_root / "intents").symlink_to(outside, target_is_directory=True)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={},
    )

    with pytest.raises(PaperCapitalStageError, match="symlink"):
        _preopen(account)

    assert list(outside.iterdir()) == []
    assert ledger.validate_checksum_chain()["event_count"] == before


def test_risk_preflight_rejects_wrong_authority_and_non_mainboard_before_reserve(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    order = _buy_order("BAD-ORDER")
    order.update(
        {
            "symbol": "300001.SZ",
            "capital_authority_id": "wrong-authority",
            "authority_generation": 999,
            "execution_lineage": "wrong-lineage",
        }
    )
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(PaperCapitalStageError, match="capital_risk_preflight"):
        _reserve(account, order)

    snapshot = ledger.snapshot()
    assert snapshot.reserved_cash_cny == 0.0
    assert snapshot.reserved_exposure_cny == 0.0
    assert ledger.validate_checksum_chain()["event_count"] == before


def test_sell_cannot_inherit_buy_reservation_at_risk_preflight(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    buy_order = _reserve(account, _buy_order("BUY-1"))["approved_orders"][0]
    reserved_before = ledger.snapshot().reserved_cash_cny
    sell_order = {
        **buy_order,
        "order_id": "SELL-1",
        "decision_id": "DECISION-SELL-1",
        "intent": "reduce",
        "side": "sell",
        "quantity": 100,
        "reservation_price_cny": 10.0,
        "expected_fee_cny": 6.0,
    }

    with pytest.raises(
        PaperCapitalStageError,
        match="capital_risk_sell_reservation_fields_forbidden",
    ):
        _reserve(account, sell_order)

    assert reserved_before == 1_056.0
    assert ledger.snapshot().reserved_cash_cny == reserved_before


def test_execution_sell_cannot_release_foreign_buy_reservation(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    buy_order = _reserve(account, _buy_order("BUY-1"))["approved_orders"][0]
    reserved_before = ledger.snapshot().reserved_cash_cny
    sell_order = {
        **buy_order,
        "order_id": "SELL-1",
        "decision_id": "DECISION-SELL-1",
        "intent": "reduce",
        "side": "sell",
        "quantity": 100,
        "reservation_price_cny": 10.0,
        "expected_fee_cny": 6.0,
        "sellable_quantity": 0,
    }
    execution_bundle = _bundle(
        permitted_order_ids=("SELL-1",),
        stage_payloads={
            RunStage.RISK_CHECKED: {
                "risk_policy_version": "safety-risk-v1",
                "oms_plan_id": "safety-plan",
                "approved_orders": [sell_order],
                "rejected_decisions": [],
            }
        },
    )

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_execution_sell_reservation_fields_forbidden",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                "SELL-1": _snapshot(
                    "SELL-1",
                    bid_price=9.9,
                    bid_size=1_000,
                    sellable_qty=0,
                )
            },
            execution_clock=_clock(),
        ).execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("SELL-1",),
            )
        )

    assert reserved_before == 1_056.0
    assert ledger.snapshot().reserved_cash_cny == reserved_before


def test_legacy_sell_reservation_aliases_fail_closed_before_execution(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    sell_order = {
        **_buy_order("SELL-LEGACY"),
        "intent": "reduce",
        "side": "sell",
        "market_capital_event_id": "LEGACY-FOREIGN-EVENT",
        "sellable_quantity": 0,
    }
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(
        PaperCapitalStageError,
        match="capital_risk_sell_reservation_fields_forbidden",
    ):
        _reserve(account, sell_order)

    execution_bundle = _bundle(
        permitted_order_ids=("SELL-LEGACY",),
        stage_payloads={
            RunStage.RISK_CHECKED: {
                "risk_policy_version": "safety-risk-v1",
                "oms_plan_id": "safety-plan",
                "approved_orders": [sell_order],
                "rejected_decisions": [],
            }
        },
    )
    with pytest.raises(
        PaperCapitalStageError,
        match="paper_execution_sell_reservation_fields_forbidden",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                "SELL-LEGACY": _snapshot(
                    "SELL-LEGACY",
                    bid_price=9.9,
                    bid_size=1_000,
                    sellable_qty=0,
                )
            },
            execution_clock=_clock(),
        ).execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("SELL-LEGACY",),
            )
        )

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_reconcile_rejects_unknown_orders_before_writing_ledger(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    before = ledger.validate_checksum_chain()["event_count"]
    bundle = _bundle(
        stage_payloads={
            RunStage.ORDERS_SIMULATED: {
                "execution_lineage": LINEAGE,
                "account_type": "simulated",
                "real_trading_enabled": False,
                "order_receipts": [],
                "unknown_order_ids": ["UNKNOWN"],
            }
        }
    )

    with pytest.raises(PaperCapitalStageError, match="unknown_order"):
        CapitalBackedReconcileStagePort(
            account=account,
            reconciled_at="2026-07-16T15:01:00+08:00",
        ).execute(_request(stage=RunStage.RECONCILED, bundle=bundle))

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_reconcile_rejects_forged_buy_release_event_id(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(
        account,
        {
            **_buy_order("ORDER-RELEASE"),
            "order_type": "limit",
            "reservation_price_cny": 9.5,
            "expected_fee_cny": 5.0,
        },
    )
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-RELEASE",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    execution_payload = dict(
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                "ORDER-RELEASE": _snapshot(
                    "ORDER-RELEASE",
                )
            },
            execution_clock=_clock(),
        )
        .execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("ORDER-RELEASE",),
            )
        )
        .payload
    )
    [receipt] = execution_payload["order_receipts"]
    assert receipt["capital_release_status"] == "released"
    assert receipt["capital_release_receipt_id"]
    execution_payload["order_receipts"] = [
        {
            **receipt,
            "capital_release_receipt_id": "MCAP-" + "0" * 32,
        }
    ]
    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-RELEASE",),
        stage_payloads={
            RunStage.RISK_CHECKED: risk_payload,
            RunStage.ORDERS_SIMULATED: execution_payload,
        },
    )

    with pytest.raises(
        PaperCapitalStageError,
        match="execution_unfilled_release_event_invalid",
    ):
        CapitalBackedReconcileStagePort(
            account=account,
            reconciled_at="2026-07-16T15:01:00+08:00",
        ).execute(
            _request(
                stage=RunStage.RECONCILED,
                bundle=reconcile_bundle,
                permitted_order_ids=("ORDER-RELEASE",),
            )
        )


def test_unfilled_buy_cannot_partially_release_tampered_reservation_amount(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(
        account,
        {
            **_buy_order("ORDER-PARTIAL-RELEASE"),
            "order_type": "limit",
            "reservation_price_cny": 9.5,
            "expected_fee_cny": 5.0,
        },
    )
    [approved_order] = risk_payload["approved_orders"]
    original_reserved = float(approved_order["market_reserved_cash_cny"])
    before = ledger.validate_checksum_chain()["event_count"]
    assert ledger.snapshot().reserved_cash_cny == pytest.approx(original_reserved)
    risk_payload["approved_orders"] = [
        {
            **approved_order,
            "market_reserved_cash_cny": original_reserved / 2,
        }
    ]
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-PARTIAL-RELEASE",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_execution_release_amount_mismatch",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                "ORDER-PARTIAL-RELEASE": _snapshot("ORDER-PARTIAL-RELEASE")
            },
            execution_clock=_clock(),
        ).execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("ORDER-PARTIAL-RELEASE",),
            )
        )

    assert ledger.snapshot().reserved_cash_cny == pytest.approx(original_reserved)
    assert ledger.validate_checksum_chain()["event_count"] == before


def test_execution_rejects_legacy_reservation_alias_on_buy_after_risk_bypass(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order("BUY-LEGACY-BYPASS"))
    [approved_order] = risk_payload["approved_orders"]
    risk_payload["approved_orders"] = [
        {
            **approved_order,
            "market_reserved_gross_cny": approved_order["market_reserved_exposure_cny"],
        }
    ]
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_execution_legacy_reservation_fields_forbidden",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"BUY-LEGACY-BYPASS": _snapshot("BUY-LEGACY-BYPASS")},
            execution_clock=_clock(),
        ).execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=_bundle(
                    permitted_order_ids=("BUY-LEGACY-BYPASS",),
                    stage_payloads={RunStage.RISK_CHECKED: risk_payload},
                ),
                permitted_order_ids=("BUY-LEGACY-BYPASS",),
            )
        )

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_canonical_release_verifier_rejects_nonterminal_partial_release(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order("ORDER-PARTIAL-VERIFY"))
    [approved_order] = risk_payload["approved_orders"]
    original_reserved = float(approved_order["market_reserved_cash_cny"])
    reason = "partial_release_must_not_reconcile"
    reference_id = (
        f"TA-PAPER-RELEASE:ashare-paper-day-fixed:{approved_order['order_id']}:{reason}"
    )
    result = ledger.release(
        approved_order["market_capital_reservation_id"],
        original_reserved / 2,
        reason,
        reference_id=reference_id,
    )

    verification = ledger.verify_release(
        reservation_id=approved_order["market_capital_reservation_id"],
        amount_cny=original_reserved / 2,
        reason=reason,
        reference_id=reference_id,
        expected_event_id=result["event_id"],
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage_id=LINEAGE,
        risk_unit_key="000001.SZ",
        require_terminal=True,
    )

    assert verification == {
        "verified": False,
        "reason": "release_not_terminal",
        "real_trading_enabled": False,
    }
    assert ledger.snapshot().reserved_cash_cny == pytest.approx(original_reserved / 2)


def test_release_guard_denial_cannot_be_reported_as_success(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(
        account,
        {
            **_buy_order("ORDER-RELEASE-DENIED"),
            "order_type": "limit",
            "reservation_price_cny": 9.5,
            "expected_fee_cny": 5.0,
        },
    )
    [approved_order] = risk_payload["approved_orders"]
    original_reserved = float(approved_order["market_reserved_cash_cny"])
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_capital_release_not_authorized:manual_release_hold",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                "ORDER-RELEASE-DENIED": _snapshot("ORDER-RELEASE-DENIED")
            },
            effect_guard=_RejectsReservationRelease(),
            execution_clock=_clock(),
        ).execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=_bundle(
                    permitted_order_ids=("ORDER-RELEASE-DENIED",),
                    stage_payloads={RunStage.RISK_CHECKED: risk_payload},
                ),
                permitted_order_ids=("ORDER-RELEASE-DENIED",),
            )
        )

    assert ledger.snapshot().reserved_cash_cny == pytest.approx(original_reserved)
    assert ledger.validate_checksum_chain()["event_count"] == before


def test_terminal_fill_cannot_be_replayed_as_unfilled_release(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order("ORDER-FILLED-NOT-RELEASED"))
    [approved_order] = risk_payload["approved_orders"]
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=_bundle(
            permitted_order_ids=("ORDER-FILLED-NOT-RELEASED",),
            stage_payloads={RunStage.RISK_CHECKED: risk_payload},
        ),
        permitted_order_ids=("ORDER-FILLED-NOT-RELEASED",),
    )
    port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={
            "ORDER-FILLED-NOT-RELEASED": _snapshot("ORDER-FILLED-NOT-RELEASED")
        },
        execution_clock=_clock(),
    )
    [receipt] = port.execute(request).payload["order_receipts"]
    assert receipt["capital_commit_status"] == "committed"
    assert ledger.snapshot().reserved_cash_cny == 0.0
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_capital_release_rejected:release_exceeds",
    ):
        port.release_unfilled(
            request=request,
            order=approved_order,
            reason="forged_unfilled_after_terminal_fill",
        )

    assert ledger.validate_checksum_chain()["event_count"] == before
    assert ledger.snapshot().reserved_cash_cny == 0.0


def test_reconcile_rejects_bad_fill_fingerprint_before_writing_ledger(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    _preopen(account)
    before = ledger.validate_checksum_chain()["event_count"]
    receipt = {
        "order_id": "ORDER-FORGED",
        "symbol": "000001.SZ",
        "intent": "open",
        "status": "filled",
        "requested_quantity": 100,
        "filled_quantity": 100,
        "residual_quantity": 0,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
        "execution_receipt_id": "receipt-forged",
        "market_evidence_receipt_id": "market-forged",
        "terminal_at": "2026-07-16T09:31:00+08:00",
        "filled_at": "2026-07-16T09:31:00+08:00",
        "filled_price_cny": 10.0,
        "fee_cny": 5.0,
        "slippage_cny": 0.0,
        "simulated_fill_id": "SIMFILL-FORGED",
        "capital_commit_receipt_id": "MCAP-FORGED",
        "capital_commit_status": "committed",
        "fill_fingerprint": "0" * 64,
        "real_trading_enabled": False,
    }
    bundle = _bundle(
        permitted_order_ids=("ORDER-FORGED",),
        stage_payloads={
            RunStage.ORDERS_SIMULATED: {
                "execution_lineage": LINEAGE,
                "account_type": "simulated",
                "real_trading_enabled": False,
                "order_receipts": [receipt],
                "unknown_order_ids": [],
            }
        },
    )

    with pytest.raises(PaperCapitalStageError, match="fill_fingerprint"):
        CapitalBackedReconcileStagePort(
            account=account,
            reconciled_at="2026-07-16T15:01:00+08:00",
        ).execute(_request(stage=RunStage.RECONCILED, bundle=bundle))

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_execution_persists_pending_and_settled_outbox(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    artifact_root = tmp_path / "artifacts"
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    assert result.payload["order_receipts"][0]["capital_commit_status"] == "committed"
    assert (
        len(result.payload["order_receipts"][0]["market_evidence_authority_sha256"])
        == 64
    )
    assert (
        len(result.payload["order_receipts"][0]["market_evidence_verification_sha256"])
        == 64
    )
    pending = list((artifact_root / "execution-outbox" / "pending").glob("*.json"))
    settled = list((artifact_root / "execution-outbox" / "settled").glob("*.json"))
    assert len(pending) == 1
    assert len(settled) == 1


def test_execution_port_requires_explicit_trusted_clock(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "clock-required-artifacts",
        mark_prices={},
    )

    with pytest.raises(PaperCapitalStageError, match="execution_clock_required"):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        )


def test_execution_rechecks_quote_freshness_before_sim_submit(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "stale-before-submit-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock("2026-07-16T09:32:00+08:00"),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["filled_quantity"] == 0
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["execution_reason"] == (
        "paper_market_snapshot_stale_before_sim_submit"
    )
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert ledger.snapshot().reserved_cash_cny == 0.0
    assert not (account.artifact_root / "execution-outbox").exists()


def test_execution_rechecks_quote_freshness_before_capital_commit(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "stale-before-commit-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(
            "2026-07-16T09:31:10+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:32:00+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["filled_quantity"] == 0
    assert receipt["capital_commit_status"] == "not_committed"
    assert receipt["execution_reason"] == (
        "paper_market_snapshot_stale_before_capital_commit"
    )
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert ledger.snapshot().reserved_cash_cny == 0.0
    assert not (account.artifact_root / "execution-outbox").exists()

    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: result.payload},
    )
    reconciled = CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))

    assert reconciled.payload["status"] == "reconciled"
    assert reconciled.payload["real_trading_enabled"] is False
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert ledger.snapshot().reserved_cash_cny == 0.0


def test_future_quote_before_submit_fails_closed_and_still_reconciles(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "future-before-submit-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock("2026-07-16T09:30:30+08:00"),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["execution_reason"] == (
        "paper_market_snapshot_future_before_sim_submit"
    )
    assert ledger.snapshot().reserved_cash_cny == 0.0

    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: result.payload},
    )
    reconciled = CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))

    assert reconciled.payload["status"] == "reconciled"
    assert reconciled.payload["real_trading_enabled"] is False


def test_subsecond_future_quote_cannot_be_truncated_into_a_fill(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "subsecond-future-quote-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={
            "ORDER-1": _snapshot(
                "ORDER-1",
                execution_time="2026-07-16T09:31:00.900000+08:00",
            )
        },
        execution_clock=_clock(
            "2026-07-16T09:31:00.100000+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:31:01+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["execution_reason"] == (
        "paper_market_snapshot_future_before_sim_submit"
    )
    assert receipt["market_execution_time"] == ("2026-07-16T09:31:00.900000+08:00")
    assert receipt["sim_submit_checked_at"] == ("2026-07-16T09:31:00.100000+08:00")
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}


def test_execution_uses_submit_time_for_fill_and_reconcile_checks_effect_order(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "positive-latency-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={
            "ORDER-1": _snapshot(
                "ORDER-1",
                execution_time="2026-07-16T09:31:00+08:00",
            )
        },
        execution_clock=_clock(
            "2026-07-16T09:31:20+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:31:21+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "filled"
    assert receipt["sim_submit_checked_at"] == "2026-07-16T09:31:20+08:00"
    assert receipt["filled_at"] == "2026-07-16T09:31:20+08:00"
    assert receipt["terminal_at"] == receipt["filled_at"]
    assert receipt["capital_commit_checked_at"] == "2026-07-16T09:31:21+08:00"

    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: result.payload},
    )
    CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))


def test_execution_rereads_authority_after_commit_clock_check(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "final-authority-reread-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    guard = _TightensOnFinalCommitCheck()

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        effect_guard=guard,
        execution_clock=_clock(
            "2026-07-16T09:31:10+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:31:11+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert guard.commit_checks == 2
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["capital_release_status"] == "released"
    assert receipt["execution_reason"] == "drift_stop_new_risk:final-reread"
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert ledger.snapshot().reserved_cash_cny == 0.0


def test_execution_fails_closed_when_capital_commit_clock_regresses(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "regressing-clock-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(
            "2026-07-16T09:31:20+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:31:10+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_committed"
    assert receipt["capital_release_status"] == "released"
    assert receipt["execution_reason"] == (
        "paper_market_clock_regressed_before_capital_commit"
    )
    assert receipt["terminal_at"] == receipt["sim_submit_checked_at"]
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert ledger.snapshot().reserved_cash_cny == 0.0

    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: result.payload},
    )
    CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))


def test_subsecond_capital_commit_regression_releases_and_reconciles(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "subsecond-regression-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(
            "2026-07-16T09:31:20.900000+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:31:20.100000+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_committed"
    assert receipt["execution_reason"] == (
        "paper_market_clock_regressed_before_capital_commit"
    )
    assert receipt["sim_submit_checked_at"] == ("2026-07-16T09:31:20.900000+08:00")
    assert receipt["capital_commit_checked_at"] == ("2026-07-16T09:31:20.100000+08:00")
    assert receipt["terminal_at"] == receipt["sim_submit_checked_at"]
    assert ledger.snapshot().reserved_cash_cny == 0.0

    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: result.payload},
    )
    CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))


def test_wrong_trade_date_commit_clock_is_auditable_and_reconciles(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "wrong-day-commit-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )

    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(
            "2026-07-16T09:31:10+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-17T09:31:10+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )

    [receipt] = result.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_committed"
    assert receipt["execution_reason"] == (
        "paper_market_clock_trade_date_mismatch_before_capital_commit"
    )
    assert receipt["capital_commit_checked_at"] == "2026-07-17T09:31:10+08:00"
    assert receipt["terminal_at"] == receipt["sim_submit_checked_at"]
    assert ledger.snapshot().reserved_cash_cny == 0.0

    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: result.payload},
    )
    reconciled = CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))
    assert reconciled.payload["status"] == "reconciled"


def test_reconcile_rejects_subsecond_commit_after_reconcile_time(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "subsecond-late-commit-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(
            "2026-07-16T09:31:20.100000+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:31:20.900000+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=execution_bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )
    assert result.payload["order_receipts"][0]["status"] == "filled"
    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: result.payload},
    )

    with pytest.raises(PaperCapitalStageError, match="effect_time_order"):
        CapitalBackedReconcileStagePort(
            account=account,
            reconciled_at="2026-07-16T09:31:20.200000+08:00",
        ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))


def test_reconcile_rejects_forged_effect_clock_order(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "forged-clock-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=execution_bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )
    receipt = dict(result.payload["order_receipts"][0])
    receipt["capital_commit_checked_at"] = "2026-07-16T09:30:59+08:00"
    receipt.pop("fill_fingerprint")
    receipt["fill_fingerprint"] = _sha_json(receipt)
    forged_payload = dict(result.payload)
    forged_payload["order_receipts"] = [receipt]
    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: forged_payload},
    )

    with pytest.raises(PaperCapitalStageError, match="effect_time_order"):
        CapitalBackedReconcileStagePort(
            account=account,
            reconciled_at="2026-07-16T15:01:00+08:00",
        ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))


def test_reconcile_rejects_uncommitted_without_commit_freshness_reason(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "forged-uncommitted-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    result = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(
            "2026-07-16T09:31:10+08:00",
            effect_overrides={
                "capital_commit:ORDER-1": "2026-07-16T09:32:00+08:00",
            },
        ),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=execution_bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )
    receipt = dict(result.payload["order_receipts"][0])
    receipt["execution_reason"] = "paper_market_forged_before_capital_commit"
    forged_payload = dict(result.payload)
    forged_payload["order_receipts"] = [receipt]
    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: forged_payload},
    )

    with pytest.raises(PaperCapitalStageError, match="uncommitted_reason"):
        CapitalBackedReconcileStagePort(
            account=account,
            reconciled_at="2026-07-16T15:01:00+08:00",
        ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))


def test_execution_replays_pending_outbox_after_commit_before_settle_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _init_ledger(tmp_path)
    artifact_root = tmp_path / "artifacts"
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=bundle,
        permitted_order_ids=("ORDER-1",),
    )
    first_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(),
    )

    def _crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault_after_capital_commit")

    monkeypatch.setattr(
        first_port,
        "_write_outbox_settlement",
        _crash,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="fault_after_capital_commit"):
        first_port.execute(request)

    event_count = ledger.validate_checksum_chain()["event_count"]
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    pending = list((artifact_root / "execution-outbox" / "pending").glob("*.json"))
    assert len(pending) == 1
    assert list((artifact_root / "execution-outbox" / "settled").glob("*.json")) == []

    replay = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(),
    ).execute(request)

    assert replay.payload["order_receipts"][0]["capital_commit_status"] == "committed"
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert ledger.validate_checksum_chain()["event_count"] == event_count
    settled = list((artifact_root / "execution-outbox" / "settled").glob("*.json"))
    assert len(settled) == 1


def test_pending_outbox_replay_precedes_new_risk_authority_tightening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _init_ledger(tmp_path)
    artifact_root = tmp_path / "tightened-replay-artifacts"
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=bundle,
        permitted_order_ids=("ORDER-1",),
    )
    guard = _StopsNewRiskAfterCrash()
    first_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        effect_guard=guard,
        execution_clock=_clock(),
    )

    def _crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault_after_capital_commit")

    monkeypatch.setattr(
        first_port,
        "_write_outbox_settlement",
        _crash,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="fault_after_capital_commit"):
        first_port.execute(request)

    event_count = ledger.validate_checksum_chain()["event_count"]
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    pending = list((artifact_root / "execution-outbox" / "pending").glob("*.json"))
    assert len(pending) == 1
    guard.stop_new_risk = True

    replay = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        effect_guard=guard,
        execution_clock=_clock(),
    ).execute(request)

    [receipt] = replay.payload["order_receipts"]
    assert receipt["capital_commit_status"] == "committed"
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert ledger.validate_checksum_chain()["event_count"] == event_count
    assert guard.release_checks == 0
    settled = list((artifact_root / "execution-outbox" / "settled").glob("*.json"))
    assert len(settled) == 1


def test_uncommitted_pending_outbox_cannot_bypass_tightened_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _init_ledger(tmp_path)
    artifact_root = tmp_path / "uncommitted-pending-artifacts"
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=bundle,
        permitted_order_ids=("ORDER-1",),
    )
    guard = _StopsNewRiskAfterCrash()
    first_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        effect_guard=guard,
        execution_clock=_clock(),
    )
    original_commit_fill = ledger.commit_fill

    def _crash_before_capital_commit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault_before_capital_commit")

    monkeypatch.setattr(ledger, "commit_fill", _crash_before_capital_commit)
    with pytest.raises(RuntimeError, match="fault_before_capital_commit"):
        first_port.execute(request)
    monkeypatch.setattr(ledger, "commit_fill", original_commit_fill)

    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    pending = list((artifact_root / "execution-outbox" / "pending").glob("*.json"))
    assert len(pending) == 1
    ledger.record_realized_pnl(
        reference_id="UNRELATED-AFTER-PENDING",
        amount_cny=0.0,
        trade_date="20260716",
        affects_loss_streak=False,
    )
    guard.stop_new_risk = True

    replay = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        effect_guard=guard,
        execution_clock=_clock(),
    ).execute(request)

    [receipt] = replay.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["capital_release_status"] == "released"
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert guard.release_checks == 1
    settled = list((artifact_root / "execution-outbox" / "settled").glob("*.json"))
    assert settled == []


def test_pending_outbox_recovery_rejects_local_trade_binding_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _init_ledger(tmp_path)
    artifact_root = tmp_path / "tampered-pending-artifacts"
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=bundle,
        permitted_order_ids=("ORDER-1",),
    )
    first_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
        execution_clock=_clock(),
    )

    def _crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault_after_capital_commit")

    monkeypatch.setattr(
        first_port,
        "_write_outbox_settlement",
        _crash,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="fault_after_capital_commit"):
        first_port.execute(request)

    pending_root = artifact_root / "execution-outbox" / "pending"
    [pending_path] = list(pending_root.glob("*.json"))
    intent = json.loads(pending_path.read_text(encoding="utf-8"))
    intent["stable_payload"]["local_trade_sha256"] = "a" * 64
    new_outbox_id = _sha_json(
        {
            "contract": "tradingagent.paper_execution_outbox_identity.v1",
            "operation": intent["operation"],
            "stable_payload": intent["stable_payload"],
        }
    )
    intent["outbox_id"] = new_outbox_id
    pending_path.unlink()
    (pending_root / f"{new_outbox_id}.json").write_text(
        json.dumps(
            intent,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_execution_outbox_recovery_invalid",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
            execution_clock=_clock(),
        ).execute(request)


def _sell_recovery_case(
    tmp_path: Path,
    *,
    case_id: str,
) -> tuple[
    MarketCapitalLedger,
    PaperCapitalAccount,
    _Bundle,
    StageRequest,
    dict[str, Any],
]:
    ledger = _init_ledger(tmp_path)
    bootstrap_account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / f"{case_id}-position-bootstrap",
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _preopen(bootstrap_account)
    buy_risk_payload = _reserve(bootstrap_account, _buy_order("BUY-FOR-SELL"))
    buy_bundle = _bundle(
        permitted_order_ids=("BUY-FOR-SELL",),
        stage_payloads={RunStage.RISK_CHECKED: buy_risk_payload},
    )
    buy_result = CapitalBackedSimulationExecutionStagePort(
        account=bootstrap_account,
        market_snapshots={"BUY-FOR-SELL": _snapshot("BUY-FOR-SELL")},
        execution_clock=_clock(),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=buy_bundle,
            permitted_order_ids=("BUY-FOR-SELL",),
        )
    )
    CapitalBackedReconcileStagePort(
        account=bootstrap_account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    ).execute(
        _request(
            stage=RunStage.RECONCILED,
            bundle=_bundle(
                permitted_order_ids=("BUY-FOR-SELL",),
                stage_payloads={RunStage.ORDERS_SIMULATED: buy_result.payload},
            ),
        )
    )
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}

    sell_account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / f"{case_id}-sell-artifacts",
        mark_prices={},
    )
    order_id = f"SELL-{case_id.upper()}"
    sell_order = {
        "order_id": order_id,
        "decision_id": f"DECISION-{order_id}",
        "symbol": "000001.SZ",
        "intent": "exit",
        "side": "sell",
        "quantity": 100,
        "reservation_price_cny": 10.2,
        "expected_fee_cny": 6.0,
        "sellable_quantity": 100,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
    }
    run_id = f"ashare-paper-sell-recovery-{case_id}"
    sell_bundle = _Bundle(
        run_id=run_id,
        context=SimpleNamespace(
            authority_id="ashare-capital-v1",
            authority_generation=1,
            execution_lineage=LINEAGE,
            trade_date="2026-07-17",
            decision_as_of="2026-07-17T10:00:00+08:00",
            account_type="simulated",
            real_trading_enabled=False,
        ),
        permitted_order_ids=(order_id,),
        stage_payloads={
            RunStage.RISK_CHECKED: {
                "risk_policy_version": "safety-risk-v1",
                "oms_plan_id": f"safety-plan-{order_id}",
                "approved_orders": [sell_order],
                "rejected_decisions": [],
            }
        },
    )
    sell_request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=sell_bundle,
        permitted_order_ids=(order_id,),
    )
    sell_snapshot = _snapshot(
        order_id,
        execution_time="2026-07-17T10:01:00+08:00",
        decision_as_of="2026-07-17T10:00:00+08:00",
        trade_date="2026-07-17",
        bid_price=10.2,
        bid_size=1_000,
        previous_close=10.1,
        sellable_qty=100,
    )
    return ledger, sell_account, sell_bundle, sell_request, sell_snapshot


def test_sell_replays_pending_outbox_only_after_idempotent_capital_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, account, bundle, request, snapshot = _sell_recovery_case(
        tmp_path,
        case_id="committed",
    )
    [order_id] = bundle.permitted_order_ids
    first_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={order_id: snapshot},
        execution_clock=_clock("2026-07-17T10:01:00+08:00"),
    )

    def _crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault_after_sell_capital_commit")

    monkeypatch.setattr(
        first_port,
        "_write_outbox_settlement",
        _crash,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="fault_after_sell_capital_commit"):
        first_port.execute(request)

    event_count = ledger.validate_checksum_chain()["event_count"]
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    pending_root = account.artifact_root / "execution-outbox" / "pending"
    [pending_path] = list(pending_root.glob("*.json"))
    pending_intent = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_intent["operation"] == "ashare_sell_commit"
    assert (
        list((account.artifact_root / "execution-outbox" / "settled").glob("*.json"))
        == []
    )

    original_commit_sell = ledger.commit_ashare_sell
    replay_decisions: list[Any] = []

    def _record_replay(commit_request: Any) -> Any:
        decision = original_commit_sell(commit_request)
        replay_decisions.append(decision)
        return decision

    monkeypatch.setattr(ledger, "commit_ashare_sell", _record_replay)
    replay = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={order_id: snapshot},
        execution_clock=_clock("2026-07-17T10:01:00+08:00"),
    ).execute(request)

    [receipt] = replay.payload["order_receipts"]
    assert receipt["capital_commit_status"] == "committed"
    assert replay_decisions and replay_decisions[0].committed is True
    assert replay_decisions[0].idempotent is True
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert ledger.validate_checksum_chain()["event_count"] == event_count
    settled = list(
        (account.artifact_root / "execution-outbox" / "settled").glob("*.json")
    )
    assert len(settled) == 1


def test_uncommitted_sell_pending_outbox_cannot_bypass_tightened_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, account, bundle, request, snapshot = _sell_recovery_case(
        tmp_path,
        case_id="uncommitted",
    )
    [order_id] = bundle.permitted_order_ids
    guard = _StopsNewRiskAfterCrash()
    first_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={order_id: snapshot},
        effect_guard=guard,
        execution_clock=_clock("2026-07-17T10:01:00+08:00"),
    )
    original_commit_sell = ledger.commit_ashare_sell

    def _crash_before_capital_commit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault_before_sell_capital_commit")

    monkeypatch.setattr(
        ledger,
        "commit_ashare_sell",
        _crash_before_capital_commit,
    )
    with pytest.raises(RuntimeError, match="fault_before_sell_capital_commit"):
        first_port.execute(request)
    monkeypatch.setattr(ledger, "commit_ashare_sell", original_commit_sell)

    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    pending_root = account.artifact_root / "execution-outbox" / "pending"
    [pending_path] = list(pending_root.glob("*.json"))
    pending_intent = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_intent["operation"] == "ashare_sell_commit"
    ledger.record_realized_pnl(
        reference_id="UNRELATED-AFTER-SELL-PENDING",
        amount_cny=0.0,
        trade_date="20260717",
        affects_loss_streak=False,
    )
    guard.stop_new_risk = True

    replay = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={order_id: snapshot},
        effect_guard=guard,
        execution_clock=_clock("2026-07-17T10:01:00+08:00"),
    ).execute(request)

    [receipt] = replay.payload["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["capital_release_status"] == "not_applicable"
    assert receipt["capital_release_receipt_id"] is None
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert guard.release_checks == 0
    assert (
        list((account.artifact_root / "execution-outbox" / "settled").glob("*.json"))
        == []
    )


def test_pending_sell_outbox_recovery_rejects_local_position_binding_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, account, bundle, request, snapshot = _sell_recovery_case(
        tmp_path,
        case_id="tampered",
    )
    [order_id] = bundle.permitted_order_ids
    first_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={order_id: snapshot},
        execution_clock=_clock("2026-07-17T10:01:00+08:00"),
    )

    def _crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault_after_sell_capital_commit")

    monkeypatch.setattr(
        first_port,
        "_write_outbox_settlement",
        _crash,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="fault_after_sell_capital_commit"):
        first_port.execute(request)

    pending_root = account.artifact_root / "execution-outbox" / "pending"
    [pending_path] = list(pending_root.glob("*.json"))
    intent = json.loads(pending_path.read_text(encoding="utf-8"))
    assert intent["operation"] == "ashare_sell_commit"
    assert (
        intent["stable_payload"]["local_position_sha256"]
        == intent["commit_request"]["local_position_sha256"]
    )
    intent["stable_payload"]["local_position_sha256"] = "a" * 64
    new_outbox_id = _sha_json(
        {
            "contract": "tradingagent.paper_execution_outbox_identity.v1",
            "operation": intent["operation"],
            "stable_payload": intent["stable_payload"],
        }
    )
    intent["outbox_id"] = new_outbox_id
    pending_path.unlink()
    (pending_root / f"{new_outbox_id}.json").write_text(
        json.dumps(
            intent,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_execution_outbox_recovery_invalid",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={order_id: snapshot},
            execution_clock=_clock("2026-07-17T10:01:00+08:00"),
        ).execute(request)

    assert ledger.snapshot().positions_quantity_by_risk_unit == {}


def test_execution_snapshot_requires_aware_time_and_matching_authority(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "artifacts",
        mark_prices={},
    )
    bad_time = _snapshot("ORDER-TIME")
    for field_name in ("observed_at", "available_at", "data_through", "execution_time"):
        bad_time[field_name] = "2026-07-16T09:31:00"
    with pytest.raises(PaperCapitalStageError, match="timezone"):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-TIME": bad_time},
            execution_clock=_clock(),
        )

    snapshot = _snapshot("ORDER-1")
    snapshot["capital_authority_id"] = "wrong-authority"
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(PaperCapitalStageError, match="evidence_authority"):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-1": snapshot},
            execution_clock=_clock(),
        )

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_mark_price_requires_explicit_pit_evidence(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)

    with pytest.raises(PaperCapitalStageError, match="mark_evidence"):
        PaperCapitalAccount(
            ledger=ledger,
            artifact_root=tmp_path / "artifacts",
            mark_prices={"000001.SZ": 10.1},
        )


def test_mark_and_quote_require_frozen_market_evidence_authority(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    mark = _mark(10.1)
    mark.pop("market_evidence_authority")
    with pytest.raises(
        PaperCapitalStageError,
        match="paper_capital_mark_evidence_authority_required",
    ):
        PaperCapitalAccount(
            ledger=ledger,
            artifact_root=tmp_path / "missing-mark-authority",
            mark_prices={"000001.SZ": mark},
        )

    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "missing-quote-authority",
        mark_prices={},
    )
    quote = _snapshot("ORDER-1")
    quote.pop("market_evidence_authority")
    with pytest.raises(
        PaperCapitalStageError,
        match="paper_market_snapshot_evidence_authority_required",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-1": quote},
            execution_clock=_clock(),
        )


def test_mark_rejects_missing_verified_session_calendar_receipt(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)

    mark = _mark(10.1)
    mark.pop("session_calendar_receipt", None)
    with pytest.raises(
        PaperCapitalStageError,
        match="paper_capital_mark_session_calendar_receipt_required",
    ):
        PaperCapitalAccount(
            ledger=ledger,
            artifact_root=tmp_path / "missing-calendar-artifacts",
            mark_prices={"000001.SZ": mark},
        )


def test_execution_rejects_quote_older_than_ttl_before_capital_mutation(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "stale-quote-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    stale = _snapshot(
        "ORDER-1",
        execution_time="2026-07-16T09:31:00+08:00",
        observed_at="2026-07-16T09:29:00+08:00",
        available_at="2026-07-16T09:29:01+08:00",
        data_through="2026-07-16T09:29:00+08:00",
        source_receipt_id="quote-receipt-order-1",
        session_calendar_receipt=_non_production_session_calendar_receipt(),
    )
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(PaperCapitalStageError, match="paper_market_snapshot_stale"):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-1": stale},
            execution_clock=_clock(),
        ).execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("ORDER-1",),
            )
        )

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_preopen_rejects_mark_older_than_previous_verified_session(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    stale_mark = _mark(
        10.1,
        trade_date="2026-07-14",
        observed_at="2026-07-14T15:00:00+08:00",
        available_at="2026-07-14T15:00:00+08:00",
    )
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "stale-mark-artifacts",
        mark_prices={"000001.SZ": stale_mark},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={
            "ORDER-1": _snapshot(
                "ORDER-1",
                data_through="2026-07-16T09:31:00+08:00",
                source_receipt_id="quote-receipt-order-1",
                session_calendar_receipt=(_non_production_session_calendar_receipt()),
            )
        },
        execution_clock=_clock(),
    ).execute(
        _request(
            stage=RunStage.ORDERS_SIMULATED,
            bundle=execution_bundle,
            permitted_order_ids=("ORDER-1",),
        )
    )
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_capital_mark_not_previous_verified_session_close",
    ):
        _preopen(account, run_id="stale-mark-second-preopen")

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_mark_cannot_treat_first_calendar_session_as_its_own_previous_close(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    same_day_mark = _mark(
        10.1,
        trade_date="2026-07-16",
        observed_at="2026-07-16T15:00:00+08:00",
        available_at="2026-07-16T15:00:00+08:00",
        decision_as_of="2026-07-16T16:00:00+08:00",
        session_calendar_receipt=_single_session_calendar_receipt(),
    )
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "first-session-artifacts",
        mark_prices={"000001.SZ": same_day_mark},
    )

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_capital_mark_session_calendar_invalid",
    ):
        account.verified_mark_evidence_binding(
            symbols=("000001.SZ",),
            pit_timestamp="2026-07-16T16:00:00+08:00",
            trade_date_value="2026-07-16",
        )


def test_execution_rejects_session_label_inconsistent_with_quote_time(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "wrong-session-artifacts",
        mark_prices={},
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    wrong_session = _snapshot(
        "ORDER-1",
        market_session="continuous_auction_pm",
        data_through="2026-07-16T09:31:00+08:00",
        source_receipt_id="quote-receipt-order-1",
        session_calendar_receipt=_non_production_session_calendar_receipt(),
    )
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_market_snapshot_session_invalid",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-1": wrong_session},
            execution_clock=_clock(),
        ).execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("ORDER-1",),
            )
        )

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_execution_rejects_closing_auction_as_continuous_session(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "closing-auction-artifacts",
        mark_prices={},
    )
    snapshot = _snapshot(
        "ORDER-CLOSE-AUCTION",
        execution_time="2026-07-16T14:58:00+08:00",
        market_session="continuous_auction_pm",
    )
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(
        PaperCapitalStageError,
        match="paper_market_snapshot_session_invalid",
    ):
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-CLOSE-AUCTION": snapshot},
            execution_clock=_clock("2026-07-16T14:58:00+08:00"),
        )

    assert ledger.validate_checksum_chain()["event_count"] == before


def test_reconcile_rejects_future_mark_evidence_before_ledger_mutation(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "future-mark-artifacts",
        mark_prices={
            "000001.SZ": _mark(
                10.1,
                available_at="2026-07-16T16:00:00+08:00",
                decision_as_of="2026-07-16T17:00:00+08:00",
            )
        },
    )
    _preopen(account)
    risk_payload = _reserve(account, _buy_order())
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    execution_payload = (
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={"ORDER-1": _snapshot("ORDER-1")},
            execution_clock=_clock(),
        )
        .execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("ORDER-1",),
            )
        )
        .payload
    )
    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.ORDERS_SIMULATED: execution_payload},
    )
    before = ledger.validate_checksum_chain()["event_count"]

    with pytest.raises(PaperCapitalStageError, match="mark_evidence_future"):
        CapitalBackedReconcileStagePort(
            account=account,
            reconciled_at="2026-07-16T15:01:00+08:00",
        ).execute(_request(stage=RunStage.RECONCILED, bundle=reconcile_bundle))

    assert ledger.validate_checksum_chain()["event_count"] == before
