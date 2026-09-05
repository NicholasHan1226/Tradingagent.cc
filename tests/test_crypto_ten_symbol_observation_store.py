from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import Crypto.ten_symbol_observation_store as store_module
from Crypto.ten_symbol_observation_store import (
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)


WINDOW_END = datetime(2026, 8, 10, 1, 5, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _observation_event(
    window_end: datetime,
    *,
    seed: str = "a",
) -> dict[str, Any]:
    cutoff = window_end + timedelta(seconds=55)
    return {
        "contract": store_module.TEN_SYMBOL_EVENT_CONTRACT,
        "event_id": f"crypto-ten-observation-{seed * 24}",
        "event_type": "observation",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": _iso(window_end),
        "observation_cutoff": _iso(cutoff),
        "catalog_version": "fixture-catalog-v1",
        "profile_sha256": "1" * 64,
        "observation": {
            "contract": "tradingagent.crypto.market_observation.v1",
            "window_end": _iso(window_end),
            "observation_sha256": f"{seed[0]}" * 64,
        },
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _reject_event(
    window_end: datetime,
    *,
    reason_code: str = "crypto_observation_watermark_invalid",
) -> dict[str, Any]:
    cutoff = window_end + timedelta(seconds=55)
    return {
        "contract": store_module.TEN_SYMBOL_EVENT_CONTRACT,
        "event_id": f"crypto-ten-data-reject-{reason_code[-24:]}",
        "event_type": "data_reject",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": _iso(window_end),
        "observation_cutoff": _iso(cutoff),
        "catalog_version": "fixture-catalog-v1",
        "profile_sha256": "1" * 64,
        "reason_code": reason_code,
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _gap_event(
    prior: datetime,
    recovery: datetime,
) -> dict[str, Any]:
    cutoff = recovery + timedelta(seconds=55)
    return {
        "contract": store_module.TEN_SYMBOL_EVENT_CONTRACT,
        "gap_contract": store_module.TEN_SYMBOL_DATA_GAP_CONTRACT,
        "event_id": "crypto-ten-data-gap-" + "b" * 24,
        "event_type": "data_gap",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": _iso(recovery),
        "observation_cutoff": _iso(cutoff),
        "prior_market_slot": _iso(prior),
        "skipped_from": _iso(prior + timedelta(minutes=5)),
        "skipped_to": _iso(recovery - timedelta(minutes=5)),
        "recovery_market_slot": _iso(recovery),
        "reason_code": "crypto_observation_watermark_invalid",
        "catalog_version": "fixture-catalog-v1",
        "profile_sha256": "1" * 64,
        "recovery_observation": {"observation_sha256": "c" * 64},
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _head(root: Path) -> dict[str, Any]:
    return json.loads((root / "head.json").read_text(encoding="utf-8"))


def test_appends_observation_and_publishes_verified_head(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)

    row = store.append_event(_observation_event(WINDOW_END))

    assert row["sequence"] == 1
    assert row["previous_checksum"] == "0" * 64
    assert len(row["checksum"]) == 64
    head = _head(tmp_path)
    assert head["sequence"] == 1
    assert head["last_checksum"] == row["checksum"]
    assert head["event_count"] == 1
    assert head["observation_count"] == 1
    assert head["latest_terminal_slot"] == _iso(WINDOW_END)
    assert store.head() == (1, row["checksum"])
    checkpoint = store.checkpoint()
    assert checkpoint["latest_terminal_slot"] == _iso(WINDOW_END)
    assert checkpoint["event_count"] == 1


def test_same_slot_replay_is_idempotent(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    first = store.append_event(_observation_event(WINDOW_END))

    replay = store.append_event(_observation_event(WINDOW_END))

    assert replay == first
    assert store.head() == (1, first["checksum"])
    assert len(store.events()) == 1


def test_same_slot_different_payload_fails_closed(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_observation_event(WINDOW_END, seed="a"))

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_slot_payload_conflict",
    ):
        store.append_event(_observation_event(WINDOW_END, seed="b"))


def test_terminal_slot_must_be_monotonic(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_observation_event(WINDOW_END + timedelta(minutes=5)))

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_slot_not_monotonic",
    ):
        store.append_event(_observation_event(WINDOW_END))


def test_terminal_slot_conflict_across_event_types(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_observation_event(WINDOW_END))

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_slot_payload_conflict",
    ):
        store.append_event(_gap_event(WINDOW_END - timedelta(minutes=10), WINDOW_END))


def test_data_reject_persists_failed_observed_at(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    observed_at = WINDOW_END + timedelta(seconds=56)
    event = _reject_event(WINDOW_END)
    event["observed_at"] = _iso(observed_at)

    stored = store.append_event(event)

    assert stored["observed_at"] == _iso(observed_at)
    assert store.data_reject_events()[0]["observed_at"] == _iso(observed_at)
    replay = store.append_event(event)
    assert replay == stored


def test_data_reject_rejects_invalid_observed_at(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    event = _reject_event(WINDOW_END)
    event["observed_at"] = "not-a-timestamp"

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_event_observed_at_invalid",
    ):
        store.append_event(event)


def test_data_reject_is_idempotent_per_attempt_and_preserves_changed_reason(
    tmp_path: Path,
) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    first = store.append_event(_reject_event(WINDOW_END))

    replay = store.append_event(_reject_event(WINDOW_END))

    assert replay == first
    assert len(store.data_reject_events()) == 1
    changed = store.append_event(
        _reject_event(WINDOW_END, reason_code="other_reason")
    )
    assert changed["sequence"] == 2
    assert changed["previous_checksum"] == first["checksum"]
    assert len(store.data_reject_events()) == 2
    assert store.event_for_slot("data_reject", _iso(WINDOW_END)) == changed


def test_data_reject_does_not_advance_terminal_slot(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_reject_event(WINDOW_END))

    row = store.append_event(_observation_event(WINDOW_END))

    assert row["event_type"] == "observation"
    assert store.checkpoint()["latest_terminal_slot"] == _iso(WINDOW_END)


def test_data_gap_advances_terminal_slot_to_recovery(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_observation_event(WINDOW_END))
    recovery = WINDOW_END + timedelta(minutes=30)

    gap = store.append_event(_gap_event(WINDOW_END, recovery))

    assert gap["event_type"] == "data_gap"
    assert store.checkpoint()["latest_terminal_slot"] == _iso(recovery)
    gaps = store.data_gap_events()
    assert len(gaps) == 1
    assert gaps[0]["recovery_market_slot"] == _iso(recovery)
    found = store.event_for_slot("data_gap", _iso(recovery))
    assert found is not None and found["event_id"] == gap["event_id"]


def test_data_gap_slot_math_is_enforced(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_observation_event(WINDOW_END))
    gap = _gap_event(WINDOW_END, WINDOW_END + timedelta(minutes=30))
    gap["skipped_to"] = _iso(WINDOW_END + timedelta(minutes=30))

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_gap_slot_invalid",
    ):
        store.append_event(gap)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority", "capital"),
        ("execution_eligible", True),
        ("capital_write_eligible", True),
        ("model_authority", True),
    ],
)
def test_authority_fields_are_fixed(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    event = _observation_event(WINDOW_END)
    event[field] = value

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_event_authority_invalid",
    ):
        store.append_event(event)


