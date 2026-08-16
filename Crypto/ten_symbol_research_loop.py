"""Stage-1 research-evolution loop: scheduled re-estimation, review only.

This module is the first stage of the crypto research-evolution closed loop.
It is a detached, offline, read-only one-shot scheduler: given the ten-symbol
observation store root plus a pre-screen configuration (label horizons), it
re-estimates the *already registered* pre-screen candidate set on the latest
accumulated observation evidence and emits one immutable, checksum-bound
human-review report artifact per new input state.

Stage-1 boundaries, all hard-coded:

- no hypothesis generation: the evaluated set is exactly the four candidates
  registered in ``ten_symbol_factor_prescreen``; a new-hypothesis generator
  is a later stage and any drift of the registered set fails closed;
- no evaluation-logic changes: metrics are produced by calling
  ``ten_symbol_factor_prescreen.analyze`` unchanged on the bar history
  reconstructed from the verified store event chain plus bars sidecars;
- no scheduler installation: one-shot invocation only, no systemd unit;
- automatic promotion stays inside the simulation domain: the review block
  derives an evidence-bound automatic recommendation (``auto_promote`` /
  ``auto_demote`` / ``auto_retain``) instead of a fixed
  ``manual_review_required`` and never authorizes real trading.

Input integrity mirrors the factor-projection precedent: the store event
chain is verified read-only, every terminal slot's bars sidecar is
re-derived and value-compared against the store event (a missing or
digest-drifting sidecar marks the slot ineligible and cuts the bar history
like a data gap), and any chain corruption fails closed.  The same input
state always produces the same artifact bytes; a rerun over an unchanged
input returns ``no_new_input`` without writing.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import uuid

from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.market_observation import OBSERVATION_SYMBOLS
import Crypto.ten_symbol_factor_prescreen as prescreen
import Crypto.ten_symbol_factor_research as projection
from Crypto.ten_symbol_observation_store import (
    CryptoTenSymbolObservationStoreError,
)


REVIEW_REPORT_CONTRACT = "tradingagent.crypto.ten_symbol_research_loop_review.v2"
LOOP_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.ten_symbol_research_loop_checkpoint.v2"
)
CHECKPOINT_FILENAME = "research_loop_checkpoint.json"
LOOP_STAGE = "stage_1_registered_hypothesis_automatic_reevaluation"
REGISTERED_CANDIDATE_IDS = (
    "xs_rs",
    "short_reversal",
    "amihud_illiquidity",
    "momentum_vol_regime",
)
DEFAULT_HORIZON_BARS = prescreen.ALLOWED_HORIZON_BARS
_SYMBOLS = OBSERVATION_SYMBOLS


class CryptoTenSymbolResearchLoopError(RuntimeError):
    """Stable fail-closed error for the stage-1 research loop."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_payload_invalid"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _result(*, status: str, **fields: Any) -> dict[str, Any]:
    return {
        "contract": REVIEW_REPORT_CONTRACT,
        "status": status,
        "loop_stage": LOOP_STAGE,
        "learning_mode": "detached_offline_worker",
        "automatic_reevaluation": True,
        **fields,
        **projection._non_authority_fields(),
    }


def _validate_horizon_bars(value: Any) -> tuple[int, ...]:
    if value is None:
        return tuple(DEFAULT_HORIZON_BARS)
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
    ):
        raise CryptoTenSymbolResearchLoopError("research_loop_horizon_invalid")
    horizons = tuple(value)
    if any(
        not isinstance(horizon, int)
        or isinstance(horizon, bool)
        or horizon not in prescreen.ALLOWED_HORIZON_BARS
        for horizon in horizons
    ):
        raise CryptoTenSymbolResearchLoopError("research_loop_horizon_invalid")
    if len(set(horizons)) != len(horizons) or tuple(sorted(horizons)) != horizons:
        raise CryptoTenSymbolResearchLoopError("research_loop_horizon_invalid")
    return horizons


# ---------------------------------------------------------------------------
# Artifact namespace: <store_root>/evolution/ten_symbol_research_loop/
# ---------------------------------------------------------------------------


def _loop_root(root: Path) -> Path:
    return root / "evolution" / "ten_symbol_research_loop"


