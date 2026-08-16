"""Stage-2 research-evolution loop: hypothesis generation, proposal only.

This module is the second stage of the crypto research-evolution closed
loop.  It is a detached, offline, read-only one-shot generator: a frozen,
versioned in-repository generation config (factor family x parameter grid x
label horizons) deterministically expands into a candidate hypothesis set;
every candidate passes a lightweight feasibility check (required data-plane
availability and minimum sample sizes) and the whole set is emitted as one
immutable, checksum-bound *registration proposal* artifact for human review.

Stage-2 boundaries, all hard-coded:

- automatic registration: candidates that pass the lightweight feasibility
  check are fixed ``registration_status=auto_registered`` and marked
  ``registered_into_prescreen``/``registered_into_evaluation``; blocked
  candidates stay unregistered, and the stage-1 registered-set drift check
  still fails closed;
- no evaluation: the generator never runs the pre-screen, the factor
  projection or the strategy evaluation on the candidates;
- no scheduler installation: one-shot invocation only, no systemd unit;
- automatic promotion stays inside the simulation domain: the review block
  is fixed ``automatic_registration`` and never authorizes real trading.

Input integrity mirrors the stage-1 precedent: the observation store event
chain is verified read-only, every terminal slot's bars sidecar is
re-derived and value-compared (a missing or digest-drifting sidecar marks
the slot ineligible), and any chain corruption fails closed.  B-class data
planes beyond OHLCV bars are declared only through an explicitly supplied,
strictly validated data-plane evidence manifest; undeclared planes default
to unavailable.  The same config plus the same input state always produces
the same artifact bytes; a rerun over an unchanged input returns
``no_new_input`` without writing.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import uuid

from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.market_observation import OBSERVATION_SYMBOLS
import Crypto.ten_symbol_factor_prescreen as prescreen
import Crypto.ten_symbol_factor_research as projection
import Crypto.ten_symbol_research_loop as research_loop
from Crypto.ten_symbol_observation_store import (
    CryptoTenSymbolObservationStoreError,
)


PROPOSAL_CONTRACT = "tradingagent.crypto.ten_symbol_hypothesis_generator_proposal.v2"
GENERATOR_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.ten_symbol_hypothesis_generator_checkpoint.v2"
)
DATA_PLANE_MANIFEST_CONTRACT = (
    "tradingagent.crypto.ten_symbol_hypothesis_generator_data_planes.v1"
)
CHECKPOINT_FILENAME = "hypothesis_generator_checkpoint.json"
GENERATOR_STAGE = "stage_2_hypothesis_generation_auto_registration"
GENERATION_CONFIG_ID = "crypto-ten-symbol-hypothesis-generation-v1"
REGISTRATION_STATUS = "auto_registered"
BLOCKED_REGISTRATION_STATUS = "blocked"
_VARIANT_PATTERN = re.compile(r"^[a-z0-9_]+$")

# Frozen data-plane registry.  ``ohlcv_bars`` is measured directly from the
# verified observation store; the B-class planes are only ever declared
# through the caller-supplied data-plane evidence manifest.
PLANES: dict[str, dict[str, Any]] = {
    "ohlcv_bars": {
        "dataset_namespace": "crypto.spot.binance.<symbol>.5m",
        "evidence_source": "ten_symbol observation store bars sidecars",
        "declaration": "store_derived",
        "min_sample_count": None,
    },
    "realized_spreads": {
        "dataset_namespace": "crypto.spot.binance.<symbol>.book_ticker",
        "evidence_source": (
            "spreads sidecars plus ten_symbol_spread_projection artifacts"
        ),
        "declaration": "data_plane_manifest",
        "min_sample_count": 12,
    },
    "open_interest_5m": {
        "dataset_namespace": "crypto.perp.binance.<symbol>.open_interest",
        "evidence_source": "TradingDatas perp open-interest 5m series",
        "declaration": "data_plane_manifest",
        "min_sample_count": 10000,
    },
    "premium_index": {
        "dataset_namespace": "crypto.perp.binance.<symbol>.premium_index",
        "evidence_source": "TradingDatas premium-index dump (funding proxy)",
        "declaration": "data_plane_manifest",
        "min_sample_count": 200,
    },
}
B_CLASS_PLANES = tuple(
    plane
    for plane, meta in PLANES.items()
    if meta["declaration"] == "data_plane_manifest"
)

# Frozen, versioned generation config: factor family x parameter grid x
# horizons.  Any drift against this schema fails closed; changing the grid
# requires a new config id in a human-reviewed change.
GENERATION_CONFIG: dict[str, Any] = {
    "config_id": GENERATION_CONFIG_ID,
    "evidence_class": "B",
    "horizon_bars": list(prescreen.ALLOWED_HORIZON_BARS),
    "families": [
        {
            "family_id": "oi_change_rate",
            "hypothesis_template": (
                "open-interest change-rate confirmation: per symbol, long"
                " when the open-interest change over the past"
                " {lookback_bars} bars is >= {oi_change_threshold} and the"
                " 1h return is >= 0"
            ),
            "evidence_basis": (
                "OI-only variants of price confirmation are a standard"
                " futures quant construction; the OHLCV-only pre-screen"
                " rejected all four A-class candidates on every horizon,"
                " so the next edge must come from B-class data; the"
                " crypto.perp.binance.<symbol>.open_interest 5m series is"
                " being backfilled as that evidence plane"
            ),
            "required_planes": ["ohlcv_bars", "open_interest_5m"],
            "parameter_sets": [
                {
                    "variant": "l12_t0p005",
                    "parameters": {"lookback_bars": 12, "oi_change_threshold": "0.005"},
                },
                {
                    "variant": "l12_t0p01",
                    "parameters": {"lookback_bars": 12, "oi_change_threshold": "0.01"},
                },
                {
                    "variant": "l48_t0p01",
                    "parameters": {"lookback_bars": 48, "oi_change_threshold": "0.01"},
                },
                {
                    "variant": "l144_t0p02",
                    "parameters": {"lookback_bars": 144, "oi_change_threshold": "0.02"},
                },
                {
                    "variant": "l288_t0p02",
                    "parameters": {"lookback_bars": 288, "oi_change_threshold": "0.02"},
                },
            ],
        },
        {
            "family_id": "price_oi_divergence",
            "hypothesis_template": (
                "price/OI divergence: per symbol, when the {lookback_bars}-bar"
                " price return and the open-interest change over the same"
                " window have opposite signs, {direction} the move (fade ="
                " expect reversal, follow = expect continuation)"
            ),
            "evidence_basis": (
                "price/open-interest divergence is a classic futures"
                " positioning signal; requires the open_interest_5m plane on"
                " top of the evidence-grade bars history"
            ),
            "required_planes": ["ohlcv_bars", "open_interest_5m"],
            "parameter_sets": [
                {
                    "variant": "l12_fade",
                    "parameters": {"lookback_bars": 12, "direction": "fade"},
                },
                {
                    "variant": "l12_follow",
                    "parameters": {"lookback_bars": 12, "direction": "follow"},
                },
                {
                    "variant": "l48_fade",
                    "parameters": {"lookback_bars": 48, "direction": "fade"},
                },
                {
                    "variant": "l48_follow",
                    "parameters": {"lookback_bars": 48, "direction": "follow"},
                },
                {
                    "variant": "l288_fade",
                    "parameters": {"lookback_bars": 288, "direction": "fade"},
                },
            ],
        },
        {
            "family_id": "oi_weighted_momentum",
            "hypothesis_template": (
                "OI-weighted cross-sectional momentum: each slot rank symbols"
                " by the {momentum_lookback_bars}-bar return weighted by the"
                " sign and magnitude of the {oi_lookback_bars}-bar"
                " open-interest change, long top-{top_k} equal weight"
            ),
            "evidence_basis": (
                "cross-sectional momentum conditioned on positioning flows;"
                " extends the rejected OHLCV-only xs_rs candidate with the"
                " open_interest_5m plane as the weighting evidence"
            ),
            "required_planes": ["ohlcv_bars", "open_interest_5m"],
            "parameter_sets": [
                {
                    "variant": "m12_o12_k2",
                    "parameters": {
                        "momentum_lookback_bars": 12,
                        "oi_lookback_bars": 12,
                        "top_k": 2,
                    },
                },
                {
                    "variant": "m12_o48_k2",
                    "parameters": {
                        "momentum_lookback_bars": 12,
                        "oi_lookback_bars": 48,
                        "top_k": 2,
                    },
                },
                {
                    "variant": "m48_o48_k2",
                    "parameters": {
                        "momentum_lookback_bars": 48,
                        "oi_lookback_bars": 48,
                        "top_k": 2,
                    },
                },
                {
                    "variant": "m48_o48_k3",
                    "parameters": {
                        "momentum_lookback_bars": 48,
                        "oi_lookback_bars": 48,
                        "top_k": 3,
                    },
                },
                {
                    "variant": "m288_o288_k2",
                    "parameters": {
                        "momentum_lookback_bars": 288,
                        "oi_lookback_bars": 288,
                        "top_k": 2,
                    },
                },
            ],
        },
        {
            "family_id": "spread_regime",
            "hypothesis_template": (
                "spread-regime gate: evaluate the frozen"
                " time_series_momentum_v1 signal only in slots whose symbol"
                " realized half-spread regime (latest sufficient UTC day"
                " bucket p75) is {mode} the {regime_threshold_bps}bps"
                " threshold (narrow_only = trade only at or below, wide_only"
                " = trade only above)"
            ),
            "evidence_basis": (
                "execution-cost regimes gate whether any signal survives"
                " fees; measured realized spreads (median ~1.06bps from the"
                " ten_symbol_spread_projection artifact) bracket the"
                " threshold grid; requires the realized_spreads plane"
            ),
            "required_planes": ["ohlcv_bars", "realized_spreads"],
            "parameter_sets": [
                {
                    "variant": "t1p0_narrow",
                    "parameters": {"regime_threshold_bps": "1.0", "mode": "narrow_only"},
                },
                {
                    "variant": "t1p5_narrow",
                    "parameters": {"regime_threshold_bps": "1.5", "mode": "narrow_only"},
                },
                {
                    "variant": "t2p0_narrow",
                    "parameters": {"regime_threshold_bps": "2.0", "mode": "narrow_only"},
                },
                {
                    "variant": "t1p5_wide",
                    "parameters": {"regime_threshold_bps": "1.5", "mode": "wide_only"},
                },
            ],
        },
        {
            "family_id": "premium_momentum",
            "hypothesis_template": (
                "premium-index momentum: per symbol, long when the premium"
                " index (funding proxy) change over the past {lookback_bars}"
                " bars is >= {premium_threshold}; a negative threshold"
                " variant fades an extreme negative premium"
            ),
            "evidence_basis": (
                "premium/funding momentum is a standard perp quant"
                " construction; requires the premium_index plane whose dump"
                " slicing is in development"
            ),
            "required_planes": ["ohlcv_bars", "premium_index"],
            "parameter_sets": [
                {
                    "variant": "l12_t0p0005",
                    "parameters": {"lookback_bars": 12, "premium_threshold": "0.0005"},
                },
                {
                    "variant": "l48_t0p001",
                    "parameters": {"lookback_bars": 48, "premium_threshold": "0.001"},
                },
                {
                    "variant": "l288_t0p002",
                    "parameters": {"lookback_bars": 288, "premium_threshold": "0.002"},
                },
                {
                    "variant": "l48_tm0p001",
                    "parameters": {"lookback_bars": 48, "premium_threshold": "-0.001"},
                },
            ],
        },
    ],
}

_FAMILY_IDS = tuple(family["family_id"] for family in GENERATION_CONFIG["families"])
_MAX_PARAMETER_SETS_PER_FAMILY = 5


class CryptoTenSymbolHypothesisGeneratorError(RuntimeError):
    """Stable fail-closed error for the stage-2 hypothesis generator."""


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
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_payload_invalid"
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
        "contract": PROPOSAL_CONTRACT,
        "status": status,
        "loop_stage": GENERATOR_STAGE,
        "learning_mode": "detached_offline_worker",
        "automatic_registration": True,
        **fields,
        **projection._non_authority_fields(),
    }


# ---------------------------------------------------------------------------
# Frozen generation config: strict validation and deterministic expansion
# ---------------------------------------------------------------------------


def _validate_decimal_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        ) from exc
    if not parsed.is_finite():
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        )
    return value


def _validate_generation_config(config: Any) -> dict[str, Any]:
    """Strictly validate the frozen generation config; any drift fails closed."""

    if not isinstance(config, Mapping) or set(config) != {
        "config_id",
        "evidence_class",
        "horizon_bars",
        "families",
    }:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        )
    if (
        config["config_id"] != GENERATION_CONFIG_ID
        or config["evidence_class"] != "B"
    ):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        )
    horizons = config["horizon_bars"]
    if (
        not isinstance(horizons, list)
        or not horizons
        or any(
            not isinstance(horizon, int)
            or isinstance(horizon, bool)
            or horizon not in prescreen.ALLOWED_HORIZON_BARS
            for horizon in horizons
        )
        or len(set(horizons)) != len(horizons)
        or sorted(horizons) != horizons
    ):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        )
    families = config["families"]
    if not isinstance(families, list) or [
        family.get("family_id") if isinstance(family, Mapping) else None
        for family in families
    ] != list(_FAMILY_IDS):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        )
    seen_variants: set[str] = set()
    for family in families:
        if set(family) != {
            "family_id",
            "hypothesis_template",
            "evidence_basis",
            "required_planes",
            "parameter_sets",
        }:
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_config_drift"
            )
        template = family["hypothesis_template"]
        if not isinstance(template, str) or not template:
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_config_drift"
            )
        if not isinstance(family["evidence_basis"], str) or not family[
            "evidence_basis"
        ]:
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_config_drift"
            )
        planes = family["required_planes"]
        if (
            not isinstance(planes, list)
            or not planes
            or "ohlcv_bars" not in planes
            or len(set(planes)) != len(planes)
            or any(plane not in PLANES for plane in planes)
        ):
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_config_drift"
            )
        placeholders = set(re.findall(r"{([a-z0-9_]+)}", template))
        parameter_sets = family["parameter_sets"]
        if (
            not isinstance(parameter_sets, list)
            or not parameter_sets
            or len(parameter_sets) > _MAX_PARAMETER_SETS_PER_FAMILY
        ):
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_config_drift"
            )
        for entry in parameter_sets:
            if not isinstance(entry, Mapping) or set(entry) != {
                "variant",
                "parameters",
            }:
                raise CryptoTenSymbolHypothesisGeneratorError(
                    "hypothesis_generator_config_drift"
                )
            variant = entry["variant"]
            if not isinstance(variant, str) or not _VARIANT_PATTERN.match(variant):
                raise CryptoTenSymbolHypothesisGeneratorError(
                    "hypothesis_generator_config_drift"
                )
            key = (family["family_id"], variant)
            if key in seen_variants:
                raise CryptoTenSymbolHypothesisGeneratorError(
                    "hypothesis_generator_config_drift"
                )
            seen_variants.add(key)
            parameters = entry["parameters"]
            if not isinstance(parameters, Mapping) or set(parameters) != placeholders:
                raise CryptoTenSymbolHypothesisGeneratorError(
                    "hypothesis_generator_config_drift"
                )
            for name, value in parameters.items():
                if isinstance(value, bool):
                    raise CryptoTenSymbolHypothesisGeneratorError(
                        "hypothesis_generator_config_drift"
                    )
                if isinstance(value, int):
                    if value <= 0:
                        raise CryptoTenSymbolHypothesisGeneratorError(
                            "hypothesis_generator_config_drift"
                        )
                elif isinstance(value, str):
                    if value in ("fade", "follow", "narrow_only", "wide_only"):
                        continue
                    _validate_decimal_text(value)
                else:
                    raise CryptoTenSymbolHypothesisGeneratorError(
                        "hypothesis_generator_config_drift"
                    )
    return deepcopy(dict(config))


def _candidate_lookback_bars(parameters: Mapping[str, Any]) -> int:
    lookbacks = [
        value
        for name, value in parameters.items()
        if "lookback" in name and isinstance(value, int)
    ]
    return max(lookbacks) if lookbacks else 0


def expand_candidates(
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Deterministically expand the frozen config into candidate definitions.

    Pure and config-only: feasibility is attached separately by the caller.
    The same config always yields the same candidate list, byte for byte.
    """

    validated = _validate_generation_config(
        GENERATION_CONFIG if config is None else config
    )
    horizons = list(validated["horizon_bars"])
    min_horizon = min(horizons)
    candidates: list[dict[str, Any]] = []
    for family in validated["families"]:
        for entry in family["parameter_sets"]:
            parameters = dict(entry["parameters"])
            candidate_id = f"{family['family_id']}__{entry['variant']}"
            lookback = _candidate_lookback_bars(parameters)
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "family": family["family_id"],
                    "hypothesis": family["hypothesis_template"].format(**parameters),
                    "evidence_basis": family["evidence_basis"],
                    "parameters": parameters,
                    "evaluation_horizon_bars": horizons,
                    "evaluation_horizon_minutes": [
                        horizon * 5 for horizon in horizons
                    ],
                    "required_evidence": [
                        {
                            "plane": plane,
                            "dataset_namespace": PLANES[plane]["dataset_namespace"],
                            "evidence_source": PLANES[plane]["evidence_source"],
                            "min_sample_count": (
                                lookback + 13 + min_horizon
                                if plane == "ohlcv_bars"
                                else PLANES[plane]["min_sample_count"]
                            ),
                        }
                        for plane in family["required_planes"]
                    ],
                    "registration_status": REGISTRATION_STATUS,
                    "registered_into_prescreen": False,
                    "registered_into_evaluation": False,
                }
            )
    ids = [candidate["candidate_id"] for candidate in candidates]
    if len(set(ids)) != len(ids) or any(
        candidate_id in prescreen._CANDIDATE_IDS for candidate_id in ids
    ):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_config_drift"
        )
    return candidates