def test_chain_fields_are_forbidden_in_input(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    event = _observation_event(WINDOW_END)
    event["checksum"] = "0" * 64

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_event_chain_fields_forbidden",
    ):
        store.append_event(event)


def test_unknown_event_type_fails_closed(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    event = _observation_event(WINDOW_END)
    event["event_type"] = "capital_commit"

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_event_type_invalid",
    ):
        store.append_event(event)


def test_unaligned_window_end_fails_closed(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    event = _observation_event(WINDOW_END)
    event["window_end"] = _iso(WINDOW_END + timedelta(seconds=1))

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_slot_invalid",
    ):
        store.append_event(event)


def test_tampered_segment_fails_closed(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_observation_event(WINDOW_END))
    events_path = tmp_path / "events.jsonl"
    row = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    row["reason_code"] = "tampered"
    material = dict(row)
    material.pop("checksum")
    row["checksum"] = store_module._sha256(material)
    events_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_head_invalid",
    ):
        CryptoTenSymbolObservationStore(tmp_path).checkpoint()


def test_broken_chain_fails_closed_during_crash_rebuild(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.append_event(_observation_event(WINDOW_END))
    events_path = tmp_path / "events.jsonl"
    row = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    row["reason_code"] = "tampered"
    events_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    (tmp_path / "head.json").unlink()

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_events_chain_invalid",
    ):
        CryptoTenSymbolObservationStore(tmp_path).checkpoint()


def test_rotation_preserves_chain_across_segments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(store_module, "MAX_EVENTS_BYTES", 2048)
    store = CryptoTenSymbolObservationStore(tmp_path)
    appended = []
    for index in range(8):
        event = _observation_event(
            WINDOW_END + timedelta(minutes=5 * index),
            seed=str(index + 1),
        )
        # pad each event so a few rows fill one segment
        event["padding"] = "x" * 700
        appended.append(store.append_event(event))

    segments = sorted(tmp_path.glob("events.segment-*.jsonl"))
    assert segments
    rows = CryptoTenSymbolObservationStore(tmp_path).events()
    assert len(rows) == 8
    assert [row["sequence"] for row in rows] == list(range(1, 9))
    assert rows[-1]["checksum"] == appended[-1]["checksum"]
    for previous, current in zip(rows, rows[1:]):
        assert current["previous_checksum"] == previous["checksum"]
    head = _head(tmp_path)
    assert head["sequence"] == 8
    assert head["segment_count"] == len(segments)


