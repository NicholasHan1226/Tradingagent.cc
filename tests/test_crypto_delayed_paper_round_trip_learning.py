from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import Crypto.delayed_paper_round_trip_learning_worker as worker_module
import Crypto.delayed_paper_round_trip_learning as learning_module
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_ledger import CryptoDelayedPaperObservationStore
from Crypto.delayed_paper_round_trip_learning import (
    CryptoRoundTripLearningError,
    round_trip_learning_exit_code,
    run_crypto_delayed_paper_round_trip_learning_full_scrub,
    run_crypto_delayed_paper_round_trip_learning_incremental,
)
from Crypto.delayed_paper_round_trip_learning_worker import (
    _existing_epoch_root,
    run_round_trip_learning_worker_once,
)
from Crypto.five_minute_data import TradingDatasCryptoFiveMinuteDataPort
from tests.test_crypto_5m_support import (
    FixtureTradingDatasTransport,
    client,
    profile,
    window_request,
)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "evolution/round_trip_learning" not in path.as_posix()
    }


def _completed_round_trip(root: Path) -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    result = run_crypto_delayed_paper_round_trip_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=window_request(),
        output_root=root,
    )
    assert result["status"] == "completed"


def test_full_scrub_projects_g4_learning_without_mutating_core(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    before = _tree_bytes(tmp_path)

    result = run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=tmp_path
    )

    assert _tree_bytes(tmp_path) == before
    assert result["status"] == "recovered"
    assert result["completion_count"] == 1
    assert result["learning_authority"] is False
    assert result["execution_authority"] is False
    assert result["promotion_authorized"] is False
    assert round_trip_learning_exit_code(result) == 0
    learning = tmp_path / "evolution" / "round_trip_learning"
    assert len(list((learning / "samples").glob("*.json"))) == 1
    assert len(list((learning / "kpis").glob("*.json"))) == 1
    assert len(list((learning / "challengers").glob("*.json"))) == 1
    assert len(list((learning / "receipts").glob("*.json"))) == 1
    assert len(list((learning / "checkpoints").glob("*.json"))) == 1
    challenger_path = next((learning / "challengers").glob("*.json"))
    challenger = json.loads(challenger_path.read_text(encoding="utf-8"))
    assert challenger["suggestion"] == "continue_simulation_outcome_accumulation"
    assert challenger["reason_codes"] == [
        "insufficient_independent_outcomes",
        "deterministic_non_live_gate_pending",
    ]
    assert "manual_review_required" not in challenger["reason_codes"]
    assert result["manual_review_required"] is False
    assert result["automatic_champion_replacement"] is False


def test_incremental_requires_full_scrub_then_is_idempotent(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    required = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=tmp_path
    )
    assert required["status"] == "full_scrub_required"
    assert required["processed_count"] == 0
    assert required["remaining_backlog_count"] == 1
    assert required["projected_completion_count"] == 0
    assert required["core_completion_count"] == 1

    run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted((tmp_path / "evolution").rglob("*"))
        if path.is_file()
    }
    current = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=tmp_path
    )
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted((tmp_path / "evolution").rglob("*"))
        if path.is_file()
    }
    assert current["status"] == "current"
    assert current["processed_count"] == 0
    assert current["remaining_backlog_count"] == 0
    assert current["projected_completion_count"] == 1
    assert current["latest_projected_observation_id"]
    assert current["checkpoint_head_sha256"]
    assert before == after


def test_full_scrub_fails_closed_for_tampered_prior_projection(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)
    receipt = next(
        (tmp_path / "evolution" / "round_trip_learning" / "receipts").glob("*.json")
    )
    receipt.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_projection_invalid|round_trip_learning_projection_not_derived",
    ):
        run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)


def test_full_scrub_fails_closed_for_missing_checkpoint_claimed_receipt(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)
    receipt = next(
        (tmp_path / "evolution" / "round_trip_learning" / "receipts").glob("*.json")
    )
    receipt.unlink()

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_claimed_projection_missing",
    ):
        run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)


def test_worker_rejects_free_or_non_g4_manifest_paths(tmp_path: Path) -> None:
    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_manifest_path_invalid",
    ):
        run_round_trip_learning_worker_once(
            mode="incremental", epoch_manifest=tmp_path / "epoch.json"
        )


