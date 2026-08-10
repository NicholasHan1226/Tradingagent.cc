from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading

import pytest

import Crypto.delayed_paper_paired_evaluation as sink
from Crypto.delayed_paper_paired_evaluation import (
    PairedEvaluationError,
    append_paired_evaluation,
    read_paired_evaluation,
)


def _source() -> dict[str, object]:
    return {
        "observation_content_sha256": "a" * 64,
        "completion_sha256": "b" * 64,
        "bindings": {
            "btc_5m": {
                "receipt_id": "receipt:btc",
                "lineage_sha256": "c" * 64,
                "semantic_sha256": "d" * 64,
                "catalog_version": "catalog-v1",
            },
            "eth_5m": {
                "receipt_id": "receipt:eth",
                "lineage_sha256": "e" * 64,
                "semantic_sha256": "f" * 64,
                "catalog_version": "catalog-v1",
            },
        },
    }


def _cost() -> dict[str, object]:
    return {
        "contract_id": "crypto-round-trip-cost-v1",
        "version": 1,
        "representation": "amount_and_return",
        "turnover_unit": "notional_ratio",
        "fee_basis": "notional_times_turnover",
        "spread_application": "round_trip_bps",
        "fee_rate": "0.001",
        "fee_asset": "USDT",
        "entry_slippage_bps": "2",
        "exit_slippage_bps": "2",
        "spread_model_id": "symmetric-1bp-per-side-tick-rounded-v1",
        "half_spread_bps": "1",
        "rounding": "ROUND_UP_8DP",
    }


def _arm(source: dict[str, object], *, arm_id: str, net: str, before: str, peak: str, max_dd: str) -> dict[str, object]:
    net_value = float(net)
    before_value = float(before)
    after = f"{before_value * (1 + net_value):.8f}"
    peak_after = f"{max(float(peak), float(after)):.8f}"
    drawdown = f"{(float(peak_after) - float(after)) / float(peak_after):.8f}"
    return {
        "strategy_id": f"strategy-{arm_id}",
        "strategy_version": "1",
        "strategy_sha256": ("1" if arm_id == "baseline" else "2") * 64,
        "model_id": f"model-{arm_id}",
        "model_version": "1",
        "model_sha256": ("3" if arm_id == "baseline" else "4") * 64,
        "config_sha256": ("5" if arm_id == "baseline" else "6") * 64,
        "pit": deepcopy(source),
        "outcome": {
            "notional": "100.00000000",
            "gross_return": f"{float(net) + 0.0016:.8f}",
            "fee_amount": "0.10000000",
            "slippage_amount": "0.04000000",
            "spread_amount": "0.02000000",
            "fee_return": "0.00100000",
            "slippage_return": "0.00040000",
            "spread_return": "0.00020000",
            "net_return": net,
            "turnover": "1.00000000",
        },
        "equity": {
            "before": before,
            "after": after,
            "running_peak_before": peak,
            "running_peak_after": peak_after,
            "drawdown": drawdown,
            "max_drawdown_before": max_dd,
            "max_drawdown_to_date": f"{max(float(max_dd), float(drawdown)):.8f}",
        },
    }


def _pair(*, net: str = "0.01000000", before: str = "100.00000000", peak: str = "100.00000000", max_dd: str = "0.00000000") -> dict[str, object]:
    source = _source()
    return {
        "observation_id": "crypto-delayed-observation-1",
        "symbol": "BTCUSDT",
        "market_slot": "2026-08-10T04:00:00Z",
        "evaluation_pair_id": "baseline-v1__challenger-v1",
        "source": source,
        "availability": {
            "contract": "crypto-availability-censoring-v1",
            "eligible": True,
            "reason_codes": [],
            "gap_event_id": None,
        },
        "cost_contract": _cost(),
        "arms": {
            "baseline": _arm(source, arm_id="baseline", net=net, before=before, peak=peak, max_dd=max_dd),
            "challenger": _arm(source, arm_id="challenger", net=net, before=before, peak=peak, max_dd=max_dd),
        },
    }


def test_identical_pit_bindings_are_required(tmp_path: Path) -> None:
    pair = _pair()
    pair["arms"]["challenger"]["pit"]["completion_sha256"] = "c" * 64

    with pytest.raises(PairedEvaluationError, match="pit_binding_mismatch"):
        append_paired_evaluation(tmp_path, pair)
    assert not (tmp_path / "evolution" / "paired_evaluation").exists()


def test_cost_contract_mismatch_is_rejected(tmp_path: Path) -> None:
    pair = _pair()
    pair["arms"]["challenger"]["cost_contract"] = {**_cost(), "version": 2}

    with pytest.raises(PairedEvaluationError, match="cost_contract_mismatch"):
        append_paired_evaluation(tmp_path, pair)


