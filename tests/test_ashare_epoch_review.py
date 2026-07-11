from __future__ import annotations

import hashlib
import json
import copy
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from Ashare import forward_validation
from Ashare.epoch_review import (
    CURRENT_DERIVED_FILES,
    apply_epoch_reset_plan,
    build_epoch_reset_plan,
    validate_current_review_set,
    validate_review_epoch,
)
from Ashare.formal_close_refresh import run_formal_close_refresh
from Ashare.portfolio_evolution import build_portfolio_evolution, write_portfolio_evolution
from Ashare.sample_learning import write_sample_learning_report
from Ashare.sample_target_monitor import write_sample_target_monitor
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
    assert (review_dir / "portfolio_evolution_log.jsonl").exists()
    assert (review_dir / "evolution_decision_log.jsonl").exists()
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


def test_post_cutover_untagged_trade_is_rejected_from_current_review(tmp_path: Path) -> None:
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

    assert report["strategy_sample_count"] == 0
    assert report["epoch_rejections"] == {"missing_capital_epoch": 1}


def test_current_derived_file_set_includes_monitor_and_formal_close() -> None:
    assert "sample_target_monitor_latest.json" in CURRENT_DERIVED_FILES
    assert "sample_target_monitor_log.jsonl" in CURRENT_DERIVED_FILES
    assert "formal_close_latest.json" in CURRENT_DERIVED_FILES
    assert "formal_close_history.jsonl" in CURRENT_DERIVED_FILES


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


def test_portfolio_ignores_stale_epoch_forward_labels(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "forward_validation_latest.json").write_text(
        json.dumps(
            {
                "capital_epoch": 1,
                "generated_at": "2026-07-10T08:00:00+00:00",
                "labels": [{"labels": {"m60": {"status": "labeled"}}}],
            }
        ),
        encoding="utf-8",
    )
    trades = tmp_path / "local_sim_trades.jsonl"
    trades.write_text(json.dumps(_strategy_trade(trade_id="current", capital_epoch=2)) + "\n")

    with patch("Ashare.portfolio_evolution.read_epoch_state", return_value=EPOCH_STATE):
        report = build_portfolio_evolution(
            trade_date="20260710",
            review_dir=review_dir,
            local_trades_path=trades,
            mark_prices={"600000.SH": 10.0},
        )

    assert report["evolution_evidence"]["forward_label_count"] == 0
    assert "no_forward_validation_labels" in report["evolution_evidence"]["blockers"]


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


def test_apply_reset_plan_is_idempotent_noop(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "portfolio_evolution_latest.json").write_text(
        json.dumps({"capital_epoch": 1}), encoding="utf-8"
    )
    plan = build_epoch_reset_plan(review_dir, tmp_path / "archive", EPOCH_STATE)

    first = apply_epoch_reset_plan(plan)
    second = apply_epoch_reset_plan(plan)

    assert first["status"] == "applied"
    assert second["status"] == "already_applied"
    assert json.loads((review_dir / "portfolio_evolution_latest.json").read_text())["capital_epoch"] == 2


def test_apply_reset_plan_does_not_claim_idempotency_when_current_log_is_missing(
    tmp_path: Path,
) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "portfolio_evolution_latest.json").write_text(
        json.dumps({"capital_epoch": 1}), encoding="utf-8"
    )
    plan = build_epoch_reset_plan(review_dir, tmp_path / "archive", EPOCH_STATE)
    assert apply_epoch_reset_plan(plan)["status"] == "applied"
    (review_dir / "portfolio_evolution_log.jsonl").unlink()

    second = apply_epoch_reset_plan(plan)

    assert second["status"] != "already_applied"


