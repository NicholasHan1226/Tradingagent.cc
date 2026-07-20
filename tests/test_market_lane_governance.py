from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from shared.governance.market_lanes import (
    ACTIVE_RUNTIME_MARKETS,
    canonical_runtime_market,
    load_market_lanes,
    validate_market_lane,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path, name: str, branch: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "lane-test@example.invalid")
    _git(repo, "config", "user.name", "Lane Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "branch", "main")
    return repo


def test_registry_has_three_disjoint_market_lanes() -> None:
    registry = load_market_lanes()
    assert {lane.lane_id for lane in registry.lanes} == {
        "ashare",
        "cnfutures",
        "crypto",
    }
    assert len({lane.branch for lane in registry.lanes}) == 3
    assert len({lane.worktree_basename for lane in registry.lanes}) == 3
    assert len({lane.authority_id for lane in registry.lanes}) == 3
    assert registry.get("ashare").authority_state == "current_verified_simulated"
    assert registry.get("cnfutures").authority_state == "current_verified_simulated"
    crypto = registry.get("crypto")
    assert crypto.authority_state == "local_fixture_simulated_candidate"
    assert crypto.authority_id == "crypto-capital-v1"
    retired = registry.get_retired_authority("crypto-shadow-sim-v1")
    assert retired.lane_id == "crypto"
    assert retired.successor_authority_id == crypto.authority_id
    assert retired.state == "historical_evidence_only"
    assert retired.read_only is True
    assert (
        len({lane.broker_boundary.simulation_contract for lane in registry.lanes}) == 3
    )
    assert (
        len(
            {lane.broker_boundary.future_live_adapter_family for lane in registry.lanes}
        )
        == 3
    )
    assert all(not lane.broker_boundary.live_enabled for lane in registry.lanes)
    assert registry.get("ashare").broker_boundary.external_test_contracts == ()
    assert registry.get("cnfutures").broker_boundary.external_test_contracts == ()
    assert registry.get("crypto").broker_boundary.external_test_contracts == (
        "tradingagent.crypto.binance_spot_testnet.v1",
    )
    assert ACTIVE_RUNTIME_MARKETS == ("ashare", "cn_futures", "crypto")
    assert registry.get_for_runtime_market("cn_futures").lane_id == "cnfutures"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ashare", "ashare"),
        ("a-share", "ashare"),
        ("cn_futures", "cn_futures"),
        ("cnfutures", "cn_futures"),
        ("crypto", "crypto"),
    ],
)
def test_runtime_market_aliases_only_map_to_owned_lanes(
    raw: str, expected: str
) -> None:
    assert canonical_runtime_market(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "us", "pm", "hk", "martian"])
def test_runtime_market_registry_rejects_missing_retired_and_unknown(
    raw: object,
) -> None:
    with pytest.raises(ValueError, match="unknown or retired"):
        canonical_runtime_market(raw)


def test_ashare_lane_accepts_only_owned_paths(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "market-ashare", "codex/market-ashare-lane")
    target = repo / "Ashare" / "runtime" / "entrypoint.py"
    target.parent.mkdir(parents=True)
    target.write_text("# candidate\n", encoding="utf-8")

    result = validate_market_lane("ashare", repo)

    assert result.changed_paths == ("Ashare/runtime/entrypoint.py",)
    assert result.base_ref == "main"
    assert result.ahead == 0
    assert result.behind == 0
    assert result.base_head == result.lane_head


@pytest.mark.parametrize(
    "path", ["shared/kernel.py", "Crypto/broker.py", "docs/architecture.md"]
)
def test_ashare_lane_rejects_handoff_or_other_market_paths(
    tmp_path: Path,
    path: str,
) -> None:
    repo = _make_repo(tmp_path, "market-ashare", "codex/market-ashare-lane")
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# forbidden\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside its ownership"):
        validate_market_lane("ashare", repo)


def test_lane_rejects_wrong_worktree_or_branch(tmp_path: Path) -> None:
    wrong_tree = _make_repo(tmp_path, "somewhere-else", "codex/market-ashare-lane")
    with pytest.raises(ValueError, match="requires worktree"):
        validate_market_lane("ashare", wrong_tree)

    wrong_branch = _make_repo(tmp_path, "market-ashare", "codex/wrong-lane")
    with pytest.raises(ValueError, match="requires branch"):
        validate_market_lane("ashare", wrong_branch)


def test_lane_rejects_development_when_branch_is_behind_base(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "market-ashare", "codex/market-ashare-lane")
    _git(repo, "checkout", "main")
    (repo / "README.md").write_text("new shared baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "advance shared kernel")
    _git(repo, "checkout", "codex/market-ashare-lane")

    with pytest.raises(ValueError, match="1 commit.s. behind main"):
        validate_market_lane("ashare", repo)
