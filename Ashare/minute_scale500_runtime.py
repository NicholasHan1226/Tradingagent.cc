"""Fail-closed scale-500/rolling selector for the A-share delayed-paper runtime.

The selector owns only an isolated simulation state root. The default mode
verifies one frozen 3193-symbol universe for a full-universe claim; the explicit
``rolling_eligible`` mode derives the current active partition and keeps recent
listings pending without blocking other symbols. Both modes delegate all market
reads to the existing formal TradingDatas catalog/query clients and normally
require the first two accepted bars to be the adjacent 09:35 and 09:40 session
observations. An explicitly armed, isolated late start may accept only the
runner's exact current completed bar; it remains partial-session and permanently
ineligible for learning. The selector never reads or writes the rollback-30 state
root. A failure selects the rollback configuration and returns a stable reason
code for systemd's rollback unit.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from .minute_auto_runner import (
    expected_available_bar_end,
    run_current_delayed_minute_paper,
    session_bar_ends,
)
from .minute_data import MAX_DELAYED_PAPER_LATENCY, SHANGHAI
from .minute_paper_runner import load_minute_research_universe
from .minute_session_initializer import (
    SCALE500_COHORT_COUNT,
    SCALE500_COHORT_SIZE,
    SCALE500_REFERENCE_KEY,
    _timeout_seconds_from_environment,
    build_scale500_reference_envelope,
    initialize_minute_session,
    partition_rolling_universe,
)
from shared.data.tradingdatas_transport import TradingDatasAuthenticationError
from shared.governance.evidence_readiness import load_evidence_readiness_contract


EXPECTED_UNIVERSE_COUNT = 3193
FORMAL_BASE_URL = "http://127.0.0.1:18082"
MINUTE_DATASET_ID = "cn.dataset.rt_min"
GATE_SCHEMA = "tradingagent.ashare.scale500-acceptance.v1"
PARTIAL_SHADOW_SCHEMA = "tradingagent.ashare.scale500-partial-shadow.v1"
GATE_DIRECTORY = ".scale500-gates"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_PATTERN = re.compile(r"^[a-z0-9_.:-]+$")

Initializer = Callable[..., dict[str, object]]
Runner = Callable[..., dict[str, object]]


class MinuteScale500RuntimeError(ValueError):
    """Fail-closed scale transition error with a stable reason code."""


def _shadow_policy() -> tuple[int, int, str]:
    """Return the catalog-independent cohort policy frozen in governance."""

    try:
        contract = load_evidence_readiness_contract()
        policy = contract.market_policies["ashare"]["cohort_shadow"]
    except (KeyError, ValueError) as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_readiness_contract_invalid"
        ) from exc
    if not isinstance(policy, Mapping):
        raise MinuteScale500RuntimeError("minute_scale500_readiness_contract_invalid")
    target = policy.get("target_size")
    ratio = policy.get("minimum_coverage_ratio")
    if (
        isinstance(target, bool)
        or not isinstance(target, int)
        or target != EXPECTED_UNIVERSE_COUNT
        or isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or ratio <= 0
        or ratio > 1
        or policy.get("full_cohort_required_for_delayed_paper") is not True
        or policy.get("partial_shadow_allowed") is not True
        or policy.get("explicit_missing_identity_set_required") is not True
        or policy.get("silent_replacement_allowed") is not False
        or policy.get("simulated_notional_allowed") is not False
    ):
        raise MinuteScale500RuntimeError("minute_scale500_readiness_contract_invalid")
    minimum = int(target * ratio)
    if minimum * 1.0 < target * ratio:
        minimum += 1
    return target, minimum, contract.contract_id


def _strict_symbols(value: object, reason: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise MinuteScale500RuntimeError(reason)
    symbols = tuple(value)
    if any(
        not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip()
        for symbol in symbols
    ):
        raise MinuteScale500RuntimeError(reason)
    if len(symbols) != len(set(symbols)):
        raise MinuteScale500RuntimeError(reason)
    return symbols


def build_scale500_partial_shadow_receipt(
    *,
    expected_symbols: tuple[str, ...],
    observed_symbols: tuple[str, ...],
    trading_date: str,
    bar_end: str,
    observed_at: datetime,
    decision_time: datetime,
) -> dict[str, object]:
    """Build an idempotent zero-notional receipt for a 99%-only cohort.

    This is deliberately not a runner and never creates candidates, capital
    state, or a delayed-paper order.  A full 500 cohort remains the sole path
    to ``run_scale500_once``.
    """

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteScale500RuntimeError("real_trading_must_remain_disabled")
    target, minimum, contract_id = _shadow_policy()
    expected = _strict_symbols(
        expected_symbols, "minute_scale500_expected_identity_invalid"
    )
    observed = _strict_symbols(
        observed_symbols, "minute_scale500_observed_identity_invalid"
    )
    if len(expected) != target:
        raise MinuteScale500RuntimeError("minute_scale500_expected_identity_invalid")
    if not isinstance(trading_date, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", trading_date
    ):
        raise MinuteScale500RuntimeError("minute_scale500_shadow_trade_date_invalid")
    if not isinstance(bar_end, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", bar_end
    ):
        raise MinuteScale500RuntimeError("minute_scale500_shadow_bar_end_invalid")
    try:
        completed_bar_end = datetime.strptime(bar_end, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=SHANGHAI
        )
    except ValueError as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_shadow_bar_end_invalid"
        ) from exc
    if (
        observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or decision_time.tzinfo is None
        or decision_time.utcoffset() is None
        or observed_at > decision_time
        or observed_at <= completed_bar_end
        or observed_at - completed_bar_end > MAX_DELAYED_PAPER_LATENCY
        or decision_time - completed_bar_end > MAX_DELAYED_PAPER_LATENCY
        or completed_bar_end.date().isoformat() != trading_date
    ):
        raise MinuteScale500RuntimeError("minute_scale500_shadow_time_invalid")
    expected_set = frozenset(expected)
    observed_set = frozenset(observed)
    if not observed_set <= expected_set:
        raise MinuteScale500RuntimeError("minute_scale500_silent_identity_replacement")
    if len(observed) == target:
        raise MinuteScale500RuntimeError(
            "minute_scale500_shadow_requires_partial_cohort"
        )
    if len(observed) < minimum:
        raise MinuteScale500RuntimeError("minute_scale500_shadow_coverage_insufficient")
    missing = tuple(sorted(expected_set - observed_set))
    receipt_id = hashlib.sha256(
        _canonical_json(
            {
                "contract_id": contract_id,
                "trading_date": trading_date,
                "bar_end": bar_end,
                "observed_at": observed_at.isoformat(),
                "expected": sorted(expected),
                "observed": sorted(observed),
            }
        )
    ).hexdigest()
    return {
        "schema": PARTIAL_SHADOW_SCHEMA,
        "receipt_id": receipt_id,
        "status": "partial_cohort_shadow",
        "readiness_contract_id": contract_id,
        "trading_date": trading_date,
        "bar_end": bar_end,
        "observed_at": observed_at.isoformat(),
        "decision_time": decision_time.isoformat(),
        "target_cohort_size": target,
        "observed_cohort_size": len(observed),
        "minimum_shadow_cohort_size": minimum,
        "missing_identity_set": list(missing),
        "missing_identity_count": len(missing),
        "silent_replacement_detected": False,
        "delayed_paper_eligible": False,
        "simulation_timing": "next_bar_only",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "candidate_authority": False,
        "capital_authority": False,
        "execution_authority": False,
        "execution_latency_eligible": False,
        "execution_eligible": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
        "simulated_notional_cny": 0,
        "training_eligible": False,
        "promotion_authorized": False,
        "real_trading_enabled": False,
    }


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_universe_not_canonical"
        ) from exc


def _load_json(path: Path, reason: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteScale500RuntimeError(reason) from exc


def canonical_universe_sha256(path: Path | str) -> str:
    """Return the initializer-compatible canonical digest for a universe."""

    source = Path(path)
    raw = _load_json(source, "minute_scale500_universe_invalid")
    return hashlib.sha256(_canonical_json(raw)).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_paths(
    *,
    scale_state_root: Path | str,
    rollback30_state_root: Path | str,
    token_file: Path | str,
    universe_source: Path | str,
) -> tuple[Path, Path, Path, Path]:
    scale = Path(scale_state_root)
    rollback = Path(rollback30_state_root)
    token = Path(token_file)
    universe = Path(universe_source)
    if not all(path.is_absolute() for path in (scale, rollback, token, universe)):
        raise MinuteScale500RuntimeError("minute_scale500_paths_must_be_absolute")
    scale_resolved = scale.resolve(strict=False)
    rollback_resolved = rollback.resolve(strict=False)
    if (
        scale_resolved == rollback_resolved
        or _is_within(scale_resolved, rollback_resolved)
        or _is_within(rollback_resolved, scale_resolved)
    ):
        raise MinuteScale500RuntimeError("minute_scale500_state_roots_not_independent")
    if rollback.is_symlink() or not rollback.is_dir():
        raise MinuteScale500RuntimeError("minute_scale500_rollback30_root_invalid")
    if scale.exists() and (scale.is_symlink() or not scale.is_dir()):
        raise MinuteScale500RuntimeError("minute_scale500_state_root_invalid")
    return scale, rollback, token, universe


def _validated_universe(
    source: Path,
    *,
    expected_sha256: str,
) -> tuple[list[Mapping[str, Any]], str]:
    if (
        not _SHA256_PATTERN.fullmatch(expected_sha256)
        or source.is_symlink()
        or not source.is_file()
    ):
        raise MinuteScale500RuntimeError("minute_scale500_universe_source_invalid")
    try:
        stat = source.stat()
    except OSError as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_universe_source_invalid"
        ) from exc
    if stat.st_nlink != 1 or stat.st_mode & 0o022 or os.access(source, os.W_OK):
        raise MinuteScale500RuntimeError("minute_scale500_universe_source_invalid")
    raw = _load_json(source, "minute_scale500_universe_invalid")
    if not isinstance(raw, list) or len(raw) != EXPECTED_UNIVERSE_COUNT:
        raise MinuteScale500RuntimeError("minute_scale500_universe_count_mismatch")
    rows: list[Mapping[str, Any]] = []
    symbols: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise MinuteScale500RuntimeError("minute_scale500_universe_invalid")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
            raise MinuteScale500RuntimeError("minute_scale500_universe_invalid")
        rows.append(item)
        symbols.append(symbol)
    if len(set(symbols)) != EXPECTED_UNIVERSE_COUNT:
        raise MinuteScale500RuntimeError("minute_scale500_universe_duplicate")
    actual_sha256 = hashlib.sha256(_canonical_json(raw)).hexdigest()
    if actual_sha256 != expected_sha256:
        raise MinuteScale500RuntimeError("minute_scale500_universe_digest_mismatch")
    try:
        universe = load_minute_research_universe(source)
    except ValueError as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_universe_policy_invalid"
        ) from exc
    if len(universe.instruments) != EXPECTED_UNIVERSE_COUNT:
        raise MinuteScale500RuntimeError("minute_scale500_universe_count_mismatch")
    return rows, actual_sha256


def _rolling_effective_universe(
    source: Path,
    *,
    trade_date: str,
) -> tuple[list[Mapping[str, Any]], str]:
    """Derive the current rolling active set from the reviewed source snapshot."""

    raw = _load_json(source, "minute_scale500_universe_invalid")
    if not isinstance(raw, list):
        raise MinuteScale500RuntimeError("minute_scale500_universe_invalid")
    try:
        target = datetime.strptime(trade_date, "%Y-%m-%d").date()
        universe = load_minute_research_universe(source)
        active, _ = partition_rolling_universe(
            universe_raw=raw,
            universe=universe,
            trade_date=target,
        )
    except (TypeError, ValueError) as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_rolling_universe_invalid"
        ) from exc
    return active, hashlib.sha256(_canonical_json(active)).hexdigest()


def _parse_bar_end_any(value: str) -> datetime:
    """Parse a bar end in either canonical space or ISO-8601 serialization."""

    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _validate_scale500_reference_fragment(
    value: object,
    *,
    universe_symbols: frozenset[str],
    universe_sha256: str,
    trading_date: str,
    expected_bar_end: str | None,
) -> None:
    if not isinstance(value, Mapping):
        raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
    if (
        value.get("universe_sha256") != universe_sha256
        or value.get("max_rows") != EXPECTED_UNIVERSE_COUNT
        or value.get("row_count") != EXPECTED_UNIVERSE_COUNT
        or value.get("cohort_count") != SCALE500_COHORT_COUNT
        or value.get("cohort_size") != SCALE500_COHORT_SIZE
    ):
        raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
    raw_target = value.get("target_bar_end")
    if not isinstance(raw_target, str):
        raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
    try:
        target = _parse_bar_end_any(raw_target)
    except ValueError as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_reference_bundle_invalid"
        ) from exc
    if (
        target.date().isoformat() != trading_date
        or target.strftime("%Y-%m-%d %H:%M:%S") != raw_target
        or expected_bar_end is not None
        and raw_target != expected_bar_end
    ):
        raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
    cohorts = value.get("cohorts")
    if not isinstance(cohorts, list) or len(cohorts) != SCALE500_COHORT_COUNT:
        raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
    seen: set[str] = set()
    for index, cohort in enumerate(cohorts):
        if not isinstance(cohort, Mapping):
            raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
        symbols = cohort.get("symbols")
        if (
            cohort.get("cohort_id") != f"scale500-{index:03d}"
            or cohort.get("row_count") != SCALE500_COHORT_SIZE
            or cohort.get("bar_end") != raw_target
            or not isinstance(symbols, list)
            or len(symbols) != SCALE500_COHORT_SIZE
            or any(not isinstance(symbol, str) for symbol in symbols)
            or len(set(symbols)) != SCALE500_COHORT_SIZE
            or seen.intersection(symbols)
        ):
            raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
        seen.update(symbols)
        for field in (
            "receipt_id",
            "source_lineage_sha256",
            "snapshot_sha256",
        ):
            field_value = cohort.get(field)
            if not isinstance(field_value, str) or not field_value.strip():
                raise MinuteScale500RuntimeError(
                    "minute_scale500_reference_bundle_invalid"
                )
        replay = cohort.get("replay")
        if (
            not isinstance(replay, Mapping)
            or replay.get("same_observation") is not True
            or any(
                not isinstance(replay.get(name), str)
                or not _SHA256_PATTERN.fullmatch(replay[name])
                for name in (
                    "pagination_trace_sha256",
                    "first_semantic_sha256",
                    "replay_semantic_sha256",
                )
            )
        ):
            raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")
    if seen != set(universe_symbols):
        raise MinuteScale500RuntimeError("minute_scale500_reference_bundle_invalid")


def _validate_late_start_canary(
    *,
    canary_receipt: Path | str | None,
    expected_symbols: frozenset[str],
    trading_date: str,
    bar_end: str,
) -> None:
    """Bind a manual scale start to one prior exact delayed-paper canary."""

    if canary_receipt is None:
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_missing")
    path = Path(canary_receipt)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_invalid")
    raw = _load_json(path, "minute_scale500_late_start_canary_invalid")
    if not isinstance(raw, Mapping):
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_invalid")
    if (
        raw.get("status") != "pass"
        or raw.get("authority_tier") != "observation_only"
        or raw.get("evidence_use") != "delayed_paper"
        or raw.get("execution_latency_eligible") is not False
        or raw.get("real_trading_enabled") is not False
        or raw.get("trading_date") != trading_date
        or raw.get("row_count") != EXPECTED_UNIVERSE_COUNT
        or raw.get("same_observation") is not True
        or raw.get("lineage_complete") is not True
        or raw.get("audit_rejections") != 0
        or not isinstance(raw.get("dataset_contract_fingerprint"), str)
        or not _SHA256_PATTERN.fullmatch(raw["dataset_contract_fingerprint"])
        or not isinstance(raw.get("consumer_profile_sha256"), str)
        or not _SHA256_PATTERN.fullmatch(raw["consumer_profile_sha256"])
        or not isinstance(raw.get("snapshot_sha256"), str)
        or not _SHA256_PATTERN.fullmatch(raw["snapshot_sha256"])
    ):
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_invalid")
    decision_time = raw.get("decision_time")
    if not isinstance(decision_time, str):
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_invalid")
    try:
        decision = datetime.fromisoformat(decision_time)
    except ValueError as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_late_start_canary_invalid"
        ) from exc
    if decision.tzinfo is None or decision.utcoffset() is None:
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_invalid")
    bars = raw.get("bars")
    if not isinstance(bars, list) or len(bars) != EXPECTED_UNIVERSE_COUNT:
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_invalid")
    try:
        expected_bar_end = _parse_bar_end_any(bar_end)
    except ValueError as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_late_start_canary_invalid"
        ) from exc
    symbols: list[str] = []
    for item in bars:
        if not isinstance(item, Mapping):
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_canary_invalid"
            )
        symbol = item.get("symbol")
        raw_bar_end = item.get("bar_end")
        try:
            item_bar_end = (
                _parse_bar_end_any(raw_bar_end)
                if isinstance(raw_bar_end, str)
                else None
            )
        except ValueError as exc:
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_canary_invalid"
            ) from exc
        if (
            not isinstance(symbol, str)
            or item_bar_end != expected_bar_end
            or not isinstance(item.get("receipt_id"), str)
            or not item["receipt_id"].strip()
            or not isinstance(item.get("observed_at"), str)
        ):
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_canary_invalid"
            )
        try:
            observed = datetime.fromisoformat(item["observed_at"])
        except ValueError as exc:
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_canary_invalid"
            ) from exc
        if (
            observed.tzinfo is None
            or observed.utcoffset() is None
            or observed > decision
        ):
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_canary_invalid"
            )
        symbols.append(symbol)
    if frozenset(symbols) != expected_symbols or len(symbols) != len(set(symbols)):
        raise MinuteScale500RuntimeError("minute_scale500_late_start_canary_invalid")


def _gate_path(scale_root: Path, trading_date: str) -> Path:
    return scale_root / GATE_DIRECTORY / f"{trading_date.replace('-', '')}.json"


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise MinuteScale500RuntimeError("minute_scale500_gate_persist_failed") from exc


def _new_gate(
    *,
    trading_date: str,
    rollback_root: Path,
    universe_sha256: str,
    expected_universe_count: int = EXPECTED_UNIVERSE_COUNT,
    selected_mode: str = "scale500",
) -> dict[str, object]:
    return {
        "schema": GATE_SCHEMA,
        "trading_date": trading_date,
        "status": "pending_two_live_snapshots",
        "selected_mode": selected_mode,
        "expected_universe_count": expected_universe_count,
        "universe_sha256": universe_sha256,
        "validated_bar_ends": [],
        "partial_session": False,
        "late_start": False,
        "late_start_bar_end": None,
        "failure_reason": None,
        "rollback30_state_root": str(rollback_root),
        "capital_layer": "simulated",
        "account_type": "simulated",
        "capital_authority": False,
        "execution_authority": False,
        "execution_eligible": False,
        "training_eligible": False,
        "promotion_authorized": False,
        "real_trading_enabled": False,
    }


def _load_gate(
    path: Path,
    *,
    trading_date: str,
    rollback_root: Path,
    universe_sha256: str,
    expected_universe_count: int = EXPECTED_UNIVERSE_COUNT,
    selected_mode: str = "scale500",
) -> dict[str, object]:
    raw = _load_json(path, "minute_scale500_gate_missing_or_invalid")
    if not isinstance(raw, Mapping):
        raise MinuteScale500RuntimeError("minute_scale500_gate_missing_or_invalid")
    gate = {
        "partial_session": False,
        "late_start": False,
        "late_start_bar_end": None,
        **dict(raw),
    }
    validated = gate.get("validated_bar_ends")
    if (
        gate.get("schema") != GATE_SCHEMA
        or gate.get("trading_date") != trading_date
        or gate.get("universe_sha256") != universe_sha256
        or gate.get("expected_universe_count") != expected_universe_count
        or gate.get("rollback30_state_root") != str(rollback_root)
        or gate.get("capital_layer") != "simulated"
        or gate.get("account_type") != "simulated"
        or gate.get("capital_authority") is not False
        or gate.get("execution_authority") is not False
        or gate.get("execution_eligible") is not False
        or gate.get("training_eligible") is not False
        or gate.get("promotion_authorized") is not False
        or gate.get("real_trading_enabled") is not False
        or gate.get("status")
        not in {
            "pending_two_live_snapshots",
            "active",
            "fallback30_selected",
        }
        or gate.get("selected_mode") not in {
            "scale500",
            "rolling_eligible",
            "rollback30",
        }
        or (
            gate.get("status") == "fallback30_selected"
            and gate.get("selected_mode") != "rollback30"
        )
        or (
            gate.get("status") != "fallback30_selected"
            and gate.get("selected_mode") != selected_mode
        )
        or not isinstance(validated, list)
        or len(validated) > 2
        or any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in validated
        )
        or not isinstance(gate.get("partial_session"), bool)
        or not isinstance(gate.get("late_start"), bool)
        or (
            gate.get("late_start_bar_end") is not None
            and (
                not isinstance(gate.get("late_start_bar_end"), str)
                or not gate["late_start_bar_end"]
                or gate["late_start_bar_end"] != gate["late_start_bar_end"].strip()
            )
        )
        or gate.get("partial_session") != gate.get("late_start")
        or (
            gate.get("late_start") is False
            and gate.get("late_start_bar_end") is not None
        )
        or (
            gate.get("late_start") is True
            and (
                gate.get("status") not in {"active", "fallback30_selected"}
                or len(validated) != 1
                or gate.get("late_start_bar_end") != validated[0]
            )
        )
    ):
        raise MinuteScale500RuntimeError("minute_scale500_gate_missing_or_invalid")
    return gate


def _reason_code(exc: BaseException) -> str:
    if isinstance(exc, TradingDatasAuthenticationError):
        return "minute_scale500_tradingdatas_authentication_rejected"
    value = str(exc).strip()
    if (
        value
        and _REASON_PATTERN.fullmatch(value)
        and value.startswith(("minute_", "real_trading_"))
    ):
        return value
    return f"minute_scale500_unclassified_{type(exc).__name__.lower()}"


def _select_rollback(
    *,
    scale_root: Path,
    trading_date: str,
    rollback_root: Path,
    universe_sha256: str,
    reason: str,
    expected_universe_count: int = EXPECTED_UNIVERSE_COUNT,
    selected_mode: str = "scale500",
) -> None:
    gate_path = _gate_path(scale_root, trading_date)
    try:
        gate = _load_gate(
            gate_path,
            trading_date=trading_date,
            rollback_root=rollback_root,
            universe_sha256=universe_sha256,
            expected_universe_count=expected_universe_count,
            selected_mode=selected_mode,
        )
    except MinuteScale500RuntimeError:
        gate = _new_gate(
            trading_date=trading_date,
            rollback_root=rollback_root,
            universe_sha256=universe_sha256,
        )
    gate.update(
        {
            "status": "fallback30_selected",
            "selected_mode": "rollback30",
            "failure_reason": reason,
        }
    )
    _atomic_write_json(gate_path, gate)


def _validate_published_session(
    *,
    scale_root: Path,
    trading_date: str,
    universe_sha256: str,
    require_no_state_bundle: bool,
    expected_bar_end: str | None = None,
    require_scale500_reference: bool = False,
    expected_universe_count: int = EXPECTED_UNIVERSE_COUNT,
) -> None:
    day_root = scale_root / trading_date.replace("-", "")
    manifest_path = day_root / "minute-manifest.json"
    references_path = day_root / "reference-facts.json"
    universe_path = day_root / "universe.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (manifest_path, references_path, universe_path)
    ):
        raise MinuteScale500RuntimeError("minute_scale500_session_inputs_missing")
    if require_no_state_bundle and (day_root / "state-bundle.json").exists():
        raise MinuteScale500RuntimeError(
            "minute_scale500_initializer_created_state_bundle"
        )
    manifest = _load_json(manifest_path, "minute_scale500_manifest_invalid")
    references = _load_json(references_path, "minute_scale500_references_invalid")
    universe = _load_json(universe_path, "minute_scale500_universe_invalid")
    if not isinstance(manifest, Mapping):
        raise MinuteScale500RuntimeError("minute_scale500_manifest_invalid")
    profile = manifest.get("profile")
    if (
        manifest.get("base_url") != FORMAL_BASE_URL
        or manifest.get("dataset_id") != MINUTE_DATASET_ID
        or manifest.get("universe_sha256") != universe_sha256
        or not isinstance(profile, Mapping)
        or profile.get("max_rows") != expected_universe_count
        or profile.get("page_limit") != expected_universe_count
        or isinstance(profile.get("max_pages"), bool)
        or not isinstance(profile.get("max_pages"), int)
        or profile.get("max_pages", 0) <= 0
    ):
        raise MinuteScale500RuntimeError("minute_scale500_manifest_invalid")
    if (
        not isinstance(universe, list)
        or len(universe) != expected_universe_count
        or hashlib.sha256(_canonical_json(universe)).hexdigest() != universe_sha256
    ):
        raise MinuteScale500RuntimeError("minute_scale500_universe_digest_mismatch")
    if require_scale500_reference or expected_bar_end is not None:
        _validate_scale500_reference_fragment(
            manifest.get(SCALE500_REFERENCE_KEY),
            universe_symbols=frozenset(
                row.get("symbol")
                for row in universe
                if isinstance(row, Mapping) and isinstance(row.get("symbol"), str)
            ),
            universe_sha256=universe_sha256,
            trading_date=trading_date,
            expected_bar_end=expected_bar_end,
        )
    expected_symbols = {
        row.get("symbol") for row in universe if isinstance(row, Mapping)
    }
    if (
        len(expected_symbols) != expected_universe_count
        or not isinstance(references, list)
        or len(references) != expected_universe_count
        or {row.get("symbol") for row in references if isinstance(row, Mapping)}
        != expected_symbols
    ):
        raise MinuteScale500RuntimeError("minute_scale500_reference_coverage_mismatch")


def initialize_scale500_session(
    *,
    scale_state_root: Path | str,
    rollback30_state_root: Path | str,
    token_file: Path | str,
    universe_source: Path | str,
    expected_universe_sha256: str,
    now: datetime,
    target_bar_end: str | None = None,
    scale500_cohort_receipts: tuple[Path | str, ...] | list[Path | str] | None = None,
    rolling_eligible: bool = False,
    initializer: Initializer = initialize_minute_session,
    timeout_seconds: float = 20.0,
) -> dict[str, object]:
    """Initialize the exact or rolling-eligible isolated simulation session."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteScale500RuntimeError("real_trading_must_remain_disabled")
    if now.tzinfo is None or now.utcoffset() is None:
        raise MinuteScale500RuntimeError("minute_scale500_now_must_be_aware")
    if not isinstance(rolling_eligible, bool):
        raise MinuteScale500RuntimeError("minute_scale500_rolling_mode_invalid")
    if rolling_eligible and (
        target_bar_end is not None or scale500_cohort_receipts is not None
    ):
        raise MinuteScale500RuntimeError(
            "minute_scale500_reference_requires_full_universe"
        )
    scale, rollback, token, universe_path = _validate_paths(
        scale_state_root=scale_state_root,
        rollback30_state_root=rollback30_state_root,
        token_file=token_file,
        universe_source=universe_source,
    )
    source_rows, source_universe_sha256 = _validated_universe(
        universe_path,
        expected_sha256=expected_universe_sha256,
    )
    trading_date = now.astimezone(SHANGHAI).date().isoformat()
    mode = "rolling_eligible" if rolling_eligible else "scale500"
    if rolling_eligible:
        effective_rows, effective_universe_sha256 = _rolling_effective_universe(
            universe_path,
            trade_date=trading_date,
        )
        expected_count = len(effective_rows)
    else:
        effective_universe_sha256 = source_universe_sha256
        expected_count = len(source_rows)
    try:
        result = initializer(
            state_root=scale,
            token_file=token,
            now=now,
            universe_source=universe_path,
            target_bar_end=target_bar_end,
            scale500_cohort_receipts=scale500_cohort_receipts,
            timeout_seconds=timeout_seconds,
            allow_pending_recent_listings=rolling_eligible,
        )
        if rolling_eligible:
            published_count = result.get("symbol_count")
            published_hash = result.get("universe_sha256")
            if (
                isinstance(published_count, bool)
                or not isinstance(published_count, int)
                or published_count <= 0
                or published_count > expected_count
                or not isinstance(published_hash, str)
                or not _SHA256_PATTERN.fullmatch(published_hash)
            ):
                raise MinuteScale500RuntimeError(
                    "minute_scale500_rolling_initializer_partition_invalid"
                )
            # Rolling initialization may isolate symbols whose daily
            # reference is unavailable.  Bind the gate to the exact
            # partition that was actually published, not the pre-query
            # membership snapshot.
            expected_count = published_count
            effective_universe_sha256 = published_hash
        if (
            result.get("status") != "pass"
            or result.get("trading_date") != trading_date
            or result.get("symbol_count") != expected_count
            or result.get("universe_sha256") != effective_universe_sha256
            or result.get("authority_tier") != "non_production_fixture"
            or not isinstance(result.get("state_bundle_created"), bool)
            or result.get("capital_authority") is not False
            or result.get("execution_authority") is not False
            or result.get("real_trading_enabled") is not False
            or (
                rolling_eligible
                and (
                    result.get("rolling_eligible") is not True
                    or result.get("source_universe_sha256")
                    != source_universe_sha256
                )
            )
            or (
                not rolling_eligible
                and result.get("rolling_eligible") is not False
            )
        ):
            raise MinuteScale500RuntimeError(
                "minute_scale500_initializer_receipt_invalid"
            )
        _validate_published_session(
            scale_root=scale,
            trading_date=trading_date,
            universe_sha256=effective_universe_sha256,
            require_no_state_bundle=True,
            expected_bar_end=target_bar_end,
            require_scale500_reference=(
                target_bar_end is not None and not rolling_eligible
            ),
            expected_universe_count=expected_count,
        )
        gate_path = _gate_path(scale, trading_date)
        if gate_path.exists():
            gate = _load_gate(
                gate_path,
                trading_date=trading_date,
                rollback_root=rollback,
                universe_sha256=effective_universe_sha256,
                expected_universe_count=expected_count,
                selected_mode=mode,
            )
            if gate["status"] == "fallback30_selected":
                raise MinuteScale500RuntimeError(
                    "minute_scale500_fallback_already_selected"
                )
        else:
            gate = _new_gate(
                trading_date=trading_date,
                rollback_root=rollback,
                universe_sha256=effective_universe_sha256,
                expected_universe_count=expected_count,
                selected_mode=mode,
            )
            _atomic_write_json(gate_path, gate)
    except Exception as exc:
        reason = _reason_code(exc)
        _select_rollback(
            scale_root=scale,
            trading_date=trading_date,
            rollback_root=rollback,
            universe_sha256=effective_universe_sha256,
            reason=reason,
            expected_universe_count=expected_count,
            selected_mode=mode,
        )
        raise MinuteScale500RuntimeError(reason) from exc
    return {
        **dict(result),
        "scale500_acceptance_status": gate["status"],
        "selected_mode": gate["selected_mode"],
        "rollback30_state_root_preserved": True,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "execution_eligible": False,
        "training_eligible": False,
        "promotion_authorized": False,
    }