def _ensure_root(root: Path) -> Path:
    parent = root / "evolution"
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise CryptoTenSymbolResearchLoopError("research_loop_directory_invalid")
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    evolution = _loop_root(root)
    for directory in (evolution, evolution / "reports"):
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise CryptoTenSymbolResearchLoopError(
                    "research_loop_directory_invalid"
                )
        else:
            directory.mkdir(mode=0o700)
    return evolution


def _atomic_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_checkpoint_write_failed"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_report(evolution: Path, report_sha256: str) -> dict[str, Any]:
    path = evolution / "reports" / f"{report_sha256}.json"
    try:
        report = projection._parse_canonical(
            path, reason="research_loop_report_invalid"
        )
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_report_invalid"
        ) from exc
    material = dict(report)
    claimed = material.pop("report_sha256", None)
    if (
        report.get("contract") != REVIEW_REPORT_CONTRACT
        or report.get("loop_stage") != LOOP_STAGE
        or report.get("automatic_reevaluation") is not True
        or claimed != _sha256(material)
        or claimed != report_sha256
        or any(
            report.get(key) != value
            for key, value in projection._non_authority_fields().items()
        )
    ):
        raise CryptoTenSymbolResearchLoopError("research_loop_report_invalid")
    return report


def _validated_current(evolution: Path) -> dict[str, Any] | None:
    """Validate only the compact checkpoint and its one bound report."""

    checkpoint_path = evolution / CHECKPOINT_FILENAME
    if not checkpoint_path.exists() and not checkpoint_path.is_symlink():
        return None
    try:
        current = projection._parse_canonical(
            checkpoint_path, reason="research_loop_checkpoint_invalid"
        )
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_checkpoint_invalid"
        ) from exc
    material = dict(current)
    claimed = material.pop("checkpoint_sha256", None)
    if (
        current.get("contract") != LOOP_CHECKPOINT_CONTRACT
        or claimed != _sha256(material)
        or not _is_sha256(current.get("last_input_digest"))
        or not _is_sha256(current.get("report_sha256"))
        or any(
            current.get(key) != value
            for key, value in projection._non_authority_fields().items()
        )
    ):
        raise CryptoTenSymbolResearchLoopError("research_loop_checkpoint_invalid")
    return {
        "checkpoint": current,
        "report": _load_report(evolution, str(current["report_sha256"])),
    }


# ---------------------------------------------------------------------------
# Source inventory: verified terminal units merged into per-symbol history
# ---------------------------------------------------------------------------


def _open_store(root: Path):
    try:
        return projection._open_store(root)
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_root_incomplete"
        ) from exc


def _terminal_unit_material(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "observation_id": unit["observation_id"],
            "source_event_checksum": unit["source_event_checksum"],
            "eligible": bool(unit["eligible"]),
            "ineligible_reason": unit["ineligible_reason"],
            "market_slot": projection._iso(unit["slot"]),
            "sidecar_sha256": unit["sidecar_sha256"],
        }
        for unit in units
    ]


