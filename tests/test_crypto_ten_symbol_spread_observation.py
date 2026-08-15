"""Book-ticker spread sampling tests for the ten-symbol accumulator.

The spread leg is auxiliary, degradation-tolerant evidence: each slot also
samples the ten ``.book_ticker`` current snapshots and persists them as an
immutable spreads sidecar, while the store event anchors only the derived
status block and the sidecar digest.  Spread failures of any kind —
catalog drift, stale/degraded metadata, watermark violations, invalid
quotes, transport faults — degrade to recorded per-symbol or leg-wide
statuses and must never cost the bar observation they ride on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from Crypto.market_observation import (
    OBSERVATION_SYMBOLS,
    TEN_SYMBOL_SPREAD_CONTRACT,
    build_spread_event_block,
    build_ten_symbol_bars_sidecar,
    validate_ten_symbol_spreads_sidecar,
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
from shared.data.sharedsignals_v1 import HTTPResponse
from tests.test_crypto_ten_symbol_observation_runtime import (
    _assert_recursive_non_authority,
    _factory,
    _forbidden_factory,
    _profile,
    _run,
    _runtime_paths,
)
from tests.test_crypto_ten_symbol_support import (
    OBSERVATION_SYMBOLS as FIXTURE_SYMBOLS,
    WINDOW_END,
    TenSymbolFixtureTransport,
    catalog_row,
    collect_fixture_observation,
    collect_fixture_spreads_sidecar,
    iso,
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _spread_digests(row: dict[str, Any]) -> tuple[str, str]:
    return (
        _canonical_sha256([{"symbol": row["symbol"]}]),
        _canonical_sha256([row]),
    )


def _spreads_sidecar_path(root: Path, window_end: object) -> Path:
    return CryptoTenSymbolObservationStore(root).spreads_sidecar_path(
        iso(window_end)
    )


def _run_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: Any,
) -> tuple[dict[str, Any], Path, Path]:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(transport),
    )
    assert receipt["status"] == "completed"
    return receipt, token_file, output_root


def test_completed_cycle_samples_all_book_tickers_into_event_and_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt, _, output_root = _run_completed(
        monkeypatch, tmp_path, TenSymbolFixtureTransport()
    )

    assert receipt["core_result"]["spread_status"] == "completed"
    store = CryptoTenSymbolObservationStore(output_root)
    event = store.events()[0]
    spread = event["spread"]
    assert spread["contract"] == TEN_SYMBOL_SPREAD_CONTRACT
    assert spread["status"] == "completed"
    assert spread["reason_code"] is None
    assert spread["sampled_symbol_count"] == 10
    assert spread["rejected_symbol_count"] == 0
    assert spread["rejected_reasons"] == {}
    _assert_recursive_non_authority(event)

    sidecar_path = _spreads_sidecar_path(output_root, WINDOW_END)
    assert sidecar_path.is_file()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    _assert_recursive_non_authority(sidecar)
    assert sidecar["window_end"] == iso(WINDOW_END)
    assert sidecar["profile_sha256"] == receipt["fresh_query_profile_sha256"]
    entries = sidecar["entries"]
    assert [entry["symbol"] for entry in entries] == list(OBSERVATION_SYMBOLS)
    for entry in entries:
        assert entry["status"] == "sampled"
        assert entry["dataset_id"] == (
            f"crypto.spot.binance.{entry['symbol'].lower()}.book_ticker"
        )
        identity_sha256, market_data_sha256 = _spread_digests(entry["row"])
        assert entry["identity_sha256"] == identity_sha256
        assert entry["market_data_sha256"] == market_data_sha256
        row = entry["row"]
        assert float(row["ask_price"]) > float(row["bid_price"])
    # The event anchors exactly the sidecar digest claim; a detached consumer
    # re-derives it from the persisted entries and compares byte-for-byte.
    assert sidecar["spread_sha256"] == _canonical_sha256(entries)
    assert spread["spread_sha256"] == sidecar["spread_sha256"]
    assert spread["catalog_version"] == sidecar["catalog_version"]
    assert validate_ten_symbol_spreads_sidecar(sidecar) == entries


def test_stale_book_ticker_degrades_one_symbol_without_losing_bars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def go_stale(dataset_id: str, metadata: dict[str, Any]) -> None:
        if dataset_id.endswith("ethusdt.book_ticker"):
            metadata["freshness"] = {"state": "stale", "stale": True}

    transport = TenSymbolFixtureTransport(book_ticker_metadata_mutator=go_stale)
    receipt, _, output_root = _run_completed(monkeypatch, tmp_path, transport)

    assert receipt["core_result"]["spread_status"] == "degraded"
    assert crypto_ten_symbol_observation_exit_code(receipt) == 0
    store = CryptoTenSymbolObservationStore(output_root)
    event = store.events()[0]
    spread = event["spread"]
    assert spread["status"] == "degraded"
    assert spread["sampled_symbol_count"] == 9
    assert spread["rejected_symbol_count"] == 1
    assert spread["rejected_reasons"] == {
        "ETHUSDT": "crypto_spread_metadata_invalid"
    }
    # The bar observation evidence is completely unaffected.
    assert len(event["observation"]["sources"]) == 10
    sidecar = json.loads(
        _spreads_sidecar_path(output_root, WINDOW_END).read_text(encoding="utf-8")
    )
    rejected = [
        entry for entry in sidecar["entries"] if entry["status"] == "rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0]["symbol"] == "ETHUSDT"
    assert rejected[0]["reason_code"] == "crypto_spread_metadata_invalid"
    assert rejected[0]["catalog_contract_sha256"] is not None


def test_missing_book_ticker_catalog_rows_degrade_leg_without_bar_reject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bar_only_rows = [catalog_row(symbol) for symbol in FIXTURE_SYMBOLS]
    transport = TenSymbolFixtureTransport(catalog_rows=bar_only_rows)
    receipt, _, output_root = _run_completed(monkeypatch, tmp_path, transport)

    assert receipt["core_result"]["spread_status"] == "unavailable"
    assert crypto_ten_symbol_observation_exit_code(receipt) == 0
    store = CryptoTenSymbolObservationStore(output_root)
    event = store.events()[0]
    spread = event["spread"]
    assert spread["status"] == "unavailable"
    assert spread["reason_code"] == "crypto_spread_contract_unavailable"
    assert spread["spread_sha256"] is None
    assert spread["catalog_version"] is None
    # A leg-wide degradation persists no sidecar and never costs the bars.
    assert not _spreads_sidecar_path(output_root, WINDOW_END).exists()
    assert store.checkpoint()["observation_count"] == 1
    assert store.data_reject_events() == []


def test_book_ticker_transport_failure_records_rejections_not_bar_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    delegate = TenSymbolFixtureTransport()

    def spread_dead_transport(**kwargs: Any) -> HTTPResponse:
        if kwargs["method"] == "POST" and str(
            kwargs["json_body"]["dataset_id"]
        ).endswith(".book_ticker"):
            raise TimeoutError("book ticker wire timed out")
        return delegate(**kwargs)

    receipt, _, output_root = _run_completed(
        monkeypatch, tmp_path, spread_dead_transport
    )

    assert receipt["core_result"]["spread_status"] == "unavailable"
    assert receipt["collect_attempts"] == 1
    store = CryptoTenSymbolObservationStore(output_root)
    event = store.events()[0]
    spread = event["spread"]
    assert spread["status"] == "unavailable"
    assert spread["reason_code"] is None
    assert spread["sampled_symbol_count"] == 0
    assert spread["rejected_symbol_count"] == 10
    assert set(spread["rejected_reasons"]) == set(OBSERVATION_SYMBOLS)
    assert set(spread["rejected_reasons"].values()) == {
        "crypto_spread_query_transport_failed"
    }
    # All-rejected legs still persist their rejection evidence as a sidecar.
    sidecar = json.loads(
        _spreads_sidecar_path(output_root, WINDOW_END).read_text(encoding="utf-8")
    )
    assert spread["spread_sha256"] == sidecar["spread_sha256"]
    assert store.checkpoint()["observation_count"] == 1


def test_crossed_or_invalid_quotes_are_rejected_per_symbol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def cross_book(dataset_id: str, row: dict[str, Any]) -> None:
        if dataset_id.endswith("btcusdt.book_ticker"):
            row["ask_price"] = row["bid_price"]
            row["bid_price"] = "999999"
        if dataset_id.endswith("solusdt.book_ticker"):
            row["bid_qty"] = "0"

    transport = TenSymbolFixtureTransport(book_ticker_row_mutator=cross_book)
    receipt, _, output_root = _run_completed(monkeypatch, tmp_path, transport)

    assert receipt["core_result"]["spread_status"] == "degraded"
    spread = CryptoTenSymbolObservationStore(output_root).events()[0]["spread"]
    assert spread["rejected_reasons"] == {
        "BTCUSDT": "crypto_spread_quote_invalid",
        "SOLUSDT": "crypto_spread_quote_invalid",
    }
    assert spread["sampled_symbol_count"] == 8


def test_book_ticker_observed_at_after_cutoff_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    transport = TenSymbolFixtureTransport(
        book_ticker_observed_at=WINDOW_END + timedelta(seconds=120)
    )
    receipt, _, output_root = _run_completed(monkeypatch, tmp_path, transport)

    assert receipt["core_result"]["spread_status"] == "unavailable"
    spread = CryptoTenSymbolObservationStore(output_root).events()[0]["spread"]
    assert spread["rejected_symbol_count"] == 10
    assert set(spread["rejected_reasons"].values()) == {
        "crypto_spread_watermark_invalid"
    }


def test_book_ticker_observed_at_before_slot_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    transport = TenSymbolFixtureTransport(
        book_ticker_observed_at=WINDOW_END - timedelta(seconds=1)
    )
    receipt, _, output_root = _run_completed(monkeypatch, tmp_path, transport)

    assert receipt["core_result"]["spread_status"] == "unavailable"
    spread = CryptoTenSymbolObservationStore(output_root).events()[0]["spread"]
    assert spread["rejected_symbol_count"] == 10
    assert set(spread["rejected_reasons"].values()) == {
        "crypto_spread_watermark_invalid"
    }


def test_spreads_sidecar_rejects_sample_before_its_slot() -> None:
    payload = collect_fixture_spreads_sidecar(
        WINDOW_END, profile_sha256=_profile().profile_sha256
    )
    payload["entries"][0]["observed_at"] = iso(WINDOW_END - timedelta(seconds=1))
    payload["spread_sha256"] = _canonical_sha256(payload["entries"])

    with pytest.raises(
        ValueError,
        match="crypto_observation_spreads_sidecar_invalid",
    ):
        validate_ten_symbol_spreads_sidecar(payload)


def test_same_slot_replay_keeps_spreads_sidecar_untouched_without_network(
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
    sidecar_bytes = _spreads_sidecar_path(output_root, WINDOW_END).read_bytes()

    replay = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=59),
        transport_factory=_forbidden_factory,
    )

    assert replay["status"] == "noop"
    assert replay["market_data_network_used"] is False
    assert _spreads_sidecar_path(output_root, WINDOW_END).read_bytes() == (
        sidecar_bytes
    )
    assert crypto_ten_symbol_observation_exit_code(replay) == 0


def _write_orphan_sidecars(output_root: Path) -> None:
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


def test_orphan_spreads_sidecar_reused_for_zero_network_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    # Crash window: both sidecars fsynced, no event appended yet.
    _write_orphan_sidecars(output_root)
    spread_payload = collect_fixture_spreads_sidecar(
        WINDOW_END, profile_sha256=_profile().profile_sha256
    )
    store = CryptoTenSymbolObservationStore(output_root)
    store.write_spreads_sidecar(spread_payload)

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
    assert receipt["core_result"]["spread_status"] == "completed"
    event = CryptoTenSymbolObservationStore(output_root).events()[0]
    assert event["spread"]["status"] == "completed"
    assert event["spread"]["spread_sha256"] == spread_payload["spread_sha256"]


def test_bars_orphan_without_spreads_sidecar_records_sidecar_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    # Crash window before the spreads sidecar write: the zero-network
    # recovery records an honest leg-wide degradation instead of inventing
    # evidence or requiring the network/token on the reuse path.
    _write_orphan_sidecars(output_root)

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_forbidden_factory,
    )

    assert receipt["status"] == "completed"
    assert receipt["market_data_network_used"] is False
    assert receipt["core_result"]["spread_status"] == "unavailable"
    event = CryptoTenSymbolObservationStore(output_root).events()[0]
    assert event["spread"]["status"] == "unavailable"
    assert event["spread"]["reason_code"] == "crypto_spread_sidecar_missing"
    assert event["spread"]["spread_sha256"] is None


def test_corrupt_spreads_sidecar_fails_closed_without_data_reject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    _write_orphan_sidecars(output_root)
    spread_payload = collect_fixture_spreads_sidecar(
        WINDOW_END, profile_sha256=_profile().profile_sha256
    )
    spread_payload["entries"][0]["row"]["bid_price"] = "999999"
    store = CryptoTenSymbolObservationStore(output_root)
    store.write_spreads_sidecar(spread_payload)

    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_spreads_sidecar_invalid",
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


def test_spreads_sidecar_immutable_write_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    store = CryptoTenSymbolObservationStore(tmp_path)
    payload = collect_fixture_spreads_sidecar(
        WINDOW_END, profile_sha256=_profile().profile_sha256
    )

    written = store.write_spreads_sidecar(payload)
    rewritten = store.write_spreads_sidecar(payload)
    assert rewritten == written
    assert store.read_spreads_sidecar(iso(WINDOW_END)) == written

    mutated = json.loads(json.dumps(payload))
    mutated["entries"][0]["row"]["ask_price"] = "1"
    with pytest.raises(
        CryptoTenSymbolObservationStoreError,
        match="ten_symbol_observation_artifact_content_conflict",
    ):
        store.write_spreads_sidecar(mutated)


def test_outage_gap_recovery_window_embeds_spread_block(
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
    assert gap["spread"]["contract"] == TEN_SYMBOL_SPREAD_CONTRACT
    assert gap["spread"]["status"] == "completed"
    assert gap["spread"]["sampled_symbol_count"] == 10
    sidecar = json.loads(
        _spreads_sidecar_path(output_root, current_end).read_text(encoding="utf-8")
    )
    assert sidecar["window_end"] == iso(current_end)
    assert gap["spread"]["spread_sha256"] == sidecar["spread_sha256"]
    _assert_recursive_non_authority(gap)


def test_spread_event_block_status_derivation() -> None:
    entries = json.loads(
        json.dumps(
            collect_fixture_spreads_sidecar(
                WINDOW_END, profile_sha256=_profile().profile_sha256
            )["entries"]
        )
    )
    block = build_spread_event_block(
        entries=entries,
        catalog_version="fixture-catalog",
        spread_sha256="0" * 64,
    )
    assert block["status"] == "completed"

    entries[3]["status"] = "rejected"
    entries[3] = {
        "symbol": entries[3]["symbol"],
        "dataset_id": entries[3]["dataset_id"],
        "status": "rejected",
        "reason_code": "crypto_spread_metadata_invalid",
        "catalog_contract_sha256": None,
    }
    block = build_spread_event_block(
        entries=entries,
        catalog_version="fixture-catalog",
        spread_sha256="0" * 64,
    )
    assert block["status"] == "degraded"
    assert block["sampled_symbol_count"] == 9
    assert block["rejected_reasons"] == {
        OBSERVATION_SYMBOLS[3]: "crypto_spread_metadata_invalid"
    }
