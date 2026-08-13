from __future__ import annotations

import os
import json
import fcntl
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import Crypto.delayed_paper_round_trip_health as health_module
import Crypto.delayed_paper_round_trip_runtime as runtime_module
import Crypto.delayed_paper_ledger as ledger_module
from Crypto.delayed_paper_runner import _data_reject
from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperObservationStore,
    _sha256,
)
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_round_trip_health import (
    CryptoRoundTripHealthError,
    build_crypto_delayed_paper_round_trip_health,
    health_exit_code,
    run_crypto_delayed_paper_round_trip_health_once,
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


def _rewrite_ledger_state(root: Path, state: dict) -> None:
    material = dict(state)
    material.pop("state_sha256", None)
    state["state_sha256"] = _sha256(material)
    (root / "delayed_paper" / "decision_ledger_state.json").write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _rebind_ledger_aggregate(state: dict) -> None:
    aggregate = {
        key: state.get(key)
        for key in ledger_module.LEDGER_AGGREGATE_FIELDS
        if key != "aggregate_sha256"
    }
    state["aggregate_sha256"] = _sha256(aggregate)


def _legacy_v1_state_payload(state: dict) -> dict:
    """Reproduce the prior writer's exact state shape without Git history access."""

    legacy = {
        key: value
        for key, value in state.items()
        if key
        not in {
            *ledger_module.LEDGER_AGGREGATE_FIELDS,
            "aggregate_contract",
            "rotation_in_progress",
            "state_sha256",
        }
    }
    legacy["state_sha256"] = _sha256(legacy)
    return legacy


def _legacy_v1_reader_accepts(state: dict) -> bool:
    """Mirror the prior reader's checksum/base-field acceptance of extra keys."""

    material = dict(state)
    claimed = material.pop("state_sha256", None)
    integers = (
        state.get("sequence"),
        state.get("segment_count"),
        state.get("current_start_sequence"),
        state.get("current_row_count"),
    )
    return (
        state.get("contract") == ledger_module.DECISION_LEDGER_STATE_CONTRACT
        and claimed == _sha256(material)
        and not any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in integers
        )
        and state.get("current_start_sequence")
        == state.get("sequence") - state.get("current_row_count") + 1
        and isinstance(state.get("last_checksum"), str)
        and len(state["last_checksum"]) == 64
        and isinstance(state.get("current_start_previous_checksum"), str)
        and len(state["current_start_previous_checksum"]) == 64
    )


def test_round_trip_health_is_read_only_and_reports_sample_kpis(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    before = _tree_bytes(tmp_path)

    result = build_crypto_delayed_paper_round_trip_health(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=5),
    )

    assert _tree_bytes(tmp_path) == before
    assert result["status"] == "healthy"
    assert result["core"]["pending"] is False
    assert result["failure_count"] == 0
    assert result["read_only"] is True


def test_round_trip_health_does_not_scan_ledger_or_rotated_segments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _completed_round_trip(tmp_path)
    monkeypatch.setattr(ledger_module, "LEDGER_ROTATION_TARGET_BYTES", 1)
    store = CryptoDelayedPaperObservationStore(tmp_path)
    store.append_event(
        runtime_module._round_trip_data_gap_event(
            prior_market_slot=WINDOW_END,
            reason_code="crypto_5m_observation_after_cutoff",
            recorded_at=WINDOW_END + timedelta(minutes=10),
        )
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("normal health must not scan ledger history")

    monkeypatch.setattr(CryptoDelayedPaperObservationStore, "_read_ledger", forbidden)
    monkeypatch.setattr(
        CryptoDelayedPaperObservationStore, "_ledger_event_at_sequence", forbidden
    )
    monkeypatch.setattr(Path, "glob", forbidden)
    result = build_crypto_delayed_paper_round_trip_health(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=5),
    )
    assert result["status"] == "healthy"
    assert result["sample_kpis"]["verified_decision_events"] == 2