def _merge_eligible_bars(
    eligible: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Merge eligible sidecar windows into one validated history per symbol.

    Consecutive slot windows overlap by twelve bars; identical rows merge,
    conflicting rows for the same open_time fail closed.  The merged,
    strictly increasing sequence is revalidated through the pre-screen row
    validator so gap recording and OHLC/UTC discipline stay identical to the
    backfill path.
    """

    raw: dict[str, dict[datetime, dict[str, Any]]] = {
        symbol: {} for symbol in _SYMBOLS
    }
    for unit in eligible:
        rows_by_symbol = unit["rows_by_symbol"]
        if not isinstance(rows_by_symbol, dict):
            raise CryptoTenSymbolResearchLoopError("research_loop_unit_invalid")
        for symbol in _SYMBOLS:
            rows = rows_by_symbol.get(symbol)
            if not isinstance(rows, list):
                raise CryptoTenSymbolResearchLoopError("research_loop_unit_invalid")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise CryptoTenSymbolResearchLoopError(
                        "research_loop_unit_invalid"
                    )
                try:
                    open_time = prescreen._parse_utc(row.get("open_time"))
                except prescreen.CryptoTenSymbolFactorPrescreenError as exc:
                    raise CryptoTenSymbolResearchLoopError(
                        "research_loop_unit_invalid"
                    ) from exc
                bucket = raw[symbol]
                existing = bucket.get(open_time)
                if existing is not None:
                    if existing != row:
                        raise CryptoTenSymbolResearchLoopError(
                            "research_loop_sidecar_rows_conflict"
                        )
                    continue
                bucket[open_time] = dict(row)
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, Any] = {}
    for symbol in _SYMBOLS:
        ordered = [raw[symbol][open_time] for open_time in sorted(raw[symbol])]
        try:
            validated, gaps = prescreen._validate_history_rows(ordered, symbol=symbol)
        except prescreen.CryptoTenSymbolFactorPrescreenError as exc:
            raise CryptoTenSymbolResearchLoopError(
                "research_loop_source_rows_invalid"
            ) from exc
        rows_by_symbol[symbol] = validated
        meta[symbol] = {
            "row_count": len(validated),
            "first_open_time": projection._iso(validated[0]["open_time"]),
            "last_open_time": projection._iso(validated[-1]["open_time"]),
            "gap_count": len(gaps),
        }
    return rows_by_symbol, meta


# ---------------------------------------------------------------------------
# Reused analysis, summary matrix, and diff
# ---------------------------------------------------------------------------


def _analysis_block(result: Mapping[str, Any]) -> dict[str, Any]:
    """Embed the pre-screen analysis minus its backfill-specific banner.

    The pre-screen top-level ``_non_evidence_fields`` banner describes its
    own network backfill path (``historical_backfill_no_pit``); here the same
    pure evaluation functions consume evidence-grade observation-store data,
    so only the metric payload is embedded and this artifact's own authority
    block governs.
    """

    candidates = result.get("candidates")
    if not isinstance(candidates, list) or [
        candidate.get("candidate_id") for candidate in candidates
    ] != list(REGISTERED_CANDIDATE_IDS):
        raise CryptoTenSymbolResearchLoopError("research_loop_registered_set_drift")
    return {
        "analysis_engine": "Crypto.ten_symbol_factor_prescreen.analyze",
        "forward_horizon_bars": result["forward_horizon_bars"],
        "forward_horizon_minutes": result["forward_horizon_minutes"],
        "non_overlap_stride": result["non_overlap_stride"],
        "cost_policy": result["cost_policy"],
        "candidates": candidates,
    }


def _variant_summary(metrics: Mapping[str, Any]) -> dict[str, Any]:
    subset = metrics.get("non_overlapping")
    if not isinstance(subset, Mapping):
        subset = {}
    return {
        "signal_count": metrics.get("signal_count"),
        "universe_count": metrics.get("universe_count"),
        "hit_rate": metrics.get("hit_rate"),
        "mean_gross": metrics.get("mean_gross"),
        "mean_net": metrics.get("mean_net"),
        "baseline_delta": metrics.get("baseline_delta"),
        "non_overlapping_signal_count": subset.get("signal_count"),
        "non_overlapping_mean_net": subset.get("mean_net"),
    }


def _metrics_summary(analyses: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for horizon_key, block in analyses.items():
        for candidate in block["candidates"]:
            variants = candidate.get("variants")
            if not isinstance(variants, Mapping):
                raise CryptoTenSymbolResearchLoopError(
                    "research_loop_analysis_invalid"
                )
            summary.setdefault(str(candidate["candidate_id"]), {})[horizon_key] = {
                str(name): _variant_summary(metrics)
                for name, metrics in variants.items()
            }
    return summary


def _candidate_recommendation(
    candidate_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive an evidence-bound automatic recommendation for one candidate.

    The recommendation uses the best non-overlapping cost-adjusted mean net
    across every evaluated horizon/variant; a positive value recommends
    ``auto_promote``, a resolved non-positive value recommends
    ``auto_demote``, and no resolved sample recommends ``auto_retain``.
    """

    if not isinstance(candidate_summary, Mapping):
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_analysis_invalid"
        )
    best: Decimal | None = None
    for horizon_key, variants in candidate_summary.items():
        if not isinstance(horizon_key, str) or not isinstance(variants, Mapping):
            raise CryptoTenSymbolResearchLoopError(
                "research_loop_analysis_invalid"
            )
        for name, metrics in variants.items():
            if not isinstance(name, str) or not isinstance(metrics, Mapping):
                raise CryptoTenSymbolResearchLoopError(
                    "research_loop_analysis_invalid"
                )
            value = metrics.get("non_overlapping_mean_net")
            if value is None:
                continue
            parsed = _decimal_value(value)
            if best is None or parsed > best:
                best = parsed
    if best is None:
        return {
            "recommendation": "auto_retain",
            "automatic_action": "retain_for_more_evidence",
        }
    if best > Decimal("0"):
        return {
            "recommendation": "auto_promote",
            "automatic_action": "promote_into_sim_capital",
        }
    return {
        "recommendation": "auto_demote",
        "automatic_action": "demote_or_retire",
    }