@pytest.mark.parametrize("generation", (4, 5))
def test_worker_admits_only_matching_g4_or_g5_manifest_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generation: int
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = manifest_directory / f"crypto-delayed-paper-round-trip-epoch-g{generation}-test.json"
    identity = tmp_path / "identity.json"
    identity.write_bytes(b"identity")
    root = worker_module.ROUND_TRIP_LEARNING_EPOCH_ROOTS[generation]
    context = SimpleNamespace(
        epoch_generation=generation,
        output_root=root,
        identity_path=identity,
        epoch_id=f"g{generation}-test",
        manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=root, identity_path=identity)
    seen: list[Path] = []
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(worker_module, "_existing_epoch_root", lambda _: None)
    monkeypatch.setattr(worker_module, "prepare_round_trip_epoch_candidate", lambda _: prepared)
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_full_scrub",
        lambda *, output_root: seen.append(output_root) or {"status": "recovered"},
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_full_scrub",
        lambda *, output_root, _deadline: {"status": "scrubbed"},
    )
    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        lambda *, output_root: {"status": "no_new_outcome"},
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="full-scrub", epoch_manifest=manifest
    )

    assert seen == [root]
    assert result["epoch_generation"] == generation
    assert result["epoch_output_root"] == str(root)
    assert result["factor_projection"]["status"] == "scrubbed"
    assert result["factor_strategy_evaluation"]["status"] == "no_new_outcome"


def test_worker_evaluation_failure_is_retryable_debt_after_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = manifest_directory / "crypto-delayed-paper-round-trip-epoch-g5-test.json"
    identity = tmp_path / "identity.json"
    identity.write_bytes(b"identity")
    root = worker_module.ROUND_TRIP_LEARNING_EPOCH_ROOTS[5]
    context = SimpleNamespace(
        epoch_generation=5, output_root=root, identity_path=identity,
        epoch_id="g5-test", manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=root, identity_path=identity)
    monkeypatch.setattr(worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory)
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(worker_module, "_existing_epoch_root", lambda _: None)
    monkeypatch.setattr(worker_module, "prepare_round_trip_epoch_candidate", lambda _: prepared)
    monkeypatch.setattr(
        worker_module, "run_crypto_delayed_paper_round_trip_learning_incremental",
        lambda *, output_root: {"status": "current"},
    )
    monkeypatch.setattr(
        worker_module, "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: {"status": "projected_incremental", "completion_count": 7},
    )
    monkeypatch.setattr(
        worker_module, "run_factor_strategy_post_projection",
        lambda *, output_root: (_ for _ in ()).throw(
            worker_module.CryptoFactorStrategyPostProjectionError(
                "factor_strategy_evaluation_failed"
            )
        ),
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="incremental", epoch_manifest=manifest
    )

    assert result["factor_projection"]["status"] == "projected_incremental"
    assert result["factor_strategy_evaluation"]["status"] == "evaluation_debt"
    assert result["factor_strategy_evaluation"]["retry_on_next_learning_cadence"] is True
    assert result["factor_strategy_evaluation"]["stage"] == "factor_strategy_evaluation"
    assert result["factor_strategy_evaluation"]["reason"] == "factor_strategy_evaluation_failed"
    assert result["factor_strategy_evaluation"]["execution_authority"] is False


