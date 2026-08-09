from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import Crypto.delayed_paper_ledger as ledger_module
import Crypto.delayed_paper_runner as runner_module
from Crypto.fixture_sim.contracts import CryptoEvidenceError, CryptoSafetyError
from Crypto.delayed_paper_ledger import CryptoDelayedPaperObservationStore
from Crypto.delayed_paper_runner import run_crypto_delayed_paper_once
from Crypto.five_minute_data import (
    CryptoFiveMinuteDataError,
    CryptoFiveMinuteWindowRequest,
    TradingDatasCryptoFiveMinuteDataPort,
)
from tests.test_crypto_5m_support import (
    BAR_DATASETS,
    RULE_DATASETS,
    SYMBOLS,
    WINDOW_END,
    FixtureTradingDatasTransport,
    bar_rows,
    client,
    iso,
    metadata,
    profile,
    window_request,
)


def _runner_inputs(
    *,
    transport: FixtureTradingDatasTransport | None = None,
) -> tuple[
    TradingDatasCryptoFiveMinuteDataPort,
    Any,
    Any,
    FixtureTradingDatasTransport,
]:
    resolved = transport or FixtureTradingDatasTransport()
    tradingdatas_client = client(resolved)
    return (
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile(tradingdatas_client),
        window_request(),
        resolved,
    )


def _shifted_runner_inputs(
    minutes: int,
) -> tuple[
    TradingDatasCryptoFiveMinuteDataPort,
    Any,
    CryptoFiveMinuteWindowRequest,
    FixtureTradingDatasTransport,
]:
    delta = timedelta(minutes=minutes)
    shifted = bar_rows()
    for row in shifted:
        for field_name in ("open_time", "close_time"):
            parsed = datetime.fromisoformat(str(row[field_name]).replace("Z", "+00:00"))
            row[field_name] = iso(parsed + delta)
    shifted_end = WINDOW_END + delta
    metadata_by_dataset: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        metadata_by_dataset[BAR_DATASETS[symbol]] = metadata(
            dataset_id=BAR_DATASETS[symbol],
            data_through=shifted_end - timedelta(milliseconds=1),
            observed_at=shifted_end + timedelta(seconds=20),
        )
        metadata_by_dataset[RULE_DATASETS[symbol]] = metadata(
            dataset_id=RULE_DATASETS[symbol],
            data_through=shifted_end + timedelta(seconds=5),
            observed_at=shifted_end + timedelta(seconds=10),
        )
    transport = FixtureTradingDatasTransport(
        bars=shifted,
        metadata_by_dataset=metadata_by_dataset,
    )
    tradingdatas_client = client(transport)
    return (
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile(tradingdatas_client),
        CryptoFiveMinuteWindowRequest(
            window_end=shifted_end,
            observation_cutoff=shifted_end + timedelta(seconds=30),
        ),
        transport,
    )


def _capital_bytes(root: Path) -> dict[str, bytes]:
    capital = root / "capital"
    if not capital.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(capital.rglob("*"))
        if path.is_file()
    }


