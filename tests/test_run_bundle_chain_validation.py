from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest

from shared.runtime.run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunBundleError,
    RunContext,
    RunStage,
    STAGE_ORDER,
    StageReceipt,
    parse_run_bundle,
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _initial_bundle() -> RunBundle:
    context = RunContext(
        trade_date="2026-07-16",
        decision_as_of="2026-07-16T09:05:00+08:00",
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage="ashare-sim-chain-fixture-v1",
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256="c" * 64,
    )
    components = tuple(
        ComponentIdentity(
            stage=stage,
            component_id=f"fixture-{stage.value}",
            version="1",
            artifact_sha256=f"{index + 1:x}" * 64,
        )
        for index, stage in enumerate(STAGE_ORDER)
    )
    return RunBundle.create(context, components)


def _idempotency_key(
    bundle: RunBundle,
    stage: RunStage,
    component: ComponentIdentity,
) -> str:
    return _canonical_sha256(
        {
            "run_id": bundle.run_id,
            "stage": stage.value,
            "input_bundle_sha256": bundle.bundle_sha256,
            "component_id": component.component_id,
            "component_version": component.version,
            "component_artifact_sha256": component.artifact_sha256,
        }
    )


def _stage_payload(stage: RunStage, bundle: RunBundle) -> dict[str, Any]:
    payloads: dict[RunStage, dict[str, Any]] = {
        RunStage.PREOPEN: {
            "market": "ashare",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "account_authority_valid": True,
            "position_authority_valid": True,
        },
        RunStage.EVIDENCE_READY: {
            "decision_as_of": bundle.context.decision_as_of,
        },
        RunStage.UNIVERSE_READY: {
            "feasible_symbols": ["000001.SZ"],
        },
        RunStage.DECISION_READY: {
            "champion_manifest_sha256": bundle.context.champion_manifest_sha256,
            "decisions": [
                {
                    "decision_id": "decision-1",
                    "symbol": "000001.SZ",
                    "action": "open",
                }
            ],
        },
        RunStage.RISK_CHECKED: {
            "approved_orders": [
                {
                    "order_id": "order-1",
                    "symbol": "000001.SZ",
                    "intent": "open",
                }
            ],
            "rejected_order_ids": [],
        },
        RunStage.ORDERS_SIMULATED: {
            "execution_lineage": bundle.context.execution_lineage,
            "account_type": "simulated",
            "real_trading_enabled": False,
            "order_receipts": [
                {
                    "order_id": "order-1",
                    "status": "filled",
                }
            ],
            "unknown_order_ids": [],
        },
        RunStage.RECONCILED: {
            "status": "reconciled",
            "position_authority_valid": True,
            "execution_lineage": bundle.context.execution_lineage,
            "capital_authority_id": bundle.context.authority_id,
            "authority_generation": bundle.context.authority_generation,
            "source_run_id": bundle.run_id,
            "source_input_bundle_sha256": bundle.bundle_sha256,
        },
        RunStage.LEARNING_RECORDED: {
            "recorded": True,
            "source_run_id": bundle.run_id,
            "source_input_bundle_sha256": bundle.bundle_sha256,
        },
        RunStage.REPORTED: {
            "reported": True,
            "source_run_id": bundle.run_id,
            "source_input_bundle_sha256": bundle.bundle_sha256,
        },
    }
    return deepcopy(payloads[stage])