def test_worker_factor_projection_failure_is_retryable_debt_after_learning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = manifest_directory / "crypto-delayed-paper-round-trip-epoch-g5-test.json"
    identity = tmp_path / "identity.json"
    identity.write_bytes(b"identity")
    root = worker_module.ROUND_TRIP_LEARNING_EPOCH_ROOTS[5]
    context = SimpleNamespace(
        epoch_generation=5, output_root=root, identity_path=identity,
        epoch_id="g5-test", manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=root, identity_path=identity)
    monkeypatch.setattr(worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory)
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(worker_module, "_existing_epoch_root", lambda _: None)
    monkeypatch.setattr(worker_module, "prepare_round_trip_epoch_candidate", lambda _: prepared)
    monkeypatch.setattr(
        worker_module, "run_crypto_delayed_paper_round_trip_learning_incremental",
        lambda *, output_root: {"status": "current", "projected_completion_count": 7},
    )
    monkeypatch.setattr(
        worker_module, "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: (_ for _ in ()).throw(
            worker_module.CryptoFactorProjectionError("factor_projection_source_invalid")
        ),
    )
    monkeypatch.setattr(
        worker_module, "run_factor_strategy_post_projection",
        lambda *, output_root: pytest.fail("evaluation ran after factor projection debt"),
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="incremental", epoch_manifest=manifest
    )

    assert result["status"] == "current"
    assert result["projected_completion_count"] == 7
    assert result["factor_projection"] == result["factor_strategy_evaluation"]
    assert result["factor_projection"]["status"] == "evaluation_debt"
    assert result["factor_projection"]["stage"] == "factor_projection"
    assert result["factor_projection"]["reason"] == "factor_projection_failed"
    assert result["factor_projection"]["retry_on_next_learning_cadence"] is True
    assert result["factor_projection"]["execution_authority"] is False

    monkeypatch.setattr(
        worker_module, "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    with pytest.raises(RuntimeError, match="unexpected"):
        worker_module.run_round_trip_learning_worker_once(
            mode="incremental", epoch_manifest=manifest
        )


def _admit_full_scrub_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = manifest_directory / "crypto-delayed-paper-round-trip-epoch-g5-test.json"
    identity = tmp_path / "identity.json"
    identity.write_bytes(b"identity")
    root = worker_module.ROUND_TRIP_LEARNING_EPOCH_ROOTS[5]
    context = SimpleNamespace(
        epoch_generation=5,
        output_root=root,
        identity_path=identity,
        epoch_id="g5-test",
        manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=root, identity_path=identity)
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )
    monkeypatch.setattr(
        worker_module, "load_round_trip_epoch_manifest", lambda _: context
    )
    monkeypatch.setattr(worker_module, "_existing_epoch_root", lambda _: None)
    monkeypatch.setattr(
        worker_module, "prepare_round_trip_epoch_candidate", lambda _: prepared
    )
    return manifest, root


@pytest.mark.parametrize("factor_status", ("projected_incremental", "up_to_date"))
def test_worker_incremental_zero_label_path_is_bounded_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, factor_status: str
) -> None:
    manifest, root = _admit_full_scrub_worker(monkeypatch, tmp_path)
    factor_result = {
        "status": factor_status,
        "completion_count": 2_957,
        "label_count": 0,
        "label_learning_eligible_sample_count": 0,
    }
    calls: list[tuple[Path, bool | None]] = []
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_incremental",
        lambda *, output_root: {
            "status": "projected",
            "projected_completion_count": 2_957,
        },
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: factor_result,
    )

    def bounded_post_projection(
        *, output_root: Path, _resolved_outcome_changed: bool | None = None
    ) -> dict[str, object]:
        calls.append((output_root, _resolved_outcome_changed))
        return {
            "status": "no_new_outcome",
            "reason": "no_new_resolved_outcome",
        }

    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        bounded_post_projection,
    )

    first = worker_module.run_round_trip_learning_worker_once(
        mode="incremental", epoch_manifest=manifest
    )
    second = worker_module.run_round_trip_learning_worker_once(
        mode="incremental", epoch_manifest=manifest
    )

    assert first == second
    assert calls == [(root, False), (root, False)]
    assert first["factor_projection"] == factor_result
    assert first["factor_strategy_evaluation"] == {
        "status": "no_new_outcome",
        "reason": "no_new_resolved_outcome",
    }


def test_worker_incremental_compact_state_tamper_is_retryable_evaluation_debt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, _ = _admit_full_scrub_worker(monkeypatch, tmp_path)
    factor_result = {
        "status": "projected_incremental",
        "completion_count": 2_957,
        "label_count": 0,
        "label_learning_eligible_sample_count": 0,
    }
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_incremental",
        lambda *, output_root: {
            "status": "projected",
            "projected_completion_count": 2_957,
        },
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: factor_result,
    )
    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        lambda **_: (_ for _ in ()).throw(
            worker_module.CryptoFactorStrategyPostProjectionError(
                "factor_strategy_artifact_invalid"
            )
        ),
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="incremental", epoch_manifest=manifest
    )

    assert result["factor_projection"] == factor_result
    assert result["factor_strategy_evaluation"]["status"] == "evaluation_debt"
    assert result["factor_strategy_evaluation"]["reason"] == (
        "factor_strategy_evaluation_failed"
    )
    assert result["factor_strategy_evaluation"]["execution_authority"] is False