def _decimal_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_previous_report_invalid"
        )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_previous_report_invalid"
        ) from exc
    if not result.is_finite():
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_previous_report_invalid"
        )
    return result


def _count_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_previous_report_invalid"
        )
    return value


def _metric_delta(current: Any, previous: Any) -> str | None:
    current_value = _decimal_value(current)
    previous_value = _decimal_value(previous)
    if current_value is None or previous_value is None:
        return None
    return format(current_value - previous_value, "f")


def _variant_cell(current: Any, previous: Any) -> dict[str, Any]:
    if current is not None and not isinstance(current, Mapping):
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_previous_report_invalid"
        )
    if previous is not None and not isinstance(previous, Mapping):
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_previous_report_invalid"
        )
    if current is None:
        change = "removed"
    elif previous is None:
        change = "added"
    elif dict(current) == dict(previous):
        change = "unchanged"
    else:
        change = "changed"
    current_count = _count_value(current.get("signal_count")) if current else None
    previous_count = _count_value(previous.get("signal_count")) if previous else None
    return {
        "change": change,
        "previous": dict(previous) if previous is not None else None,
        "signal_count_delta": (
            current_count - previous_count
            if current_count is not None and previous_count is not None
            else None
        ),
        "hit_rate_delta": _metric_delta(
            current.get("hit_rate") if current else None,
            previous.get("hit_rate") if previous else None,
        ),
        "mean_gross_delta": _metric_delta(
            current.get("mean_gross") if current else None,
            previous.get("mean_gross") if previous else None,
        ),
        "mean_net_delta": _metric_delta(
            current.get("mean_net") if current else None,
            previous.get("mean_net") if previous else None,
        ),
        "baseline_delta_delta": _metric_delta(
            current.get("baseline_delta") if current else None,
            previous.get("baseline_delta") if previous else None,
        ),
        "non_overlapping_mean_net_delta": _metric_delta(
            current.get("non_overlapping_mean_net") if current else None,
            previous.get("non_overlapping_mean_net") if previous else None,
        ),
    }


def _horizon_keys(*summaries: Mapping[str, Any]) -> list[str]:
    keys: set[str] = set()
    for summary in summaries:
        for key in summary:
            if not isinstance(key, str) or not key.isdigit():
                raise CryptoTenSymbolResearchLoopError(
                    "research_loop_previous_report_invalid"
                )
            keys.add(key)
    return sorted(keys, key=int)


def _diff_vs_previous(
    previous: Mapping[str, Any] | None,
    summary: Mapping[str, Any],
    *,
    previous_checkpoint: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if previous is None or previous_checkpoint is None:
        return {
            "status": "initial_report",
            "previous_report_sha256": None,
            "previous_input_digest": None,
            "cells": {},
        }
    previous_summary = previous.get("metrics_summary")
    if not isinstance(previous_summary, Mapping):
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_previous_report_invalid"
        )
    cells: dict[str, Any] = {}
    for candidate_id in REGISTERED_CANDIDATE_IDS:
        current_horizons = summary.get(candidate_id) or {}
        previous_horizons = previous_summary.get(candidate_id) or {}
        if not isinstance(current_horizons, Mapping) or not isinstance(
            previous_horizons, Mapping
        ):
            raise CryptoTenSymbolResearchLoopError(
                "research_loop_previous_report_invalid"
            )
        horizon_cells: dict[str, Any] = {}
        for horizon_key in _horizon_keys(current_horizons, previous_horizons):
            current_variants = current_horizons.get(horizon_key) or {}
            previous_variants = previous_horizons.get(horizon_key) or {}
            if not isinstance(current_variants, Mapping) or not isinstance(
                previous_variants, Mapping
            ):
                raise CryptoTenSymbolResearchLoopError(
                    "research_loop_previous_report_invalid"
                )
            horizon_cells[horizon_key] = {
                variant: _variant_cell(
                    current_variants.get(variant), previous_variants.get(variant)
                )
                for variant in sorted(
                    set(current_variants) | set(previous_variants)
                )
            }
        cells[candidate_id] = horizon_cells
    return {
        "status": "compared",
        "previous_report_sha256": previous.get("report_sha256"),
        "previous_input_digest": previous_checkpoint.get("last_input_digest"),
        "cells": cells,
    }


