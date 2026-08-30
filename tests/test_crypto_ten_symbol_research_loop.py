"""Tests for the stage-1 detached ten-symbol research-evolution loop."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from Crypto.market_observation import OBSERVATION_SYMBOLS
import Crypto.ten_symbol_factor_prescreen as prescreen
from Crypto.ten_symbol_observation_store import CryptoTenSymbolObservationStore
from Crypto.ten_symbol_research_loop import (
    CHECKPOINT_FILENAME,
    LOOP_CHECKPOINT_CONTRACT,
    LOOP_DIRECTORY_NAME,
    LOOP_STAGE,
    REGISTERED_CANDIDATE_IDS,
    REVIEW_REPORT_CONTRACT,
    CryptoTenSymbolResearchLoopError,
    main,
    run_ten_symbol_research_loop_once,
    run_ten_symbol_research_cycle_once,
    ten_symbol_research_loop_exit_code,
)
from tests.test_crypto_ten_symbol_factor_strategy_evaluation import (
    TREND_EPOCH,
    TrendingFixtureTransport,
)
from tests.test_crypto_ten_symbol_observation_runtime import (
    _assert_recursive_non_authority,
    _factory,
    _run,
    _runtime_paths,
)
from tests.test_crypto_ten_symbol_support import WINDOW_END
from shared.data.sharedsignals_v1 import HTTPResponse


class ResearchLoopTrendingTransport(TrendingFixtureTransport):
    """Trending fixture whose rows are byte-identical across slot windows.

    The base fixture derives ``trade_count`` from the within-window row
    index, so the same bar differs between overlapping 13-bar windows.  The
    research loop merges sidecar windows into one history and fails closed
    on conflicting rows, so this variant pins ``trade_count`` to the bar's
    absolute open_time instead.
    """

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        response = super().__call__(**kwargs)
        body = kwargs.get("json_body")
        if kwargs.get("method") == "GET" or not isinstance(body, dict):
            return response
        if "open_time" not in body.get("filters", {}):
            return response
        for row in response.json_body["data"]:
            open_time = datetime.fromisoformat(
                str(row["open_time"]).replace("Z", "+00:00")
            )
            step = int((open_time - TREND_EPOCH).total_seconds() // 300)
            row["trade_count"] = 10 + step
        return response


def _accumulate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    count: int,
    *,
    start_index: int = 0,
    paths: tuple[Path, Path] | None = None,
) -> Path:
    if paths is None:
        token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    else:
        token_file, output_root = paths
    for index in range(start_index, count):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(ResearchLoopTrendingTransport()),
        )
        assert receipt["status"] == "completed"
    return output_root


def _loop_dir(root: Path) -> Path:
    return root / "evolution" / LOOP_DIRECTORY_NAME


def _report_path(root: Path, report_sha256: str) -> Path:
    return _loop_dir(root) / "reports" / f"{report_sha256}.json"


def _load_report(root: Path, report_sha256: str) -> dict[str, Any]:
    return json.loads(_report_path(root, report_sha256).read_text(encoding="utf-8"))


def _independent_history(
    root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Rebuild the merged bar history straight from the store, test-side."""

    store = CryptoTenSymbolObservationStore(root)
    merged: dict[str, dict[str, dict[str, Any]]] = {
        symbol: {} for symbol in OBSERVATION_SYMBOLS
    }
    for event in store.events_read_only():
        if event["event_type"] not in ("observation", "data_gap"):
            continue
        sidecar = store.read_bars_sidecar(str(event["window_end"]))
        if sidecar is None:
            continue
        for source in sidecar["sources"]:
            for row in source["rows"]:
                merged[source["symbol"]].setdefault(row["open_time"], row)
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, Any] = {}
    for symbol in OBSERVATION_SYMBOLS:
        ordered = [merged[symbol][key] for key in sorted(merged[symbol])]
        validated, gaps = prescreen._validate_history_rows(ordered, symbol=symbol)
        rows_by_symbol[symbol] = validated
        meta[symbol] = {
            "row_count": len(validated),
            "first_open_time": validated[0]["open_time"]
            .isoformat()
            .replace("+00:00", "Z"),
            "last_open_time": validated[-1]["open_time"]
            .isoformat()
            .replace("+00:00", "Z"),
            "gap_count": len(gaps),
        }
    return rows_by_symbol, meta


