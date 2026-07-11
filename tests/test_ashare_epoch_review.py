from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from Ashare import forward_validation
from Ashare.epoch_review import (
    CURRENT_DERIVED_FILES,
    apply_epoch_reset_plan,
    build_epoch_reset_plan,
    validate_review_epoch,
)
from Ashare.formal_close_refresh import run_formal_close_refresh
from Ashare.portfolio_evolution import build_portfolio_evolution, write_portfolio_evolution
from tools.rebuild_current_epoch_reviews import main as rebuild_reviews_main


EPOCH_STATE = {
    "current_epoch_id": 2,
    "capital_cny": 50_000.0,
    "cutover_timestamp": "2026-07-10T20:56:58+00:00",
}


def test_epoch_one_review_is_rejected_after_epoch_two_cutover(tmp_path: Path) -> None:
    payload = {"capital_epoch": 1, "generated_at": "2026-07-10T08:58:08+00:00"}

    valid, reason = validate_review_epoch(
        payload,
        current_epoch_id=2,
        current_cutover_timestamp="2026-07-10T20:56:58+00:00",
    )

    assert valid is False
    assert reason == "capital_epoch_mismatch"


def test_legacy_current_review_is_rejected_after_cutover() -> None:
    valid, reason = validate_review_epoch(
        {"generated_at": "2026-07-10T08:58:08+00:00"},
        current_epoch_id=2,
        current_cutover_timestamp="2026-07-10T20:56:58+00:00",
    )

    assert valid is False
    assert reason == "missing_capital_epoch"


def test_current_epoch_review_at_cutover_is_accepted() -> None:
    valid, reason = validate_review_epoch(
        {
            "capital_epoch": 2,
            "generated_at": "2026-07-10T20:56:58+00:00",
        },
        current_epoch_id=2,
        current_cutover_timestamp="2026-07-10T20:56:58+00:00",
    )

    assert valid is True
    assert reason == "current_epoch"


