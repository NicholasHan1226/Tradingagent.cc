"""Immutable receipts for one automatic A-share simulated trading day.

This module stores orchestration evidence only.  Capital, positions, orders,
fills, reconciliation and learning remain owned by their injected components.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo


DAY_LOOP_CONTRACT_ID = "tradingagent.paper_day_loop.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_TEXT_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_RECEIPT_STATUSES = frozenset({"completed", "completed_with_blocks"})
_EXECUTION_POSITION_INVALIDATING_REASONS = frozenset(
    {
        "execution_authority_proof_invalid",
        "execution_receipt_state_invalid",
        "execution_receipt_time_invalid",
        "execution_risk_order_mismatch",
        "execution_time_precedes_decision",
        "execution_without_risk_order",
        "fill_quantity_conservation_invalid",
        "non_mainboard_execution_leak",
        "order_receipt_missing",
        "unfilled_receipt_proof_invalid",
        "unknown_simulated_order",
    }
)
_NEW_RISK_INTENTS = frozenset({"open", "increase"})
_REDUCE_RISK_INTENTS = frozenset({"reduce", "exit"})
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class RunBundleError(ValueError):
    """Raised when orchestration evidence violates the frozen contract."""


def _exact_mapping(
    value: object,
    *,
    field_name: str,
    keys: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RunBundleError(f"{field_name}_fields_invalid")
    return value


class RunStage(str, Enum):
    PREOPEN = "preopen"
    EVIDENCE_READY = "evidence_ready"
    UNIVERSE_READY = "universe_ready"
    DECISION_READY = "decision_ready"
    RISK_CHECKED = "risk_checked"
    ORDERS_SIMULATED = "orders_simulated"
    RECONCILED = "reconciled"
    LEARNING_RECORDED = "learning_recorded"
    REPORTED = "reported"


STAGE_ORDER: Tuple[RunStage, ...] = (
    RunStage.PREOPEN,
    RunStage.EVIDENCE_READY,
    RunStage.UNIVERSE_READY,
    RunStage.DECISION_READY,
    RunStage.RISK_CHECKED,
    RunStage.ORDERS_SIMULATED,
    RunStage.RECONCILED,
    RunStage.LEARNING_RECORDED,
    RunStage.REPORTED,
)


def _canonical_json(value: Any, *, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise RunBundleError(f"{field_name}_must_be_canonical_json") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_text(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not _RUN_TEXT_RE.fullmatch(value)
    ):
        raise RunBundleError(f"{field_name}_invalid")
    return value


def _strict_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RunBundleError(f"{field_name}_invalid")
    return value


def _unique_texts(values: Tuple[str, ...], *, field_name: str) -> Tuple[str, ...]:
    normalized = tuple(_strict_text(item, field_name=field_name) for item in values)
    if len(normalized) != len(set(normalized)):
        raise RunBundleError(f"{field_name}_must_be_unique")
    return normalized


def _canonical_decision_as_of(
    value: object,
    *,
    trade_date: date,
) -> str:
    if value is None:
        parsed = datetime.combine(trade_date, time.min, tzinfo=_SHANGHAI)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RunBundleError("decision_as_of_invalid") from exc
    else:
        raise RunBundleError("decision_as_of_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RunBundleError("decision_as_of_timezone_required")
    if parsed.microsecond:
        raise RunBundleError("decision_as_of_subsecond_not_supported")
    normalized = parsed.astimezone(_SHANGHAI)
    if normalized.date() != trade_date:
        raise RunBundleError("decision_as_of_trade_date_mismatch")
    return normalized.isoformat(timespec="seconds")


@dataclass(frozen=True)
class ComponentIdentity:
    """Frozen code/config identity supplied by an injected authority."""

    stage: Optional[RunStage]
    component_id: str
    version: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        if self.stage is not None and not isinstance(self.stage, RunStage):
            raise RunBundleError("component_stage_invalid")
        _strict_text(self.component_id, field_name="component_id")
        _strict_text(self.version, field_name="component_version")
        _strict_sha256(self.artifact_sha256, field_name="component_artifact_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value if self.stage is not None else None,
            "component_id": self.component_id,
            "version": self.version,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True)
class RunContext:
    """Stable business identity and simulation boundary for one trading day."""

    trade_date: str
    market: str
    authority_id: str
    authority_generation: int
    execution_lineage: str
    account_type: str
    real_trading_enabled: bool
    champion_manifest_sha256: str
    decision_as_of: str | datetime | None = None

    def __post_init__(self) -> None:
        try:
            parsed = date.fromisoformat(self.trade_date)
        except (TypeError, ValueError) as exc:
            raise RunBundleError("trade_date_invalid") from exc
        if parsed.isoformat() != self.trade_date:
            raise RunBundleError("trade_date_invalid")
        object.__setattr__(
            self,
            "decision_as_of",
            _canonical_decision_as_of(
                self.decision_as_of,
                trade_date=parsed,
            ),
        )
        if self.market != "ashare":
            raise RunBundleError("market_must_be_ashare")
        _strict_text(self.authority_id, field_name="authority_id")
        if (
            isinstance(self.authority_generation, bool)
            or not isinstance(self.authority_generation, int)
            or self.authority_generation <= 0
        ):
            raise RunBundleError("authority_generation_invalid")
        _strict_text(self.execution_lineage, field_name="execution_lineage")
        if self.account_type != "simulated":
            raise RunBundleError("account_type_must_be_simulated")
        if type(self.real_trading_enabled) is not bool:
            raise RunBundleError("real_trading_enabled_must_be_bool")
        if self.real_trading_enabled:
            raise RunBundleError("real_trading_must_be_disabled")
        _strict_sha256(
            self.champion_manifest_sha256,
            field_name="champion_manifest_sha256",
        )

    @property
    def run_id(self) -> str:
        identity = {
            "contract_id": DAY_LOOP_CONTRACT_ID,
            "trade_date": self.trade_date,
            "decision_as_of": self.decision_as_of,
            "market": self.market,
            "authority_id": self.authority_id,
            "authority_generation": self.authority_generation,
            "execution_lineage": self.execution_lineage,
            "account_type": self.account_type,
            "real_trading_enabled": False,
        }
        return f"ashare-paper-day-{_sha256(_canonical_json(identity, field_name='run_identity'))[:32]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "decision_as_of": self.decision_as_of,
            "market": self.market,
            "authority_id": self.authority_id,
            "authority_generation": self.authority_generation,
            "execution_lineage": self.execution_lineage,
            "account_type": self.account_type,
            "real_trading_enabled": False,
            "champion_manifest_sha256": self.champion_manifest_sha256,
        }


@dataclass(frozen=True)
class StageReceipt:
    """Deterministic, immutable result for one completed stage attempt."""

    stage: RunStage
    status: str
    idempotency_key: str
    component: ComponentIdentity
    input_bundle_sha256: str
    payload_json: str
    payload_sha256: str
    reason_codes: Tuple[str, ...]
    receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, RunStage):
            raise RunBundleError("receipt_stage_invalid")
        if self.status not in _RECEIPT_STATUSES:
            raise RunBundleError("receipt_status_invalid")
        _strict_sha256(self.idempotency_key, field_name="idempotency_key")
        if self.component.stage is not self.stage:
            raise RunBundleError("receipt_component_stage_mismatch")
        _strict_sha256(
            self.input_bundle_sha256,
            field_name="input_bundle_sha256",
        )
        if not isinstance(self.payload_json, str):
            raise RunBundleError("payload_json_invalid")
        try:
            payload = json.loads(self.payload_json)
        except (TypeError, ValueError) as exc:
            raise RunBundleError("payload_json_invalid") from exc
        if not isinstance(payload, dict):
            raise RunBundleError("payload_must_be_object")
        canonical = _canonical_json(payload, field_name="payload")
        if canonical != self.payload_json:
            raise RunBundleError("payload_json_not_canonical")
        _strict_sha256(self.payload_sha256, field_name="payload_sha256")
        if self.payload_sha256 != _sha256(self.payload_json):
            raise RunBundleError("payload_sha256_mismatch")
        _unique_texts(self.reason_codes, field_name="reason_code")
        _strict_sha256(self.receipt_id, field_name="receipt_id")
        identity = {
            "stage": self.stage.value,
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "component": self.component.to_dict(),
            "input_bundle_sha256": self.input_bundle_sha256,
            "payload_sha256": self.payload_sha256,
            "reason_codes": list(self.reason_codes),
        }
        if self.receipt_id != _sha256(
            _canonical_json(identity, field_name="receipt_identity")
        ):
            raise RunBundleError("receipt_id_mismatch")

    @classmethod
    def create(
        cls,
        *,
        stage: RunStage,
        status: str,
        idempotency_key: str,
        component: ComponentIdentity,
        input_bundle_sha256: str,
        payload: Mapping[str, Any],
        reason_codes: Tuple[str, ...],
    ) -> "StageReceipt":
        if not isinstance(payload, Mapping):
            raise RunBundleError("payload_must_be_mapping")
        payload_json = _canonical_json(dict(payload), field_name="payload")
        payload_sha256 = _sha256(payload_json)
        reasons = _unique_texts(tuple(reason_codes), field_name="reason_code")
        identity = {
            "stage": stage.value,
            "status": status,
            "idempotency_key": idempotency_key,
            "component": component.to_dict(),
            "input_bundle_sha256": input_bundle_sha256,
            "payload_sha256": payload_sha256,
            "reason_codes": list(reasons),
        }
        receipt_id = _sha256(_canonical_json(identity, field_name="receipt_identity"))
        return cls(
            stage=stage,
            status=status,
            idempotency_key=idempotency_key,
            component=component,
            input_bundle_sha256=input_bundle_sha256,
            payload_json=payload_json,
            payload_sha256=payload_sha256,
            reason_codes=reasons,
            receipt_id=receipt_id,
        )

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "component": self.component.to_dict(),
            "input_bundle_sha256": self.input_bundle_sha256,
            "payload": self.payload,
            "payload_sha256": self.payload_sha256,
            "reason_codes": list(self.reason_codes),
            "receipt_id": self.receipt_id,
        }


@dataclass(frozen=True)
class RunBundle:
    """Append-only orchestration projection for a single stable run ID."""

    context: RunContext
    components: Tuple[ComponentIdentity, ...]
    stage_receipts: Tuple[StageReceipt, ...] = ()
    stop_new_risk: bool = False
    position_authority_valid: bool = False
    block_reasons: Tuple[str, ...] = ()
    permitted_order_ids: Tuple[str, ...] = ()
    contract_id: str = DAY_LOOP_CONTRACT_ID
    component_manifest_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.contract_id != DAY_LOOP_CONTRACT_ID:
            raise RunBundleError("day_loop_contract_id_mismatch")
        if not isinstance(self.context, RunContext):
            raise RunBundleError("context_invalid")
        expected_stages = [
            component.stage for component in self.components if component.stage
        ]
        if tuple(expected_stages) != STAGE_ORDER:
            raise RunBundleError("stage_component_manifest_incomplete_or_unordered")
        identities = [
            (component.stage, component.component_id) for component in self.components
        ]
        if len(identities) != len(set(identities)):
            raise RunBundleError("component_identity_duplicate")
        component_manifest = _canonical_json(
            [component.to_dict() for component in self.components],
            field_name="component_manifest",
        )
        object.__setattr__(
            self,
            "component_manifest_sha256",
            _sha256(component_manifest),
        )
        if type(self.stop_new_risk) is not bool:
            raise RunBundleError("stop_new_risk_must_be_bool")
        if type(self.position_authority_valid) is not bool:
            raise RunBundleError("position_authority_valid_must_be_bool")
        _unique_texts(self.block_reasons, field_name="block_reason")
        _unique_texts(self.permitted_order_ids, field_name="permitted_order_id")
        for index, receipt in enumerate(self.stage_receipts):
            if receipt.stage is not STAGE_ORDER[index]:
                raise RunBundleError("stage_receipts_out_of_order")
            if receipt.component != self.component_for(receipt.stage):
                raise RunBundleError("stage_receipt_component_drift")

    @classmethod
    def create(
        cls,
        context: RunContext,
        components: Tuple[ComponentIdentity, ...],
    ) -> "RunBundle":
        return cls(context=context, components=components)

    @property
    def run_id(self) -> str:
        return self.context.run_id

    @property
    def current_stage(self) -> Optional[RunStage]:
        if not self.stage_receipts:
            return None
        return self.stage_receipts[-1].stage

    @property
    def next_stage(self) -> Optional[RunStage]:
        if len(self.stage_receipts) == len(STAGE_ORDER):
            return None
        return STAGE_ORDER[len(self.stage_receipts)]

    @property
    def status(self) -> str:
        if self.next_stage is not None:
            return "incomplete_with_blocks" if self.stop_new_risk else "incomplete"
        return "completed_with_blocks" if self.stop_new_risk else "completed"

    @property
    def exit_evaluation_allowed(self) -> bool:
        return self.position_authority_valid

    def component_for(self, stage: RunStage) -> ComponentIdentity:
        for component in self.components:
            if component.stage is stage:
                return component
        raise RunBundleError(f"component_missing_for_{stage.value}")

    def receipt_for(self, stage: RunStage) -> StageReceipt:
        for receipt in self.stage_receipts:
            if receipt.stage is stage:
                return receipt
        raise RunBundleError(f"receipt_missing_for_{stage.value}")

    def append(
        self,
        receipt: StageReceipt,
        *,
        stop_new_risk: bool,
        position_authority_valid: Optional[bool],
        block_reasons: Tuple[str, ...],
        permitted_order_ids: Optional[Tuple[str, ...]],
    ) -> "RunBundle":
        if receipt.receipt_id in {
            existing.receipt_id for existing in self.stage_receipts
        }:
            return self
        if receipt.stage is not self.next_stage:
            raise RunBundleError("receipt_stage_is_not_next")
        if receipt.input_bundle_sha256 != self.bundle_sha256:
            raise RunBundleError("receipt_input_bundle_mismatch")
        merged_reasons = list(self.block_reasons)
        for reason in block_reasons:
            if reason not in merged_reasons:
                merged_reasons.append(reason)
        next_position_valid = self.position_authority_valid
        if position_authority_valid is not None:
            if type(position_authority_valid) is not bool:
                raise RunBundleError("position_authority_override_must_be_bool")
            next_position_valid = position_authority_valid
        next_permitted = self.permitted_order_ids
        if permitted_order_ids is not None:
            next_permitted = _unique_texts(
                permitted_order_ids,
                field_name="permitted_order_id",
            )
        return replace(
            self,
            stage_receipts=(*self.stage_receipts, receipt),
            stop_new_risk=self.stop_new_risk or stop_new_risk,
            position_authority_valid=next_position_valid,
            block_reasons=tuple(merged_reasons),
            permitted_order_ids=next_permitted,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "run_id": self.run_id,
            "context": self.context.to_dict(),
            "components": [component.to_dict() for component in self.components],
            "component_manifest_sha256": self.component_manifest_sha256,
            "stage_receipts": [receipt.to_dict() for receipt in self.stage_receipts],
            "stop_new_risk": self.stop_new_risk,
            "position_authority_valid": self.position_authority_valid,
            "exit_evaluation_allowed": self.exit_evaluation_allowed,
            "block_reasons": list(self.block_reasons),
            "permitted_order_ids": list(self.permitted_order_ids),
            "status": self.status,
        }

    @property
    def bundle_sha256(self) -> str:
        return _sha256(_canonical_json(self.to_dict(), field_name="run_bundle"))


def _expected_idempotency_key(
    bundle: RunBundle,
    stage: RunStage,
    component: ComponentIdentity,
) -> str:
    return _sha256(
        _canonical_json(
            {
                "run_id": bundle.run_id,
                "stage": stage.value,
                "input_bundle_sha256": bundle.bundle_sha256,
                "component_id": component.component_id,
                "component_version": component.version,
                "component_artifact_sha256": component.artifact_sha256,
            },
            field_name="stage_idempotency_key",
        )
    )


def _receipt_stops_new_risk(receipt: StageReceipt) -> bool:
    if receipt.reason_codes:
        return True
    if receipt.stage is not RunStage.RISK_CHECKED:
        return False
    drift = receipt.payload.get("drift_constraint")
    return isinstance(drift, Mapping) and drift.get("stop_new_orders") is True


def _position_authority_override(
    bundle: RunBundle,
    receipt: StageReceipt,
) -> Optional[bool]:
    payload = receipt.payload
    if receipt.stage is RunStage.PREOPEN:
        return payload.get("position_authority_valid") is True
    if receipt.stage is RunStage.ORDERS_SIMULATED:
        position_valid = bundle.position_authority_valid
        raw_receipts = payload.get("order_receipts")
        if isinstance(raw_receipts, list):
            for raw_receipt in raw_receipts:
                if not isinstance(raw_receipt, Mapping):
                    continue
                status = raw_receipt.get("status")
                if isinstance(status, str) and status.lower() in {
                    "filled",
                    "partial",
                }:
                    position_valid = False
                    break
        if _EXECUTION_POSITION_INVALIDATING_REASONS.intersection(receipt.reason_codes):
            position_valid = False
        return position_valid
    if receipt.stage is RunStage.RECONCILED:
        return bool(
            not receipt.reason_codes and payload.get("position_authority_valid") is True
        )
    return None


def _validated_permitted_order_ids(
    bundle: RunBundle,
    receipt: StageReceipt,
    persisted_order_ids: Tuple[str, ...],
) -> Tuple[str, ...]:
    payload = receipt.payload
    raw_orders = payload.get("approved_orders")
    candidate_orders: list[tuple[str, str]] = []
    seen_order_ids: set[str] = set()
    risk_order_contract_invalid = not isinstance(raw_orders, list)
    if isinstance(raw_orders, list):
        for raw_order in raw_orders:
            if not isinstance(raw_order, Mapping):
                risk_order_contract_invalid = True
                continue
            order_id = raw_order.get("order_id")
            intent = raw_order.get("intent")
            if (
                not isinstance(order_id, str)
                or not order_id
                or order_id != order_id.strip()
                or not _RUN_TEXT_RE.fullmatch(order_id)
                or order_id in seen_order_ids
                or not isinstance(intent, str)
            ):
                risk_order_contract_invalid = True
                continue
            seen_order_ids.add(order_id)
            normalized_intent = intent.lower()
            if normalized_intent not in _NEW_RISK_INTENTS | _REDUCE_RISK_INTENTS:
                risk_order_contract_invalid = True
                continue
            candidate_orders.append((order_id, normalized_intent))
    block_new_risk = (
        bundle.stop_new_risk
        or _receipt_stops_new_risk(receipt)
        or risk_order_contract_invalid
    )
    derived_order_ids: list[str] = []
    for order_id, normalized_intent in candidate_orders:
        if normalized_intent in _NEW_RISK_INTENTS:
            if block_new_risk:
                continue
        else:
            if not bundle.position_authority_valid:
                continue
        derived_order_ids.append(order_id)
    derived = tuple(derived_order_ids)
    if derived != persisted_order_ids:
        raise RunBundleError("run_bundle_permitted_order_ids_mismatch")
    return derived


def _rebuild_receipt_chain(
    *,
    context: RunContext,
    components: Tuple[ComponentIdentity, ...],
    receipts: Tuple[StageReceipt, ...],
    persisted_order_ids: Tuple[str, ...],
) -> RunBundle:
    bundle = RunBundle.create(context, components)
    for receipt in receipts:
        if receipt.input_bundle_sha256 != bundle.bundle_sha256:
            raise RunBundleError("receipt_input_bundle_mismatch")
        component = bundle.component_for(receipt.stage)
        if receipt.idempotency_key != _expected_idempotency_key(
            bundle,
            receipt.stage,
            component,
        ):
            raise RunBundleError("receipt_idempotency_key_mismatch")
        expected_status = (
            "completed_with_blocks" if receipt.reason_codes else "completed"
        )
        if receipt.status != expected_status:
            raise RunBundleError("receipt_status_reason_codes_mismatch")
        permitted_order_ids: Optional[Tuple[str, ...]] = None
        if receipt.stage is RunStage.RISK_CHECKED:
            permitted_order_ids = _validated_permitted_order_ids(
                bundle,
                receipt,
                persisted_order_ids,
            )
        bundle = bundle.append(
            receipt,
            stop_new_risk=_receipt_stops_new_risk(receipt),
            position_authority_valid=_position_authority_override(bundle, receipt),
            block_reasons=receipt.reason_codes,
            permitted_order_ids=permitted_order_ids,
        )
    return bundle


def parse_run_bundle(value: object) -> RunBundle:
    """Rebuild and revalidate a persisted bundle without trusting its hashes."""

    root = _exact_mapping(
        value,
        field_name="run_bundle",
        keys=frozenset(
            {
                "contract_id",
                "run_id",
                "context",
                "components",
                "component_manifest_sha256",
                "stage_receipts",
                "stop_new_risk",
                "position_authority_valid",
                "exit_evaluation_allowed",
                "block_reasons",
                "permitted_order_ids",
                "status",
            }
        ),
    )
    context_raw = _exact_mapping(
        root.get("context"),
        field_name="context",
        keys=frozenset(
            {
                "trade_date",
                "decision_as_of",
                "market",
                "authority_id",
                "authority_generation",
                "execution_lineage",
                "account_type",
                "real_trading_enabled",
                "champion_manifest_sha256",
            }
        ),
    )
    try:
        context = RunContext(**dict(context_raw))
    except (TypeError, ValueError) as exc:
        raise RunBundleError("context_invalid") from exc

    components_raw = root.get("components")
    if not isinstance(components_raw, list):
        raise RunBundleError("components_invalid")
    components: list[ComponentIdentity] = []
    for raw in components_raw:
        component_raw = _exact_mapping(
            raw,
            field_name="component",
            keys=frozenset({"stage", "component_id", "version", "artifact_sha256"}),
        )
        raw_stage = component_raw.get("stage")
        try:
            stage = None if raw_stage is None else RunStage(raw_stage)
            component = ComponentIdentity(
                stage=stage,
                component_id=component_raw.get("component_id"),
                version=component_raw.get("version"),
                artifact_sha256=component_raw.get("artifact_sha256"),
            )
        except (TypeError, ValueError) as exc:
            raise RunBundleError("component_invalid") from exc
        components.append(component)

    receipts_raw = root.get("stage_receipts")
    if not isinstance(receipts_raw, list):
        raise RunBundleError("stage_receipts_invalid")
    receipts: list[StageReceipt] = []
    for raw in receipts_raw:
        receipt_raw = _exact_mapping(
            raw,
            field_name="stage_receipt",
            keys=frozenset(
                {
                    "stage",
                    "status",
                    "idempotency_key",
                    "component",
                    "input_bundle_sha256",
                    "payload",
                    "payload_sha256",
                    "reason_codes",
                    "receipt_id",
                }
            ),
        )
        try:
            stage = RunStage(receipt_raw.get("stage"))
        except (TypeError, ValueError) as exc:
            raise RunBundleError("receipt_stage_invalid") from exc
        component_raw = receipt_raw.get("component")
        component = next(
            (item for item in components if item.stage is stage),
            None,
        )
        if component is None or component.to_dict() != component_raw:
            raise RunBundleError("receipt_component_invalid")
        raw_reasons = receipt_raw.get("reason_codes")
        if not isinstance(raw_reasons, list):
            raise RunBundleError("receipt_reason_codes_invalid")
        try:
            receipt = StageReceipt.create(
                stage=stage,
                status=receipt_raw.get("status"),
                idempotency_key=receipt_raw.get("idempotency_key"),
                component=component,
                input_bundle_sha256=receipt_raw.get("input_bundle_sha256"),
                payload=receipt_raw.get("payload"),
                reason_codes=tuple(raw_reasons),
            )
        except (TypeError, ValueError) as exc:
            raise RunBundleError("stage_receipt_invalid") from exc
        if receipt.to_dict() != receipt_raw:
            raise RunBundleError("stage_receipt_hash_mismatch")
        receipts.append(receipt)

    raw_block_reasons = root.get("block_reasons")
    raw_permitted = root.get("permitted_order_ids")
    if not isinstance(raw_block_reasons, list) or not isinstance(raw_permitted, list):
        raise RunBundleError("run_bundle_reason_or_order_ids_invalid")
    if root.get("contract_id") != DAY_LOOP_CONTRACT_ID:
        raise RunBundleError("day_loop_contract_id_mismatch")
    try:
        bundle = _rebuild_receipt_chain(
            context=context,
            components=tuple(components),
            receipts=tuple(receipts),
            persisted_order_ids=tuple(raw_permitted),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, RunBundleError):
            raise
        raise RunBundleError("run_bundle_invalid") from exc
    if bundle.block_reasons != tuple(raw_block_reasons):
        raise RunBundleError("run_bundle_block_reasons_mismatch")
    if bundle.stop_new_risk != root.get("stop_new_risk"):
        raise RunBundleError("run_bundle_stop_new_risk_mismatch")
    if bundle.position_authority_valid != root.get("position_authority_valid"):
        raise RunBundleError("run_bundle_position_authority_mismatch")
    if bundle.permitted_order_ids != tuple(raw_permitted):
        raise RunBundleError("run_bundle_permitted_order_ids_mismatch")
    if bundle.to_dict() != root:
        raise RunBundleError("run_bundle_projection_mismatch")
    return bundle


__all__ = [
    "ComponentIdentity",
    "DAY_LOOP_CONTRACT_ID",
    "RunBundle",
    "RunBundleError",
    "RunContext",
    "RunStage",
    "STAGE_ORDER",
    "StageReceipt",
    "parse_run_bundle",
]