def test_research_loop_reestimates_registered_hypotheses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 30)

    result = run_ten_symbol_research_loop_once(store_root=root)

    assert result["status"] == "report_written"
    assert result["terminal_slot_count"] == 30
    assert result["eligible_slot_count"] == 30
    assert result["horizon_bars"] == [12, 48, 144, 288]
    assert result["loop_stage"] == LOOP_STAGE
    assert result["automatic_reevaluation"] is True
    assert ten_symbol_research_loop_exit_code(result) == 0
    _assert_recursive_non_authority(result)

    report = _load_report(root, result["report_sha256"])
    assert report["contract"] == REVIEW_REPORT_CONTRACT
    assert report["loop_stage"] == LOOP_STAGE
    assert report["registered_candidate_ids"] == list(REGISTERED_CANDIDATE_IDS)
    assert report["horizon_bars"] == [12, 48, 144, 288]
    assert report["horizon_minutes"] == [60, 240, 720, 1440]
    assert report["stage_boundaries"]["hypothesis_generation"] == (
        "disabled_stage_1_registered_set_only"
    )
    assert report["stage_boundaries"]["scheduler"] == "detached_one_shot_no_systemd"
    assert report["diff_vs_previous"]["status"] == "initial_report"
    assert report["review"]["recommendation"] == "automatic_reevaluation_complete"
    assert all(
        entry["recommendation"] in {"positive_in_sample_only", "nonpositive_in_sample", "auto_retain"}
        and entry["automatic_action"]
        in {
            "retain_for_frozen_forward_validation",
            "do_not_allocate",
            "retain_for_more_evidence",
        }
        for entry in report["review"]["per_candidate"].values()
    )
    assert report["promotion_authorized"] is False
    assert report["automatic_champion_replacement"] is False
    assert report["slow_trend"]["forward"]["net_returns"] is None
    assert report["trial_accounting"]["evaluated_cells_this_run"] > 0
    _assert_recursive_non_authority(report)

    summary = report["metrics_summary"]
    assert set(summary) == set(REGISTERED_CANDIDATE_IDS)
    for candidate_id in REGISTERED_CANDIDATE_IDS:
        assert set(summary[candidate_id]) == {"12", "48", "144", "288"}
    # 30 accumulated slots give a 42-bar merged history; the 12-bar forward
    # label resolves exactly 18 evaluation slots and xs_rs is always in market.
    cell = summary["xs_rs"]["12"]["top_1"]
    assert cell["signal_count"] == 18
    assert cell["universe_count"] == 18
    assert cell["mean_gross"] is not None
    assert cell["mean_net"] is not None
    # The longest horizon cannot resolve any label on 42 bars yet.
    far = summary["xs_rs"]["288"]["top_1"]
    assert far["signal_count"] == 0
    assert far["mean_net"] is None

    # The embedded analysis must equal a direct pre-screen run over the
    # independently rebuilt history: the loop reuses, never re-implements.
    rows_by_symbol, meta = _independent_history(root)
    expected = prescreen.analyze(rows_by_symbol, meta=meta, horizon_bars=12)
    assert report["analyses"]["12"]["candidates"] == expected["candidates"]
    assert report["analyses"]["12"]["cost_policy"] == expected["cost_policy"]
    assert report["source"]["data_window"] == meta


def test_research_loop_rerun_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 30)
    first = run_ten_symbol_research_loop_once(store_root=root)
    report_path = _report_path(root, first["report_sha256"])
    first_bytes = report_path.read_bytes()
    checkpoint_path = _loop_dir(root) / CHECKPOINT_FILENAME
    checkpoint_bytes = checkpoint_path.read_bytes()

    second = run_ten_symbol_research_loop_once(store_root=root)

    assert second["status"] == "no_new_input"
    assert second["report_sha256"] == first["report_sha256"]
    assert second["input_digest"] == first["input_digest"]
    assert ten_symbol_research_loop_exit_code(second) == 0
    assert report_path.read_bytes() == first_bytes
    assert checkpoint_path.read_bytes() == checkpoint_bytes
    assert len(list((_loop_dir(root) / "reports").glob("*.json"))) == 1