# ---------------------------------------------------------------------------
# Report build and run
# ---------------------------------------------------------------------------


def _build_report(
    *,
    events: Sequence[Mapping[str, Any]],
    units: Sequence[Mapping[str, Any]],
    eligible: Sequence[Mapping[str, Any]],
    horizons: tuple[int, ...],
    analyses: Mapping[str, Any],
    meta: Mapping[str, Any],
    summary: Mapping[str, Any],
    diff: Mapping[str, Any],
) -> dict[str, Any]:
    report = {
        "contract": REVIEW_REPORT_CONTRACT,
        "event_type": "hypothesis_reevaluation_review",
        "loop_stage": LOOP_STAGE,
        "stage_boundaries": {
            "hypothesis_generation": "disabled_stage_1_registered_set_only",
            "evaluation_logic": "reused_unchanged_from_prescreen",
            "scheduler": "detached_one_shot_no_systemd",
            "promotion": "automatic_sim_domain",
            "execution": "not_connected",
        },
        "registered_candidate_ids": list(REGISTERED_CANDIDATE_IDS),
        "symbols": list(_SYMBOLS),
        "horizon_bars": list(horizons),
        "horizon_minutes": [horizon * 5 for horizon in horizons],
        "source": {
            "store_event_count": len(events),
            "store_head_checksum": str(events[-1]["checksum"]),
            "terminal_slot_count": len(units),
            "eligible_slot_count": len(eligible),
            "ineligible_slot_count": len(units) - len(eligible),
            "first_eligible_slot": projection._iso(eligible[0]["slot"]),
            "last_eligible_slot": projection._iso(eligible[-1]["slot"]),
            "terminal_units_sha256": _sha256(_terminal_unit_material(units)),
            "terminal_units": _terminal_unit_material(units),
            "data_window": dict(meta),
        },
        "analyses": dict(analyses),
        "metrics_summary": dict(summary),
        "diff_vs_previous": dict(diff),
        "review": {
            "recommendation": "automatic_reevaluation_complete",
            "per_candidate": {
                candidate_id: _candidate_recommendation(summary.get(candidate_id) or {})
                for candidate_id in REGISTERED_CANDIDATE_IDS
            },
        },
        "automatic_reevaluation": True,
        **projection._non_authority_fields(),
    }
    report["report_sha256"] = _sha256(report)
    return report


