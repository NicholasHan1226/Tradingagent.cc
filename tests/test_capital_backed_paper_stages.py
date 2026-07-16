from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

import pytest

from shared.capital.market_ledger import (
    MarketCapitalLedger,
    MarketCapitalLedgerError,
    OpeningStateManifest,
)
from shared.capital.market_policy import (
    PINNED_CUTOVER_DECISION_ID,
    PINNED_SOURCE_THREAD_ID,
    MarketPolicy,
)
from shared.runtime.capital_stages import (
    CapitalBackedPreopenStagePort,
    CapitalBackedReconcileStagePort,
    CapitalBackedRiskStagePort,
    CapitalBackedSimulationExecutionStagePort,
    PaperCapitalAccount,
    PaperCapitalStageError,
)
from shared.runtime.day_loop import StageRequest, StageResult
from shared.runtime.run_bundle import ComponentIdentity, RunStage
from shared.runtime.trusted_clock import NonProductionFixtureExecutionClock
from tests._ashare_validation_plan_fixture import (
    build_non_production_ashare_validation_plan,
)
from tests._market_evidence_fixture import (
    attach_mark_authority,
    attach_quote_authority,
)


TRADE_DATE = "2026-07-16"
DECISION_AS_OF = "2026-07-16T09:30:00+08:00"
LINEAGE = "paper-capital-lineage-20260716"