# ---------------------------------------------------------------------------
# Data-plane evidence manifest (caller-supplied, strictly validated)
# ---------------------------------------------------------------------------


def _load_data_plane_manifest(path: Path) -> dict[str, Any]:
    try:
        encoded = projection._assert_regular(
            path, reason="hypothesis_generator_manifest_invalid"
        )
        manifest = json.loads(encoded.decode("utf-8"))
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_manifest_invalid"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_manifest_invalid"
        ) from exc
    if not isinstance(manifest, dict) or set(manifest) != {"contract", "planes"}:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_manifest_invalid"
        )
    if manifest["contract"] != DATA_PLANE_MANIFEST_CONTRACT:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_manifest_invalid"
        )
    planes = manifest["planes"]
    if not isinstance(planes, dict) or not set(planes) <= set(B_CLASS_PLANES):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_manifest_invalid"
        )
    for plane, state in planes.items():
        if not isinstance(state, dict) or not set(state) <= {
            "status",
            "sample_count",
            "evidence_ref",
            "evidence_sha256",
        }:
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_manifest_invalid"
            )
        if state.get("status") not in ("available", "accumulating", "unavailable"):
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_manifest_invalid"
            )
        sample_count = state.get("sample_count")
        if sample_count is not None and (
            not isinstance(sample_count, int)
            or isinstance(sample_count, bool)
            or sample_count < 0
        ):
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_manifest_invalid"
            )
        if state.get("status") == "available" and sample_count is None:
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_manifest_invalid"
            )
        for key in ("evidence_ref",):
            if state.get(key) is not None and not isinstance(state.get(key), str):
                raise CryptoTenSymbolHypothesisGeneratorError(
                    "hypothesis_generator_manifest_invalid"
                )
        if state.get("evidence_sha256") is not None and not _is_sha256(
            state.get("evidence_sha256")
        ):
            raise CryptoTenSymbolHypothesisGeneratorError(
                "hypothesis_generator_manifest_invalid"
            )
    return manifest


