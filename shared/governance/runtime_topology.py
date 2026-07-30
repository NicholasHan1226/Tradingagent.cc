"""Fail-closed runtime topology contract for multi-market deployment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from shared.governance.market_lanes import (
    ACTIVE_RUNTIME_MARKETS,
    MarketLaneRegistry,
    load_market_lanes,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_TOPOLOGY_PATH = ROOT / "shared" / "governance" / "runtime_topology.yaml"
EXPECTED_DATA_ROUTES = ("GET /v1/catalog", "POST /v1/query")
ALLOWED_ISOLATION_LEVELS = frozenset({"process", "host"})
ALLOWED_LEARNING_PLACEMENT_POLICIES = frozenset(
    {"with_market_core", "dedicated_shared_compute"}
)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True)
class RuntimeSafety:
    simulation_only: bool
    real_trading_enabled: bool
    external_execution_enabled: bool
    automatic_promotion_enabled: bool
    automatic_risk_expansion_enabled: bool
    shared_writable_filesystem_allowed: bool


@dataclass(frozen=True)
class DataContract:
    product: str
    routes: tuple[str, ...]
    explicit_endpoint_required: bool
    token_file_required: bool
    direct_database_access_allowed: bool
    provider_fallback_allowed: bool


@dataclass(frozen=True)
class MarketRuntime:
    market: str
    lane_id: str
    currency: str
    capital_authority_id: str
    core_component: str
    learning_component: str
    data_component: str
    fault_domain: str
    writer_identity: str
    state_namespace: str
    service_prefix: str
    schedule_class: str
    max_active_writers: int
    failover_mode: str
    learning_failure_isolated: bool


@dataclass(frozen=True)
class DeploymentProfile:
    profile_id: str
    isolation_level: str
    learning_placement_policy: str
    placements: Mapping[str, str]


@dataclass(frozen=True)
class RuntimeTopology:
    version: int
    contract_id: str
    safety: RuntimeSafety
    data_contract: DataContract
    market_runtimes: tuple[MarketRuntime, ...]
    deployment_profiles: tuple[DeploymentProfile, ...]

    def market(self, market: str) -> MarketRuntime:
        matches = [item for item in self.market_runtimes if item.market == market]
        if len(matches) != 1:
            raise ValueError(f"unknown or duplicate runtime market: {market}")
        return matches[0]

    def profile(self, profile_id: str) -> DeploymentProfile:
        matches = [
            item for item in self.deployment_profiles if item.profile_id == profile_id
        ]
        if len(matches) != 1:
            raise ValueError(f"unknown or duplicate deployment profile: {profile_id}")
        return matches[0]

    @property
    def required_components(self) -> frozenset[str]:
        components = {"front-readonly"}
        for runtime in self.market_runtimes:
            components.update(
                {
                    runtime.core_component,
                    runtime.learning_component,
                    runtime.data_component,
                }
            )
        return frozenset(components)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return value


def _parse_safety(value: Any) -> RuntimeSafety:
    raw = _mapping(value, "safety")
    safety = RuntimeSafety(
        simulation_only=_strict_bool(
            raw.get("simulation_only"), "safety.simulation_only"
        ),
        real_trading_enabled=_strict_bool(
            raw.get("real_trading_enabled"), "safety.real_trading_enabled"
        ),
        external_execution_enabled=_strict_bool(
            raw.get("external_execution_enabled"),
            "safety.external_execution_enabled",
        ),
        automatic_promotion_enabled=_strict_bool(
            raw.get("automatic_promotion_enabled"),
            "safety.automatic_promotion_enabled",
        ),
        automatic_risk_expansion_enabled=_strict_bool(
            raw.get("automatic_risk_expansion_enabled"),
            "safety.automatic_risk_expansion_enabled",
        ),
        shared_writable_filesystem_allowed=_strict_bool(
            raw.get("shared_writable_filesystem_allowed"),
            "safety.shared_writable_filesystem_allowed",
        ),
    )
    if not safety.simulation_only:
        raise ValueError("runtime topology must remain simulation-only")
    unsafe = {
        "real_trading_enabled": safety.real_trading_enabled,
        "external_execution_enabled": safety.external_execution_enabled,
        "automatic_promotion_enabled": safety.automatic_promotion_enabled,
        "automatic_risk_expansion_enabled": (safety.automatic_risk_expansion_enabled),
        "shared_writable_filesystem_allowed": (
            safety.shared_writable_filesystem_allowed
        ),
    }
    enabled = sorted(key for key, value in unsafe.items() if value)
    if enabled:
        raise ValueError("unsafe runtime topology flags enabled: " + ", ".join(enabled))
    return safety


def _parse_data_contract(value: Any) -> DataContract:
    raw = _mapping(value, "data_contract")
    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, list):
        raise ValueError("data_contract.routes must be a list")
    routes = tuple(_text(item, "data_contract.routes[]") for item in routes_raw)
    contract = DataContract(
        product=_text(raw.get("product"), "data_contract.product"),
        routes=routes,
        explicit_endpoint_required=_strict_bool(
            raw.get("explicit_endpoint_required"),
            "data_contract.explicit_endpoint_required",
        ),
        token_file_required=_strict_bool(
            raw.get("token_file_required"), "data_contract.token_file_required"
        ),
        direct_database_access_allowed=_strict_bool(
            raw.get("direct_database_access_allowed"),
            "data_contract.direct_database_access_allowed",
        ),
        provider_fallback_allowed=_strict_bool(
            raw.get("provider_fallback_allowed"),
            "data_contract.provider_fallback_allowed",
        ),
    )
    if contract.product != "TradingDatas":
        raise ValueError("data contract product must be TradingDatas")
    if contract.routes != EXPECTED_DATA_ROUTES:
        raise ValueError("data contract routes must remain catalog/query only")
    if not contract.explicit_endpoint_required or not contract.token_file_required:
        raise ValueError("data contract requires explicit endpoint and token file")
    if contract.direct_database_access_allowed or contract.provider_fallback_allowed:
        raise ValueError("data contract forbids database access and provider fallback")
    return contract


def _parse_market_runtimes(value: Any) -> tuple[MarketRuntime, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("market_runtimes must be a non-empty list")
    runtimes: list[MarketRuntime] = []
    for index, item in enumerate(value):
        raw = _mapping(item, f"market_runtimes[{index}]")
        runtime = MarketRuntime(
            market=_text(raw.get("market"), f"market_runtimes[{index}].market"),
            lane_id=_text(raw.get("lane_id"), f"market_runtimes[{index}].lane_id"),
            currency=_text(raw.get("currency"), f"market_runtimes[{index}].currency"),
            capital_authority_id=_text(
                raw.get("capital_authority_id"),
                f"market_runtimes[{index}].capital_authority_id",
            ),
            core_component=_text(
                raw.get("core_component"),
                f"market_runtimes[{index}].core_component",
            ),
            learning_component=_text(
                raw.get("learning_component"),
                f"market_runtimes[{index}].learning_component",
            ),
            data_component=_text(
                raw.get("data_component"),
                f"market_runtimes[{index}].data_component",
            ),
            fault_domain=_text(
                raw.get("fault_domain"), f"market_runtimes[{index}].fault_domain"
            ),
            writer_identity=_text(
                raw.get("writer_identity"),
                f"market_runtimes[{index}].writer_identity",
            ),
            state_namespace=_text(
                raw.get("state_namespace"),
                f"market_runtimes[{index}].state_namespace",
            ),
            service_prefix=_text(
                raw.get("service_prefix"),
                f"market_runtimes[{index}].service_prefix",
            ),
            schedule_class=_text(
                raw.get("schedule_class"),
                f"market_runtimes[{index}].schedule_class",
            ),
            max_active_writers=_positive_int(
                raw.get("max_active_writers"),
                f"market_runtimes[{index}].max_active_writers",
            ),
            failover_mode=_text(
                raw.get("failover_mode"),
                f"market_runtimes[{index}].failover_mode",
            ),
            learning_failure_isolated=_strict_bool(
                raw.get("learning_failure_isolated"),
                f"market_runtimes[{index}].learning_failure_isolated",
            ),
        )
        if runtime.max_active_writers != 1:
            raise ValueError(f"{runtime.market} must have exactly one active writer")
        if runtime.failover_mode != "manual_fenced":
            raise ValueError(f"{runtime.market} failover must be manual_fenced")
        if not runtime.learning_failure_isolated:
            raise ValueError(f"{runtime.market} learning failure must be isolated")
        runtimes.append(runtime)
    return tuple(runtimes)


def _parse_profiles(value: Any) -> tuple[DeploymentProfile, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("deployment_profiles must be a non-empty list")
    profiles: list[DeploymentProfile] = []
    for index, item in enumerate(value):
        raw = _mapping(item, f"deployment_profiles[{index}]")
        placements_raw = _mapping(
            raw.get("placements"), f"deployment_profiles[{index}].placements"
        )
        placements = {
            _text(component, f"deployment_profiles[{index}].placements.key"): _text(
                host, f"deployment_profiles[{index}].placements[{component}]"
            )
            for component, host in placements_raw.items()
        }
        isolation_level = _text(
            raw.get("isolation_level"),
            f"deployment_profiles[{index}].isolation_level",
        )
        if isolation_level not in ALLOWED_ISOLATION_LEVELS:
            raise ValueError(
                f"deployment_profiles[{index}].isolation_level is unsupported"
            )
        learning_placement_policy = _text(
            raw.get("learning_placement_policy"),
            f"deployment_profiles[{index}].learning_placement_policy",
        )
        if learning_placement_policy not in ALLOWED_LEARNING_PLACEMENT_POLICIES:
            raise ValueError(
                f"deployment_profiles[{index}].learning_placement_policy is unsupported"
            )
        profiles.append(
            DeploymentProfile(
                profile_id=_text(
                    raw.get("profile_id"),
                    f"deployment_profiles[{index}].profile_id",
                ),
                isolation_level=isolation_level,
                learning_placement_policy=learning_placement_policy,
                placements=placements,
            )
        )
    return tuple(profiles)


def _validate_market_alignment(
    topology: RuntimeTopology, lanes: MarketLaneRegistry
) -> None:
    expected_markets = set(ACTIVE_RUNTIME_MARKETS)
    actual_markets = {runtime.market for runtime in topology.market_runtimes}
    if actual_markets != expected_markets:
        raise ValueError(
            "runtime topology markets must exactly match active market lanes"
        )
    if len(actual_markets) != len(topology.market_runtimes):
        raise ValueError("runtime topology markets must not contain duplicates")

    unique_fields = {
        "fault_domain": [item.fault_domain for item in topology.market_runtimes],
        "writer_identity": [item.writer_identity for item in topology.market_runtimes],
        "state_namespace": [item.state_namespace for item in topology.market_runtimes],
        "service_prefix": [item.service_prefix for item in topology.market_runtimes],
        "core_component": [item.core_component for item in topology.market_runtimes],
        "learning_component": [
            item.learning_component for item in topology.market_runtimes
        ],
    }
    for field, values in unique_fields.items():
        if len(values) != len(set(values)):
            raise ValueError(f"market runtime {field} values must be unique")

    for runtime in topology.market_runtimes:
        lane = lanes.get_for_runtime_market(runtime.market)
        if lane.lane_id != runtime.lane_id:
            raise ValueError(f"{runtime.market} lane_id does not match market lanes")
        if lane.authority_id != runtime.capital_authority_id:
            raise ValueError(
                f"{runtime.market} capital authority does not match market lanes"
            )


def _validate_profiles(topology: RuntimeTopology) -> None:
    ids = [profile.profile_id for profile in topology.deployment_profiles]
    if len(ids) != len(set(ids)):
        raise ValueError("deployment profile ids must be unique")
    required = topology.required_components
    for profile in topology.deployment_profiles:
        actual = frozenset(profile.placements)
        if actual != required:
            missing = sorted(required.difference(actual))
            extra = sorted(actual.difference(required))
            raise ValueError(
                f"{profile.profile_id} placements mismatch; "
                f"missing={missing}, extra={extra}"
            )
        if profile.isolation_level == "host":
            core_hosts = {
                profile.placements[runtime.core_component]
                for runtime in topology.market_runtimes
            }
            if len(core_hosts) != len(topology.market_runtimes):
                raise ValueError(
                    f"{profile.profile_id} must place market cores on distinct hosts"
                )
            front_host = profile.placements["front-readonly"]
            if front_host in core_hosts:
                raise ValueError(
                    f"{profile.profile_id} must isolate the read-only front host"
                )
        core_hosts = {
            profile.placements[runtime.core_component]
            for runtime in topology.market_runtimes
        }
        learning_hosts = {
            profile.placements[runtime.learning_component]
            for runtime in topology.market_runtimes
        }
        if profile.learning_placement_policy == "with_market_core":
            for runtime in topology.market_runtimes:
                if (
                    profile.placements[runtime.learning_component]
                    != profile.placements[runtime.core_component]
                ):
                    raise ValueError(
                        f"{profile.profile_id} must keep each market learning worker "
                        "with its own market core"
                    )
        elif profile.learning_placement_policy == "dedicated_shared_compute":
            if profile.isolation_level != "host":
                raise ValueError(
                    f"{profile.profile_id} dedicated learning requires host isolation"
                )
            if len(learning_hosts) != 1:
                raise ValueError(
                    f"{profile.profile_id} must use one dedicated research host"
                )
            research_host = next(iter(learning_hosts))
            disallowed_hosts = {
                *core_hosts,
                profile.placements["front-readonly"],
                *(
                    profile.placements[runtime.data_component]
                    for runtime in topology.market_runtimes
                ),
            }
            if research_host in disallowed_hosts:
                raise ValueError(
                    f"{profile.profile_id} research host must be isolated from "
                    "core, data and front hosts"
                )


def load_runtime_topology(
    path: Path = DEFAULT_RUNTIME_TOPOLOGY_PATH,
    *,
    market_lanes_path: Path | None = None,
) -> RuntimeTopology:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("runtime topology contract must be a regular file")
    root = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    if not isinstance(root, Mapping):
        raise ValueError("runtime topology contract must be a mapping")
    version = root.get("version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("runtime topology version must be integer 1")
    topology = RuntimeTopology(
        version=1,
        contract_id=_text(root.get("contract_id"), "contract_id"),
        safety=_parse_safety(root.get("safety")),
        data_contract=_parse_data_contract(root.get("data_contract")),
        market_runtimes=_parse_market_runtimes(root.get("market_runtimes")),
        deployment_profiles=_parse_profiles(root.get("deployment_profiles")),
    )
    if topology.contract_id != "tradingagent.runtime_topology.v1":
        raise ValueError("unexpected runtime topology contract_id")
    lanes = (
        load_market_lanes()
        if market_lanes_path is None
        else load_market_lanes(Path(market_lanes_path))
    )
    _validate_market_alignment(topology, lanes)
    _validate_profiles(topology)
    return topology