def test_gap_or_late_window_is_censored_without_a_write(tmp_path: Path) -> None:
    pair = _pair()
    pair["availability"] = {
        "contract": "crypto-availability-censoring-v1",
        "eligible": False,
        "reason_codes": ["gap_window"],
        "gap_event_id": "gap-1",
    }

    with pytest.raises(PairedEvaluationError, match="availability_censored"):
        append_paired_evaluation(tmp_path, pair)
    assert not (tmp_path / "evolution" / "paired_evaluation").exists()


def test_cost_arithmetic_and_equity_peak_drawdown_are_checkpointed(tmp_path: Path) -> None:
    first = append_paired_evaluation(tmp_path, _pair(net="0.10000000"))
    second_pair = _pair(
        net="-0.20000000",
        before="110.00000000",
        peak="110.00000000",
        max_dd="0.00000000",
    )
    second_pair["observation_id"] = "crypto-delayed-observation-2"
    second_pair["market_slot"] = "2026-08-10T04:05:00Z"
    second = append_paired_evaluation(tmp_path, second_pair)

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    readback = read_paired_evaluation(tmp_path)
    assert len(readback["pairs"]) == 2
    assert readback["pairs"][1]["arms"]["baseline"]["equity"]["after"] == "88.00000000"
    assert readback["pairs"][1]["arms"]["baseline"]["equity"]["drawdown"] == "0.20000000"
    assert readback["pairs"][1]["arms"]["baseline"]["equity"]["max_drawdown_to_date"] == "0.20000000"