def test_worker_incremental_unknown_post_projection_error_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, _ = _admit_full_scrub_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_incremental",
        lambda *, output_root: {"status": "projected"},
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: {
            "status": "projected_incremental",
            "label_count": 0,
            "label_learning_eligible_sample_count": 0,
        },
    )
    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        lambda **_: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )

    with pytest.raises(RuntimeError, match="unexpected"):
        worker_module.run_round_trip_learning_worker_once(
            mode="incremental", epoch_manifest=manifest
        )


def test_worker_incremental_explicit_new_labels_run_existing_full_evaluator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, root = _admit_full_scrub_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_incremental",
        lambda *, output_root: {"status": "projected"},
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: {
            "status": "projected_incremental",
            "label_count": 2,
            "label_learning_eligible_sample_count": 2,
        },
    )
    seen: list[Path] = []
    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        lambda *, output_root: seen.append(output_root)
        or {"status": "shadow_evaluated"},
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="incremental", epoch_manifest=manifest
    )

    assert seen == [root]
    assert result["factor_strategy_evaluation"]["status"] == "shadow_evaluated"


def test_worker_incremental_unknown_factor_status_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, _ = _admit_full_scrub_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_incremental",
        lambda *, output_root: {"status": "projected"},
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_incremental",
        lambda *, output_root: {"status": "unexpected"},
    )
    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        lambda **_: pytest.fail("evaluation ran for an unknown factor status"),
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="incremental", epoch_manifest=manifest
    )

    assert result["factor_projection"] == result["factor_strategy_evaluation"]
    assert result["factor_projection"]["status"] == "evaluation_debt"
    assert result["factor_projection"]["reason"] == "factor_projection_failed"


def test_worker_round_trip_budget_debt_short_circuits_factor_and_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, _ = _admit_full_scrub_worker(monkeypatch, tmp_path)
    round_trip_result = {
        "status": "deferred_time_budget",
        "completion_count": 12_252,
        "projected_completion_count": 12_000,
    }
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_full_scrub",
        lambda *, output_root: round_trip_result,
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_full_scrub",
        lambda *, output_root, _deadline: pytest.fail(
            "factor ran after round-trip deferral"
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        lambda *, output_root: pytest.fail("evaluation ran after round-trip deferral"),
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="full-scrub", epoch_manifest=manifest
    )

    assert result["status"] == "deferred_time_budget"
    assert result["completion_count"] == 12_252
    assert result["projected_completion_count"] == 12_000
    assert result["factor_projection"] == result["factor_strategy_evaluation"]
    assert result["factor_projection"]["status"] == "evaluation_debt"
    assert result["factor_projection"]["reason"] == "factor_projection_time_budget"
    assert result["factor_projection"]["retry_on_next_learning_cadence"] is True
    assert result["factor_projection"]["execution_authority"] is False


def test_worker_factor_budget_preserves_projection_and_skips_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, _ = _admit_full_scrub_worker(monkeypatch, tmp_path)
    round_trip_result = {
        "status": "scrubbed",
        "completion_count": 12_252,
        "projected_completion_count": 12_252,
    }
    factor_result = {
        "status": "deferred_time_budget",
        "completion_count": 12_252,
        "verified_record_count": 12_252,
        "verified_label_source_count": 1_530,
    }
    deadlines: list[float] = []
    monkeypatch.setattr(worker_module, "monotonic", lambda: 7.0)
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_full_scrub",
        lambda *, output_root: round_trip_result,
    )
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_factor_research_full_scrub",
        lambda *, output_root, _deadline: deadlines.append(_deadline) or factor_result,
    )
    monkeypatch.setattr(
        worker_module,
        "run_factor_strategy_post_projection",
        lambda *, output_root: pytest.fail("evaluation ran after factor deferral"),
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="full-scrub", epoch_manifest=manifest
    )

    assert result["status"] == "scrubbed"
    assert result["projected_completion_count"] == 12_252
    assert deadlines == [117.0]
    assert result["factor_projection"] == factor_result
    assert result["factor_strategy_evaluation"]["status"] == "evaluation_debt"
    assert result["factor_strategy_evaluation"]["stage"] == "factor_projection"
    assert (
        result["factor_strategy_evaluation"]["reason"]
        == "factor_projection_time_budget"
    )
    assert result["factor_strategy_evaluation"]["execution_authority"] is False


