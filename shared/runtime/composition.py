"""Explicit, network-closed composition root for one A-share paper day.

This module wires existing authorities without reimplementing them.  The
caller supplies every business stage, every durable local store, the exact
TradingDatas V1 contract and a fixture transport.  The three adapter stages
are assembled here so their persistence and publication dependencies cannot
silently drift or acquire defaults.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from shared.data.evidence_gate import DataEvidenceGate, DatasetEvidencePolicy
from shared.data.research_snapshot import ResearchDataProfile
from shared.data.research_snapshot_store import FileResearchSnapshotStore
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.execution.execution_lineage import (
    ASHARE_CAPITAL_AUTHORITY_ID,
)
from shared.models.drift_action_store import DriftActionStoreError
from shared.models.drift_runtime import (
    DriftRuntimeConstraint,
    DriftRuntimeContractError,
    DriftRuntimeRiskAdapter,
)
from shared.models.champion_registry import (
    ChampionRegistryError,
    ChampionSelectionReceipt,
    ChampionSelectionRegistry,
)
from shared.review.sample_journal import SampleJournal
from shared.universe.policy import (
    CANONICAL_MAINBOARD_SCOPE_POLICY_SHA256,
    CanonicalMainboardScopePolicy,
)

from .day_loop import (
    ASharePaperDayLoop,
    StageRequest,
    StageResult,
)
from .drift_stage import (
    DriftConstrainedRiskStagePort,
    DriftConstrainedSimulationExecutionStagePort,
    DriftRiskStageContractError,
)
from .canonical_small_account_stage import CanonicalSmallAccountDecisionStagePort
from .capital_stages import (
    CapitalEffectAuthorization,
    CapitalEffectGuard,
    CapitalBackedPreopenStagePort,
    CapitalBackedReconcileStagePort,
    CapitalBackedRiskStagePort,
    CapitalBackedSimulationExecutionStagePort,
    PaperCapitalAccount,
    PaperCapitalStageError,
)
from .file_store import FileRunBundleStore
from .publisher import (
    LocalRunBundlePublisher,
    PublishedRunBundle,
    RunBundlePublishError,
)
from .run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunContext,
    RunStage,
    parse_run_bundle,
)
from .small_account_stage import SmallAccountDecisionStagePort
from .trusted_clock import TrustedExecutionClock
from .stage_ports import (
    LocalTodayReportPort,
    SampleJournalLearningPort,
    SharedSignalsResearchEvidencePort,
)


_BUSINESS_STAGES = frozenset(
    {
        RunStage.PREOPEN,
        RunStage.UNIVERSE_READY,
        RunStage.DECISION_READY,
        RunStage.RISK_CHECKED,
        RunStage.ORDERS_SIMULATED,
        RunStage.RECONCILED,
    }
)
_MANAGED_STAGES = frozenset(
    {
        RunStage.EVIDENCE_READY,
        RunStage.LEARNING_RECORDED,
        RunStage.REPORTED,
    }
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class PaperRuntimeCompositionError(RuntimeError):
    """Base error for explicit paper-runtime assembly or final readback."""


class PaperRuntimeConfigurationError(PaperRuntimeCompositionError, ValueError):
    """Raised before any stage runs when required configuration is unsafe."""


class PaperRuntimePublicationError(PaperRuntimeCompositionError):
    """Raised when the completed local-candidate projection cannot be proven."""


class FrozenFixtureHTTPTransport:
    """Data-only V1 response tape for the network-closed paper composition.

    Responses are canonicalized at construction and replayed as fresh values.
    The composition root accepts this exact type only, so an arbitrary callable
    cannot self-certify that it is offline.
    """

    __slots__ = (
        "_call_json",
        "_cursor",
        "_response_json",
        "fixture_sha256",
    )

    offline_fixture = True

    def __init__(self, responses: object) -> None:
        try:
            values = tuple(responses)  # type: ignore[arg-type]
        except TypeError as exc:
            raise PaperRuntimeConfigurationError(
                "frozen_transport_responses_invalid"
            ) from exc

        encoded_responses: list[str] = []
        fixture_payload: list[dict[str, object]] = []
        for response in values:
            if type(response) is not HTTPResponse:
                raise PaperRuntimeConfigurationError(
                    "frozen_transport_response_invalid"
                )
            envelope = {
                "json_body": dict(response.json_body),
                "status_code": response.status_code,
            }
            encoded = _canonical_fixture_json(envelope)
            encoded_responses.append(encoded)
            fixture_payload.append(json.loads(encoded))

        object.__setattr__(self, "_response_json", tuple(encoded_responses))
        object.__setattr__(self, "_call_json", [])
        object.__setattr__(self, "_cursor", 0)
        object.__setattr__(
            self,
            "fixture_sha256",
            _canonical_fixture_sha256(fixture_payload),
        )

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Return detached canonical call records for tests and audit."""

        return [json.loads(value) for value in self._call_json]

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        if method not in {"GET", "POST"} or not isinstance(url, str):
            raise PaperRuntimeConfigurationError("frozen_transport_request_invalid")
        parsed = urllib.parse.urlsplit(url)
        expected_method = {
            "/v1/catalog": "GET",
            "/v1/query": "POST",
        }.get(parsed.path)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or expected_method != method
        ):
            raise PaperRuntimeConfigurationError("frozen_transport_request_invalid")
        if not isinstance(headers, Mapping):
            raise PaperRuntimeConfigurationError("frozen_transport_request_invalid")
        if method == "GET" and json_body is not None:
            raise PaperRuntimeConfigurationError("frozen_transport_request_invalid")
        if method == "POST" and not isinstance(json_body, Mapping):
            raise PaperRuntimeConfigurationError("frozen_transport_request_invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise PaperRuntimeConfigurationError("frozen_transport_request_invalid")

        record = {
            "headers": dict(headers),
            "json_body": dict(json_body) if json_body is not None else None,
            "method": method,
            "timeout_seconds": float(timeout_seconds),
            "url": url,
        }
        try:
            call_json = json.dumps(
                record,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise PaperRuntimeConfigurationError(
                "frozen_transport_request_invalid"
            ) from exc
        self._call_json.append(call_json)

        cursor = self._cursor
        if cursor >= len(self._response_json):
            raise PaperRuntimeConfigurationError("frozen_transport_response_exhausted")
        object.__setattr__(self, "_cursor", cursor + 1)
        envelope = json.loads(self._response_json[cursor])
        return HTTPResponse(
            status_code=envelope["status_code"],
            json_body=envelope["json_body"],
        )


def _canonical_fixture_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise PaperRuntimeConfigurationError("frozen_fixture_payload_invalid") from exc
    if not isinstance(decoded, dict):
        raise PaperRuntimeConfigurationError("frozen_fixture_payload_invalid")
    return encoded


def _canonical_fixture_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PaperRuntimeConfigurationError("frozen_fixture_payload_invalid") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, init=False)
class FrozenFixtureStagePort:
    """Data-only business stage for the network-closed local candidate.

    Its component identity is derived from the canonical payload template.
    Exact-type validation rejects subclasses and arbitrary executable ports.
    """

    identity: ComponentIdentity
    _payload_template_json: str = field(repr=False)

    def __init__(self, stage: RunStage, payload: Mapping[str, Any]) -> None:
        if stage not in _BUSINESS_STAGES:
            raise PaperRuntimeConfigurationError(
                "frozen_fixture_stage_must_be_business_stage"
            )
        payload_json = _canonical_fixture_json(dict(payload))
        object.__setattr__(
            self,
            "identity",
            ComponentIdentity(
                stage=stage,
                component_id=f"frozen-fixture-{stage.value}",
                version="1",
                artifact_sha256=hashlib.sha256(
                    payload_json.encode("utf-8")
                ).hexdigest(),
            ),
        )
        object.__setattr__(self, "_payload_template_json", payload_json)

    @classmethod
    def _bind_placeholders(
        cls,
        value: Any,
        *,
        replacements: Mapping[str, Any],
    ) -> Any:
        if isinstance(value, Mapping):
            return {
                key: cls._bind_placeholders(item, replacements=replacements)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                cls._bind_placeholders(item, replacements=replacements)
                for item in value
            ]
        if isinstance(value, str) and value in replacements:
            return json.loads(
                json.dumps(
                    replacements[value],
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return value

    def execute(self, request: StageRequest) -> StageResult:
        if request.stage is not self.identity.stage:
            raise PaperRuntimeConfigurationError(
                "frozen_fixture_request_stage_mismatch"
            )
        replacements: dict[str, Any] = {
            "__RUN_ID__": request.run_id,
            "__INPUT_BUNDLE_SHA__": request.input_bundle_sha256,
            "__AUTHORITY_ID__": request.bundle.context.authority_id,
            "__AUTHORITY_GENERATION__": request.bundle.context.authority_generation,
            "__EXECUTION_LINEAGE__": request.bundle.context.execution_lineage,
        }
        if RunStage.EVIDENCE_READY in {
            receipt.stage for receipt in request.bundle.stage_receipts
        }:
            evidence = request.bundle.receipt_for(RunStage.EVIDENCE_READY).payload
            source_payload = {
                "profile_id": evidence["profile_id"],
                "catalog_version": evidence["catalog_version"],
                "decision_as_of": evidence["decision_as_of"],
                "research_snapshot_sha256": evidence["snapshot_sha256"],
                "dataset_receipt_ids": {
                    item["dataset_id"]: item["receipt_id"]
                    for item in evidence["datasets"]
                },
            }
            replacements["__RESEARCH_SOURCE_PAYLOAD__"] = source_payload
            replacements["__RESEARCH_SOURCE_SHA256__"] = _canonical_fixture_sha256(
                source_payload
            )
        if request.stage is RunStage.RISK_CHECKED and (
            "__OPTIMIZER_" in self._payload_template_json
            or "__POSITION_SNAPSHOT_RECEIPT_ID__" in self._payload_template_json
            or "__STARTING_AVAILABLE_CASH_CNY__" in self._payload_template_json
        ):
            decision_payload = request.bundle.receipt_for(
                RunStage.DECISION_READY
            ).payload
            decisions = decision_payload.get("decisions")
            plan = decision_payload.get("small_account_plan")
            plan_rows = (
                plan.get("plan_decisions") if isinstance(plan, Mapping) else None
            )
            actionable = [
                row
                for row in decisions or ()
                if isinstance(row, Mapping) and row.get("action") != "hold"
            ]
            if not isinstance(plan_rows, list) or len(actionable) != 1:
                raise PaperRuntimeConfigurationError(
                    "frozen_fixture_optimizer_binding_invalid"
                )
            decision = actionable[0]
            matching_rows = [
                row
                for row in plan_rows
                if isinstance(row, Mapping)
                and row.get("decision_id") == decision.get("decision_id")
            ]
            if len(matching_rows) != 1:
                raise PaperRuntimeConfigurationError(
                    "frozen_fixture_optimizer_binding_invalid"
                )
            plan_row = matching_rows[0]
            side = "buy" if decision.get("action") in {"open", "increase"} else "sell"
            replacements.update(
                {
                    "__OPTIMIZER_DECISION_ID__": decision.get("decision_id"),
                    "__OPTIMIZER_SYMBOL__": decision.get("symbol"),
                    "__OPTIMIZER_ACTION__": decision.get("action"),
                    "__OPTIMIZER_SIDE__": side,
                    "__OPTIMIZER_ORDER_QUANTITY__": plan_row.get("order_quantity"),
                    "__OPTIMIZER_RESERVATION_PRICE_CNY__": plan_row.get(
                        "reservation_price_cny"
                    ),
                    "__OPTIMIZER_ESTIMATED_ORDER_COST_CNY__": plan_row.get(
                        "estimated_order_cost_cny"
                    ),
                    "__OPTIMIZER_T1_ELIGIBLE__": (
                        decision.get("action") in {"reduce", "exit"}
                        and isinstance(plan_row.get("sellable_shares"), int)
                        and isinstance(plan_row.get("order_quantity"), int)
                        and plan_row.get("sellable_shares")
                        >= plan_row.get("order_quantity")
                    ),
                    "__OPTIMIZER_SELLABLE_QUANTITY__": plan_row.get("sellable_shares"),
                    "__POSITION_SNAPSHOT_RECEIPT_ID__": plan.get(
                        "position_snapshot_receipt_id"
                    ),
                    "__STARTING_AVAILABLE_CASH_CNY__": plan.get(
                        "starting_available_cash_cny"
                    ),
                }
            )
        if request.stage is RunStage.ORDERS_SIMULATED and (
            "__OPTIMIZER_" in self._payload_template_json
        ):
            risk_payload = request.bundle.receipt_for(RunStage.RISK_CHECKED).payload
            approved_orders = risk_payload.get("approved_orders")
            if not isinstance(approved_orders, list) or len(approved_orders) != 1:
                raise PaperRuntimeConfigurationError(
                    "frozen_fixture_optimizer_order_binding_invalid"
                )
            order = approved_orders[0]
            if not isinstance(order, Mapping):
                raise PaperRuntimeConfigurationError(
                    "frozen_fixture_optimizer_order_binding_invalid"
                )
            replacements.update(
                {
                    "__OPTIMIZER_SYMBOL__": order.get("symbol"),
                    "__OPTIMIZER_ACTION__": order.get("intent"),
                    "__OPTIMIZER_ORDER_QUANTITY__": order.get("quantity"),
                }
            )
        if request.stage is RunStage.RECONCILED:
            order_receipts = request.bundle.receipt_for(
                RunStage.ORDERS_SIMULATED
            ).payload["order_receipts"]
            replacements["__ORDER_RECEIPTS_SHA256__"] = _canonical_fixture_sha256(
                order_receipts
            )
        payload = self._bind_placeholders(
            json.loads(self._payload_template_json),
            replacements=replacements,
        )
        plan = payload.get("small_account_plan")
        if isinstance(plan, dict) and plan.get("plan_sha256") == "__PLAN_SHA__":
            unsigned_plan = dict(plan)
            unsigned_plan.pop("plan_sha256")
            plan["plan_sha256"] = _canonical_fixture_sha256(unsigned_plan)
        if payload.get("small_account_plan_sha256") == "__PLAN_SHA__":
            plan_sha = request.bundle.receipt_for(RunStage.DECISION_READY).payload[
                "small_account_plan"
            ]["plan_sha256"]
            payload["small_account_plan_sha256"] = plan_sha
            for order in payload.get("approved_orders", []):
                if order.get("small_account_plan_sha256") == "__PLAN_SHA__":
                    order["small_account_plan_sha256"] = plan_sha
        if request.stage is RunStage.ORDERS_SIMULATED:
            for receipt in payload.get("order_receipts", []):
                if receipt.get("fill_fingerprint") == (
                    "__CANONICAL_FILL_FINGERPRINT__"
                ):
                    fingerprint_payload = dict(receipt)
                    fingerprint_payload.pop("fill_fingerprint", None)
                    receipt["fill_fingerprint"] = _canonical_fixture_sha256(
                        fingerprint_payload
                    )
        return StageResult(payload=payload)


@dataclass(frozen=True)
class _ChampionCurrentBinding:
    """Detached expectation that is rechecked against the durable registry."""

    registry: ChampionSelectionRegistry
    receipt_sha256: str
    manifest_sha256: str

    @classmethod
    def load(
        cls,
        *,
        registry: ChampionSelectionRegistry,
        expected_manifest_sha256: str,
    ) -> "_ChampionCurrentBinding":
        if type(registry) is not ChampionSelectionRegistry:
            raise PaperRuntimeConfigurationError("champion_selection_registry_invalid")
        try:
            current = registry.load_current()
        except ChampionRegistryError as exc:
            raise PaperRuntimeConfigurationError(
                "champion_current_selection_unavailable"
            ) from exc
        cls._validate_simulation_receipt(
            current,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        return cls(
            registry=registry,
            receipt_sha256=current.receipt_sha256,
            manifest_sha256=current.selected_manifest_sha256,
        )

    @staticmethod
    def _validate_simulation_receipt(
        receipt: ChampionSelectionReceipt,
        *,
        expected_manifest_sha256: str,
    ) -> None:
        if (
            not isinstance(receipt, ChampionSelectionReceipt)
            or receipt.selected_manifest_sha256 != expected_manifest_sha256
            or receipt.action not in {"activate", "rollback"}
            or not receipt.human_approval_reference
            or receipt.capital_layer != "simulated"
            or receipt.account_type != "simulated"
            or receipt.simulation_only is not True
            or receipt.real_trading_enabled is not False
            or receipt.live_transition_authorized is not False
            or not isinstance(receipt.automatic_promotion_enabled, bool)
            or receipt.automatic_risk_expansion_enabled is not False
        ):
            raise PaperRuntimeConfigurationError("champion_current_selection_mismatch")

    def verify_current(self) -> ChampionSelectionReceipt:
        try:
            current = self.registry.load_current()
        except ChampionRegistryError as exc:
            raise PaperRuntimeConfigurationError(
                "champion_current_selection_unavailable"
            ) from exc
        self._validate_simulation_receipt(
            current,
            expected_manifest_sha256=self.manifest_sha256,
        )
        if current.receipt_sha256 != self.receipt_sha256:
            raise PaperRuntimeConfigurationError("champion_current_selection_changed")
        return current


class _ChampionBoundStagePort:
    """Recheck the current simulation-only Champion before a capital-bearing stage."""

    __slots__ = ("_base_port", "_binding", "identity")

    def __init__(
        self,
        *,
        base_port: object,
        binding: _ChampionCurrentBinding,
    ) -> None:
        identity = getattr(base_port, "identity", None)
        if not isinstance(identity, ComponentIdentity) or identity.stage is None:
            raise PaperRuntimeConfigurationError("champion_bound_stage_invalid")
        self.identity = ComponentIdentity(
            stage=identity.stage,
            component_id=f"champion-bound-{identity.component_id}",
            version="1",
            artifact_sha256=_canonical_fixture_sha256(
                {
                    "contract": "tradingagent.champion_bound_stage.v1",
                    "base": identity.to_dict(),
                    "champion_manifest_sha256": binding.manifest_sha256,
                    "champion_selection_receipt_sha256": binding.receipt_sha256,
                }
            ),
        )
        self._base_port = base_port
        self._binding = binding

    def execute(self, request: StageRequest) -> StageResult:
        self._binding.verify_current()
        result = self._base_port.execute(request)  # type: ignore[attr-defined]
        if not isinstance(result, StageResult):
            raise PaperRuntimeConfigurationError("champion_bound_stage_result_invalid")
        return result


class _ExecutionBundleRiskView:
    """Read-only view replacing only RISK_CHECKED for a reduce-only replay."""

    __slots__ = ("_bundle", "_risk_payload")

    def __init__(self, bundle: RunBundle, risk_payload: Mapping[str, Any]) -> None:
        self._bundle = bundle
        self._risk_payload = deepcopy(dict(risk_payload))

    def receipt_for(self, stage: RunStage) -> object:
        if stage is RunStage.RISK_CHECKED:
            return SimpleNamespace(payload=deepcopy(self._risk_payload))
        return self._bundle.receipt_for(stage)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bundle, name)


def _runtime_constraint_from_risk_payload(
    risk_payload: Mapping[str, Any],
) -> DriftRuntimeConstraint:
    raw = risk_payload.get("drift_constraint")
    if not isinstance(raw, Mapping):
        raise PaperRuntimeConfigurationError("risk_drift_constraint_missing")
    try:
        reason_codes = raw.get("reason_codes")
        if not isinstance(reason_codes, list) or not all(
            isinstance(value, str) and value for value in reason_codes
        ):
            raise TypeError("reason_codes")
        constraint = DriftRuntimeConstraint(
            max_risk_multiplier=raw["risk_multiplier_cap"],
            stop_new_orders=raw["stop_new_orders"],
            reduce_only=raw["reduce_only"],
            quarantined=raw["quarantined"],
            review_required=raw["review_required"],
            active_action_receipt_sha256=raw["active_action_receipt_sha256"],
            reason_codes=tuple(reason_codes),
            schema_version=raw["schema_version"],
        )
    except (KeyError, TypeError, ValueError, DriftRuntimeContractError) as exc:
        raise PaperRuntimeConfigurationError("risk_drift_constraint_invalid") from exc
    if _canonical_fixture_sha256(dict(raw)) != risk_payload.get(
        "drift_constraint_sha256"
    ):
        raise PaperRuntimeConfigurationError("risk_drift_constraint_digest_invalid")
    return constraint


def _constraint_loosened(
    current: DriftRuntimeConstraint,
    previous: DriftRuntimeConstraint,
) -> bool:
    if current.max_risk_multiplier > previous.max_risk_multiplier:
        return True
    return any(
        getattr(previous, field_name) and not getattr(current, field_name)
        for field_name in (
            "stop_new_orders",
            "reduce_only",
            "quarantined",
            "review_required",
        )
    )


def _constraint_tightened(
    current: DriftRuntimeConstraint,
    previous: DriftRuntimeConstraint,
) -> bool:
    if current.max_risk_multiplier < previous.max_risk_multiplier:
        return True
    return any(
        getattr(current, field_name) and not getattr(previous, field_name)
        for field_name in (
            "stop_new_orders",
            "reduce_only",
            "quarantined",
            "review_required",
        )
    )


class _PerEffectAuthorityGuard(CapitalEffectGuard):
    """Re-read drift and Champion immediately before every local side effect.

    Open/increase effects are denied as soon as the durable drift constraint
    tightens.  Reduce/exit effects remain available under drift tightening.
    Reservation release is always permitted after the re-read attempt because
    retaining stale frozen cash is strictly less safe than conservative cleanup.
    """

    _EFFECTS = frozenset(
        {
            "reserve",
            "sim_submit",
            "capital_commit",
            "reservation_release",
        }
    )

    def __init__(
        self,
        *,
        constraint_provider: object,
        champion_binding: _ChampionCurrentBinding | None = None,
        baseline_constraint: DriftRuntimeConstraint | None = None,
    ) -> None:
        if not callable(getattr(constraint_provider, "snapshot", None)):
            raise PaperRuntimeConfigurationError("capital_effect_guard_invalid")
        if baseline_constraint is not None and not isinstance(
            baseline_constraint,
            DriftRuntimeConstraint,
        ):
            raise PaperRuntimeConfigurationError("capital_effect_guard_invalid")
        self._constraint_provider = constraint_provider
        self._binding = champion_binding
        self._baseline_constraint = baseline_constraint
        self._strictest_constraint = baseline_constraint
        self.latest_constraint = baseline_constraint
        self.identity_sha256 = _canonical_fixture_sha256(
            {
                "contract": "tradingagent.per_effect_authority_guard.v1",
                "champion_selection_receipt_sha256": (
                    champion_binding.receipt_sha256
                    if champion_binding is not None
                    else None
                ),
                "baseline_constraint": (
                    baseline_constraint.to_day_loop_risk_context()
                    if baseline_constraint is not None
                    else None
                ),
            }
        )

    @staticmethod
    def _intent(order: Mapping[str, Any]) -> str:
        intent = str(order.get("intent") or "").strip().lower()
        if intent not in {"open", "increase", "reduce", "exit"}:
            raise PaperRuntimeConfigurationError("capital_effect_order_intent_invalid")
        return intent

    def _fresh_constraint(self) -> tuple[DriftRuntimeConstraint | None, str | None]:
        try:
            current = self._constraint_provider.snapshot()
        except Exception:
            return None, "drift_effect_constraint_unavailable"
        if not isinstance(current, DriftRuntimeConstraint):
            return None, "drift_effect_constraint_invalid"
        previous = self._strictest_constraint
        if previous is not None and _constraint_loosened(current, previous):
            return current, "drift_constraint_loosened_during_effect"
        if self._baseline_constraint is None:
            self._baseline_constraint = current
        self._strictest_constraint = current
        self.latest_constraint = current
        return current, None

    def authorize(
        self,
        *,
        effect: str,
        request: StageRequest,
        order: Mapping[str, Any],
    ) -> CapitalEffectAuthorization:
        if effect not in self._EFFECTS or not isinstance(request, StageRequest):
            raise PaperRuntimeConfigurationError(
                "capital_effect_authorization_request_invalid"
            )
        intent = self._intent(order)
        current, drift_error = self._fresh_constraint()

        champion_error: str | None = None
        if self._binding is not None:
            try:
                self._binding.verify_current()
            except PaperRuntimeConfigurationError as exc:
                champion_error = str(exc)

        if effect == "reservation_release":
            reason = champion_error or drift_error or "cleanup_authorized"
            return CapitalEffectAuthorization(allowed=True, reason=reason)
        if champion_error is not None:
            return CapitalEffectAuthorization(
                allowed=False,
                reason=champion_error,
            )
        if drift_error is not None or current is None:
            return CapitalEffectAuthorization(
                allowed=False,
                reason=drift_error or "drift_effect_constraint_invalid",
            )
        if intent in {"open", "increase"}:
            baseline = self._baseline_constraint
            tightened = baseline is not None and _constraint_tightened(
                current, baseline
            )
            if current.stop_new_orders or tightened:
                receipt = current.active_action_receipt_sha256 or "unreceipted"
                return CapitalEffectAuthorization(
                    allowed=False,
                    reason=f"drift_stop_new_risk:{receipt}",
                )
        return CapitalEffectAuthorization(allowed=True, reason="authorized")


def _canonical_execution_evidence_snapshot(
    *,
    order_id: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze explicit execution receipt clocks without consulting wall time."""

    if (
        not isinstance(order_id, str)
        or not order_id
        or not isinstance(snapshot, Mapping)
    ):
        raise PaperRuntimeConfigurationError(
            "capital_execution_evidence_snapshot_invalid"
        )
    normalized = deepcopy(dict(snapshot))
    parsed: dict[str, datetime] = {}
    for field_name in (
        "execution_time",
        "available_at",
        "ingested_at",
        "retrieved_as_of",
    ):
        raw = normalized.get(field_name)
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise PaperRuntimeConfigurationError(
                f"capital_execution_{field_name}_missing:{order_id}"
            )
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PaperRuntimeConfigurationError(
                f"capital_execution_{field_name}_invalid:{order_id}"
            ) from exc
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise PaperRuntimeConfigurationError(
                f"capital_execution_{field_name}_timezone_required:{order_id}"
            )
        parsed[field_name] = instant.astimezone(timezone.utc)
        normalized[field_name] = instant.isoformat()
    if not (
        parsed["execution_time"]
        <= parsed["available_at"]
        <= parsed["ingested_at"]
        <= parsed["retrieved_as_of"]
    ):
        raise PaperRuntimeConfigurationError(
            f"capital_execution_evidence_time_order_invalid:{order_id}"
        )
    return normalized


class _PreSideEffectDriftCapitalExecutionStagePort:
    """Re-read drift before any simulator, ledger fill, or reservation release.

    A constraint tightened after RISK_CHECKED turns open/increase orders into
    deterministic no-fills and releases their already-created reservations.
    Verified reduce/exit orders are executed through a fresh capital-backed
    port containing only their market snapshots.
    """

    __slots__ = (
        "_account",
        "_base_port",
        "_binding",
        "_constraint_provider",
        "_market_snapshots",
        "_strictest_constraint",
        "identity",
    )

    def __init__(
        self,
        *,
        base_port: CapitalBackedSimulationExecutionStagePort,
        account: PaperCapitalAccount,
        market_snapshots: Mapping[str, Mapping[str, Any]],
        constraint_provider: object,
        champion_binding: _ChampionCurrentBinding | None = None,
    ) -> None:
        if (
            type(base_port) is not CapitalBackedSimulationExecutionStagePort
            or type(account) is not PaperCapitalAccount
            or not callable(getattr(constraint_provider, "snapshot", None))
        ):
            raise PaperRuntimeConfigurationError("capital_execution_drift_gate_invalid")
        if set(market_snapshots) != set(base_port.market_snapshots):
            raise PaperRuntimeConfigurationError(
                "capital_execution_market_snapshot_set_mismatch"
            )
        try:
            frozen_snapshots = json.loads(
                json.dumps(
                    dict(base_port.market_snapshots),
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (TypeError, ValueError) as exc:
            raise PaperRuntimeConfigurationError(
                "capital_execution_market_snapshots_invalid"
            ) from exc
        if not isinstance(frozen_snapshots, dict):
            raise PaperRuntimeConfigurationError(
                "capital_execution_market_snapshots_invalid"
            )
        frozen_snapshots = {
            order_id: _canonical_execution_evidence_snapshot(
                order_id=order_id,
                snapshot=snapshot,
            )
            for order_id, snapshot in frozen_snapshots.items()
        }
        self.identity = ComponentIdentity(
            stage=RunStage.ORDERS_SIMULATED,
            component_id="pre-side-effect-drift-capital-execution",
            version="1",
            artifact_sha256=_canonical_fixture_sha256(
                {
                    "contract": (
                        "tradingagent.pre_side_effect_drift_capital_execution.v1"
                    ),
                    "base": base_port.identity.to_dict(),
                    "capital_account": account.identity_sha256,
                    "champion_selection_receipt_sha256": (
                        champion_binding.receipt_sha256
                        if champion_binding is not None
                        else None
                    ),
                }
            ),
        )
        self._base_port = base_port
        self._account = account
        self._market_snapshots = frozen_snapshots
        self._constraint_provider = constraint_provider
        self._strictest_constraint: DriftRuntimeConstraint | None = None
        self._binding = champion_binding

    def _canonical_receipt(
        self,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project a capital receipt into the learning/reconcile contract."""

        row = deepcopy(dict(receipt))
        order_id = row.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise PaperRuntimeConfigurationError(
                "capital_execution_receipt_order_id_invalid"
            )
        snapshot = self._market_snapshots.get(order_id)
        if not isinstance(snapshot, Mapping):
            raise PaperRuntimeConfigurationError(
                f"capital_execution_evidence_snapshot_missing:{order_id}"
            )
        for field_name in ("available_at", "ingested_at", "retrieved_as_of"):
            expected = snapshot[field_name]
            supplied = row.get(field_name)
            if supplied not in (None, expected):
                raise PaperRuntimeConfigurationError(
                    f"capital_execution_receipt_{field_name}_conflict:{order_id}"
                )
            row[field_name] = expected

        status = row.get("status")
        if status in {"filled", "partial"}:
            if row.get("execution_eligible") not in (None, True):
                raise PaperRuntimeConfigurationError(
                    f"capital_execution_eligibility_conflict:{order_id}"
                )
            row["execution_eligible"] = True
        elif status in {"not_filled", "rejected", "cancelled"}:
            reason = row.get("execution_reason")
            supplied_reason = row.get("nonfill_reason")
            if not isinstance(reason, str) or not reason:
                raise PaperRuntimeConfigurationError(
                    f"capital_execution_nonfill_reason_missing:{order_id}"
                )
            if supplied_reason not in (None, reason):
                raise PaperRuntimeConfigurationError(
                    f"capital_execution_nonfill_reason_conflict:{order_id}"
                )
            row["nonfill_reason"] = reason
        else:
            raise PaperRuntimeConfigurationError(
                f"capital_execution_receipt_status_invalid:{order_id}"
            )

        if row.get("fill_fingerprint") is not None:
            fingerprint_payload = dict(row)
            fingerprint_payload.pop("fill_fingerprint", None)
            row["fill_fingerprint"] = _canonical_fixture_sha256(fingerprint_payload)
        return row

    def _canonical_receipts(
        self,
        receipts: object,
    ) -> list[dict[str, Any]]:
        if not isinstance(receipts, list):
            raise PaperRuntimeConfigurationError("capital_execution_receipts_invalid")
        canonical: list[dict[str, Any]] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                raise PaperRuntimeConfigurationError(
                    "capital_execution_receipt_row_invalid"
                )
            canonical.append(self._canonical_receipt(receipt))
        return canonical

    def _latest_constraint(self) -> DriftRuntimeConstraint:
        try:
            current = self._constraint_provider.snapshot()
        except Exception as exc:
            raise PaperRuntimeConfigurationError(
                "drift_execution_constraint_unavailable"
            ) from exc
        if not isinstance(current, DriftRuntimeConstraint):
            raise PaperRuntimeConfigurationError("drift_execution_constraint_invalid")
        if self._strictest_constraint is not None and _constraint_loosened(
            current,
            self._strictest_constraint,
        ):
            raise PaperRuntimeConfigurationError(
                "drift_constraint_loosened_during_execution"
            )
        self._strictest_constraint = current
        return current

    @staticmethod
    def _blocked_receipt(
        *,
        request: StageRequest,
        base_port: CapitalBackedSimulationExecutionStagePort,
        order: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        order_id = str(order["order_id"])
        quantity = int(order["quantity"])
        market_sha256 = _canonical_fixture_sha256(dict(snapshot))
        release_receipt_id = base_port.release_unfilled(
            request=request,
            order=order,
            reason=reason,
        )
        if not release_receipt_id:
            raise PaperRuntimeConfigurationError(
                "drift_blocked_reservation_release_missing"
            )
        return {
            "order_id": order_id,
            "symbol": order["symbol"],
            "intent": order["intent"],
            "requested_quantity": quantity,
            "capital_authority_id": request.bundle.context.authority_id,
            "authority_generation": request.bundle.context.authority_generation,
            "execution_lineage": request.bundle.context.execution_lineage,
            "execution_receipt_id": _canonical_fixture_sha256(
                {
                    "contract": "tradingagent.paper_execution_receipt.v1",
                    "run_id": request.run_id,
                    "order_id": order_id,
                    "market_snapshot_sha256": market_sha256,
                }
            ),
            "market_evidence_receipt_id": snapshot["snapshot_id"],
            "market_snapshot_sha256": market_sha256,
            "terminal_at": snapshot["execution_time"],
            "real_trading_enabled": False,
            "status": "not_filled",
            "filled_quantity": 0,
            "residual_quantity": quantity,
            "capital_commit_status": "not_applicable",
            "capital_release_receipt_id": release_receipt_id,
            "capital_release_status": "released",
            "execution_reason": reason,
        }

    def execute(self, request: StageRequest) -> StageResult:
        if request.stage is not RunStage.ORDERS_SIMULATED:
            raise PaperRuntimeConfigurationError(
                "capital_execution_drift_request_invalid"
            )
        try:
            risk_payload = request.bundle.receipt_for(RunStage.RISK_CHECKED).payload
        except (AttributeError, KeyError) as exc:
            raise PaperRuntimeConfigurationError(
                "risk_receipt_missing_before_execution"
            ) from exc
        if not isinstance(risk_payload, Mapping):
            raise PaperRuntimeConfigurationError(
                "risk_receipt_invalid_before_execution"
            )
        risk_constraint = _runtime_constraint_from_risk_payload(risk_payload)
        guard = _PerEffectAuthorityGuard(
            constraint_provider=self._constraint_provider,
            champion_binding=self._binding,
            baseline_constraint=risk_constraint,
        )
        guarded_port = self._base_port.with_effect_guard(guard)
        result = guarded_port.execute(request)
        payload = dict(result.payload)
        payload["order_receipts"] = self._canonical_receipts(
            payload.get("order_receipts")
        )
        latest = guard.latest_constraint or risk_constraint
        context = latest.to_day_loop_risk_context()
        payload["drift_execution_constraint"] = context
        payload["drift_execution_constraint_sha256"] = _canonical_fixture_sha256(
            context
        )
        return StageResult(payload=payload)


class _LocalCandidateOptimizerDecisionPort:
    """Join optimizer authority with data-only fixture learning evidence.

    The optimizer remains the sole producer of actions, quantities, costs and
    cash.  The frozen decision payload contributes only candidate-set and
    prediction evidence needed by the audit-only learning stage.
    """

    __slots__ = (
        "_champion_binding",
        "_evidence_port",
        "_optimizer_port",
        "identity",
    )

    def __init__(
        self,
        *,
        optimizer_port: (
            SmallAccountDecisionStagePort | CanonicalSmallAccountDecisionStagePort
        ),
        evidence_port: FrozenFixtureStagePort,
        champion_binding: _ChampionCurrentBinding | None = None,
    ) -> None:
        identity_payload: dict[str, Any] = {
            "contract": "optimizer-plus-evidence-decision-identity-v1",
            "optimizer": optimizer_port.identity.to_dict(),
            "evidence": evidence_port.identity.to_dict(),
        }
        if champion_binding is not None:
            identity_payload["champion_selection_receipt_sha256"] = (
                champion_binding.receipt_sha256
            )
        self.identity = ComponentIdentity(
            stage=RunStage.DECISION_READY,
            component_id="local-candidate-small-account-decision",
            version="1",
            artifact_sha256=_canonical_fixture_sha256(identity_payload),
        )
        self._optimizer_port = optimizer_port
        self._evidence_port = evidence_port
        self._champion_binding = champion_binding

    @property
    def thesis_risk_authority(self):
        return self._optimizer_port.thesis_risk_authority

    def execute(self, request: StageRequest) -> StageResult:
        if self._champion_binding is not None:
            self._champion_binding.verify_current()
        optimized = self._optimizer_port._execute_under_runtime_identity(
            request,
            runtime_identity=self.identity,
        )
        evidence = self._evidence_port.execute(request).payload
        decisions = optimized.payload.get("decisions")
        candidate_set = evidence.get("candidate_set_receipt")
        predictions = evidence.get("journal_predictions")
        if not isinstance(decisions, list):
            raise PaperRuntimeConfigurationError("optimizer_decision_evidence_invalid")
        if not isinstance(candidate_set, Mapping):
            raise PaperRuntimeConfigurationError("candidate_set_receipt_missing")
        if not isinstance(candidate_set.get("candidates"), list):
            raise PaperRuntimeConfigurationError(
                "candidate_set_receipt_candidates_invalid"
            )
        if not isinstance(predictions, list):
            raise PaperRuntimeConfigurationError("journal_predictions_invalid")

        decision_by_symbol: dict[str, Mapping[str, Any]] = {}
        for decision in decisions:
            if not isinstance(decision, Mapping):
                raise PaperRuntimeConfigurationError(
                    "optimizer_decision_evidence_invalid"
                )
            symbol = decision.get("symbol")
            if (
                not isinstance(symbol, str)
                or not symbol
                or symbol in decision_by_symbol
            ):
                raise PaperRuntimeConfigurationError(
                    "optimizer_decision_evidence_invalid"
                )
            decision_by_symbol[symbol] = decision

        candidates = deepcopy(candidate_set["candidates"])
        prediction_rows = deepcopy(predictions)
        candidate_by_symbol = {
            row.get("symbol"): row
            for row in candidates
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
        }
        prediction_by_symbol = {
            row.get("symbol"): row
            for row in prediction_rows
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
        }
        if (
            set(candidate_by_symbol) != set(prediction_by_symbol)
            or len(candidate_by_symbol) != len(candidates)
            or len(prediction_by_symbol) != len(prediction_rows)
            or not set(candidate_by_symbol).issubset(decision_by_symbol)
        ):
            raise PaperRuntimeConfigurationError(
                "optimizer_decision_evidence_symbol_set_mismatch"
            )
        optimizer_only_symbols = set(decision_by_symbol) - set(candidate_by_symbol)
        if any(
            decision_by_symbol[symbol].get("action") != "hold"
            for symbol in optimizer_only_symbols
        ):
            raise PaperRuntimeConfigurationError(
                "optimizer_decision_evidence_missing_for_actionable_symbol"
            )

        for symbol in candidate_by_symbol:
            decision = decision_by_symbol[symbol]
            decision_id = decision["decision_id"]
            cluster_id = decision["decision_cluster_id"]
            selected = decision["action"] != "hold"
            candidate = candidate_by_symbol[symbol]
            prediction = prediction_by_symbol[symbol]
            candidate["decision_id"] = decision_id
            candidate["selected"] = selected
            if not selected:
                candidate["selection_propensity"] = 0.0
                candidate["selection_reason"] = "optimizer_hold"
            prediction["decision_id"] = decision_id
            prediction["decision_cluster_id"] = cluster_id
            prediction["capital_authority_id"] = request.bundle.context.authority_id
            prediction["authority_generation"] = (
                request.bundle.context.authority_generation
            )
            prediction["execution_lineage_id"] = (
                request.bundle.context.execution_lineage
            )
            prediction["real_trading_enabled"] = False
            prediction["live_execution_enabled"] = False
            if candidate.get("prediction_snapshot_id") != prediction.get("snapshot_id"):
                raise PaperRuntimeConfigurationError(
                    "optimizer_decision_prediction_binding_invalid"
                )

        merged = dict(optimized.payload)
        merged["candidate_set_receipt"] = {
            **deepcopy(dict(candidate_set)),
            "candidates": candidates,
        }
        merged["journal_predictions"] = prediction_rows
        return StageResult(payload=merged)


def _aware_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PaperRuntimeConfigurationError(f"{field_name}_invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PaperRuntimeConfigurationError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)


def _request_instant(value: QueryRequest) -> datetime | None:
    if value.as_of is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.as_of.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PaperRuntimeConfig:
    """All frozen business and TradingDatas identities for one paper run."""

    trade_date: str
    decision_as_of: datetime
    tradingdatas_v1_base_url: str
    tradingdatas_catalog_version: str
    tradingdatas_access_policy_id: str
    dataset_profile: ResearchDataProfile
    dataset_requests: Mapping[str, QueryRequest]
    evidence_policies: Mapping[str, DatasetEvidencePolicy]
    capital_authority_id: str
    authority_generation: int
    execution_lineage: str
    champion_manifest_sha256: str
    real_trading_enabled: bool = False
    live_execution_enabled: bool = False
    network_enabled: bool = False
    tradingdatas_timeout_seconds: float = 10.0
    tradingdatas_max_limit: int = 10_000
    _client_config: SharedSignalsV1Config = field(init=False, repr=False)
    _run_context: RunContext = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "real_trading_enabled",
                "live_execution_enabled",
                "network_enabled",
            ):
                value = getattr(self, field_name)
                if type(value) is not bool or value:
                    raise PaperRuntimeConfigurationError(
                        f"{field_name}_must_be_native_false"
                    )

            decision_as_of = _aware_utc(
                self.decision_as_of,
                field_name="decision_as_of",
            )
            if (
                decision_as_of.astimezone(_SHANGHAI).date().isoformat()
                != self.trade_date
            ):
                raise PaperRuntimeConfigurationError(
                    "decision_as_of_trade_date_mismatch"
                )
            if not isinstance(self.dataset_profile, ResearchDataProfile):
                raise PaperRuntimeConfigurationError("dataset_profile_missing")
            if (
                self.dataset_profile.catalog_version
                != self.tradingdatas_catalog_version
            ):
                raise PaperRuntimeConfigurationError(
                    "dataset_profile_catalog_version_mismatch"
                )

            requests = dict(self.dataset_requests)
            policies = dict(self.evidence_policies)
            expected_datasets = set(self.dataset_profile.dataset_ids)
            if set(requests) != expected_datasets:
                raise PaperRuntimeConfigurationError("dataset_request_set_incomplete")
            if set(policies) != expected_datasets:
                raise PaperRuntimeConfigurationError("evidence_policy_set_incomplete")
            requirements = {
                requirement.dataset_id: requirement
                for requirement in self.dataset_profile.requirements
            }
            for dataset_id in self.dataset_profile.dataset_ids:
                request = requests[dataset_id]
                policy = policies[dataset_id]
                requirement = requirements[dataset_id]
                if (
                    not isinstance(request, QueryRequest)
                    or request.dataset_id != dataset_id
                ):
                    raise PaperRuntimeConfigurationError(
                        f"dataset_request_invalid:{dataset_id}"
                    )
                request_instant = _request_instant(request)
                if requirement.query_as_of_mode == "decision_as_of":
                    query_time_valid = request_instant == decision_as_of
                else:
                    query_time_valid = request.as_of is None
                if not query_time_valid:
                    raise PaperRuntimeConfigurationError(
                        f"dataset_request_as_of_mode_invalid:{dataset_id}"
                    )
                if (
                    not isinstance(policy, DatasetEvidencePolicy)
                    or policy.dataset_id != dataset_id
                ):
                    raise PaperRuntimeConfigurationError(
                        f"evidence_policy_invalid:{dataset_id}"
                    )

            client_config = SharedSignalsV1Config(
                base_url=self.tradingdatas_v1_base_url,
                expected_catalog_version=self.tradingdatas_catalog_version,
                dataset_ids=frozenset(expected_datasets),
                access_policy_id=self.tradingdatas_access_policy_id,
                timeout_seconds=self.tradingdatas_timeout_seconds,
                max_limit=self.tradingdatas_max_limit,
                cache_ttl_seconds=0.0,
            )
            run_context = RunContext(
                trade_date=self.trade_date,
                decision_as_of=decision_as_of,
                market="ashare",
                authority_id=self.capital_authority_id,
                authority_generation=self.authority_generation,
                execution_lineage=self.execution_lineage,
                account_type="simulated",
                real_trading_enabled=False,
                champion_manifest_sha256=self.champion_manifest_sha256,
            )
        except PaperRuntimeConfigurationError:
            raise
        except (TypeError, ValueError) as exc:
            raise PaperRuntimeConfigurationError(
                "paper_runtime_configuration_invalid"
            ) from exc

        object.__setattr__(self, "decision_as_of", decision_as_of)
        object.__setattr__(
            self,
            "tradingdatas_v1_base_url",
            client_config.base_url,
        )
        object.__setattr__(self, "dataset_requests", MappingProxyType(requests))
        object.__setattr__(self, "evidence_policies", MappingProxyType(policies))
        object.__setattr__(self, "_client_config", client_config)
        object.__setattr__(self, "_run_context", run_context)


@dataclass(frozen=True)
class PaperRuntimeResult:
    """Validated completed bundle and its local-candidate publication."""

    bundle: RunBundle
    publication: PublishedRunBundle


class PaperRuntimeComposition:
    """Run the explicitly assembled paper loop and prove its final projection."""

    def __init__(
        self,
        *,
        loop: ASharePaperDayLoop,
        context: RunContext,
        publisher: LocalRunBundlePublisher,
        champion_binding: _ChampionCurrentBinding | None = None,
    ) -> None:
        self._loop = loop
        self._context = context
        self._publisher = publisher
        self._champion_binding = champion_binding

    @staticmethod
    def _verify_final_publication(
        bundle: RunBundle,
        publication: PublishedRunBundle,
    ) -> None:
        try:
            immutable_bytes = publication.immutable_path.read_bytes()
            latest_bytes = publication.latest_path.read_bytes()
            if immutable_bytes != latest_bytes:
                raise ValueError("projection_bytes_mismatch")
            raw = json.loads(latest_bytes.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("projection_not_object")
            metadata = raw.pop("_projection", None)
            expected_metadata = {
                "authority": "non_authority",
                "bundle_sha256": bundle.bundle_sha256,
                "environment": "local_candidate",
                "production_verified": False,
                "record_type": "run_bundle_projection",
                "schema_version": 1,
            }
            if metadata != expected_metadata:
                raise ValueError("projection_metadata_mismatch")
            if raw.get("status") not in {"completed", "completed_with_blocks"}:
                raise ValueError("projection_status_not_terminal")
            if raw.get("status") != bundle.status:
                raise ValueError("projection_status_mismatch")
            if raw.get("run_id") != bundle.run_id:
                raise ValueError("projection_run_id_mismatch")
            recovered = parse_run_bundle(raw)
            if recovered != bundle or recovered.bundle_sha256 != bundle.bundle_sha256:
                raise ValueError("projection_bundle_mismatch")
            if (
                publication.run_id != bundle.run_id
                or publication.bundle_sha256 != bundle.bundle_sha256
            ):
                raise ValueError("publication_identity_mismatch")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PaperRuntimePublicationError(
                "final_projection_readback_invalid"
            ) from exc

    def run(self) -> PaperRuntimeResult:
        if self._champion_binding is not None:
            self._champion_binding.verify_current()
        bundle = self._loop.run(self._context)
        if bundle.status not in {"completed", "completed_with_blocks"}:
            raise PaperRuntimePublicationError("paper_runtime_bundle_not_completed")
        try:
            publication = self._publisher.publish(bundle)
        except RunBundlePublishError as exc:
            raise PaperRuntimePublicationError(
                "final_projection_publish_failed"
            ) from exc
        self._verify_final_publication(bundle, publication)
        return PaperRuntimeResult(bundle=bundle, publication=publication)


def _validated_ports(
    value: Mapping[RunStage, FrozenFixtureStagePort],
) -> dict[RunStage, FrozenFixtureStagePort]:
    try:
        ports = dict(value)
    except (TypeError, ValueError) as exc:
        raise PaperRuntimeConfigurationError("business_stage_ports_missing") from exc
    if set(ports) != _BUSINESS_STAGES:
        raise PaperRuntimeConfigurationError("business_stage_port_set_incomplete")
    for stage, port in ports.items():
        if type(port) is not FrozenFixtureStagePort:
            raise PaperRuntimeConfigurationError(
                f"business_stage_port_must_be_frozen_fixture:{stage.value}"
            )
        if port.identity.stage is not stage:
            raise PaperRuntimeConfigurationError(
                f"business_stage_port_invalid:{stage.value}"
            )
    return ports


def _validated_managed_identities(
    value: Mapping[RunStage, ComponentIdentity],
) -> dict[RunStage, ComponentIdentity]:
    try:
        identities = dict(value)
    except (TypeError, ValueError) as exc:
        raise PaperRuntimeConfigurationError(
            "managed_stage_identities_missing"
        ) from exc
    if set(identities) != _MANAGED_STAGES:
        raise PaperRuntimeConfigurationError("managed_stage_identity_set_incomplete")
    for stage, identity in identities.items():
        if not isinstance(identity, ComponentIdentity) or identity.stage is not stage:
            raise PaperRuntimeConfigurationError(
                f"managed_stage_identity_invalid:{stage.value}"
            )
    return identities


def _validated_scope_policy(value: object) -> CanonicalMainboardScopePolicy:
    if type(value) is not CanonicalMainboardScopePolicy:
        raise PaperRuntimeConfigurationError(
            "scope_policy_must_be_exact_canonical_mainboard_policy"
        )
    expected = CanonicalMainboardScopePolicy()
    if (
        value.policy_sha256 != CANONICAL_MAINBOARD_SCOPE_POLICY_SHA256
        or value.identity != expected.identity
        or dict(value.manifest) != dict(expected.manifest)
    ):
        raise PaperRuntimeConfigurationError(
            "scope_policy_must_be_exact_canonical_mainboard_policy"
        )
    return value


def compose_paper_runtime(
    *,
    config: PaperRuntimeConfig,
    transport_fixture: FrozenFixtureHTTPTransport,
    research_snapshot_store: FileResearchSnapshotStore,
    run_bundle_store: FileRunBundleStore,
    sample_journal: SampleJournal,
    business_stage_ports: Mapping[RunStage, FrozenFixtureStagePort],
    small_account_decision_port: SmallAccountDecisionStagePort,
    drift_risk_adapter: DriftRuntimeRiskAdapter,
    managed_stage_identities: Mapping[RunStage, ComponentIdentity],
    scope_policy: CanonicalMainboardScopePolicy,
    local_publisher: LocalRunBundlePublisher,
) -> PaperRuntimeComposition:
    """Assemble the sole fixture-driven, simulation-only paper runtime."""

    if not isinstance(config, PaperRuntimeConfig):
        raise PaperRuntimeConfigurationError("paper_runtime_config_missing")
    if type(transport_fixture) is not FrozenFixtureHTTPTransport:
        raise PaperRuntimeConfigurationError("transport_fixture_must_be_frozen_fixture")
    if not isinstance(research_snapshot_store, FileResearchSnapshotStore):
        raise PaperRuntimeConfigurationError("research_snapshot_store_missing")
    if not isinstance(run_bundle_store, FileRunBundleStore):
        raise PaperRuntimeConfigurationError("run_bundle_store_missing")
    if not isinstance(sample_journal, SampleJournal):
        raise PaperRuntimeConfigurationError("sample_journal_missing")
    if not isinstance(local_publisher, LocalRunBundlePublisher):
        raise PaperRuntimeConfigurationError("local_publisher_missing")

    scope_policy = _validated_scope_policy(scope_policy)

    ports = _validated_ports(business_stage_ports)
    if (
        type(small_account_decision_port) is not SmallAccountDecisionStagePort
        or small_account_decision_port.identity.stage is not RunStage.DECISION_READY
        or small_account_decision_port.account_authority_source_class
        != "offline_fixture"
        or small_account_decision_port.runtime_environment != "local_candidate"
        or small_account_decision_port.promotion_eligible is not False
    ):
        raise PaperRuntimeConfigurationError("small_account_decision_port_invalid")
    ports[RunStage.DECISION_READY] = _LocalCandidateOptimizerDecisionPort(
        optimizer_port=small_account_decision_port,
        evidence_port=ports[RunStage.DECISION_READY],
    )
    if type(drift_risk_adapter) is not DriftRuntimeRiskAdapter:
        raise PaperRuntimeConfigurationError("drift_risk_adapter_invalid")
    try:
        ports[RunStage.RISK_CHECKED] = DriftConstrainedRiskStagePort(
            base_port=ports[RunStage.RISK_CHECKED],
            constraint_provider=drift_risk_adapter,
        )
        ports[RunStage.ORDERS_SIMULATED] = DriftConstrainedSimulationExecutionStagePort(
            base_port=ports[RunStage.ORDERS_SIMULATED],
            constraint_provider=drift_risk_adapter,
        )
    except (
        DriftActionStoreError,
        DriftRuntimeContractError,
        DriftRiskStageContractError,
    ) as exc:
        raise PaperRuntimeConfigurationError("drift_risk_snapshot_invalid") from exc
    identities = _validated_managed_identities(managed_stage_identities)
    client = SharedSignalsV1Client(
        config._client_config,
        transport=transport_fixture,
    )
    ports[RunStage.EVIDENCE_READY] = SharedSignalsResearchEvidencePort(
        identity=identities[RunStage.EVIDENCE_READY],
        client=client,
        profile=config.dataset_profile,
        requests=config.dataset_requests,
        evidence_gate=DataEvidenceGate(config.evidence_policies),
        decision_as_of=config.decision_as_of,
        snapshot_store=research_snapshot_store,
    )
    ports[RunStage.LEARNING_RECORDED] = SampleJournalLearningPort(
        identity=identities[RunStage.LEARNING_RECORDED],
        journal=sample_journal,
    )
    ports[RunStage.REPORTED] = LocalTodayReportPort(
        identity=identities[RunStage.REPORTED],
        publisher=local_publisher,
    )
    loop = ASharePaperDayLoop(
        ports=ports,
        scope_policy=scope_policy,
        store=run_bundle_store,
        thesis_risk_authority=small_account_decision_port.thesis_risk_authority,
        environ={"REAL_TRADING_ENABLED": "false"},
    )
    return PaperRuntimeComposition(
        loop=loop,
        context=config._run_context,
        publisher=local_publisher,
    )


def compose_capital_backed_paper_runtime(
    *,
    config: PaperRuntimeConfig,
    transport_fixture: FrozenFixtureHTTPTransport,
    research_snapshot_store: FileResearchSnapshotStore,
    run_bundle_store: FileRunBundleStore,
    sample_journal: SampleJournal,
    business_stage_ports: Mapping[RunStage, FrozenFixtureStagePort],
    canonical_small_account_decision_port: CanonicalSmallAccountDecisionStagePort,
    capital_account: PaperCapitalAccount,
    market_snapshots: Mapping[str, Mapping[str, Any]],
    execution_clock: TrustedExecutionClock,
    reconciled_at: str,
    drift_risk_adapter: DriftRuntimeRiskAdapter,
    champion_selection_registry: ChampionSelectionRegistry,
    managed_stage_identities: Mapping[RunStage, ComponentIdentity],
    scope_policy: CanonicalMainboardScopePolicy,
    local_publisher: LocalRunBundlePublisher,
) -> PaperRuntimeComposition:
    """Assemble the canonical-capital, network-closed A-share simulator.

    Unlike :func:`compose_paper_runtime`, this composition never accepts a
    fixture account snapshot.  The decision adapter re-reads the one
    ``ashare-capital-v1`` ledger after PREOPEN; risk reserves against that same
    account; execution commits to it; and RECONCILED closes it.  The durable
    manual Champion selection and the negative-only drift latch are re-read
    before capital-bearing stages.
    """

    if not isinstance(config, PaperRuntimeConfig):
        raise PaperRuntimeConfigurationError("paper_runtime_config_missing")
    if type(transport_fixture) is not FrozenFixtureHTTPTransport:
        raise PaperRuntimeConfigurationError("transport_fixture_must_be_frozen_fixture")
    if not isinstance(research_snapshot_store, FileResearchSnapshotStore):
        raise PaperRuntimeConfigurationError("research_snapshot_store_missing")
    if not isinstance(run_bundle_store, FileRunBundleStore):
        raise PaperRuntimeConfigurationError("run_bundle_store_missing")
    if not isinstance(sample_journal, SampleJournal):
        raise PaperRuntimeConfigurationError("sample_journal_missing")
    if not isinstance(local_publisher, LocalRunBundlePublisher):
        raise PaperRuntimeConfigurationError("local_publisher_missing")
    if type(capital_account) is not PaperCapitalAccount:
        raise PaperRuntimeConfigurationError("canonical_capital_account_missing")
    if type(canonical_small_account_decision_port) is not (
        CanonicalSmallAccountDecisionStagePort
    ):
        raise PaperRuntimeConfigurationError(
            "canonical_small_account_decision_port_invalid"
        )
    if (
        canonical_small_account_decision_port.identity.stage
        is not RunStage.DECISION_READY
        or canonical_small_account_decision_port.account_authority_source_class
        != "canonical_authority"
        or canonical_small_account_decision_port.runtime_environment
        != "canonical_simulated"
        or canonical_small_account_decision_port.promotion_eligible is not False
        or not canonical_small_account_decision_port.is_bound_to(
            account=capital_account,
            trade_date=config.trade_date,
            decision_time=config.decision_as_of,
        )
    ):
        raise PaperRuntimeConfigurationError(
            "canonical_small_account_decision_port_invalid"
        )
    if type(drift_risk_adapter) is not DriftRuntimeRiskAdapter:
        raise PaperRuntimeConfigurationError("drift_risk_adapter_invalid")

    scope_policy = _validated_scope_policy(scope_policy)

    try:
        capital_snapshot = capital_account.ledger.snapshot()
    except Exception as exc:
        raise PaperRuntimeConfigurationError(
            "canonical_capital_snapshot_unavailable"
        ) from exc
    if (
        config.capital_authority_id != ASHARE_CAPITAL_AUTHORITY_ID
        or capital_snapshot.authority_id != config.capital_authority_id
        or capital_snapshot.authority_generation != config.authority_generation
        or capital_snapshot.execution_lineage_id != config.execution_lineage
        or capital_account.ledger.policy.capital_authority_id
        != config.capital_authority_id
        or capital_account.ledger.policy.market != "ashare"
    ):
        raise PaperRuntimeConfigurationError("canonical_capital_context_mismatch")

    champion_binding = _ChampionCurrentBinding.load(
        registry=champion_selection_registry,
        expected_manifest_sha256=config.champion_manifest_sha256,
    )
    ports: dict[RunStage, Any] = dict(_validated_ports(business_stage_ports))
    ports[RunStage.PREOPEN] = CapitalBackedPreopenStagePort(
        base_port=ports[RunStage.PREOPEN],
        account=capital_account,
    )
    ports[RunStage.DECISION_READY] = _LocalCandidateOptimizerDecisionPort(
        optimizer_port=canonical_small_account_decision_port,
        evidence_port=ports[RunStage.DECISION_READY],
        champion_binding=champion_binding,
    )
    try:
        drift_risk_port = DriftConstrainedRiskStagePort(
            base_port=ports[RunStage.RISK_CHECKED],
            constraint_provider=drift_risk_adapter,
        )
        champion_bound_risk = _ChampionBoundStagePort(
            base_port=drift_risk_port,
            binding=champion_binding,
        )
        ports[RunStage.RISK_CHECKED] = CapitalBackedRiskStagePort(
            base_port=champion_bound_risk,
            account=capital_account,
            effect_guard=_PerEffectAuthorityGuard(
                constraint_provider=drift_risk_adapter,
                champion_binding=champion_binding,
            ),
        )
        capital_execution = CapitalBackedSimulationExecutionStagePort(
            account=capital_account,
            market_snapshots=market_snapshots,
            execution_clock=execution_clock,
        )
        ports[RunStage.ORDERS_SIMULATED] = _PreSideEffectDriftCapitalExecutionStagePort(
            base_port=capital_execution,
            account=capital_account,
            market_snapshots=market_snapshots,
            constraint_provider=drift_risk_adapter,
            champion_binding=champion_binding,
        )
        ports[RunStage.RECONCILED] = CapitalBackedReconcileStagePort(
            account=capital_account,
            reconciled_at=reconciled_at,
        )
    except (
        DriftActionStoreError,
        DriftRuntimeContractError,
        DriftRiskStageContractError,
        PaperCapitalStageError,
    ) as exc:
        raise PaperRuntimeConfigurationError(
            "capital_runtime_stage_configuration_invalid"
        ) from exc

    identities = _validated_managed_identities(managed_stage_identities)
    client = SharedSignalsV1Client(
        config._client_config,
        transport=transport_fixture,
    )
    ports[RunStage.EVIDENCE_READY] = SharedSignalsResearchEvidencePort(
        identity=identities[RunStage.EVIDENCE_READY],
        client=client,
        profile=config.dataset_profile,
        requests=config.dataset_requests,
        evidence_gate=DataEvidenceGate(config.evidence_policies),
        decision_as_of=config.decision_as_of,
        snapshot_store=research_snapshot_store,
    )
    ports[RunStage.LEARNING_RECORDED] = SampleJournalLearningPort(
        identity=identities[RunStage.LEARNING_RECORDED],
        journal=sample_journal,
    )
    ports[RunStage.REPORTED] = LocalTodayReportPort(
        identity=identities[RunStage.REPORTED],
        publisher=local_publisher,
    )
    loop = ASharePaperDayLoop(
        ports=ports,
        scope_policy=scope_policy,
        store=run_bundle_store,
        thesis_risk_authority=(
            canonical_small_account_decision_port.thesis_risk_authority
        ),
        environ={"REAL_TRADING_ENABLED": "false"},
    )
    return PaperRuntimeComposition(
        loop=loop,
        context=config._run_context,
        publisher=local_publisher,
        champion_binding=champion_binding,
    )


__all__ = [
    "FrozenFixtureHTTPTransport",
    "FrozenFixtureStagePort",
    "PaperRuntimeComposition",
    "PaperRuntimeCompositionError",
    "PaperRuntimeConfig",
    "PaperRuntimeConfigurationError",
    "PaperRuntimePublicationError",
    "PaperRuntimeResult",
    "compose_capital_backed_paper_runtime",
    "compose_paper_runtime",
]
