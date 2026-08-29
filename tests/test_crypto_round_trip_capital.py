from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import pytest

import Crypto.delayed_paper_round_trip as round_trip_runner_module
import Crypto.round_trip_capital as capital_module
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.five_minute_data import (
    CryptoFiveMinuteWindowRequest,
    TradingDatasCryptoFiveMinuteDataPort,
)
from Crypto.round_trip_capital import (
    CryptoRoundTripError,
    ROUND_TRIP_CAPITAL_POLICY,
    RoundTripCapitalLedger,
    run_round_trip_fixture_cycle,
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
)


def _inputs(
    *,
    minutes: int = 0,
    price_multiplier: Decimal = Decimal("1"),
) -> tuple[Any, Any, CryptoFiveMinuteWindowRequest]:
    delta = timedelta(minutes=minutes)
    rows = bar_rows()
    for row in rows:
        for field_name in ("open_time", "close_time"):
            parsed = datetime.fromisoformat(str(row[field_name]).replace("Z", "+00:00"))
            row[field_name] = iso(parsed + delta)
        if price_multiplier != Decimal("1"):
            for field_name in ("open", "high", "low", "close"):
                scaled = (Decimal(str(row[field_name])) * price_multiplier).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                )
                row[field_name] = format(scaled, "f")
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
        bars=rows,
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
    )


def _capital_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted((root / "round_trip_capital").rglob("*"))
        if path.is_file()
    }


def _direct_payload(
    *,
    fixture_id: str,
    slot: str,
    action: str = "buy",
    regime_return: str = "0.01",
    decision_return: str = "0.01",
    bid: str = "99999.99",
    ask: str = "100000.01",
) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "symbol": "BTCUSDT",
        "execution_slot": slot,
        "decision": {
            "action": action,
            "regime_return": regime_return,
            "decision_return": decision_return,
            "decision_id": f"decision-{fixture_id}",
        },
        "quote": {"bid": bid, "ask": ask},
        "instrument": {
            "price_tick": "0.01",
            "quantity_step": "0.00001",
            "min_quantity": "0.00001",
            "min_notional": "5",
        },
        "evidence_receipt_id": f"receipt-{fixture_id}",
        "market_evidence_sha256": "a" * 64,
        "champion_id": "crypto-fixture-champion-v1",
        "champion_sha256": "b" * 64,
    }


def test_new_generation_round_trip_runner_buys_then_sells_at_next_closed_bar(
    tmp_path: Path,
) -> None:
    first_port, first_profile, first_request = _inputs()
    first = run_crypto_delayed_paper_round_trip_once(
        port=first_port,
        profile=first_profile,
        request=first_request,
        output_root=tmp_path,
    )
    assert first["status"] == "completed"
    assert first["capital_generation"] == 2
    assert first["capital_authority_id"] == "crypto-round-trip-capital-v1"
    assert {item["capital"]["order"]["side"] for item in first["symbols"].values()} == {
        "buy"
    }

    port, frozen, request = _inputs(
        minutes=5,
        price_multiplier=Decimal("1.05"),
    )
    second = run_crypto_delayed_paper_round_trip_once(
        port=port,
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )
    assert {
        item["capital"]["order"]["side"] for item in second["symbols"].values()
    } == {"sell"}
    assert {item["capital"]["exit_reason"] for item in second["symbols"].values()} == {
        "take_profit_threshold_reached"
    }
    final = second["capital"]
    assert final["positions"] == {}
    assert Decimal(final["cash"]) > Decimal("10000")
    assert Decimal(final["fees"]) > Decimal("0")
    assert final["balanced"] is True
    assert final["real_trading_enabled"] is False
    assert final["execution_authority"] is False
    assert final["production_eligible"] is False

    before = _capital_bytes(tmp_path)
    replay_port, replay_profile, replay_request = _inputs(
        minutes=5,
        price_multiplier=Decimal("1.05"),
    )
    replay = run_crypto_delayed_paper_round_trip_once(
        port=replay_port,
        profile=replay_profile,
        request=replay_request,
        output_root=tmp_path,
    )
    assert replay["idempotent_replay"] is True
    assert _capital_bytes(tmp_path) == before


