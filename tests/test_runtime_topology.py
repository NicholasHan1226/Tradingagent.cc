from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared.governance.runtime_topology import load_runtime_topology


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_PATH = ROOT / "shared" / "governance" / "runtime_topology.yaml"


def _write_mutation(tmp_path: Path, mutate) -> Path:
    payload = yaml.safe_load(TOPOLOGY_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    target = tmp_path / "runtime_topology.yaml"
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


def test_tracked_topology_supports_single_split_and_research_host_simulation() -> None:
    topology = load_runtime_topology()

    assert topology.safety.simulation_only is True
    assert topology.safety.real_trading_enabled is False
    assert topology.data_contract.routes == (
        "GET /v1/catalog",
        "POST /v1/query",
    )
    assert {item.market for item in topology.market_runtimes} == {
        "ashare",
        "cn_futures",
        "crypto",
    }
    assert all(item.max_active_writers == 1 for item in topology.market_runtimes)
    assert all(
        item.failover_mode == "manual_fenced" for item in topology.market_runtimes
    )
    assert all(item.learning_failure_isolated for item in topology.market_runtimes)

    single = topology.profile("single_host_sim")
    split = topology.profile("split_market_sim")
    research = topology.profile("split_market_with_research_host_sim")
    assert single.isolation_level == "process"
    assert single.learning_placement_policy == "with_market_core"
    assert len(set(single.placements.values())) == 1
    assert split.isolation_level == "host"
    assert split.learning_placement_policy == "with_market_core"
    assert (
        len(
            {split.placements[item.core_component] for item in topology.market_runtimes}
        )
        == 3
    )
    assert split.placements["front-readonly"] not in {
        split.placements[item.core_component] for item in topology.market_runtimes
    }
    assert research.learning_placement_policy == "dedicated_shared_compute"
    assert {
        research.placements[item.learning_component]
        for item in topology.market_runtimes
    } == {"research-host"}


@pytest.mark.parametrize(
    "field",
    [
        "real_trading_enabled",
        "external_execution_enabled",
        "automatic_promotion_enabled",
        "automatic_risk_expansion_enabled",
        "shared_writable_filesystem_allowed",
    ],
)
def test_topology_rejects_unsafe_flags(tmp_path: Path, field: str) -> None:
    path = _write_mutation(
        tmp_path,
        lambda payload: payload["safety"].__setitem__(field, True),
    )
    with pytest.raises(ValueError, match="unsafe runtime topology flags"):
        load_runtime_topology(path)


def test_topology_rejects_provider_route_or_database_fallback(
    tmp_path: Path,
) -> None:
    def mutate(payload: dict) -> None:
        payload["data_contract"]["routes"].append("GET /tushare")
        payload["data_contract"]["direct_database_access_allowed"] = True

    path = _write_mutation(tmp_path, mutate)
    with pytest.raises(ValueError, match="catalog/query only"):
        load_runtime_topology(path)


def test_topology_rejects_two_active_writers(tmp_path: Path) -> None:
    path = _write_mutation(
        tmp_path,
        lambda payload: payload["market_runtimes"][0].__setitem__(
            "max_active_writers", 2
        ),
    )
    with pytest.raises(ValueError, match="exactly one active writer"):
        load_runtime_topology(path)


def test_topology_rejects_cross_market_state_namespace(tmp_path: Path) -> None:
    def mutate(payload: dict) -> None:
        payload["market_runtimes"][1]["state_namespace"] = payload["market_runtimes"][
            0
        ]["state_namespace"]

    path = _write_mutation(tmp_path, mutate)
    with pytest.raises(ValueError, match="state_namespace values must be unique"):
        load_runtime_topology(path)


def test_split_profile_rejects_colocated_market_cores(tmp_path: Path) -> None:
    def mutate(payload: dict) -> None:
        split = payload["deployment_profiles"][1]["placements"]
        split["crypto-core"] = split["ashare-core"]

    path = _write_mutation(tmp_path, mutate)
    with pytest.raises(ValueError, match="market cores on distinct hosts"):
        load_runtime_topology(path)


def test_split_profile_rejects_learning_worker_on_other_market_host(
    tmp_path: Path,
) -> None:
    def mutate(payload: dict) -> None:
        split = payload["deployment_profiles"][1]["placements"]
        split["crypto-learning"] = split["ashare-core"]

    path = _write_mutation(tmp_path, mutate)
    with pytest.raises(ValueError, match="each market learning worker"):
        load_runtime_topology(path)


def test_dedicated_research_host_cannot_share_core_data_or_front(
    tmp_path: Path,
) -> None:
    def mutate(payload: dict) -> None:
        profile = payload["deployment_profiles"][2]
        for component in (
            "ashare-learning",
            "cnfutures-learning",
            "crypto-learning",
        ):
            profile["placements"][component] = "ashare-host"

    path = _write_mutation(tmp_path, mutate)
    with pytest.raises(ValueError, match="research host must be isolated"):
        load_runtime_topology(path)


def test_profile_must_place_every_required_component(tmp_path: Path) -> None:
    path = _write_mutation(
        tmp_path,
        lambda payload: payload["deployment_profiles"][0]["placements"].pop(
            "front-readonly"
        ),
    )
    with pytest.raises(ValueError, match="placements mismatch"):
        load_runtime_topology(path)