def test_worker_rejects_g5_manifest_with_g4_context_before_root_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = manifest_directory / "crypto-delayed-paper-round-trip-epoch-g5-test.json"
    context = SimpleNamespace(epoch_generation=4)
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(
        worker_module,
        "_existing_epoch_root",
        lambda _: pytest.fail("mismatched generation reached root access"),
    )

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_epoch_generation_invalid",
    ):
        worker_module.run_round_trip_learning_worker_once(
            mode="full-scrub", epoch_manifest=manifest
        )


@pytest.mark.parametrize("generation", (4, 5))
def test_worker_rejects_matching_generation_with_noncanonical_root_before_root_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generation: int
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = (
        manifest_directory
        / f"crypto-delayed-paper-round-trip-epoch-g{generation}-test.json"
    )
    context = SimpleNamespace(
        epoch_generation=generation,
        output_root=tmp_path / f"wrong-g{generation}-root",
    )
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(
        worker_module,
        "_existing_epoch_root",
        lambda _: pytest.fail("noncanonical root reached root access"),
    )
    monkeypatch.setattr(
        worker_module,
        "prepare_round_trip_epoch_candidate",
        lambda _: pytest.fail("noncanonical root reached prepare access"),
    )

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_epoch_root_invalid",
    ):
        worker_module.run_round_trip_learning_worker_once(
            mode="full-scrub", epoch_manifest=manifest
        )


def test_worker_rejects_non_round_trip_or_unknown_generation_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )

    for name in (
        "crypto-delayed-paper-epoch-g5-test.json",
        "crypto-delayed-paper-round-trip-epoch-g6-test.json",
    ):
        with pytest.raises(
            CryptoRoundTripLearningError,
            match="round_trip_learning_manifest_path_invalid",
        ):
            worker_module.run_round_trip_learning_worker_once(
                mode="full-scrub", epoch_manifest=manifest_directory / name
            )


def test_worker_refuses_incomplete_epoch_before_any_prepare_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "incomplete-g4"
    root.mkdir()
    context = SimpleNamespace(output_root=root, identity_path=root / ".identity.json")

    with pytest.raises(
        CryptoRoundTripLearningError, match="round_trip_learning_root_incomplete"
    ):
        _existing_epoch_root(context)

    assert list(root.iterdir()) == []


def test_worker_cli_emits_allowed_failure_reason_without_exception_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        worker_module,
        "run_round_trip_learning_worker_once",
        lambda **_: (_ for _ in ()).throw(
            CryptoRoundTripLearningError("round_trip_learning_source_invalid")
        ),
    )

    assert worker_module.main(
        ["--mode", "full-scrub", "--epoch-manifest", str(tmp_path / "epoch.json")]
    ) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "contract": "tradingagent.crypto.round_trip_learning_worker_failure.v1",
        "failure_phase": "full_scrub",
        "failure_reason": "round_trip_learning_source_invalid",
        "status": "failed_closed",
    }
    assert captured.err == "crypto round-trip learning worker failed closed\n"


def test_worker_cli_maps_unexpected_failure_to_generic_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        worker_module,
        "run_round_trip_learning_worker_once",
        lambda **_: (_ for _ in ()).throw(RuntimeError("do not disclose this text")),
    )

    assert worker_module.main(
        ["--mode", "incremental", "--epoch-manifest", str(tmp_path / "epoch.json")]
    ) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["failure_phase"] == "incremental"
    assert json.loads(captured.out)["failure_reason"] == "round_trip_learning_failed"
    assert "do not disclose this text" not in captured.out + captured.err