def _validate_runtime_receipt(
    result: Mapping[str, object],
    *,
    expected_bar_end: str,
    allow_late_start: bool,
    expected_row_count: int = EXPECTED_UNIVERSE_COUNT,
) -> bool:
    if result.get("status") != "pass":
        raise MinuteScale500RuntimeError("minute_scale500_runtime_not_pass")
    if result.get("bar_end") != expected_bar_end:
        raise MinuteScale500RuntimeError("minute_scale500_bar_end_mismatch")
    if result.get("row_count") != expected_row_count:
        raise MinuteScale500RuntimeError("minute_scale500_row_count_mismatch")
    if result.get("audit_rejections") != 0:
        raise MinuteScale500RuntimeError("minute_scale500_audit_rejections")
    if (
        result.get("authority_tier") != "non_production_fixture"
        or result.get("capital_authority") is not False
        or result.get("durable_capital") is not False
        or result.get("execution_authority") is not False
        or result.get("real_trading_enabled") is not False
    ):
        raise MinuteScale500RuntimeError("minute_scale500_authority_violation")
    late_start = result.get("late_start") is True
    if late_start:
        if not allow_late_start:
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_not_authorized"
            )
        if (
            result.get("gap_recovery") is not True
            or result.get("late_start_reason") != "incident_recovery_no_historical_pit"
            or result.get("gap_recovery_reason")
            != "incident_recovery_no_historical_pit"
            or isinstance(result.get("skipped_session_slots"), bool)
            or not isinstance(result.get("skipped_session_slots"), int)
            or result["skipped_session_slots"] <= 0
            or result.get("full_session_complete") is not False
            or result.get("learning_eligible") is not False
        ):
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_receipt_invalid"
            )
        return True
    if result.get("gap_recovery") is True:
        raise MinuteScale500RuntimeError("minute_scale500_gap_or_late_start_forbidden")
    return False