def test_research_loop_v2_preserves_unversioned_v1_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 14)
    legacy_root = root / "evolution" / "ten_symbol_research_loop"
    legacy_reports = legacy_root / "reports"
    legacy_reports.mkdir(parents=True)
    legacy_checkpoint = legacy_root / "research_loop_checkpoint.json"
    legacy_report = legacy_reports / ("1" * 64 + ".json")
    legacy_checkpoint.write_bytes(b'{"contract":"v1-evidence"}\n')
    legacy_report.write_bytes(b'{"contract":"v1-report"}\n')
    checkpoint_before = legacy_checkpoint.read_bytes()
    report_before = legacy_report.read_bytes()

    result = run_ten_symbol_research_loop_once(store_root=root)

    assert result["status"] == "report_written"
    assert (_loop_dir(root) / CHECKPOINT_FILENAME).is_file()
    assert legacy_checkpoint.read_bytes() == checkpoint_before
    assert legacy_report.read_bytes() == report_before


def test_v3_preserves_prior_v2_and_does_not_promote_positive_maximum(monkeypatch, tmp_path):
    from Crypto.ten_symbol_research_loop import _candidate_recommendation
    root = _accumulate(monkeypatch, tmp_path, 14)
    old = root / "evolution" / "ten_symbol_research_loop.v2"
    old.mkdir(parents=True)
    checkpoint = old / "research_loop_checkpoint.v2.json"
    checkpoint.write_bytes(b'{"historical_v2":"preserve"}\n')
    before = checkpoint.read_bytes()
    result = run_ten_symbol_research_loop_once(store_root=root)
    assert result["status"] == "report_written"
    assert checkpoint.read_bytes() == before
    assert LOOP_DIRECTORY_NAME.endswith(".v3")
    decision = _candidate_recommendation({"288": {"best": {"non_overlapping_mean_net": "9"}}})
    assert decision == {"recommendation": "positive_in_sample_only", "automatic_action": "retain_for_frozen_forward_validation"}


def test_research_loop_diff_against_previous_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _runtime_paths(monkeypatch, tmp_path)
    root = _accumulate(monkeypatch, tmp_path, 30, paths=paths)
    first = run_ten_symbol_research_loop_once(store_root=root)
    _accumulate(monkeypatch, tmp_path, 40, start_index=30, paths=paths)

    second = run_ten_symbol_research_loop_once(store_root=root)

    assert second["status"] == "report_written"
    assert second["report_sha256"] != first["report_sha256"]
    assert second["terminal_slot_count"] == 40
    report = _load_report(root, second["report_sha256"])
    diff = report["diff_vs_previous"]
    assert diff["status"] == "compared"
    assert diff["previous_report_sha256"] == first["report_sha256"]
    assert diff["previous_input_digest"] == first["input_digest"]

    cell = diff["cells"]["xs_rs"]["12"]["top_1"]
    current = report["metrics_summary"]["xs_rs"]["12"]["top_1"]
    assert cell["change"] == "changed"
    assert cell["previous"]["signal_count"] == 18
    assert cell["signal_count_delta"] == 10
    assert current["signal_count"] == 28
    assert Decimal(str(cell["mean_gross_delta"])) == Decimal(
        str(current["mean_gross"])
    ) - Decimal(str(cell["previous"]["mean_gross"]))
    assert Decimal(str(cell["mean_net_delta"])) == Decimal(
        str(current["mean_net"])
    ) - Decimal(str(cell["previous"]["mean_net"]))
    # The 24h horizon still resolves nothing in either run: unchanged cell.
    far = diff["cells"]["xs_rs"]["288"]["top_1"]
    assert far["change"] == "unchanged"
    assert far["signal_count_delta"] == 0
    assert far["mean_net_delta"] is None

    assert len(list((_loop_dir(root) / "reports").glob("*.json"))) == 2
    # The first report stays immutable and still validates standalone.
    first_report = _load_report(root, first["report_sha256"])
    assert first_report["diff_vs_previous"]["status"] == "initial_report"


def test_research_loop_corrupt_event_chain_fails_closed(
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
        CryptoTenSymbolResearchLoopError, match="research_loop_core_invalid"
    ):
        run_ten_symbol_research_loop_once(store_root=root)
    assert main(["--store-root", str(root)]) == 2