def test_worker_cli_emits_structured_stage_and_checkpoint_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**kwargs: object) -> None:
        context = kwargs["failure_context"]
        assert isinstance(context, dict)
        context.update(
            {
                "stage": "projection_started",
                "epoch_generation": 5,
                "epoch_manifest_sha256": "a" * 64,
                "projected_completion_count": 792,
                "core_completion_count": 2367,
                "checkpoint_head_sha256": "b" * 64,
                "checkpoint_source_completion_sha256": "c" * 64,
                "checkpoint_projection_receipt_sha256": "d" * 64,
            }
        )
        raise CryptoRoundTripLearningError("round_trip_learning_checkpoint_source_mismatch")

    monkeypatch.setattr(worker_module, "run_round_trip_learning_worker_once", fail)
    assert worker_module.main(
        ["--mode", "full-scrub", "--epoch-manifest", str(tmp_path / "epoch.json")]
    ) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_reason"] == "round_trip_learning_checkpoint_source_mismatch"
    assert payload["stage"] == "projection_started"
    assert payload["projected_completion_count"] == 792
    assert payload["core_completion_count"] == 2367
    assert payload["checkpoint_source_completion_sha256"] == "c" * 64


def test_worker_cli_invalid_arguments_do_not_emit_failure_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        worker_module.main(["--mode", "invalid", "--epoch-manifest", "epoch.json"])

    assert exited.value.code == 2
    assert capsys.readouterr().out == ""


def test_indexed_event_uses_one_verified_ledger_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = CryptoDelayedPaperObservationStore(tmp_path)
    rows = [{"sequence": 1}, {"sequence": 2}]
    calls = 0

    def read_ledger() -> list[dict[str, int]]:
        nonlocal calls
        calls += 1
        return rows

    monkeypatch.setattr(store, "_read_ledger", read_ledger)

    assert store._ledger_event_at_sequence(1) == rows[0]
    assert store._ledger_event_at_sequence(2) == rows[1]
    assert store._ledger_event_at_sequence(1) == rows[0]
    assert calls == 1


def _budgeted_scrub_stubs(
    monkeypatch: pytest.MonkeyPatch, observations: list[str]
) -> None:
    monkeypatch.setattr(
        learning_module,
        "_core_snapshot",
        lambda _: (object(), {"pending": None}, {}),
    )
    monkeypatch.setattr(
        learning_module,
        "_completion_inventory",
        lambda *args, **kwargs: observations,
    )
    monkeypatch.setattr(
        learning_module,
        "_verify_or_project",
        _stub_projection,
    )


def _stub_projection(root: Path, store, observation_id: str, **kwargs) -> dict[str, str]:
    for name, path in learning_module._paths(root, observation_id).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{observation_id}-{name}\n", encoding="utf-8")
    return {
        "observation_id": observation_id,
        "source_completion_sha256": f"source-{observation_id}",
        "projection_receipt_sha256": f"receipt-{observation_id}",
    }


def _learning_files(root: Path) -> dict[str, bytes]:
    learning = root / "evolution" / "round_trip_learning"
    return {
        path.relative_to(learning).as_posix(): path.read_bytes()
        for path in learning.rglob("*")
        if path.is_file() and path.name != ".lock"
    }


def test_full_scrub_budget_stop_is_append_only_and_resume_matches_uninterrupted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observations = ["first", "second"]
    _budgeted_scrub_stubs(monkeypatch, observations)
    clock = iter((0.0, 0.0, 91.0))
    monkeypatch.setattr(learning_module, "monotonic", lambda: next(clock), raising=False)

    interrupted_root = tmp_path / "interrupted"
    interrupted_root.mkdir()
    deferred = run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=interrupted_root
    )

    learning = interrupted_root / "evolution" / "round_trip_learning"
    assert deferred["status"] == "deferred_time_budget"
    assert deferred["projected_completion_count"] == 1
    assert (learning / "checkpoints" / "000000000001.json").is_file()
    assert not (learning / "worker_state.json").exists()
    assert not list((learning / "scrubs").glob("*.json"))

    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0, raising=False)
    resumed = run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=interrupted_root
    )
    assert resumed["status"] == "recovered"

    uninterrupted_root = tmp_path / "uninterrupted"
    uninterrupted_root.mkdir()
    uninterrupted = run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=uninterrupted_root
    )
    assert uninterrupted["status"] == "recovered"
    assert _learning_files(interrupted_root) == _learning_files(uninterrupted_root)