def test_reset_plan_archives_legacy_reviews_and_bootstraps_empty_current_epoch(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    source = review_dir / "portfolio_evolution_latest.json"
    source.write_text(
        json.dumps({"capital_epoch": 1, "strategy_sample_count": 3}),
        encoding="utf-8",
    )

    plan = build_epoch_reset_plan(review_dir, tmp_path / "archive", EPOCH_STATE)

    assert plan["status"] == "ready"
    assert plan["move_count"] == 1
    assert plan["bootstrap"]["strategy_sample_count"] == 0
    assert plan["bootstrap"]["capital_epoch"] == 2
    assert plan["moves"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert plan["moves"][0]["size"] == source.stat().st_size
    assert plan["moves"][0]["epoch_tagged"] is True


def test_reset_plan_is_read_only_and_missing_files_are_not_errors(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    source = review_dir / "forward_validation.jsonl"
    source.write_text('{"capital_epoch":1}\n', encoding="utf-8")
    before = source.read_bytes()

    plan = build_epoch_reset_plan(review_dir, tmp_path / "archive", EPOCH_STATE)

    assert plan["status"] == "ready"
    assert plan["move_count"] == 1
    assert source.read_bytes() == before
    assert not (tmp_path / "archive").exists()
    assert len(plan["missing_files"]) == len(CURRENT_DERIVED_FILES) - 1


def test_reset_plan_fails_closed_on_destination_collision(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    archive_dir = tmp_path / "archive"
    review_dir.mkdir()
    archive_dir.mkdir()
    (review_dir / "tier_experiments_latest.json").write_text("{}", encoding="utf-8")
    (archive_dir / "tier_experiments_latest.json").write_text("collision", encoding="utf-8")

    plan = build_epoch_reset_plan(review_dir, archive_dir, EPOCH_STATE)

    assert plan["status"] == "error"
    assert plan["reason"] == "destination_collision"


def test_apply_moves_all_stale_files_and_atomically_bootstraps_current_latest_files(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    archive_dir = tmp_path / "archive"
    review_dir.mkdir()
    for name in CURRENT_DERIVED_FILES:
        (review_dir / name).write_text(
            json.dumps({"capital_epoch": 1, "name": name}) + ("\n" if name.endswith(".jsonl") else ""),
            encoding="utf-8",
        )

    plan = build_epoch_reset_plan(review_dir, archive_dir, EPOCH_STATE)
    result = apply_epoch_reset_plan(plan)

    assert result["status"] == "applied"
    assert result["move_count"] == len(CURRENT_DERIVED_FILES)
    assert all((archive_dir / name).exists() for name in CURRENT_DERIVED_FILES)
    assert not (review_dir / "portfolio_evolution_log.jsonl").exists()
    assert not (review_dir / "evolution_decision_log.jsonl").exists()
    portfolio = json.loads((review_dir / "portfolio_evolution_latest.json").read_text(encoding="utf-8"))
    decision = json.loads((review_dir / "evolution_decision_latest.json").read_text(encoding="utf-8"))
    forward = json.loads((review_dir / "forward_validation_latest.json").read_text(encoding="utf-8"))
    tiers = json.loads((review_dir / "tier_experiments_latest.json").read_text(encoding="utf-8"))
    assert portfolio["capital_epoch"] == 2
    assert portfolio["strategy_sample_count"] == 0
    assert portfolio["pnl"]["equity"] == 50_000.0
    assert decision["capital_epoch"] == 2
    assert decision["recommended_action"] == "observe_and_label_candidates"
    assert forward["capital_epoch"] == 2
    assert forward["labels"] == []
    assert tiers["capital_epoch"] == 2
    assert tiers["accounts"] == []


def _strategy_trade(*, trade_id: str, capital_epoch: int | None) -> dict[str, object]:
    trade: dict[str, object] = {
        "trade_id": trade_id,
        "order_id": trade_id,
        "market": "ashare",
        "account": "ashare_server_sim",
        "trade_date": "20260710",
        "ts_code": "600000.SH",
        "side": "buy",
        "quantity": 100,
        "filled_price": 10.0,
        "amount": 1000.0,
        "commission": 5.0,
        "net_amount": 1005.0,
        "status": "filled",
        "candidate_pool_layer": "candidate",
        "execution_source": "ashare_candidate_layer",
        "fill_price_source": "market_snapshot",
        "fill_price_source_class": "verified_5min_market_data",
        "trade_timestamp_bj": "2026-07-10T10:00:00+08:00",
    }
    if capital_epoch is not None:
        trade["capital_epoch"] = capital_epoch
    return trade


def test_portfolio_evolution_excludes_old_epoch_trades_and_tiers(tmp_path: Path) -> None:
    trades = tmp_path / "local_sim_trades.jsonl"
    trades.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                _strategy_trade(trade_id="old", capital_epoch=1),
                _strategy_trade(trade_id="legacy", capital_epoch=None),
                _strategy_trade(trade_id="current", capital_epoch=2),
            )
        ),
        encoding="utf-8",
    )
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "tier_experiments_latest.json").write_text(
        json.dumps({"capital_epoch": 1, "accounts": [{"account": "ashare_200000"}]}),
        encoding="utf-8",
    )

    with patch("Ashare.portfolio_evolution.read_epoch_state", return_value=EPOCH_STATE):
        report = build_portfolio_evolution(
            trade_date="20260710",
            review_dir=review_dir,
            local_trades_path=trades,
            mark_prices={"600000.SH": 10.0},
        )

    assert report["capital_epoch"] == 2
    assert report["capital_cny"] == 50_000.0
    assert report["epoch_cutover_timestamp"] == EPOCH_STATE["cutover_timestamp"]
    assert report["strategy_sample_count"] == 1
    assert report["tier_experiments"]["account_count"] == 0
    assert report["epoch_rejections"] == {
        "capital_epoch_mismatch": 1,
        "missing_capital_epoch": 1,
    }


def test_post_cutover_untagged_trade_is_inferred_as_current_not_legacy(tmp_path: Path) -> None:
    trade = _strategy_trade(trade_id="post-cutover", capital_epoch=None)
    trade["trade_date"] = "20260713"
    trade["trade_timestamp_bj"] = "2026-07-13T10:00:00+08:00"
    trade["created_at"] = "2026-07-13T02:00:00+00:00"
    trades = tmp_path / "local_sim_trades.jsonl"
    trades.write_text(json.dumps(trade) + "\n", encoding="utf-8")

    with patch("Ashare.portfolio_evolution.read_epoch_state", return_value=EPOCH_STATE):
        report = build_portfolio_evolution(
            trade_date="20260713",
            review_dir=tmp_path / "review",
            local_trades_path=trades,
            mark_prices={"600000.SH": 10.0},
        )

    assert report["strategy_sample_count"] == 1
    assert report["epoch_rejections"] == {}


class _ForwardReader:
    def get_bars_intraday(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return [{"bar_time": "2026-07-10 11:00:00", "close": 10.5}]

    def get_bars_daily(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return [{"trade_date": "20260710", "close": 10.5}]


def test_forward_validation_labels_only_current_epoch_trades(tmp_path: Path) -> None:
    trades = tmp_path / "local_sim_trades.jsonl"
    trades.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                _strategy_trade(trade_id="old", capital_epoch=1),
                _strategy_trade(trade_id="current", capital_epoch=2),
            )
        ),
        encoding="utf-8",
    )

    with patch("Ashare.forward_validation.read_epoch_state", return_value=EPOCH_STATE):
        report = forward_validation.build_forward_validation_report(
            date="20260710",
            reader=_ForwardReader(),
            local_trades_path=trades,
            output=None,
            history=None,
        )

    assert report["capital_epoch"] == 2
    assert report["capital_cny"] == 50_000.0
    assert report["epoch_cutover_timestamp"] == EPOCH_STATE["cutover_timestamp"]
    assert report["trade_count"] == 1
    assert [row["trade_id"] for row in report["labels"]] == ["current"]
    assert report["epoch_rejections"] == {"capital_epoch_mismatch": 1}