def _plane_states(
    manifest: Mapping[str, Any] | None,
    *,
    bars_sample_count: int,
) -> dict[str, dict[str, Any]]:
    declared = manifest["planes"] if manifest is not None else {}
    states: dict[str, dict[str, Any]] = {
        "ohlcv_bars": {
            "status": "available",
            "sample_count": bars_sample_count,
            "declaration": "store_derived",
        }
    }
    for plane in B_CLASS_PLANES:
        state = declared.get(plane)
        if state is None:
            states[plane] = {
                "status": "unavailable",
                "sample_count": None,
                "declaration": "plane_not_declared",
            }
        else:
            states[plane] = {
                "status": state["status"],
                "sample_count": state.get("sample_count"),
                "declaration": "data_plane_manifest",
                "evidence_ref": state.get("evidence_ref"),
                "evidence_sha256": state.get("evidence_sha256"),
            }
    return states


# ---------------------------------------------------------------------------
# Lightweight per-candidate feasibility checks
# ---------------------------------------------------------------------------


def _candidate_feasibility(
    candidate: Mapping[str, Any],
    plane_states: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for requirement in candidate["required_evidence"]:
        plane = requirement["plane"]
        state = plane_states[plane]
        minimum = requirement["min_sample_count"]
        sample_count = state.get("sample_count")
        if state.get("status") != "available":
            ok = False
            reason = f"{plane}_unavailable"
        elif sample_count is None or sample_count < minimum:
            ok = False
            reason = f"{plane}_insufficient_samples"
        else:
            ok = True
            reason = None
        checks.append(
            {
                "plane": plane,
                "min_sample_count": minimum,
                "observed_status": state.get("status"),
                "observed_sample_count": sample_count,
                "ok": ok,
                "reason": reason,
            }
        )
    return {
        "status": (
            "feasible_for_auto_registration"
            if all(check["ok"] for check in checks)
            else "blocked"
        ),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Artifact namespace: <store_root>/evolution/ten_symbol_hypothesis_generator/
# ---------------------------------------------------------------------------


def _generator_root(root: Path) -> Path:
    return root / "evolution" / "ten_symbol_hypothesis_generator"


def _ensure_root(root: Path) -> Path:
    parent = root / "evolution"
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_directory_invalid"
        )
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    evolution = _generator_root(root)
    for directory in (evolution, evolution / "proposals"):
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise CryptoTenSymbolHypothesisGeneratorError(
                    "hypothesis_generator_directory_invalid"
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
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_checkpoint_write_failed"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_proposal(evolution: Path, proposal_sha256: str) -> dict[str, Any]:
    path = evolution / "proposals" / f"{proposal_sha256}.json"
    try:
        proposal = projection._parse_canonical(
            path, reason="hypothesis_generator_proposal_invalid"
        )
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_proposal_invalid"
        ) from exc
    material = dict(proposal)
    claimed = material.pop("proposal_sha256", None)
    if (
        proposal.get("contract") != PROPOSAL_CONTRACT
        or proposal.get("loop_stage") != GENERATOR_STAGE
        or proposal.get("automatic_registration") is not True
        or proposal.get("generation_config_id") != GENERATION_CONFIG_ID
        or claimed != _sha256(material)
        or claimed != proposal_sha256
        or any(
            proposal.get(key) != value
            for key, value in projection._non_authority_fields().items()
        )
    ):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_proposal_invalid"
        )
    candidates = proposal.get("candidates")
    if not isinstance(candidates, list) or any(
        not isinstance(candidate, Mapping)
        or candidate.get("registration_status")
        not in (REGISTRATION_STATUS, BLOCKED_REGISTRATION_STATUS)
        or candidate.get("registered_into_prescreen")
        != (candidate.get("registration_status") == REGISTRATION_STATUS)
        or candidate.get("registered_into_evaluation")
        != (candidate.get("registration_status") == REGISTRATION_STATUS)
        for candidate in candidates
    ):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_proposal_invalid"
        )
    return proposal