def _bundle_chain(attack: str | None = None) -> tuple[RunBundle, ...]:
    bundle = _initial_bundle()
    chain = [bundle]
    delayed_reasons: tuple[str, ...] = ()
    for stage in STAGE_ORDER:
        component = bundle.component_for(stage)
        payload = _stage_payload(stage, bundle)
        reason_codes: tuple[str, ...] = ()
        if attack == "position_authority" and stage is RunStage.PREOPEN:
            payload["position_authority_valid"] = False
        if attack == "permitted_order_ids" and stage is RunStage.RISK_CHECKED:
            payload["approved_orders"][0]["order_id"] = "forged-order"
        if (
            attack
            in {
                "blocked_new_risk_permitted",
                "blocked_new_risk_empty",
                "blocked_reduce_permitted",
            }
            and stage is RunStage.EVIDENCE_READY
        ):
            reason_codes = ("dataset_stale",)
        if attack == "delayed_block_reasons" and stage is RunStage.EVIDENCE_READY:
            reason_codes = ("dataset_stale",)
        if attack == "blocked_reduce_permitted" and stage is RunStage.RISK_CHECKED:
            payload["approved_orders"][0]["intent"] = "reduce"
        if (
            attack
            in {
                "duplicate_open_permitted",
                "duplicate_open_empty",
            }
            and stage is RunStage.RISK_CHECKED
        ):
            payload["approved_orders"].append(deepcopy(payload["approved_orders"][0]))
            if attack == "duplicate_open_empty":
                reason_codes = (
                    "order_identity_invalid",
                    "new_risk_order_while_blocked",
                )
        if attack == "unfilled_receipt_proof" and stage is RunStage.ORDERS_SIMULATED:
            payload["order_receipts"] = [
                {
                    "order_id": "order-1",
                    "status": "rejected",
                }
            ]
            reason_codes = ("unfilled_receipt_proof_invalid",)

        idempotency_key = _idempotency_key(bundle, stage, component)
        if attack == "idempotency_key" and stage is RunStage.DECISION_READY:
            idempotency_key = "f" * 64
        receipt = StageReceipt.create(
            stage=stage,
            status=("completed_with_blocks" if reason_codes else "completed"),
            idempotency_key=idempotency_key,
            component=component,
            input_bundle_sha256=bundle.bundle_sha256,
            payload=payload,
            reason_codes=reason_codes,
        )

        stop_new_risk = bool(reason_codes)
        block_reasons = reason_codes
        position_authority_valid = None
        permitted_order_ids = None
        if attack == "delayed_block_reasons" and stage is RunStage.EVIDENCE_READY:
            delayed_reasons = reason_codes
            stop_new_risk = False
            block_reasons = ()
        if stage is RunStage.PREOPEN:
            position_authority_valid = True
        elif stage is RunStage.RISK_CHECKED:
            permitted_order_ids = (
                ()
                if attack in {"blocked_new_risk_empty", "duplicate_open_empty"}
                else ("order-1",)
            )
            if delayed_reasons:
                stop_new_risk = True
                block_reasons = delayed_reasons
                delayed_reasons = ()
        elif stage is RunStage.ORDERS_SIMULATED:
            position_authority_valid = attack == "unfilled_receipt_proof"
        elif stage is RunStage.RECONCILED:
            position_authority_valid = True

        bundle = bundle.append(
            receipt,
            stop_new_risk=stop_new_risk,
            position_authority_valid=position_authority_valid,
            block_reasons=block_reasons,
            permitted_order_ids=permitted_order_ids,
        )
        chain.append(bundle)
    return tuple(chain)


@pytest.mark.parametrize("receipt_count", [0, 1, 5, len(STAGE_ORDER)])
def test_parse_run_bundle_accepts_valid_prefixes(receipt_count: int) -> None:
    bundle = _bundle_chain()[receipt_count]

    assert parse_run_bundle(bundle.to_dict()) == bundle


@pytest.mark.parametrize(
    ("attack", "expected_error"),
    [
        ("permitted_order_ids", "run_bundle_permitted_order_ids_mismatch"),
        ("position_authority", "receipt_input_bundle_mismatch"),
        ("delayed_block_reasons", "receipt_input_bundle_mismatch"),
        ("idempotency_key", "receipt_idempotency_key_mismatch"),
    ],
)
def test_parse_run_bundle_rejects_fully_resealed_cross_stage_state_attack(
    attack: str,
    expected_error: str,
) -> None:
    valid = _bundle_chain()[-1]
    forged = _bundle_chain(attack)[-1]
    assert forged.bundle_sha256 != valid.bundle_sha256
    assert forged.stage_receipts[4].receipt_id != valid.stage_receipts[
        4
    ].receipt_id or (attack != "permitted_order_ids")
    assert (
        all(
            forged.stage_receipts[index].input_bundle_sha256
            != valid.stage_receipts[index].input_bundle_sha256
            for index in range(5, len(STAGE_ORDER))
        )
        or attack == "idempotency_key"
    )

    with pytest.raises(RunBundleError, match=expected_error):
        parse_run_bundle(forged.to_dict())


