"""Injectable, restart-safe orchestration for one A-share simulated day.

The loop is deliberately an authority composer, not a replacement authority.
Every business operation is supplied by a versioned port.  The loop freezes
their identities, passes deterministic idempotency keys, validates boundaries,
and persists only immutable orchestration receipts through an explicit store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

from shared.execution.cost_policy import (
    ASHARE_RESEARCH_COST_POLICY_V1,
    commission,
    transfer_fee,
)
from shared.execution.execution_reality import (
    ashare_sell_quantity_rejection_reason,
)
from shared.portfolio.thesis_risk import ThesisRiskRuntimeAuthority

from .execution_receipt_contract import (
    is_reconcilable_not_committed_market_failure,
)
from .run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunContext,
    RunStage,
    STAGE_ORDER,
    StageReceipt,
)


_ALL_ACTIONS = ("open", "increase", "reduce", "exit", "hold")
_EXIT_ACTIONS = ("reduce", "exit", "hold")
_NEW_RISK_ACTIONS = frozenset({"open", "increase"})
_REDUCE_ACTIONS = frozenset({"reduce", "exit"})
_ORDER_ACTIONS = _NEW_RISK_ACTIONS | _REDUCE_ACTIONS
_TERMINAL_ORDER_STATES = frozenset(
    {"filled", "partial", "rejected", "cancelled", "not_filled"}
)
_HEX = frozenset("0123456789abcdef")
_DRIFT_CONSTRAINT_FIELDS = frozenset(
    {
        "schema_version",
        "active_action_receipt_sha256",
        "risk_multiplier_cap",
        "stop_new_orders",
        "reduce_only",
        "quarantined",
        "review_required",
        "reason_codes",
    }
)
_THESIS_RISK_DIMENSIONS = frozenset(
    {
        "industry",
        "thesis",
        "raw_material",
        "policy_event",
        "crowding",
        "model_family",
    }
)
_THESIS_RISK_EFFECT_FIELDS = frozenset(
    {
        "dimension",
        "group_id",
        "pre_exposure_cny",
        "requested_delta_cny",
        "requested_post_exposure_cny",
        "delta_cny",
        "post_exposure_cny",
        "cap_cny",
        "policy_proof_sha256",
    }
)


class DayLoopError(RuntimeError):
    """Base error for fail-closed orchestration failures."""


class FrozenRuntimeMismatch(DayLoopError):
    """Raised when a restart does not use the frozen component/context set."""


class ConcurrentRunUpdate(DayLoopError):
    """Raised when an explicit bundle store compare-and-swap fails."""


class FaultPoint(str, Enum):
    BEFORE_PORT = "before_port"
    AFTER_PORT_BEFORE_PERSIST = "after_port_before_persist"
    AFTER_PERSIST = "after_persist"


@dataclass(frozen=True)
class StageRequest:
    """Read-only request passed to an injected stage authority."""

    run_id: str
    stage: RunStage
    idempotency_key: str
    input_bundle_sha256: str
    bundle: RunBundle
    allowed_actions: Tuple[str, ...]
    permitted_order_ids: Tuple[str, ...]


@dataclass(frozen=True)
class StageResult:
    """Canonical JSON result returned by a stage authority."""

    payload: Mapping[str, Any]
    _payload_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError("stage result payload must be a mapping")
        try:
            payload_json = json.dumps(
                dict(self.payload),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            copied = json.loads(payload_json)
        except (TypeError, ValueError) as exc:
            raise TypeError("stage result payload must be canonical JSON") from exc
        object.__setattr__(self, "payload", copied)
        object.__setattr__(self, "_payload_json", payload_json)


class DayStagePort(Protocol):
    identity: ComponentIdentity

    def execute(self, request: StageRequest) -> StageResult: ...


class MainboardScopePort(Protocol):
    identity: ComponentIdentity

    def order_identity_allowed(self, symbol: str) -> bool: ...


class RunBundleStore(Protocol):
    """Explicit CAS store; implementations choose their own durability."""

    def load(self, run_id: str) -> Optional[RunBundle]: ...

    def compare_and_swap(
        self,
        *,
        run_id: str,
        expected_bundle_sha256: Optional[str],
        bundle: RunBundle,
    ) -> None: ...


class MemoryRunBundleStore:
    """Process-local store for tests and isolated paper replays only."""

    def __init__(self) -> None:
        self._bundles: dict[str, RunBundle] = {}

    def load(self, run_id: str) -> Optional[RunBundle]:
        return self._bundles.get(run_id)

    def compare_and_swap(
        self,
        *,
        run_id: str,
        expected_bundle_sha256: Optional[str],
        bundle: RunBundle,
    ) -> None:
        current = self._bundles.get(run_id)
        current_sha = current.bundle_sha256 if current is not None else None
        if current_sha != expected_bundle_sha256:
            raise ConcurrentRunUpdate("run_bundle_compare_and_swap_failed")
        if bundle.run_id != run_id:
            raise ConcurrentRunUpdate("run_bundle_id_mismatch")
        self._bundles[run_id] = bundle


@dataclass(frozen=True)
class _Validation:
    reasons: Tuple[str, ...] = ()
    stop_new_risk: bool = False
    position_authority_valid: Optional[bool] = None
    permitted_order_ids: Optional[Tuple[str, ...]] = None


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _native_bool(value: object, expected: bool) -> bool:
    return type(value) is bool and value is expected


def _text(value: object) -> str:
    return value if isinstance(value, str) and value and value == value.strip() else ""


def _sha256_text(value: object) -> str:
    text = _text(value)
    if len(text) != 64 or any(character not in _HEX for character in text):
        return ""
    return text


def _finite_number(value: object, *, minimum: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    normalized = float(value)
    if not math.isfinite(normalized):
        return False
    return minimum is None or normalized >= minimum


def _aware_instant(value: object) -> datetime | None:
    normalized = _text(value)
    if not normalized:
        return None
    try:
        instant = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return instant


def _rows(value: object) -> Optional[list[Mapping[str, Any]]]:
    if not isinstance(value, list):
        return None
    if not all(isinstance(row, Mapping) for row in value):
        return None
    return list(value)


def _strings(value: object) -> Optional[Tuple[str, ...]]:
    if not isinstance(value, list):
        return None
    output: list[str] = []
    for item in value:
        normalized = _text(item)
        if not normalized or normalized in output:
            return None
        output.append(normalized)
    return tuple(output)


def _dedupe(values: Sequence[str]) -> Tuple[str, ...]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return tuple(output)


_SMALL_ACCOUNT_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "policy_id",
        "cost_policy_id",
        "capital_authority_id",
        "authority_generation",
        "account_as_of",
        "position_snapshot_receipt_id",
        "position_snapshot_sha256",
        "verification_receipt_sha256",
        "current_equity_cny",
        "risk_budget_base_cny",
        "max_positions",
        "starting_available_cash_cny",
        "starting_gross_cny",
        "target_gross_cny",
        "cash_after_orders_cny",
        "plan_decisions",
        "thesis_risk_policy_id",
        "thesis_risk_policy_sha256",
        "thesis_risk_policy_proof_sha256",
        "thesis_risk_exposure_receipt_sha256s",
        "thesis_risk_exposure_proof_sha256s",
        "thesis_risk_exposure_set_id",
        "thesis_risk_exposure_set_sha256",
        "thesis_risk_exposure_set_proof_sha256",
        "thesis_risk_runtime_authority_sha256",
        "thesis_risk_initial_group_exposures",
        "thesis_risk_final_group_exposures",
        "plan_sha256",
    }
)
_SMALL_ACCOUNT_PLAN_DECISION_FIELDS = frozenset(
    {
        "decision_id",
        "symbol",
        "action",
        "current_shares",
        "sellable_shares",
        "target_shares",
        "order_quantity",
        "valuation_price_cny",
        "reservation_price_cny",
        "estimated_order_cost_cny",
        "target_notional_cny",
        "reason_codes",
        "thesis_risk_evaluated_order_shares",
        "thesis_risk_group_effects",
    }
)


def _small_account_plan_contract(
    bundle: RunBundle,
    payload: Mapping[str, Any],
    *,
    thesis_risk_authority: ThesisRiskRuntimeAuthority,
) -> tuple[Tuple[str, ...], dict[str, Mapping[str, Any]], str, Mapping[str, Any]]:
    """Verify the 50k allocation receipt independently of the model stage."""

    reasons: list[str] = []
    if not isinstance(thesis_risk_authority, ThesisRiskRuntimeAuthority):
        return (("small_account_thesis_risk_authority_invalid",), {}, "", {})
    context_decision_time = _aware_instant(bundle.context.decision_as_of)
    if (
        context_decision_time is None
        or context_decision_time != thesis_risk_authority.decision_time
    ):
        reasons.append("small_account_thesis_risk_authority_invalid")
    raw_plan = payload.get("small_account_plan")
    if not isinstance(raw_plan, Mapping) or set(raw_plan) != _SMALL_ACCOUNT_PLAN_FIELDS:
        return (("small_account_plan_contract_invalid",), {}, "", {})
    plan = dict(raw_plan)
    plan_sha = _sha256_text(plan.get("plan_sha256"))
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    if not plan_sha or plan_sha != _canonical_sha256(unsigned):
        reasons.append("small_account_plan_digest_invalid")
    if (
        plan.get("schema_version") != "tradingagent.small_account_plan_receipt.v1"
        or plan.get("policy_id") != "ashare-small-account-50000-v1"
        or payload.get("optimizer_policy_version") != plan.get("policy_id")
    ):
        reasons.append("small_account_plan_policy_invalid")
    if plan.get("cost_policy_id") != ASHARE_RESEARCH_COST_POLICY_V1.policy_id:
        reasons.append("small_account_plan_cost_policy_invalid")
    thesis_risk_policy_proof_sha = _sha256_text(
        plan.get("thesis_risk_policy_proof_sha256")
    )
    thesis_risk_receipt_shas = _strings(
        plan.get("thesis_risk_exposure_receipt_sha256s")
    )
    thesis_risk_proof_shas = _strings(plan.get("thesis_risk_exposure_proof_sha256s"))
    expected_receipt_shas = tuple(
        row.receipt_sha256 for row in thesis_risk_authority.exposure_receipts
    )
    expected_proof_shas = tuple(
        sorted(row.proof_sha256 for row in thesis_risk_authority.exposure_proofs)
    )
    if (
        plan.get("thesis_risk_policy_id") != thesis_risk_authority.policy.policy_id
        or plan.get("thesis_risk_policy_sha256")
        != thesis_risk_authority.policy.policy_sha256
        or thesis_risk_policy_proof_sha
        != thesis_risk_authority.policy_proof.proof_sha256
        or thesis_risk_receipt_shas is None
        or thesis_risk_proof_shas is None
        or thesis_risk_receipt_shas != expected_receipt_shas
        or thesis_risk_proof_shas != expected_proof_shas
        or plan.get("thesis_risk_exposure_set_id")
        != thesis_risk_authority.exposure_set_receipt.exposure_set_id
        or plan.get("thesis_risk_exposure_set_sha256")
        != thesis_risk_authority.exposure_set_receipt.receipt_sha256
        or plan.get("thesis_risk_exposure_set_proof_sha256")
        != thesis_risk_authority.exposure_set_proof.proof_sha256
        or plan.get("thesis_risk_runtime_authority_sha256")
        != thesis_risk_authority.authority_sha256
    ):
        reasons.append("small_account_thesis_risk_proof_invalid")
    initial_group_rows = _rows(plan.get("thesis_risk_initial_group_exposures"))
    expected_initial_rows = [
        {
            "dimension": dimension,
            "group_id": group_id,
            "exposure_cny": exposure_cny,
        }
        for dimension, group_id, exposure_cny in (
            thesis_risk_authority.initial_group_exposures
        )
    ]
    if initial_group_rows != expected_initial_rows:
        reasons.append("small_account_thesis_risk_exposure_invalid")
    final_group_rows = _rows(plan.get("thesis_risk_final_group_exposures"))
    final_group_map: dict[tuple[str, str], float] = {}
    if final_group_rows is None:
        reasons.append("small_account_thesis_risk_exposure_invalid")
    else:
        for group_row in final_group_rows:
            if set(group_row) != {"dimension", "group_id", "exposure_cny"}:
                reasons.append("small_account_thesis_risk_exposure_invalid")
                continue
            dimension = _text(group_row.get("dimension"))
            group_id = _text(group_row.get("group_id"))
            key = (dimension, group_id)
            if (
                dimension not in _THESIS_RISK_DIMENSIONS
                or not group_id
                or key in final_group_map
                or not _finite_number(group_row.get("exposure_cny"), minimum=0.0)
            ):
                reasons.append("small_account_thesis_risk_exposure_invalid")
                continue
            final_group_map[key] = float(group_row["exposure_cny"])
    if (
        plan.get("capital_authority_id") != bundle.context.authority_id
        or plan.get("authority_generation") != bundle.context.authority_generation
        or not _text(plan.get("position_snapshot_receipt_id"))
        or not _sha256_text(plan.get("position_snapshot_sha256"))
        or not _sha256_text(plan.get("verification_receipt_sha256"))
    ):
        reasons.append("small_account_plan_authority_invalid")

    account_as_of = _aware_instant(plan.get("account_as_of"))
    evidence_as_of = _aware_instant(
        bundle.receipt_for(RunStage.EVIDENCE_READY).payload.get("decision_as_of")
    )
    if (
        account_as_of is None
        or evidence_as_of is None
        or account_as_of > evidence_as_of
    ):
        reasons.append("small_account_plan_time_invalid")

    numeric_fields = (
        "current_equity_cny",
        "risk_budget_base_cny",
        "starting_available_cash_cny",
        "starting_gross_cny",
        "target_gross_cny",
        "cash_after_orders_cny",
    )
    if not all(
        _finite_number(plan.get(field), minimum=0.0) for field in numeric_fields
    ):
        reasons.append("small_account_plan_economics_invalid")
        return (_dedupe(reasons), {}, plan_sha, plan)
    current_equity = float(plan["current_equity_cny"])
    risk_budget = float(plan["risk_budget_base_cny"])
    max_positions = plan.get("max_positions")
    starting_cash = float(plan["starting_available_cash_cny"])
    starting_gross = float(plan["starting_gross_cny"])
    target_gross = float(plan["target_gross_cny"])
    cash_after = float(plan["cash_after_orders_cny"])
    expected_risk_budget = min(50_000.0, current_equity)
    max_positions_valid = (
        not isinstance(max_positions, bool)
        and isinstance(max_positions, int)
        and 1 <= max_positions <= 8
    )
    if (
        not max_positions_valid
        or abs(current_equity - (starting_cash + starting_gross)) > 1e-6
        or abs(risk_budget - expected_risk_budget) > 1e-6
        or target_gross > risk_budget * 0.90 + 1e-6
    ):
        reasons.append("small_account_plan_economics_invalid")

    plan_rows = _rows(plan.get("plan_decisions"))
    decision_rows = _rows(payload.get("decisions")) or []
    if plan_rows is None or len(plan_rows) != len(decision_rows):
        reasons.append("small_account_plan_decisions_invalid")
        return (_dedupe(reasons), {}, plan_sha, plan)
    decision_map = {
        _text(row.get("decision_id")): row
        for row in decision_rows
        if _text(row.get("decision_id"))
    }
    plan_map: dict[str, Mapping[str, Any]] = {}
    computed_starting_gross = 0.0
    computed_target_gross = 0.0
    computed_cash_after = starting_cash
    positioned_symbols: set[str] = set()
    running_group_exposure: dict[tuple[str, str], float] = {
        (dimension, group_id): exposure_cny
        for dimension, group_id, exposure_cny in (
            thesis_risk_authority.initial_group_exposures
        )
    }
    authority_groups_by_symbol: dict[str, dict[str, str]] = {}
    for receipt in thesis_risk_authority.exposure_receipts:
        groups = dict(receipt.groups.items())
        existing_groups = authority_groups_by_symbol.setdefault(
            receipt.symbol,
            groups,
        )
        if existing_groups != groups:
            reasons.append("small_account_thesis_risk_group_binding_invalid")
    single_name_cap = risk_budget * 0.15
    for row in plan_rows:
        if set(row) != _SMALL_ACCOUNT_PLAN_DECISION_FIELDS:
            reasons.append("small_account_plan_decisions_invalid")
            continue
        decision_id = _text(row.get("decision_id"))
        symbol = _text(row.get("symbol"))
        action = _text(row.get("action")).lower()
        if (
            not decision_id
            or decision_id in plan_map
            or not symbol
            or action not in _ALL_ACTIONS
        ):
            reasons.append("small_account_plan_decisions_invalid")
            continue
        share_fields = (
            "current_shares",
            "sellable_shares",
            "target_shares",
            "order_quantity",
        )
        if any(
            isinstance(row.get(field), bool)
            or not isinstance(row.get(field), int)
            or int(row.get(field, -1)) < 0
            for field in share_fields
        ):
            reasons.append("small_account_plan_decisions_invalid")
            continue
        current_shares = int(row["current_shares"])
        sellable_shares = int(row["sellable_shares"])
        target_shares = int(row["target_shares"])
        order_quantity = int(row["order_quantity"])
        evaluated_order_shares = row.get("thesis_risk_evaluated_order_shares")
        if isinstance(evaluated_order_shares, bool) or not isinstance(
            evaluated_order_shares,
            int,
        ):
            reasons.append("small_account_thesis_risk_effect_invalid")
            continue
        if sellable_shares > current_shares:
            reasons.append("small_account_plan_decisions_invalid")
        if not (
            _finite_number(row.get("valuation_price_cny"), minimum=0.0000001)
            and _finite_number(row.get("reservation_price_cny"), minimum=0.0000001)
            and _finite_number(row.get("estimated_order_cost_cny"), minimum=0.0)
            and _finite_number(row.get("target_notional_cny"), minimum=0.0)
        ):
            reasons.append("small_account_plan_decisions_invalid")
            continue
        valuation_price = float(row["valuation_price_cny"])
        reservation_price = float(row["reservation_price_cny"])
        estimated_cost = float(row["estimated_order_cost_cny"])
        target_notional = float(row["target_notional_cny"])
        if abs(target_notional - target_shares * valuation_price) > 1e-6:
            reasons.append("small_account_plan_decisions_invalid")
        if target_notional > single_name_cap + 1e-6:
            reasons.append("small_account_single_name_cap_exceeded")
        if target_shares > 0:
            positioned_symbols.add(symbol)

        risk_reason_codes = _strings(row.get("reason_codes"))
        risk_effect_rows = _rows(row.get("thesis_risk_group_effects"))
        authoritative_groups = authority_groups_by_symbol.get(symbol)
        if authoritative_groups is None:
            reasons.append("small_account_thesis_risk_group_binding_invalid")
        parsed_effects: list[Mapping[str, Any]] = []
        risk_cap_rejected = bool(
            risk_reason_codes is not None and "risk_group_cap" in risk_reason_codes
        )
        if action in _NEW_RISK_ACTIONS:
            expected_evaluated_shares = order_quantity
        elif action in _REDUCE_ACTIONS:
            expected_evaluated_shares = -order_quantity
        elif risk_cap_rejected:
            expected_evaluated_shares = evaluated_order_shares
            if evaluated_order_shares <= 0 or evaluated_order_shares % 100 != 0:
                reasons.append("small_account_thesis_risk_cap_invalid")
        else:
            expected_evaluated_shares = 0
        if evaluated_order_shares != expected_evaluated_shares:
            reasons.append("small_account_thesis_risk_effect_invalid")
        expected_risk_delta = round(
            evaluated_order_shares * valuation_price,
            6,
        )
        if (
            risk_reason_codes is None
            or risk_effect_rows is None
            or len(risk_effect_rows) != len(_THESIS_RISK_DIMENSIONS)
        ):
            reasons.append("small_account_thesis_risk_effect_invalid")
        else:
            seen_dimensions: set[str] = set()
            for effect in risk_effect_rows:
                if set(effect) != _THESIS_RISK_EFFECT_FIELDS:
                    reasons.append("small_account_thesis_risk_effect_invalid")
                    continue
                dimension = _text(effect.get("dimension"))
                group_id = _text(effect.get("group_id"))
                if (
                    authoritative_groups is None
                    or authoritative_groups.get(dimension) != group_id
                ):
                    reasons.append("small_account_thesis_risk_group_binding_invalid")
                if (
                    dimension not in _THESIS_RISK_DIMENSIONS
                    or dimension in seen_dimensions
                    or not group_id
                    or _sha256_text(effect.get("policy_proof_sha256"))
                    != thesis_risk_policy_proof_sha
                ):
                    reasons.append("small_account_thesis_risk_effect_invalid")
                    continue
                nonnegative_fields = (
                    "pre_exposure_cny",
                    "requested_post_exposure_cny",
                    "post_exposure_cny",
                    "cap_cny",
                )
                if not all(
                    _finite_number(effect.get(field), minimum=0.0)
                    for field in nonnegative_fields
                ) or not all(
                    _finite_number(effect.get(field))
                    for field in ("requested_delta_cny", "delta_cny")
                ):
                    reasons.append("small_account_thesis_risk_effect_invalid")
                    continue
                pre = float(effect["pre_exposure_cny"])
                requested_delta = float(effect["requested_delta_cny"])
                requested_post = float(effect["requested_post_exposure_cny"])
                delta = float(effect["delta_cny"])
                post = float(effect["post_exposure_cny"])
                cap = float(effect["cap_cny"])
                group_key = (dimension, group_id)
                expected_pre = float(running_group_exposure.get(group_key, 0.0))
                if abs(requested_delta - expected_risk_delta) > 1e-6:
                    reasons.append("small_account_thesis_risk_notional_binding_invalid")
                if (
                    cap <= 0.0
                    or abs(cap - thesis_risk_authority.policy.cap_for(dimension)) > 1e-6
                    or abs(pre - expected_pre) > 1e-6
                    or abs(requested_delta - expected_risk_delta) > 1e-6
                    or abs(requested_post - max(0.0, pre + requested_delta)) > 1e-6
                    or abs(post - max(0.0, pre + delta)) > 1e-6
                ):
                    reasons.append("small_account_thesis_risk_effect_invalid")
                    continue
                seen_dimensions.add(dimension)
                parsed_effects.append(effect)
                running_group_exposure[group_key] = post
            if seen_dimensions != _THESIS_RISK_DIMENSIONS:
                reasons.append("small_account_thesis_risk_effect_invalid")

        if action in _NEW_RISK_ACTIONS:
            action_consistent = (
                target_shares - current_shares == order_quantity
                and order_quantity > 0
                and order_quantity % 100 == 0
            )
            computed_cash_after -= order_quantity * reservation_price + estimated_cost
        elif action in _REDUCE_ACTIONS:
            sell_quantity_rejection = ashare_sell_quantity_rejection_reason(
                current_shares=current_shares,
                sellable_shares=sellable_shares,
                requested_shares=order_quantity,
            )
            action_consistent = (
                current_shares - target_shares == order_quantity
                and 0 < order_quantity <= sellable_shares
                and sell_quantity_rejection is None
            )
            if sell_quantity_rejection is not None:
                reasons.append(sell_quantity_rejection)
            computed_cash_after += order_quantity * reservation_price - estimated_cost
        else:
            action_consistent = target_shares == current_shares and order_quantity == 0
        if not action_consistent:
            reasons.append("small_account_plan_decisions_invalid")

        if risk_cap_rejected:
            if (
                action != "hold"
                or order_quantity != 0
                or target_shares != current_shares
                or evaluated_order_shares <= 0
                or not parsed_effects
                or any(float(effect["delta_cny"]) != 0.0 for effect in parsed_effects)
                or not any(
                    float(effect["requested_delta_cny"]) > 0.0
                    and float(effect["requested_post_exposure_cny"])
                    > float(effect["cap_cny"]) + 1e-9
                    for effect in parsed_effects
                )
            ):
                reasons.append("small_account_thesis_risk_cap_invalid")
        elif action in _NEW_RISK_ACTIONS:
            if any(
                abs(float(effect["delta_cny"]) - expected_risk_delta) > 1e-6
                or float(effect["post_exposure_cny"]) > float(effect["cap_cny"]) + 1e-9
                for effect in parsed_effects
            ):
                reasons.append("small_account_thesis_risk_cap_invalid")
        elif action in _REDUCE_ACTIONS:
            if risk_cap_rejected or any(
                abs(float(effect["delta_cny"]) - expected_risk_delta) > 1e-6
                for effect in parsed_effects
            ):
                reasons.append("small_account_thesis_risk_reduction_invalid")
        elif any(
            abs(float(effect["delta_cny"])) > 1e-6
            or abs(float(effect["requested_delta_cny"])) > 1e-6
            for effect in parsed_effects
        ):
            reasons.append("small_account_thesis_risk_effect_invalid")

        expected_cost = 0.0
        if action in _ORDER_ACTIONS and order_quantity > 0:
            order_notional = order_quantity * reservation_price
            expected_cost = commission(
                order_notional,
                ASHARE_RESEARCH_COST_POLICY_V1,
            ) + transfer_fee(
                order_notional,
                ASHARE_RESEARCH_COST_POLICY_V1,
            )
            if action in _REDUCE_ACTIONS:
                expected_cost += (
                    order_notional * ASHARE_RESEARCH_COST_POLICY_V1.sell_stamp_duty_rate
                )
            expected_cost = round(expected_cost, 6)
        if abs(estimated_cost - expected_cost) > 1e-6:
            reasons.append("small_account_plan_cost_policy_invalid")

        decision = decision_map.get(decision_id)
        requested_notional = (
            order_quantity * reservation_price if action != "hold" else 0.0
        )
        if (
            decision is None
            or decision.get("symbol") != symbol
            or _text(decision.get("action")).lower() != action
            or decision.get("target_shares") != target_shares
            or not _finite_number(decision.get("requested_notional_cny"), minimum=0.0)
            or abs(
                float(decision.get("requested_notional_cny", -1.0)) - requested_notional
            )
            > 1e-6
        ):
            reasons.append("small_account_plan_decision_binding_invalid")
        elif (
            decision.get("reason_codes") != row.get("reason_codes")
            or decision.get("thesis_risk_evaluated_order_shares")
            != row.get("thesis_risk_evaluated_order_shares")
            or decision.get("thesis_risk_group_effects")
            != row.get("thesis_risk_group_effects")
        ):
            reasons.append("small_account_thesis_risk_decision_binding_invalid")
        computed_starting_gross += current_shares * valuation_price
        computed_target_gross += target_notional
        plan_map[decision_id] = row

    if set(plan_map) != set(decision_map):
        reasons.append("small_account_plan_decisions_invalid")
    if max_positions_valid and len(positioned_symbols) > max_positions:
        reasons.append("small_account_max_positions_exceeded")
    if (
        abs(computed_starting_gross - starting_gross) > 1e-6
        or abs(computed_target_gross - target_gross) > 1e-6
        or abs(computed_cash_after - cash_after) > 1e-6
        or computed_cash_after < -1e-6
    ):
        reasons.append("small_account_plan_economics_invalid")
    if set(final_group_map) != set(running_group_exposure) or any(
        abs(final_group_map[key] - exposure) > 1e-6
        for key, exposure in running_group_exposure.items()
    ):
        reasons.append("small_account_thesis_risk_exposure_invalid")
    return (_dedupe(reasons), plan_map, plan_sha, plan)


class ASharePaperDayLoop:
    """Run the frozen simulated-day graph with idempotent stage boundaries."""

    def __init__(
        self,
        *,
        ports: Mapping[RunStage, DayStagePort],
        scope_policy: MainboardScopePort,
        store: RunBundleStore,
        thesis_risk_authority: ThesisRiskRuntimeAuthority,
        environ: Optional[Mapping[str, str]] = None,
        fault_hook: Optional[Callable[[RunStage, FaultPoint], None]] = None,
    ) -> None:
        if set(ports) != set(STAGE_ORDER):
            raise ValueError("ports must provide every day-loop stage exactly once")
        if not isinstance(thesis_risk_authority, ThesisRiskRuntimeAuthority):
            raise TypeError("thesis_risk_runtime_authority_required")
        normalized: dict[RunStage, DayStagePort] = {}
        identities: list[ComponentIdentity] = []
        for stage in STAGE_ORDER:
            port = ports[stage]
            identity = getattr(port, "identity", None)
            if (
                not isinstance(identity, ComponentIdentity)
                or identity.stage is not stage
            ):
                raise ValueError(f"port identity mismatch for {stage.value}")
            normalized[stage] = port
            identities.append(identity)
        scope_identity = getattr(scope_policy, "identity", None)
        if (
            not isinstance(scope_identity, ComponentIdentity)
            or scope_identity.stage is not None
        ):
            raise ValueError("scope policy must expose a stage-neutral identity")
        identities.append(scope_identity)
        if (
            getattr(
                normalized[RunStage.DECISION_READY],
                "thesis_risk_authority",
                None,
            )
            is not thesis_risk_authority
        ):
            raise ValueError("decision_thesis_risk_authority_mismatch")
        self._ports = normalized
        self._scope_policy = scope_policy
        self._store = store
        self._environ = dict(os.environ if environ is None else environ)
        self._fault_hook = fault_hook
        self._components = tuple(identities)
        self._thesis_risk_authority = thesis_risk_authority

    def _fault(self, stage: RunStage, point: FaultPoint) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage, point)

    def _assert_sim_only(self, context: RunContext) -> None:
        if context.real_trading_enabled or context.account_type != "simulated":
            raise DayLoopError(
                "day loop requires simulated account and disabled real trading"
            )
        raw = str(self._environ.get("REAL_TRADING_ENABLED", "false")).strip().lower()
        if raw != "false":
            raise DayLoopError("day loop requires REAL_TRADING_ENABLED=false")

    @staticmethod
    def _allowed_actions(bundle: RunBundle) -> Tuple[str, ...]:
        if not bundle.position_authority_valid:
            return ("hold",) if bundle.stop_new_risk else _ALL_ACTIONS
        if bundle.stop_new_risk:
            return _EXIT_ACTIONS
        return _ALL_ACTIONS

    @staticmethod
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

    def _scope_allowed(self, symbol: object) -> bool:
        normalized = _text(symbol)
        if not normalized:
            return False
        try:
            decision = self._scope_policy.order_identity_allowed(normalized)
        except Exception:
            return False
        return type(decision) is bool and decision

    def _validate_preopen(
        self,
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        del bundle
        reasons: list[str] = []
        if payload.get("market") != "ashare":
            reasons.append("preopen_market_invalid")
        if payload.get("account_type") != "simulated" or not _native_bool(
            payload.get("real_trading_enabled"), False
        ):
            reasons.append("simulation_boundary_invalid")
        account_valid = _native_bool(payload.get("account_authority_valid"), True)
        position_valid = _native_bool(payload.get("position_authority_valid"), True)
        if not account_valid:
            reasons.append("account_authority_invalid")
        if not position_valid:
            reasons.append("position_authority_invalid")
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
            position_authority_valid=position_valid,
        )

    @staticmethod
    def _validate_evidence(
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        del bundle
        reasons: list[str] = []
        snapshot_contract_valid = all(
            (
                _text(payload.get("profile_id")),
                _text(payload.get("catalog_version")),
                _sha256_text(payload.get("snapshot_sha256")),
            )
        )
        decision_as_of = _text(payload.get("decision_as_of"))
        try:
            parsed_as_of = datetime.fromisoformat(decision_as_of.replace("Z", "+00:00"))
            snapshot_contract_valid = bool(
                snapshot_contract_valid
                and parsed_as_of.tzinfo is not None
                and parsed_as_of.utcoffset() is not None
            )
        except (TypeError, ValueError):
            snapshot_contract_valid = False
        execution_eligible = payload.get("execution_eligible")
        blocking_reasons = _strings(payload.get("blocking_reasons"))
        if type(execution_eligible) is not bool or blocking_reasons is None:
            snapshot_contract_valid = False
        if not snapshot_contract_valid:
            reasons.append("research_snapshot_contract_invalid")

        datasets = _rows(payload.get("datasets"))
        if not datasets:
            return _Validation(
                reasons=_dedupe((*reasons, "dataset_evidence_missing")),
                stop_new_risk=True,
            )
        seen: set[str] = set()
        required_accepted = True
        required_count = 0
        dataset_contract_valid = True
        for dataset in datasets:
            dataset_id = _text(dataset.get("dataset_id"))
            role = _text(dataset.get("role")).lower()
            state = _text(dataset.get("state")).lower()
            action = _text(dataset.get("evidence_action")).lower()
            receipt_id = _text(dataset.get("receipt_id"))
            weight = dataset.get("effective_weight")
            weight_valid = (
                not isinstance(weight, bool)
                and isinstance(weight, (int, float))
                and math.isfinite(float(weight))
            )
            if (
                not dataset_id
                or dataset_id in seen
                or not receipt_id
                or role not in {"required_execution", "optional_context"}
            ):
                reasons.append("dataset_evidence_contract_invalid")
                dataset_contract_valid = False
                continue
            seen.add(dataset_id)
            if role == "required_execution":
                required_count += 1

            row_count = dataset.get("row_count")
            row_pit_sha256 = _sha256_text(dataset.get("row_pit_sha256"))
            max_row_available = _text(dataset.get("max_row_available_time"))
            row_pit_valid = (
                not isinstance(row_count, bool)
                and isinstance(row_count, int)
                and row_count > 0
                and bool(row_pit_sha256)
                and bool(max_row_available)
            )
            if row_pit_valid:
                try:
                    row_available_instant = datetime.fromisoformat(
                        max_row_available.replace("Z", "+00:00")
                    )
                    row_pit_valid = bool(
                        row_available_instant.tzinfo is not None
                        and row_available_instant.utcoffset() is not None
                        and row_available_instant <= parsed_as_of
                    )
                except (TypeError, ValueError):
                    row_pit_valid = False
            if not row_pit_valid:
                reasons.append("dataset_row_pit_invalid")
                dataset_contract_valid = False
                if role == "required_execution":
                    required_accepted = False

            accepted = (
                state == "ready"
                and action == "accept"
                and weight_valid
                and float(weight) == 1.0
            )
            deweighted = (
                state == "degraded"
                and action == "deweight"
                and weight_valid
                and 0.0 < float(weight) < 1.0
            )
            rejected = action == "reject" and weight_valid and float(weight) == 0.0

            if role == "optional_context" and (accepted or deweighted or rejected):
                continue
            if role == "required_execution" and accepted:
                continue

            if role == "required_execution":
                required_accepted = False
                reasons.append("required_dataset_not_accepted")
            if state == "degraded" and action == "deweight" and not deweighted:
                reasons.append("dataset_deweight_invalid")
                continue
            if state == "stale":
                reasons.append("dataset_stale")
            elif state == "failed":
                reasons.append("dataset_failed")
            elif state == "degraded" and action == "deweight":
                # Bounded deweight is valid only for optional context.
                continue
            else:
                reasons.append("dataset_evidence_rejected")

        if required_count == 0:
            required_accepted = False
            reasons.append("required_dataset_evidence_missing")

        derived_eligible = bool(
            snapshot_contract_valid
            and dataset_contract_valid
            and required_accepted
            and required_count > 0
        )
        if type(execution_eligible) is bool:
            if execution_eligible != derived_eligible:
                reasons.append("research_snapshot_eligibility_mismatch")
            if not execution_eligible:
                reasons.append("research_snapshot_ineligible")
        if blocking_reasons is not None:
            if bool(blocking_reasons) == bool(execution_eligible):
                # Eligible snapshots cannot have blockers; ineligible ones
                # must explain why. Equal truthiness violates that invariant.
                reasons.append("research_snapshot_blocking_reason_mismatch")
            if execution_eligible is False:
                reasons.extend(blocking_reasons)
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
        )

    def _validate_universe(
        self,
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        del bundle
        reasons: list[str] = []
        for receipt_field in (
            "context_receipt_id",
            "tradable_receipt_id",
            "feasible_receipt_id",
        ):
            if not _text(payload.get(receipt_field)):
                reasons.append("universe_receipt_missing")
        context_entities = _rows(payload.get("context_entities"))
        if context_entities is None:
            reasons.append("context_universe_invalid")
        else:
            for entity in context_entities:
                if not _native_bool(
                    entity.get("context_only"), True
                ) or not _native_bool(entity.get("order_identity_allowed"), False):
                    reasons.append("context_order_identity_leak")
        tradable = _strings(payload.get("tradable_symbols"))
        feasible = _strings(payload.get("feasible_symbols"))
        if tradable is None or feasible is None:
            reasons.append("universe_symbols_invalid")
        else:
            if not set(feasible).issubset(tradable):
                reasons.append("feasible_universe_not_subset")
            if any(
                not self._scope_allowed(symbol) for symbol in (*tradable, *feasible)
            ):
                reasons.append("non_mainboard_universe_leak")
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
        )

    def _validate_decision(
        self,
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        reasons: list[str] = []
        if payload.get("champion_manifest_sha256") != (
            bundle.context.champion_manifest_sha256
        ):
            reasons.append("frozen_champion_manifest_mismatch")
        if not _text(payload.get("optimizer_policy_version")):
            reasons.append("optimizer_policy_version_missing")
        decisions = _rows(payload.get("decisions"))
        if decisions is None:
            decisions = []
            reasons.append("decision_contract_invalid")
        feasible = set(
            _strings(
                bundle.receipt_for(RunStage.UNIVERSE_READY).payload.get(
                    "feasible_symbols"
                )
            )
            or ()
        )
        seen: set[str] = set()
        for decision in decisions:
            decision_id = _text(decision.get("decision_id"))
            decision_cluster_id = _text(decision.get("decision_cluster_id"))
            symbol = _text(decision.get("symbol"))
            action = _text(decision.get("action")).lower()
            if not decision_id or decision_id in seen:
                reasons.append("decision_identity_invalid")
            else:
                seen.add(decision_id)
            if action not in _ALL_ACTIONS:
                reasons.append("decision_action_invalid")
            if not decision_cluster_id:
                reasons.append("decision_cluster_identity_invalid")
            requested_notional = decision.get("requested_notional_cny")
            if not _finite_number(requested_notional, minimum=0.0):
                reasons.append("decision_requested_notional_invalid")
            elif action == "hold" and float(requested_notional) != 0.0:
                reasons.append("hold_requested_notional_invalid")
            if not self._scope_allowed(symbol):
                reasons.append("non_mainboard_decision_leak")
            if action in _NEW_RISK_ACTIONS and symbol not in feasible:
                reasons.append("new_risk_symbol_not_feasible")
            if bundle.stop_new_risk and action in _NEW_RISK_ACTIONS:
                reasons.append("new_risk_decision_while_blocked")
            if not bundle.position_authority_valid and action in _REDUCE_ACTIONS:
                reasons.append("exit_without_position_authority")
            if decision.get("score_semantics") != (
                "uncalibrated_deterministic_rank_score"
            ):
                reasons.append("decision_score_semantics_invalid")
        plan_reasons, _, _, _ = _small_account_plan_contract(
            bundle,
            payload,
            thesis_risk_authority=self._thesis_risk_authority,
        )
        reasons.extend(plan_reasons)
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
        )

    def _validate_risk(
        self,
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        reasons: list[str] = []
        if not _text(payload.get("risk_policy_version")):
            reasons.append("risk_policy_version_missing")
        if not _text(payload.get("oms_plan_id")):
            reasons.append("oms_plan_id_missing")
        drift = payload.get("drift_constraint")
        drift_stop_new_orders = False
        if not isinstance(drift, Mapping) or set(drift) != _DRIFT_CONSTRAINT_FIELDS:
            reasons.append("drift_constraint_contract_invalid")
        else:
            drift_payload = dict(drift)
            receipt_sha = drift_payload.get("active_action_receipt_sha256")
            multiplier = drift_payload.get("risk_multiplier_cap")
            reason_codes = _strings(drift_payload.get("reason_codes"))
            booleans_valid = all(
                type(drift_payload.get(field)) is bool
                for field in (
                    "stop_new_orders",
                    "reduce_only",
                    "quarantined",
                    "review_required",
                )
            )
            receipt_valid = receipt_sha is None or bool(_sha256_text(receipt_sha))
            multiplier_valid = (
                _finite_number(multiplier, minimum=0.0) and float(multiplier) <= 1.0
            )
            semantic_valid = bool(
                booleans_valid
                and receipt_valid
                and multiplier_valid
                and reason_codes is not None
                and drift_payload.get("schema_version")
                == "tradingagent.drift_runtime_constraint.v1"
            )
            if semantic_valid:
                drift_stop_new_orders = bool(drift_payload["stop_new_orders"])
                if drift_payload["reduce_only"] and not drift_stop_new_orders:
                    semantic_valid = False
                if drift_payload["quarantined"] and not (
                    drift_payload["reduce_only"] and drift_stop_new_orders
                ):
                    semantic_valid = False
                neutral = bool(
                    float(multiplier) == 1.0
                    and not drift_stop_new_orders
                    and not drift_payload["reduce_only"]
                    and not drift_payload["quarantined"]
                    and not drift_payload["review_required"]
                    and not reason_codes
                )
                if (receipt_sha is None) != neutral:
                    semantic_valid = False
            if not semantic_valid:
                reasons.append("drift_constraint_contract_invalid")
            elif payload.get("drift_constraint_sha256") != _canonical_sha256(
                drift_payload
            ):
                reasons.append("drift_constraint_digest_invalid")
        orders = _rows(payload.get("approved_orders"))
        if orders is None:
            orders = []
            reasons.append("approved_orders_invalid")
        decisions = (
            _rows(bundle.receipt_for(RunStage.DECISION_READY).payload.get("decisions"))
            or []
        )
        decision_payload = bundle.receipt_for(RunStage.DECISION_READY).payload
        plan_reasons, plan_map, plan_sha, plan = _small_account_plan_contract(
            bundle,
            decision_payload,
            thesis_risk_authority=self._thesis_risk_authority,
        )
        reasons.extend(plan_reasons)
        if not plan_sha or payload.get("small_account_plan_sha256") != plan_sha:
            reasons.append("risk_small_account_plan_binding_invalid")
        risk_cash = (
            float(plan.get("starting_available_cash_cny"))
            if _finite_number(plan.get("starting_available_cash_cny"), minimum=0.0)
            else None
        )
        decision_map = {
            _text(decision.get("decision_id")): decision
            for decision in decisions
            if _text(decision.get("decision_id"))
        }
        rejected_decisions = _rows(payload.get("rejected_decisions"))
        if rejected_decisions is None:
            rejected_decisions = []
            reasons.append("rejected_decisions_invalid")
        rejected_ids: set[str] = set()
        for rejection in rejected_decisions:
            decision_id = _text(rejection.get("decision_id"))
            reason = _text(rejection.get("reason"))
            if (
                not decision_id
                or decision_id in rejected_ids
                or decision_id not in decision_map
                or not reason
            ):
                reasons.append("rejected_decision_contract_invalid")
                continue
            if _text(decision_map[decision_id].get("action")).lower() == "hold":
                reasons.append("hold_decision_cannot_be_rejected")
                continue
            rejected_ids.add(decision_id)
        safe_orders: list[tuple[str, str]] = []
        seen: set[str] = set()
        approved_decision_ids: set[str] = set()
        for order in orders:
            order_id = _text(order.get("order_id"))
            decision_id = _text(order.get("decision_id"))
            symbol = _text(order.get("symbol"))
            intent = _text(order.get("intent")).lower()
            quantity = order.get("quantity")
            order_valid = True
            if not order_id or order_id in seen:
                reasons.append("order_identity_invalid")
                order_valid = False
            else:
                seen.add(order_id)
            if not decision_id or decision_id in approved_decision_ids:
                reasons.append("approved_decision_identity_invalid")
                order_valid = False
            else:
                approved_decision_ids.add(decision_id)
            decision = decision_map.get(decision_id)
            if decision is None:
                reasons.append("order_without_decision")
                order_valid = False
            elif symbol != decision.get("symbol") or intent != decision.get("action"):
                reasons.append("order_decision_mismatch")
                order_valid = False
            plan_decision = plan_map.get(decision_id)
            if (
                plan_decision is None
                or order.get("small_account_plan_sha256") != plan_sha
                or symbol != plan_decision.get("symbol")
                or intent != plan_decision.get("action")
                or quantity != plan_decision.get("order_quantity")
            ):
                reasons.append("order_small_account_plan_mismatch")
                order_valid = False
            if not self._scope_allowed(symbol):
                reasons.append("non_mainboard_order_leak")
                order_valid = False
            if intent not in _ORDER_ACTIONS:
                reasons.append("order_intent_invalid")
                order_valid = False
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
            ):
                reasons.append("order_quantity_invalid")
                order_valid = False
            elif intent in _NEW_RISK_ACTIONS and quantity % 100 != 0:
                reasons.append("order_board_lot_invalid")
                order_valid = False

            authority_valid = bool(
                order.get("capital_authority_id") == bundle.context.authority_id
                and order.get("authority_generation")
                == bundle.context.authority_generation
                and order.get("execution_lineage") == bundle.context.execution_lineage
                and _text(order.get("risk_receipt_id"))
                and _text(order.get("position_authority_receipt_id"))
                and _text(order.get("cash_authority_receipt_id"))
            )
            if not authority_valid:
                reasons.append("order_authority_proof_invalid")
                order_valid = False
            elif plan and order.get("position_authority_receipt_id") != plan.get(
                "position_snapshot_receipt_id"
            ):
                reasons.append("order_small_account_plan_mismatch")
                order_valid = False

            reservation_price = order.get("reservation_price_cny")
            expected_fee = order.get("expected_fee_cny")
            available_cash = order.get("available_cash_before_cny")
            if not (
                _finite_number(reservation_price, minimum=0.0000001)
                and _finite_number(expected_fee, minimum=0.0)
                and _finite_number(available_cash, minimum=0.0)
            ):
                reasons.append("order_reservation_economics_invalid")
                order_valid = False
            elif plan_decision is not None and (
                abs(
                    float(reservation_price)
                    - float(plan_decision.get("reservation_price_cny", -1.0))
                )
                > 1e-6
                or abs(
                    float(expected_fee)
                    - float(plan_decision.get("estimated_order_cost_cny", -1.0))
                )
                > 1e-6
            ):
                reasons.append("order_small_account_plan_mismatch")
                order_valid = False
            elif (
                risk_cash is not None and abs(float(available_cash) - risk_cash) > 1e-6
            ):
                reasons.append("order_cash_sequence_invalid")
                order_valid = False
            elif (
                intent in _NEW_RISK_ACTIONS
                and isinstance(quantity, int)
                and float(available_cash)
                < quantity * float(reservation_price) + float(expected_fee)
            ):
                reasons.append("order_cash_reservation_invalid")
                order_valid = False

            if (
                risk_cash is not None
                and plan_decision is not None
                and isinstance(quantity, int)
                and _finite_number(reservation_price, minimum=0.0000001)
                and _finite_number(expected_fee, minimum=0.0)
            ):
                order_value = quantity * float(reservation_price)
                if intent in _NEW_RISK_ACTIONS:
                    risk_cash -= order_value + float(expected_fee)
                elif intent in _REDUCE_ACTIONS:
                    risk_cash += order_value - float(expected_fee)

            if not all(
                (
                    _native_bool(order.get("session_policy_verified"), True),
                    _native_bool(order.get("not_suspended"), True),
                    _native_bool(order.get("limit_fillable"), True),
                )
            ):
                reasons.append("order_session_policy_invalid")
                order_valid = False

            if intent in _REDUCE_ACTIONS:
                sellable = order.get("sellable_quantity")
                if not _native_bool(order.get("t_plus_one_eligible"), True):
                    reasons.append("sell_t_plus_one_ineligible")
                    order_valid = False
                if (
                    isinstance(sellable, bool)
                    or not isinstance(sellable, int)
                    or not isinstance(quantity, int)
                    or sellable < quantity
                ):
                    reasons.append("sellable_quantity_insufficient")
                    order_valid = False
                if plan_decision is not None:
                    sell_quantity_rejection = ashare_sell_quantity_rejection_reason(
                        current_shares=plan_decision.get("current_shares"),
                        sellable_shares=plan_decision.get("sellable_shares"),
                        requested_shares=quantity,
                    )
                    if sell_quantity_rejection is not None:
                        reasons.append(sell_quantity_rejection)
                        order_valid = False
            if order_valid:
                safe_orders.append((order_id, intent))
        if approved_decision_ids & rejected_ids:
            reasons.append("decision_disposition_conflict")
        for decision_id, decision in decision_map.items():
            action = _text(decision.get("action")).lower()
            if action == "hold":
                if decision_id in approved_decision_ids:
                    reasons.append("hold_decision_cannot_create_order")
                continue
            if decision_id not in approved_decision_ids | rejected_ids:
                reasons.append("decision_disposition_missing")
        block_new_risk = bundle.stop_new_risk or bool(reasons) or drift_stop_new_orders
        permitted: list[str] = []
        for order_id, intent in safe_orders:
            if intent in _NEW_RISK_ACTIONS and block_new_risk:
                if "new_risk_order_while_blocked" not in reasons:
                    reasons.append("new_risk_order_while_blocked")
                continue
            if intent in _REDUCE_ACTIONS and not bundle.position_authority_valid:
                if "exit_without_position_authority" not in reasons:
                    reasons.append("exit_without_position_authority")
                continue
            permitted.append(order_id)
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons) or drift_stop_new_orders,
            permitted_order_ids=tuple(permitted),
        )

    def _validate_execution(
        self,
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        reasons: list[str] = []
        if payload.get("execution_lineage") != bundle.context.execution_lineage:
            reasons.append("execution_lineage_mismatch")
        if payload.get("account_type") != "simulated" or not _native_bool(
            payload.get("real_trading_enabled"), False
        ):
            reasons.append("simulation_boundary_invalid")
        receipts = _rows(payload.get("order_receipts"))
        if receipts is None:
            receipts = []
            reasons.append("order_receipts_invalid")
        explicit_unknown = _strings(payload.get("unknown_order_ids"))
        if explicit_unknown is None:
            explicit_unknown = ()
            reasons.append("unknown_order_contract_invalid")
        permitted = set(bundle.permitted_order_ids)
        risk_orders = (
            _rows(
                bundle.receipt_for(RunStage.RISK_CHECKED).payload.get("approved_orders")
            )
            or []
        )
        risk_order_map = {
            _text(order.get("order_id")): order
            for order in risk_orders
            if _text(order.get("order_id"))
        }
        seen: set[str] = set()
        position_valid = bundle.position_authority_valid
        decision_instant = _aware_instant(
            bundle.receipt_for(RunStage.EVIDENCE_READY).payload.get("decision_as_of")
        )
        for receipt in receipts:
            order_id = _text(receipt.get("order_id"))
            symbol = _text(receipt.get("symbol"))
            intent = _text(receipt.get("intent")).lower()
            status = _text(receipt.get("status")).lower()
            if not order_id or order_id in seen:
                reasons.append("execution_receipt_identity_invalid")
            else:
                seen.add(order_id)
            if order_id not in permitted:
                reasons.append("unknown_simulated_order")
                position_valid = False
            risk_order = risk_order_map.get(order_id)
            if risk_order is None:
                reasons.append("execution_without_risk_order")
                position_valid = False
            elif symbol != risk_order.get("symbol") or intent != risk_order.get(
                "intent"
            ):
                reasons.append("execution_risk_order_mismatch")
                position_valid = False
            if not self._scope_allowed(symbol):
                reasons.append("non_mainboard_execution_leak")
                position_valid = False
            if intent not in _ORDER_ACTIONS or status not in _TERMINAL_ORDER_STATES:
                reasons.append("execution_receipt_state_invalid")
                position_valid = False

            authority_valid = bool(
                receipt.get("capital_authority_id") == bundle.context.authority_id
                and receipt.get("authority_generation")
                == bundle.context.authority_generation
                and receipt.get("execution_lineage") == bundle.context.execution_lineage
            )
            if not authority_valid:
                reasons.append("execution_authority_proof_invalid")
                position_valid = False

            requested = receipt.get("requested_quantity")
            filled = receipt.get("filled_quantity")
            residual = receipt.get("residual_quantity")
            quantities_valid = all(
                not isinstance(value, bool) and isinstance(value, int) and value >= 0
                for value in (requested, filled, residual)
            )
            if (
                not quantities_valid
                or requested != filled + residual
                or risk_order is None
                or requested != risk_order.get("quantity")
            ):
                reasons.append("fill_quantity_conservation_invalid")
                position_valid = False

            status_valid = bool(
                quantities_valid
                and (
                    (status == "filled" and filled == requested and residual == 0)
                    or (
                        status == "partial"
                        and isinstance(filled, int)
                        and isinstance(requested, int)
                        and isinstance(residual, int)
                        and 0 < filled < requested
                        and residual > 0
                    )
                    or (
                        status in {"rejected", "cancelled", "not_filled"}
                        and filled == 0
                        and residual == requested
                    )
                )
            )
            if not status_valid:
                reasons.append("execution_receipt_state_invalid")
                position_valid = False

            terminal_at = _aware_instant(receipt.get("terminal_at"))
            if terminal_at is None:
                reasons.append("execution_receipt_time_invalid")
                position_valid = False
            elif decision_instant is None or terminal_at < decision_instant:
                reasons.append("execution_time_precedes_decision")
                position_valid = False

            if status in {"filled", "partial"}:
                fingerprint_payload = dict(receipt)
                claimed_fill_fingerprint = _sha256_text(
                    fingerprint_payload.pop("fill_fingerprint", None)
                )
                expected_fill_fingerprint = _canonical_sha256(fingerprint_payload)
                filled_at = _aware_instant(receipt.get("filled_at"))
                economics_valid = bool(
                    _finite_number(receipt.get("filled_price_cny"), minimum=0.0000001)
                    and _finite_number(receipt.get("fee_cny"), minimum=0.0)
                    and _finite_number(receipt.get("slippage_cny"), minimum=0.0)
                    and filled_at is not None
                    and _text(receipt.get("execution_receipt_id"))
                    and _text(receipt.get("market_evidence_receipt_id"))
                    and _text(receipt.get("capital_commit_receipt_id"))
                    and receipt.get("capital_commit_status") == "committed"
                    and claimed_fill_fingerprint
                )
                if not economics_valid:
                    reasons.append("filled_receipt_economics_invalid")
                elif decision_instant is None or filled_at < decision_instant:
                    reasons.append("execution_time_precedes_decision")
                elif terminal_at is None or terminal_at < filled_at:
                    reasons.append("execution_time_order_invalid")
                elif not hmac.compare_digest(
                    claimed_fill_fingerprint,
                    expected_fill_fingerprint,
                ):
                    reasons.append("fill_fingerprint_content_mismatch")
                position_valid = False
            else:
                commit_status = receipt.get("capital_commit_status")
                not_applicable_valid = bool(
                    commit_status == "not_applicable"
                    and receipt.get("capital_commit_receipt_id") is None
                    and receipt.get("simulated_fill_id") is None
                    and receipt.get("filled_at") is None
                    and receipt.get("fill_fingerprint") is None
                )
                not_committed_valid = (
                    commit_status == "not_committed"
                    and is_reconcilable_not_committed_market_failure(
                        receipt,
                        expected_trade_date=bundle.context.trade_date,
                    )
                )
                if not (
                    _text(receipt.get("execution_receipt_id"))
                    and (not_applicable_valid or not_committed_valid)
                ):
                    reasons.append("unfilled_receipt_proof_invalid")
                    position_valid = False
        if explicit_unknown:
            reasons.append("unknown_simulated_order")
            position_valid = False
        if permitted - seen:
            reasons.append("order_receipt_missing")
            position_valid = False
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
            position_authority_valid=position_valid,
        )

    @staticmethod
    def _validate_reconcile(
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        reasons: list[str] = []
        account_valid = _native_bool(payload.get("account_authority_valid"), True)
        position_valid = _native_bool(payload.get("position_authority_valid"), True)
        unknown = _strings(payload.get("unknown_order_ids"))
        unreconciled = _strings(payload.get("unreconciled_order_ids"))
        if payload.get("status") != "reconciled" or not account_valid:
            reasons.append("account_unreconciled")
        if not position_valid:
            reasons.append("position_authority_invalid")
        if payload.get("execution_lineage") != bundle.context.execution_lineage:
            reasons.append("execution_lineage_mismatch")
        if (
            payload.get("capital_authority_id") != bundle.context.authority_id
            or payload.get("authority_generation")
            != bundle.context.authority_generation
        ):
            reasons.append("reconcile_authority_proof_invalid")
        if (
            payload.get("source_run_id") != bundle.run_id
            or payload.get("source_input_bundle_sha256") != bundle.bundle_sha256
        ):
            reasons.append("reconcile_run_binding_invalid")
        reconciled_at = _aware_instant(payload.get("reconciled_at"))
        if (
            reconciled_at is None
            or not _text(payload.get("reconciliation_receipt_id"))
            or not _sha256_text(payload.get("capital_ledger_head_sha256"))
            or not _sha256_text(payload.get("position_fingerprint"))
        ):
            reasons.append("reconcile_authority_proof_invalid")
        execution_receipts = bundle.receipt_for(RunStage.ORDERS_SIMULATED).payload.get(
            "order_receipts"
        )
        terminal_instants = tuple(
            instant
            for receipt in (_rows(execution_receipts) or [])
            if (instant := _aware_instant(receipt.get("terminal_at"))) is not None
        )
        if (
            reconciled_at is not None
            and terminal_instants
            and reconciled_at < max(terminal_instants)
        ):
            reasons.append("reconcile_precedes_execution_terminal")
        if not _sha256_text(payload.get("order_receipts_sha256")) or payload.get(
            "order_receipts_sha256"
        ) != _canonical_sha256(execution_receipts):
            reasons.append("reconcile_order_receipt_binding_invalid")
        if not (
            _finite_number(payload.get("account_equity_cny"), minimum=0.0)
            and _finite_number(payload.get("cash_cny"), minimum=0.0)
        ):
            reasons.append("reconcile_account_economics_invalid")
        if unknown is None or unreconciled is None:
            reasons.append("reconcile_order_contract_invalid")
        elif unknown or unreconciled:
            reasons.append("account_unreconciled")
        reconciled_position = not reasons and position_valid
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
            position_authority_valid=reconciled_position,
        )

    @staticmethod
    def _validate_learning(
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        reasons: list[str] = []
        if not _native_bool(payload.get("recorded"), True):
            reasons.append("learning_record_not_committed")
        if not _text(payload.get("record_id")):
            reasons.append("learning_record_id_missing")
        if payload.get("journal_authority") != "SampleJournal":
            reasons.append("learning_authority_invalid")
        if payload.get("source_run_id") != bundle.run_id:
            reasons.append("learning_run_binding_invalid")
        if payload.get("source_input_bundle_sha256") != bundle.bundle_sha256:
            reasons.append("learning_input_bundle_binding_invalid")
        if not _native_bool(payload.get("authority_readback_verified"), True):
            reasons.append("learning_authority_readback_invalid")
        event_ids = _strings(payload.get("journal_event_ids"))
        event_ids_sha = _sha256_text(payload.get("journal_event_ids_sha256"))
        if (
            event_ids is None
            or not event_ids
            or len(set(event_ids)) != len(event_ids)
            or payload.get("record_id") not in event_ids
            or not event_ids_sha
            or event_ids_sha != _canonical_sha256(list(event_ids))
        ):
            reasons.append("learning_event_receipt_invalid")
        head_count = payload.get("journal_head_event_count")
        if (
            isinstance(head_count, bool)
            or not isinstance(head_count, int)
            or head_count < len(event_ids or ())
            or not _sha256_text(payload.get("journal_head_sha256"))
            or not _sha256_text(payload.get("journal_source_sha256"))
        ):
            reasons.append("learning_authority_head_invalid")
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
        )

    @staticmethod
    def _validate_report(
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        reasons: list[str] = []
        if not _native_bool(payload.get("reported"), True):
            reasons.append("report_not_committed")
        if not _text(payload.get("report_id")):
            reasons.append("report_id_missing")
        if payload.get("source_run_id") != bundle.run_id:
            reasons.append("report_run_binding_invalid")
        if payload.get("source_input_bundle_sha256") != bundle.bundle_sha256:
            reasons.append("report_input_bundle_binding_invalid")
        if (
            payload.get("projection_type") != "today_run_local_candidate"
            or not _native_bool(payload.get("local_candidate"), True)
            or not _native_bool(payload.get("production_verified"), False)
        ):
            reasons.append("report_candidate_boundary_invalid")
        artifact_sha = _sha256_text(payload.get("artifact_sha256"))
        readback_sha = _sha256_text(payload.get("readback_sha256"))
        if not artifact_sha or not readback_sha or artifact_sha != readback_sha:
            reasons.append("report_artifact_readback_mismatch")
        return _Validation(
            reasons=_dedupe(reasons),
            stop_new_risk=bool(reasons),
        )

    def _validate(
        self,
        stage: RunStage,
        bundle: RunBundle,
        payload: Mapping[str, Any],
    ) -> _Validation:
        validators = {
            RunStage.PREOPEN: self._validate_preopen,
            RunStage.EVIDENCE_READY: self._validate_evidence,
            RunStage.UNIVERSE_READY: self._validate_universe,
            RunStage.DECISION_READY: self._validate_decision,
            RunStage.RISK_CHECKED: self._validate_risk,
            RunStage.ORDERS_SIMULATED: self._validate_execution,
            RunStage.RECONCILED: self._validate_reconcile,
            RunStage.LEARNING_RECORDED: self._validate_learning,
            RunStage.REPORTED: self._validate_report,
        }
        return validators[stage](bundle, payload)

    def _load_or_create(
        self,
        context: RunContext,
    ) -> tuple[RunBundle, Optional[str]]:
        if not isinstance(context, RunContext):
            raise TypeError("context must be a RunContext")
        self._assert_sim_only(context)
        bundle = self._store.load(context.run_id)
        if bundle is None:
            return RunBundle.create(context, self._components), None
        if bundle.context != context:
            raise FrozenRuntimeMismatch("run_context_changed_during_restart")
        if bundle.components != self._components:
            raise FrozenRuntimeMismatch("component_manifest_changed_during_restart")
        return bundle, bundle.bundle_sha256

    def _advance_one(
        self,
        bundle: RunBundle,
        *,
        persisted_sha: Optional[str],
    ) -> RunBundle:
        stage = bundle.next_stage
        if stage is None:
            return bundle
        component = bundle.component_for(stage)
        idempotency_key = self._idempotency_key(bundle, stage, component)
        request = StageRequest(
            run_id=bundle.run_id,
            stage=stage,
            idempotency_key=idempotency_key,
            input_bundle_sha256=bundle.bundle_sha256,
            bundle=bundle,
            allowed_actions=self._allowed_actions(bundle),
            permitted_order_ids=(
                bundle.permitted_order_ids if stage is RunStage.ORDERS_SIMULATED else ()
            ),
        )
        self._fault(stage, FaultPoint.BEFORE_PORT)
        result = self._ports[stage].execute(request)
        if not isinstance(result, StageResult):
            raise DayLoopError(f"{stage.value} returned an invalid StageResult")
        validation = self._validate(stage, bundle, result.payload)
        receipt = StageReceipt.create(
            stage=stage,
            status=("completed_with_blocks" if validation.reasons else "completed"),
            idempotency_key=idempotency_key,
            component=component,
            input_bundle_sha256=bundle.bundle_sha256,
            payload=result.payload,
            reason_codes=validation.reasons,
        )
        next_bundle = bundle.append(
            receipt,
            stop_new_risk=validation.stop_new_risk,
            position_authority_valid=validation.position_authority_valid,
            block_reasons=validation.reasons,
            permitted_order_ids=validation.permitted_order_ids,
        )
        self._fault(stage, FaultPoint.AFTER_PORT_BEFORE_PERSIST)
        self._store.compare_and_swap(
            run_id=bundle.run_id,
            expected_bundle_sha256=persisted_sha,
            bundle=next_bundle,
        )
        self._fault(stage, FaultPoint.AFTER_PERSIST)
        return next_bundle

    def run_next(
        self,
        context: RunContext,
        *,
        expected_stage: RunStage | None = None,
    ) -> RunBundle:
        """Advance one durable stage for an external session scheduler."""

        if expected_stage is not None and not isinstance(expected_stage, RunStage):
            raise TypeError("expected_stage must be a RunStage")
        bundle, persisted_sha = self._load_or_create(context)
        if expected_stage is not None and bundle.next_stage is not expected_stage:
            raise FrozenRuntimeMismatch("next_stage_mismatch")
        return self._advance_one(bundle, persisted_sha=persisted_sha)

    def run_until(
        self,
        context: RunContext,
        *,
        through_stage: RunStage,
    ) -> RunBundle:
        """Advance only through the requested ordered session boundary."""

        if not isinstance(through_stage, RunStage):
            raise TypeError("through_stage must be a RunStage")
        target_index = STAGE_ORDER.index(through_stage)
        bundle, persisted_sha = self._load_or_create(context)
        while bundle.next_stage is not None:
            if STAGE_ORDER.index(bundle.next_stage) > target_index:
                break
            bundle = self._advance_one(bundle, persisted_sha=persisted_sha)
            persisted_sha = bundle.bundle_sha256
        return bundle

    def run(self, context: RunContext) -> RunBundle:
        return self.run_until(context, through_stage=RunStage.REPORTED)


__all__ = [
    "ASharePaperDayLoop",
    "ConcurrentRunUpdate",
    "DayLoopError",
    "DayStagePort",
    "FaultPoint",
    "FrozenRuntimeMismatch",
    "MainboardScopePort",
    "MemoryRunBundleStore",
    "RunBundleStore",
    "StageRequest",
    "StageResult",
]