def test_round_trip_health_does_not_scan_capital_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _completed_round_trip(tmp_path)
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_read_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("capital scan")
        ),
    )
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_replay",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("capital replay")
        ),
    )
    result = build_crypto_delayed_paper_round_trip_health(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=5),
    )
    assert result["capital"]["order_count"] == 2


def test_round_trip_health_rejects_same_size_old_writer_advance(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    capital_root = tmp_path / "round_trip_capital"
    events = capital_root / "events.jsonl"
    original = events.read_bytes()
    replacement = capital_root / "events.replacement"
    replacement.write_bytes(original)
    replacement.replace(events)
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_source_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )


def test_round_trip_health_rejects_head_or_runtime_partial_state(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    capital_root = tmp_path / "round_trip_capital"
    head = capital_root / "head.json"
    payload = json.loads(head.read_text(encoding="utf-8"))
    payload["sequence"] += 1
    head.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(CryptoRoundTripHealthError, match="round_trip_health_source_invalid"):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )


def test_round_trip_health_fails_closed_on_aggregate_counter_or_head_tamper(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["latest_event_checksum"] = "f" * 64
    _rebind_ledger_aggregate(state)
    _rewrite_ledger_state(tmp_path, state)
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_ledger_aggregate_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )


def test_round_trip_health_fails_closed_on_decision_counter_mismatch(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["decision_count"] = 0
    _rebind_ledger_aggregate(state)
    _rewrite_ledger_state(tmp_path, state)
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_decision_count_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )


def test_round_trip_health_fails_closed_on_failure_counter_mismatch(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["failure_count"] = state["event_count"] + 1
    _rebind_ledger_aggregate(state)
    _rewrite_ledger_state(tmp_path, state)
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_ledger_aggregate_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )


def test_round_trip_health_fails_closed_on_partial_aggregate_and_crash_state(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("failure_count")
    _rewrite_ledger_state(tmp_path, state)
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_ledger_aggregate_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )

    crash_root = tmp_path / "crash"
    _completed_round_trip(crash_root)
    crash_state_path = crash_root / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(crash_state_path.read_text(encoding="utf-8"))
    ledger_path = crash_root / "delayed_paper" / "decision_ledger.jsonl"
    ledger_path.write_bytes(ledger_path.read_bytes() + b"\n")
    _rewrite_ledger_state(crash_root, state)
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_ledger_current_file_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=crash_root, now=WINDOW_END + timedelta(minutes=5)
        )


