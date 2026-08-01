"""Role-scoped evidence readiness contract shared by all market lanes.

The contract deliberately separates data usability from trading authority.  It
does not make a dataset ready; it only evaluates caller-supplied, independently
verified proofs against the tracked policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_READINESS_PATH = (
    ROOT / "shared" / "governance" / "evidence_readiness.yaml"
)
EXPECTED_ROLES = (
    "observation_ready",
    "historical_pit_ready",
    "delayed_paper_ready",
    "execution_ready",
)
EXPECTED_FRESHNESS_POLICIES = (
    "execution_equivalent",
    "delayed_observation",
    "historical_pit",
)
EXPECTED_MARKETS = ("ashare", "crypto", "cn_futures")
DATASET_CONTRACT_FINGERPRINT_FIELDS = (
    "dataset_id",
    "schema_major",
    "default_fields",
    "filter_operators",
    "default_order",
    "limits",
    "identity_fields",
)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ValueError(f"{field} must be {qualifier}")
    result = tuple(_text(item, f"{field}[]") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _unique_text_list(value: Any, field: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ValueError(f"{field} must be {qualifier}")
    result = [_text(item, f"{field}[]") for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _canonical_json_value(value: Any, field: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be canonical JSON") from exc


def dataset_contract_material(catalog_row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Project one public catalog row into the canonical consumer contract.

    Unrelated catalog rows and runtime metadata are intentionally excluded.
    Ordered fields/order/identity retain their order; filter operators are
    normalized as sets because their advertised order has no wire meaning.
    """

    row = _mapping(catalog_row, "catalog_row")
    dataset_id = _text(row.get("dataset_id"), "catalog_row.dataset_id")
    schema_major = _positive_int(row.get("schema_major"), "catalog_row.schema_major")
    default_fields = _unique_text_list(
        row.get("default_fields"),
        "catalog_row.default_fields",
        allow_empty=False,
    )
    default_order = _unique_text_list(
        row.get("default_order"),
        "catalog_row.default_order",
        allow_empty=True,
    )
    identity_fields = _unique_text_list(
        row.get("identity_fields"),
        "catalog_row.identity_fields",
        allow_empty=True,
    )
    if not set(identity_fields).issubset(default_fields):
        raise ValueError("catalog_row.identity_fields must be default fields")

    raw_operators = _mapping(
        row.get("filter_operators"), "catalog_row.filter_operators"
    )
    operators: dict[str, list[str]] = {}
    for raw_field in sorted(raw_operators):
        field = _text(raw_field, "catalog_row.filter_operators field")
        if field not in default_fields:
            raise ValueError(
                "catalog_row.filter_operators field must be a default field"
            )
        values = _unique_text_list(
            raw_operators[raw_field],
            f"catalog_row.filter_operators.{field}",
            allow_empty=False,
        )
        operators[field] = sorted(values)

    limits = _canonical_json_value(row.get("limits"), "catalog_row.limits")
    if not isinstance(limits, dict) or not limits:
        raise ValueError("catalog_row.limits must be a non-empty mapping")
    for key, value in limits.items():
        _text(key, "catalog_row.limits key")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("catalog_row.limits values must be positive integers")

    return {
        "dataset_id": dataset_id,
        "schema_major": schema_major,
        "default_fields": default_fields,
        "filter_operators": operators,
        "default_order": default_order,
        "limits": limits,
        "identity_fields": identity_fields,
    }