def _all_values(value: Any) -> list[Any]:
    result = [value]
    if isinstance(value, dict):
        for item in value.values():
            result.extend(_all_values(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_all_values(item))
    return result


def test_two_symbol_weekend_window_runs_one_causal_delayed_paper_cycle(
    tmp_path: Path,
) -> None:
    port, frozen, request, _ = _runner_inputs()
    result = run_crypto_delayed_paper_once(
        port=port,
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )

    assert result["status"] == "completed"
    assert result["market_session"] == "24x7"
    assert result["max_positions"] == 2
    assert set(result["symbols"]) == {"BTCUSDT", "ETHUSDT"}
    for symbol, item in result["symbols"].items():
        assert item["disposition"] == "fixture_simulated_fill"
        bundle = item["bundle"]
        assert bundle["decision"]["regime_interval"] == "1h"
        assert bundle["decision"]["decision_interval"] == "15m"
        assert bundle["decision"]["execution_interval"] == "5m"
        assert bundle["decision"]["action"] == "buy"
        assert bundle["paper_receipt"]["status"] == "fixture_simulated"
        assert bundle["paper_receipt"]["execution_authority"] is False
        assert item["counterfactual"]["execution_quote_kind"] == (
            "next_closed_bar_open_counterfactual"
        )
        assert item["counterfactual"]["label_status"] == "pending"
        assert item["counterfactual"]["decision_data_through"].endswith("01:00:00Z")
        assert item["counterfactual"]["market_slot"].endswith("01:00:00Z")
        assert item["counterfactual"]["available_after"].endswith("01:05:20Z")
        assert item["counterfactual"]["execution_observed_at"].endswith("01:06:00Z")
        assert bundle["decision"]["decision_observed_at"].endswith("01:05:20Z")
        assert bundle["order_intent"]["execution_slot"].endswith("01:06:00Z")
        assert bundle["paper_receipt"]["filled_at"].endswith("01:06:00Z")
        assert symbol == bundle["decision"]["symbol"]

    final = result["symbols"]["ETHUSDT"]["bundle"]["capital"]["final"]
    assert set(final["positions"]) == {"BTCUSDT", "ETHUSDT"}
    assert len(final["positions"]) == 2
    assert final["balanced"] is True
    assert final["reserved_cash"] == "0.00000000"
    assert all(not isinstance(value, float) for value in _all_values(final))

    events = [
        json.loads(line)
        for line in (tmp_path / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(events) == 2
    assert {event["event_type"] for event in events} == {"decision"}
    assert all(event["execution_authority"] is False for event in events)
    assert all(event["production_eligible"] is False for event in events)
    assert all("cash" not in event and "positions" not in event for event in events)


def test_execution_bar_tail_never_changes_decision_or_counterfactual_price(
    tmp_path: Path,
) -> None:
    baseline_port, baseline_profile, request, _ = _runner_inputs()
    baseline = run_crypto_delayed_paper_once(
        port=baseline_port,
        profile=baseline_profile,
        request=request,
        output_root=tmp_path / "baseline",
    )

    changed = bar_rows()
    for index in (12, 25):
        changed[index]["high"] = changed[index]["open"]
        changed[index]["low"] = changed[index]["open"]
        changed[index]["close"] = changed[index]["open"]
        changed[index]["volume"] = "999999"
    changed_port, changed_profile, _, _ = _runner_inputs(
        transport=FixtureTradingDatasTransport(bars=changed)
    )
    comparison = run_crypto_delayed_paper_once(
        port=changed_port,
        profile=changed_profile,
        request=request,
        output_root=tmp_path / "changed",
    )

    for symbol in ("BTCUSDT", "ETHUSDT"):
        first = baseline["symbols"][symbol]["bundle"]
        second = comparison["symbols"][symbol]["bundle"]
        assert first["decision"]["regime_return"] == second["decision"]["regime_return"]
        assert (
            first["decision"]["decision_return"]
            == second["decision"]["decision_return"]
        )
        assert (
            first["order_intent"]["reference_price"]
            == second["order_intent"]["reference_price"]
        )
        assert first["order_intent"]["quantity"] == second["order_intent"]["quantity"]


def test_bad_data_writes_one_idempotent_data_reject_and_never_touches_capital(
    tmp_path: Path,
) -> None:
    broken = bar_rows()
    broken.pop()
    port, frozen, request, _ = _runner_inputs(
        transport=FixtureTradingDatasTransport(bars=broken)
    )
    first = run_crypto_delayed_paper_once(
        port=port,
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )
    capital_after_first = _capital_bytes(tmp_path)

    port2, frozen2, request2, _ = _runner_inputs(
        transport=FixtureTradingDatasTransport(bars=broken)
    )
    second = run_crypto_delayed_paper_once(
        port=port2,
        profile=frozen2,
        request=request2,
        output_root=tmp_path,
    )

    assert first["status"] == second["status"] == "data_reject"
    assert first["reason_code"] == "crypto_5m_window_incomplete"
    assert _capital_bytes(tmp_path) == capital_after_first == {}
    events = (
        (tmp_path / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(events) == 1
    event = json.loads(events[0])
    assert event["event_type"] == "data_reject"
    assert event["request_window_end"] == request.window_end.isoformat().replace(
        "+00:00", "Z"
    )
    assert event["request_observation_cutoff"] == (
        request.observation_cutoff.isoformat().replace("+00:00", "Z")
    )
    assert event["execution_eligible"] is False
    assert event["capital_commit_id"] is None


def test_second_symbol_counterfactual_failure_rejects_before_any_capital(
    tmp_path: Path,
) -> None:
    rows = bar_rows()
    for field_name in ("open", "high", "low", "close"):
        rows[25][field_name] = "0.01"
    port, frozen, request, _ = _runner_inputs(
        transport=FixtureTradingDatasTransport(bars=rows)
    )

    rejected = run_crypto_delayed_paper_once(
        port=port,
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )

    assert rejected["status"] == "data_reject"
    assert rejected["reason_code"] == "crypto_5m_counterfactual_spread_invalid"
    assert not (tmp_path / "capital").exists()
    assert not any((tmp_path / "delayed_paper" / "observations").glob("*.json"))
    events = (
        (tmp_path / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(events) == 1
    assert json.loads(events[0])["event_type"] == "data_reject"


def test_untyped_snapshot_with_junk_proof_is_rejected_before_capital(
    tmp_path: Path,
) -> None:
    port, frozen, request, _ = _runner_inputs()
    valid = port.load_snapshot(profile=frozen, request=request)
    weak = SimpleNamespace(
        profile_sha256="0" * 64,
        market_content_sha256="1" * 64,
        observation_sha256="2" * 64,
        same_observation=True,
        bars=valid.bars,
        instrument_rules=valid.instrument_rules,
        source_proofs=(),
        request=request,
        source_bindings=lambda: {"junk": {"observed_at": "2026-07-19T01:05:20Z"}},
    )

    class WeakPort:
        def load_snapshot(self, **_: Any) -> Any:
            return weak

    rejected = run_crypto_delayed_paper_once(
        port=WeakPort(),
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )

    assert rejected["status"] == "data_reject"
    assert rejected["reason_code"] == "crypto_5m_snapshot_type_invalid"
    assert not (tmp_path / "capital").exists()
    assert not any((tmp_path / "delayed_paper" / "observations").glob("*.json"))


def test_bad_data_after_completed_cycle_leaves_existing_capital_unchanged(
    tmp_path: Path,
) -> None:
    first_port, first_profile, request, _ = _runner_inputs()
    completed = run_crypto_delayed_paper_once(
        port=first_port,
        profile=first_profile,
        request=request,
        output_root=tmp_path,
    )
    assert completed["status"] == "completed"
    capital_before = _capital_bytes(tmp_path)

    broken = bar_rows()
    broken.pop()
    rejected_port, rejected_profile, _, _ = _runner_inputs(
        transport=FixtureTradingDatasTransport(bars=broken)
    )
    rejected = run_crypto_delayed_paper_once(
        port=rejected_port,
        profile=rejected_profile,
        request=request,
        output_root=tmp_path,
    )

    assert rejected["status"] == "data_reject"
    assert rejected["reason_code"] == "crypto_5m_window_incomplete"
    assert _capital_bytes(tmp_path) == capital_before


def test_same_observation_replay_does_not_duplicate_fill_or_decision(
    tmp_path: Path,
) -> None:
    port, frozen, request, _ = _runner_inputs()
    first = run_crypto_delayed_paper_once(
        port=port,
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )
    capital_before = _capital_bytes(tmp_path)
    ledger_before = (tmp_path / "delayed_paper" / "decision_ledger.jsonl").read_bytes()

    replay_port, replay_profile, replay_request, _ = _runner_inputs()
    second = run_crypto_delayed_paper_once(
        port=replay_port,
        profile=replay_profile,
        request=replay_request,
        output_root=tmp_path,
    )

    assert second["status"] == "completed"
    assert second["observation_id"] == first["observation_id"]
    assert all(item["idempotent_replay"] is True for item in second["symbols"].values())
    assert _capital_bytes(tmp_path) == capital_before
    assert (
        tmp_path / "delayed_paper" / "decision_ledger.jsonl"
    ).read_bytes() == ledger_before


def test_same_slot_different_payload_is_rejected_before_capital(
    tmp_path: Path,
) -> None:
    first_port, first_profile, request, _ = _runner_inputs()
    completed = run_crypto_delayed_paper_once(
        port=first_port,
        profile=first_profile,
        request=request,
        output_root=tmp_path,
    )
    assert completed["status"] == "completed"
    capital_before = _capital_bytes(tmp_path)

    changed = bar_rows()
    changed[0]["close"] = "50025.00"
    changed[0]["high"] = "50050.10"
    second_port, second_profile, _, _ = _runner_inputs(
        transport=FixtureTradingDatasTransport(bars=changed)
    )
    rejected = run_crypto_delayed_paper_once(
        port=second_port,
        profile=second_profile,
        request=request,
        output_root=tmp_path,
    )

    assert rejected["status"] == "data_reject"
    assert rejected["reason_code"] == "crypto_5m_slot_payload_conflict"
    assert _capital_bytes(tmp_path) == capital_before
    events = [
        json.loads(line)
        for line in (tmp_path / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event_type"] for event in events] == [
        "decision",
        "decision",
        "data_reject",
    ]
    assert events[-1]["rejected_observation_sha256"]


def test_older_global_slot_is_rejected_before_capital(
    tmp_path: Path,
) -> None:
    first_port, first_profile, request, _ = _runner_inputs()
    completed = run_crypto_delayed_paper_once(
        port=first_port,
        profile=first_profile,
        request=request,
        output_root=tmp_path,
    )
    assert completed["status"] == "completed"
    capital_before = _capital_bytes(tmp_path)

    delta = timedelta(minutes=-5)
    older_rows = bar_rows()
    for row in older_rows:
        for field_name in ("open_time", "close_time"):
            parsed = datetime.fromisoformat(str(row[field_name]).replace("Z", "+00:00"))
            row[field_name] = iso(parsed + delta)
    older_end = WINDOW_END + delta
    metadata_by_dataset: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        metadata_by_dataset[BAR_DATASETS[symbol]] = metadata(
            dataset_id=BAR_DATASETS[symbol],
            data_through=older_end - timedelta(milliseconds=1),
            observed_at=older_end + timedelta(seconds=20),
        )
        metadata_by_dataset[RULE_DATASETS[symbol]] = metadata(
            dataset_id=RULE_DATASETS[symbol],
            data_through=older_end + timedelta(seconds=5),
            observed_at=older_end + timedelta(seconds=10),
        )
    older_transport = FixtureTradingDatasTransport(
        bars=older_rows,
        metadata_by_dataset=metadata_by_dataset,
    )
    older_client = client(older_transport)
    rejected = run_crypto_delayed_paper_once(
        port=TradingDatasCryptoFiveMinuteDataPort(older_client),
        profile=profile(older_client),
        request=CryptoFiveMinuteWindowRequest(
            window_end=older_end,
            observation_cutoff=older_end + timedelta(seconds=30),
        ),
        output_root=tmp_path,
    )

    assert rejected["status"] == "data_reject"
    assert rejected["reason_code"] == "crypto_5m_slot_not_monotonic"
    assert _capital_bytes(tmp_path) == capital_before


def test_later_buy_candidates_reconcile_marks_without_adding_positions(
    tmp_path: Path,
) -> None:
    first_port, first_profile, first_request, _ = _runner_inputs()
    first = run_crypto_delayed_paper_once(
        port=first_port,
        profile=first_profile,
        request=first_request,
        output_root=tmp_path,
    )
    initial_capital = first["symbols"]["ETHUSDT"]["bundle"]["capital"]["final"]

    for minutes in (5, 10, 15):
        port, frozen, request, _ = _shifted_runner_inputs(minutes)
        result = run_crypto_delayed_paper_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=tmp_path,
        )
        assert result["status"] == "completed"
        assert result["capital_effect"] == "mark_only_risk_reconcile"
        assert {item["disposition"] for item in result["symbols"].values()} == {
            "risk_rejected"
        }
        for item in result["symbols"].values():
            assert item["bundle"]["order_intent"] is None
            assert item["bundle"]["paper_receipt"] is None
            assert (
                item["risk_reject"]["reason_code"]
                == "frozen_champion_position_cap_exceeded"
            )
        final = result["symbols"]["ETHUSDT"]["bundle"]["capital"]["final"]
        for field_name in ("cash", "positions", "orders", "fees"):
            assert final[field_name] == initial_capital[field_name]
        assert (
            CryptoDelayedPaperObservationStore(tmp_path).pending_observation() is None
        )

    events = [
        json.loads(line)
        for line in (tmp_path / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event_type"] for event in events] == [
        "decision",
        "decision",
        "risk_reject",
        "risk_reject",
        "risk_reject",
        "risk_reject",
        "risk_reject",
        "risk_reject",
    ]


def test_store_refuses_a_second_unfinished_observation(
    tmp_path: Path,
) -> None:
    first_port, first_profile, first_request, _ = _runner_inputs()
    second_port, second_profile, second_request, _ = _shifted_runner_inputs(5)
    first_observation = runner_module._snapshot_to_observation(
        first_port.load_snapshot(
            profile=first_profile,
            request=first_request,
        )
    )
    second_observation = runner_module._snapshot_to_observation(
        second_port.load_snapshot(
            profile=second_profile,
            request=second_request,
        )
    )
    store = CryptoDelayedPaperObservationStore(tmp_path)
    store.accept(first_observation)

    with pytest.raises(
        ledger_module.CryptoDelayedPaperLedgerError,
        match="delayed_paper_prior_observation_pending",
    ):
        store.accept(second_observation)

    pending = store.pending_observation()
    assert pending is not None
    assert pending["observation_id"] == first_observation["observation_id"]


def test_crash_after_core_bundle_recovers_pending_without_refetch_or_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, frozen, request, _ = _runner_inputs()
    original = CryptoDelayedPaperObservationStore.mark_complete
    calls = 0

    def crash_once(
        self: CryptoDelayedPaperObservationStore,
        observation: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("completion marker crash")
        return original(self, observation, result)

    monkeypatch.setattr(
        CryptoDelayedPaperObservationStore,
        "mark_complete",
        crash_once,
    )
    with pytest.raises(OSError, match="completion marker crash"):
        run_crypto_delayed_paper_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=tmp_path,
        )
    capital_after_crash = _capital_bytes(tmp_path)

    class BombPort:
        def load_snapshot(self, **_: Any) -> Any:
            raise AssertionError("pending recovery must run before a fresh data read")

    recovered = run_crypto_delayed_paper_once(
        port=BombPort(),
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )

    assert recovered["status"] == "completed"
    assert recovered["recovered_pending"] is True
    assert all(
        item["idempotent_replay"] is True for item in recovered["symbols"].values()
    )
    assert _capital_bytes(tmp_path) == capital_after_crash


def test_crash_between_symbols_replays_first_and_executes_second_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, frozen, request, _ = _runner_inputs()
    original = runner_module.run_fixture_auto_sim
    calls = 0

    def crash_before_second(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("between-symbol crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner_module, "run_fixture_auto_sim", crash_before_second)
    with pytest.raises(OSError, match="between-symbol crash"):
        run_crypto_delayed_paper_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=tmp_path,
        )
    events_after_crash = (
        (tmp_path / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(events_after_crash) == 1

    monkeypatch.setattr(runner_module, "run_fixture_auto_sim", original)

    class BombPort:
        def load_snapshot(self, **_: Any) -> Any:
            raise AssertionError("pending recovery must not refetch data")

    recovered = run_crypto_delayed_paper_once(
        port=BombPort(),
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )

    assert recovered["status"] == "completed"
    assert recovered["recovered_pending"] is True
    assert recovered["symbols"]["BTCUSDT"]["idempotent_replay"] is True
    assert recovered["symbols"]["ETHUSDT"]["idempotent_replay"] is False
    final = recovered["symbols"]["ETHUSDT"]["bundle"]["capital"]["final"]
    assert set(final["positions"]) == {"BTCUSDT", "ETHUSDT"}
    events = (
        (tmp_path / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(events) == 2


def test_two_position_gap_uses_one_observation_valuation_and_recovers_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_port, initial_profile, initial_request, _ = _runner_inputs()
    initial = run_crypto_delayed_paper_once(
        port=initial_port,
        profile=initial_profile,
        request=initial_request,
        output_root=tmp_path,
    )
    assert set(
        initial["symbols"]["ETHUSDT"]["bundle"]["capital"]["final"]["positions"]
    ) == {"BTCUSDT", "ETHUSDT"}

    gap_port, gap_profile, gap_request, _ = _shifted_runner_inputs(15)
    original = runner_module.run_fixture_auto_sim
    calls = 0

    def crash_before_gap_second(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("gap between-symbol crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        runner_module,
        "run_fixture_auto_sim",
        crash_before_gap_second,
    )
    with pytest.raises(OSError, match="gap between-symbol crash"):
        run_crypto_delayed_paper_once(
            port=gap_port,
            profile=gap_profile,
            request=gap_request,
            output_root=tmp_path,
        )

    monkeypatch.setattr(runner_module, "run_fixture_auto_sim", original)

    class BombPort:
        def load_snapshot(self, **_: Any) -> Any:
            raise AssertionError("gap recovery must reuse the pending observation")

    recovered = run_crypto_delayed_paper_once(
        port=BombPort(),
        profile=gap_profile,
        request=gap_request,
        output_root=tmp_path,
    )

    assert recovered["status"] == "completed"
    assert recovered["recovered_pending"] is True
    assert recovered["symbols"]["BTCUSDT"]["idempotent_replay"] is True
    assert recovered["symbols"]["ETHUSDT"]["idempotent_replay"] is False
    final = recovered["symbols"]["ETHUSDT"]["bundle"]["capital"]["final"]
    assert set(final["positions"]) == {"BTCUSDT", "ETHUSDT"}
    assert set(final["mark_slots"]) == {"BTCUSDT", "ETHUSDT"}
    assert len(set(final["mark_slots"].values())) == 1
    assert final["valuation_slot"] == next(iter(final["mark_slots"].values()))
    assert CryptoDelayedPaperObservationStore(tmp_path).pending_observation() is None
    claims = [
        event
        for event in (
            json.loads(line)
            for line in (tmp_path / "capital" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if event["event_type"] == "cycle_claim"
    ]
    assert len(claims) == 4
    for claim in claims:
        context = claim["payload"]["valuation_context"]
        assert set(context["marks"]) == {"BTCUSDT", "ETHUSDT"}
        assert context["valuation_slot"] == claim["payload"]["execution_slot"]
        assert all(
            len(mark["market_evidence_sha256"]) == 64 and mark["evidence_receipt_id"]
            for mark in context["marks"].values()
        )


def test_partial_decision_ledger_temp_write_after_capital_recovers_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, frozen, request, _ = _runner_inputs()
    original_write_all = ledger_module._write_all
    crashed = False

    def partial_then_crash(descriptor: int, encoded: bytes) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            os.write(descriptor, encoded[: max(1, len(encoded) // 2)])
            raise OSError("decision ledger temp write crash")
        original_write_all(descriptor, encoded)

    monkeypatch.setattr(ledger_module, "_write_all", partial_then_crash)
    with pytest.raises(OSError, match="decision ledger temp write crash"):
        run_crypto_delayed_paper_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=tmp_path,
        )
    assert _capital_bytes(tmp_path)
    ledger_path = tmp_path / "delayed_paper" / "decision_ledger.jsonl"
    assert not ledger_path.exists()
    assert not list((tmp_path / "delayed_paper").glob("*.tmp"))

    monkeypatch.setattr(ledger_module, "_write_all", original_write_all)

    class BombPort:
        def load_snapshot(self, **_: Any) -> Any:
            raise AssertionError("pending recovery must not refetch data")

    recovered = run_crypto_delayed_paper_once(
        port=BombPort(),
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )

    assert recovered["status"] == "completed"
    assert recovered["recovered_pending"] is True
    assert recovered["symbols"]["BTCUSDT"]["idempotent_replay"] is True
    assert recovered["symbols"]["ETHUSDT"]["idempotent_replay"] is False
    events = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 2
    assert [json.loads(line)["symbol"] for line in events].count("BTCUSDT") == 1


def test_decision_ledger_rotates_atomically_and_recovers_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def event(event_id: str) -> dict[str, Any]:
        return {
            "contract": ledger_module.DECISION_LEDGER_CONTRACT,
            "event_id": event_id,
            "event_type": "rotation_test",
            "market": "crypto",
            **ledger_module._non_authority_fields(),
        }

    seed = CryptoDelayedPaperObservationStore(tmp_path / "seed")
    seed.append_event(event("rotation-event-000001"))
    one_event_bytes = seed.ledger_path.stat().st_size
    monkeypatch.setattr(
        ledger_module,
        "MAX_LEDGER_BYTES",
        one_event_bytes + 32,
    )

    store = CryptoDelayedPaperObservationStore(tmp_path / "target")
    first = store.append_event(event("rotation-event-000001"))
    original_write_all = ledger_module._write_all
    crashed = False

    def partial_then_crash(descriptor: int, encoded: bytes) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            os.write(descriptor, encoded[: max(1, len(encoded) // 2)])
            raise OSError("rotation current write crash")
        original_write_all(descriptor, encoded)

    monkeypatch.setattr(ledger_module, "_write_all", partial_then_crash)
    with pytest.raises(OSError, match="rotation current write crash"):
        store.append_event(event("rotation-event-000002"))
    assert not store.ledger_path.exists()
    assert (store.root / "decision_ledger.segment-000001.jsonl").exists()

    monkeypatch.setattr(ledger_module, "_write_all", original_write_all)
    second = store.append_event(event("rotation-event-000002"))
    third = store.append_event(event("rotation-event-000003"))
    rows = store._read_ledger()

    assert [row["sequence"] for row in rows] == [1, 2, 3]
    assert rows[0]["checksum"] == first["checksum"]
    assert rows[1]["checksum"] == second["checksum"]
    assert rows[2]["checksum"] == third["checksum"]
    assert (
        store.append_event(event("rotation-event-000002"))["checksum"]
        == second["checksum"]
    )
    assert len(store._segment_paths()) == 2


def test_decision_ledger_rotates_at_runtime_target_below_read_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def event(event_id: str) -> dict[str, Any]:
        return {
            "contract": ledger_module.DECISION_LEDGER_CONTRACT,
            "event_id": event_id,
            "event_type": "rotation_target_test",
            "market": "crypto",
            **ledger_module._non_authority_fields(),
        }

    seed = CryptoDelayedPaperObservationStore(tmp_path / "seed")
    seed.append_event(event("rotation-target-event-000001"))
    one_event_bytes = seed.ledger_path.stat().st_size
    monkeypatch.setattr(
        ledger_module,
        "LEDGER_ROTATION_TARGET_BYTES",
        one_event_bytes + 32,
    )

    store = CryptoDelayedPaperObservationStore(tmp_path / "target")
    store.append_event(event("rotation-target-event-000001"))
    store.append_event(event("rotation-target-event-000002"))
    store.append_event(event("rotation-target-event-000003"))

    assert ledger_module.LEDGER_ROTATION_TARGET_BYTES < ledger_module.MAX_LEDGER_BYTES
    assert len(store._segment_paths()) == 2
    assert [row["sequence"] for row in store._read_ledger()] == [1, 2, 3]


def test_llm_sidecar_text_cannot_change_decision_order_or_capital(
    tmp_path: Path,
) -> None:
    first_port, first_profile, request, _ = _runner_inputs()
    first = run_crypto_delayed_paper_once(
        port=first_port,
        profile=first_profile,
        request=request,
        output_root=tmp_path / "one",
        llm_evidence={
            "mode": "offline_fixture",
            "authority": "none",
            "network_used": False,
            "evidence_id": "llm-a",
            "summary": "first wording",
        },
    )
    second_port, second_profile, _, _ = _runner_inputs()
    second = run_crypto_delayed_paper_once(
        port=second_port,
        profile=second_profile,
        request=request,
        output_root=tmp_path / "two",
        llm_evidence={
            "mode": "offline_fixture",
            "authority": "none",
            "network_used": False,
            "evidence_id": "llm-b",
            "summary": "completely different wording",
        },
    )

    for symbol in ("BTCUSDT", "ETHUSDT"):
        left = first["symbols"][symbol]["bundle"]
        right = second["symbols"][symbol]["bundle"]
        assert left["run_id"] == right["run_id"]
        assert left["decision"] == right["decision"]
        assert left["order_intent"] == right["order_intent"]
        assert left["paper_receipt"] == right["paper_receipt"]
        assert left["capital"]["final"] == right["capital"]["final"]


def test_runner_never_calls_retired_or_network_paths() -> None:
    source = inspect.getsource(runner_module).lower()
    for forbidden in (
        "crypto.workflow",
        "crypto.simulator",
        "crypto.sim_executor",
        "crypto.shadow_runner",
        "binance",
        "socket",
        "urllib",
        "requests",
        "httpx",
    ):
        assert forbidden not in source


def test_real_trading_flag_fails_before_any_runner_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    port, frozen, request, _ = _runner_inputs()
    with pytest.raises(CryptoSafetyError, match="real_trading_enabled_must_be_false"):
        run_crypto_delayed_paper_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=tmp_path,
        )
    assert not (tmp_path / "delayed_paper").exists()
    assert not (tmp_path / "capital").exists()


def test_cyclic_llm_sidecar_fails_closed_before_any_runner_state(
    tmp_path: Path,
) -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    port, frozen, request, _ = _runner_inputs()

    with pytest.raises(CryptoEvidenceError, match="json_tree_cycle"):
        run_crypto_delayed_paper_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=tmp_path,
            llm_evidence=cyclic,
        )
    assert not (tmp_path / "delayed_paper").exists()
    assert not (tmp_path / "capital").exists()


class AlwaysRejectPort:
    def load_snapshot(self, **_: Any) -> Any:
        raise CryptoFiveMinuteDataError("crypto_5m_fixture_reject")