def _partial_session_projection(gate: Mapping[str, object]) -> dict[str, object]:
    if gate.get("partial_session") is not True:
        return {}
    return {
        "partial_session": True,
        "late_start": True,
        "late_start_bar_end": gate["late_start_bar_end"],
        "learning_eligible": False,
        "full_session_complete": False,
    }


def _validated_partial_runtime_observation(
    result: Mapping[str, object],
    *,
    expected_symbols: frozenset[str],
    expected_bar_end: str,
) -> tuple[tuple[str, ...], datetime, datetime, list[dict[str, object]]]:
    target, minimum, _ = _shadow_policy()
    accepted = _strict_symbols(
        result.get("accepted_symbols"),
        "minute_scale500_partial_observation_invalid",
    )
    missing = _strict_symbols(
        result.get("missing_symbols"),
        "minute_scale500_partial_observation_invalid",
    )
    accepted_set = frozenset(accepted)
    missing_set = frozenset(missing)
    if (
        result.get("status") != "partial_observation"
        or result.get("bar_end") != expected_bar_end
        or result.get("requested_count") != target
        or result.get("accepted_count") != len(accepted)
        or result.get("missing_count") != len(missing)
        or not minimum <= len(accepted) < target
        or not accepted_set <= expected_symbols
        or accepted_set & missing_set
        or accepted_set | missing_set != expected_symbols
        or result.get("same_observation") is not True
        or result.get("lineage_complete") is not True
        or result.get("proof_complete") is not True
        or result.get("audit_rejections") != 0
        or any(
            result.get(field) is not False
            for field in (
                "capital_authority",
                "execution_authority",
                "execution_eligible",
                "training_eligible",
                "promotion_authorized",
                "real_trading_enabled",
            )
        )
    ):
        raise MinuteScale500RuntimeError(
            "minute_scale500_partial_observation_invalid"
        )
    try:
        observed_at = datetime.fromisoformat(str(result["observed_at"]))
        decision_time = datetime.fromisoformat(str(result["decision_time"]))
    except (KeyError, ValueError) as exc:
        raise MinuteScale500RuntimeError(
            "minute_scale500_partial_observation_invalid"
        ) from exc
    raw_rows = result.get("per_row_evidence")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(accepted):
        raise MinuteScale500RuntimeError(
            "minute_scale500_partial_observation_invalid"
        )
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    row_observed_times: list[datetime] = []
    expected_bar_time = _parse_bar_end_any(expected_bar_end)
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise MinuteScale500RuntimeError(
                "minute_scale500_partial_observation_invalid"
            )
        row = dict(raw)
        symbol = row.get("symbol")
        if (
            not isinstance(symbol, str)
            or symbol not in accepted_set
            or symbol in seen
            or row.get("bar_end")
            not in {expected_bar_end, expected_bar_time.isoformat()}
            or not isinstance(row.get("receipt_id"), str)
            or not row["receipt_id"].strip()
            or any(
                not isinstance(row.get(field), str)
                or not _SHA256_PATTERN.fullmatch(row[field])
                for field in (
                    "source_lineage_sha256",
                    "envelope_proof_sha256",
                    "source_row_sha256",
                )
            )
        ):
            raise MinuteScale500RuntimeError(
                "minute_scale500_partial_observation_invalid"
            )
        try:
            data_through = datetime.fromisoformat(str(row["data_through"]))
            row_observed = datetime.fromisoformat(str(row["observed_at"]))
        except (KeyError, ValueError) as exc:
            raise MinuteScale500RuntimeError(
                "minute_scale500_partial_observation_invalid"
            ) from exc
        if (
            data_through.tzinfo is None
            or data_through.utcoffset() is None
            or row_observed.tzinfo is None
            or row_observed.utcoffset() is None
            or not expected_bar_time <= data_through <= row_observed <= decision_time
        ):
            raise MinuteScale500RuntimeError(
                "minute_scale500_partial_observation_invalid"
            )
        seen.add(symbol)
        row_observed_times.append(row_observed)
        rows.append(row)
    if seen != accepted_set or observed_at != max(row_observed_times):
        raise MinuteScale500RuntimeError(
            "minute_scale500_partial_observation_invalid"
        )
    return accepted, observed_at, decision_time, rows


