"""Bars-sidecar tests for the ten-symbol observation accumulator.

The sidecar persists each slot's validated raw bar rows next to the digest
claims already bound in the append-only store event, so detached consumers
can independently re-derive every per-source ``identity_sha256`` and
``market_data_sha256`` and compare them against the event evidence.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pytest

from Crypto.market_observation import (
    OBSERVATION_SYMBOLS,
    CryptoMarketObservationError,
    build_ten_symbol_bars_sidecar,
    observation_from_ten_symbol_bars_sidecar,
)
import Crypto.ten_symbol_observation_runtime as runtime_module
from Crypto.ten_symbol_observation_runtime import (
    CryptoTenSymbolObservationRuntimeError,
    crypto_ten_symbol_observation_exit_code,
)
from Crypto.ten_symbol_observation_store import (
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)
from tests.test_crypto_ten_symbol_observation_runtime import (
    _assert_recursive_non_authority,
    _factory,
    _forbidden_factory,
    _profile,
    _run,
    _runtime_paths,
)
from tests.test_crypto_ten_symbol_support import (
    WINDOW_END,
    TenSymbolFixtureTransport,
    collect_fixture_observation,
    iso,
)


def _sidecar_path(root: Path, window_end: object) -> Path:
    return CryptoTenSymbolObservationStore(root).bars_sidecar_path(iso(window_end))


def _recomputed_digests(rows: list[dict[str, object]]) -> tuple[str, str]:
    encoded_rows = json.dumps(
        rows, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    identities = json.dumps(
        [{"symbol": row["symbol"], "open_time": row["open_time"]} for row in rows],
        ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    import hashlib

    return (
        hashlib.sha256(identities).hexdigest(),
        hashlib.sha256(encoded_rows).hexdigest(),
    )


def test_completed_cycle_writes_verifiable_bars_sidecar_before_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )

    assert receipt["status"] == "completed"
    sidecar_path = _sidecar_path(output_root, WINDOW_END)
    assert sidecar_path.is_file()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    _assert_recursive_non_authority(sidecar)
    assert sidecar["window_end"] == iso(WINDOW_END)
    assert sidecar["profile_sha256"] == receipt["fresh_query_profile_sha256"]
    assert len(sidecar["sources"]) == 10
    assert [source["symbol"] for source in sidecar["sources"]] == list(
        OBSERVATION_SYMBOLS
    )

    store = CryptoTenSymbolObservationStore(output_root)
    event = store.events()[0]
    # The consumer recomputes every per-source row digest from the persisted
    # rows and compares them against the append-only store event claims.
    for event_source, sidecar_source in zip(
        event["observation"]["sources"], sidecar["sources"]
    ):
        assert sidecar_source["symbol"] == event_source["symbol"]
        rows = sidecar_source["rows"]
        assert len(rows) == 13
        identity_sha256, market_data_sha256 = _recomputed_digests(rows)
        assert identity_sha256 == event_source["identity_sha256"]
        assert market_data_sha256 == event_source["market_data_sha256"]
        assert sidecar_source["receipt_id"] == event_source["receipt_id"]
    assert (
        sidecar["observation_sha256"] == event["observation"]["observation_sha256"]
    )
    rebuilt, rows_by_symbol = observation_from_ten_symbol_bars_sidecar(sidecar)
    assert rebuilt.observation_sha256 == event["observation"]["observation_sha256"]
    assert rebuilt.market_data_sha256 == event["observation"]["market_data_sha256"]
    assert tuple(rows_by_symbol) == OBSERVATION_SYMBOLS


def test_same_slot_replay_keeps_sidecar_untouched_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"
    sidecar_bytes = _sidecar_path(output_root, WINDOW_END).read_bytes()

    replay = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=59),
        transport_factory=_forbidden_factory,
    )

    assert replay["status"] == "noop"
    assert replay["market_data_network_used"] is False
    assert _sidecar_path(output_root, WINDOW_END).read_bytes() == sidecar_bytes
    assert crypto_ten_symbol_observation_exit_code(replay) == 0


def test_sidecar_immutable_write_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    observation, rows = collect_fixture_observation(WINDOW_END)
    payload = build_ten_symbol_bars_sidecar(
        window=observation.window,
        profile_sha256=_profile().profile_sha256,
        observation=observation,
        rows_by_symbol=rows,
    )

    written = store.write_bars_sidecar(payload)
    rewritten = store.write_bars_sidecar(payload)
    assert rewritten == written
    assert store.read_bars_sidecar(iso(WINDOW_END)) == written

    mutated = json.loads(json.dumps(payload))
    mutated["sources"][0]["rows"][0]["close"] = "999999"
    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_artifact_content_conflict",
    ):
        store.write_bars_sidecar(mutated)

    with pytest.raises(CryptoMarketObservationError):
        observation_from_ten_symbol_bars_sidecar(mutated)


def test_orphan_sidecar_from_crash_window_is_reused_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    # Simulate a crash after the sidecar fsync but before the event append.
    store = CryptoTenSymbolObservationStore(output_root)
    observation, rows = collect_fixture_observation(WINDOW_END)
    store.write_bars_sidecar(
        build_ten_symbol_bars_sidecar(
            window=observation.window,
            profile_sha256=_profile().profile_sha256,
            observation=observation,
            rows_by_symbol=rows,
        )
    )

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_forbidden_factory,
    )

    assert receipt["status"] == "completed"
    assert receipt["market_data_network_used"] is False
    assert receipt["transport_factory_attempt_count"] == 0
    assert receipt["core_result"]["observation_sha256"] == (
        observation.observation_sha256
    )
    event = CryptoTenSymbolObservationStore(output_root).events()[0]
    assert event["observation"]["observation_sha256"] == (
        observation.observation_sha256
    )
    assert store.pending_record() is None


def test_corrupt_sidecar_fails_closed_without_data_reject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    store = CryptoTenSymbolObservationStore(output_root)
    observation, rows = collect_fixture_observation(WINDOW_END)
    payload = build_ten_symbol_bars_sidecar(
        window=observation.window,
        profile_sha256=_profile().profile_sha256,
        observation=observation,
        rows_by_symbol=rows,
    )
    payload["sources"][0]["rows"][0]["close"] = "999999"
    store.write_bars_sidecar(payload)

    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_bars_sidecar_invalid",
    ):
        _run(
            tmp_path,
            token_file,
            output_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_forbidden_factory,
        )

    store = CryptoTenSymbolObservationStore(output_root)
    assert store.events() == []
    assert store.data_reject_events() == []


def test_outage_gap_recovery_window_writes_matching_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"
    current_end = WINDOW_END + timedelta(minutes=30)
    transport = TenSymbolFixtureTransport(
        observed_at=current_end + timedelta(seconds=20)
    )

    recovered = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_end + timedelta(seconds=55),
        transport_factory=_factory(transport),
    )

    assert recovered["status"] == "completed"
    assert recovered["outage_gap_recovered"] is True
    store = CryptoTenSymbolObservationStore(output_root)
    gap = store.data_gap_events()[0]
    sidecar_path = _sidecar_path(output_root, current_end)
    assert sidecar_path.is_file()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["window_end"] == iso(current_end)
    assert (
        sidecar["observation_sha256"]
        == gap["recovery_observation"]["observation_sha256"]
    )
    rebuilt, _ = observation_from_ten_symbol_bars_sidecar(sidecar)
    assert rebuilt.observation_sha256 == (
        gap["recovery_observation"]["observation_sha256"]
    )
    assert _sidecar_path(output_root, WINDOW_END).is_file()


def test_pre_sidecar_slots_have_no_sidecar_and_replay_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert receipt["status"] == "completed"
    # A slot recorded by a pre-sidecar release simply has no bars file; the
    # replay path never invents one and never needs the network.
    sidecar = _sidecar_path(output_root, WINDOW_END)
    sidecar.unlink()
    replay = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=59),
        transport_factory=_forbidden_factory,
    )
    assert replay["status"] == "noop"
    assert not sidecar.exists()
    assert runtime_module.crypto_ten_symbol_observation_exit_code(replay) == 0
