"""Fail-closed rolling evaluation tests over a local immutable store copy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

import Crypto.forty_symbol_rolling_evaluation as rolling
import Crypto.ten_symbol_observation_store as store_module
from Crypto.forty_symbol_rolling_evaluation import (
    ENTRY_FEE,
    ENTRY_THRESHOLD,
    EXIT_FEE,
    FortySymbolRollingEvaluationError,
    SLIPPAGE_RATE,
    _evaluate_segment,
    _first_executable_entry_index,
    _round_trip_net,
    build_artifact,
    main,
)
from Crypto.market_observation import (
    OBSERVATION_SYMBOLS_V40,
    CryptoMarketObservation,
    CryptoObservationSource,
    CryptoObservationWindow,
    _canonical_sha256,
    _recomputed_identity_sha256,
    _recomputed_market_data_sha256,
    build_ten_symbol_bars_sidecar,
)
from Crypto.ten_symbol_observation_store import (
    FORTY_SYMBOL_CONTRACTS,
    CryptoTenSymbolObservationStore,
)

SYMBOLS = OBSERVATION_SYMBOLS_V40


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rows(symbol: str, window: CryptoObservationWindow) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for offset in range(13):
        open_time = window.first_open_time + timedelta(minutes=5 * offset)
        # Absolute-time prices make overlapping sidecars byte-identical.
        price = Decimal("100") + Decimal(open_time.timestamp()) / Decimal("1000000")
        text = format(price, "f")
        rows.append(
            {
                "symbol": symbol,
                "open_time": _iso(open_time),
                "close_time": _iso(
                    open_time + timedelta(minutes=5) - timedelta(milliseconds=1)
                ),
                "open": text,
                "high": text,
                "low": text,
                "close": text,
                "volume": "1",
                "quote_volume": "1",
                "trade_count": 1,
            }
        )
    return rows


def _observation_and_rows(
    window: CryptoObservationWindow,
) -> tuple[CryptoMarketObservation, dict[str, list[dict[str, object]]]]:
    rows_by_symbol: dict[str, list[dict[str, object]]] = {}
    sources: list[CryptoObservationSource] = []
    for symbol in SYMBOLS:
        rows = _rows(symbol, window)
        rows_by_symbol[symbol] = rows
        sources.append(
            CryptoObservationSource(
                symbol=symbol,
                dataset_id=f"crypto.spot.binance.{symbol.lower()}.5m",
                row_count=len(rows),
                page_count=1,
                receipt_id=f"receipt-{symbol.lower()}-{int(window.window_end.timestamp())}",
                data_through=window.last_open_time + timedelta(minutes=5) - timedelta(milliseconds=1),
                observed_at=window.window_end + timedelta(seconds=20),
                identity_sha256=_recomputed_identity_sha256(rows),
                market_data_sha256=_recomputed_market_data_sha256(rows),
                semantic_sha256="1" * 64,
                pagination_trace_sha256="2" * 64,
            )
        )
    market_data = {
        "contract": "tradingagent.crypto.market_observation.v1",
        "catalog_version": "test-catalog-v1",
        "window_end": _iso(window.window_end),
        "sources": [source.to_market_data_payload() for source in sources],
    }
    market_data_sha256 = _canonical_sha256(market_data)
    observation_payload = {
        "contract": "tradingagent.crypto.market_observation.v1",
        "catalog_version": "test-catalog-v1",
        "window_end": _iso(window.window_end),
        "observation_cutoff": _iso(window.observation_cutoff),
        "sources": [source.to_payload() for source in sources],
        "market_data_sha256": market_data_sha256,
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }
    return (
        CryptoMarketObservation(
            catalog_version="test-catalog-v1",
            window=window,
            sources=tuple(sources),
            market_data_sha256=market_data_sha256,
            observation_sha256=_canonical_sha256(observation_payload),
            symbols=SYMBOLS,
        ),
        rows_by_symbol,
    )


def _store_with_slots(tmp_path: Path, slots: int = 4) -> CryptoTenSymbolObservationStore:
    root = tmp_path / "forty-store"
    store = CryptoTenSymbolObservationStore(root, contracts=FORTY_SYMBOL_CONTRACTS)
    start = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    for index in range(slots):
        window = CryptoObservationWindow(
            window_end=start + timedelta(minutes=5 * index),
            observation_cutoff=start + timedelta(minutes=5 * index, seconds=270),
        )
        observation, rows_by_symbol = _observation_and_rows(window)
        sidecar = build_ten_symbol_bars_sidecar(
            window=window,
            profile_sha256="a" * 64,
            observation=observation,
            rows_by_symbol=rows_by_symbol,
            bars_sidecar_contract=FORTY_SYMBOL_CONTRACTS.bars_sidecar,
        )
        store.write_bars_sidecar(sidecar)
        store.append_event(
            {
                "contract": FORTY_SYMBOL_CONTRACTS.event,
                "event_id": f"crypto-forty-observation-{index:024x}",
                "event_type": "observation",
                "window_end": _iso(window.window_end),
                "observation_cutoff": _iso(window.observation_cutoff),
                "catalog_version": observation.catalog_version,
                "profile_sha256": "a" * 64,
                "observation": observation.to_payload(),
                "authority": "none",
                "execution_eligible": False,
                "capital_write_eligible": False,
                "model_authority": False,
            }
        )
    return store


def _artifact(store: CryptoTenSymbolObservationStore) -> dict[str, object]:
    return build_artifact(store_root=store.root, replay_command="test-replay")


def test_full_store_happy_path_is_receipt_integrity_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    result = _artifact(_store_with_slots(tmp_path))

    assert result["authority"] == "none"
    assert result["source_receipt_integrity_verified"] is True
    assert result["tradeable_pit_verified"] is False
    assert result["receipt_bound_pit"] is False
    assert result["generated_from"]["store_read_mode"] == "events_read_only_with_immutable_head_anchor"  # type: ignore[index]
    assert result["segment"]["slot_count"] == 4  # type: ignore[index]
    assert result["evaluation"]["baseline_buy_hold"]["tradeable_pit_verified"] is False  # type: ignore[index]


def test_canonical_store_rejects_forged_event_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path)
    lines = store.events_path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[0])
    forged["observation"]["observation_sha256"] = "f" * 64
    lines[0] = json.dumps(forged, sort_keys=True, separators=(",", ":"))
    store.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(FortySymbolRollingEvaluationError, match="store_read_invalid"):
        _artifact(store)


def test_sidecar_source_metadata_must_rebuild_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path)
    sidecar = store.bars_sidecar_path("2026-08-30T04:00:00Z")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["sources"][0]["receipt_id"] = "forged-receipt"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    result = _artifact(store)
    # The forged oldest slot is ineligible and cuts the accepted prefix; the
    # intact suffix remains valid source evidence.
    assert result["segment"]["slot_count"] == 3  # type: ignore[index]
    assert result["segment"]["dropped_prefix_receipts"] == [  # type: ignore[index]
        "2026-08-30 04:00:00+00:00"
    ]


def test_head_mismatch_fails_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path)
    head = json.loads(store.head_path.read_text(encoding="utf-8"))
    head["last_checksum"] = "0" * 64
    material = dict(head)
    material.pop("head_sha256")
    head["head_sha256"] = _canonical_sha256(material)
    store.head_path.write_text(json.dumps(head), encoding="utf-8")

    with pytest.raises(FortySymbolRollingEvaluationError, match="store_head_invalid"):
        _artifact(store)


def test_tail_file_is_not_an_accepted_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path)
    tail_root = tmp_path / "tail-only"
    tail_root.mkdir()
    (tail_root / "slot_index").mkdir()
    (tail_root / "head.json").write_text(
        store.head_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tail_root / "events.jsonl").write_text(
        store.events_path.read_text(encoding="utf-8").splitlines()[-1] + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FortySymbolRollingEvaluationError, match="store_read_invalid"):
        build_artifact(store_root=tail_root, replay_command="test-replay")


def test_cli_does_not_accept_free_events_or_bars_arguments() -> None:
    with pytest.raises(SystemExit, match="2"):
        main(["--events", "tail.jsonl", "--bars-dir", "bars"])


def test_head_advance_requests_retry_not_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path)
    original = rolling._read_head_bytes
    calls = 0

    def advancing_head(current: CryptoTenSymbolObservationStore) -> bytes:
        nonlocal calls
        calls += 1
        value = original(current)
        return value if calls == 1 else value + b"advance"

    monkeypatch.setattr(rolling, "_read_head_bytes", advancing_head)
    with pytest.raises(FortySymbolRollingEvaluationError, match="store_advanced_retry"):
        _artifact(store)


def test_entry_requires_bar_after_all_observed_inputs() -> None:
    start = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    open_times = [_iso(start + timedelta(minutes=5 * index)) for index in range(14)]
    available = [
        start + timedelta(minutes=5 * index, seconds=20) for index in range(14)
    ]
    assert _first_executable_entry_index(open_times, available, 12) == (13, available[12])
    assert _first_executable_entry_index(open_times[:13], available[:13], 12) is None


def test_signal_close_is_not_used_as_its_own_entry_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    open_times = [_iso(start + timedelta(minutes=5 * index)) for index in range(15)]
    rows = [
        {
            "open": str(Decimal("100") + index),
            "high": str(Decimal("100") + index),
            "low": str(Decimal("100") + index),
            "close": str(Decimal("100") + index),
        }
        for index in range(15)
    ]
    # A close is first observable after its bar closes.  The frozen signal at
    # index 12 therefore cannot enter before the index-14 open: the index-13
    # open precedes its observed_at timestamp.
    available = [
        start + timedelta(minutes=5 * (index + 1), seconds=20)
        for index in range(15)
    ]
    calls: list[tuple[Decimal, int]] = []
    original = rolling._simulate_path

    def capture(
        highs: list[Decimal],
        lows: list[Decimal],
        closes: list[Decimal],
        entry_price: Decimal,
        entry_index: int,
    ) -> dict[str, object]:
        calls.append((entry_price, entry_index))
        return original(highs, lows, closes, entry_price, entry_index)

    monkeypatch.setattr(rolling, "_simulate_path", capture)
    result = _evaluate_segment(
        {
            "open_times": open_times,
            "bars_by_symbol": {symbol: rows for symbol in SYMBOLS},
            "available_at_by_symbol": {symbol: available for symbol in SYMBOLS},
        }
    )
    assert result["trips_total"] == len(SYMBOLS)
    assert {entry_index for _, entry_index in calls} == {14}
    assert {entry_price for entry_price, _ in calls} == {Decimal("114")}


def test_signal_without_later_executable_bar_is_an_abstention() -> None:
    start = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    open_times = [_iso(start + timedelta(minutes=5 * index)) for index in range(13)]
    rows = [
        {
            "open": str(Decimal("100") + index),
            "high": str(Decimal("100") + index),
            "low": str(Decimal("100") + index),
            "close": str(Decimal("100") + index),
        }
        for index in range(13)
    ]
    available = [
        start + timedelta(minutes=5 * (index + 1), seconds=20)
        for index in range(13)
    ]
    result = _evaluate_segment(
        {
            "open_times": open_times,
            "bars_by_symbol": {symbol: rows for symbol in SYMBOLS},
            "available_at_by_symbol": {symbol: available for symbol in SYMBOLS},
        }
    )
    assert result["trips_total"] == 0
    assert result["abstentions_no_later_observed_bar"] == len(SYMBOLS)


def test_round_trip_net_matches_declared_costs() -> None:
    one = Decimal(1)
    gross = Decimal("0.03")
    expected = (
        (one + gross)
        / (one + ENTRY_FEE)
        * (one - SLIPPAGE_RATE)
        * (one - EXIT_FEE)
        * (one - SLIPPAGE_RATE)
        - one
    )
    assert _round_trip_net(gross) == expected
    assert _round_trip_net(Decimal(0)) < 0


def test_entry_threshold_is_frozen_champion_value() -> None:
    assert ENTRY_THRESHOLD == Decimal("0.001")
    assert ENTRY_FEE == Decimal("0.001")
    assert EXIT_FEE == Decimal("0.001")
    assert SLIPPAGE_RATE == Decimal("0.0002")


def test_open_entry_includes_own_bar_stop_before_target() -> None:
    result = rolling._simulate_path(
        [Decimal("104")], [Decimal("97")], [Decimal("101")], Decimal("100"), 0
    )
    assert result["resolved"] is True
    assert result["exit_offset_bars"] == 0
    assert result["exit_reason"] == "stop_loss"
    assert result["gross"] == Decimal("-0.02")


@pytest.mark.parametrize("field,value", [
    ("receipt_id", "forged"),
    ("observed_at", "2026-08-30T04:15:21Z"),
    ("semantic_sha256", "f" * 64),
    ("pagination_trace_sha256", "e" * 64),
])
def test_latest_sidecar_metadata_tampering_has_no_eligible_suffix(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path)
    path = store.bars_sidecar_path("2026-08-30T04:15:00Z")
    payload = json.loads(path.read_text())
    payload["sources"][0][field] = value
    path.write_text(json.dumps(payload))
    before = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    with pytest.raises(FortySymbolRollingEvaluationError, match="success_events_empty"):
        _artifact(store)
    assert before == {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("field,value", [
    ("sequence", 200), ("current_file_sha256", "f" * 64),
    ("latest_event_checksum", "e" * 64), ("segment_count", 9),
])
def test_head_payload_rehashed_but_not_bound_to_full_chain_is_rejected(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path)
    head = json.loads(store.head_path.read_text())
    head[field] = value
    material = dict(head)
    material.pop("head_sha256")
    head["head_sha256"] = _canonical_sha256(material)
    store.head_path.write_text(json.dumps(head))
    before = store.head_path.read_bytes()
    with pytest.raises(FortySymbolRollingEvaluationError):
        _artifact(store)
    assert store.head_path.read_bytes() == before


def test_forged_lone_genesis_cannot_use_arbitrary_sequence_and_checksums(tmp_path, monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    store = _store_with_slots(tmp_path, slots=1)
    event = json.loads(store.events_path.read_text())
    event.update(sequence=200, checksum="x" * 64, previous_checksum="not_genesis")
    store.events_path.write_text(json.dumps(event) + "\n")
    with pytest.raises(FortySymbolRollingEvaluationError, match="store_read_invalid"):
        _artifact(store)


def test_cli_refuses_output_within_source_even_through_alias(tmp_path):
    store = _store_with_slots(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(store.root, target_is_directory=True)
    before = store.head_path.read_bytes()
    with pytest.raises(FortySymbolRollingEvaluationError, match="output_inside_source"):
        main(["--store-root", str(store.root), "--out-json", str(alias / "head.json")])
    assert store.head_path.read_bytes() == before


def test_complete_multi_segment_store_is_accepted_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    monkeypatch.setattr(store_module, "MAX_EVENTS_BYTES", 35_000)
    store = _store_with_slots(tmp_path, slots=5)
    assert len(store._segment_paths()) >= 2
    before = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    result = _artifact(store)
    assert result["generated_from"]["head_sequence"] == 5
    assert result["segment"]["slot_count"] == 5
    assert before == {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