@pytest.mark.parametrize(
    "attack",
    ["blocked_new_risk_permitted", "duplicate_open_permitted"],
)
def test_parse_run_bundle_rejects_permitted_ids_not_produced_by_day_loop_reducer(
    attack: str,
) -> None:
    forged = _bundle_chain(attack)[-1]

    with pytest.raises(
        RunBundleError,
        match="run_bundle_permitted_order_ids_mismatch",
    ):
        parse_run_bundle(forged.to_dict())


@pytest.mark.parametrize(
    "scenario",
    ["blocked_new_risk_empty", "duplicate_open_empty"],
)
def test_parse_run_bundle_accepts_fail_closed_risk_reducer_output(
    scenario: str,
) -> None:
    bundle = _bundle_chain(scenario)[5]

    assert bundle.current_stage is RunStage.RISK_CHECKED
    assert bundle.permitted_order_ids == ()
    assert parse_run_bundle(bundle.to_dict()) == bundle


def test_parse_run_bundle_preserves_reduce_order_under_existing_block() -> None:
    bundle = _bundle_chain("blocked_reduce_permitted")[5]

    assert bundle.current_stage is RunStage.RISK_CHECKED
    assert bundle.stop_new_risk is True
    assert bundle.position_authority_valid is True
    assert bundle.permitted_order_ids == ("order-1",)
    assert parse_run_bundle(bundle.to_dict()) == bundle


def test_unfilled_proof_invalidates_rebuilt_position_authority() -> None:
    forged = _bundle_chain("unfilled_receipt_proof")[6]
    assert forged.current_stage is RunStage.ORDERS_SIMULATED
    assert forged.position_authority_valid is True

    with pytest.raises(
        RunBundleError,
        match="run_bundle_position_authority_mismatch",
    ):
        parse_run_bundle(forged.to_dict())


def test_parse_run_bundle_rejects_unknown_top_level_field() -> None:
    payload = _bundle_chain()[0].to_dict()
    payload["future_unreviewed_field"] = True

    with pytest.raises(RunBundleError, match="run_bundle_fields_invalid"):
        parse_run_bundle(payload)


def test_run_context_normalizes_equivalent_instants_to_shanghai_run_identity() -> None:
    canonical = _initial_bundle().context
    equivalent = RunContext(
        trade_date="2026-07-16",
        decision_as_of="2026-07-16T01:05:00Z",
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage="ashare-sim-chain-fixture-v1",
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256="c" * 64,
    )

    assert equivalent.decision_as_of == "2026-07-16T09:05:00+08:00"
    assert equivalent.run_id == canonical.run_id


@pytest.mark.parametrize(
    ("decision_as_of", "expected_error"),
    [
        ("2026-07-16T09:05:00.000001+08:00", "subsecond_not_supported"),
        ("2026-07-15T23:59:59+08:00", "trade_date_mismatch"),
    ],
)
def test_run_context_rejects_noncanonical_trading_day_instants(
    decision_as_of: str,
    expected_error: str,
) -> None:
    with pytest.raises(RunBundleError, match=expected_error):
        RunContext(
            trade_date="2026-07-16",
            decision_as_of=decision_as_of,
            market="ashare",
            authority_id="ashare-capital-v1",
            authority_generation=1,
            execution_lineage="ashare-sim-chain-fixture-v1",
            account_type="simulated",
            real_trading_enabled=False,
            champion_manifest_sha256="c" * 64,
        )