def test_crash_after_segment_write_recovers_from_fsynced_events(
    tmp_path: Path,
) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    first = store.append_event(_observation_event(WINDOW_END, seed="1"))
    store.append_event(
        _observation_event(WINDOW_END + timedelta(minutes=5), seed="2")
    )
    # Crash between publishing the event file and the head: the head is lost.
    (tmp_path / "head.json").unlink()

    recovered = CryptoTenSymbolObservationStore(tmp_path)
    assert recovered.head() == (2, recovered.events()[-1]["checksum"])
    third = recovered.append_event(
        _observation_event(WINDOW_END + timedelta(minutes=10), seed="3")
    )
    assert third["sequence"] == 3
    assert third["previous_checksum"] != first["checksum"]


def test_crash_after_event_publish_rebuilds_head_and_missing_slot_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    event = _observation_event(WINDOW_END)
    original_write_index = store_module._write_immutable_json

    def fail_index_publish(path: Path, value: dict[str, Any]) -> dict[str, Any]:
        if path.parent.name == "slot_index":
            raise OSError("simulated crash after events fsync")
        return original_write_index(path, value)

    monkeypatch.setattr(store_module, "_write_immutable_json", fail_index_publish)
    with pytest.raises(OSError, match="simulated crash"):
        store.append_event(event)

    assert (tmp_path / "events.jsonl").exists()
    assert not list((tmp_path / "slot_index").glob("*.json"))
    monkeypatch.setattr(store_module, "_write_immutable_json", original_write_index)

    recovered = CryptoTenSymbolObservationStore(tmp_path)
    checkpoint = recovered.checkpoint()
    assert checkpoint["event_count"] == 1
    indexed = recovered.event_for_slot("observation", _iso(WINDOW_END))
    assert indexed is not None
    assert indexed["checksum"] == checkpoint["last_checksum"]
    assert recovered.append_event(event) == indexed
    assert len(recovered.events()) == 1


def test_crash_after_rotation_before_new_current_rebuilds_same_head(
    tmp_path: Path,
) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    first = store.append_event(_observation_event(WINDOW_END))

    # Simulate the atomic rotation having landed while the following current
    # file and new row have not.  The verified ledger is unchanged, only its
    # file representation differs from the stale head.
    os.replace(
        tmp_path / "events.jsonl",
        tmp_path / "events.segment-000001.jsonl",
    )

    recovered = CryptoTenSymbolObservationStore(tmp_path)
    assert recovered.head() == (1, first["checksum"])
    second = recovered.append_event(
        _observation_event(WINDOW_END + timedelta(minutes=5), seed="2")
    )
    assert second["sequence"] == 2
    assert second["previous_checksum"] == first["checksum"]


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_root_symlink_not_allowed",
    ):
        CryptoTenSymbolObservationStore(link)


def test_pending_marker_round_trip_and_conflict(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    record = {
        "window_end": _iso(WINDOW_END),
        "observation_cutoff": _iso(WINDOW_END + timedelta(seconds=55)),
        "profile_sha256": "1" * 64,
        "catalog_version": "fixture-catalog-v1",
    }
    assert store.pending_record() is None

    store.set_pending(record)
    pending = store.pending_record()
    assert pending is not None
    assert pending["window_end"] == record["window_end"]
    assert len(pending["pending_sha256"]) == 64

    store.set_pending(record)  # identical re-set is idempotent
    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_pending_conflict",
    ):
        store.set_pending({**record, "profile_sha256": "2" * 64})

    store.clear_pending(_iso(WINDOW_END))
    assert store.pending_record() is None
    store.clear_pending(_iso(WINDOW_END))  # already clear: no-op


def test_pending_marker_tamper_fails_closed(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    store.set_pending(
        {
            "window_end": _iso(WINDOW_END),
            "observation_cutoff": _iso(WINDOW_END + timedelta(seconds=55)),
            "profile_sha256": "1" * 64,
            "catalog_version": "fixture-catalog-v1",
        }
    )
    pending_path = tmp_path / "pending.json"
    payload = json.loads(pending_path.read_text(encoding="utf-8"))
    payload["window_end"] = _iso(WINDOW_END + timedelta(minutes=5))
    pending_path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_pending_invalid",
    ):
        store.pending_record()


def test_cycle_lock_serializes_invocations(tmp_path: Path) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    fd = os.open(store.cycle_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            with store.cycle(nonblocking=True):
                pass
    finally:
        os.close(fd)
