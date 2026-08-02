#!/usr/bin/env python3
"""Frozen out-of-sample evaluation for TradingCopilot forecast challengers.

The evaluator scores externally produced point forecasts on the same
point-in-time samples.  It does not train/download models and cannot promote a
challenger into TradingAgent or TradingCopilot.  Kronos is an optional
challenger, never a prerequisite or source of trade authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


INPUT_CONTRACT = "tradingagent.trading_copilot_forecast_evaluation_input.v1"
OUTPUT_CONTRACT = "tradingagent.trading_copilot_forecast_evaluation.v1"
RECEIPT_CONTRACT = "tradingagent.trading_copilot_forecast_evaluation_receipt.v1"
REQUIRED_BASELINES = ("naive_last_value", "linear_ridge_baseline")
KRONOS_MODEL_ID = "kronos_challenger"
MIN_INDEPENDENT_SAMPLES = 40
_SHA = frozenset("0123456789abcdef")


class ForecastEvaluationError(ValueError):
    """Fail-closed scientific evaluation error."""


def _canonical(value: object) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise ForecastEvaluationError("forecast_evaluation_not_canonical") from exc


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ForecastEvaluationError(reason)
    return value


def _number(value: object, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ForecastEvaluationError(reason)
    return float(value)


def _timestamp(value: object, reason: str) -> datetime:
    raw = _text(value, reason)
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForecastEvaluationError(reason) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ForecastEvaluationError(reason)
    return result


def _sha(value: object, reason: str) -> str:
    raw = _text(value, reason)
    if len(raw) != 64 or any(character not in _SHA for character in raw):
        raise ForecastEvaluationError(reason)
    return raw


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ForecastEvaluationError(reason)
    return value


def _load(path: Path) -> Mapping[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ForecastEvaluationError("forecast_evaluation_input_path_invalid")
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), "forecast_evaluation_input_invalid")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ForecastEvaluationError("forecast_evaluation_input_invalid") from exc


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def evaluate_forecasts(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("contractId") != INPUT_CONTRACT:
        raise ForecastEvaluationError("forecast_evaluation_contract_invalid")
    generated = _timestamp(payload.get("generatedAt"), "forecast_evaluation_generated_at_invalid")
    horizon = _text(payload.get("horizon"), "forecast_evaluation_horizon_invalid")
    cost_bps = _number(payload.get("roundTripCostBps"), "forecast_evaluation_cost_invalid")
    if not 0 <= cost_bps <= 500:
        raise ForecastEvaluationError("forecast_evaluation_cost_invalid")
    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ForecastEvaluationError("forecast_evaluation_samples_empty")
    seen: set[str] = set()
    samples: list[dict[str, Any]] = []
    model_ids: set[str] | None = None
    for raw in raw_samples:
        sample = _mapping(raw, "forecast_evaluation_sample_invalid")
        sample_id = _text(sample.get("sampleId"), "forecast_evaluation_sample_id_invalid")
        if sample_id in seen:
            raise ForecastEvaluationError("forecast_evaluation_sample_duplicate")
        seen.add(sample_id)
        decision = _timestamp(sample.get("decisionTime"), "forecast_evaluation_decision_time_invalid")
        label = _timestamp(sample.get("labelTime"), "forecast_evaluation_label_time_invalid")
        if not decision < label <= generated:
            raise ForecastEvaluationError("forecast_evaluation_future_leakage")
        predictions = _mapping(sample.get("predictions"), "forecast_evaluation_predictions_invalid")
        current_ids = set(predictions)
        if not set(REQUIRED_BASELINES).issubset(current_ids):
            raise ForecastEvaluationError("forecast_evaluation_baseline_missing")
        if model_ids is None:
            model_ids = current_ids
        elif current_ids != model_ids:
            raise ForecastEvaluationError("forecast_evaluation_sample_set_mismatch")
        parsed_predictions: dict[str, dict[str, float | None]] = {}
        for model_id, prediction_value in predictions.items():
            _text(model_id, "forecast_evaluation_model_id_invalid")
            prediction = _mapping(prediction_value, "forecast_evaluation_prediction_invalid")
            point = _number(prediction.get("pointReturn"), "forecast_evaluation_point_invalid")
            lower_raw, upper_raw = prediction.get("lowerReturn"), prediction.get("upperReturn")
            lower = None if lower_raw is None else _number(lower_raw, "forecast_evaluation_interval_invalid")
            upper = None if upper_raw is None else _number(upper_raw, "forecast_evaluation_interval_invalid")
            if (lower is None) != (upper is None) or (lower is not None and (lower > point or point > upper)):
                raise ForecastEvaluationError("forecast_evaluation_interval_invalid")
            parsed_predictions[model_id] = {"point": point, "lower": lower, "upper": upper}
        samples.append({
            "sampleId": sample_id,
            "symbol": _text(sample.get("symbol"), "forecast_evaluation_symbol_invalid"),
            "decision": decision,
            "label": label,
            "actual": _number(sample.get("actualReturn"), "forecast_evaluation_actual_invalid"),
            "sourceReceiptId": _text(sample.get("sourceReceiptId"), "forecast_evaluation_receipt_invalid"),
            "sourceReceiptSha256": _sha(sample.get("sourceReceiptSha256"), "forecast_evaluation_receipt_sha_invalid"),
            "predictions": parsed_predictions,
        })
    assert model_ids is not None
    independent = 0
    last_label: dict[str, datetime] = {}
    for sample in sorted(samples, key=lambda item: (item["decision"], item["symbol"])):
        previous = last_label.get(sample["symbol"])
        if previous is None or sample["decision"] >= previous:
            independent += 1
            last_label[sample["symbol"]] = sample["label"]
    metrics: dict[str, Any] = {}
    cost = cost_bps / 10_000
    for model_id in sorted(model_ids):
        errors: list[float] = []
        directions: list[bool] = []
        utilities: list[float] = []
        covered: list[bool] = []
        for sample in samples:
            prediction = sample["predictions"][model_id]
            point = prediction["point"]
            actual = sample["actual"]
            assert isinstance(point, float) and isinstance(actual, float)
            errors.append(abs(point - actual))
            directions.append(_sign(point) == _sign(actual))
            utilities.append(_sign(point) * actual - (cost if _sign(point) else 0))
            if prediction["lower"] is not None:
                covered.append(bool(prediction["lower"] <= actual <= prediction["upper"]))
        metrics[model_id] = {
            "sampleCount": len(samples),
            "effectiveIndependentSampleCount": independent,
            "mae": sum(errors) / len(errors),
            "directionalAccuracy": sum(directions) / len(directions),
            "meanPostCostDirectionalUtility": sum(utilities) / len(utilities),
            "intervalCoverage": sum(covered) / len(covered) if covered else None,
            "probabilitySemantics": None,
        }
    gate_reasons: list[str] = []
    if KRONOS_MODEL_ID not in metrics:
        gate_reasons.append("Kronos 尚未提供同样本、同周期的冻结预测")
    else:
        challenger = metrics[KRONOS_MODEL_ID]
        baseline = metrics["linear_ridge_baseline"]
        if independent < MIN_INDEPENDENT_SAMPLES:
            gate_reasons.append(f"有效独立样本 {independent} 少于门槛 {MIN_INDEPENDENT_SAMPLES}")
        if challenger["mae"] > baseline["mae"] * 0.95:
            gate_reasons.append("Kronos MAE 未比线性基线至少改善 5%")
        if challenger["directionalAccuracy"] < baseline["directionalAccuracy"]:
            gate_reasons.append("Kronos 方向准确率低于线性基线")
        if challenger["meanPostCostDirectionalUtility"] <= 0:
            gate_reasons.append("Kronos 扣除申报成本后的方向效用不为正")
    source_receipts = sorted({
        (sample["sourceReceiptId"], sample["sourceReceiptSha256"]) for sample in samples
    })
    return {
        "contractId": OUTPUT_CONTRACT,
        "generatedAt": generated.isoformat(),
        "horizon": horizon,
        "roundTripCostBps": cost_bps,
        "sampleCount": len(samples),
        "effectiveIndependentSampleCount": independent,
        "metrics": metrics,
        "challengerGate": {
            "modelId": KRONOS_MODEL_ID,
            "status": "eligible_for_shadow_comparison" if not gate_reasons else "blocked",
            "reasons": gate_reasons or ["仅可进入影子比较；仍不获得上线、资金或交易权限"],
            "promotionAuthority": False,
        },
        "sourceReceipts": [
            {"receiptId": receipt_id, "receiptSha256": receipt_sha}
            for receipt_id, receipt_sha in source_receipts
        ],
        "authority": {
            "capital": False, "orders": False, "broker": False,
            "training": False, "promotion": False, "realTradingEnabled": False,
        },
    }


def write_evaluation(*, input_path: Path | str, output_path: Path | str) -> dict[str, Any]:
    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise ForecastEvaluationError("real_trading_must_remain_disabled")
    input_file = Path(input_path)
    output_file = Path(output_path)
    if not output_file.is_absolute() or output_file.is_symlink():
        raise ForecastEvaluationError("forecast_evaluation_output_path_invalid")
    payload = _load(input_file)
    result = evaluate_forecasts(payload)
    result_bytes = _canonical(result)
    receipt = {
        "contractId": RECEIPT_CONTRACT,
        "inputSha256": hashlib.sha256(input_file.read_bytes()).hexdigest(),
        "evaluationSha256": hashlib.sha256(result_bytes).hexdigest(),
        "generatedAt": result["generatedAt"],
        "promotionAuthority": False,
        "realTradingEnabled": False,
    }
    output_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path, value in ((output_file, result_bytes), (output_file.with_suffix(output_file.suffix + ".receipt.json"), _canonical(receipt))):
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = write_evaluation(input_path=arguments.input.resolve(), output_path=arguments.output.resolve())
    except ForecastEvaluationError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "pass", "challengerGate": result["challengerGate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