def _partial_cohort_failure(
    gate: Mapping[str, object], reason: object
) -> dict[str, object]:
    stable_reason = (
        reason
        if isinstance(reason, str) and _REASON_PATTERN.fullmatch(reason)
        else "minute_scale500_partial_observation_invalid"
    )
    return {
        "status": "failed_closed",
        "reason_code": stable_reason,
        "cohort_failed": True,
        "selected_mode": "scale500",
        "scale500_acceptance_status": gate["status"],
        "rollback30_state_root_preserved": True,
        "quality_status": "unusable_for_capability",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "capital_authority": False,
        "execution_authority": False,
        "execution_eligible": False,
        "training_eligible": False,
        "promotion_authorized": False,
        "real_trading_enabled": False,
    }


def run_scale500_once(
    *,
    scale_state_root: Path | str,
    rollback30_state_root: Path | str,
    token_file: Path | str,
    universe_source: Path | str,
    expected_universe_sha256: str,
    now: datetime,
    target_bar_end: str | None = None,
    allow_late_start: bool = False,
    canary_receipt: Path | str | None = None,
    rolling_eligible: bool = False,
    runner: Runner = run_current_delayed_minute_paper,
) -> dict[str, object]:
    """Run one exact or rolling-eligible delayed-paper step."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteScale500RuntimeError("real_trading_must_remain_disabled")
    if now.tzinfo is None or now.utcoffset() is None:
        raise MinuteScale500RuntimeError("minute_scale500_now_must_be_aware")
    if not isinstance(allow_late_start, bool):
        raise MinuteScale500RuntimeError("minute_scale500_late_start_flag_invalid")
    if not isinstance(rolling_eligible, bool):
        raise MinuteScale500RuntimeError("minute_scale500_rolling_mode_invalid")
    if rolling_eligible and allow_late_start:
        raise MinuteScale500RuntimeError(
            "minute_scale500_rolling_late_start_forbidden"
        )
    if rolling_eligible and target_bar_end is not None:
        raise MinuteScale500RuntimeError(
            "minute_scale500_reference_requires_full_universe"
        )
    scale, rollback, token, universe_path = _validate_paths(
        scale_state_root=scale_state_root,
        rollback30_state_root=rollback30_state_root,
        token_file=token_file,
        universe_source=universe_source,
    )
    universe_rows, source_universe_sha256 = _validated_universe(
        universe_path,
        expected_sha256=expected_universe_sha256,
    )
    mode = "rolling_eligible" if rolling_eligible else "scale500"
    target = expected_available_bar_end(now)
    if target is None:
        return {
            "status": "noop",
            "reason": "outside_delayed_session_window",
            "selected_mode": mode,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "execution_eligible": False,
            "training_eligible": False,
            "promotion_authorized": False,
            "real_trading_enabled": False,
        }
    trading_date = target.astimezone(SHANGHAI).date().isoformat()
    expected_target = target.strftime("%Y-%m-%d %H:%M:%S")
    if rolling_eligible:
        effective_rows, effective_universe_sha256 = _rolling_effective_universe(
            universe_path,
            trade_date=trading_date,
        )
        expected_count = len(effective_rows)
        expected_symbols = frozenset(
            str(row["symbol"]) for row in effective_rows
        )
    else:
        effective_universe_sha256 = source_universe_sha256
        expected_count = len(universe_rows)
        expected_symbols = frozenset(str(row["symbol"]) for row in universe_rows)
    if target_bar_end is not None:
        try:
            supplied_target = _parse_bar_end_any(target_bar_end).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError as exc:
            raise MinuteScale500RuntimeError(
                "minute_scale500_target_bar_end_invalid"
            ) from exc
        if supplied_target != expected_target:
            raise MinuteScale500RuntimeError("minute_scale500_target_bar_end_mismatch")
    gate_path = _gate_path(scale, trading_date)
    try:
        gate = _load_gate(
            gate_path,
            trading_date=trading_date,
            rollback_root=rollback,
            universe_sha256=effective_universe_sha256,
            expected_universe_count=expected_count,
            selected_mode=mode,
        )
        if gate["status"] == "fallback30_selected":
            return {
                "status": "noop",
                "reason": "rollback30_selected",
                "selected_mode": "rollback30",
                "failure_reason": gate["failure_reason"],
                "rollback30_state_root_preserved": True,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "execution_eligible": False,
                "training_eligible": False,
                "promotion_authorized": False,
                "real_trading_enabled": False,
            }
        if allow_late_start:
            _validate_late_start_canary(
                canary_receipt=canary_receipt,
                expected_symbols=expected_symbols,
                trading_date=trading_date,
                bar_end=target.strftime("%Y-%m-%d %H:%M:%S"),
            )
        _validate_published_session(
            scale_root=scale,
            trading_date=trading_date,
            universe_sha256=effective_universe_sha256,
            require_no_state_bundle=False,
            expected_bar_end=(target_bar_end if not rolling_eligible else None),
            require_scale500_reference=(
                target_bar_end is not None and not rolling_eligible
            ),
            expected_universe_count=expected_count,
        )
        _, partial_minimum, _ = _shadow_policy()
        result = runner(
            state_root=scale,
            token_file=token,
            now=now,
            allow_late_start=allow_late_start,
            pin_universe_filter=True,
            partial_observation_minimum=(
                None if rolling_eligible else partial_minimum
            ),
        )
        if result.get("status") == "partial_observation_failed_closed":
            return _partial_cohort_failure(gate, result.get("reason_code"))
        if result.get("status") == "partial_observation":
            if rolling_eligible:
                raise MinuteScale500RuntimeError(
                    "minute_scale500_rolling_observation_partial"
                )
            try:
                expected_bar_end = target.strftime("%Y-%m-%d %H:%M:%S")
                accepted, observed_at, decision_time, evidence_rows = (
                    _validated_partial_runtime_observation(
                        result,
                        expected_symbols=expected_symbols,
                        expected_bar_end=expected_bar_end,
                    )
                )
                shadow = build_scale500_partial_shadow_receipt(
                    expected_symbols=tuple(sorted(expected_symbols)),
                    observed_symbols=accepted,
                    trading_date=trading_date,
                    bar_end=expected_bar_end,
                    observed_at=observed_at,
                    decision_time=decision_time,
                )
                shadow.update(
                    {
                        "quality_status": "usable_degraded",
                        "requested_count": result["requested_count"],
                        "accepted_count": result["accepted_count"],
                        "missing_count": result["missing_count"],
                        "accepted_symbols": list(accepted),
                        "proof_complete": True,
                        "lineage_complete": True,
                        "per_row_evidence": evidence_rows,
                    }
                )
                receipt_path = (
                    scale
                    / trading_date.replace("-", "")
                    / "partial-shadow-receipts"
                    / f"{target.strftime('%H%M%S')}.json"
                )
                _atomic_write_json(receipt_path, shadow)
            except MinuteScale500RuntimeError as exc:
                return _partial_cohort_failure(gate, str(exc))
            return {
                **shadow,
                "status": "partial_cohort_shadow",
                "scale500_acceptance_status": gate["status"],
                "selected_mode": "scale500",
                "rollback30_state_root_preserved": True,
            }
        if result.get("status") == "noop":
            if result.get("reason") != "bar_already_processed":
                raise MinuteScale500RuntimeError(
                    "minute_scale500_unexpected_runtime_noop"
                )
            return {
                **dict(result),
                "scale500_acceptance_status": gate["status"],
                "selected_mode": "scale500",
                "rollback30_state_root_preserved": True,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "execution_eligible": False,
                "training_eligible": False,
                "promotion_authorized": False,
                **_partial_session_projection(gate),
            }
        expected_bar_end = target.strftime("%Y-%m-%d %H:%M:%S")
        late_start = _validate_runtime_receipt(
            result,
            expected_bar_end=expected_bar_end,
            allow_late_start=allow_late_start,
            expected_row_count=expected_count,
        )
        validated = list(gate["validated_bar_ends"])
        if late_start and (gate["status"] != "pending_two_live_snapshots" or validated):
            raise MinuteScale500RuntimeError(
                "minute_scale500_late_start_gate_not_pending"
            )
        if gate["status"] == "pending_two_live_snapshots":
            if late_start:
                if validated:
                    raise MinuteScale500RuntimeError(
                        "minute_scale500_gate_missing_or_invalid"
                    )
                gate["status"] = "active"
                gate["validated_bar_ends"] = [expected_bar_end]
                gate["partial_session"] = True
                gate["late_start"] = True
                gate["late_start_bar_end"] = expected_bar_end
            else:
                first_two = session_bar_ends(target.date())[:2]
                expected_index = len(validated)
                if expected_index >= 2:
                    raise MinuteScale500RuntimeError(
                        "minute_scale500_gate_missing_or_invalid"
                    )
                expected_acceptance_bar = first_two[expected_index].strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if expected_bar_end != expected_acceptance_bar:
                    reason = (
                        "minute_scale500_first_bar_mismatch"
                        if expected_index == 0
                        else "minute_scale500_second_bar_mismatch"
                    )
                    raise MinuteScale500RuntimeError(reason)
                validated.append(expected_bar_end)
                gate["validated_bar_ends"] = validated
                if len(validated) == 2:
                    gate["status"] = "active"
            _atomic_write_json(gate_path, gate)
    except Exception as exc:
        reason = _reason_code(exc)
        _select_rollback(
            scale_root=scale,
            trading_date=trading_date,
            rollback_root=rollback,
            universe_sha256=effective_universe_sha256,
            reason=reason,
            expected_universe_count=expected_count,
            selected_mode=mode,
        )
        raise MinuteScale500RuntimeError(reason) from exc
    return {
        **dict(result),
        "scale500_acceptance_status": gate["status"],
        "selected_mode": mode,
        "validated_bar_ends": list(gate["validated_bar_ends"]),
        "rollback30_state_root_preserved": True,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "execution_eligible": False,
        "training_eligible": False,
        "promotion_authorized": False,
        **_partial_session_projection(gate),
    }


def _failure_payload(reason: str) -> dict[str, object]:
    return {
        "status": "failed_closed",
        "reason_code": reason,
        "selected_mode": "rollback30",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "capital_authority": False,
        "execution_authority": False,
        "execution_eligible": False,
        "training_eligible": False,
        "promotion_authorized": False,
        "real_trading_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the isolated A-share scale-500 acceptance selector"
    )
    parser.add_argument("command", choices=("initialize", "run"))
    parser.add_argument("--scale-state-root", type=Path, required=True)
    parser.add_argument("--rollback30-state-root", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--universe-source", type=Path, required=True)
    parser.add_argument("--expected-universe-sha256", required=True)
    parser.add_argument(
        "--canary-receipt",
        type=Path,
        help="Required secret-free delayed-paper canary receipt for --allow-late-start.",
    )
    parser.add_argument(
        "--target-bar-end",
        help="Exact bar_end bound by the initializer's five-cohort reference envelope",
    )
    parser.add_argument(
        "--scale500-cohort-receipt",
        action="append",
        type=Path,
        dest="scale500_cohort_receipts",
        help="One of exactly five existing 100-symbol canary receipts for initialize",
    )
    parser.add_argument(
        "--allow-late-start",
        action="store_true",
        help="Permit one partial-session run from the exact current completed bar.",
    )
    parser.add_argument(
        "--rolling-eligible",
        action="store_true",
        help=(
            "Run the current eligible partition; recent listings remain pending "
            "and do not block the active partition."
        ),
    )
    parser.add_argument("--now", help="Explicit aware ISO timestamp for tests")
    args = parser.parse_args(argv)
    try:
        now = (
            datetime.now(tz=SHANGHAI)
            if args.now is None
            else datetime.fromisoformat(args.now)
        )
        kwargs = {
            "scale_state_root": args.scale_state_root,
            "rollback30_state_root": args.rollback30_state_root,
            "token_file": args.token_file,
            "universe_source": args.universe_source,
            "expected_universe_sha256": args.expected_universe_sha256,
            "now": now,
        }
        if args.command == "initialize":
            if args.allow_late_start:
                raise MinuteScale500RuntimeError("minute_scale500_late_start_run_only")
            configured_timeout = _timeout_seconds_from_environment()
            result = initialize_scale500_session(
                **kwargs,
                target_bar_end=args.target_bar_end,
                scale500_cohort_receipts=(
                    tuple(args.scale500_cohort_receipts)
                    if args.scale500_cohort_receipts is not None
                    else None
                ),
                timeout_seconds=(
                    configured_timeout if configured_timeout is not None else 20.0
                ),
                rolling_eligible=args.rolling_eligible,
            )
        else:
            result = run_scale500_once(
                **kwargs,
                target_bar_end=args.target_bar_end,
                allow_late_start=args.allow_late_start,
                canary_receipt=args.canary_receipt,
                rolling_eligible=args.rolling_eligible,
            )
    except Exception as exc:
        reason = _reason_code(exc)
        print(
            json.dumps(
                _failure_payload(reason),
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPECTED_UNIVERSE_COUNT",
    "MinuteScale500RuntimeError",
    "build_scale500_partial_shadow_receipt",
    "canonical_universe_sha256",
    "initialize_scale500_session",
    "main",
    "run_scale500_once",
]
