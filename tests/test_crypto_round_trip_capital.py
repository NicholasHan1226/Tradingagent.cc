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


def test_round_trip_event_head_recovers_without_duplicate_fill(
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
    replay = run_crypto_delayed_paper_round_trip_once(
        port=replay_port,
        profile=replay_profile,
        request=replay_request,
        output_root=tmp_path,
    )
    assert replay["idempotent_replay"] is True
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == event_count
    assert replay["capital"] == completed["capital"]
    assert (
        RoundTripCapitalLedger(tmp_path / "round_trip_capital").head()[0] == event_count
    )


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