def _clock(instant: str) -> NonProductionFixtureExecutionClock:
    return NonProductionFixtureExecutionClock.from_isoformat(
        default_instant=instant,
        effect_overrides={},
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_calendar_receipt() -> dict[str, Any]:
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


def _mark(
    price: float,
    *,
    trade_date: str = "2026-07-15",
    observed_at: str | None = None,
    available_at: str | None = None,
    decision_as_of: str = DECISION_AS_OF,
    authority_generation: int = 1,
    execution_lineage: str = LINEAGE,
) -> dict[str, Any]:
    observed = observed_at or f"{trade_date}T15:00:00+08:00"
    available = available_at or observed
    raw = {
        "price_cny": price,
        "market": "ashare",
        "trade_date": trade_date,
        "observed_at": observed,
        "available_at": available,
        "data_through": observed,
        "market_session": "close",
        "source_receipt_id": f"mark-{trade_date}-{price}",
        "source_sha256": _sha(f"mark-{trade_date}-{price}-{observed}-{available}"),
        "data_authority_id": "frozen-paper-market-fixture-v1",
        "session_calendar_receipt": _session_calendar_receipt(),
        "real_trading_enabled": False,
    }
    return attach_mark_authority(
        raw,
        symbol="000001.SZ",
        decision_as_of=decision_as_of,
        capital_authority_id="ashare-capital-v1",
        authority_generation=authority_generation,
        execution_lineage=execution_lineage,
    )


def _market_snapshot(
    order_id: str,
    *,
    trade_date: str,
    execution_time: str,
    decision_as_of: str | None = None,
    **updates: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "snapshot_id": f"SNAPSHOT-{order_id}",
        "source_receipt_id": f"quote-receipt-{order_id}",
        "source_sha256": _sha(f"SNAPSHOT-{order_id}-{execution_time}"),
        "market": "ashare",
        "trade_date": trade_date,
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
        "session_calendar_receipt": _session_calendar_receipt(),
        "cash_available": 50_000.0,
    }
    value.update(updates)
    return attach_quote_authority(
        value,
        symbol=str(value.get("symbol") or "000001.SZ"),
        decision_as_of=(decision_as_of or f"{trade_date}T09:30:00+08:00"),
    )


def _init_ledger(
    tmp_path: Path,
    *,
    bootstrap_date: str = TRADE_DATE,
    authority_generation: int | None = None,
    execution_lineage: str | None = None,
) -> MarketCapitalLedger:
    legacy_archive = tmp_path / "legacy-archive"
    legacy_archive.mkdir(parents=True)
    legacy_events = tmp_path / "legacy-events.jsonl"
    legacy_events.write_text(
        json.dumps({"event_id": "LEGACY-1"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    policy = MarketPolicy.load("ashare")
    if authority_generation is not None:
        policy = replace(policy, authority_generation=authority_generation)
    lineage = LINEAGE if execution_lineage is None else execution_lineage
    ledger = MarketCapitalLedger(tmp_path / "capital", policy=policy)
    ledger.initialize(
        OpeningStateManifest(
            market="ashare",
            authority_id=policy.capital_authority_id,
            cutover_decision_id=PINNED_CUTOVER_DECISION_ID,
            mode="fresh_start",
            as_of=bootstrap_date.replace("-", ""),
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
            source="test-capital-bootstrap",
            source_sha256=_sha("test-capital-bootstrap"),
            execution_lineage_id=lineage,
            real=False,
        ),
        cutover_manifest={
            "cutover_decision_id": PINNED_CUTOVER_DECISION_ID,
            "source_thread_id": PINNED_SOURCE_THREAD_ID,
            "cutover_state": "fresh_start_approved",
            "authority_generation": policy.authority_generation,
        },
        legacy_freeze_manifest={
            "events_path": str(legacy_events),
            "sha256": hashlib.sha256(legacy_events.read_bytes()).hexdigest(),
            "last_event_id": "LEGACY-1",
            "row_count": 1,
            "frozen_at": f"{bootstrap_date}T00:00:00+08:00",
            "archive_path": str(legacy_archive),
            "imported": False,
        },
    )
    return ledger


@dataclass(frozen=True)
class _BundleStub:
    context: Any
    run_id: str = "ashare-paper-test-run"
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
            component_id=f"test-{stage.value}",
            version="1",
            artifact_sha256=_sha(stage.value),
        )
        self._payload = dict(payload)

    def execute(self, request: StageRequest) -> StageResult:
        assert request.stage is self.identity.stage
        return StageResult(payload=self._payload)


def _bundle(
    *,
    permitted_order_ids: tuple[str, ...] = (),
    stage_payloads: Mapping[RunStage, Mapping[str, Any]] | None = None,
    run_id: str = "ashare-paper-test-run",
    trade_date: str = TRADE_DATE,
    decision_as_of: str = DECISION_AS_OF,
) -> _BundleStub:
    return _BundleStub(
        context=SimpleNamespace(
            authority_id="ashare-capital-v1",
            authority_generation=1,
            execution_lineage=LINEAGE,
            trade_date=trade_date,
            decision_as_of=decision_as_of,
            account_type="simulated",
            real_trading_enabled=False,
        ),
        run_id=run_id,
        permitted_order_ids=permitted_order_ids,
        stage_payloads=dict(stage_payloads or {}),
    )


def _request(
    *,
    stage: RunStage,
    bundle: _BundleStub,
    permitted_order_ids: tuple[str, ...] = (),
) -> StageRequest:
    return StageRequest(
        run_id=bundle.run_id,
        stage=stage,
        idempotency_key=_sha(f"{bundle.run_id}:{stage.value}"),
        input_bundle_sha256=bundle.bundle_sha256,
        bundle=bundle,  # type: ignore[arg-type]
        allowed_actions=("open", "increase", "reduce", "exit", "hold"),
        permitted_order_ids=permitted_order_ids,
    )


def _execute_buy(
    *,
    account: PaperCapitalAccount,
    order_id: str,
    run_id: str,
    trade_date: str,
    decision_as_of: str,
    execution_time: str,
    quantity: int = 100,
) -> Mapping[str, Any]:
    order = {
        "order_id": order_id,
        "decision_id": f"DECISION-{order_id}",
        "symbol": "000001.SZ",
        "intent": "open",
        "side": "buy",
        "quantity": quantity,
        "reservation_price_cny": 10.5,
        "expected_fee_cny": 6.0,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
    }
    context = {
        "run_id": run_id,
        "trade_date": trade_date,
        "decision_as_of": decision_as_of,
    }
    preopen_bundle = _bundle(**context)
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
    ).execute(_request(stage=RunStage.PREOPEN, bundle=preopen_bundle))
    risk_bundle = _bundle(**context)
    risk_payload = (
        CapitalBackedRiskStagePort(
            base_port=_StaticPort(
                RunStage.RISK_CHECKED,
                {
                    "risk_policy_version": "test-risk-v1",
                    "oms_plan_id": f"PLAN-{order_id}",
                    "approved_orders": [order],
                    "rejected_decisions": [],
                },
            ),
            account=account,
        )
        .execute(_request(stage=RunStage.RISK_CHECKED, bundle=risk_bundle))
        .payload
    )
    execution_bundle = _bundle(
        **context,
        permitted_order_ids=(order_id,),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    return (
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                order_id: _market_snapshot(
                    order_id,
                    trade_date=trade_date,
                    execution_time=execution_time,
                    decision_as_of=decision_as_of,
                )
            },
            execution_clock=_clock(execution_time),
        )
        .execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=(order_id,),
            )
        )
        .payload
    )