def test_research_loop_checkpoint_tamper_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 14)
    first = run_ten_symbol_research_loop_once(store_root=root)
    assert first["status"] == "report_written"
    checkpoint_path = _loop_dir(root) / CHECKPOINT_FILENAME
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["report_sha256"] = "0" * 64
    checkpoint_path.write_text(
        json.dumps(checkpoint) + "\n", encoding="utf-8"
    )

    with pytest.raises(
        CryptoTenSymbolResearchLoopError,
        match="research_loop_checkpoint_invalid",
    ):
        run_ten_symbol_research_loop_once(store_root=root)


def test_research_loop_missing_sidecar_marks_slot_ineligible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 20)
    store = CryptoTenSymbolObservationStore(root)
    events = store.events_read_only()
    target = events[5]
    sidecar_path = store.bars_sidecar_path(str(target["window_end"]))
    sidecar_path.unlink()

    result = run_ten_symbol_research_loop_once(store_root=root)

    assert result["status"] == "report_written"
    assert result["terminal_slot_count"] == 20
    assert result["eligible_slot_count"] == 19
    assert result["ineligible_slot_count"] == 1
    report = _load_report(root, result["report_sha256"])
    ineligible = [
        unit
        for unit in report["source"]["terminal_units"]
        if not unit["eligible"]
    ]
    assert len(ineligible) == 1
    assert ineligible[0]["observation_id"] == target["event_id"]
    assert ineligible[0]["ineligible_reason"] == "sidecar_missing"


