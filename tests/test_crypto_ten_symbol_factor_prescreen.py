"""Tests for the offline ten-symbol factor pre-screen research module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

import Crypto.ten_symbol_factor_prescreen as prescreen
from Crypto.ten_symbol_factor_prescreen import (
    PRESCREEN_CONTRACT,
    RAW_CONTRACT,
    CryptoTenSymbolFactorPrescreenError,
    analyze,
    fetch_raw_history,
    load_raw_dir,
    render_report,
)
from tests.test_crypto_ten_symbol_observation_sidecar import (
    _assert_recursive_non_authority,
)
from tests.test_crypto_ten_symbol_support import (
    ALL_DATASETS,
    CATALOG_VERSION,
    TenSymbolFixtureTransport,
    catalog_payload,
    client,
    iso,
    query_metadata,
)
from Crypto.market_observation import FIVE_MINUTES, OBSERVATION_SYMBOLS
from shared.data.sharedsignals_v1 import HTTPResponse


START = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
FEE = Decimal("0.001")
SLIP = Decimal("0.0002")


def _typed_bars(
    closes: list[str],
    *,
    start: datetime = START,
    quote_volume: str = "1000",
) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for index, close in enumerate(closes):
        open_time = start + index * FIVE_MINUTES
        price = Decimal(close)
        bars.append(
            {
                "open_time": open_time,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": Decimal("10"),
                "quote_volume": Decimal(quote_volume),
            }
        )
    return bars


def _wire_rows(
    closes: list[str],
    *,
    symbol: str,
    start: datetime = START,
    quote_volume: str = "1000",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, close in enumerate(closes):
        open_time = start + index * FIVE_MINUTES
        price = Decimal(close)
        rows.append(
            {
                "symbol": symbol,
                "open_time": iso(open_time),
                "close_time": iso(
                    open_time + FIVE_MINUTES - timedelta(milliseconds=1)
                ),
                "open": format(price, "f"),
                "high": format(price + 1, "f"),
                "low": format(price - 1, "f"),
                "close": format(price, "f"),
                "volume": "10",
                "quote_volume": quote_volume,
                "trade_count": 10 + index,
            }
        )
    return rows


def _expected_cost_adjusted(entry: str, exit_: str) -> Decimal:
    entry_d = Decimal(entry)
    exit_d = Decimal(exit_)
    net = exit_d * (1 - FEE) / (entry_d * (1 + FEE)) - 1
    return (1 + net) * (1 - SLIP) ** 2 - 1


def _candidates(result: dict[str, Any]) -> dict[str, Any]:
    return {c["candidate_id"]: c for c in result["candidates"]}


def test_analyze_xs_rs_ranks_and_costs_exactly() -> None:
    count = 40
    rows_by_symbol = {
        "AAA": _typed_bars([format(100 + 2 * i, "f") for i in range(count)]),
        "BBB": _typed_bars([format(100 + i, "f") for i in range(count)]),
        "CCC": _typed_bars(["100"] * count, quote_volume="1000000000"),
        "DDD": _typed_bars(
            [format(200 - i, "f") for i in range(count)], quote_volume="1"
        ),
    }

    result = analyze(rows_by_symbol)

    assert result["contract"] == PRESCREEN_CONTRACT
    assert result["not_promotion_evidence"] is True
    assert result["historical_backfill_no_pit"] is True
    _assert_recursive_non_authority(result)
    candidates = _candidates(result)
    xs = candidates["xs_rs"]
    # Slots with a full 13-bar window and a +12 forward bar.
    assert xs["evaluation_slots"] == count - 12 - 12
    for top_k in (1, 2, 3):
        metrics = xs["variants"][f"top_{top_k}"]
        assert metrics["universe_count"] == xs["evaluation_slots"]
        assert metrics["signal_count"] == xs["evaluation_slots"]
        assert metrics["coverage"] == "1"
        assert metrics["turnover"] == "1"
    mean_1 = Decimal(xs["variants"]["top_1"]["mean_net"])
    mean_2 = Decimal(xs["variants"]["top_2"]["mean_net"])
    mean_3 = Decimal(xs["variants"]["top_3"]["mean_net"])
    assert mean_1 > mean_2 > mean_3
    # Exact top-1 check: every slot picks AAA, whose forward close is +24.
    slots = sorted(
        slot for slot in range(12, count - 12)
    )
    expected = sum(
        (
            _expected_cost_adjusted(
                format(100 + 2 * slot, "f"), format(100 + 2 * (slot + 12), "f")
            )
            for slot in slots
        ),
        Decimal("0"),
    ) / Decimal(len(slots))
    assert mean_1 == expected
    # Always-invest baseline sits between the best and worst members.
    baseline_delta_1 = Decimal(xs["variants"]["top_1"]["baseline_delta"])
    assert baseline_delta_1 > 0
    assert Decimal(xs["variants"]["top_3"]["baseline_delta"]) > 0
    per_symbol = xs["per_symbol"]["symbols"]
    assert per_symbol["AAA"]["inclusion_count"] == xs["evaluation_slots"]
    assert per_symbol["BBB"]["inclusion_count"] == xs["evaluation_slots"]
    assert per_symbol["DDD"]["inclusion_count"] == 0
    # Non-overlapping subsample keeps every 12th slot.
    subset = xs["variants"]["top_1"]["non_overlapping"]
    assert subset["stride"] == 12
    assert subset["slot_count"] == 2
    assert subset["signal_count"] == 2

    amihud = candidates["amihud_illiquidity"]
    assert amihud["evaluation_slots"] == xs["evaluation_slots"]
    inclusion = amihud["per_symbol"]
    # Tiny quote volume makes DDD the most illiquid; AAA ranks second.
    assert inclusion["DDD"]["inclusion_count"] == amihud["evaluation_slots"]
    assert inclusion["AAA"]["inclusion_count"] == amihud["evaluation_slots"]
    assert inclusion["CCC"]["inclusion_count"] == 0


def test_analyze_short_reversal_variants_and_per_symbol() -> None:
    closes = (
        ["100"] * 12
        + ["99"]
        + ["99"] * 11
        + ["101"] * 16
    )
    rows_by_symbol = {"REV": _typed_bars(closes), "FLAT": _typed_bars(["50"] * 40)}

    result = analyze(rows_by_symbol)
    reversal = _candidates(result)["short_reversal"]

    strict = reversal["variants"]["strict"]
    naive = reversal["variants"]["naive"]
    # Strict: slots 15..23 (1h = -1% with 15m > -0.1%), naive also 12..14.
    assert strict["signal_count"] == 9
    assert naive["signal_count"] == 12
    assert strict["hit_rate"] == "1"
    expected = _expected_cost_adjusted("99", "101")
    assert Decimal(strict["mean_net"]) == expected
    assert Decimal(naive["mean_net"]) == expected
    assert Decimal(strict["median_net"]) == expected
    assert Decimal(strict["cash_delta"]) == expected
    assert strict["max_drawdown"] == "0"
    per_symbol = reversal["per_symbol"]["strict"]
    assert per_symbol["REV"]["signal_count"] == 9
    assert per_symbol["REV"]["hit_rate"] == "1"
    assert per_symbol["FLAT"]["signal_count"] == 0
    assert per_symbol["FLAT"]["hit_rate"] is None
    assert strict["non_overlapping"]["stride"] == 12
    # The evaluation grid keeps slots 12 and 24; neither is a strict signal.
    assert strict["non_overlapping"]["signal_count"] == 0
    assert strict["non_overlapping"]["mean_net"] is None
    # Slot 12 is a naive signal and survives the grid.
    assert naive["non_overlapping"]["signal_count"] == 1
    assert Decimal(naive["non_overlapping"]["mean_net"]) == expected


def test_analyze_momentum_vol_regime_partitions_halves() -> None:
    count = 40
    rows_by_symbol = {
        "AAA": _typed_bars([format(100 + 2 * i, "f") for i in range(count)]),
        "BBB": _typed_bars([format(100 + i, "f") for i in range(count)]),
        "CCC": _typed_bars(["100"] * count),
        "DDD": _typed_bars([format(200 - i, "f") for i in range(count)]),
    }

    result = analyze(rows_by_symbol)
    regime = _candidates(result)["momentum_vol_regime"]

    high = regime["variants"]["high_vol_half"]
    low = regime["variants"]["low_vol_half"]
    slots = count - 12 - 12
    assert high["universe_count"] + low["universe_count"] == 4 * slots
    # Volatility order is AAA > BBB > DDD > CCC, so the high half holds the
    # two steep uptrends and both fire the momentum signal each slot.
    assert high["universe_count"] == 2 * slots
    assert low["universe_count"] == 2 * slots
    assert high["signal_count"] == 2 * slots
    assert low["signal_count"] == 0
    assert high["hit_rate"] == "1"
    assert low["hit_rate"] is None
    assert regime["median_realized_volatility_1h"] is not None


def test_load_raw_dir_roundtrip_and_gap_recording(tmp_path: Path) -> None:
    wire = _wire_rows([format(100 + i, "f") for i in range(20)], symbol="AAA")
    gap_wire = _wire_rows(
        [format(120 + i, "f") for i in range(18)],
        symbol="AAA",
        start=START + 22 * FIVE_MINUTES,
    )
    rows = wire + gap_wire
    payload = prescreen._raw_payload(
        symbol="AAA",
        dataset_id="crypto.spot.binance.aaa.5m",
        catalog_version=CATALOG_VERSION,
        start_open_time=START,
        end_open_time=START + 39 * FIVE_MINUTES,
        rows=rows,
        receipts=[{"receipt_id": "r1", "data_through": iso(START), "observed_at": iso(START)}],
    )
    assert payload["contract"] == RAW_CONTRACT
    assert payload["row_count"] == 38
    assert payload["gaps"] == [
        {
            "from_open_time": iso(START + 19 * FIVE_MINUTES),
            "to_open_time": iso(START + 22 * FIVE_MINUTES),
            "missing_bars": 2,
        }
    ]
    prescreen._write_file_atomic(
        tmp_path / "AAA.json",
        (prescreen._canonical_json(payload) + "\n").encode("utf-8"),
    )

    rows_by_symbol, meta = load_raw_dir(tmp_path, expected_symbols=("AAA",))

    assert meta["AAA"]["row_count"] == 38
    assert meta["AAA"]["gap_count"] == 1
    result = analyze(rows_by_symbol, meta=meta)
    # No evaluation slot may straddle the two missing bars.
    universe = prescreen._symbol_evaluation_rows(rows_by_symbol)
    for slot in universe["AAA"]:
        window_starts = slot - 12 * FIVE_MINUTES
        assert not (
            window_starts <= START + 19 * FIVE_MINUTES
            and slot >= START + 22 * FIVE_MINUTES
        )


def test_raw_dir_rejects_tampered_rows(tmp_path: Path) -> None:
    wire = _wire_rows([format(100 + i, "f") for i in range(20)], symbol="AAA")
    payload = prescreen._raw_payload(
        symbol="AAA",
        dataset_id="crypto.spot.binance.aaa.5m",
        catalog_version=CATALOG_VERSION,
        start_open_time=START,
        end_open_time=START + 19 * FIVE_MINUTES,
        rows=wire,
        receipts=[],
    )
    path = tmp_path / "AAA.json"
    prescreen._write_file_atomic(
        path, (prescreen._canonical_json(payload) + "\n").encode("utf-8")
    )
    mutated = json.loads(path.read_text(encoding="utf-8"))
    mutated["rows"][0]["close"] = "999999"
    path.write_text(json.dumps(mutated) + "\n", encoding="utf-8")

    with pytest.raises(CryptoTenSymbolFactorPrescreenError):
        load_raw_dir(tmp_path, expected_symbols=("AAA",))


class HistoryTransport:
    """Loopback fixture honoring between/limit with server-like cursors."""

    def __init__(self, rows_by_symbol: dict[str, list[dict[str, Any]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(kwargs)
        if kwargs["method"] == "GET":
            return HTTPResponse(200, catalog_payload())
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        dataset_id = body["dataset_id"]
        if dataset_id not in ALL_DATASETS:
            return HTTPResponse(404, {"error": "unknown dataset"})
        symbol = str(body["filters"]["symbol"]["eq"])
        between = body["filters"]["open_time"]["between"]
        lower = datetime.fromisoformat(str(between[0]).replace("Z", "+00:00"))
        upper = datetime.fromisoformat(str(between[1]).replace("Z", "+00:00"))
        limit = int(body["limit"])
        window = [
            row
            for row in self.rows_by_symbol[symbol]
            if lower
            <= datetime.fromisoformat(row["open_time"].replace("Z", "+00:00"))
            <= upper
        ]
        page = window[:limit]
        next_cursor = f"cursor-{len(window)}" if len(window) > limit else None
        data_through = upper - timedelta(milliseconds=1)
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"fixture-history-{dataset_id}",
                "dataset_id": dataset_id,
                "data": page,
                "next_cursor": next_cursor,
                "metadata": query_metadata(
                    dataset_id,
                    data_through=data_through,
                    observed_at=data_through + timedelta(seconds=20),
                ),
            },
        )


def _history_rows(count: int) -> dict[str, list[dict[str, Any]]]:
    return {
        symbol: _wire_rows(
            [format(100 + i, "f") for i in range(count)], symbol=symbol
        )
        for symbol in OBSERVATION_SYMBOLS
    }


def test_fetch_raw_history_persists_multi_window_canonical_raw(
    tmp_path: Path,
) -> None:
    transport = HistoryTransport(_history_rows(600))
    end = START + 599 * FIVE_MINUTES

    summary = fetch_raw_history(
        client=client(transport),
        raw_dir=tmp_path / "raw",
        start_open_time=START,
        end_open_time=end,
    )

    assert summary["event_type"] == "fetch_summary"
    assert summary["network_used"] is True
    assert summary["not_promotion_evidence"] is True
    assert len(summary["datasets"]) == 10
    first = summary["datasets"][0]
    assert first["row_count"] == 600
    assert first["window_count"] == 2
    rows_by_symbol, meta = load_raw_dir(tmp_path / "raw")
    assert len(rows_by_symbol) == 10
    assert all(meta[s]["row_count"] == 600 for s in OBSERVATION_SYMBOLS)
    result = analyze(rows_by_symbol, meta=meta)
    assert result["data_window"]["AAAUSDT" if "AAAUSDT" in result["data_window"] else OBSERVATION_SYMBOLS[0]]


def test_fetch_fails_closed_when_server_paginates_beyond_budget(
    tmp_path: Path,
) -> None:
    # A window capped at 500 opens can never legitimately exceed the page
    # limit; a cursor here means contract drift and must fail closed.
    class LyingTransport(HistoryTransport):
        def __call__(self, **kwargs: Any) -> HTTPResponse:
            response = super().__call__(**kwargs)
            if kwargs["method"] != "GET":
                payload = dict(response.json_body)
                payload["next_cursor"] = "unexpected-cursor"
                return HTTPResponse(response.status_code, payload)
            return response

    transport = LyingTransport(_history_rows(20))
    with pytest.raises(
        CryptoTenSymbolFactorPrescreenError,
        match="prescreen_fetch_query_invalid",
    ):
        fetch_raw_history(
            client=client(transport),
            raw_dir=tmp_path / "raw",
            start_open_time=START,
            end_open_time=START + 19 * FIVE_MINUTES,
        )


def test_fetch_fails_closed_on_empty_history(tmp_path: Path) -> None:
    transport = HistoryTransport(_history_rows(0))
    with pytest.raises(
        CryptoTenSymbolFactorPrescreenError,
        match="prescreen_fetch_empty_history",
    ):
        fetch_raw_history(
            client=client(transport),
            raw_dir=tmp_path / "raw",
            start_open_time=START,
            end_open_time=START + 19 * FIVE_MINUTES,
        )


def test_fetch_window_budget_fails_closed(tmp_path: Path) -> None:
    transport = HistoryTransport(_history_rows(20))
    with pytest.raises(
        CryptoTenSymbolFactorPrescreenError,
        match="prescreen_fetch_window_budget_exceeded",
    ):
        fetch_raw_history(
            client=client(transport),
            raw_dir=tmp_path / "raw",
            start_open_time=START,
            end_open_time=START + 19 * FIVE_MINUTES,
            max_windows=0,
        )


def _write_raw_dir(root: Path) -> None:
    for symbol in OBSERVATION_SYMBOLS:
        wire = _wire_rows(
            [format(100 + i, "f") for i in range(40)], symbol=symbol
        )
        payload = prescreen._raw_payload(
            symbol=symbol,
            dataset_id=f"crypto.spot.binance.{symbol.lower()}.5m",
            catalog_version=CATALOG_VERSION,
            start_open_time=START,
            end_open_time=START + 39 * FIVE_MINUTES,
            rows=wire,
            receipts=[],
        )
        prescreen._write_file_atomic(
            root / f"{symbol}.json",
            (prescreen._canonical_json(payload) + "\n").encode("utf-8"),
        )


def test_cli_analyze_and_report_modes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _write_raw_dir(raw_dir)

    exit_code = prescreen.main(["--raw-dir", str(raw_dir)])
    captured = capsys.readouterr()
    assert exit_code == 0
    result = json.loads(captured.out)
    assert result["contract"] == PRESCREEN_CONTRACT
    assert result["not_promotion_evidence"] is True
    assert len(result["candidates"]) == 4

    report_path = tmp_path / "report.md"
    exit_code = prescreen.main(
        ["--raw-dir", str(raw_dir), "--report", str(report_path)]
    )
    capsys.readouterr()
    assert exit_code == 0
    report = report_path.read_text(encoding="utf-8")
    assert "非证据声明" in report
    assert "not_promotion_evidence=true" in report
    assert "XS-RS" in report
    assert "非重叠" in report
    assert "| top_1 |" in report

    fetch_exit = prescreen.main(["--raw-dir", str(raw_dir), "--fetch"])
    captured = capsys.readouterr()
    assert fetch_exit == 2
    assert captured.out == ""
    assert captured.err.strip() == (
        "crypto ten-symbol factor prescreen failed closed"
    )


def test_render_report_marks_non_evidence_and_overlap() -> None:
    rows_by_symbol = {
        "AAA": _typed_bars([format(100 + 2 * i, "f") for i in range(40)]),
        "BBB": _typed_bars([format(100 + i, "f") for i in range(40)]),
    }
    result = analyze(rows_by_symbol)

    report = render_report(result)

    assert "not_promotion_evidence=true" in report
    assert "historical_backfill_no_pit=true" in report
    assert "每 12 槽取 1" in report
    assert "结论与预注册建议" in report
    assert "AAA" in report