def _validated_current(evolution: Path) -> dict[str, Any] | None:
    """Validate only the compact checkpoint and its one bound proposal."""

    checkpoint_path = evolution / CHECKPOINT_FILENAME
    if not checkpoint_path.exists() and not checkpoint_path.is_symlink():
        return None
    try:
        current = projection._parse_canonical(
            checkpoint_path, reason="hypothesis_generator_checkpoint_invalid"
        )
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_checkpoint_invalid"
        ) from exc
    material = dict(current)
    claimed = material.pop("checkpoint_sha256", None)
    if (
        current.get("contract") != GENERATOR_CHECKPOINT_CONTRACT
        or claimed != _sha256(material)
        or not _is_sha256(current.get("last_input_digest"))
        or not _is_sha256(current.get("proposal_sha256"))
        or any(
            current.get(key) != value
            for key, value in projection._non_authority_fields().items()
        )
    ):
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_checkpoint_invalid"
        )
    return {
        "checkpoint": current,
        "proposal": _load_proposal(evolution, str(current["proposal_sha256"])),
    }


# ---------------------------------------------------------------------------
# Proposal build and run
# ---------------------------------------------------------------------------


def _build_proposal(
    *,
    config: Mapping[str, Any],
    config_sha256: str,
    manifest_sha256: str | None,
    events: Sequence[Mapping[str, Any]],
    units: Sequence[Mapping[str, Any]],
    eligible: Sequence[Mapping[str, Any]],
    meta: Mapping[str, Any],
    plane_states: Mapping[str, Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    proposal = {
        "contract": PROPOSAL_CONTRACT,
        "event_type": "hypothesis_registration_proposal",
        "loop_stage": GENERATOR_STAGE,
        "stage_boundaries": {
            "registration": "auto_register_feasible",
            "evaluation": "not_run_by_generator",
            "scheduler": "detached_one_shot_no_systemd",
            "promotion": "automatic_sim_domain",
            "execution": "not_connected",
        },
        "generation_config_id": config["config_id"],
        "generation_config_sha256": config_sha256,
        "generation_config": dict(config),
        "symbols": list(OBSERVATION_SYMBOLS),
        "source": {
            "store_event_count": len(events),
            "store_head_checksum": str(events[-1]["checksum"]),
            "terminal_slot_count": len(units),
            "eligible_slot_count": len(eligible),
            "ineligible_slot_count": len(units) - len(eligible),
            "first_eligible_slot": projection._iso(eligible[0]["slot"]),
            "last_eligible_slot": projection._iso(eligible[-1]["slot"]),
            "terminal_units_sha256": _sha256(
                research_loop._terminal_unit_material(units)
            ),
            "data_window": dict(meta),
            "data_plane_manifest_sha256": manifest_sha256,
            "plane_states": {plane: dict(state) for plane, state in plane_states.items()},
        },
        "candidate_count": len(candidates),
        "candidates": [dict(candidate) for candidate in candidates],
        "review": {
            "recommendation": "automatic_registration",
            "registration": "auto_registered_feasible",
            "per_candidate": {
                candidate["candidate_id"]: {
                    "recommendation": (
                        "auto_register"
                        if candidate["registration_status"] == REGISTRATION_STATUS
                        else "blocked"
                    ),
                    "automatic_action": (
                        "register_into_prescreen"
                        if candidate["registration_status"] == REGISTRATION_STATUS
                        else "none"
                    ),
                }
                for candidate in candidates
            },
        },
        "automatic_registration": True,
        **projection._non_authority_fields(),
    }
    proposal["proposal_sha256"] = _sha256(proposal)
    return proposal


def run_ten_symbol_hypothesis_generation_once(
    *,
    store_root: Path | str,
    data_plane_manifest: Path | str | None = None,
) -> dict[str, Any]:
    """Expand the frozen config and emit one immutable proposal artifact.

    The run deterministically rebuilds the full input state on every
    invocation: the same frozen config, the same store state and the same
    data-plane manifest always yield the same proposal bytes.  A rerun over
    an unchanged input returns ``no_new_input`` after re-validating the
    checkpoint and its bound proposal; corrupted chains, manifests,
    checkpoints or proposals fail closed.
    """

    _assert_simulation_only()
    config = _validate_generation_config(GENERATION_CONFIG)
    config_sha256 = _sha256(config)
    manifest: dict[str, Any] | None = None
    manifest_sha256: str | None = None
    if data_plane_manifest is not None:
        manifest = _load_data_plane_manifest(Path(data_plane_manifest))
        manifest_sha256 = _sha256(manifest)
    root = Path(store_root)
    try:
        store = projection._open_store(root)
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_root_incomplete"
        ) from exc
    if store.pending_record_read_only() is not None:
        return _result(status="deferred_core_pending")
    try:
        events = store.events_read_only()
    except (CryptoTenSymbolObservationStoreError, OSError, ValueError) as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_core_invalid"
        ) from exc
    if not events:
        return _result(status="deferred_core_pending")
    try:
        units = projection._build_units(store)
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_core_invalid"
        ) from exc
    if not units:
        return _result(status="deferred_core_pending")
    eligible = [unit for unit in units if unit["eligible"]]
    counts = {
        "terminal_slot_count": len(units),
        "eligible_slot_count": len(eligible),
        "ineligible_slot_count": len(units) - len(eligible),
    }
    if not eligible:
        return _result(status="insufficient_eligible_slots", **counts)
    input_digest = _sha256(
        {
            "contract": PROPOSAL_CONTRACT,
            "generation_config_sha256": config_sha256,
            "data_plane_manifest_sha256": manifest_sha256,
            "store_event_count": len(events),
            "store_head_checksum": str(events[-1]["checksum"]),
            "terminal_units": research_loop._terminal_unit_material(units),
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
                    proposal_sha256=current["checkpoint"]["proposal_sha256"],
                    proposal_path=str(
                        evolution
                        / "proposals"
                        / f"{current['checkpoint']['proposal_sha256']}.json"
                    ),
                    **counts,
                )
            try:
                _rows_by_symbol, meta = research_loop._merge_eligible_bars(eligible)
            except research_loop.CryptoTenSymbolResearchLoopError as exc:
                raise CryptoTenSymbolHypothesisGeneratorError(
                    "hypothesis_generator_source_rows_invalid"
                ) from exc
            bars_sample_count = min(
                int(meta[symbol]["row_count"]) for symbol in OBSERVATION_SYMBOLS
            )
            plane_states = _plane_states(
                manifest, bars_sample_count=bars_sample_count
            )
            expanded = expand_candidates(config)
            candidates = []
            for candidate in expanded:
                feasibility = _candidate_feasibility(candidate, plane_states)
                feasible = feasibility["status"] == "feasible_for_auto_registration"
                candidates.append(
                    {
                        **candidate,
                        "feasibility": feasibility,
                        "registration_status": (
                            REGISTRATION_STATUS
                            if feasible
                            else BLOCKED_REGISTRATION_STATUS
                        ),
                        "registered_into_prescreen": feasible,
                        "registered_into_evaluation": feasible,
                    }
                )
            proposal = _build_proposal(
                config=config,
                config_sha256=config_sha256,
                manifest_sha256=manifest_sha256,
                events=events,
                units=units,
                eligible=eligible,
                meta=meta,
                plane_states=plane_states,
                candidates=candidates,
            )
            proposal_path = (
                evolution / "proposals" / f"{proposal['proposal_sha256']}.json"
            )
            projection._write_immutable(proposal_path, proposal)
            checkpoint = {
                "contract": GENERATOR_CHECKPOINT_CONTRACT,
                "last_input_digest": input_digest,
                "proposal_sha256": proposal["proposal_sha256"],
                "generation_config_sha256": config_sha256,
                "last_eligible_slot": projection._iso(eligible[-1]["slot"]),
                **counts,
                **projection._non_authority_fields(),
            }
            checkpoint["checkpoint_sha256"] = _sha256(checkpoint)
            _atomic_checkpoint(evolution / CHECKPOINT_FILENAME, checkpoint)
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolHypothesisGeneratorError(
            "hypothesis_generator_artifact_invalid"
        ) from exc
    return _result(
        status="proposal_written",
        input_digest=input_digest,
        proposal_sha256=proposal["proposal_sha256"],
        proposal_path=str(proposal_path),
        candidate_count=len(candidates),
        feasible_candidate_count=sum(
            candidate["feasibility"]["status"] == "feasible_for_auto_registration"
            for candidate in candidates
        ),
        last_eligible_slot=projection._iso(eligible[-1]["slot"]),
        **counts,
    )


def ten_symbol_hypothesis_generator_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") not in {
        "proposal_written",
        "no_new_input",
        "deferred_core_pending",
        "insufficient_eligible_slots",
    }:
        return 2
    return (
        0
        if result.get("automatic_registration") is True
        and result.get("loop_stage") == GENERATOR_STAGE
        and all(
            result.get(key) == value
            for key, value in projection._non_authority_fields().items()
        )
        else 2
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detached stage-2 Crypto ten-symbol hypothesis generator"
            " (one-shot, read-only, proposal only, no scheduler)"
        )
    )
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument(
        "--data-plane-manifest",
        type=Path,
        default=None,
        help=(
            "Optional canonical JSON declaring B-class data-plane"
            " availability (contract "
            f"{DATA_PLANE_MANIFEST_CONTRACT}); undeclared planes default"
            " to unavailable"
        ),
    )
    args = parser.parse_args(argv)
    try:
        result = run_ten_symbol_hypothesis_generation_once(
            store_root=args.store_root,
            data_plane_manifest=args.data_plane_manifest,
        )
    except Exception:
        print("crypto ten-symbol hypothesis generator failed closed", file=sys.stderr)
        return 2
    if ten_symbol_hypothesis_generator_exit_code(result):
        print("crypto ten-symbol hypothesis generator failed closed", file=sys.stderr)
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
    "B_CLASS_PLANES",
    "BLOCKED_REGISTRATION_STATUS",
    "CHECKPOINT_FILENAME",
    "DATA_PLANE_MANIFEST_CONTRACT",
    "GENERATION_CONFIG",
    "GENERATION_CONFIG_ID",
    "GENERATOR_CHECKPOINT_CONTRACT",
    "GENERATOR_STAGE",
    "PLANES",
    "PROPOSAL_CONTRACT",
    "REGISTRATION_STATUS",
    "CryptoTenSymbolHypothesisGeneratorError",
    "expand_candidates",
    "main",
    "run_ten_symbol_hypothesis_generation_once",
    "ten_symbol_hypothesis_generator_exit_code",
]