def _execute_sell(
    *,
    account: PaperCapitalAccount,
    order_id: str,
    run_id: str,
    trade_date: str,
    decision_as_of: str,
    execution_time: str,
    quantity: int = 100,
) -> Mapping[str, Any]:
    order = {
        "order_id": order_id,
        "decision_id": f"DECISION-{order_id}",
        "symbol": "000001.SZ",
        "intent": "exit",
        "side": "sell",
        "quantity": quantity,
        "reservation_price_cny": 10.2,
        "expected_fee_cny": 6.0,
        "sellable_quantity": quantity,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
    }
    sell_bundle = _bundle(
        run_id=run_id,
        trade_date=trade_date,
        decision_as_of=decision_as_of,
        permitted_order_ids=(order_id,),
        stage_payloads={
            RunStage.RISK_CHECKED: {
                "risk_policy_version": "test-risk-v1",
                "oms_plan_id": f"PLAN-{order_id}",
                "approved_orders": [order],
                "rejected_decisions": [],
            }
        },
    )
    return (
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                order_id: _market_snapshot(
                    order_id,
                    trade_date=trade_date,
                    execution_time=execution_time,
                    decision_as_of=decision_as_of,
                    bid_price=10.2,
                    bid_size=1_000,
                    previous_close=10.1,
                    sellable_qty=quantity,
                )
            },
            execution_clock=_clock(execution_time),
        )
        .execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=sell_bundle,
                permitted_order_ids=(order_id,),
            )
        )
        .payload
    )


def test_preopen_port_creates_current_day_reconcile_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "reconcile-artifacts",
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
    request = _request(stage=RunStage.PREOPEN, bundle=_bundle())

    first = port.execute(request).payload
    event_count = ledger.validate_checksum_chain()["event_count"]
    second = port.execute(request).payload

    assert first == second
    assert first["account_authority_valid"] is True
    assert first["position_authority_valid"] is True
    assert first["capital_reconcile_status"] in {
        "reconciled",
        "idempotent_reconcile",
    }
    assert first["capital_ledger_event_id"]
    assert first["capital_ledger_head_sha256"]
    assert ledger.provider_state(TRADE_DATE.replace("-", ""))["fresh"] is True
    assert ledger.validate_checksum_chain()["event_count"] == event_count


