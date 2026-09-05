from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from Crypto.cost_aware_challenger import (
    COST_AWARE_CHALLENGER,
    ROUND_TRIP_COST_FLOOR_RETURN,
    CostAwareChallenger,
    evaluate_cost_aware_challenger,
    ROUND_TRIP_HALF_SPREAD_BPS,
)
from Crypto.delayed_paper_cost_aware_challenger import (
    COST_AWARE_CHALLENGER_RUNNER_CONTRACT,
    run_cost_aware_challenger_once,
)
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.fixture_auto_sim import evaluate_frozen_champion, qualify_fixture_evidence
from Crypto.fixture_sim.contracts import CryptoSafetyError
from Crypto.round_trip_capital import (
    COST_AWARE_CHALLENGER_CAPITAL_POLICY,
    CryptoRoundTripError,
)
from tests.test_crypto_delayed_paper_runner import _runner_inputs


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "Crypto" / "fixtures" / "auto_sim_spot_cycle_v1.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_cost_floor_is_frozen_and_covers_two_leg_model_plus_margin() -> None:
    candidate = COST_AWARE_CHALLENGER

    assert candidate.minimum_decision_return == ROUND_TRIP_COST_FLOOR_RETURN
    assert candidate.minimum_decision_return == (
        candidate.entry_fee_rate
        + candidate.exit_fee_rate
        + candidate.entry_slippage_bps / 10_000
        + candidate.exit_slippage_bps / 10_000
        + candidate.entry_half_spread_bps / 10_000
        + candidate.exit_half_spread_bps / 10_000
        + candidate.entry_margin_return
    )
    assert candidate.manual_promotion_required is True
    assert candidate.promotion_authorized is False
    assert candidate.real_trading_enabled is False
    assert candidate.to_payload()["sha256"] == candidate.sha256

    with pytest.raises(ValueError, match="fields_are_immutable"):
        CostAwareChallenger(minimum_decision_return=ROUND_TRIP_COST_FLOOR_RETURN + 1)


def test_challenger_rejects_baseline_buy_that_does_not_cover_cost_floor() -> None:
    payload = _fixture()
    bars = payload["bars_5m"]
    assert isinstance(bars, list)
    last = bars[-1]
    assert isinstance(last, dict)
    last.update({"high": "50000.00", "low": "49900.00", "close": "49950.00"})
    evidence = qualify_fixture_evidence(payload)

    baseline = evaluate_frozen_champion(evidence)
    challenger = evaluate_cost_aware_challenger(evidence)

    assert baseline.action == "buy"
    assert baseline.decision_return < ROUND_TRIP_COST_FLOOR_RETURN
    assert challenger.action == "observe"
    assert challenger.reason == "cost_aware_momentum_cost_floor_not_met"
    assert challenger.champion_id == COST_AWARE_CHALLENGER.challenger_id
    assert challenger.champion_sha256 == COST_AWARE_CHALLENGER.sha256


def test_challenger_keeps_only_cost_covering_entry_and_never_promotes() -> None:
    payload = _fixture()
    bars = payload["bars_5m"]
    assert isinstance(bars, list)
    last = bars[-1]
    assert isinstance(last, dict)
    last.update({"high": "50150.00", "low": "49900.00", "close": "50050.00"})
    evidence = qualify_fixture_evidence(payload)
    decision = evaluate_cost_aware_challenger(evidence)

    assert decision.action == "buy"
    assert decision.decision_return >= ROUND_TRIP_COST_FLOOR_RETURN
    assert decision.reason == "cost_aware_momentum_cost_floor_passed"
    assert decision.promotion_authorized is False
    assert decision.real_trading_enabled is False
    assert COST_AWARE_CHALLENGER.entry_half_spread_bps == ROUND_TRIP_HALF_SPREAD_BPS


def test_noncanonical_challenger_is_rejected() -> None:
    evidence = qualify_fixture_evidence(_fixture())
    mutated = object.__new__(CostAwareChallenger)
    for field_name in CostAwareChallenger.__dataclass_fields__:
        object.__setattr__(
            mutated,
            field_name,
            getattr(COST_AWARE_CHALLENGER, field_name),
        )
    object.__setattr__(mutated, "challenger_id", "not-canonical")

    with pytest.raises(CryptoSafetyError, match="not_canonical"):
        evaluate_cost_aware_challenger(evidence, mutated)


def test_shadow_runner_uses_distinct_root_and_challenger_decisions(
    tmp_path: Path,
) -> None:
    baseline_port, baseline_profile, request, _ = _runner_inputs()
    baseline_root = tmp_path / "baseline"
    run_crypto_delayed_paper_round_trip_once(
        port=baseline_port,
        profile=baseline_profile,
        request=request,
        output_root=baseline_root,
    )
    baseline_before = _tree(baseline_root)
    port, profile, challenger_request, _ = _runner_inputs()
    result = run_cost_aware_challenger_once(
        port=port,
        profile=profile,
        request=challenger_request,
        output_root=tmp_path / "challenger",
    )

    assert result["status"] == "completed"
    assert result["challenger_runner_contract"] == COST_AWARE_CHALLENGER_RUNNER_CONTRACT
    assert result["capital_authority_id"] == COST_AWARE_CHALLENGER_CAPITAL_POLICY.authority_id
    assert result["capital_generation"] == COST_AWARE_CHALLENGER_CAPITAL_POLICY.generation
    assert result["execution_authority"] is False
    assert result["real_trading_enabled"] is False
    assert _tree(baseline_root) == baseline_before
    assert (tmp_path / "challenger" / "round_trip_capital").exists()
    for symbol in ("BTCUSDT", "ETHUSDT"):
        decision = result["symbols"][symbol]["bundle"]["decision"]
        assert decision["champion_id"] == COST_AWARE_CHALLENGER.challenger_id
        assert decision["champion_sha256"] == COST_AWARE_CHALLENGER.sha256
        assert (
            result["symbols"][symbol]["capital"]["capital"]["authority_id"]
            == COST_AWARE_CHALLENGER_CAPITAL_POLICY.authority_id
        )


def test_shadow_runner_rejects_existing_g5_root_without_writing(tmp_path: Path) -> None:
    baseline_port, baseline_profile, request, _ = _runner_inputs()
    baseline_root = tmp_path / "baseline"
    run_crypto_delayed_paper_round_trip_once(
        port=baseline_port,
        profile=baseline_profile,
        request=request,
        output_root=baseline_root,
    )
    before = _tree(baseline_root)
    challenger_port, challenger_profile, challenger_request, _ = _runner_inputs()

    with pytest.raises(CryptoRoundTripError, match="round_trip_head_mismatch"):
        run_cost_aware_challenger_once(
            port=challenger_port,
            profile=challenger_profile,
            request=challenger_request,
            output_root=baseline_root,
        )

    assert _tree(baseline_root) == before


def test_existing_round_trip_runner_rejects_noncallable_challenger_hook(
    tmp_path: Path,
) -> None:
    port, profile, request, _ = _runner_inputs()

    with pytest.raises(RuntimeError, match="decision_evaluator_invalid"):
        run_crypto_delayed_paper_round_trip_once(
            port=port,
            profile=profile,
            request=request,
            output_root=tmp_path,
            decision_evaluator=copy.deepcopy("not-a-function"),  # type: ignore[arg-type]
        )