def dataset_contract_fingerprint(catalog_row: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 for one provider-neutral catalog contract."""

    material = dataset_contract_material(catalog_row)
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReadinessRole:
    role_id: str
    depends_on: tuple[str, ...]
    enabled_in_current_phase: bool
    requires: tuple[str, ...]
    permits: tuple[str, ...]
    prohibits: tuple[str, ...]


@dataclass(frozen=True)
class FreshnessPolicy:
    policy_id: str
    wall_clock_freshness_required: bool
    maximum_lag_seconds: int | None
    maximum_bar_cadence_multiple: int | None
    maximum_jitter_seconds: int
    same_event_execution_allowed: bool


@dataclass(frozen=True)
class ContractBindingPolicy:
    api_major: str
    global_catalog_version_is_evidence: bool
    unrelated_catalog_change_blocks_consumption: bool
    per_dataset_contract_fingerprint_required: bool
    fingerprint_fields: tuple[str, ...]


@dataclass(frozen=True)
class ReplayPolicy:
    routine_cycle: str
    full_semantic_double_traversal_triggers: tuple[str, ...]
    retry_on_auth_failure: bool
    retry_on_cursor_anomaly: bool


@dataclass(frozen=True)
class ReadinessAssessment:
    granted_roles: tuple[str, ...]
    blocked_reasons: Mapping[str, tuple[str, ...]]

    def grants(self, role_id: str) -> bool:
        return role_id in self.granted_roles


@dataclass(frozen=True)
class EvidenceReadinessContract:
    version: int
    contract_id: str
    roles: tuple[ReadinessRole, ...]
    freshness_policies: tuple[FreshnessPolicy, ...]
    contract_binding: ContractBindingPolicy
    replay_policy: ReplayPolicy
    market_policies: Mapping[str, Mapping[str, Any]]
    safety: Mapping[str, bool]

    def role(self, role_id: str) -> ReadinessRole:
        matches = [item for item in self.roles if item.role_id == role_id]
        if len(matches) != 1:
            raise ValueError(f"unknown or duplicate readiness role: {role_id}")
        return matches[0]

    def freshness(self, policy_id: str) -> FreshnessPolicy:
        matches = [
            item for item in self.freshness_policies if item.policy_id == policy_id
        ]
        if len(matches) != 1:
            raise ValueError(f"unknown or duplicate freshness policy: {policy_id}")
        return matches[0]

    def assess(self, proofs: Mapping[str, bool]) -> ReadinessAssessment:
        """Evaluate explicit proof booleans without creating new authority."""

        if not isinstance(proofs, Mapping):
            raise ValueError("readiness proofs must be a mapping")
        unknown_values = sorted(
            key for key, value in proofs.items() if not isinstance(value, bool)
        )
        if unknown_values:
            raise ValueError(
                "readiness proofs must contain booleans: " + ", ".join(unknown_values)
            )
        granted: list[str] = []
        blocked: dict[str, tuple[str, ...]] = {}
        for role in self.roles:
            reasons: list[str] = []
            if not role.enabled_in_current_phase:
                reasons.append("role_disabled_in_current_phase")
            reasons.extend(
                f"dependency_not_granted:{dependency}"
                for dependency in role.depends_on
                if dependency not in granted
            )
            reasons.extend(
                f"proof_missing_or_false:{requirement}"
                for requirement in role.requires
                if proofs.get(requirement) is not True
            )
            if reasons:
                blocked[role.role_id] = tuple(reasons)
            else:
                granted.append(role.role_id)
        return ReadinessAssessment(
            granted_roles=tuple(granted), blocked_reasons=blocked
        )


def _parse_roles(value: Any) -> tuple[ReadinessRole, ...]:
    raw = _mapping(value, "roles")
    if tuple(raw) != EXPECTED_ROLES:
        raise ValueError("readiness roles must use the canonical order and names")
    roles: list[ReadinessRole] = []
    seen: set[str] = set()
    for role_id in EXPECTED_ROLES:
        item = _mapping(raw[role_id], f"roles.{role_id}")
        depends_on = _strings(
            item.get("depends_on"), f"roles.{role_id}.depends_on", allow_empty=True
        )
        if any(dependency not in seen for dependency in depends_on):
            raise ValueError(f"roles.{role_id}.depends_on must reference earlier roles")
        permits = _strings(
            item.get("permits"), f"roles.{role_id}.permits", allow_empty=True
        )
        prohibits = _strings(item.get("prohibits"), f"roles.{role_id}.prohibits")
        if set(permits) & set(prohibits):
            raise ValueError(f"roles.{role_id} permits and prohibits must not overlap")
        roles.append(
            ReadinessRole(
                role_id=role_id,
                depends_on=depends_on,
                enabled_in_current_phase=_strict_bool(
                    item.get("enabled_in_current_phase"),
                    f"roles.{role_id}.enabled_in_current_phase",
                ),
                requires=_strings(item.get("requires"), f"roles.{role_id}.requires"),
                permits=permits,
                prohibits=prohibits,
            )
        )
        seen.add(role_id)
    if roles[-1].enabled_in_current_phase:
        raise ValueError("execution_ready must remain disabled in the current phase")
    if "real_execution" not in roles[-1].prohibits:
        raise ValueError("execution_ready must prohibit real execution while disabled")
    return tuple(roles)


def _parse_freshness(value: Any) -> tuple[FreshnessPolicy, ...]:
    raw = _mapping(value, "freshness_policies")
    if tuple(raw) != EXPECTED_FRESHNESS_POLICIES:
        raise ValueError("freshness policies must use canonical names")
    policies: list[FreshnessPolicy] = []
    for policy_id in EXPECTED_FRESHNESS_POLICIES:
        item = _mapping(raw[policy_id], f"freshness_policies.{policy_id}")
        policies.append(
            FreshnessPolicy(
                policy_id=policy_id,
                wall_clock_freshness_required=_strict_bool(
                    item.get("wall_clock_freshness_required"),
                    f"freshness_policies.{policy_id}.wall_clock_freshness_required",
                ),
                maximum_lag_seconds=_optional_positive_int(
                    item.get("maximum_lag_seconds"),
                    f"freshness_policies.{policy_id}.maximum_lag_seconds",
                ),
                maximum_bar_cadence_multiple=_optional_positive_int(
                    item.get("maximum_bar_cadence_multiple"),
                    f"freshness_policies.{policy_id}.maximum_bar_cadence_multiple",
                ),
                maximum_jitter_seconds=_non_negative_int(
                    item.get("maximum_jitter_seconds"),
                    f"freshness_policies.{policy_id}.maximum_jitter_seconds",
                ),
                same_event_execution_allowed=_strict_bool(
                    item.get("same_event_execution_allowed"),
                    f"freshness_policies.{policy_id}.same_event_execution_allowed",
                ),
            )
        )
    execution, delayed, historical = policies
    if (
        execution.maximum_lag_seconds != 30
        or execution.same_event_execution_allowed
        or delayed.maximum_bar_cadence_multiple != 1
        or delayed.same_event_execution_allowed
        or historical.wall_clock_freshness_required
    ):
        raise ValueError("freshness policy safety invariants changed")
    return tuple(policies)


def _parse_binding(value: Any) -> ContractBindingPolicy:
    raw = _mapping(value, "contract_binding")
    policy = ContractBindingPolicy(
        api_major=_text(raw.get("api_major"), "contract_binding.api_major"),
        global_catalog_version_is_evidence=_strict_bool(
            raw.get("global_catalog_version_is_evidence"),
            "contract_binding.global_catalog_version_is_evidence",
        ),
        unrelated_catalog_change_blocks_consumption=_strict_bool(
            raw.get("unrelated_catalog_change_blocks_consumption"),
            "contract_binding.unrelated_catalog_change_blocks_consumption",
        ),
        per_dataset_contract_fingerprint_required=_strict_bool(
            raw.get("per_dataset_contract_fingerprint_required"),
            "contract_binding.per_dataset_contract_fingerprint_required",
        ),
        fingerprint_fields=_strings(
            raw.get("fingerprint_fields"), "contract_binding.fingerprint_fields"
        ),
    )
    if (
        policy.api_major != "v1"
        or not policy.global_catalog_version_is_evidence
        or policy.unrelated_catalog_change_blocks_consumption
        or not policy.per_dataset_contract_fingerprint_required
    ):
        raise ValueError("contract binding must be dataset-scoped and API-v1 bound")
    if policy.fingerprint_fields != DATASET_CONTRACT_FINGERPRINT_FIELDS:
        raise ValueError("contract binding fingerprint fields mismatch")
    return policy


def _parse_replay(value: Any) -> ReplayPolicy:
    raw = _mapping(value, "replay_policy")
    policy = ReplayPolicy(
        routine_cycle=_text(raw.get("routine_cycle"), "replay_policy.routine_cycle"),
        full_semantic_double_traversal_triggers=_strings(
            raw.get("full_semantic_double_traversal_triggers"),
            "replay_policy.full_semantic_double_traversal_triggers",
        ),
        retry_on_auth_failure=_strict_bool(
            raw.get("retry_on_auth_failure"), "replay_policy.retry_on_auth_failure"
        ),
        retry_on_cursor_anomaly=_strict_bool(
            raw.get("retry_on_cursor_anomaly"),
            "replay_policy.retry_on_cursor_anomaly",
        ),
    )
    if (
        policy.routine_cycle != "receipt_bound_single_traversal"
        or policy.retry_on_auth_failure
        or policy.retry_on_cursor_anomaly
    ):
        raise ValueError("replay policy must remain fail closed and non-retrying")
    return policy


def _parse_market_policies(value: Any) -> Mapping[str, Mapping[str, Any]]:
    raw = _mapping(value, "market_policies")
    if tuple(raw) != EXPECTED_MARKETS:
        raise ValueError("market policies must cover only canonical markets")
    result: dict[str, Mapping[str, Any]] = {}
    for market in EXPECTED_MARKETS:
        result[market] = dict(_mapping(raw[market], f"market_policies.{market}"))
    ashare_shadow = _mapping(result["ashare"].get("cohort_shadow"), "cohort_shadow")
    ratio = ashare_shadow.get("minimum_coverage_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or ratio != 0.99:
        raise ValueError("A-share partial shadow coverage must remain exactly 0.99")
    if (
        ashare_shadow.get("simulated_notional_allowed") is not False
        or ashare_shadow.get("silent_replacement_allowed") is not False
        or ashare_shadow.get("explicit_missing_identity_set_required") is not True
    ):
        raise ValueError(
            "A-share partial cohort must remain zero-notional and explicit"
        )
    crypto = _mapping(result["crypto"].get("operational_maturity"), "crypto")
    if crypto.get("minimum_continuous_slots") != 288:
        raise ValueError("Crypto operational maturity must remain 288 slots")
    return result


def _parse_safety(value: Any) -> Mapping[str, bool]:
    raw = _mapping(value, "safety")
    expected = {
        "simulation_only": True,
        "real_trading_enabled": False,
        "external_execution_enabled": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }
    if set(raw) != set(expected):
        raise ValueError("readiness safety fields mismatch")
    parsed = {key: _strict_bool(raw.get(key), f"safety.{key}") for key in expected}
    if parsed != expected:
        raise ValueError("readiness safety contract enables unsafe behavior")
    return parsed


def load_evidence_readiness_contract(
    path: Path = DEFAULT_EVIDENCE_READINESS_PATH,
) -> EvidenceReadinessContract:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("evidence readiness contract must be a regular file")
    payload = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    root = _mapping(payload, "evidence readiness contract")
    version = root.get("version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("evidence readiness version must be integer 1")
    contract = EvidenceReadinessContract(
        version=version,
        contract_id=_text(root.get("contract_id"), "contract_id"),
        roles=_parse_roles(root.get("roles")),
        freshness_policies=_parse_freshness(root.get("freshness_policies")),
        contract_binding=_parse_binding(root.get("contract_binding")),
        replay_policy=_parse_replay(root.get("replay_policy")),
        market_policies=_parse_market_policies(root.get("market_policies")),
        safety=_parse_safety(root.get("safety")),
    )
    if contract.contract_id != "tradingagent.evidence_readiness.v1":
        raise ValueError("evidence readiness contract_id invalid")
    return contract


__all__ = [
    "DATASET_CONTRACT_FINGERPRINT_FIELDS",
    "DEFAULT_EVIDENCE_READINESS_PATH",
    "EvidenceReadinessContract",
    "FreshnessPolicy",
    "ReadinessAssessment",
    "ReadinessRole",
    "dataset_contract_fingerprint",
    "dataset_contract_material",
    "load_evidence_readiness_contract",
]