def test_risk_port_reserves_buy_cash_and_exposure_exactly_once(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "reconcile-artifacts",
        mark_prices={},
    )
    preopen = CapitalBackedPreopenStagePort(
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
    preopen.execute(_request(stage=RunStage.PREOPEN, bundle=_bundle()))
    order = {
        "order_id": "ORDER-1",
        "decision_id": "DECISION-1",
        "symbol": "000001.SZ",
        "intent": "open",
        "side": "buy",
        "quantity": 500,
        "reservation_price_cny": 10.0,
        "expected_fee_cny": 5.0,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
    }
    port = CapitalBackedRiskStagePort(
        base_port=_StaticPort(
            RunStage.RISK_CHECKED,
            {
                "risk_policy_version": "test-risk-v1",
                "oms_plan_id": "test-plan",
                "approved_orders": [order],
                "rejected_decisions": [],
            },
        ),
        account=account,
    )
    request = _request(stage=RunStage.RISK_CHECKED, bundle=_bundle())

    first = port.execute(request).payload
    event_count = ledger.validate_checksum_chain()["event_count"]
    second = port.execute(request).payload

    assert first == second
    approved = first["approved_orders"]
    assert len(approved) == 1
    assert approved[0]["market_capital_reservation_id"]
    assert approved[0]["market_capital_reservation_event_id"]
    assert approved[0]["market_reserved_cash_cny"] == 5_005.0
    assert approved[0]["market_reserved_exposure_cny"] == 5_000.0
    snapshot = ledger.snapshot()
    assert snapshot.reserved_cash_cny == 5_005.0
    assert snapshot.reserved_exposure_cny == 5_000.0
    # Cash would allow another CNY 44,995, but the A-share gross-exposure
    # budget has only CNY 40,000 left.  The ledger must expose the tighter
    # economic constraint rather than treating cash as unlimited risk budget.
    assert snapshot.cash_balance_cny - snapshot.reserved_cash_cny == 44_995.0
    assert snapshot.available_to_reserve_cny == 40_000.0
    assert ledger.validate_checksum_chain()["event_count"] == event_count


def test_execution_port_commits_simulated_buy_fill_exactly_once(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "reconcile-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    preopen = CapitalBackedPreopenStagePort(
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
    preopen.execute(_request(stage=RunStage.PREOPEN, bundle=_bundle()))
    order = {
        "order_id": "ORDER-1",
        "decision_id": "DECISION-1",
        "symbol": "000001.SZ",
        "intent": "open",
        "side": "buy",
        "quantity": 500,
        "reservation_price_cny": 10.5,
        "expected_fee_cny": 6.0,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
    }
    risk_port = CapitalBackedRiskStagePort(
        base_port=_StaticPort(
            RunStage.RISK_CHECKED,
            {
                "risk_policy_version": "test-risk-v1",
                "oms_plan_id": "test-plan",
                "approved_orders": [order],
                "rejected_decisions": [],
            },
        ),
        account=account,
    )
    risk_payload = risk_port.execute(
        _request(stage=RunStage.RISK_CHECKED, bundle=_bundle())
    ).payload
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-1",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    execution_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={
            "ORDER-1": _market_snapshot(
                "ORDER-1",
                trade_date=TRADE_DATE,
                execution_time="2026-07-16T09:31:00+08:00",
                ask_size=2_000,
            )
        },
        execution_clock=_clock("2026-07-16T09:31:00+08:00"),
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=execution_bundle,
        permitted_order_ids=("ORDER-1",),
    )

    first = execution_port.execute(request).payload
    event_count = ledger.validate_checksum_chain()["event_count"]
    second = execution_port.execute(request).payload

    assert first == second
    assert first["account_type"] == "simulated"
    assert first["real_trading_enabled"] is False
    assert first["unknown_order_ids"] == []
    [receipt] = first["order_receipts"]
    assert receipt["order_id"] == "ORDER-1"
    assert receipt["status"] == "filled"
    assert receipt["requested_quantity"] == 500
    assert receipt["filled_quantity"] == 500
    assert receipt["residual_quantity"] == 0
    assert receipt["capital_commit_status"] == "committed"
    assert receipt["capital_commit_receipt_id"]
    assert len(receipt["fill_fingerprint"]) == 64
    snapshot = ledger.snapshot()
    assert snapshot.positions_quantity_by_risk_unit == {"000001.SZ": 500}
    assert snapshot.reserved_cash_cny == 0.0
    assert snapshot.reserved_exposure_cny == 0.0
    assert snapshot.cash_balance_cny < 45_000.0
    assert ledger.validate_checksum_chain()["event_count"] == event_count