def test_checkpoint_uses_stream_sha_and_prior_state_is_contiguous(tmp_path: Path) -> None:
    first = append_paired_evaluation(tmp_path, _pair(net="0.10000000"))
    second_pair = _pair(net="0.01000000", before="110.00000000", peak="110.00000000")
    second_pair["observation_id"] = "crypto-delayed-observation-2"
    second_pair["market_slot"] = "2026-08-10T04:05:00Z"
    second = append_paired_evaluation(tmp_path, second_pair)

    stream_sha = hashlib.sha256(
        json.dumps(
            {"evaluation_pair_id": second_pair["evaluation_pair_id"], "symbol": second_pair["symbol"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    readback = read_paired_evaluation(tmp_path)
    assert first["sequence"] == 1 and second["sequence"] == 2
    assert readback["checkpoints"][0]["stream_key_sha256"] == stream_sha
    assert readback["checkpoints"][1]["stream_key_sha256"] == stream_sha
    assert readback["checkpoints"][1]["stream_sequence"] == 2

    mismatch = _pair(net="0.01", before="999.00000000", peak="999.00000000")
    mismatch["observation_id"] = "crypto-delayed-observation-3"
    mismatch["market_slot"] = "2026-08-10T04:10:00Z"
    with pytest.raises(PairedEvaluationError, match="equity_chain_invalid"):
        append_paired_evaluation(tmp_path, mismatch)


def test_later_exact_replay_does_not_revalidate_as_initial(tmp_path: Path) -> None:
    append_paired_evaluation(tmp_path, _pair(net="0.10000000"))
    second_pair = _pair(net="0.01000000", before="110.00000000", peak="110.00000000")
    second_pair["observation_id"] = "crypto-delayed-observation-2"
    second_pair["market_slot"] = "2026-08-10T04:05:00Z"
    second = append_paired_evaluation(tmp_path, second_pair)
    assert append_paired_evaluation(tmp_path, deepcopy(second_pair)) == second
    assert len(read_paired_evaluation(tmp_path)["checkpoints"]) == 2


def test_concurrent_first_appends_are_serialized_into_one_chain(tmp_path: Path) -> None:
    first = _pair()
    second = _pair()
    second["symbol"] = "ETHUSDT"
    second["observation_id"] = "crypto-delayed-observation-eth"
    second["arms"]["baseline"]["pit"] = deepcopy(second["source"])
    second["arms"]["challenger"]["pit"] = deepcopy(second["source"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda item: append_paired_evaluation(tmp_path, item), (first, second)))
    assert {receipt["sequence"] for receipt in receipts} == {1, 2}
    assert [row["sequence"] for row in read_paired_evaluation(tmp_path)["checkpoints"]] == [1, 2]


def test_concurrent_first_append_mkdir_lock_interleaving_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _pair()
    second = _pair()
    second["symbol"] = "ETHUSDT"
    second["observation_id"] = "crypto-delayed-observation-eth"
    entered_mkdir = threading.Event()
    entered_second = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    first_call = True
    original_lock = sink._lock

    @contextmanager
    def interleaved_lock(namespace: Path):
        nonlocal first_call
        with guard:
            is_first = first_call
            first_call = False
        if is_first:
            namespace.mkdir(parents=True, exist_ok=True, mode=0o700)
            entered_mkdir.set()
            assert release_first.wait(5)
        else:
            entered_second.set()
        with original_lock(namespace):
            yield

    monkeypatch.setattr(sink, "_lock", interleaved_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(append_paired_evaluation, tmp_path, first)
        assert entered_mkdir.wait(5)
        second_future = pool.submit(append_paired_evaluation, tmp_path, second)
        assert entered_second.wait(5)
        release_first.set()
        receipts = [first_future.result(), second_future.result()]
    assert {receipt["sequence"] for receipt in receipts} == {1, 2}
    assert [row["sequence"] for row in read_paired_evaluation(tmp_path)["checkpoints"]] == [1, 2]


def test_partial_existing_namespace_fails_closed(tmp_path: Path) -> None:
    namespace = tmp_path / "evolution" / "paired_evaluation"
    namespace.mkdir(parents=True)
    (namespace / ".lock").write_bytes(b"lock\n")
    (namespace / "pairs").mkdir()
    with pytest.raises(PairedEvaluationError, match="artifact_incomplete"):
        append_paired_evaluation(tmp_path, _pair())


def test_cost_amounts_and_spread_are_derived_not_trusted(tmp_path: Path) -> None:
    pair = _pair()
    pair["arms"]["baseline"]["outcome"]["fee_return"] = "0.00200000"
    pair["arms"]["baseline"]["outcome"]["net_return"] = "-0.00060000"
    with pytest.raises(PairedEvaluationError, match="cost_arithmetic_invalid"):
        append_paired_evaluation(tmp_path, pair)
    assert not (tmp_path / "evolution" / "paired_evaluation").exists()


def test_readback_rejects_receipt_corruption_and_orphans(tmp_path: Path) -> None:
    receipt = append_paired_evaluation(tmp_path, _pair())
    receipt_path = tmp_path / "evolution" / "paired_evaluation" / "receipts" / f"{receipt['pair_key_sha256']}.json"
    payload = json.loads(receipt_path.read_text())
    payload["source_completion_sha256"] = "f" * 64
    payload["receipt_sha256"] = hashlib.sha256(
        json.dumps({k: v for k, v in payload.items() if k != "receipt_sha256"}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(PairedEvaluationError, match="receipt_source_hash_invalid"):
        read_paired_evaluation(tmp_path)

    orphan_root = tmp_path / "orphan"
    append_paired_evaluation(orphan_root, _pair())
    pair_dir = orphan_root / "evolution" / "paired_evaluation" / "pairs"
    (pair_dir / ("0" * 64 + ".json")).write_text("{}\n")
    with pytest.raises(PairedEvaluationError, match="(cardinality|filename_binding)_invalid"):
        read_paired_evaluation(orphan_root)


def test_readback_rejects_missing_checkpoint_and_rehashed_checkpoint_binding(tmp_path: Path) -> None:
    receipt = append_paired_evaluation(tmp_path, _pair())
    checkpoint_path = tmp_path / "evolution" / "paired_evaluation" / "checkpoints" / "000000000001.json"
    checkpoint_path.unlink()
    with pytest.raises(PairedEvaluationError, match="cardinality_invalid"):
        read_paired_evaluation(tmp_path)

    rebound_root = tmp_path / "rebound"
    receipt = append_paired_evaluation(rebound_root, _pair())
    checkpoint_path = rebound_root / "evolution" / "paired_evaluation" / "checkpoints" / "000000000001.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["stream_key_sha256"] = "0" * 64
    checkpoint["checkpoint_sha256"] = hashlib.sha256(
        json.dumps({k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    checkpoint_path.write_text(json.dumps(checkpoint, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(PairedEvaluationError, match="stream_hash_invalid"):
        read_paired_evaluation(rebound_root)


def test_exact_replay_is_idempotent_and_conflict_fails_closed(tmp_path: Path) -> None:
    pair = _pair()
    first = append_paired_evaluation(tmp_path, pair)
    replay = append_paired_evaluation(tmp_path, deepcopy(pair))
    assert replay == first
    assert len(read_paired_evaluation(tmp_path)["checkpoints"]) == 1

    conflict = deepcopy(pair)
    conflict["arms"]["challenger"]["config_sha256"] = "7" * 64
    with pytest.raises(PairedEvaluationError, match="pair_conflict"):
        append_paired_evaluation(tmp_path, conflict)
    assert len(read_paired_evaluation(tmp_path)["pairs"]) == 1


def test_sink_does_not_mutate_core_or_capital_bytes(tmp_path: Path) -> None:
    core = tmp_path / "delayed_paper" / "observations"
    capital = tmp_path / "round_trip_capital"
    core.mkdir(parents=True)
    capital.mkdir(parents=True)
    (core / "core.json").write_bytes(b"core\n")
    (capital / "events.jsonl").write_bytes(b"capital\n")
    before = {
        path: path.read_bytes()
        for path in (core / "core.json", capital / "events.jsonl")
    }

    append_paired_evaluation(tmp_path, _pair())

    assert {path: path.read_bytes() for path in before} == before
