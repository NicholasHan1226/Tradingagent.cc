"""Detached, future-only paired baseline/challenger evaluation evidence.

This module is intentionally not imported by the delayed-paper runtime.  A
future offline/shadow caller may pass one already-validated observation pair;
the sink writes only its own immutable research namespace and a hash-chained
checkpoint.  It never reads or mutates the core, decision ledger, or capital
ledger on behalf of the caller.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
from decimal import ROUND_HALF_EVEN, ROUND_UP
from pathlib import Path
import os
import stat
from typing import Any, Iterator, Mapping


PAIRED_EVALUATION_CONTRACT = "tradingagent.crypto.paired_evaluation.v1"
PAIRED_EVALUATION_RECEIPT_CONTRACT = (
    "tradingagent.crypto.paired_evaluation_receipt.v1"
)
PAIRED_EVALUATION_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.paired_evaluation_checkpoint.v1"
)
AVAILABILITY_CENSORING_CONTRACT = "crypto-availability-censoring-v1"
_NAMESPACE = "paired_evaluation"
_MAX_FILE_BYTES = 2 * 1024 * 1024
_ARMS = ("baseline", "challenger")


class PairedEvaluationError(RuntimeError):
    """Raised when paired evidence is invalid, conflicting, or unsafe."""


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise PairedEvaluationError("paired_evaluation_value_invalid")


def _json(value: Any) -> str:
    try:
        return json.dumps(
            _canonical(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PairedEvaluationError("paired_evaluation_payload_invalid") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise PairedEvaluationError(f"paired_evaluation_{field}_sha_invalid")
    return value


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PairedEvaluationError(f"paired_evaluation_{field}_invalid")
    return value


def _decimal(value: Any, *, field: str, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str):
        raise PairedEvaluationError(f"paired_evaluation_{field}_invalid")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise PairedEvaluationError(f"paired_evaluation_{field}_invalid") from exc
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise PairedEvaluationError(f"paired_evaluation_{field}_invalid")
    return parsed


def _decimal_equal(left: Any, right: Any, *, field: str) -> None:
    if _decimal(left, field=field) != _decimal(right, field=field):
        raise PairedEvaluationError(f"paired_evaluation_{field}_arithmetic_invalid")


def _authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "research_only": True,
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _ensure_regular(path: Path, *, reason: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PairedEvaluationError(reason) from exc
    try:
        before = os.fstat(descriptor)
        node = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or node.st_dev != before.st_dev
            or node.st_ino != before.st_ino
            or before.st_size <= 0
            or before.st_size > _MAX_FILE_BYTES
        ):
            raise PairedEvaluationError(reason)
        data = os.read(descriptor, _MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(data) != before.st_size or after.st_size != before.st_size:
            raise PairedEvaluationError(reason)
    except PairedEvaluationError:
        raise
    except OSError as exc:
        raise PairedEvaluationError(reason) from exc
    finally:
        os.close(descriptor)
    if not data.endswith(b"\n") or b"\x00" in data:
        raise PairedEvaluationError(reason)
    return data


def _read_json(path: Path, *, reason: str) -> dict[str, Any]:
    try:
        payload = json.loads(_ensure_regular(path, reason=reason).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairedEvaluationError(reason) from exc
    if not isinstance(payload, dict) or (_json(payload) + "\n").encode() != path.read_bytes():
        raise PairedEvaluationError(reason)
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    canonical = _canonical(payload)
    if not isinstance(canonical, dict):
        raise PairedEvaluationError("paired_evaluation_payload_invalid")
    encoded = (_json(canonical) + "\n").encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise PairedEvaluationError("paired_evaluation_artifact_too_large")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        existing = _read_json(path, reason="paired_evaluation_immutable_conflict")
        if _json(existing) != _json(canonical):
            raise PairedEvaluationError("paired_evaluation_immutable_conflict")
        return
    descriptor: int | None = None
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path)
        temporary.unlink()
        _fsync_directory(path.parent)
    except FileExistsError:
        if path.exists() and _read_json(path, reason="paired_evaluation_immutable_conflict") != canonical:
            raise PairedEvaluationError("paired_evaluation_immutable_conflict")
    except OSError as exc:
        raise PairedEvaluationError("paired_evaluation_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _source_valid(source: Mapping[str, Any]) -> dict[str, Any]:
    observation = _sha(source.get("observation_content_sha256"), field="observation")
    completion = _sha(source.get("completion_sha256"), field="completion")
    bindings = source.get("bindings")
    if not isinstance(bindings, Mapping) or not bindings:
        raise PairedEvaluationError("paired_evaluation_source_bindings_invalid")
    normalized: dict[str, Any] = {}
    for dataset_id, binding in sorted(bindings.items()):
        _text(dataset_id, field="dataset_id")
        if not isinstance(binding, Mapping):
            raise PairedEvaluationError("paired_evaluation_source_binding_invalid")
        normalized[str(dataset_id)] = {
            "receipt_id": _text(binding.get("receipt_id"), field="receipt_id"),
            "lineage_sha256": _sha(binding.get("lineage_sha256"), field="lineage"),
            "semantic_sha256": _sha(binding.get("semantic_sha256"), field="semantic"),
            "catalog_version": _text(binding.get("catalog_version"), field="catalog"),
        }
    return {
        "observation_content_sha256": observation,
        "completion_sha256": completion,
        "bindings": normalized,
    }


def _cost_valid(cost: Mapping[str, Any]) -> dict[str, Any]:
    contract_id = _text(cost.get("contract_id"), field="cost_contract_id")
    version = cost.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise PairedEvaluationError("paired_evaluation_cost_contract_invalid")
    if cost.get("representation") != "amount_and_return":
        raise PairedEvaluationError("paired_evaluation_cost_representation_invalid")
    if cost.get("turnover_unit") != "notional_ratio":
        raise PairedEvaluationError("paired_evaluation_turnover_unit_invalid")
    if cost.get("fee_basis") != "notional_times_turnover":
        raise PairedEvaluationError("paired_evaluation_fee_basis_invalid")
    if cost.get("spread_application") != "round_trip_bps":
        raise PairedEvaluationError("paired_evaluation_spread_application_invalid")
    _decimal(cost.get("fee_rate"), field="fee_rate", nonnegative=True)
    _decimal(cost.get("entry_slippage_bps"), field="entry_slippage", nonnegative=True)
    _decimal(cost.get("exit_slippage_bps"), field="exit_slippage", nonnegative=True)
    _decimal(cost.get("half_spread_bps"), field="half_spread", nonnegative=True)
    return {
        "contract_id": contract_id,
        "version": version,
        "representation": cost["representation"],
        "turnover_unit": cost["turnover_unit"],
        "fee_basis": cost["fee_basis"],
        "spread_application": cost["spread_application"],
        "fee_rate": cost["fee_rate"],
        "fee_asset": _text(cost.get("fee_asset"), field="fee_asset"),
        "entry_slippage_bps": cost["entry_slippage_bps"],
        "exit_slippage_bps": cost["exit_slippage_bps"],
        "spread_model_id": _text(cost.get("spread_model_id"), field="spread_model"),
        "half_spread_bps": cost["half_spread_bps"],
        "rounding": _text(cost.get("rounding"), field="rounding"),
    }


def _quantize(value: Decimal, cost: Mapping[str, Any]) -> Decimal:
    rounding = cost["rounding"]
    if rounding == "ROUND_UP_8DP":
        mode = ROUND_UP
    elif rounding == "ROUND_HALF_EVEN_8DP":
        mode = ROUND_HALF_EVEN
    else:
        raise PairedEvaluationError("paired_evaluation_rounding_invalid")
    return value.quantize(Decimal("0.00000001"), rounding=mode)


def _cost_expectations(
    outcome: Mapping[str, Any], cost: Mapping[str, Any]
) -> dict[str, Decimal]:
    notional = _decimal(outcome.get("notional"), field="notional", nonnegative=True)
    turnover = _decimal(outcome.get("turnover"), field="turnover", nonnegative=True)
    if notional <= 0:
        raise PairedEvaluationError("paired_evaluation_notional_invalid")
    fee_rate = _decimal(cost["fee_rate"], field="fee_rate", nonnegative=True)
    entry_slip = _decimal(cost["entry_slippage_bps"], field="entry_slippage", nonnegative=True)
    exit_slip = _decimal(cost["exit_slippage_bps"], field="exit_slippage", nonnegative=True)
    half_spread = _decimal(cost["half_spread_bps"], field="half_spread", nonnegative=True)
    fee_amount = _quantize(notional * turnover * fee_rate, cost)
    slippage_amount = _quantize(
        notional * turnover * (entry_slip + exit_slip) / Decimal("10000"), cost
    )
    spread_amount = _quantize(
        notional * turnover * (Decimal("2") * half_spread) / Decimal("10000"), cost
    )
    return {
        "notional": notional,
        "turnover": turnover,
        "fee_amount": fee_amount,
        "slippage_amount": slippage_amount,
        "spread_amount": spread_amount,
        "fee_return": _quantize(fee_amount / notional, cost),
        "slippage_return": _quantize(slippage_amount / notional, cost),
        "spread_return": _quantize(spread_amount / notional, cost),
    }


def _arm_valid(
    arm: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    cost: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    check_chain: bool = True,
) -> dict[str, Any]:
    for field in ("strategy_id", "strategy_version", "model_id", "model_version"):
        _text(arm.get(field), field=field)
    for field in ("strategy_sha256", "model_sha256", "config_sha256"):
        _sha(arm.get(field), field=field)
    pit = arm.get("pit")
    if not isinstance(pit, Mapping) or _json(_source_valid(pit)) != _json(source):
        raise PairedEvaluationError("paired_evaluation_pit_binding_mismatch")
    arm_cost = arm.get("cost_contract", cost)
    if not isinstance(arm_cost, Mapping) or _json(_cost_valid(arm_cost)) != _json(cost):
        raise PairedEvaluationError("paired_evaluation_cost_contract_mismatch")
    outcome = arm.get("outcome")
    equity = arm.get("equity")
    if not isinstance(outcome, Mapping) or not isinstance(equity, Mapping):
        raise PairedEvaluationError("paired_evaluation_arm_outcome_invalid")
    gross = _decimal(outcome.get("gross_return"), field="gross_return")
    expected_costs = _cost_expectations(outcome, cost)
    for field in (
        "notional",
        "turnover",
        "fee_amount",
        "slippage_amount",
        "spread_amount",
        "fee_return",
        "slippage_return",
        "spread_return",
    ):
        supplied = _decimal(
            outcome.get(field),
            field=field,
            nonnegative=True,
        )
        if supplied != expected_costs[field]:
            raise PairedEvaluationError("paired_evaluation_cost_arithmetic_invalid")
    fee = expected_costs["fee_return"]
    slip = expected_costs["slippage_return"]
    spread = expected_costs["spread_return"]
    net = _decimal(outcome.get("net_return"), field="net_return")
    if gross - fee - slip - spread != net:
        raise PairedEvaluationError("paired_evaluation_net_return_arithmetic_invalid")
    before = _decimal(equity.get("before"), field="equity_before", nonnegative=True)
    after = _decimal(equity.get("after"), field="equity_after", nonnegative=True)
    peak_before = _decimal(equity.get("running_peak_before"), field="peak_before", nonnegative=True)
    peak_after = _decimal(equity.get("running_peak_after"), field="peak_after", nonnegative=True)
    drawdown = _decimal(equity.get("drawdown"), field="drawdown", nonnegative=True)
    max_before = _decimal(equity.get("max_drawdown_before"), field="max_drawdown_before", nonnegative=True)
    max_to_date = _decimal(equity.get("max_drawdown_to_date"), field="max_drawdown_to_date", nonnegative=True)
    if check_chain and prior is None:
        if before != peak_before or max_before != 0:
            raise PairedEvaluationError("paired_evaluation_equity_initial_invalid")
    elif check_chain and prior is not None:
        if (
            before != _decimal(prior["equity_after"], field="prior_equity")
            or peak_before != _decimal(prior["running_peak"], field="prior_peak")
            or max_before != _decimal(prior["max_drawdown"], field="prior_drawdown")
        ):
            raise PairedEvaluationError("paired_evaluation_equity_chain_invalid")
    expected_after = before * (Decimal("1") + net)
    if after != expected_after:
        raise PairedEvaluationError("paired_evaluation_equity_arithmetic_invalid")
    expected_peak = max(peak_before, after)
    expected_drawdown = (expected_peak - after) / expected_peak if expected_peak else Decimal("0")
    expected_max = max(max_before, expected_drawdown)
    if peak_after != expected_peak or drawdown != expected_drawdown or max_to_date != expected_max:
        raise PairedEvaluationError("paired_evaluation_drawdown_arithmetic_invalid")
    return deepcopy(_canonical(arm))


def _validate_pair(
    raw: Mapping[str, Any],
    *,
    prior_state: Mapping[str, Mapping[str, Any]] | None = None,
    check_chain: bool = True,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PairedEvaluationError("paired_evaluation_pair_invalid")
    observation_id = _text(raw.get("observation_id"), field="observation_id")
    symbol = _text(raw.get("symbol"), field="symbol")
    market_slot = _text(raw.get("market_slot"), field="market_slot")
    pair_id = _text(raw.get("evaluation_pair_id"), field="evaluation_pair_id")
    source = raw.get("source")
    if not isinstance(source, Mapping):
        raise PairedEvaluationError("paired_evaluation_source_invalid")
    normalized_source = _source_valid(source)
    availability = raw.get("availability")
    if not isinstance(availability, Mapping) or availability.get("contract") != AVAILABILITY_CENSORING_CONTRACT:
        raise PairedEvaluationError("paired_evaluation_availability_contract_invalid")
    reason_codes = availability.get("reason_codes")
    if availability.get("eligible") is not True or not isinstance(reason_codes, list) or reason_codes or availability.get("gap_event_id") is not None:
        raise PairedEvaluationError("paired_evaluation_availability_censored")
    cost = raw.get("cost_contract")
    if not isinstance(cost, Mapping):
        raise PairedEvaluationError("paired_evaluation_cost_contract_invalid")
    normalized_cost = _cost_valid(cost)
    arms = raw.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != set(_ARMS):
        raise PairedEvaluationError("paired_evaluation_arms_invalid")
    prior_state = prior_state or {}
    normalized_arms = {
        arm_name: _arm_valid(
            arms[arm_name],
            source=normalized_source,
            cost=normalized_cost,
            prior=prior_state.get(arm_name),
            check_chain=check_chain,
        )
        for arm_name in _ARMS
    }
    return {
        "contract": PAIRED_EVALUATION_CONTRACT,
        "observation_id": observation_id,
        "symbol": symbol,
        "market_slot": market_slot,
        "evaluation_pair_id": pair_id,
        "source": normalized_source,
        "availability": {
            "contract": AVAILABILITY_CENSORING_CONTRACT,
            "eligible": True,
            "reason_codes": [],
            "gap_event_id": None,
        },
        "cost_contract": normalized_cost,
        "arms": normalized_arms,
        **_authority_fields(),
    }


def _key(pair: Mapping[str, Any]) -> dict[str, str]:
    return {
        "observation_id": str(pair["observation_id"]),
        "symbol": str(pair["symbol"]),
        "evaluation_pair_id": str(pair["evaluation_pair_id"]),
    }


def _key_sha(pair: Mapping[str, Any]) -> str:
    return _sha256(_key(pair))


def _stream_sha(pair: Mapping[str, Any]) -> str:
    return _sha256(
        {
            "symbol": str(pair["symbol"]),
            "evaluation_pair_id": str(pair["evaluation_pair_id"]),
        }
    )


def _namespace(root: Path) -> Path:
    return root / "evolution" / _NAMESPACE


def _paths(root: Path) -> tuple[Path, Path, Path, Path]:
    namespace = _namespace(root)
    return namespace, namespace / "pairs", namespace / "receipts", namespace / "checkpoints"


@contextmanager
def _lock(namespace: Path) -> Iterator[None]:
    namespace.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = namespace / ".lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_namespace_layout(
    namespace: Path,
    pair_dir: Path,
    receipt_dir: Path,
    checkpoint_dir: Path,
) -> None:
    """Initialize only a lock-only namespace; reject any partial artifact set."""

    required = {pair_dir.name, receipt_dir.name, checkpoint_dir.name}
    entries = {entry.name for entry in namespace.iterdir() if entry.name != ".lock"}
    if not entries:
        for directory in (pair_dir, receipt_dir, checkpoint_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return
    if entries != required or not all(
        directory.is_dir() for directory in (pair_dir, receipt_dir, checkpoint_dir)
    ):
        raise PairedEvaluationError("paired_evaluation_artifact_incomplete")


def _read_checkpoints(checkpoint_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    paths = _json_paths(checkpoint_dir, reason="paired_evaluation_checkpoint_invalid")
    for path in paths:
        row = _read_json(path, reason="paired_evaluation_checkpoint_invalid")
        material = dict(row)
        claimed = material.pop("checkpoint_sha256", None)
        sequence = len(rows) + 1
        if (
            path.name != f"{sequence:012d}.json"
            or row.get("contract") != PAIRED_EVALUATION_CHECKPOINT_CONTRACT
            or row.get("sequence") != sequence
            or row.get("previous_checkpoint_sha256") != (rows[-1]["checkpoint_sha256"] if rows else None)
            or claimed != _sha256(material)
        ):
            raise PairedEvaluationError("paired_evaluation_checkpoint_invalid")
        rows.append(row)
    return rows


def _json_paths(directory: Path, *, reason: str) -> list[Path]:
    if not directory.is_dir():
        raise PairedEvaluationError(reason)
    paths: list[Path] = []
    for path in directory.iterdir():
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            raise PairedEvaluationError(reason)
        paths.append(path)
    return sorted(paths, key=lambda item: item.name)


def _without_hash(row: Mapping[str, Any], field: str) -> dict[str, Any]:
    material = dict(row)
    material.pop(field, None)
    return material


def _pair_material(row: Mapping[str, Any]) -> dict[str, Any]:
    material = dict(row)
    material.pop("pair_sha256", None)
    material.pop("pair_key_sha256", None)
    return material


def _pair_row(normalized: Mapping[str, Any], pair_key_sha: str) -> dict[str, Any]:
    return {
        **deepcopy(_canonical(normalized)),
        "pair_key_sha256": pair_key_sha,
        "pair_sha256": _sha256(normalized),
    }


def _prior_state_for_stream(
    checkpoints: list[Mapping[str, Any]], stream_sha: str
) -> tuple[dict[str, Mapping[str, Any]], int]:
    for checkpoint in reversed(checkpoints):
        if checkpoint.get("stream_key_sha256") == stream_sha:
            return checkpoint.get("state") or {}, int(checkpoint["stream_sequence"])
    return {}, 0


def _prior_state_for_pair(
    checkpoints: list[Mapping[str, Any]], pair_key_sha: str
) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    matches = [
        (index, checkpoint)
        for index, checkpoint in enumerate(checkpoints)
        if checkpoint.get("pair_key_sha256") == pair_key_sha
    ]
    if len(matches) != 1:
        raise PairedEvaluationError("paired_evaluation_artifact_incomplete")
    index, checkpoint = matches[0]
    stream_sha = checkpoint.get("stream_key_sha256")
    prior: dict[str, Mapping[str, Any]] = {}
    for previous in reversed(checkpoints[:index]):
        if previous.get("stream_key_sha256") == stream_sha:
            prior = previous.get("state") or {}
            break
    return prior, checkpoint


def _source_bindings_sha(pair: Mapping[str, Any]) -> str:
    return _sha256(pair["source"]["bindings"])


def _receipt_for_pair(
    pair: Mapping[str, Any], checkpoint: Mapping[str, Any], sequence: int
) -> dict[str, Any]:
    receipt = {
        "contract": PAIRED_EVALUATION_RECEIPT_CONTRACT,
        "pair_key_sha256": pair["pair_key_sha256"],
        "pair_sha256": pair["pair_sha256"],
        "source_observation_content_sha256": pair["source"]["observation_content_sha256"],
        "source_completion_sha256": pair["source"]["completion_sha256"],
        "source_bindings_sha256": _source_bindings_sha(pair),
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "sequence": sequence,
        **_authority_fields(),
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    return receipt


def _checkpoint_for_pair(
    pair: Mapping[str, Any],
    *,
    sequence: int,
    stream_sequence: int,
    checkpoints: list[Mapping[str, Any]],
) -> dict[str, Any]:
    checkpoint = {
        "contract": PAIRED_EVALUATION_CHECKPOINT_CONTRACT,
        "sequence": sequence,
        "stream_sequence": stream_sequence,
        "stream_key_sha256": _stream_sha(pair),
        "pair_key_sha256": pair["pair_key_sha256"],
        "pair_sha256": pair["pair_sha256"],
        "previous_checkpoint_sha256": checkpoints[-1]["checkpoint_sha256"] if checkpoints else None,
        "state": {
            arm: {
                "equity_after": pair["arms"][arm]["equity"]["after"],
                "running_peak": pair["arms"][arm]["equity"]["running_peak_after"],
                "max_drawdown": pair["arms"][arm]["equity"]["max_drawdown_to_date"],
            }
            for arm in _ARMS
        },
        **_authority_fields(),
    }
    checkpoint["checkpoint_sha256"] = _sha256(checkpoint)
    return checkpoint


def _validate_stored_artifacts(
    pairs: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
) -> None:
    if not pairs or len(pairs) != len(receipts) or len(pairs) != len(checkpoints):
        raise PairedEvaluationError("paired_evaluation_cardinality_invalid")
    pairs_by_key = {pair.get("pair_key_sha256"): pair for pair in pairs}
    receipts_by_key = {receipt.get("pair_key_sha256"): receipt for receipt in receipts}
    checkpoints_by_key = {checkpoint.get("pair_key_sha256"): checkpoint for checkpoint in checkpoints}
    if (
        len(pairs_by_key) != len(pairs)
        or len(receipts_by_key) != len(receipts)
        or len(checkpoints_by_key) != len(checkpoints)
        or set(pairs_by_key) != set(receipts_by_key)
        or set(pairs_by_key) != set(checkpoints_by_key)
    ):
        raise PairedEvaluationError("paired_evaluation_cardinality_invalid")
    for sequence, checkpoint in enumerate(checkpoints, start=1):
        pair_key = checkpoint["pair_key_sha256"]
        pair = pairs_by_key[pair_key]
        if pair.get("pair_sha256") != _sha256(_pair_material(pair)):
            raise PairedEvaluationError("paired_evaluation_pair_hash_invalid")
        if pair_key != _key_sha(pair) or checkpoint.get("pair_sha256") != pair["pair_sha256"]:
            raise PairedEvaluationError("paired_evaluation_checkpoint_pair_hash_invalid")
        if checkpoint.get("stream_key_sha256") != _stream_sha(pair):
            raise PairedEvaluationError("paired_evaluation_stream_hash_invalid")
        prior_state: dict[str, Mapping[str, Any]] = {}
        prior_stream_sequence = 0
        for previous in reversed(checkpoints[: sequence - 1]):
            if previous.get("stream_key_sha256") == checkpoint["stream_key_sha256"]:
                prior_state = previous.get("state") or {}
                prior_stream_sequence = int(previous.get("stream_sequence", 0))
                break
        if checkpoint.get("stream_sequence") != prior_stream_sequence + 1:
            raise PairedEvaluationError("paired_evaluation_stream_sequence_invalid")
        normalized = _validate_pair(pair, prior_state=prior_state, check_chain=True)
        if _json(normalized) != _json(_pair_material(pair)):
            raise PairedEvaluationError("paired_evaluation_pair_incomplete")
        receipt = receipts_by_key[pair_key]
        if (
            receipt.get("contract") != PAIRED_EVALUATION_RECEIPT_CONTRACT
            or receipt.get("receipt_sha256") != _sha256(_without_hash(receipt, "receipt_sha256"))
            or receipt.get("pair_sha256") != pair["pair_sha256"]
            or receipt.get("checkpoint_sha256") != checkpoint["checkpoint_sha256"]
            or receipt.get("sequence") != sequence
            or receipt.get("source_observation_content_sha256") != pair["source"]["observation_content_sha256"]
            or receipt.get("source_completion_sha256") != pair["source"]["completion_sha256"]
            or receipt.get("source_bindings_sha256") != _source_bindings_sha(pair)
        ):
            raise PairedEvaluationError("paired_evaluation_receipt_source_hash_invalid")
        expected_state = {
            arm: {
                "equity_after": pair["arms"][arm]["equity"]["after"],
                "running_peak": pair["arms"][arm]["equity"]["running_peak_after"],
                "max_drawdown": pair["arms"][arm]["equity"]["max_drawdown_to_date"],
            }
            for arm in _ARMS
        }
        if checkpoint.get("state") != expected_state:
            raise PairedEvaluationError("paired_evaluation_checkpoint_state_invalid")


def append_paired_evaluation(output_root: Path | str, pair: Mapping[str, Any]) -> dict[str, Any]:
    """Append one future-only paired row, or return its exact idempotent replay."""

    root = Path(output_root)
    namespace, pair_dir, receipt_dir, checkpoint_dir = _paths(root)
    # Static validation is intentionally before namespace creation.  Once it
    # passes, every real append (including the first writer) takes the lock.
    _validate_pair(pair, check_chain=False)
    with _lock(namespace):
        _ensure_namespace_layout(namespace, pair_dir, receipt_dir, checkpoint_dir)
        checkpoints = _read_checkpoints(checkpoint_dir) if checkpoint_dir.exists() else []
        pair_key_sha = _key_sha(pair)
        existing_path = pair_dir / f"{pair_key_sha}.json"
        if existing_path.exists() or existing_path.is_symlink():
            prior_state, checkpoint = _prior_state_for_pair(checkpoints, pair_key_sha)
            normalized = _validate_pair(pair, prior_state=prior_state, check_chain=True)
            candidate = _pair_row(normalized, pair_key_sha)
            existing = _read_json(existing_path, reason="paired_evaluation_pair_invalid")
            if existing.get("pair_sha256") != _sha256(_pair_material(existing)):
                raise PairedEvaluationError("paired_evaluation_pair_hash_invalid")
            if _json(existing) != _json(candidate):
                raise PairedEvaluationError("paired_evaluation_pair_conflict")
            receipt = _read_json(
                receipt_dir / f"{pair_key_sha}.json",
                reason="paired_evaluation_receipt_invalid",
            )
            all_pairs = [
                _read_json(path, reason="paired_evaluation_pair_invalid")
                for path in _json_paths(pair_dir, reason="paired_evaluation_pair_invalid")
            ]
            all_receipts = [
                _read_json(path, reason="paired_evaluation_receipt_invalid")
                for path in _json_paths(receipt_dir, reason="paired_evaluation_receipt_invalid")
            ]
            _validate_stored_artifacts(all_pairs, all_receipts, checkpoints)
            return receipt
        stream_sha = _stream_sha(pair)
        prior_state, previous_stream_sequence = _prior_state_for_stream(checkpoints, stream_sha)
        normalized = _validate_pair(pair, prior_state=prior_state, check_chain=True)
        pair_row = _pair_row(normalized, pair_key_sha)
        sequence = len(checkpoints) + 1
        checkpoint = _checkpoint_for_pair(
            pair_row,
            sequence=sequence,
            stream_sequence=previous_stream_sequence + 1,
            checkpoints=checkpoints,
        )
        receipt = _receipt_for_pair(pair_row, checkpoint, sequence)
        _write_immutable(pair_dir / f"{pair_key_sha}.json", pair_row)
        _write_immutable(receipt_dir / f"{pair_key_sha}.json", receipt)
        _write_immutable(checkpoint_dir / f"{sequence:012d}.json", checkpoint)
        return receipt


def read_paired_evaluation(output_root: Path | str) -> dict[str, list[dict[str, Any]]]:
    """Read and verify all pair rows and their hash chain without writing."""

    namespace, pair_dir, receipt_dir, checkpoint_dir = _paths(Path(output_root))
    if not namespace.exists():
        return {"pairs": [], "receipts": [], "checkpoints": []}
    with _lock_read_only(namespace):
        if not pair_dir.is_dir() or not receipt_dir.is_dir() or not checkpoint_dir.is_dir():
            raise PairedEvaluationError("paired_evaluation_artifact_incomplete")
        checkpoints = _read_checkpoints(checkpoint_dir)
        pair_paths = _json_paths(pair_dir, reason="paired_evaluation_pair_invalid")
        receipt_paths = _json_paths(receipt_dir, reason="paired_evaluation_receipt_invalid")
        pairs = [_read_json(path, reason="paired_evaluation_pair_invalid") for path in pair_paths]
        receipts = [_read_json(path, reason="paired_evaluation_receipt_invalid") for path in receipt_paths]
        if any(path.stem != pair.get("pair_key_sha256") for path, pair in zip(pair_paths, pairs)):
            raise PairedEvaluationError("paired_evaluation_filename_binding_invalid")
        if any(path.stem != receipt.get("pair_key_sha256") for path, receipt in zip(receipt_paths, receipts)):
            raise PairedEvaluationError("paired_evaluation_filename_binding_invalid")
        _validate_stored_artifacts(pairs, receipts, checkpoints)
        return {"pairs": pairs, "receipts": receipts, "checkpoints": checkpoints}


@contextmanager
def _lock_read_only(namespace: Path) -> Iterator[None]:
    descriptor = os.open(namespace / ".lock", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


__all__ = [
    "AVAILABILITY_CENSORING_CONTRACT",
    "PAIRED_EVALUATION_CHECKPOINT_CONTRACT",
    "PAIRED_EVALUATION_CONTRACT",
    "PAIRED_EVALUATION_RECEIPT_CONTRACT",
    "PairedEvaluationError",
    "append_paired_evaluation",
    "read_paired_evaluation",
]