def test_restart_replays_committed_pending_outbox_without_duplicate_fill(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    artifact_root = tmp_path / "reconcile-artifacts"
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=artifact_root,
        mark_prices={"000001.SZ": _mark(10.1)},
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
    order = {
        "order_id": "ORDER-OUTBOX-RESTART",
        "decision_id": "DECISION-OUTBOX-RESTART",
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
    risk_payload = (
        CapitalBackedRiskStagePort(
            base_port=_StaticPort(
                RunStage.RISK_CHECKED,
                {
                    "risk_policy_version": "test-risk-v1",
                    "oms_plan_id": "test-plan",
                    "approved_orders": [order],
                    "rejected_decisions": [],
                },
            ),
            account=account,
        )
        .execute(_request(stage=RunStage.RISK_CHECKED, bundle=_bundle()))
        .payload
    )
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-OUTBOX-RESTART",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=execution_bundle,
        permitted_order_ids=("ORDER-OUTBOX-RESTART",),
    )
    market_snapshots = {
        "ORDER-OUTBOX-RESTART": _market_snapshot(
            "ORDER-OUTBOX-RESTART",
            trade_date=TRADE_DATE,
            execution_time="2026-07-16T09:31:00+08:00",
        )
    }
    execution_port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots=market_snapshots,
        execution_clock=_clock("2026-07-16T09:31:00+08:00"),
    )

    with patch.object(
        execution_port,
        "_write_outbox_settlement",
        side_effect=RuntimeError("injected_post_commit_crash"),
    ):
        with pytest.raises(RuntimeError, match="injected_post_commit_crash"):
            execution_port.execute(request)

    post_crash = ledger.snapshot()
    post_crash_event_count = ledger.validate_checksum_chain()["event_count"]
    assert post_crash.positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert post_crash.unreconciled_fill_commit_ids == (post_crash.event_id,)
    assert post_crash.reserved_cash_cny == 0.0
    assert post_crash.reserved_exposure_cny == 0.0
    [pending_path] = tuple(
        (artifact_root / "execution-outbox" / "pending").glob("*.json")
    )
    pending_payload = json.loads(pending_path.read_text(encoding="utf-8"))
    assert not (artifact_root / "execution-outbox" / "settled").exists()

    restarted_ledger = MarketCapitalLedger(
        tmp_path / "capital",
        policy=MarketPolicy.load("ashare"),
    )
    restarted_account = PaperCapitalAccount(
        ledger=restarted_ledger,
        artifact_root=artifact_root,
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    replay_payload = (
        CapitalBackedSimulationExecutionStagePort(
            account=restarted_account,
            market_snapshots=market_snapshots,
            execution_clock=_clock("2026-07-16T09:31:00+08:00"),
        )
        .execute(request)
        .payload
    )

    assert restarted_ledger.validate_checksum_chain()["event_count"] == (
        post_crash_event_count
    )
    [replayed_receipt] = replay_payload["order_receipts"]
    assert replayed_receipt["status"] == "filled"
    assert replayed_receipt["filled_quantity"] == 100
    assert replayed_receipt["capital_commit_status"] == "committed"
    assert replayed_receipt["capital_commit_receipt_id"] == post_crash.event_id
    replayed_snapshot = restarted_ledger.snapshot()
    assert replayed_snapshot.positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert replayed_snapshot.cash_balance_cny == post_crash.cash_balance_cny
    assert replayed_snapshot.unreconciled_fill_commit_ids == (post_crash.event_id,)
    assert replayed_snapshot.reserved_cash_cny == 0.0
    assert replayed_snapshot.reserved_exposure_cny == 0.0
    [settled_path] = tuple(
        (artifact_root / "execution-outbox" / "settled").glob("*.json")
    )
    settlement = json.loads(settled_path.read_text(encoding="utf-8"))
    expected_pending_sha256 = hashlib.sha256(
        json.dumps(
            pending_payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert settlement["capital_commit_event_id"] == post_crash.event_id
    assert settlement["pending_intent_sha256"] == expected_pending_sha256

    reconcile_payload = (
        CapitalBackedReconcileStagePort(
            account=restarted_account,
            reconciled_at="2026-07-16T15:01:00+08:00",
        )
        .execute(
            _request(
                stage=RunStage.RECONCILED,
                bundle=_bundle(
                    permitted_order_ids=("ORDER-OUTBOX-RESTART",),
                    stage_payloads={RunStage.ORDERS_SIMULATED: replay_payload},
                ),
            )
        )
        .payload
    )
    final_snapshot = restarted_ledger.snapshot()
    assert reconcile_payload["status"] == "reconciled"
    assert final_snapshot.reconciled is True
    assert final_snapshot.unreconciled_fill_commit_ids == ()
    assert final_snapshot.positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert final_snapshot.reserved_cash_cny == 0.0
    assert final_snapshot.reserved_exposure_cny == 0.0
    assert restarted_ledger.validate_checksum_chain()["event_count"] == (
        post_crash_event_count + 1
    )


def test_execution_port_releases_reservation_when_limit_is_not_filled(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "reconcile-artifacts",
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
    order = {
        "order_id": "ORDER-NOFILL",
        "decision_id": "DECISION-NOFILL",
        "symbol": "000001.SZ",
        "intent": "open",
        "side": "buy",
        "quantity": 100,
        "reservation_price_cny": 9.5,
        "expected_fee_cny": 5.0,
        "order_type": "limit",
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
    }
    risk_payload = (
        CapitalBackedRiskStagePort(
            base_port=_StaticPort(
                RunStage.RISK_CHECKED,
                {
                    "risk_policy_version": "test-risk-v1",
                    "oms_plan_id": "test-plan",
                    "approved_orders": [order],
                    "rejected_decisions": [],
                },
            ),
            account=account,
        )
        .execute(_request(stage=RunStage.RISK_CHECKED, bundle=_bundle()))
        .payload
    )
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-NOFILL",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={
            "ORDER-NOFILL": _market_snapshot(
                "ORDER-NOFILL",
                trade_date=TRADE_DATE,
                execution_time="2026-07-16T09:31:00+08:00",
            )
        },
        execution_clock=_clock("2026-07-16T09:31:00+08:00"),
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=execution_bundle,
        permitted_order_ids=("ORDER-NOFILL",),
    )

    first = port.execute(request).payload
    event_count = ledger.validate_checksum_chain()["event_count"]
    second = port.execute(request).payload

    assert first == second
    [receipt] = first["order_receipts"]
    assert receipt["status"] == "not_filled"
    assert receipt["filled_quantity"] == 0
    assert receipt["residual_quantity"] == 100
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["capital_release_status"] == "released"
    assert receipt["capital_release_receipt_id"]
    snapshot = ledger.snapshot()
    assert snapshot.positions_quantity_by_risk_unit == {}
    assert snapshot.reserved_cash_cny == 0.0
    assert snapshot.reserved_exposure_cny == 0.0
    assert snapshot.cash_balance_cny == 50_000.0
    assert ledger.validate_checksum_chain()["event_count"] == event_count


def test_reconcile_port_closes_fill_cash_position_and_receipt_chain(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "reconcile-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
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
    order = {
        "order_id": "ORDER-RECONCILE",
        "decision_id": "DECISION-RECONCILE",
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
    risk_payload = (
        CapitalBackedRiskStagePort(
            base_port=_StaticPort(
                RunStage.RISK_CHECKED,
                {
                    "risk_policy_version": "test-risk-v1",
                    "oms_plan_id": "test-plan",
                    "approved_orders": [order],
                    "rejected_decisions": [],
                },
            ),
            account=account,
        )
        .execute(_request(stage=RunStage.RISK_CHECKED, bundle=_bundle()))
        .payload
    )
    execution_bundle = _bundle(
        permitted_order_ids=("ORDER-RECONCILE",),
        stage_payloads={RunStage.RISK_CHECKED: risk_payload},
    )
    execution_payload = (
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                "ORDER-RECONCILE": _market_snapshot(
                    "ORDER-RECONCILE",
                    trade_date=TRADE_DATE,
                    execution_time="2026-07-16T09:31:00+08:00",
                )
            },
            execution_clock=_clock("2026-07-16T09:31:00+08:00"),
        )
        .execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=execution_bundle,
                permitted_order_ids=("ORDER-RECONCILE",),
            )
        )
        .payload
    )
    reconcile_bundle = _bundle(
        permitted_order_ids=("ORDER-RECONCILE",),
        stage_payloads={RunStage.ORDERS_SIMULATED: execution_payload},
    )
    port = CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T15:01:00+08:00",
    )
    request = _request(stage=RunStage.RECONCILED, bundle=reconcile_bundle)

    first = port.execute(request).payload
    event_count = ledger.validate_checksum_chain()["event_count"]
    second = port.execute(request).payload

    assert first == second
    assert first["status"] == "reconciled"
    assert first["account_authority_valid"] is True
    assert first["position_authority_valid"] is True
    assert first["source_run_id"] == reconcile_bundle.run_id
    assert first["source_input_bundle_sha256"] == reconcile_bundle.bundle_sha256
    assert first["unknown_order_ids"] == []
    assert first["unreconciled_order_ids"] == []
    assert len(first["order_receipts_sha256"]) == 64
    assert len(first["position_fingerprint"]) == 64
    assert len(first["capital_ledger_head_sha256"]) == 64
    snapshot = ledger.snapshot()
    assert snapshot.reconciled is True
    assert snapshot.unreconciled_fill_commit_ids == ()
    assert snapshot.positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert first["account_equity_cny"] == snapshot.equity_cny
    assert first["cash_cny"] == snapshot.cash_balance_cny
    assert ledger.validate_checksum_chain()["event_count"] == event_count


