from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from Ashare.evolution_controller import write_evolution_decision
from Ashare.formal_close_refresh import run_formal_close_refresh
from Ashare.forward_validation import build_forward_validation_report
from Ashare.portfolio_evolution import write_portfolio_evolution
from Ashare.sample_learning import write_sample_learning_report
from Ashare.sample_target_monitor import write_sample_target_monitor
from tools.rebuild_current_epoch_reviews import main as rebuild_reviews_main


VALID_EPOCH_STATE = {
    "current_epoch_id": 2,
    "capital_cny": 50_000.0,
    "cutover_timestamp": "2026-07-10T20:56:58+00:00",
}

INVALID_EPOCH_STATES = (
    {
        "capital_cny": 50_000.0,
        "cutover_timestamp": "2026-07-10T20:56:58+00:00",
    },
    {
        "current_epoch_id": 2,
        "cutover_timestamp": "2026-07-10T20:56:58+00:00",
    },
    {"current_epoch_id": 2, "capital_cny": 50_000.0},
    {
        "current_epoch_id": 1,
        "capital_cny": 200_000.0,
        "cutover_timestamp": "2026-07-10T20:56:58+00:00",
    },
    {
        "current_epoch_id": 2,
        "capital_cny": 50_000.0,
        "cutover_timestamp": "2026-07-10T20:56:58",
    },
    {
        "current_epoch_id": 2,
        "capital_cny": 200_000.0,
        "cutover_timestamp": "2026-07-10T20:56:58+00:00",
    },
)


def _seed(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = b'{"sentinel":"must-not-change"}\n'
    path.write_bytes(value)
    return value


@pytest.mark.parametrize("epoch_state", INVALID_EPOCH_STATES)
def test_portfolio_writer_rejects_invalid_authoritative_metadata_without_overwrite(
    tmp_path: Path,
    epoch_state: dict[str, object],
) -> None:
    review_dir = tmp_path / "review"
    latest = review_dir / "portfolio_evolution_latest.json"
    before = _seed(latest)

    with patch("Ashare.portfolio_evolution.read_epoch_state", return_value=epoch_state):
        with pytest.raises(ValueError, match="invalid_epoch_state"):
            write_portfolio_evolution(review_dir=review_dir, local_trades_path=tmp_path / "trades.jsonl")

    assert latest.read_bytes() == before
    assert not (review_dir / "portfolio_evolution_log.jsonl").exists()
    assert not (review_dir / "evolution_decision_latest.json").exists()


@pytest.mark.parametrize("epoch_state", INVALID_EPOCH_STATES)
def test_evolution_writer_rejects_invalid_authoritative_metadata_without_overwrite(
    tmp_path: Path,
    epoch_state: dict[str, object],
) -> None:
    review_dir = tmp_path / "review"
    latest = review_dir / "evolution_decision_latest.json"
    before = _seed(latest)

    with patch("Ashare.evolution_controller.read_epoch_state", return_value=epoch_state):
        with pytest.raises(ValueError, match="invalid_epoch_state"):
            write_evolution_decision({}, review_dir=review_dir)

    assert latest.read_bytes() == before
    assert not (review_dir / "evolution_decision_log.jsonl").exists()


@pytest.mark.parametrize("epoch_state", INVALID_EPOCH_STATES)
def test_forward_writer_rejects_invalid_authoritative_metadata_without_overwrite(
    tmp_path: Path,
    epoch_state: dict[str, object],
) -> None:
    output = tmp_path / "forward_validation_latest.json"
    history = tmp_path / "forward_validation.jsonl"
    before = _seed(output)

    with patch("Ashare.forward_validation.read_epoch_state", return_value=epoch_state):
        with pytest.raises(ValueError, match="invalid_epoch_state"):
            build_forward_validation_report(output=output, history=history)

    assert output.read_bytes() == before
    assert not history.exists()


@pytest.mark.parametrize("epoch_state", INVALID_EPOCH_STATES)
def test_learning_writer_rejects_invalid_authoritative_metadata_without_overwrite(
    tmp_path: Path,
    epoch_state: dict[str, object],
) -> None:
    review_dir = tmp_path / "review"
    latest = review_dir / "sample_learning_latest.json"
    before = _seed(latest)

    with patch("Ashare.sample_learning.read_epoch_state", return_value=epoch_state):
        with pytest.raises(ValueError, match="invalid_epoch_state"):
            write_sample_learning_report(review_dir=review_dir)

    assert latest.read_bytes() == before
    assert not (review_dir / "sample_learning_log.jsonl").exists()


@pytest.mark.parametrize("epoch_state", INVALID_EPOCH_STATES)
def test_monitor_writer_rejects_invalid_authoritative_metadata_without_overwrite(
    tmp_path: Path,
    epoch_state: dict[str, object],
) -> None:
    review_dir = tmp_path / "review"
    latest = review_dir / "sample_target_monitor_latest.json"
    before = _seed(latest)

    with patch("Ashare.sample_target_monitor.read_epoch_state", return_value=epoch_state):
        with pytest.raises(ValueError, match="invalid_epoch_state"):
            write_sample_target_monitor(
                review_dir=review_dir,
                now=datetime(2026, 7, 11, 11, 45, tzinfo=timezone.utc),
            )

    assert latest.read_bytes() == before
    assert not (review_dir / "sample_target_monitor_log.jsonl").exists()


@pytest.mark.parametrize("epoch_state", INVALID_EPOCH_STATES)
def test_formal_close_rejects_invalid_authoritative_metadata_without_overwrite(
    tmp_path: Path,
    epoch_state: dict[str, object],
) -> None:
    review_dir = tmp_path / "review"
    latest = review_dir / "formal_close_latest.json"
    before = _seed(latest)

    with patch("Ashare.formal_close_refresh.read_epoch_state", return_value=epoch_state), patch(
        "Ashare.formal_close_refresh.local_sim_ledger.get_local_sim_pnl"
    ) as pnl:
        with pytest.raises(ValueError, match="invalid_epoch_state"):
            run_formal_close_refresh(review_dir=review_dir)

    assert latest.read_bytes() == before
    assert not (review_dir / "formal_close_history.jsonl").exists()
    pnl.assert_not_called()


@pytest.mark.parametrize("epoch_state", INVALID_EPOCH_STATES)
def test_rebuild_tool_rejects_invalid_authoritative_metadata_without_overwrite(
    tmp_path: Path,
    epoch_state: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    review_dir = tmp_path / "review"
    latest = review_dir / "portfolio_evolution_latest.json"
    before = _seed(latest)

    with patch("tools.rebuild_current_epoch_reviews.read_epoch_state", return_value=epoch_state):
        exit_code = rebuild_reviews_main(
            [
                "--apply",
                "--review-dir",
                str(review_dir),
                "--archive-dir",
                str(tmp_path / "archive"),
            ]
        )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["reason"].startswith("invalid_epoch_state")
    assert latest.read_bytes() == before
    assert not (tmp_path / "archive").exists()