def run_ten_symbol_research_loop_once(
    *,
    store_root: Path | str,
    horizon_bars: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Re-estimate the registered candidates on the latest store evidence.

    The run deterministically rebuilds the full inventory from the verified
    event chain on every invocation: the same store state plus the same
    horizon configuration always yields the same report bytes.  A rerun over
    an unchanged input returns ``no_new_input`` after re-validating the
    checkpoint and its bound report; corrupted chains, sidecars, checkpoints
    or reports fail closed.
    """

    _assert_simulation_only()
    horizons = _validate_horizon_bars(horizon_bars)
    root = Path(store_root)
    store = _open_store(root)
    if store.pending_record_read_only() is not None:
        return _result(status="deferred_core_pending")
    try:
        events = store.events_read_only()
    except (CryptoTenSymbolObservationStoreError, OSError, ValueError) as exc:
        raise CryptoTenSymbolResearchLoopError("research_loop_core_invalid") from exc
    if not events:
        return _result(status="deferred_core_pending")
    try:
        units = projection._build_units(store)
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolResearchLoopError("research_loop_core_invalid") from exc
    if not units:
        return _result(status="deferred_core_pending")
    eligible = [unit for unit in units if unit["eligible"]]
    counts = {
        "terminal_slot_count": len(units),
        "eligible_slot_count": len(eligible),
        "ineligible_slot_count": len(units) - len(eligible),
    }
    if not eligible:
        return _result(
            status="insufficient_eligible_slots",
            horizon_bars=list(horizons),
            **counts,
        )
    input_digest = _sha256(
        {
            "contract": REVIEW_REPORT_CONTRACT,
            "horizon_bars": list(horizons),
            "store_event_count": len(events),
            "store_head_checksum": str(events[-1]["checksum"]),
            "terminal_units": _terminal_unit_material(units),
        }
    )
    evolution = _ensure_root(root)
    try:
        with projection._lock(evolution):
            current = _validated_current(evolution)
            if current is not None and current["checkpoint"].get(
                "last_input_digest"
            ) == input_digest:
                return _result(
                    status="no_new_input",
                    input_digest=input_digest,
                    report_sha256=current["checkpoint"]["report_sha256"],
                    report_path=str(
                        evolution
                        / "reports"
                        / f"{current['checkpoint']['report_sha256']}.json"
                    ),
                    horizon_bars=list(horizons),
                    **counts,
                )
            rows_by_symbol, meta = _merge_eligible_bars(eligible)
            analyses: dict[str, Any] = {}
            for horizon in horizons:
                try:
                    analysis = prescreen.analyze(
                        rows_by_symbol, meta=meta, horizon_bars=horizon
                    )
                except prescreen.CryptoTenSymbolFactorPrescreenError as exc:
                    raise CryptoTenSymbolResearchLoopError(
                        "research_loop_analysis_invalid"
                    ) from exc
                analyses[str(horizon)] = _analysis_block(analysis)
            summary = _metrics_summary(analyses)
            diff = _diff_vs_previous(
                current["report"] if current is not None else None,
                summary,
                previous_checkpoint=(
                    current["checkpoint"] if current is not None else None
                ),
            )
            report = _build_report(
                events=events,
                units=units,
                eligible=eligible,
                horizons=horizons,
                analyses=analyses,
                meta=meta,
                summary=summary,
                diff=diff,
            )
            report_path = evolution / "reports" / f"{report['report_sha256']}.json"
            projection._write_immutable(report_path, report)
            checkpoint = {
                "contract": LOOP_CHECKPOINT_CONTRACT,
                "last_input_digest": input_digest,
                "report_sha256": report["report_sha256"],
                "last_eligible_slot": projection._iso(eligible[-1]["slot"]),
                **counts,
                **projection._non_authority_fields(),
            }
            checkpoint["checkpoint_sha256"] = _sha256(checkpoint)
            _atomic_checkpoint(evolution / CHECKPOINT_FILENAME, checkpoint)
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolResearchLoopError(
            "research_loop_artifact_invalid"
        ) from exc
    return _result(
        status="report_written",
        input_digest=input_digest,
        report_sha256=report["report_sha256"],
        report_path=str(report_path),
        horizon_bars=list(horizons),
        last_eligible_slot=projection._iso(eligible[-1]["slot"]),
        **counts,
    )


def ten_symbol_research_loop_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") not in {
        "report_written",
        "no_new_input",
        "deferred_core_pending",
        "insufficient_eligible_slots",
    }:
        return 2
    return (
        0
        if result.get("automatic_reevaluation") is True
        and result.get("loop_stage") == LOOP_STAGE
        and all(
            result.get(key) == value
            for key, value in projection._non_authority_fields().items()
        )
        else 2
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detached stage-1 Crypto ten-symbol research-evolution loop"
            " (one-shot, read-only, no scheduler)"
        )
    )
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument(
        "--horizon-bars",
        type=str,
        default=",".join(str(horizon) for horizon in DEFAULT_HORIZON_BARS),
        help=(
            "Comma-separated forward label horizons in 5m bars; subset of"
            " 12,48,144,288 (60/240/720/1440 minutes)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        horizons = tuple(int(token) for token in str(args.horizon_bars).split(","))
        result = run_ten_symbol_research_loop_once(
            store_root=args.store_root, horizon_bars=horizons
        )
    except Exception:
        print("crypto ten-symbol research loop failed closed", file=sys.stderr)
        return 2
    if ten_symbol_research_loop_exit_code(result):
        print("crypto ten-symbol research loop failed closed", file=sys.stderr)
        return 2
    print(
        json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CHECKPOINT_FILENAME",
    "DEFAULT_HORIZON_BARS",
    "LOOP_CHECKPOINT_CONTRACT",
    "LOOP_STAGE",
    "REGISTERED_CANDIDATE_IDS",
    "REVIEW_REPORT_CONTRACT",
    "CryptoTenSymbolResearchLoopError",
    "main",
    "run_ten_symbol_research_loop_once",
    "ten_symbol_research_loop_exit_code",
]