def test_formal_close_refresh_propagates_epoch_fields_when_no_positions(tmp_path: Path) -> None:
    with patch("Ashare.formal_close_refresh.read_epoch_state", return_value=EPOCH_STATE), patch(
        "Ashare.formal_close_refresh.local_sim_ledger.get_local_sim_pnl",
        return_value={"positions": {}},
    ):
        report = run_formal_close_refresh(
            trade_date="20260710",
            review_dir=tmp_path,
            reader=_ForwardReader(),
        )

    assert report["status"] == "pass"
    assert report["capital_epoch"] == 2
    assert report["capital_cny"] == 50_000.0
    assert report["epoch_cutover_timestamp"] == EPOCH_STATE["cutover_timestamp"]


def test_evolution_decision_writer_persists_all_epoch_fields(tmp_path: Path) -> None:
    trades = tmp_path / "local_sim_trades.jsonl"
    trades.write_text(json.dumps(_strategy_trade(trade_id="current", capital_epoch=2)) + "\n")
    review_dir = tmp_path / "review"

    with patch("Ashare.portfolio_evolution.read_epoch_state", return_value=EPOCH_STATE), patch(
        "Ashare.portfolio_evolution._refresh_local_sim_snapshot_for_review",
        return_value={"status": "skipped", "reason": "no_mark_prices"},
    ):
        write_portfolio_evolution(
            trade_date="20260710",
            review_dir=review_dir,
            local_trades_path=trades,
        )

    decision = json.loads((review_dir / "evolution_decision_latest.json").read_text(encoding="utf-8"))
    assert decision["capital_epoch"] == 2
    assert decision["capital_cny"] == 50_000.0
    assert decision["epoch_cutover_timestamp"] == EPOCH_STATE["cutover_timestamp"]


def test_rebuild_cli_defaults_to_dry_run_without_writes(tmp_path: Path, capsys: object) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    stale = review_dir / "portfolio_evolution_latest.json"
    stale.write_text(json.dumps({"capital_epoch": 1}), encoding="utf-8")

    with patch("tools.rebuild_current_epoch_reviews.read_epoch_state", return_value=EPOCH_STATE):
        exit_code = rebuild_reviews_main(
            [
                "--review-dir",
                str(review_dir),
                "--archive-dir",
                str(tmp_path / "archive"),
                "--pretty",
            ]
        )

    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert exit_code == 0
    assert output["status"] == "dry_run"
    assert output["plan"]["move_count"] == 1
    assert stale.exists()
    assert not (tmp_path / "archive").exists()


def test_rebuild_cli_is_directly_executable() -> None:
    root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, str(root / "tools" / "rebuild_current_epoch_reviews.py"), "--help"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout
    assert "--apply" in result.stdout