def test_apply_reset_plan_rejects_forged_bootstrap_before_any_write(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    source = review_dir / "portfolio_evolution_latest.json"
    source.write_bytes(b'{"capital_epoch":1}\n')
    original = source.read_bytes()
    archive_dir = tmp_path / "archive"
    plan = build_epoch_reset_plan(review_dir, archive_dir, EPOCH_STATE)
    plan["latest_bootstraps"]["portfolio_evolution_latest.json"]["capital_cny"] = 200_000.0

    result = apply_epoch_reset_plan(plan)

    assert result == {"status": "error", "reason": "invalid_plan_digest"}
    assert source.read_bytes() == original
    assert not archive_dir.exists()


def test_apply_reset_plan_rejects_any_immutable_payload_tamper_before_write(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    archive_dir = tmp_path / "archive"
    review_dir.mkdir()
    source = review_dir / "portfolio_evolution_latest.json"
    source.write_bytes(b'{"capital_epoch":1}\n')
    original = source.read_bytes()
    plan = build_epoch_reset_plan(review_dir, archive_dir, EPOCH_STATE)
    forged_root = tmp_path / "forged-root"
    forged_root.mkdir()

    mutations = (
        lambda candidate: candidate.update({"review_dir": str(tmp_path / "other-review")}),
        lambda candidate: candidate.update({"archive_dir": str(tmp_path / "other-archive")}),
        lambda candidate: candidate.update({"allowed_root": str(forged_root)}),
        lambda candidate: candidate.update({"missing_files": []}),
        lambda candidate: candidate["moves"][0].update(
            {"destination": str(tmp_path / "within-root-forged-destination.json")}
        ),
        lambda candidate: candidate["moves"][0].update({"source": str(source.parent / "other.json")}),
    )
    for mutate in mutations:
        forged = copy.deepcopy(plan)
        mutate(forged)
        result = apply_epoch_reset_plan(forged)
        assert result == {"status": "error", "reason": "invalid_plan_digest"}
        assert source.read_bytes() == original
        assert not archive_dir.exists()
        assert not (tmp_path / "within-root-forged-destination.json").exists()


def test_current_review_set_requires_all_latest_and_logs_with_exact_authority(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "portfolio_evolution_latest.json").write_text(
        json.dumps({"capital_epoch": 1}), encoding="utf-8"
    )
    plan = build_epoch_reset_plan(review_dir, tmp_path / "archive", EPOCH_STATE)
    assert apply_epoch_reset_plan(plan)["status"] == "applied"

    current = validate_current_review_set(review_dir, EPOCH_STATE)
    assert current["status"] == "current"
    assert current["checked_file_count"] == len(CURRENT_DERIVED_FILES)

    log_path = review_dir / "portfolio_evolution_log.jsonl"
    log_payload = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    log_payload["capital_cny"] = 200_000.0
    log_path.write_text(json.dumps(log_payload) + "\n", encoding="utf-8")
    mismatch = validate_current_review_set(review_dir, EPOCH_STATE)
    assert mismatch["status"] == "stale_or_missing"
    assert {item["reason"] for item in mismatch["issues"]} == {"capital_cny_mismatch"}

    log_payload["capital_cny"] = 50_000.0
    log_payload["epoch_cutover_timestamp"] = "2026-07-11T04:56:58+08:00"
    log_path.write_text(json.dumps(log_payload) + "\n", encoding="utf-8")
    timezone_alias = validate_current_review_set(review_dir, EPOCH_STATE)
    assert timezone_alias["status"] == "stale_or_missing"
    assert {item["reason"] for item in timezone_alias["issues"]} == {
        "epoch_cutover_timestamp_mismatch"
    }

    log_path.unlink()
    missing = validate_current_review_set(review_dir, EPOCH_STATE)
    assert missing["status"] == "stale_or_missing"
    assert {item["reason"] for item in missing["issues"]} == {"missing_current_review"}


def test_rebuilt_plan_after_apply_is_already_applied_noop(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "portfolio_evolution_latest.json").write_text(
        json.dumps({"capital_epoch": 1}), encoding="utf-8"
    )
    archive_dir = tmp_path / "archive"
    first_plan = build_epoch_reset_plan(review_dir, archive_dir, EPOCH_STATE)
    assert apply_epoch_reset_plan(first_plan)["status"] == "applied"

    second_plan = build_epoch_reset_plan(review_dir, archive_dir, EPOCH_STATE)
    second = apply_epoch_reset_plan(second_plan)

    assert second_plan["status"] == "already_applied"
    assert second["status"] == "already_applied"


def test_reset_plan_rejects_symlinked_review_root_escape(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed_root.mkdir()
    outside.mkdir()
    (outside / "portfolio_evolution_latest.json").write_text("{}", encoding="utf-8")
    review_link = allowed_root / "review"
    review_link.symlink_to(outside, target_is_directory=True)
    state = {**EPOCH_STATE, "allowed_root": str(allowed_root)}

    plan = build_epoch_reset_plan(review_link, allowed_root / "archive", state)

    assert plan["status"] == "error"
    assert plan["reason"] == "unsafe_path"


def test_reset_plan_rejects_symlinked_derived_file(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    review_dir = allowed_root / "review"
    archive_dir = allowed_root / "archive"
    outside = tmp_path / "outside.json"
    review_dir.mkdir(parents=True)
    outside.write_text("{}", encoding="utf-8")
    (review_dir / "portfolio_evolution_latest.json").symlink_to(outside)
    state = {**EPOCH_STATE, "allowed_root": str(allowed_root)}

    plan = build_epoch_reset_plan(review_dir, archive_dir, state)

    assert plan["status"] == "error"
    assert plan["reason"] == "unsafe_path"


def test_apply_reset_reports_blocked_when_rollback_action_fails(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    source = review_dir / "portfolio_evolution_latest.json"
    source.write_text(json.dumps({"capital_epoch": 1}), encoding="utf-8")
    plan = build_epoch_reset_plan(review_dir, tmp_path / "archive", EPOCH_STATE)
    real_replace = __import__("os").replace
    calls = {"count": 0}

    def fail_restore(src: str, dst: str) -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("restore blocked")
        real_replace(src, dst)

    with patch("Ashare.epoch_review._atomic_write_json", side_effect=OSError("bootstrap failed")), patch(
        "Ashare.epoch_review.os.replace", side_effect=fail_restore
    ):
        result = apply_epoch_reset_plan(plan)

    assert result["status"] == "blocked"
    assert result["rollback_errors"]
    assert any(item["action"] == "restore_review_file" for item in result["rollback_errors"])


def test_apply_reset_audits_archive_cleanup_inspection_failure(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "portfolio_evolution_latest.json").write_text(
        json.dumps({"capital_epoch": 1}), encoding="utf-8"
    )
    archive_dir = tmp_path / "archive"
    plan = build_epoch_reset_plan(review_dir, archive_dir, EPOCH_STATE)
    real_iterdir = Path.iterdir

    def fail_archive_iterdir(path: Path):
        if path == archive_dir:
            raise OSError("archive inspection denied")
        return real_iterdir(path)

    with patch("Ashare.epoch_review._atomic_write_json", side_effect=OSError("bootstrap failed")), patch(
        "pathlib.Path.iterdir", side_effect=fail_archive_iterdir, autospec=True
    ):
        result = apply_epoch_reset_plan(plan)

    assert result["status"] == "blocked"
    assert any(item["action"] == "remove_empty_archive_dir" for item in result["rollback_errors"])


def test_next_monitor_and_learning_round_preserves_current_epoch_after_reset(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "portfolio_evolution_latest.json").write_text(
        json.dumps({"capital_epoch": 1}), encoding="utf-8"
    )
    plan = build_epoch_reset_plan(review_dir, tmp_path / "archive", EPOCH_STATE)
    assert apply_epoch_reset_plan(plan)["status"] == "applied"
    trades = tmp_path / "local_sim_trades.jsonl"
    trades.write_text("", encoding="utf-8")

    with patch("Ashare.sample_target_monitor.read_epoch_state", return_value=EPOCH_STATE):
        monitor = write_sample_target_monitor(
            review_dir=review_dir,
            now=datetime.fromisoformat("2026-07-11T11:45:00+08:00"),
        )
    with patch("Ashare.sample_learning.read_epoch_state", return_value=EPOCH_STATE):
        learning = write_sample_learning_report(
            trade_date="20260711",
            review_dir=review_dir,
            local_trades_path=trades,
        )

    for payload in (monitor, learning):
        valid, reason = validate_review_epoch(
            payload,
            current_epoch_id=2,
            current_cutover_timestamp=EPOCH_STATE["cutover_timestamp"],
        )
        assert valid, reason
        assert payload["capital_cny"] == 50_000.0
