from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_round_trip_report import (
    CryptoRoundTripReportError,
    build_crypto_delayed_paper_round_trip_report,
    evaluate_crypto_delayed_paper_round_trip_acceptance,
    main,
    _slot_summary,
    run_crypto_delayed_paper_round_trip_acceptance_once,
)
from Crypto.five_minute_data import TradingDatasCryptoFiveMinuteDataPort
from Crypto.round_trip_capital import CryptoRoundTripError, RoundTripCapitalLedger
from tests.test_crypto_5m_support import (
    FixtureTradingDatasTransport,
    WINDOW_END,
    client,
    profile,
    window_request,
)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
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


def test_round_trip_report_is_read_only_and_separates_kpi_layers(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    before = _tree_bytes(tmp_path)

    report = build_crypto_delayed_paper_round_trip_report(
        output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
    )

    assert _tree_bytes(tmp_path) == before
    assert report["service_reliability"]["completion_count"] == 1
    assert report["service_reliability"]["continuous"] is True
    assert report["service_reliability"]["latest_continuous_completion_count"] == 1
    assert report["audited_samples"]["verified_decision_events"] == 2
    assert report["audited_samples"]["completed_round_trip_count"] == 0
    assert report["simulated_capital_only"]["balanced"] is True
    assert report["simulated_capital_only"]["not_strategy_edge"] is True
    assert report["strategy_assessment"]["status"] == "not_assessed"
    assert report["execution_authority"] is False


def test_acceptance_is_not_ready_before_24_hour_evidence(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)

    result = evaluate_crypto_delayed_paper_round_trip_acceptance(
        output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
    )

    assert result["status"] == "not_ready"
    assert "insufficient_completed_5m_windows" in result["gate_reason_codes"]
    assert result["learning_timer_enable_authorized"] is False
    assert result["next_action"] == "continue_core_accumulation"


def test_latest_continuous_streak_ignores_old_epoch_gaps() -> None:
    slots = [
        WINDOW_END - timedelta(minutes=25),
        WINDOW_END - timedelta(minutes=20),
        WINDOW_END - timedelta(minutes=5),
        WINDOW_END,
    ]

    summary = _slot_summary(slots)

    assert summary["continuous"] is False
    assert summary["latest_continuous_completion_count"] == 2
    assert summary["latest_continuous_covered_minutes"] == 10
    assert summary["latest_continuous_first_market_slot"] == (
        WINDOW_END - timedelta(minutes=5)
    ).isoformat().replace("+00:00", "Z")


def test_acceptance_can_be_eligible_without_using_pnl_as_a_gate(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)

    result = evaluate_crypto_delayed_paper_round_trip_acceptance(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=5),
        minimum_completion_count=1,
    )

    assert result["status"] == "eligible"
    assert result["learning_timer_enable_authorized"] is False
    assert result["next_action"] == "run_disabled_full_scrub_then_idempotent_replay"


def test_report_fails_closed_on_capital_event_tamper(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    events = tmp_path / "round_trip_capital" / "events.jsonl"
    events.write_bytes(events.read_bytes() + b"{}\n")
    before = _tree_bytes(tmp_path)

    with pytest.raises(
        CryptoRoundTripReportError, match="round_trip_report_source_invalid"
    ):
        build_crypto_delayed_paper_round_trip_report(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )

    assert _tree_bytes(tmp_path) == before


def test_round_trip_capital_events_read_only_requires_existing_lock(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    lock = tmp_path / "round_trip_capital" / ".lock"
    lock.unlink()

    with pytest.raises(
        CryptoRoundTripError, match="round_trip_readonly_lock_unavailable"
    ):
        RoundTripCapitalLedger(tmp_path / "round_trip_capital").events_read_only()

    assert not lock.exists()


def test_acceptance_runner_rejects_free_manifest_path(tmp_path: Path) -> None:
    with pytest.raises(
        CryptoRoundTripReportError, match="round_trip_report_manifest_path_invalid"
    ):
        run_crypto_delayed_paper_round_trip_acceptance_once(
            epoch_manifest=tmp_path / "g4.json"
        )


def test_module_cli_executes_the_fail_closed_acceptance_path(tmp_path: Path) -> None:
    assert main(["--epoch-manifest", str(tmp_path / "g4.json")]) == 2
