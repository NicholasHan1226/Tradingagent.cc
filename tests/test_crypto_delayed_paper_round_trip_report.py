from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import Crypto.delayed_paper_round_trip_report as report_module
import Crypto.delayed_paper_round_trip_runtime as runtime_module
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_round_trip_report import (
    CryptoRoundTripReportError,
    build_crypto_delayed_paper_round_trip_report,
    evaluate_crypto_delayed_paper_round_trip_acceptance,
    main,
    _continuity_segments,
    _manifest_path,
    _runtime_rejects_by_slot,
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
    assert "insufficient_48h_runtime" in result["gate_reason_codes"]
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


def test_continuity_segments_expose_gaps_without_attributing_external_cause() -> None:
    slots = [
        WINDOW_END - timedelta(minutes=25),
        WINDOW_END - timedelta(minutes=20),
        WINDOW_END - timedelta(minutes=5),
        WINDOW_END,
    ]

    result = _continuity_segments(slots)

    assert result["continuous_segment_count"] == 2
    assert result["longest_continuous_completion_count"] == 2
    assert result["segments"] == [
        {
            "first_completed_market_slot": (WINDOW_END - timedelta(minutes=25))
            .isoformat()
            .replace("+00:00", "Z"),
            "latest_completed_market_slot": (WINDOW_END - timedelta(minutes=20))
            .isoformat()
            .replace("+00:00", "Z"),
            "completion_count": 2,
            "covered_minutes": 10,
        },
        {
            "first_completed_market_slot": (WINDOW_END - timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
            "latest_completed_market_slot": WINDOW_END.isoformat().replace(
                "+00:00", "Z"
            ),
            "completion_count": 2,
            "covered_minutes": 10,
        },
    ]
    assert result["gaps"] == [
        {
            "previous_completed_market_slot": (WINDOW_END - timedelta(minutes=20))
            .isoformat()
            .replace("+00:00", "Z"),
            "next_completed_market_slot": (WINDOW_END - timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
            "missing_completion_count": 2,
            "gap_minutes": 10,
            "cause": "unclassified_completion_gap",
        }
    ]


def test_continuity_segments_preserve_runtime_reject_evidence_without_claiming_cause() -> None:
    slots = [WINDOW_END - timedelta(minutes=10), WINDOW_END]

    result = _continuity_segments(
        slots,
        runtime_rejects={
            WINDOW_END - timedelta(minutes=5): ("crypto_5m_metadata_not_ready",)
        },
    )

    assert result["gaps"][0]["cause"] == "unclassified_completion_gap"
    assert result["gaps"][0]["runtime_rejects"] == [
        {
            "market_slot": (WINDOW_END - timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
            "reason_codes": ["crypto_5m_metadata_not_ready"],
        }
    ]


def test_runtime_rejects_bind_only_new_receipts_to_a_missing_slot() -> None:
    store = SimpleNamespace(
        data_reject_events=lambda: [
            {
                "request_window_end": WINDOW_END.isoformat().replace("+00:00", "Z"),
                "reason_code": "crypto_5m_metadata_not_ready",
            },
            {"reason_code": "legacy_receipt_without_window"},
        ]
    )

    assert _runtime_rejects_by_slot(store) == {
        WINDOW_END - timedelta(minutes=5): ("crypto_5m_metadata_not_ready",)
    }


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


def test_acceptance_counts_explicit_data_gaps_as_observed_coverage(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    store = runtime_module.CryptoDelayedPaperObservationStore(tmp_path)
    store.append_event(
        runtime_module._round_trip_data_gap_event(
            prior_market_slot=WINDOW_END - timedelta(minutes=5),
            reason_code="crypto_5m_observation_after_cutoff",
            recorded_at=WINDOW_END + timedelta(minutes=10),
        )
    )

    result = evaluate_crypto_delayed_paper_round_trip_acceptance(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=5),
        minimum_completion_count=2,
    )

    assert result["status"] == "eligible"
    reliability = result["report"]["service_reliability"]
    assert reliability["terminal_window_span_count"] == 2
    assert reliability["terminal_window_count"] == 2
    assert reliability["terminal_coverage_ratio"] == 1.0
    assert reliability["data_gap_window_count"] == 1
    assert reliability["integrity_error_count"] == 0


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


def test_acceptance_manifest_allows_versioned_g5_only_after_g4(tmp_path: Path) -> None:
    directory = report_module.ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
    g5 = directory / "crypto-delayed-paper-round-trip-epoch-g5-recovery.json"
    assert _manifest_path(g5) == g5

    with pytest.raises(
        CryptoRoundTripReportError, match="round_trip_report_manifest_path_invalid"
    ):
        _manifest_path(directory / "crypto-delayed-paper-round-trip-epoch-g3-old.json")


def test_g5_acceptance_runner_returns_not_ready_without_mutating_epoch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = report_module.ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
    manifest = directory / "crypto-delayed-paper-round-trip-epoch-g5-recovery.json"
    identity = tmp_path / ".round_trip_epoch_identity.json"
    identity.write_bytes(b"g5-identity\n")
    context = SimpleNamespace(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g5-recovery",
        epoch_generation=5,
        manifest_sha256="a" * 64,
        output_root=tmp_path,
    )
    prepared = SimpleNamespace(identity_path=identity, output_root=tmp_path)
    monkeypatch.setattr(
        report_module, "load_round_trip_epoch_manifest", lambda _: context
    )
    monkeypatch.setattr(
        report_module, "prepare_round_trip_epoch_candidate", lambda _: prepared
    )
    monkeypatch.setattr(report_module, "_existing_root", lambda _: None)
    monkeypatch.setattr(
        report_module,
        "evaluate_crypto_delayed_paper_round_trip_acceptance",
        lambda **_: {
            "status": "not_ready",
            "gate_reason_codes": ["insufficient_completed_5m_windows"],
        },
    )

    result = run_crypto_delayed_paper_round_trip_acceptance_once(
        epoch_manifest=manifest
    )

    assert result["status"] == "not_ready"
    assert result["epoch_generation"] == 5
    assert identity.read_bytes() == b"g5-identity\n"


def test_module_cli_executes_the_fail_closed_acceptance_path(tmp_path: Path) -> None:
    assert main(["--epoch-manifest", str(tmp_path / "g4.json")]) == 2
