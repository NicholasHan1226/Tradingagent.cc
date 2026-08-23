"""Receipt-bound rolling evaluation tests over synthetic observation chains.

The fixtures build real bars sidecars whose ``identity_sha256`` and
``market_data_sha256`` are derived with the production hash functions, so the
verification path exercised here is the same one the recovered forty-symbol
observer's receipts go through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from Crypto.forty_symbol_rolling_evaluation import (
    ENTRY_FEE,
    ENTRY_THRESHOLD,
    EXIT_FEE,
    FortySymbolRollingEvaluationError,
    SLIPPAGE_RATE,
    _assemble_segment,
    _load_and_verify_sidecar,
    _round_trip_net,
    build_artifact,
)
from Crypto.market_observation import (
    BAR_FIELDS,
    OBSERVATION_SYMBOLS_V40,
    _recomputed_identity_sha256,
    _recomputed_market_data_sha256,
)

SYMBOLS = OBSERVATION_SYMBOLS_V40


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rows_for(
    symbol: str,
    window_end: datetime,
    base_offset: int = 0,
) -> list[dict[str, str]]:
    """Prices are flat per chain and depend only on the chain's base offset,
    mirroring immutable market history: chained slots' overlapping bars agree
    byte for byte, and a flat series never fires the momentum entry."""

    rows = []
    for offset in range(13, 0, -1):
        open_time = window_end - timedelta(minutes=5 * offset)
        price = str(1 + base_offset).ljust(11, "0")
        rows.append(
            {
                "symbol": symbol,
                "open_time": _iso(open_time),
                "close_time": _iso(open_time + timedelta(minutes=5))
                .replace(".000000Z", ".999Z"),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": "1.00000000",
                "quote_volume": "1.00000000",
                "trade_count": 1,
            }
        )
    return rows


def _sidecar(window_end: datetime, base_offset: int = 0) -> dict[str, object]:
    sources = []
    for symbol in SYMBOLS:
        rows = _rows_for(symbol, window_end, base_offset)
        sources.append(
            {
                "symbol": symbol,
                "dataset_id": f"crypto.spot.binance.{symbol.lower()}.5m",
                "row_count": len(rows),
                "page_count": 1,
                "receipt_id": "receipt:" + "0" * 64,
                "data_through": _iso(window_end - timedelta(minutes=5))
                .replace(".000000Z", ".999000Z"),
                "observed_at": _iso(window_end),
                "identity_sha256": _recomputed_identity_sha256(rows),
                "market_data_sha256": _recomputed_market_data_sha256(rows),
                "semantic_sha256": "0" * 64,
                "pagination_trace_sha256": "0" * 64,
                "rows": rows,
            }
        )
    return {
        "contract": "tradingagent.crypto.forty_symbol_observation_bars.v1",
        "catalog_version": "v1-test",
        "window_end": _iso(window_end),
        "observation_cutoff": _iso(window_end + timedelta(seconds=225)),
        "observation_sha256": hashlib.sha256(
            json.dumps(sources, sort_keys=True).encode()
        ).hexdigest(),
        "market_data_sha256": "0" * 64,
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
        "sources": sources,
    }


class _Chain:
    def __init__(
        self,
        tmp_path: Path,
        slot_count: int = 3,
        step_minutes: int = 5,
        base_offset: int = 0,
        first_gap_minutes: int | None = None,
    ) -> None:
        self.bars_dir = tmp_path / "bars"
        self.bars_dir.mkdir(parents=True, exist_ok=True)
        start = datetime(2026, 8, 23, 4, 45, tzinfo=timezone.utc)
        events = []
        checksum = "genesis"
        prior = "genesis"
        for index in range(slot_count):
            if index == 0 and first_gap_minutes is not None:
                window_end = start - timedelta(minutes=first_gap_minutes)
            else:
                window_end = start + timedelta(minutes=step_minutes * (index - 1))
            payload = _sidecar(window_end, base_offset=base_offset)
            path = self.bars_dir / (_iso(window_end).replace(":", "-") + ".json")
            path.write_text(json.dumps(payload), encoding="utf-8")
            checksum = hashlib.sha256(checksum.encode()).hexdigest()
            events.append(
                {
                    "contract": "tradingagent.crypto.forty_symbol_observation_event.v1",
                    "event_id": f"crypto-forty-observation-{index:024x}",
                    "event_type": "observation",
                    "sequence": 200 + index,
                    "checksum": checksum,
                    "previous_checksum": "genesis" if index == 0 else prior,
                    "window_end": _iso(window_end),
                    "observation": {
                        "observation_sha256": payload["observation_sha256"],
                        "window_end": _iso(window_end),
                    },
                }
            )
            prior = checksum
        self.events_path = tmp_path / "events.jsonl"
        self.events_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )


def test_happy_chain_produces_shadow_only_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    chain = _Chain(tmp_path)
    result = build_artifact(
        events_path=chain.events_path,
        bars_dir=chain.bars_dir,
        replay_command="test-replay",
    )

    assert result["contract"] == "tradingagent.crypto.forty_symbol_rolling_evaluation.v1"
    assert result["authority"] == "none"
    assert result["capital_write_eligible"] is False
    assert result["not_promotion_evidence"] is True
    assert result["receipt_bound_pit"] is True
    assert result["segment"]["gap_free"] is True
    assert result["segment"]["slot_count"] == 3
    # 3 chained slots at 5-minute spacing cover 13 + (3 - 1) = 15 bars.
    assert result["segment"]["bar_count_per_symbol"] == 15
    assert result["champion_configuration"]["scan_performed"] is False
    evaluation = result["evaluation"]
    assert evaluation["trips_total"] == 0  # flat synthetic prices never signal
    assert evaluation["recommendation"]["action"] == "continue_accumulation"


def test_tampered_row_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    chain = _Chain(tmp_path)
    victim = sorted(chain.bars_dir.glob("*.json"))[1]
    payload = json.loads(victim.read_text(encoding="utf-8"))
    payload["sources"][0]["rows"][0]["close"] = "99999999.00000000"
    victim.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FortySymbolRollingEvaluationError) as excinfo:
        build_artifact(
            events_path=chain.events_path,
            bars_dir=chain.bars_dir,
            replay_command="test-replay",
        )
    assert "row_digest_invalid" in str(excinfo.value)


def test_missing_slot_fails_closed_as_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    chain = _Chain(tmp_path, slot_count=3)
    victim = sorted(chain.bars_dir.glob("*.json"))[1]
    victim.unlink()

    with pytest.raises(FortySymbolRollingEvaluationError) as excinfo:
        build_artifact(
            events_path=chain.events_path,
            bars_dir=chain.bars_dir,
            replay_command="test-replay",
        )
    assert "sidecar_unreadable" in str(excinfo.value)


def test_broken_event_chain_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    chain = _Chain(tmp_path)
    lines = chain.events_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[2])
    event["previous_checksum"] = "forged"
    lines[2] = json.dumps(event, sort_keys=True)
    chain.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(FortySymbolRollingEvaluationError) as excinfo:
        build_artifact(
            events_path=chain.events_path,
            bars_dir=chain.bars_dir,
            replay_command="test-replay",
        )
    assert "chain_broken" in str(excinfo.value)


def test_sidecar_event_binding_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    chain = _Chain(tmp_path)
    lines = chain.events_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[1])
    event["observation"]["observation_sha256"] = "f" * 64
    lines[1] = json.dumps(event, sort_keys=True)
    chain.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(FortySymbolRollingEvaluationError) as excinfo:
        build_artifact(
            events_path=chain.events_path,
            bars_dir=chain.bars_dir,
            replay_command="test-replay",
        )
    assert "binding_invalid" in str(excinfo.value)


def test_authority_flag_rejected(tmp_path: Path) -> None:
    chain = _Chain(tmp_path, slot_count=1)
    victim = next(chain.bars_dir.glob("*.json"))
    payload = json.loads(victim.read_text(encoding="utf-8"))
    payload["execution_eligible"] = True
    victim.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FortySymbolRollingEvaluationError) as excinfo:
        _load_and_verify_sidecar(
            chain.bars_dir,
            json.loads(chain.events_path.read_text(encoding="utf-8").splitlines()[0]),
        )
    assert "authority_invalid" in str(excinfo.value)


def test_isolated_prefix_slots_drop_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    chain = _Chain(tmp_path, slot_count=3, first_gap_minutes=10)
    result = build_artifact(
        events_path=chain.events_path,
        bars_dir=chain.bars_dir,
        replay_command="test-replay",
    )
    # Only the longest contiguous suffix forms the segment; the isolated
    # earlier receipt stays recorded as evidence but outside the segment.
    assert result["segment"]["slot_count"] == 2
    assert len(result["segment"]["dropped_prefix_receipts"]) == 1


def test_assemble_segment_rejects_disjoint_slot_unions(tmp_path: Path) -> None:
    chain_a = _Chain(tmp_path / "a", slot_count=1, base_offset=1)
    chain_b = _Chain(tmp_path / "b", slot_count=1, base_offset=7)
    slot_a = _load_and_verify_sidecar(
        chain_a.bars_dir,
        json.loads(chain_a.events_path.read_text(encoding="utf-8").splitlines()[0]),
    )
    slot_b = _load_and_verify_sidecar(
        chain_b.bars_dir,
        json.loads(chain_b.events_path.read_text(encoding="utf-8").splitlines()[0]),
    )
    # Same window grid, different prices: overlap conflict must fail closed.
    with pytest.raises(FortySymbolRollingEvaluationError) as excinfo:
        _assemble_segment([slot_a, slot_b])
    assert (
        "overlap_conflict" in str(excinfo.value)
        or "symbols_disagree" in str(excinfo.value)
        or "segment_gap" in str(excinfo.value)
    )


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
    assert _round_trip_net(Decimal(0)) < 0  # costs alone make flat trips negative


def test_entry_threshold_is_frozen_champion_value() -> None:
    assert ENTRY_THRESHOLD == Decimal("0.001")
    assert ENTRY_FEE == Decimal("0.001")
    assert EXIT_FEE == Decimal("0.001")
    assert SLIPPAGE_RATE == Decimal("0.0002")
