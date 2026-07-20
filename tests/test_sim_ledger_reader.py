from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.review import sim_ledger_reader
from shared.review.sim_ledger_reader import load_sim_trades_for_date


def test_local_sim_review_row_preserves_capital_scope_and_retry_lineage(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "local_sim_trades.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "trade_id": "LSIM-RETRY",
                "order_id": "SIM-RETRY-1",
                "idempotency_key": "retry-1",
                "market": "ashare",
                "trade_date": "2026-07-10",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "filled_price": 10.0,
                "status": "filled",
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "fill_price_source": "signal_card.price",
                "fill_price_source_class": "signal_card_price",
                "trade_timestamp_bj": "2026-07-10T10:00:00+08:00",
                "capital_scope": "strategy",
                "retry_of": "SIM-ORIGINAL",
                "retry_attempt": 1,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    rows = load_sim_trades_for_date(
        "20260710", markets=("ashare",), local_trades_path=ledger
    )

    assert len(rows) == 1
    assert rows[0]["capital_scope"] == "strategy"
    assert rows[0]["account_scope"] == "ashare_sim"
    assert rows[0]["account_scope_source"] == "documented_single_ashare_sim_account"
    assert rows[0]["retry_of"] == "SIM-ORIGINAL"
    assert rows[0]["retry_attempt"] == 1


def test_default_review_markets_are_exact_active_lanes() -> None:
    assert sim_ledger_reader.DEFAULT_REVIEW_MARKETS == (
        "ashare",
        "cn_futures",
        "crypto",
    )


def test_retired_market_filter_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown or retired runtime market"):
        load_sim_trades_for_date(
            "20260710",
            markets=("us",),
            ledger_root=tmp_path,
            local_trades_path=tmp_path / "missing.jsonl",
        )


def test_retired_market_row_under_active_path_is_not_loaded(tmp_path: Path) -> None:
    journal = tmp_path / "crypto" / "balanced" / "trade_journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "market": "pm",
                "trade_date": "20260710",
                "symbol": "retired-event",
                "side": "buy",
                "fill_qty": 1,
                "fill_price": 0.5,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows = load_sim_trades_for_date(
        "20260710",
        markets=("crypto",),
        ledger_root=tmp_path,
        local_trades_path=tmp_path / "missing.jsonl",
    )

    assert rows == []


def test_style_ledger_path_produces_distinct_account_scopes(tmp_path: Path) -> None:
    for strategy, symbol in (("grid", "BTCUSDT"), ("momentum", "ETHUSDT")):
        journal = tmp_path / "crypto" / strategy / "trade_journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps(
                {
                    "market": "crypto",
                    "trade_date": "20260710",
                    "symbol": symbol,
                    "side": "buy",
                    "fill_qty": 1,
                    "fill_price": 100,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    rows = load_sim_trades_for_date(
        "20260710",
        markets=("crypto",),
        ledger_root=tmp_path,
        local_trades_path=tmp_path / "missing.jsonl",
    )

    assert {row["account_scope"] for row in rows} == {
        "crypto:simulated:grid",
        "crypto:simulated:momentum",
    }
    assert {row["account_scope_source"] for row in rows} == {"style_ledger_path"}
