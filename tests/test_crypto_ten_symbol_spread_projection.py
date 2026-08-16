"""Tests for the detached ten-symbol realized-spread projection.

The projection consumes the observation store's event chain plus the
immutable spreads sidecars (mirroring the bars-sidecar precedent): every
terminal slot's event ``spread`` block is compared value-for-value against
the re-derived sidecar content before the slot is aggregated into per
symbol per UTC-day realized-spread statistics.  Missing sidecars exclude
the slot explicitly; corrupt sidecars or digest drift fail closed; rejected
entries never enter the spread statistics; reruns are idempotent.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from Crypto.ten_symbol_observation_store import CryptoTenSymbolObservationStore
from Crypto.ten_symbol_spread_projection import (
    CHECKPOINT_FILENAME,
    TEN_SYMBOL_SPREAD_PROJECTION_CHECKPOINT_CONTRACT,
    TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT,
    CryptoTenSymbolSpreadProjectionError,
    run_crypto_ten_symbol_spread_projection,
    ten_symbol_spread_projection_exit_code,
)
from tests.test_crypto_ten_symbol_observation_runtime import (
    _assert_recursive_non_authority,
    _factory,
    _profile,
    _run,
    _runtime_paths,
)
from tests.test_crypto_ten_symbol_support import (
    CATALOG_VERSION,
    OBSERVATION_SYMBOLS,
    WINDOW_END,
    TenSymbolFixtureTransport,
    catalog_row,
    generated_book_ticker_row,
    iso,
)


def _canonical_sha256(value: object, *, ensure_ascii: bool = False) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _accumulate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    count: int,
    *,
    start: datetime = WINDOW_END,
    transport_factory: Callable[[datetime], Any] | None = None,
) -> Path:
    if transport_factory is None:
        # Pin the receipt observed_at inside each slot's watermark window;
        # the fixture default is pinned to WINDOW_END and would reject every
        # later slot's spread leg.
        transport_factory = lambda end: TenSymbolFixtureTransport(  # noqa: E731
            observed_at=end + timedelta(seconds=20)
        )
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    for index in range(count):
        end = start + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(transport_factory(end)),
        )
        assert receipt["status"] == "completed"
    return output_root


def _store_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "evolution" not in path.as_posix()
    }


def _projection_dir(root: Path) -> Path:
    return root / "evolution" / "ten_symbol_spread_projection"


def _projection_files(root: Path) -> dict[str, bytes]:
    evolution = _projection_dir(root)
    if not evolution.exists():
        return {}
    return {
        path.relative_to(evolution).as_posix(): path.read_bytes()
        for path in sorted(evolution.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


def _artifacts(root: Path) -> list[dict[str, Any]]:
    artifacts_dir = _projection_dir(root) / "artifacts"
    if not artifacts_dir.exists():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(artifacts_dir.glob("*.json"))
    ]


def _checkpoint(root: Path) -> dict[str, Any]:
    return json.loads(
        (_projection_dir(root) / CHECKPOINT_FILENAME).read_text(encoding="utf-8")
    )


def _expected_bps(symbol: str) -> Decimal:
    row = generated_book_ticker_row(symbol)
    bid = Decimal(row["bid_price"])
    ask = Decimal(row["ask_price"])
    return ((ask - bid) / ((ask + bid) / Decimal(2)) * Decimal(10000)).quantize(
        Decimal("0.00000001")
    )


def _expected_spread_digests(row: dict[str, Any]) -> tuple[str, str]:
    return (
        _canonical_sha256([{"symbol": row["symbol"]}]),
        _canonical_sha256([row]),
    )


def test_projection_aggregates_realized_spreads_per_symbol_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 3)
    before = _store_bytes(output_root)

    result = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert result["contract"] == TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT
    assert result["status"] == "projected"
    assert result["terminal_slot_count"] == 3
    assert result["slots_with_spread_evidence"] == 3
    assert result["slots_feature_ineligible"] == 0
    assert result["slots_spread_unavailable"] == 0
    assert result["slots_sidecar_missing"] == 0
    assert result["sampled_entry_count"] == 30
    assert result["rejected_entry_count"] == 0
    assert result["projected_through_slot"] == iso(WINDOW_END + timedelta(minutes=5))
    assert ten_symbol_spread_projection_exit_code(result) == 0
    _assert_recursive_non_authority(result)
    assert _store_bytes(output_root) == before

    artifacts = _artifacts(output_root)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    _assert_recursive_non_authority(artifact)
    assert artifact["contract"] == TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT
    assert artifact["outcome_sha256"] == result["outcome_sha256"]
    material = dict(artifact)
    claimed = material.pop("artifact_sha256")
    assert claimed == result["artifact_sha256"]
    assert claimed == _canonical_sha256(material, ensure_ascii=True)
    assert artifact["spread_metric"]["aggregation_window"] == (
        "utc_calendar_day_per_symbol"
    )
    source = artifact["source"]
    assert len(source["spread_sources"]) == 3
    events = CryptoTenSymbolObservationStore(output_root).events_read_only()
    assert source["store_head_checksum"] == events[-1]["checksum"]
    assert [item["source_event_checksum"] for item in source["spread_sources"]] == [
        event["checksum"] for event in events
    ]
    assert source["skipped_slots"] == []

    day = WINDOW_END.date().isoformat()
    assert sorted(artifact["buckets"]) == sorted(OBSERVATION_SYMBOLS)
    for symbol in OBSERVATION_SYMBOLS:
        bucket = artifact["buckets"][symbol][day]
        assert bucket["sample_count"] == 3
        assert bucket["rejected_count"] == 0
        assert bucket["rejection_rate"] == "0"
        assert bucket["rejected_reason_counts"] == {}
        assert bucket["slot_count"] == 3
        expected = format(_expected_bps(symbol), "f")
        assert bucket["mean_bps"] == expected
        assert bucket["median_bps"] == expected
        assert bucket["min_bps"] == expected
        assert bucket["max_bps"] == expected
        assert bucket["first_slot"] == iso(WINDOW_END - timedelta(minutes=5))
        assert bucket["last_slot"] == iso(WINDOW_END + timedelta(minutes=5))
        assert bucket["first_observed_at"] is not None
    totals = artifact["totals"]
    assert totals["sample_count"] == 30
    assert totals["rejected_count"] == 0
    assert artifact["symbol_totals"]["BTCUSDT"]["sample_count"] == 3

    checkpoint = _checkpoint(output_root)
    _assert_recursive_non_authority(checkpoint)
    assert checkpoint["contract"] == TEN_SYMBOL_SPREAD_PROJECTION_CHECKPOINT_CONTRACT
    assert checkpoint["last_projected_outcome_sha256"] == result["outcome_sha256"]
    assert checkpoint["artifact_sha256"] == result["artifact_sha256"]
    material = dict(checkpoint)
    assert material.pop("checkpoint_sha256") == _canonical_sha256(
        material, ensure_ascii=True
    )


def test_projection_rerun_is_idempotent_no_new_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    before = _store_bytes(output_root)

    first = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    after_first = _projection_files(output_root)
    second = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    after_second = _projection_files(output_root)

    assert first["status"] == "projected"
    assert second["status"] == "no_new_outcome"
    assert second["outcome_sha256"] == first["outcome_sha256"]
    assert second["artifact_sha256"] == first["artifact_sha256"]
    assert ten_symbol_spread_projection_exit_code(second) == 0
    assert after_first == after_second
    assert _store_bytes(output_root) == before


def test_projection_advances_outcome_with_new_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    first = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    _accumulate(monkeypatch, tmp_path, 1, start=WINDOW_END + timedelta(minutes=10))

    second = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert second["status"] == "projected"
    assert second["outcome_sha256"] != first["outcome_sha256"]
    assert second["sampled_entry_count"] == 30
    artifacts = _artifacts(output_root)
    assert len(artifacts) == 2
    assert {artifact["outcome_sha256"] for artifact in artifacts} == {
        first["outcome_sha256"],
        second["outcome_sha256"],
    }
    assert _checkpoint(output_root)["last_projected_outcome_sha256"] == (
        second["outcome_sha256"]
    )


def test_missing_sidecar_slot_is_recorded_and_excluded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 3)
    missing_slot = WINDOW_END + timedelta(minutes=5)
    store = CryptoTenSymbolObservationStore(output_root)
    store.spreads_sidecar_path(iso(missing_slot)).unlink()

    result = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert result["status"] == "projected"
    assert result["slots_sidecar_missing"] == 1
    assert result["slots_with_spread_evidence"] == 2
    assert result["sampled_entry_count"] == 20
    artifact = _artifacts(output_root)[0]
    assert artifact["source"]["skipped_slots"] == [
        {"window_end": iso(missing_slot), "reason": "sidecar_missing"}
    ]
    day = WINDOW_END.date().isoformat()
    bucket = artifact["buckets"]["BTCUSDT"][day]
    assert bucket["sample_count"] == 2
    assert bucket["slot_count"] == 2


def test_corrupt_sidecar_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    store = CryptoTenSymbolObservationStore(output_root)
    path = store.spreads_sidecar_path(iso(WINDOW_END))
    path.write_bytes(b"not-json\n")

    with pytest.raises(
        CryptoTenSymbolSpreadProjectionError,
        match="ten_symbol_spread_projection_sidecar_invalid",
    ):
        run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert _projection_files(output_root) == {}


def test_sidecar_digest_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    store = CryptoTenSymbolObservationStore(output_root)
    path = store.spreads_sidecar_path(iso(WINDOW_END))
    payload = json.loads(path.read_text(encoding="utf-8"))
    # A self-consistent but drifted sidecar: every internal digest is
    # recomputed, so local validation passes and only the value-for-value
    # comparison against the store event can catch it.
    row = payload["entries"][0]["row"]
    row["bid_price"] = "111.00"
    identity_sha256, market_data_sha256 = _expected_spread_digests(row)
    payload["entries"][0]["identity_sha256"] = identity_sha256
    payload["entries"][0]["market_data_sha256"] = market_data_sha256
    payload["spread_sha256"] = _canonical_sha256(payload["entries"])
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CryptoTenSymbolSpreadProjectionError,
        match="ten_symbol_spread_projection_spread_digest_mismatch",
    ):
        run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert _projection_files(output_root) == {}


def test_rejected_entries_never_enter_spread_statistics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def go_stale(dataset_id: str, metadata: dict[str, Any]) -> None:
        if dataset_id.endswith("ethusdt.book_ticker"):
            metadata["freshness"] = {"state": "stale", "stale": True}

    output_root = _accumulate(
        monkeypatch,
        tmp_path,
        2,
        transport_factory=lambda end: TenSymbolFixtureTransport(
            observed_at=end + timedelta(seconds=20),
            book_ticker_metadata_mutator=go_stale,
        ),
    )

    result = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert result["status"] == "projected"
    assert result["sampled_entry_count"] == 18
    assert result["rejected_entry_count"] == 2
    artifact = _artifacts(output_root)[0]
    day = WINDOW_END.date().isoformat()
    eth = artifact["buckets"]["ETHUSDT"][day]
    assert eth["sample_count"] == 0
    assert eth["rejected_count"] == 2
    assert eth["rejection_rate"] == "1"
    assert eth["rejected_reason_counts"] == {"crypto_spread_metadata_invalid": 2}
    assert eth["mean_bps"] is None
    assert eth["median_bps"] is None
    btc = artifact["buckets"]["BTCUSDT"][day]
    assert btc["sample_count"] == 2
    assert btc["rejection_rate"] == "0"
    totals = artifact["totals"]
    assert totals["sample_count"] == 18
    assert totals["rejected_count"] == 2
    assert totals["rejection_rate"] == "0.1"


def test_all_rejected_slot_counts_rejections_without_samples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    delegate = TenSymbolFixtureTransport()

    def spread_dead_transport(**kwargs: Any) -> Any:
        if kwargs["method"] == "POST" and str(
            kwargs["json_body"]["dataset_id"]
        ).endswith(".book_ticker"):
            raise TimeoutError("book ticker wire timed out")
        return delegate(**kwargs)

    output_root = _accumulate(
        monkeypatch, tmp_path, 1, transport_factory=lambda end: spread_dead_transport
    )

    result = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert result["status"] == "insufficient_spread_samples"
    assert result["slots_with_spread_evidence"] == 1
    assert result["sampled_entry_count"] == 0
    assert result["rejected_entry_count"] == 10
    assert ten_symbol_spread_projection_exit_code(result) == 0
    assert _projection_files(output_root) == {}


def test_leg_wide_unavailable_slot_is_recorded_without_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bar_only_rows = [catalog_row(symbol) for symbol in OBSERVATION_SYMBOLS]
    output_root = _accumulate(
        monkeypatch,
        tmp_path,
        1,
        transport_factory=lambda end: TenSymbolFixtureTransport(
            catalog_rows=bar_only_rows
        ),
    )

    result = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert result["status"] == "insufficient_spread_samples"
    assert result["slots_spread_unavailable"] == 1
    assert result["slots_with_spread_evidence"] == 0
    assert result["sampled_entry_count"] == 0
    assert ten_symbol_spread_projection_exit_code(result) == 0
    assert _projection_files(output_root) == {}


def test_pre_feature_slot_without_spread_block_is_feature_ineligible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = _accumulate(monkeypatch, tmp_path, 1)
    event = dict(CryptoTenSymbolObservationStore(source_root).events()[0])
    for key in ("sequence", "previous_checksum", "checksum", "spread"):
        event.pop(key, None)
    fresh_root = tmp_path / "pre-feature-store"
    store = CryptoTenSymbolObservationStore(fresh_root)
    store.append_event(event)

    result = run_crypto_ten_symbol_spread_projection(output_root=fresh_root)

    assert result["status"] == "insufficient_spread_samples"
    assert result["terminal_slot_count"] == 1
    assert result["slots_feature_ineligible"] == 1
    assert result["slots_with_spread_evidence"] == 0
    assert ten_symbol_spread_projection_exit_code(result) == 0
    assert _projection_files(fresh_root) == {}


def test_data_gap_recovery_slot_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 1)
    token_file = tmp_path / "tradingdatas-crypto-read.token"
    current_end = WINDOW_END + timedelta(minutes=30)
    recovered = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_end + timedelta(seconds=55),
        transport_factory=_factory(
            TenSymbolFixtureTransport(observed_at=current_end + timedelta(seconds=20))
        ),
    )
    assert recovered["outage_gap_recovered"] is True

    result = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert result["status"] == "projected"
    assert result["terminal_slot_count"] == 2
    assert result["slots_with_spread_evidence"] == 2
    assert result["sampled_entry_count"] == 20
    artifact = _artifacts(output_root)[0]
    day = WINDOW_END.date().isoformat()
    assert artifact["buckets"]["BTCUSDT"][day]["sample_count"] == 2
    assert artifact["source"]["first_slot"] == iso(WINDOW_END - timedelta(minutes=5))
    assert artifact["source"]["last_slot"] == iso(current_end - timedelta(minutes=5))


def test_pending_marker_defers_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 1)
    store = CryptoTenSymbolObservationStore(output_root)
    store.set_pending(
        {
            "window_end": iso(WINDOW_END + timedelta(minutes=5)),
            "observation_cutoff": iso(WINDOW_END + timedelta(minutes=5, seconds=55)),
            "profile_sha256": _profile().profile_sha256,
            "catalog_version": CATALOG_VERSION,
        }
    )

    result = run_crypto_ten_symbol_spread_projection(output_root=output_root)

    assert result["status"] == "deferred_core_pending"
    assert ten_symbol_spread_projection_exit_code(result) == 0
    _assert_recursive_non_authority(result)
    assert _projection_files(output_root) == {}


def test_empty_store_reports_insufficient_samples(tmp_path: Path) -> None:
    root = tmp_path / "empty-store"
    CryptoTenSymbolObservationStore(root)

    result = run_crypto_ten_symbol_spread_projection(output_root=root)

    assert result["status"] == "insufficient_spread_samples"
    assert result["terminal_slot_count"] == 0
    assert result["sampled_entry_count"] == 0
    assert ten_symbol_spread_projection_exit_code(result) == 0
    assert _projection_files(root) == {}


def test_tampered_checkpoint_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 1)
    first = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert first["status"] == "projected"
    checkpoint_path = _projection_dir(output_root) / CHECKPOINT_FILENAME
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["artifact_sha256"] = "0" * 64
    checkpoint_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CryptoTenSymbolSpreadProjectionError,
        match="ten_symbol_spread_projection_checkpoint_invalid",
    ):
        run_crypto_ten_symbol_spread_projection(output_root=output_root)
