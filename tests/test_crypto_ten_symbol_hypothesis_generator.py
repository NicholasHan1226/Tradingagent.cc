"""Tests for the stage-2 detached ten-symbol hypothesis generator."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from Crypto.market_observation import OBSERVATION_SYMBOLS
import Crypto.ten_symbol_factor_prescreen as prescreen
from Crypto.ten_symbol_observation_store import CryptoTenSymbolObservationStore
from Crypto.ten_symbol_hypothesis_generator import (
    B_CLASS_PLANES,
    CHECKPOINT_FILENAME,
    DATA_PLANE_MANIFEST_CONTRACT,
    GENERATION_CONFIG,
    GENERATION_CONFIG_ID,
    GENERATOR_CHECKPOINT_CONTRACT,
    GENERATOR_STAGE,
    PLANES,
    PROPOSAL_CONTRACT,
    REGISTRATION_STATUS,
    CryptoTenSymbolHypothesisGeneratorError,
    expand_candidates,
    main,
    run_ten_symbol_hypothesis_generation_once,
    ten_symbol_hypothesis_generator_exit_code,
    _candidate_feasibility,
    _load_data_plane_manifest,
)
from Crypto.ten_symbol_research_loop import REGISTERED_CANDIDATE_IDS
from tests.test_crypto_ten_symbol_observation_runtime import (
    _assert_recursive_non_authority,
    _runtime_paths,
)
from tests.test_crypto_ten_symbol_research_loop import _accumulate


def _generator_dir(root: Path) -> Path:
    return root / "evolution" / "ten_symbol_hypothesis_generator"


def _proposal_path(root: Path, proposal_sha256: str) -> Path:
    return _generator_dir(root) / "proposals" / f"{proposal_sha256}.json"


def _load_proposal(root: Path, proposal_sha256: str) -> dict[str, Any]:
    return json.loads(
        _proposal_path(root, proposal_sha256).read_text(encoding="utf-8")
    )


def _write_manifest(tmp_path: Path, planes: dict[str, Any]) -> Path:
    path = tmp_path / "data_planes.json"
    path.write_text(
        json.dumps({"contract": DATA_PLANE_MANIFEST_CONTRACT, "planes": planes})
        + "\n",
        encoding="utf-8",
    )
    return path


def _available(sample_count: int) -> dict[str, Any]:
    return {
        "status": "available",
        "sample_count": sample_count,
        "evidence_ref": "test-fixture",
    }


# ---------------------------------------------------------------------------
# Deterministic expansion and frozen-config drift
# ---------------------------------------------------------------------------


def test_expansion_is_deterministic_and_proposal_only() -> None:
    first = expand_candidates()
    second = expand_candidates()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    assert len(first) == 23
    ids = [candidate["candidate_id"] for candidate in first]
    assert len(set(ids)) == len(ids)
    # Proposals never collide with the registered pre-screen / stage-1 sets.
    assert not set(ids) & set(prescreen._CANDIDATE_IDS)
    assert not set(ids) & set(REGISTERED_CANDIDATE_IDS)

    families: dict[str, int] = {}
    for candidate in first:
        families[candidate["family"]] = families.get(candidate["family"], 0) + 1
        assert candidate["registration_status"] == REGISTRATION_STATUS
        assert candidate["registered_into_prescreen"] is False
        assert candidate["registered_into_evaluation"] is False
        assert candidate["evaluation_horizon_bars"] == [12, 48, 144, 288]
        assert candidate["evaluation_horizon_minutes"] == [60, 240, 720, 1440]
        assert candidate["required_evidence"][0]["plane"] == "ohlcv_bars"
        for requirement in candidate["required_evidence"]:
            plane = requirement["plane"]
            assert requirement["dataset_namespace"] == PLANES[plane][
                "dataset_namespace"
            ]
    assert set(families) == {
        "oi_change_rate",
        "price_oi_divergence",
        "oi_weighted_momentum",
        "spread_regime",
        "premium_momentum",
    }
    assert all(count <= 5 for count in families.values())
    # Every hypothesis text is fully rendered (no leftover placeholders).
    assert all("{" not in candidate["hypothesis"] for candidate in first)


def _tampered(**overrides: Any) -> dict[str, Any]:
    config = deepcopy(GENERATION_CONFIG)
    for key, value in overrides.items():
        config[key] = value
    return config


def test_config_drift_fails_closed() -> None:
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(config=_tampered(config_id="someone-elses-config"))
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(config=_tampered(evidence_class="A"))
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(config=_tampered(horizon_bars=[12, 13]))
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(config=_tampered(horizon_bars=[48, 12]))
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(config=_tampered(extra_key=True))
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(config="not-a-mapping")

    family_drift = deepcopy(GENERATION_CONFIG["families"][0])
    family_drift["family_id"] = "unregistered_family"
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(
            config=_tampered(
                families=[family_drift, *deepcopy(GENERATION_CONFIG["families"][1:])]
            )
        )

    too_many = deepcopy(GENERATION_CONFIG["families"][0])
    too_many["parameter_sets"] = too_many["parameter_sets"] * 2
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(
            config=_tampered(
                families=[too_many, *deepcopy(GENERATION_CONFIG["families"][1:])]
            )
        )

    unknown_plane = deepcopy(GENERATION_CONFIG["families"][0])
    unknown_plane["required_planes"] = ["ohlcv_bars", "order_book_depth"]
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(
            config=_tampered(
                families=[unknown_plane, *deepcopy(GENERATION_CONFIG["families"][1:])]
            )
        )

    no_bars = deepcopy(GENERATION_CONFIG["families"][0])
    no_bars["required_planes"] = ["open_interest_5m"]
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(
            config=_tampered(
                families=[no_bars, *deepcopy(GENERATION_CONFIG["families"][1:])]
            )
        )

    placeholder_mismatch = deepcopy(GENERATION_CONFIG["families"][0])
    placeholder_mismatch["parameter_sets"] = [
        {"variant": "l12_t0p005", "parameters": {"lookback_bars": 12}}
    ]
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(
            config=_tampered(
                families=[
                    placeholder_mismatch,
                    *deepcopy(GENERATION_CONFIG["families"][1:]),
                ]
            )
        )

    duplicate_variant = deepcopy(GENERATION_CONFIG["families"][0])
    duplicate_variant["parameter_sets"] = [
        duplicate_variant["parameter_sets"][0],
        duplicate_variant["parameter_sets"][0],
    ]
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(
            config=_tampered(
                families=[
                    duplicate_variant,
                    *deepcopy(GENERATION_CONFIG["families"][1:]),
                ]
            )
        )

    bad_param = deepcopy(GENERATION_CONFIG["families"][0])
    bad_param["parameter_sets"] = [
        {
            "variant": "l12_t0p005",
            "parameters": {"lookback_bars": -12, "oi_change_threshold": "0.005"},
        }
    ]
    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_config_drift",
    ):
        expand_candidates(
            config=_tampered(
                families=[bad_param, *deepcopy(GENERATION_CONFIG["families"][1:])]
            )
        )

    # The shipped frozen config itself always validates.
    assert expand_candidates(config=GENERATION_CONFIG)


# ---------------------------------------------------------------------------
# Feasibility boundaries (pure)
# ---------------------------------------------------------------------------


def _feasibility(
    candidate_id: str,
    *,
    bars: int,
    planes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidate = next(
        entry for entry in expand_candidates() if entry["candidate_id"] == candidate_id
    )
    states: dict[str, dict[str, Any]] = {
        "ohlcv_bars": {"status": "available", "sample_count": bars}
    }
    for plane in B_CLASS_PLANES:
        states[plane] = (planes or {}).get(
            plane, {"status": "unavailable", "sample_count": None}
        )
    return _candidate_feasibility(candidate, states)


def test_feasibility_boundaries() -> None:
    # oi_change_rate__l12_* needs 12 + 13 + 12 = 37 bars and >= 10000 OI rows.
    boundary = _feasibility(
        "oi_change_rate__l12_t0p005",
        bars=37,
        planes={"open_interest_5m": {"status": "available", "sample_count": 10000}},
    )
    assert boundary["status"] == "feasible_for_automatic_scientific_gate"
    assert all(check["ok"] for check in boundary["checks"])

    one_bar_short = _feasibility(
        "oi_change_rate__l12_t0p005",
        bars=36,
        planes={"open_interest_5m": {"status": "available", "sample_count": 10000}},
    )
    assert one_bar_short["status"] == "blocked"
    bars_check = next(
        check for check in one_bar_short["checks"] if check["plane"] == "ohlcv_bars"
    )
    assert bars_check["reason"] == "ohlcv_bars_insufficient_samples"

    one_oi_short = _feasibility(
        "oi_change_rate__l12_t0p005",
        bars=37,
        planes={"open_interest_5m": {"status": "available", "sample_count": 9999}},
    )
    assert one_oi_short["status"] == "blocked"
    oi_check = next(
        check
        for check in one_oi_short["checks"]
        if check["plane"] == "open_interest_5m"
    )
    assert oi_check["reason"] == "open_interest_5m_insufficient_samples"

    # Accumulating is not available: only a declared "available" plane counts.
    accumulating = _feasibility(
        "oi_change_rate__l12_t0p005",
        bars=1000,
        planes={
            "open_interest_5m": {"status": "accumulating", "sample_count": 50000}
        },
    )
    assert accumulating["status"] == "blocked"
    assert accumulating["checks"][1]["reason"] == "open_interest_5m_unavailable"

    undeclared = _feasibility("spread_regime__t1p0_narrow", bars=1000)
    assert undeclared["status"] == "blocked"
    assert undeclared["checks"][1]["reason"] == "realized_spreads_unavailable"

    # spread_regime has no lookback parameter: 13 + 12 = 25 bars minimum and
    # the 12-sample plane minimum mirroring the evaluation day bucket.
    spread_ok = _feasibility(
        "spread_regime__t1p0_narrow",
        bars=25,
        planes={"realized_spreads": {"status": "available", "sample_count": 12}},
    )
    assert spread_ok["status"] == "feasible_for_automatic_scientific_gate"
    spread_short = _feasibility(
        "spread_regime__t1p0_narrow",
        bars=25,
        planes={"realized_spreads": {"status": "available", "sample_count": 11}},
    )
    assert spread_short["status"] == "blocked"


# ---------------------------------------------------------------------------
# Data-plane manifest validation
# ---------------------------------------------------------------------------


def test_data_plane_manifest_validation(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {
            "open_interest_5m": _available(50000),
            "realized_spreads": {"status": "accumulating", "sample_count": 40},
        },
    )
    manifest = _load_data_plane_manifest(path)
    assert manifest["contract"] == DATA_PLANE_MANIFEST_CONTRACT

    def _expect_invalid(planes: Any, *, contract: str = DATA_PLANE_MANIFEST_CONTRACT) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps({"contract": contract, "planes": planes}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(
            CryptoTenSymbolHypothesisGeneratorError,
            match="hypothesis_generator_manifest_invalid",
        ):
            _load_data_plane_manifest(bad)

    _expect_invalid({"ohlcv_bars": _available(100)})  # store-derived only
    _expect_invalid({"unknown_plane": _available(100)})
    _expect_invalid({"open_interest_5m": {"status": "available"}})  # no sample
    _expect_invalid({"open_interest_5m": {"status": "available", "sample_count": -1}})
    _expect_invalid({"open_interest_5m": {"status": "ready", "sample_count": 10}})
    _expect_invalid({"open_interest_5m": {**_available(10), "extra": True}})
    _expect_invalid({"open_interest_5m": {**_available(10), "evidence_sha256": "zz"}})
    _expect_invalid({}, contract="tradingagent.crypto.something_else.v1")

    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_manifest_invalid",
    ):
        _load_data_plane_manifest(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# Store-backed proposal runs
# ---------------------------------------------------------------------------


def test_generator_writes_proposal_and_rerun_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 30)

    result = run_ten_symbol_hypothesis_generation_once(store_root=root)

    assert result["status"] == "proposal_written"
    assert result["candidate_count"] == 23
    assert result["feasible_candidate_count"] == 0
    assert result["terminal_slot_count"] == 30
    assert result["eligible_slot_count"] == 30
    assert result["loop_stage"] == GENERATOR_STAGE
    assert result["manual_review_required"] is False
    assert result["human_approval_required"] is False
    assert result["automatic_scientific_gate_required"] is True
    assert ten_symbol_hypothesis_generator_exit_code(result) == 0
    _assert_recursive_non_authority(result)

    proposal = _load_proposal(root, result["proposal_sha256"])
    assert proposal["contract"] == PROPOSAL_CONTRACT
    assert proposal["event_type"] == "hypothesis_registration_proposal"
    assert proposal["loop_stage"] == GENERATOR_STAGE
    assert proposal["generation_config_id"] == GENERATION_CONFIG_ID
    assert proposal["stage_boundaries"]["registration"] == (
        "manual_only_no_auto_register"
    )
    assert proposal["stage_boundaries"]["evaluation"] == "not_run_by_generator"
    assert proposal["stage_boundaries"]["scheduler"] == "detached_one_shot_no_systemd"
    assert proposal["stage_boundaries"]["promotion"] == "manual_review_only"
    assert proposal["candidate_count"] == 23
    assert proposal["source"]["data_plane_manifest_sha256"] is None
    assert proposal["source"]["plane_states"]["ohlcv_bars"] == {
        "status": "available",
        "sample_count": 42,
        "declaration": "store_derived",
    }
    for plane in B_CLASS_PLANES:
        assert proposal["source"]["plane_states"][plane]["status"] == "unavailable"
    for candidate in proposal["candidates"]:
        assert candidate["registration_status"] == REGISTRATION_STATUS
        assert candidate["registered_into_prescreen"] is False
        assert candidate["registered_into_evaluation"] is False
        # Without a data-plane manifest every B-class candidate is blocked.
        assert candidate["feasibility"]["status"] == "blocked"
    assert proposal["review"]["recommendation"] == "automatic_scientific_gate_pending"
    assert proposal["manual_review_required"] is False
    assert proposal["human_approval_required"] is False
    assert proposal["automatic_scientific_gate_required"] is True
    assert proposal["review"]["registration"] == "not_registered"
    assert set(proposal["review"]["per_candidate"]) == {
        candidate["candidate_id"] for candidate in proposal["candidates"]
    }
    _assert_recursive_non_authority(proposal)

    proposal_path = _proposal_path(root, result["proposal_sha256"])
    proposal_bytes = proposal_path.read_bytes()
    checkpoint_path = _generator_dir(root) / CHECKPOINT_FILENAME
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes.decode("utf-8"))
    assert checkpoint["contract"] == GENERATOR_CHECKPOINT_CONTRACT
    assert checkpoint["proposal_sha256"] == result["proposal_sha256"]

    second = run_ten_symbol_hypothesis_generation_once(store_root=root)

    assert second["status"] == "no_new_input"
    assert second["proposal_sha256"] == result["proposal_sha256"]
    assert second["input_digest"] == result["input_digest"]
    assert ten_symbol_hypothesis_generator_exit_code(second) == 0
    assert proposal_path.read_bytes() == proposal_bytes
    assert checkpoint_path.read_bytes() == checkpoint_bytes
    assert len(list((_generator_dir(root) / "proposals").glob("*.json"))) == 1


def test_generator_manifest_controls_b_class_feasibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 30)
    manifest_path = _write_manifest(
        tmp_path,
        {
            "open_interest_5m": _available(50000),
            "realized_spreads": _available(1000),
            "premium_index": _available(500),
        },
    )

    result = run_ten_symbol_hypothesis_generation_once(
        store_root=root, data_plane_manifest=manifest_path
    )

    assert result["status"] == "proposal_written"
    proposal = _load_proposal(root, result["proposal_sha256"])
    feasibility = {
        candidate["candidate_id"]: candidate["feasibility"]["status"]
        for candidate in proposal["candidates"]
    }
    # 42 merged bars: lookback-12 candidates clear the 37-bar minimum,
    # longer lookbacks stay blocked on the bars plane.
    expected_feasible = {
        "oi_change_rate__l12_t0p005",
        "oi_change_rate__l12_t0p01",
        "price_oi_divergence__l12_fade",
        "price_oi_divergence__l12_follow",
        "oi_weighted_momentum__m12_o12_k2",
        "spread_regime__t1p0_narrow",
        "spread_regime__t1p5_narrow",
        "spread_regime__t2p0_narrow",
        "spread_regime__t1p5_wide",
        "premium_momentum__l12_t0p0005",
    }
    assert {
        candidate_id
        for candidate_id, status in feasibility.items()
        if status == "feasible_for_automatic_scientific_gate"
    } == expected_feasible
    assert result["feasible_candidate_count"] == len(expected_feasible)
    blocked_bars = feasibility["oi_change_rate__l48_t0p01"]
    assert blocked_bars == "blocked"
    candidate = next(
        entry
        for entry in proposal["candidates"]
        if entry["candidate_id"] == "oi_change_rate__l48_t0p01"
    )
    bars_check = next(
        check
        for check in candidate["feasibility"]["checks"]
        if check["plane"] == "ohlcv_bars"
    )
    assert bars_check["reason"] == "ohlcv_bars_insufficient_samples"
    assert bars_check["min_sample_count"] == 73
    assert bars_check["observed_sample_count"] == 42
    assert proposal["source"]["data_plane_manifest_sha256"] is not None
    assert (
        proposal["source"]["plane_states"]["open_interest_5m"]["sample_count"]
        == 50000
    )


def test_generator_new_input_writes_new_immutable_proposal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _runtime_paths(monkeypatch, tmp_path)
    root = _accumulate(monkeypatch, tmp_path, 30, paths=paths)
    first = run_ten_symbol_hypothesis_generation_once(store_root=root)
    _accumulate(monkeypatch, tmp_path, 34, start_index=30, paths=paths)

    second = run_ten_symbol_hypothesis_generation_once(store_root=root)

    assert second["status"] == "proposal_written"
    assert second["proposal_sha256"] != first["proposal_sha256"]
    assert second["terminal_slot_count"] == 34
    assert len(list((_generator_dir(root) / "proposals").glob("*.json"))) == 2
    # The first proposal stays immutable and still parses standalone.
    first_proposal = _load_proposal(root, first["proposal_sha256"])
    assert first_proposal["source"]["terminal_slot_count"] == 30


def test_generator_corrupt_event_chain_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 14)
    events_path = root / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["reason_code"] = "tampered"
    lines[0] = json.dumps(row)
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_core_invalid",
    ):
        run_ten_symbol_hypothesis_generation_once(store_root=root)
    assert main(["--store-root", str(root)]) == 2


def test_generator_checkpoint_tamper_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 14)
    first = run_ten_symbol_hypothesis_generation_once(store_root=root)
    assert first["status"] == "proposal_written"
    checkpoint_path = _generator_dir(root) / CHECKPOINT_FILENAME
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["proposal_sha256"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")

    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_checkpoint_invalid",
    ):
        run_ten_symbol_hypothesis_generation_once(store_root=root)


def test_generator_proposal_tamper_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 14)
    first = run_ten_symbol_hypothesis_generation_once(store_root=root)
    assert first["status"] == "proposal_written"
    proposal_path = _proposal_path(root, first["proposal_sha256"])
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal["candidates"][0]["registration_status"] = "registered"
    proposal_path.write_text(json.dumps(proposal) + "\n", encoding="utf-8")

    with pytest.raises(
        CryptoTenSymbolHypothesisGeneratorError,
        match="hypothesis_generator_proposal_invalid",
    ):
        run_ten_symbol_hypothesis_generation_once(store_root=root)


def test_generator_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 14)

    assert main(["--store-root", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "proposal_written"
    assert payload["candidate_count"] == 23

    assert main(["--store-root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no_new_input"

    capsys.readouterr()
    assert main(["--store-root", str(tmp_path / "missing-root")]) == 2
    assert (
        main(
            [
                "--store-root",
                str(root),
                "--data-plane-manifest",
                str(tmp_path / "missing.json"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "failed closed" in captured.err


def test_generator_empty_store_defers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    assert token_file is not None
    CryptoTenSymbolObservationStore(output_root)

    result = run_ten_symbol_hypothesis_generation_once(store_root=output_root)

    assert result["status"] == "deferred_core_pending"
    assert ten_symbol_hypothesis_generator_exit_code(result) == 0
    _assert_recursive_non_authority(result)
    assert not _generator_dir(output_root).exists()