def test_partial_and_rejected_sell_receipts_preserve_capital(
    tmp_path: Path,
) -> None:
    port, frozen, request = _inputs()
    first = run_crypto_delayed_paper_round_trip_once(
        port=port,
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )
    btc_quantity = Decimal(first["capital"]["positions"]["BTCUSDT"]["quantity"])

    exit_port, exit_profile, exit_request = _inputs(
        minutes=5,
        price_multiplier=Decimal("1.05"),
    )
    partial = run_crypto_delayed_paper_round_trip_once(
        port=exit_port,
        profile=exit_profile,
        request=exit_request,
        output_root=tmp_path,
        paper_fill_capacities={"BTCUSDT": btc_quantity / Decimal("2")},
    )
    btc = partial["symbols"]["BTCUSDT"]["capital"]
    assert btc["receipt"]["status"] == "fixture_partially_simulated"
    assert Decimal(partial["capital"]["positions"]["BTCUSDT"]["quantity"]) > 0

    next_port, next_profile, next_request = _inputs(
        minutes=10,
        price_multiplier=Decimal("1.06"),
    )
    cash_before = Decimal(partial["capital"]["cash"])
    position_before = dict(partial["capital"]["positions"]["BTCUSDT"])
    rejected = run_crypto_delayed_paper_round_trip_once(
        port=next_port,
        profile=next_profile,
        request=next_request,
        output_root=tmp_path,
        paper_fill_capacities={"BTCUSDT": Decimal("0")},
    )
    btc_reject = rejected["symbols"]["BTCUSDT"]["capital"]
    assert btc_reject["receipt"]["status"] == "fixture_rejected"
    assert btc_reject["receipt"]["filled_quantity"] == "0"
    assert Decimal(btc_reject["capital"]["cash"]) == cash_before
    assert btc_reject["capital"]["positions"]["BTCUSDT"] == position_before


def test_round_trip_missing_head_fails_closed_without_duplicate_fill(
    tmp_path: Path,
) -> None:
    port, frozen, request = _inputs()
    run_crypto_delayed_paper_round_trip_once(
        port=port,
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )
    exit_port, exit_profile, exit_request = _inputs(
        minutes=5,
        price_multiplier=Decimal("1.05"),
    )
    completed = run_crypto_delayed_paper_round_trip_once(
        port=exit_port,
        profile=exit_profile,
        request=exit_request,
        output_root=tmp_path,
    )
    events_path = tmp_path / "round_trip_capital" / "events.jsonl"
    event_count = len(events_path.read_text(encoding="utf-8").splitlines())
    (tmp_path / "round_trip_capital" / "head.json").unlink()

    replay_port, replay_profile, replay_request = _inputs(
        minutes=5,
        price_multiplier=Decimal("1.05"),
    )
    with pytest.raises(CryptoRoundTripError, match="runtime_state_stale"):
        run_crypto_delayed_paper_round_trip_once(
            port=replay_port,
            profile=replay_profile,
            request=replay_request,
            output_root=tmp_path,
        )
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == event_count
    assert completed["capital"]["head_sequence"] == event_count


def test_same_cycle_conflicting_fill_report_fails_closed(tmp_path: Path) -> None:
    payload = _direct_payload(
        fixture_id="fixture-direct",
        slot="2026-07-30T00:00:00Z",
    )
    first = run_round_trip_fixture_cycle(payload, output_root=tmp_path)
    assert first["order"]["side"] == "buy"
    conflict = json.loads(json.dumps(payload))
    conflict["quote"]["ask"] = "100100.01"
    with pytest.raises(CryptoRoundTripError, match="reference_conflict"):
        run_round_trip_fixture_cycle(conflict, output_root=tmp_path)