def test_ashare_t1_projection_blocks_same_day_sell_and_allows_next_day(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "t1-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
    )
    _execute_buy(
        account=account,
        order_id="ORDER-T1-BUY",
        run_id="RUN-T1-BUY",
        trade_date="2026-07-16",
        decision_as_of="2026-07-16T09:30:00+08:00",
        execution_time="2026-07-16T01:31:00+00:00",
    )

    assert ledger.ashare_sellable_quantities("20260716") == {"000001.SZ": 0}
    assert ledger.ashare_sellable_quantities("2026-07-16") == {"000001.SZ": 0}
    with pytest.raises(MarketCapitalLedgerError, match="invalid_trade_date"):
        ledger.ashare_sellable_quantities("2026-02-30")
    with pytest.raises(
        MarketCapitalLedgerError,
        match="ashare_sellable_as_of_before_ledger_fill",
    ):
        ledger.ashare_sellable_quantities("20260715")

    event_count = ledger.validate_checksum_chain()["event_count"]
    with pytest.raises(
        PaperCapitalStageError,
        match="paper_capital_sell_rejected:ashare_sell_quantity_exceeds_t1_sellable",
    ):
        _execute_sell(
            account=account,
            order_id="ORDER-T1-SELL-SAME-DAY",
            run_id="RUN-T1-SELL-SAME-DAY",
            trade_date="2026-07-16",
            decision_as_of="2026-07-16T10:00:00+08:00",
            execution_time="2026-07-16T10:01:00+08:00",
        )
    assert ledger.validate_checksum_chain()["event_count"] == event_count
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}

    assert ledger.ashare_sellable_quantities("20260717") == {"000001.SZ": 100}
    next_day = _execute_sell(
        account=account,
        order_id="ORDER-T1-SELL-NEXT-DAY",
        run_id="RUN-T1-SELL-NEXT-DAY",
        trade_date="2026-07-17",
        decision_as_of="2026-07-17T09:30:00+08:00",
        execution_time="2026-07-17T09:31:00+08:00",
    )
    [receipt] = next_day["order_receipts"]
    assert receipt["capital_commit_status"] == "committed"
    assert ledger.snapshot().positions_quantity_by_risk_unit == {}


