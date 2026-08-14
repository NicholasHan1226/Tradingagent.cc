from __future__ import annotations

import json
from pathlib import Path

import pytest

import Crypto.factor_strategy_post_projection as post
from Crypto.factor_strategy_post_projection import (
    CryptoFactorStrategyPostProjectionError,
    run_factor_strategy_post_projection,
)


def _outcome(marker: str) -> dict[str, object]:
    return {
        "completion_sha256": marker * 64,
        "outcome_sha256": ("b" if marker == "a" else "d") * 64,
        "evaluation_as_of": "2026-08-14T00:00:00Z",
        "samples": [{"bound": marker}],
    }


def _evaluation(*, strategy_name: str, **_: object) -> dict[str, object]:
    return {
        "strategy_name": strategy_name,
        "evaluation_sha256": strategy_name[0] * 64,
        "recommendation": {"shadow_only_action": "downweight"},
        "authority": "none", "execution_authority": False,
        "promotion_authorized": False, "real_trading_enabled": False,
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "epoch"
    evolution = root / "evolution" / "factor_research"
    evolution.mkdir(parents=True)
    (evolution / ".lock").write_bytes(b"lock\n")
    return root


def test_deterministic_idempotent_and_new_outcome_append(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    inventory = [_outcome("a")]
    monkeypatch.setattr(post, "_inventory", lambda _: list(inventory))
    monkeypatch.setattr(post, "build_factor_strategy_evaluation", _evaluation)

    first = run_factor_strategy_post_projection(output_root=root)
    evaluation_root = root / "evolution" / "factor_research" / "strategy_evaluations"
    first_bytes = {path.name: path.read_bytes() for path in evaluation_root.iterdir()}
    second = run_factor_strategy_post_projection(output_root=root)

    assert first["status"] == "shadow_evaluated"
    assert set(first["evaluations"]) == {"momentum", "trend_pullback", "volume_breakout"}
    assert second["status"] == "no_new_outcome"
    assert first_bytes == {path.name: path.read_bytes() for path in evaluation_root.iterdir()}
    assert len(first_bytes) == 1

    inventory.append(_outcome("c"))
    third = run_factor_strategy_post_projection(output_root=root)
    checkpoint = json.loads(
        (root / "evolution" / "factor_research" / "strategy_evaluation_checkpoint.json").read_text()
    )
    assert third["status"] == "shadow_evaluated"
    assert checkpoint["last_evaluated_completion_sha256"] == "c" * 64
    assert len(list(evaluation_root.glob("*.json"))) == 2
    assert all(value["execution_authority"] is False for value in third["evaluations"].values())


def test_no_resolved_outcome_is_a_write_free_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(post, "_inventory", lambda _: [])

    result = run_factor_strategy_post_projection(output_root=root)

    assert result["status"] == "no_new_outcome"
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_incremental_no_new_outcome_uses_only_compact_checkpoint_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(post, "_inventory", lambda _: [_outcome("a")])
    monkeypatch.setattr(post, "build_factor_strategy_evaluation", _evaluation)
    created = run_factor_strategy_post_projection(output_root=root)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        post,
        "_inventory",
        lambda _: pytest.fail("full strategy inventory ran for unchanged outcome"),
    )

    first = run_factor_strategy_post_projection(
        output_root=root, _resolved_outcome_changed=False
    )
    second = run_factor_strategy_post_projection(
        output_root=root, _resolved_outcome_changed=False
    )

    assert first == second
    assert first["status"] == "no_new_outcome"
    assert first["reason"] == "no_new_resolved_outcome"
    assert first["last_evaluated_outcome_sha256"] == created[
        "last_evaluated_outcome_sha256"
    ]
    assert first["artifact_sha256"] == created["artifact_sha256"]
    assert before == {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_checkpoint_bound_artifact_corruption_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(post, "_inventory", lambda _: [_outcome("a")])
    monkeypatch.setattr(post, "build_factor_strategy_evaluation", _evaluation)
    result = run_factor_strategy_post_projection(output_root=root)
    artifact = (
        root / "evolution" / "factor_research" / "strategy_evaluations"
        / f"{result['last_evaluated_outcome_sha256']}.json"
    )
    artifact.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        post,
        "_inventory",
        lambda _: pytest.fail("full strategy inventory ran before tamper rejection"),
    )

    with pytest.raises(
        CryptoFactorStrategyPostProjectionError,
        match="factor_strategy_artifact_invalid",
    ):
        run_factor_strategy_post_projection(
            output_root=root, _resolved_outcome_changed=False
        )


def test_incremental_no_new_outcome_requires_a_compact_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(
        post,
        "_inventory",
        lambda _: pytest.fail("full strategy inventory ran without a checkpoint"),
    )

    with pytest.raises(
        CryptoFactorStrategyPostProjectionError,
        match="factor_strategy_checkpoint_missing",
    ):
        run_factor_strategy_post_projection(
            output_root=root, _resolved_outcome_changed=False
        )


def test_incremental_no_new_outcome_rejects_a_rehashed_checkpoint_rebinding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(post, "_inventory", lambda _: [_outcome("a")])
    monkeypatch.setattr(post, "build_factor_strategy_evaluation", _evaluation)
    run_factor_strategy_post_projection(output_root=root)
    checkpoint_path = (
        root
        / "evolution"
        / "factor_research"
        / "strategy_evaluation_checkpoint.json"
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["last_evaluated_completion_sha256"] = "f" * 64
    checkpoint.pop("checkpoint_sha256")
    checkpoint["checkpoint_sha256"] = post.projection._sha256(checkpoint)
    checkpoint_path.write_text(
        post.projection._canonical_json(checkpoint) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        post,
        "_inventory",
        lambda _: pytest.fail("full strategy inventory ran before rebinding rejection"),
    )

    with pytest.raises(
        CryptoFactorStrategyPostProjectionError,
        match="factor_strategy_artifact_invalid",
    ):
        run_factor_strategy_post_projection(
            output_root=root, _resolved_outcome_changed=False
        )


def test_evaluation_failure_preserves_projection_and_checkpoint_for_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    projection = root / "evolution" / "factor_research" / "records" / "source.json"
    projection.parent.mkdir()
    projection.write_bytes(b"projection-complete\n")
    monkeypatch.setattr(post, "_inventory", lambda _: [_outcome("a")])
    monkeypatch.setattr(
        post, "build_factor_strategy_evaluation",
        lambda **_: (_ for _ in ()).throw(ValueError("evaluation failed")),
    )

    with pytest.raises(
        CryptoFactorStrategyPostProjectionError,
        match="factor_strategy_evaluation_failed",
    ):
        run_factor_strategy_post_projection(output_root=root)

    assert projection.read_bytes() == b"projection-complete\n"
    assert not (root / "evolution" / "factor_research" / "strategy_evaluation_checkpoint.json").exists()
    assert not (root / "evolution" / "factor_research" / "strategy_evaluations").exists()