def test_writer_cycle_reuses_one_validated_capital_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_count = 0
    original_replay = RoundTripCapitalLedger._replay

    def counted_replay(
        self: RoundTripCapitalLedger,
        rows: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal replay_count
        replay_count += 1
        return original_replay(self, rows)

    monkeypatch.setattr(RoundTripCapitalLedger, "_replay", counted_replay)
    payload = _direct_payload(
        fixture_id="fixture-cycle-cache",
        slot="2026-07-30T00:00:00Z",
    )

    result = run_round_trip_fixture_cycle(payload, output_root=tmp_path)

    assert result["idempotent_replay"] is False
    assert replay_count == 0

    replay = run_round_trip_fixture_cycle(payload, output_root=tmp_path)

    assert replay["idempotent_replay"] is True
    assert replay_count == 0


def test_nonempty_missing_runtime_state_requires_explicit_full_audit_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _direct_payload(fixture_id="runtime-legacy", slot="2026-07-30T00:00:00Z")
    run_round_trip_fixture_cycle(payload, output_root=tmp_path)
    ledger_root = tmp_path / "round_trip_capital"
    runtime_path = ledger_root / "runtime_state.json"
    runtime_path.unlink()
    ledger = RoundTripCapitalLedger(
        ledger_root, _capability=capital_module._WRITE_CAPABILITY
    )
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_read_rows_unlocked",
        lambda self: (_ for _ in ()).throw(AssertionError("writer history scan")),
    )
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_replay",
        lambda self, rows: (_ for _ in ()).throw(AssertionError("writer replay")),
    )
    with ledger.cycle():
        with pytest.raises(CryptoRoundTripError, match="runtime_state_missing"):
            ledger.state_for_writer()
        with pytest.raises(CryptoRoundTripError, match="runtime_state_missing"):
            ledger.event_for_writer("missing")
        with pytest.raises(CryptoRoundTripError, match="runtime_state_missing"):
            ledger.ensure_opening()

    monkeypatch.undo()
    with ledger.cycle():
        rebuilt = ledger.runtime_state_payload_for_rebuild()
    runtime_path.write_text(capital_module._canonical_json(rebuilt) + "\n", encoding="utf-8")
    assert json.loads(runtime_path.read_text(encoding="utf-8"))["sequence"] == 2


def test_runtime_state_missing_or_stale_fails_closed_without_history_scan(
    tmp_path: Path,
) -> None:
    payload = _direct_payload(fixture_id="runtime-stale", slot="2026-07-30T00:00:00Z")
    run_round_trip_fixture_cycle(payload, output_root=tmp_path)
    root = tmp_path / "round_trip_capital"
    runtime = root / "runtime_state.json"
    state = json.loads(runtime.read_text(encoding="utf-8"))
    state["events_fingerprint"][2] += 1
    state["state_sha256"] = capital_module._sha256(
        {k: v for k, v in state.items() if k != "state_sha256"}
    )
    runtime.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CryptoRoundTripError, match="runtime_state_invalid"):
        RoundTripCapitalLedger(root).state_read_only()
    runtime.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CryptoRoundTripError, match="runtime_state_invalid"):
        RoundTripCapitalLedger(root).state_read_only()


@pytest.mark.parametrize("tamper", ["event_checksum", "final_head", "fork"])
def test_compact_writer_index_tamper_fails_closed(
    tmp_path: Path, tamper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _direct_payload(
        fixture_id=f"runtime-index-{tamper}", slot="2026-07-30T00:00:00Z"
    )
    run_round_trip_fixture_cycle(payload, output_root=tmp_path)
    root = tmp_path / "round_trip_capital"
    runtime_path = root / "runtime_state.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    item = next(iter(runtime["event_index"].values()))
    if tamper == "event_checksum":
        item["event_checksum"] = "0" * 64
    elif tamper == "final_head":
        item["final_head_checksum"] = "0" * 64
    else:
        item["event"]["previous_checksum"] = "0" * 64
        material = dict(item["event"])
        material.pop("checksum")
        item["event"]["checksum"] = capital_module._sha256(material)
        item["event_checksum"] = item["event"]["checksum"]
    runtime["state_sha256"] = capital_module._sha256(
        {key: value for key, value in runtime.items() if key != "state_sha256"}
    )
    runtime_path.write_text(capital_module._canonical_json(runtime) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_read_rows_unlocked",
        lambda self: (_ for _ in ()).throw(AssertionError("writer history scan")),
    )
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_replay",
        lambda self, rows: (_ for _ in ()).throw(AssertionError("writer replay")),
    )
    ledger = RoundTripCapitalLedger(root, _capability=capital_module._WRITE_CAPABILITY)
    with ledger.cycle(), pytest.raises(
        CryptoRoundTripError, match="runtime_state_invalid|runtime_state_fork"
    ):
        ledger.state_for_writer()