def test_ashare_t1_projection_consumes_sellable_lots_fifo(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path, bootstrap_date="2026-07-15")
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "fifo-artifacts",
        mark_prices={
            "000001.SZ": _mark(
                10.1,
                trade_date="2026-07-15",
                available_at="2026-07-15T15:00:00+08:00",
            )
        },
    )
    _execute_buy(
        account=account,
        order_id="ORDER-FIFO-BUY-OLD",
        run_id="RUN-FIFO-BUY-OLD",
        trade_date="2026-07-15",
        decision_as_of="2026-07-15T09:30:00+08:00",
        execution_time="2026-07-15T09:31:00+08:00",
    )
    _execute_buy(
        account=account,
        order_id="ORDER-FIFO-BUY-NEW",
        run_id="RUN-FIFO-BUY-NEW",
        trade_date="2026-07-16",
        decision_as_of="2026-07-16T09:30:00+08:00",
        execution_time="2026-07-16T09:31:00+08:00",
    )
    assert ledger.ashare_sellable_quantities("20260716") == {"000001.SZ": 100}

    _execute_sell(
        account=account,
        order_id="ORDER-FIFO-SELL-OLD",
        run_id="RUN-FIFO-SELL-OLD",
        trade_date="2026-07-16",
        decision_as_of="2026-07-16T10:00:00+08:00",
        execution_time="2026-07-16T10:01:00+08:00",
    )
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    assert ledger.ashare_sellable_quantities("20260716") == {"000001.SZ": 0}
    assert ledger.ashare_sellable_quantities("20260717") == {"000001.SZ": 100}


