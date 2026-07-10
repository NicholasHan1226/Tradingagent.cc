from __future__ import annotations

import json
from pathlib import Path

from shared.review.sim_ledger_reader import load_sim_trades_for_date


def test_local_sim_review_row_preserves_capital_scope_and_retry_lineage(tmp_path: Path) -> None:
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

    rows = load_sim_trades_for_date("20260710", markets=("ashare",), local_trades_path=ledger)

    assert len(rows) == 1
    assert rows[0]["capital_scope"] == "strategy"
    assert rows[0]["retry_of"] == "SIM-ORIGINAL"
    assert rows[0]["retry_attempt"] == 1