def test_fresh_writer_uses_compact_snapshot_and_returns_exact_historical_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_payload = _direct_payload(
        fixture_id="writer-compact-first", slot="2026-07-30T00:00:00Z"
    )
    first = run_round_trip_fixture_cycle(first_payload, output_root=tmp_path)
    root = tmp_path / "round_trip_capital"
    stored_events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_read_rows_unlocked",
        lambda self: (_ for _ in ()).throw(AssertionError("writer history scan")),
    )
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_replay",
        lambda self, rows: (_ for _ in ()).throw(AssertionError("writer replay")),
    )
    fresh = RoundTripCapitalLedger(
        root, _capability=capital_module._WRITE_CAPABILITY
    )
    with fresh.cycle():
        opening, opening_replayed = fresh.ensure_opening()
        assert opening_replayed is True
        assert opening == stored_events[0]
        assert fresh.state_for_writer()["initialized"] is True
        assert fresh.event_for_writer(stored_events[1]["reference_id"]) == stored_events[1]
        appended, appended_replayed = fresh.append(
            event_type=stored_events[1]["event_type"],
            reference_id=stored_events[1]["reference_id"],
            payload=stored_events[1]["payload"],
        )
        assert appended_replayed is True
        assert appended == stored_events[1]
    replay = run_round_trip_fixture_cycle(first_payload, output_root=tmp_path)
    assert replay["idempotent_replay"] is True
    assert replay["capital"] == first["capital"]


def test_same_cycle_two_new_appends_are_monotonic_and_third_writer_resolves_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_round_trip_fixture_cycle(
        _direct_payload(fixture_id="writer-base", slot="2026-07-30T00:00:00Z"),
        output_root=tmp_path,
    )
    root = tmp_path / "round_trip_capital"
    audited = RoundTripCapitalLedger(root).events_read_only()
    template = audited[-1]
    writer = RoundTripCapitalLedger(root, _capability=capital_module._WRITE_CAPABILITY)
    with writer.cycle():
        writer.ensure_opening()
        writer.state_for_writer()
        assert writer.event_for_writer(template["reference_id"]) == template
        new_events = []
        for offset in (1, 2):
            reference_id = f"cycle:writer-direct-{offset}"
            payload = json.loads(json.dumps(template["payload"]))
            payload["cycle_id"] = reference_id.removeprefix("cycle:")
            payload["fixture_id"] = f"writer-direct-{offset}"
            payload["execution_slot"] = f"2026-07-30T00:{offset * 5:02d}:00Z"
            payload["before"] = writer._capital_checkpoint(writer.state_for_writer())
            payload["order"] = None
            payload["receipt"] = None
            payload["exit_reason"] = None
            next_state = capital_module.copy.deepcopy(writer.state_for_writer())
            next_state["marks"][payload["symbol"]] = Decimal(payload["quote"]["bid"])
            next_state["cycles"][payload["cycle_id"]] = ""
            next_state["last_slot_by_symbol"][payload["symbol"]] = datetime.fromisoformat(
                payload["execution_slot"].replace("Z", "+00:00")
            )
            payload["after"] = writer._capital_checkpoint(next_state)
            event, replayed = writer.append(
                event_type="cycle", reference_id=reference_id, payload=payload
            )
            assert replayed is False
            new_events.append(event)
    assert [event["sequence"] for event in new_events] == [3, 4]

    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_read_rows_unlocked",
        lambda self: (_ for _ in ()).throw(AssertionError("writer history scan")),
    )
    monkeypatch.setattr(
        RoundTripCapitalLedger,
        "_replay",
        lambda self, rows: (_ for _ in ()).throw(AssertionError("writer replay")),
    )
    third = RoundTripCapitalLedger(root, _capability=capital_module._WRITE_CAPABILITY)
    with third.cycle():
        for event in [*audited, *new_events]:
            assert third.event_for_writer(event["reference_id"]) == event
        assert third.head() == (4, new_events[-1]["checksum"])