def test_legacy_ledger_state_fails_health_then_first_append_upgrades(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for key in (
        *ledger_module.LEDGER_AGGREGATE_FIELDS,
        "aggregate_contract",
        "rotation_in_progress",
    ):
        state.pop(key, None)
    _rewrite_ledger_state(tmp_path, state)
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_ledger_aggregate_missing"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )

    store = CryptoDelayedPaperObservationStore(tmp_path)
    event = {
        "contract": ledger_module.DECISION_LEDGER_CONTRACT,
        "event_id": "legacy-upgrade-event",
        "event_type": "upgrade_test",
        "market": "crypto",
        **ledger_module._non_authority_fields(),
    }
    row = store.append_event(event)
    upgraded = json.loads(state_path.read_text(encoding="utf-8"))
    assert upgraded["aggregate_contract"] == ledger_module.LEDGER_AGGREGATE_CONTRACT
    assert upgraded["event_count"] == upgraded["sequence"] == row["sequence"]
    assert upgraded["latest_event_checksum"] == row["checksum"]


def test_ledger_append_is_idempotent_and_second_append_is_o1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = CryptoDelayedPaperObservationStore(tmp_path)

    def event(event_id: str) -> dict:
        return {
            "contract": ledger_module.DECISION_LEDGER_CONTRACT,
            "event_id": event_id,
            "event_type": "append_test",
            "market": "crypto",
            **ledger_module._non_authority_fields(),
        }

    first = store.append_event(event("append-o1-1"))
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    before = state_path.read_bytes()
    assert store.append_event(event("append-o1-1"))["checksum"] == first["checksum"]
    assert state_path.read_bytes() == before

    monkeypatch.setattr(store, "_read_ledger_state", lambda: (_ for _ in ()).throw(
        AssertionError("second append must not rebuild full ledger")
    ))
    second = store.append_event(event("append-o1-2"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert second["sequence"] == first["sequence"] + 1
    assert state["event_count"] == 2
    assert state["latest_event_checksum"] == second["checksum"]


def test_upgraded_ledger_current_file_mismatch_fails_closed_on_writer(
    tmp_path: Path,
) -> None:
    store = CryptoDelayedPaperObservationStore(tmp_path)
    event = {
        "contract": ledger_module.DECISION_LEDGER_CONTRACT,
        "event_id": "upgraded-crash-event",
        "event_type": "append_test",
        "market": "crypto",
        **ledger_module._non_authority_fields(),
    }
    store.append_event(event)
    store.ledger_path.write_bytes(store.ledger_path.read_bytes() + b"\n")
    with pytest.raises(
        ledger_module.CryptoDelayedPaperLedgerError,
        match="delayed_paper_decision_ledger_current_file_invalid",
    ):
        store.append_event({**event, "event_id": "upgraded-crash-event-2"})


def test_upgraded_ledger_missing_current_with_segments_fails_closed_on_writer(
    tmp_path: Path,
) -> None:
    store = CryptoDelayedPaperObservationStore(tmp_path)
    event = {
        "contract": ledger_module.DECISION_LEDGER_CONTRACT,
        "event_id": "upgraded-segment-event",
        "event_type": "append_test",
        "market": "crypto",
        **ledger_module._non_authority_fields(),
    }
    store.append_event(event)
    store._rotate_current_ledger(1)
    with pytest.raises(
        ledger_module.CryptoDelayedPaperLedgerError,
        match="delayed_paper_decision_ledger_current_file_invalid",
    ):
        store.append_event({**event, "event_id": "upgraded-segment-event-2"})


def test_forged_rotation_marker_fails_closed_before_rebuild(tmp_path: Path) -> None:
    store = CryptoDelayedPaperObservationStore(tmp_path)
    event = {
        "contract": ledger_module.DECISION_LEDGER_CONTRACT,
        "event_id": "forged-marker-event",
        "event_type": "append_test",
        "market": "crypto",
        **ledger_module._non_authority_fields(),
    }
    store.append_event(event)
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["rotation_in_progress"] = "true"
    _rewrite_ledger_state(tmp_path, state)
    with pytest.raises(
        ledger_module.CryptoDelayedPaperLedgerError,
        match="delayed_paper_decision_ledger_state_invalid",
    ):
        store.append_event({**event, "event_id": "forged-marker-event-2"})


def test_health_fails_closed_on_valid_rotation_marker(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["rotation_in_progress"] = True
    _rebind_ledger_aggregate(state)
    _rewrite_ledger_state(tmp_path, state)
    with pytest.raises(
        CryptoRoundTripHealthError,
        match="round_trip_health_ledger_current_file_invalid",
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path, now=WINDOW_END + timedelta(minutes=5)
        )


def test_legacy_reader_accepts_extended_checksum_bound_state(tmp_path: Path) -> None:
    store = CryptoDelayedPaperObservationStore(tmp_path)
    event = {
        "contract": ledger_module.DECISION_LEDGER_CONTRACT,
        "event_id": "rollback-compat-event",
        "event_type": "append_test",
        "market": "crypto",
        **ledger_module._non_authority_fields(),
    }
    store.append_event(event)
    state = json.loads(store.ledger_state_path.read_text(encoding="utf-8"))
    assert state["aggregate_contract"] == ledger_module.LEDGER_AGGREGATE_CONTRACT
    assert _legacy_v1_reader_accepts(state) is True


def test_mixed_version_old_writer_downgrade_fails_health_closed(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _completed_round_trip(root)
    store = CryptoDelayedPaperObservationStore(root)
    event = {
        "contract": ledger_module.DECISION_LEDGER_CONTRACT,
        "event_id": "mixed-version-seed",
        "event_type": "append_test",
        "market": "crypto",
        **ledger_module._non_authority_fields(),
    }
    store.append_event(event)
    state_path = root / "delayed_paper" / "decision_ledger_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state_path.write_text(
        json.dumps(_legacy_v1_state_payload(state), sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        ledger_module.CryptoDelayedPaperLedgerError,
        match="delayed_paper_decision_ledger_aggregate_missing",
    ):
        CryptoDelayedPaperObservationStore(root)._ledger_runtime_state_read_only()


def test_round_trip_health_writer_overlap_is_nonblocking_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    original_glob = Path.glob

    def reject_completion_scan(path: Path, pattern: str):
        if path.name == "completions":
            raise AssertionError("completion directory scan is forbidden")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_completion_scan)
    started = time.monotonic()
    with (tmp_path / "delayed_paper" / ".lock").open("r") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        result = build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path,
            now=WINDOW_END + timedelta(minutes=5),
        )
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    assert time.monotonic() - started < 0.5

    assert result["status"] == "pending"
    assert result["health_outcome"] == "pending_writer_overlap"
    assert result["non_authoritative_reason"] == "writer_lock_busy"
    assert result["failure_count"] == "unavailable"
    assert result["effective_release"] == "unavailable"
    assert health_exit_code(result) == 0


def test_round_trip_health_writer_overlap_does_not_promote_pending_slot_to_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "observation_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["observation_count"] = state["completion_count"] + 1
    state["pending_observation_id"] = "crypto-delayed-observation-pending-health"
    state["latest_market_slot"] = "2026-07-19T01:05:00Z"
    material = dict(state)
    material.pop("state_sha256", None)
    state["state_sha256"] = _sha256(material)
    state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    original_glob = Path.glob

    def reject_completion_scan(path: Path, pattern: str):
        if path.name == "completions":
            raise AssertionError("completion directory scan is forbidden")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_completion_scan)
    with (tmp_path / "delayed_paper" / ".lock").open("r") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        result = build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path,
            now=WINDOW_END + timedelta(minutes=5),
        )
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    assert result["status"] == "pending"
    assert result["authoritative"] is False
    assert result["core"]["observation_count"] == result["core"]["completion_count"] + 1
    assert result["core"]["latest_market_slot"] == "2026-07-19T01:05:00Z"
    assert result["core"]["latest_completed_market_slot"] is None
    assert result["failure_count"] == "unavailable"


def test_round_trip_health_writer_overlap_still_fails_closed_on_state_tamper(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    state_path = tmp_path / "delayed_paper" / "observation_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["observation_count"] = 2
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")

    with (tmp_path / "delayed_paper" / ".lock").open("r") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        with pytest.raises(
            CryptoRoundTripHealthError, match="round_trip_health_source_invalid"
        ):
            build_crypto_delayed_paper_round_trip_health(
                output_root=tmp_path,
                now=WINDOW_END + timedelta(minutes=5),
            )
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def test_round_trip_health_failure_count_counts_only_data_reject_events(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    store = CryptoDelayedPaperObservationStore(tmp_path)
    _data_reject(
        store=store,
        profile=profile(tradingdatas_client),
        request=window_request(),
        reason_code="crypto_5m_window_incomplete",
    )
    store.append_event(
        runtime_module._round_trip_data_gap_event(
            prior_market_slot=WINDOW_END,
            reason_code="crypto_5m_observation_after_cutoff",
            recorded_at=WINDOW_END + timedelta(minutes=10),
        )
    )

    result = build_crypto_delayed_paper_round_trip_health(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=5),
    )

    assert result["failure_count"] == 1


def test_round_trip_health_failure_count_definition_is_durable_data_rejects() -> None:
    assert health_module._failure_count(
        [
            {"event_type": "data_reject"},
            {"event_type": "data_gap"},
            {"event_type": "risk_reject"},
            {"event_type": "decision"},
            {"event_type": "runtime_failure"},
        ]
    ) == 1


def test_round_trip_health_tolerates_ledger_data_gap_events(
    tmp_path: Path,
) -> None:
    """Ledger data-gap rows are audit events, not decisions, in the KPI count."""

    _completed_round_trip(tmp_path)
    store = CryptoDelayedPaperObservationStore(tmp_path)
    store.append_event(
        runtime_module._round_trip_data_gap_event(
            prior_market_slot=WINDOW_END,
            reason_code="crypto_5m_observation_after_cutoff",
            recorded_at=WINDOW_END + timedelta(minutes=10),
        )
    )

    result = build_crypto_delayed_paper_round_trip_health(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=5),
    )

    assert result["status"] == "healthy"
    assert result["sample_kpis"] == {
        "usable_completed_observations": 1,
        "verified_decision_events": 2,
        "expected_decision_events": 2,
        "capital_cycle_events": 2,
        "symbol_decisions_per_observation": 2,
    }
    assert result["capital"]["balanced"] is True
    assert result["capital"]["receipt_counts"]["buy"] == 2
    assert result["failure_count"] == 0
    assert result["execution_authority"] is False
    assert result["real_trading_enabled"] is False
    assert result["network_used"] is False
    assert health_exit_code(result) == 0


def test_round_trip_health_reports_stale_without_repairing_root(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    before = _tree_bytes(tmp_path)

    result = build_crypto_delayed_paper_round_trip_health(
        output_root=tmp_path,
        now=WINDOW_END + timedelta(minutes=31),
    )

    assert _tree_bytes(tmp_path) == before
    assert result["status"] == "stale"
    assert result["freshness"]["state"] == "stale"
    assert health_exit_code(result) == 2


def test_round_trip_health_fails_closed_on_stale_observation_state_without_write(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    observations = tmp_path / "delayed_paper" / "observations"
    before_mtime = observations.stat().st_mtime_ns
    os.utime(observations, ns=(before_mtime + 1, before_mtime + 1))
    before = _tree_bytes(tmp_path)

    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_source_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path,
            now=WINDOW_END + timedelta(minutes=5),
        )

    assert _tree_bytes(tmp_path) == before


def test_round_trip_health_never_bootstraps_an_incomplete_root(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    before = _tree_bytes(incomplete)

    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_root_incomplete"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=incomplete,
            now=WINDOW_END + timedelta(minutes=5),
        )

    assert _tree_bytes(incomplete) == before


def test_round_trip_health_fails_closed_on_capital_chain_tamper_without_repair(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    events = tmp_path / "round_trip_capital" / "events.jsonl"
    events.write_bytes(events.read_bytes() + b"{}\n")
    before = _tree_bytes(tmp_path)

    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_source_invalid"
    ):
        build_crypto_delayed_paper_round_trip_health(
            output_root=tmp_path,
            now=WINDOW_END + timedelta(minutes=5),
        )

    assert _tree_bytes(tmp_path) == before


def test_round_trip_capital_read_only_state_never_creates_missing_lock(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    capital_root = tmp_path / "round_trip_capital"
    lock_path = capital_root / ".lock"
    lock_path.unlink()

    with pytest.raises(
        CryptoRoundTripError, match="round_trip_readonly_lock_unavailable"
    ):
        RoundTripCapitalLedger(capital_root).state_read_only()

    assert not lock_path.exists()


def test_round_trip_health_rejects_nonversioned_manifest_path(tmp_path: Path) -> None:
    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_manifest_path_invalid"
    ):
        run_crypto_delayed_paper_round_trip_health_once(
            epoch_manifest=tmp_path / "round-trip.epoch.json",
            now=WINDOW_END + timedelta(minutes=5),
        )


def test_round_trip_health_runner_rechecks_prepared_versioned_epoch_without_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    identity = tmp_path / ".round_trip_epoch_identity.json"
    identity.write_text('{"epoch":"g4"}\n', encoding="utf-8")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    manifest = manifests / "crypto-delayed-paper-round-trip-epoch-g4-test.json"
    context = SimpleNamespace(
        output_root=tmp_path,
        identity_path=identity,
        epoch_id="crypto-delayed-paper-round-trip-epoch-g4-test",
        epoch_generation=4,
        manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=tmp_path, identity_path=identity)
    monkeypatch.setattr(health_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifests)
    monkeypatch.setattr(
        health_module, "load_round_trip_epoch_manifest", lambda _: context
    )
    monkeypatch.setattr(
        health_module,
        "prepare_round_trip_epoch_candidate",
        lambda _: prepared,
    )
    prepare_calls = {"count": 0}

    def prepare_once(_: object) -> object:
        prepare_calls["count"] += 1
        return prepared

    monkeypatch.setattr(
        health_module,
        "prepare_round_trip_epoch_candidate",
        prepare_once,
    )
    monkeypatch.setattr(
        health_module,
        "_epoch_identity_bytes",
        lambda _: (identity.read_bytes(),),
    )
    before = _tree_bytes(tmp_path)

    result = run_crypto_delayed_paper_round_trip_health_once(
        epoch_manifest=manifest,
        now=WINDOW_END + timedelta(minutes=5),
    )

    assert _tree_bytes(tmp_path) == before
    assert prepare_calls["count"] == 1
    assert result["epoch_id"] == context.epoch_id
    assert result["epoch_generation"] == 4
    assert result["epoch_manifest_sha256"] == context.manifest_sha256
    assert result["status"] == "healthy"


def test_round_trip_health_runner_rejects_anchor_change_without_second_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    identity = tmp_path / ".round_trip_epoch_identity.json"
    identity.write_text('{"epoch":"g4"}\n', encoding="utf-8")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    manifest = manifests / "crypto-delayed-paper-round-trip-epoch-g4-test.json"
    context = SimpleNamespace(
        output_root=tmp_path,
        identity_path=identity,
        epoch_id="crypto-delayed-paper-round-trip-epoch-g4-test",
        epoch_generation=4,
        manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=tmp_path, identity_path=identity)
    monkeypatch.setattr(health_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifests)
    monkeypatch.setattr(health_module, "load_round_trip_epoch_manifest", lambda _: context)
    prepare_calls = {"count": 0}

    def prepare_once(_: object) -> object:
        prepare_calls["count"] += 1
        return prepared

    monkeypatch.setattr(health_module, "prepare_round_trip_epoch_candidate", prepare_once)
    identities = iter((b"before", b"after"))
    monkeypatch.setattr(health_module, "_epoch_identity_bytes", lambda _: next(identities))

    with pytest.raises(
        CryptoRoundTripHealthError, match="round_trip_health_identity_changed"
    ):
        run_crypto_delayed_paper_round_trip_health_once(
            epoch_manifest=manifest,
            now=WINDOW_END + timedelta(minutes=5),
        )

    assert prepare_calls["count"] == 1


def test_round_trip_health_runner_allows_mutable_runtime_change_after_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    identity = tmp_path / ".round_trip_epoch_identity.json"
    identity.write_text('{"epoch":"g4"}\n', encoding="utf-8")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    manifest = manifests / "crypto-delayed-paper-round-trip-epoch-g4-test.json"
    context = SimpleNamespace(
        output_root=tmp_path,
        identity_path=identity,
        epoch_id="crypto-delayed-paper-round-trip-epoch-g4-test",
        epoch_generation=4,
        manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=tmp_path, identity_path=identity)
    monkeypatch.setattr(health_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifests)
    monkeypatch.setattr(health_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(health_module, "prepare_round_trip_epoch_candidate", lambda _: prepared)
    real_build = health_module.build_crypto_delayed_paper_round_trip_health

    def build_then_writer_progress(**kwargs: object) -> dict:
        result = real_build(**kwargs)
        os.utime(tmp_path / "delayed_paper" / "observation_state.json", None)
        os.utime(tmp_path / "round_trip_capital" / "head.json", None)
        return result

    monkeypatch.setattr(
        health_module,
        "build_crypto_delayed_paper_round_trip_health",
        build_then_writer_progress,
    )

    result = run_crypto_delayed_paper_round_trip_health_once(
        epoch_manifest=manifest,
        now=WINDOW_END + timedelta(minutes=5),
    )

    assert result["status"] == "healthy"