def test_incremental_backlog_is_bounded_and_resume_matches_uninterrupted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observations = ["first", "second", "third"]
    core_count = 1

    def core_snapshot(_root: Path):
        return (
            object(),
            {"pending": None, "completion_count": core_count, "observation_count": core_count},
            {"latest_observation_id": observations[core_count - 1]},
        )

    monkeypatch.setattr(learning_module, "_core_snapshot", core_snapshot)
    monkeypatch.setattr(
        learning_module, "_completion_inventory", lambda *args, **kwargs: observations
    )
    monkeypatch.setattr(learning_module, "_verify_or_project", _stub_projection)
    monkeypatch.setattr(
        learning_module, "ROUND_TRIP_LEARNING_INCREMENTAL_MAX_RECORDS", 1
    )

    interrupted_root = tmp_path / "incremental-interrupted"
    interrupted_root.mkdir()
    monkeypatch.setattr(learning_module, "_completion_inventory", lambda *args, **kwargs: observations[:1])
    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0)
    assert run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=interrupted_root
    )["status"] == "recovered"
    core_count = 3
    monkeypatch.setattr(
        learning_module, "_completion_inventory", lambda *args, **kwargs: observations
    )
    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0)
    first = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=interrupted_root
    )
    assert first["status"] == "backlog_remaining"
    assert first["processed_count"] == 1
    assert first["remaining_backlog_count"] == 1
    assert first["projected_completion_count"] == 2

    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0)
    resumed = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=interrupted_root
    )
    assert resumed["status"] == "projected"
    assert resumed["processed_count"] == 1
    assert resumed["remaining_backlog_count"] == 0

    uninterrupted_root = tmp_path / "incremental-uninterrupted"
    uninterrupted_root.mkdir()
    core_count = 1
    monkeypatch.setattr(
        learning_module, "_completion_inventory", lambda *args, **kwargs: observations[:1]
    )
    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0)
    assert run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=uninterrupted_root
    )["status"] == "recovered"
    core_count = 3
    monkeypatch.setattr(
        learning_module, "_completion_inventory", lambda *args, **kwargs: observations
    )
    monkeypatch.setattr(
        learning_module, "ROUND_TRIP_LEARNING_INCREMENTAL_MAX_RECORDS", 8
    )
    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0)
    uninterrupted = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=uninterrupted_root
    )
    assert uninterrupted["status"] == "projected"
    assert _learning_files(interrupted_root) == _learning_files(uninterrupted_root)


def test_incremental_backlog_preserves_pending_core_deferral(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        learning_module,
        "_core_snapshot",
        lambda _: (object(), {"pending": {"observation_id": "pending"}}, {}),
    )
    result = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=tmp_path
    )
    assert result["status"] == "deferred_core_pending"
    assert result["processed_count"] == 0
    assert result["remaining_backlog_count"] == 0


def test_incremental_fails_closed_for_tampered_prior_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observations = ["first"]
    core_count = 1
    monkeypatch.setattr(
        learning_module,
        "_core_snapshot",
        lambda _: (
            object(),
            {"pending": None, "completion_count": core_count, "observation_count": core_count},
            {"latest_observation_id": observations[-1]},
        ),
    )
    monkeypatch.setattr(
        learning_module, "_completion_inventory", lambda *args, **kwargs: observations
    )
    monkeypatch.setattr(learning_module, "_verify_or_project", _stub_projection)
    root = tmp_path / "tampered-checkpoint"
    root.mkdir()
    assert run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=root
    )["status"] == "recovered"
    checkpoint = next((root / "evolution" / "round_trip_learning" / "checkpoints").glob("*.json"))
    checkpoint.write_text("{}\n", encoding="utf-8")
    with pytest.raises(
        CryptoRoundTripLearningError, match="round_trip_learning_checkpoint_invalid"
    ):
        run_crypto_delayed_paper_round_trip_learning_incremental(output_root=root)