def test_explicit_full_audit_remains_unchanged(tmp_path: Path) -> None:
    run_round_trip_fixture_cycle(
        _direct_payload(fixture_id="full-audit", slot="2026-07-30T00:00:00Z"),
        output_root=tmp_path,
    )
    ledger = RoundTripCapitalLedger(tmp_path / "round_trip_capital")
    events = ledger.events_read_only()
    assert len(events) == 2
    assert ledger.head() == (2, events[-1]["checksum"])


def test_legacy_reader_ignores_adjacent_runtime_state(tmp_path: Path) -> None:
    run_round_trip_fixture_cycle(
        _direct_payload(fixture_id="rollback", slot="2026-07-30T00:00:00Z"),
        output_root=tmp_path,
    )
    # Legacy v1 semantics read only events/head and never inspect the
    # adjacent runtime-state file.  Keep this proof local so shallow clones
    # do not require historical Git objects.
    ledger = RoundTripCapitalLedger(tmp_path / "round_trip_capital")
    rows = ledger._read_rows()
    state, checksum = ledger._replay(rows)
    ledger._validate_head(rows, checksum)
    assert len(rows) == 2
    assert checksum == json.loads(
        ledger.head_path.read_text(encoding="utf-8")
    )["checksum"]
    assert state["initialized"] is True


def test_crash_after_events_and_head_before_runtime_state_self_repairs_on_writer_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_round_trip_fixture_cycle(
        _direct_payload(fixture_id="crash-before-state", slot="2026-07-30T00:00:00Z"),
        output_root=tmp_path,
    )
    original = RoundTripCapitalLedger._write_runtime_state

    def crash(self: RoundTripCapitalLedger, **kwargs: Any) -> dict[str, Any]:
        raise OSError("injected_after_head")

    monkeypatch.setattr(RoundTripCapitalLedger, "_write_runtime_state", crash)
    with pytest.raises(OSError, match="injected_after_head"):
        run_round_trip_fixture_cycle(
            _direct_payload(fixture_id="crash-before-state-2", slot="2026-07-30T00:05:00Z"),
            output_root=tmp_path,
        )
    with pytest.raises(
        CryptoRoundTripError, match="runtime_state_invalid|runtime_state_stale"
    ):
        RoundTripCapitalLedger(tmp_path / "round_trip_capital").state_read_only()
    monkeypatch.setattr(RoundTripCapitalLedger, "_write_runtime_state", original)
    retry = run_round_trip_fixture_cycle(
        _direct_payload(
            fixture_id="crash-before-state-2",
            slot="2026-07-30T00:05:00Z",
        ),
        output_root=tmp_path,
    )
    assert retry["idempotent_replay"] is True
    repaired = RoundTripCapitalLedger(tmp_path / "round_trip_capital").state_read_only()
    assert repaired["head_sequence"] == 3


