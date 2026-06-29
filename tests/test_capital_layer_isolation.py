from __future__ import annotations

from pathlib import Path

import pytest

from shared.accounting import capital_ledger, position_ledger
from shared.execution import shadow_broker


def _patch_position_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "position_logs"
    position_csv = ledger_dir / "position_ledger.csv"
    monkeypatch.setattr(position_ledger, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(position_ledger, "POSITION_CSV", position_csv)
    monkeypatch.setattr(position_ledger, "POSITION_LOCK", position_csv.with_suffix(".csv.lock"))


def _patch_capital_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "capital_logs"
    capital_csv = ledger_dir / "capital_ledger.csv"
    monkeypatch.setattr(capital_ledger, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(capital_ledger, "CAPITAL_CSV", capital_csv)
    monkeypatch.setattr(capital_ledger, "CAPITAL_LOCK", capital_csv.with_suffix(".csv.lock"))


def test_position_default_query_returns_only_real_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_position_paths(monkeypatch, tmp_path)

    position_ledger.open_position("600000.SH", 100, 10.0, capital_layer="real")
    position_ledger.open_position("600000.SH", 200, 11.0, capital_layer="shadow")
    position_ledger.open_position("600000.SH", 300, 12.0, capital_layer="simulated")

    default_positions = position_ledger.get_positions()
    assert len(default_positions) == 1
    assert default_positions[0]["capital_layer"] == "real"
    assert default_positions[0]["is_real_money"] == "Y"
    assert default_positions[0]["quantity"] == 100

    shadow_positions = position_ledger.get_positions(capital_layer="shadow")
    assert len(shadow_positions) == 1
    assert shadow_positions[0]["is_real_money"] == "N"
    assert shadow_positions[0]["quantity"] == 200

    simulated_positions = position_ledger.get_positions(capital_layer="simulated")
    assert len(simulated_positions) == 1
    assert simulated_positions[0]["is_real_money"] == "N"
    assert simulated_positions[0]["quantity"] == 300

    all_positions = position_ledger.get_positions(capital_layer="all")
    assert {p["capital_layer"] for p in all_positions} == {"real", "shadow", "simulated"}


def test_capital_cash_default_query_returns_only_real_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_capital_paths(monkeypatch, tmp_path)

    capital_ledger.record_deposit(1000.0, "2026-06-30T09:30:00", capital_layer="real")
    capital_ledger.record_deposit(2000.0, "2026-06-30T09:31:00", capital_layer="shadow")
    capital_ledger.record_deposit(3000.0, "2026-06-30T09:32:00", capital_layer="simulated")

    assert capital_ledger.get_cash_position() == 1000.0
    assert capital_ledger.get_cash_position(capital_layer="real") == 1000.0
    assert capital_ledger.get_cash_position(capital_layer="shadow") == 2000.0
    assert capital_ledger.get_cash_position(capital_layer="simulated") == 3000.0
    assert capital_ledger.get_cash_position(capital_layer="all") == 6000.0


def test_legacy_position_rows_default_to_shadow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_position_paths(monkeypatch, tmp_path)
    position_ledger.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    position_ledger.POSITION_CSV.write_text(
        "\n".join(
            [
                "entry_id,timestamp,event_type,ts_code,quantity,price,amount,"
                "running_quantity,running_cost,running_avg_price,realized_pnl,"
                "order_id,audit_id,note",
                "LEGACY-1,2026-06-30T09:30:00,open,600001.SH,100,10,1000,"
                "100,1000,10,0,,,legacy",
            ]
        ),
        encoding="utf-8",
    )

    assert position_ledger.get_positions() == []
    shadow_positions = position_ledger.get_positions(capital_layer="shadow")
    assert len(shadow_positions) == 1
    assert shadow_positions[0]["capital_layer"] == "shadow"
    assert shadow_positions[0]["is_real_money"] == "N"


def test_shadow_broker_rejects_real_capital_layer() -> None:
    with pytest.raises(ValueError):
        shadow_broker.record_shadow(
            {
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "capital_layer": "real",
            },
            "capital-layer-test",
        )