def test_research_loop_cli_and_horizon_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _accumulate(monkeypatch, tmp_path, 14)

    assert main(["--store-root", str(root), "--horizon-bars", "12,48"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "report_written"
    assert payload["horizon_bars"] == [12, 48]
    report = _load_report(root, payload["report_sha256"])
    assert set(report["analyses"]) == {"12", "48"}

    capsys.readouterr()
    assert main(["--store-root", str(root), "--horizon-bars", "13"]) == 2
    assert main(["--store-root", str(root), "--horizon-bars", "48,12"]) == 2
    assert main(["--store-root", str(root), "--horizon-bars", "abc"]) == 2
    assert main(["--store-root", str(tmp_path / "missing-root")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "failed closed" in captured.err


def test_research_loop_empty_store_defers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    assert token_file is not None
    CryptoTenSymbolObservationStore(output_root)

    result = run_ten_symbol_research_loop_once(store_root=output_root)

    assert result["status"] == "deferred_core_pending"
    assert ten_symbol_research_loop_exit_code(result) == 0
    _assert_recursive_non_authority(result)


def test_research_cycle_classifies_without_false_registration_and_replays(monkeypatch, tmp_path):
    import Crypto.ten_symbol_factor_strategy_evaluation as evaluation

    def forbidden(**kwargs):
        pytest.fail("research cycle must not invoke promotion-capable evaluation")

    monkeypatch.setattr(evaluation, "run_ten_symbol_factor_strategy_evaluation", forbidden)
    root = _accumulate(monkeypatch, tmp_path, 14)
    first = run_ten_symbol_research_cycle_once(store_root=root, horizon_bars=(12,))
    assert first["status"] == "cycle_completed"
    assert first["strategy_evaluation_invoked"] is False
    assert first["reevaluated_candidate_ids"] == list(REGISTERED_CANDIDATE_IDS)
    assert first["generated_candidates_evaluated_count"] == 0
    candidates = first["candidate_classification"]
    assert len(candidates) == 23
    assert all(row["evaluation_status"] == "blocked" for row in candidates)
    assert all(not row["evaluated"] and not row["registered_into_prescreen"] for row in candidates)
    assert not set(first["reevaluated_candidate_ids"]) & {row["candidate_id"] for row in candidates}
    _assert_recursive_non_authority(first)
    before = {str(path): path.read_bytes() for path in (root / "evolution").rglob("*.json")}
    second = run_ten_symbol_research_cycle_once(store_root=root, horizon_bars=(12,))
    assert second["hypothesis_generation"]["status"] == "no_new_input"
    assert second["registered_candidate_reevaluation"]["status"] == "no_new_input"
    assert second["candidate_classification"] == candidates
    assert before == {str(path): path.read_bytes() for path in (root / "evolution").rglob("*.json")}


def test_research_cycle_declared_data_is_not_verified_or_evaluated(monkeypatch, tmp_path):
    from Crypto.ten_symbol_hypothesis_generator import DATA_PLANE_MANIFEST_CONTRACT

    root = _accumulate(monkeypatch, tmp_path, 30)
    manifest = tmp_path / "declared.json"
    manifest.write_text(json.dumps({
        "contract": DATA_PLANE_MANIFEST_CONTRACT,
        "planes": {"open_interest_5m": {"status": "available", "sample_count": 50000}},
    }) + "\n")
    result = run_ten_symbol_research_cycle_once(
        store_root=root, horizon_bars=(12,), data_plane_manifest=manifest
    )
    row = next(row for row in result["candidate_classification"] if row["candidate_id"] == "oi_change_rate__l12_t0p005")
    assert row["evaluation_status"] == "pending"
    assert row["available_executor"] == "Crypto.ten_symbol_oi_prescreen.analyze"
    assert row["evaluation_inputs_verified"] is False
    assert row["executor_connected"] is False
    assert row["evaluation_artifact_sha256"] is None
    assert row["registered_into_evaluation"] is False
    assert row["evaluated"] is False


def test_research_cycle_resumes_after_interrupted_downstream_without_rewriting_proposal(monkeypatch, tmp_path):
    import Crypto.ten_symbol_research_loop as loop
    from Crypto.ten_symbol_hypothesis_generator import GENERATOR_DIRECTORY_NAME

    root = _accumulate(monkeypatch, tmp_path, 14)
    original = loop.run_ten_symbol_research_loop_once

    def interrupted(**kwargs):
        raise OSError("test interruption")

    monkeypatch.setattr(loop, "run_ten_symbol_research_loop_once", interrupted)
    with pytest.raises(OSError, match="test interruption"):
        run_ten_symbol_research_cycle_once(store_root=root, horizon_bars=(12,))
    proposal_root = root / "evolution" / GENERATOR_DIRECTORY_NAME
    before = {str(path): path.read_bytes() for path in proposal_root.rglob("*.json")}
    monkeypatch.setattr(loop, "run_ten_symbol_research_loop_once", original)
    result = run_ten_symbol_research_cycle_once(store_root=root, horizon_bars=(12,))
    assert result["status"] == "cycle_completed"
    assert result["hypothesis_generation"]["status"] == "no_new_input"
    assert before == {str(path): path.read_bytes() for path in proposal_root.rglob("*.json")}


def test_research_cycle_integrity_error_is_not_retried_or_rebuilt(monkeypatch, tmp_path):
    import Crypto.ten_symbol_research_loop as loop
    from Crypto.ten_symbol_hypothesis_generator import (
        CHECKPOINT_FILENAME as GENERATOR_CHECKPOINT,
        GENERATOR_DIRECTORY_NAME, CryptoTenSymbolHypothesisGeneratorError,
    )

    root = _accumulate(monkeypatch, tmp_path, 14)
    run_ten_symbol_research_cycle_once(store_root=root, horizon_bars=(12,))
    checkpoint = root / "evolution" / GENERATOR_DIRECTORY_NAME / GENERATOR_CHECKPOINT
    checkpoint.write_text("corrupted\n")
    calls = []
    monkeypatch.setattr(loop, "run_ten_symbol_research_loop_once", lambda **kwargs: calls.append(kwargs))
    for _ in range(2):
        with pytest.raises(CryptoTenSymbolHypothesisGeneratorError):
            run_ten_symbol_research_cycle_once(store_root=root, horizon_bars=(12,))
    assert calls == []
    assert checkpoint.read_text() == "corrupted\n"


def test_research_cycle_cli_is_explicit_and_empty_store_defers(monkeypatch, tmp_path, capsys):
    root = _accumulate(monkeypatch, tmp_path, 14)
    assert main(["--store-root", str(root), "--horizon-bars", "12", "--include-proposals"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "cycle_completed"
    assert main(["--store-root", str(root), "--data-plane-manifest", "not-read.json"]) == 2
    capsys.readouterr()
    fresh = tmp_path / "empty"
    fresh.mkdir()
    monkeypatch.undo()
    _, output_root = _runtime_paths(monkeypatch, fresh)
    CryptoTenSymbolObservationStore(output_root)
    deferred = run_ten_symbol_research_cycle_once(store_root=output_root)
    assert deferred["status"] == "deferred"
    assert deferred["registered_candidate_reevaluation"]["status"] == "not_run"
    assert not (output_root / "evolution").exists()