def test_writer_refuses_multi_event_runtime_snapshot_gap(tmp_path: Path) -> None:
    first = _direct_payload(fixture_id="snapshot-gap-1", slot="2026-07-30T00:00:00Z")
    second = _direct_payload(fixture_id="snapshot-gap-2", slot="2026-07-30T00:05:00Z")
    third = _direct_payload(fixture_id="snapshot-gap-3", slot="2026-07-30T00:10:00Z")
    run_round_trip_fixture_cycle(first, output_root=tmp_path)
    runtime_path = tmp_path / "round_trip_capital" / "runtime_state.json"
    first_runtime = runtime_path.read_bytes()
    run_round_trip_fixture_cycle(second, output_root=tmp_path)
    run_round_trip_fixture_cycle(third, output_root=tmp_path)
    runtime_path.write_bytes(first_runtime)
    before = _capital_bytes(tmp_path)
    writer = RoundTripCapitalLedger(
        tmp_path / "round_trip_capital", _capability=capital_module._WRITE_CAPABILITY
    )
    with writer.cycle(), pytest.raises(
        CryptoRoundTripError, match="runtime_state_stale"
    ):
        writer.state_for_writer()
    assert _capital_bytes(tmp_path) == before


def test_writer_cycle_invalidates_cache_after_external_ledger_change(
    tmp_path: Path,
) -> None:
    payload = _direct_payload(
        fixture_id="fixture-cache-source",
        slot="2026-07-30T00:00:00Z",
    )
    run_round_trip_fixture_cycle(payload, output_root=tmp_path)
    ledger = RoundTripCapitalLedger(
        tmp_path / "round_trip_capital",
        _capability=capital_module._WRITE_CAPABILITY,
    )

    with ledger.cycle():
        ledger.state_for_writer()
        with ledger.events_path.open("a", encoding="utf-8") as stream:
            stream.write("{}\n")
        with pytest.raises(CryptoRoundTripError, match="runtime_state_stale"):
            ledger.state_for_writer()


@pytest.mark.parametrize(
    ("exit_payload", "reason"),
    [
        (
            {
                "fixture_id": "stop",
                "slot": "2026-07-30T00:05:00Z",
                "bid": "97000.00",
                "ask": "97000.02",
            },
            "stop_loss_threshold_reached",
        ),
        (
            {
                "fixture_id": "time",
                "slot": "2026-07-31T00:00:00Z",
                "bid": "100000.00",
                "ask": "100000.02",
            },
            "max_holding_period_reached",
        ),
        (
            {
                "fixture_id": "momentum",
                "slot": "2026-07-30T00:05:00Z",
                "action": "observe",
                "regime_return": "-0.0001",
                "decision_return": "-0.0001",
                "bid": "100000.00",
                "ask": "100000.02",
            },
            "momentum_reversal_observed",
        ),
    ],
)
def test_all_frozen_exit_triggers_are_replayed_as_sell_capital_facts(
    tmp_path: Path,
    exit_payload: dict[str, str],
    reason: str,
) -> None:
    entry = run_round_trip_fixture_cycle(
        _direct_payload(
            fixture_id="entry",
            slot="2026-07-30T00:00:00Z",
        ),
        output_root=tmp_path,
    )
    sold = run_round_trip_fixture_cycle(
        _direct_payload(**exit_payload),
        output_root=tmp_path,
    )
    assert sold["exit_reason"] == reason
    assert sold["order"]["side"] == "sell"
    assert sold["receipt"]["status"] == "fixture_simulated"
    assert sold["capital"]["positions"] == {}
    assert Decimal(sold["order"]["reference_price"]) <= Decimal(
        sold["order"]["quote_bid"]
    )
    entry_cost = Decimal(entry["receipt"]["notional"]) + Decimal(
        entry["receipt"]["fee"]
    )
    exit_proceeds = Decimal(sold["receipt"]["notional"]) - Decimal(
        sold["receipt"]["fee"]
    )
    assert Decimal(sold["capital"]["cash"]) == (
        Decimal("10000") - entry_cost + exit_proceeds
    )