def test_execution_port_commits_existing_position_sell_exactly_once(
    tmp_path: Path,
) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "reconcile-artifacts",
        mark_prices={"000001.SZ": _mark(10.1)},
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
    buy_order = {
        "order_id": "ORDER-BUY-BEFORE-SELL",
        "decision_id": "DECISION-BUY-BEFORE-SELL",
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
    buy_risk_payload = (
        CapitalBackedRiskStagePort(
            base_port=_StaticPort(
                RunStage.RISK_CHECKED,
                {
                    "risk_policy_version": "test-risk-v1",
                    "oms_plan_id": "test-plan-buy",
                    "approved_orders": [buy_order],
                    "rejected_decisions": [],
                },
            ),
            account=account,
        )
        .execute(_request(stage=RunStage.RISK_CHECKED, bundle=_bundle()))
        .payload
    )
    buy_bundle = _bundle(
        permitted_order_ids=("ORDER-BUY-BEFORE-SELL",),
        stage_payloads={RunStage.RISK_CHECKED: buy_risk_payload},
    )
    buy_execution = (
        CapitalBackedSimulationExecutionStagePort(
            account=account,
            market_snapshots={
                "ORDER-BUY-BEFORE-SELL": _market_snapshot(
                    "ORDER-BUY-BEFORE-SELL",
                    trade_date=TRADE_DATE,
                    execution_time="2026-07-16T09:31:00+08:00",
                )
            },
            execution_clock=_clock("2026-07-16T09:31:00+08:00"),
        )
        .execute(
            _request(
                stage=RunStage.ORDERS_SIMULATED,
                bundle=buy_bundle,
                permitted_order_ids=("ORDER-BUY-BEFORE-SELL",),
            )
        )
        .payload
    )
    CapitalBackedReconcileStagePort(
        account=account,
        reconciled_at="2026-07-16T10:00:00+08:00",
    ).execute(
        _request(
            stage=RunStage.RECONCILED,
            bundle=_bundle(
                permitted_order_ids=("ORDER-BUY-BEFORE-SELL",),
                stage_payloads={RunStage.ORDERS_SIMULATED: buy_execution},
            ),
        )
    )
    assert ledger.snapshot().positions_quantity_by_risk_unit == {"000001.SZ": 100}
    sell_order = {
        "order_id": "ORDER-SELL",
        "decision_id": "DECISION-SELL",
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
    sell_bundle = _bundle(
        run_id="ashare-paper-test-sell-run",
        trade_date="2026-07-17",
        decision_as_of="2026-07-17T10:00:00+08:00",
        permitted_order_ids=("ORDER-SELL",),
        stage_payloads={
            RunStage.RISK_CHECKED: {
                "risk_policy_version": "test-risk-v1",
                "oms_plan_id": "test-plan-sell",
                "approved_orders": [sell_order],
                "rejected_decisions": [],
            }
        },
    )
    port = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={
            "ORDER-SELL": _market_snapshot(
                "ORDER-SELL",
                trade_date="2026-07-17",
                execution_time="2026-07-17T10:01:00+08:00",
                decision_as_of="2026-07-17T10:00:00+08:00",
                bid_price=10.2,
                bid_size=1_000,
                previous_close=10.1,
                sellable_qty=100,
            )
        },
        execution_clock=_clock("2026-07-17T10:01:00+08:00"),
    )
    request = _request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=sell_bundle,
        permitted_order_ids=("ORDER-SELL",),
    )

    first = port.execute(request).payload
    event_count = ledger.validate_checksum_chain()["event_count"]
    second = port.execute(request).payload

    assert first == second
    [receipt] = first["order_receipts"]
    assert receipt["status"] == "filled"
    assert receipt["filled_quantity"] == 100
    assert receipt["residual_quantity"] == 0
    assert receipt["capital_commit_status"] == "committed"
    snapshot = ledger.snapshot()
    assert snapshot.positions_quantity_by_risk_unit == {}
    assert snapshot.positions_market_value_cny == 0.0
    assert snapshot.reserved_cash_cny == 0.0
    assert snapshot.cash_balance_cny > 49_990.0
    assert ledger.validate_checksum_chain()["event_count"] == event_count