def test_incremental_fails_closed_for_core_regression(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observations = ["first"]
    core_count = 1

    def core_snapshot(_root: Path):
        return (
            object(),
            {"pending": None, "completion_count": core_count, "observation_count": core_count},
            {"latest_observation_id": observations[-1]},
        )

    monkeypatch.setattr(learning_module, "_core_snapshot", core_snapshot)
    monkeypatch.setattr(
        learning_module, "_completion_inventory", lambda *args, **kwargs: observations
    )
    monkeypatch.setattr(learning_module, "_verify_or_project", _stub_projection)
    root = tmp_path / "core-regression"
    root.mkdir()
    assert run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=root
    )["status"] == "recovered"
    core_count = 0
    with pytest.raises(
        CryptoRoundTripLearningError, match="round_trip_learning_core_regressed"
    ):
        run_crypto_delayed_paper_round_trip_learning_incremental(output_root=root)


def test_full_scrub_inventory_budget_stop_writes_no_learning_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "inventory-interrupted"
    root.mkdir()
    completions = root / "completions"
    completions.mkdir()
    for observation_id in ("first", "second"):
        (completions / f"{observation_id}.json").write_text("{}\n", encoding="utf-8")
    store = SimpleNamespace(completions_dir=completions)
    slots = {"first": "2026-08-04T00:00:00Z", "second": "2026-08-04T00:05:00Z"}
    seen: list[str] = []

    monkeypatch.setattr(
        learning_module,
        "_core_snapshot",
        lambda _: (
            store,
            {"pending": None, "completion_count": 2, "observation_count": 2},
            {"latest_observation_id": "second"},
        ),
    )
    monkeypatch.setattr(
        learning_module,
        "_source_record",
        lambda _, observation_id, **__: seen.append(observation_id)
        or {"observation": {"market_slot": slots[observation_id]}},
    )
    monkeypatch.setattr(learning_module, "_verify_or_project", _stub_projection)
    clock = iter((0.0, 0.0, 91.0))
    monkeypatch.setattr(learning_module, "monotonic", lambda: next(clock), raising=False)

    deferred = run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=root)

    assert deferred == learning_module._result(
        status="deferred_inventory_time_budget", inventory_complete=False
    )
    assert len(seen) == 1
    assert seen[0] in ("first", "second")
    assert not (root / "evolution").exists()

    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0, raising=False)
    resumed = run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=root)
    assert resumed["status"] == "recovered"

    uninterrupted_root = tmp_path / "inventory-uninterrupted"
    uninterrupted_root.mkdir()
    completed = run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=uninterrupted_root
    )
    assert completed["status"] == "recovered"
    assert _learning_files(root) == _learning_files(uninterrupted_root)


def test_full_scrub_resume_rejects_drifted_checkpoint_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observations = ["first", "second"]
    drifted = False
    _budgeted_scrub_stubs(monkeypatch, observations)

    def projection(root, store, observation_id, **kwargs) -> dict[str, str]:
        suffix = "-drifted" if drifted and observation_id == "first" else ""
        for name, path in learning_module._paths(root, observation_id).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{observation_id}-{name}\n", encoding="utf-8")
        return {
            "observation_id": observation_id,
            "source_completion_sha256": f"source-{observation_id}{suffix}",
            "projection_receipt_sha256": f"receipt-{observation_id}{suffix}",
        }

    monkeypatch.setattr(learning_module, "_verify_or_project", projection)
    clock = iter((0.0, 0.0, 91.0))
    monkeypatch.setattr(learning_module, "monotonic", lambda: next(clock), raising=False)
    root = tmp_path / "drifted"
    root.mkdir()
    run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=root)

    drifted = True
    monkeypatch.setattr(learning_module, "monotonic", lambda: 0.0, raising=False)
    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_checkpoint_source_mismatch",
    ):
        run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=root)


def test_worker_cli_treats_budget_deferred_as_controlled_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        worker_module,
        "run_round_trip_learning_worker_once",
        lambda **_: learning_module._result(status="deferred_time_budget"),
    )

    assert worker_module.main(
        ["--mode", "full-scrub", "--epoch-manifest", str(tmp_path / "epoch.json")]
    ) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "deferred_time_budget"
    assert captured.err == ""