def test_pending_two_symbol_crash_recovers_without_port_or_duplicate_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    port, frozen, request = _inputs()
    real_cycle = round_trip_runner_module.run_round_trip_fixture_cycle
    calls = 0

    def crash_on_second(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected_after_first_symbol")
        return real_cycle(*args, **kwargs)

    monkeypatch.setattr(
        round_trip_runner_module,
        "run_round_trip_fixture_cycle",
        crash_on_second,
    )
    with pytest.raises(RuntimeError, match="injected_after_first_symbol"):
        run_crypto_delayed_paper_round_trip_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=tmp_path,
        )

    class BombPort:
        def load_snapshot(self, **_: Any) -> Any:
            raise AssertionError("pending recovery must not query data again")

    monkeypatch.setattr(
        round_trip_runner_module,
        "run_round_trip_fixture_cycle",
        real_cycle,
    )
    recovered = run_crypto_delayed_paper_round_trip_once(
        port=BombPort(),
        profile=frozen,
        request=request,
        output_root=tmp_path,
    )
    assert recovered["recovered_pending"] is True
    assert recovered["symbols"]["BTCUSDT"]["idempotent_replay"] is True
    assert recovered["symbols"]["ETHUSDT"]["idempotent_replay"] is False
    assert len(recovered["capital"]["orders"]) == 2


def test_detached_exit_shadow_artifacts_cannot_change_capital(tmp_path: Path) -> None:
    control = tmp_path / "control"
    shadowed = tmp_path / "shadowed"
    for root in (control, shadowed):
        port, frozen, request = _inputs()
        run_crypto_delayed_paper_round_trip_once(
            port=port,
            profile=frozen,
            request=request,
            output_root=root,
        )
    shadow_path = shadowed / "evolution" / "exit_shadow"
    shadow_path.mkdir(parents=True)
    (shadow_path / "forged.json").write_text(
        '{"action":"shadow_exit","authority":"none"}\n',
        encoding="utf-8",
    )
    results = []
    for root in (control, shadowed):
        port, frozen, request = _inputs(
            minutes=5,
            price_multiplier=Decimal("1.05"),
        )
        results.append(
            run_crypto_delayed_paper_round_trip_once(
                port=port,
                profile=frozen,
                request=request,
                output_root=root,
            )["capital"]
        )
    assert results[0] == results[1]


def test_recomputed_checksum_cannot_forge_an_exit_reason(tmp_path: Path) -> None:
    run_round_trip_fixture_cycle(
        _direct_payload(
            fixture_id="entry",
            slot="2026-07-30T00:00:00Z",
        ),
        output_root=tmp_path,
    )
    run_round_trip_fixture_cycle(
        _direct_payload(
            fixture_id="exit",
            slot="2026-07-30T00:05:00Z",
            bid="104000.00",
            ask="104000.02",
        ),
        output_root=tmp_path,
    )
    events_path = tmp_path / "round_trip_capital" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    rows[-1]["payload"]["exit_reason"] = "max_holding_period_reached"
    rows[-1]["event_id"] = (
        "crypto-round-trip-event-"
        + capital_module._sha256(
            {
                "event_type": rows[-1]["event_type"],
                "reference_id": rows[-1]["reference_id"],
                "payload": rows[-1]["payload"],
            }
        )[:24]
    )
    material = dict(rows[-1])
    material.pop("checksum")
    rows[-1]["checksum"] = capital_module._sha256(material)
    events_path.write_text(
        "".join(
            json.dumps(
                row,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    head_path = tmp_path / "round_trip_capital" / "head.json"
    head = json.loads(head_path.read_text(encoding="utf-8"))
    head["checksum"] = rows[-1]["checksum"]
    head_path.write_text(
        json.dumps(
            head,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CryptoRoundTripError, match="exit_reason_mismatch"):
        RoundTripCapitalLedger(tmp_path / "round_trip_capital").head()


def test_policy_is_independent_and_simulation_only() -> None:
    assert ROUND_TRIP_CAPITAL_POLICY.generation == 2
    assert ROUND_TRIP_CAPITAL_POLICY.initial_cash == Decimal("10000")
    assert ROUND_TRIP_CAPITAL_POLICY.aggregate_with_prior_generations is False
    assert ROUND_TRIP_CAPITAL_POLICY.real_trading_enabled is False
    assert ROUND_TRIP_CAPITAL_POLICY.execution_authority is False
