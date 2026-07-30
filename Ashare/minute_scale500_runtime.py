"""Fail-closed scale-500 selector for the A-share delayed-paper runtime.

The selector owns only an isolated simulation state root. It verifies one
frozen 500-symbol universe, delegates all market reads to the existing formal
TradingDatas catalog/query clients, and requires the first two accepted bars to
be the adjacent 09:35 and 09:40 session observations. It never reads or writes
the rollback-30 state root. A failure selects the rollback configuration and
returns a stable reason code for systemd's rollback unit.
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
from .minute_data import SHANGHAI
from .minute_paper_runner import load_minute_research_universe
from .minute_session_initializer import initialize_minute_session


EXPECTED_UNIVERSE_COUNT = 500
FORMAL_BASE_URL = "http://127.0.0.1:18082"
MINUTE_DATASET_ID = "cn.dataset.rt_min"
GATE_SCHEMA = "tradingagent.ashare.scale500-acceptance.v1"
GATE_DIRECTORY = ".scale500-gates"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_PATTERN = re.compile(r"^[a-z0-9_.:-]+$")

Initializer = Callable[..., dict[str, object]]
Runner = Callable[..., dict[str, object]]


class MinuteScale500RuntimeError(ValueError):
    """Fail-closed scale transition error with a stable reason code."""


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
) -> dict[str, object]:
    return {
        "schema": GATE_SCHEMA,
        "trading_date": trading_date,
        "status": "pending_two_live_snapshots",
        "selected_mode": "scale500",
        "expected_universe_count": EXPECTED_UNIVERSE_COUNT,
        "universe_sha256": universe_sha256,
        "validated_bar_ends": [],
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
) -> dict[str, object]:
    raw = _load_json(path, "minute_scale500_gate_missing_or_invalid")
    if not isinstance(raw, Mapping):
        raise MinuteScale500RuntimeError("minute_scale500_gate_missing_or_invalid")
    gate = dict(raw)
    validated = gate.get("validated_bar_ends")
    if (
        gate.get("schema") != GATE_SCHEMA
        or gate.get("trading_date") != trading_date
        or gate.get("universe_sha256") != universe_sha256
        or gate.get("expected_universe_count") != EXPECTED_UNIVERSE_COUNT
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
        or gate.get("selected_mode") not in {"scale500", "rollback30"}
        or (
            gate.get("status") == "fallback30_selected"
            and gate.get("selected_mode") != "rollback30"
        )
        or (
            gate.get("status") != "fallback30_selected"
            and gate.get("selected_mode") != "scale500"
        )
        or not isinstance(validated, list)
        or len(validated) > 2
        or any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in validated
        )
    ):
        raise MinuteScale500RuntimeError("minute_scale500_gate_missing_or_invalid")
    return gate


def _reason_code(exc: BaseException) -> str:
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
) -> None:
    gate_path = _gate_path(scale_root, trading_date)
    try:
        gate = _load_gate(
            gate_path,
            trading_date=trading_date,
            rollback_root=rollback_root,
            universe_sha256=universe_sha256,
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
        or profile.get("max_rows") != EXPECTED_UNIVERSE_COUNT
        or profile.get("page_limit") != EXPECTED_UNIVERSE_COUNT
        or isinstance(profile.get("max_pages"), bool)
        or not isinstance(profile.get("max_pages"), int)
        or profile.get("max_pages", 0) <= 0
    ):
        raise MinuteScale500RuntimeError("minute_scale500_manifest_invalid")
    if (
        not isinstance(universe, list)
        or len(universe) != EXPECTED_UNIVERSE_COUNT
        or hashlib.sha256(_canonical_json(universe)).hexdigest() != universe_sha256
    ):
        raise MinuteScale500RuntimeError("minute_scale500_universe_digest_mismatch")
    expected_symbols = {
        row.get("symbol") for row in universe if isinstance(row, Mapping)
    }
    if (
        len(expected_symbols) != EXPECTED_UNIVERSE_COUNT
        or not isinstance(references, list)
        or len(references) != EXPECTED_UNIVERSE_COUNT
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
    initializer: Initializer = initialize_minute_session,
) -> dict[str, object]:
    """Initialize only the isolated 500-symbol session and acceptance gate."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteScale500RuntimeError("real_trading_must_remain_disabled")
    if now.tzinfo is None or now.utcoffset() is None:
        raise MinuteScale500RuntimeError("minute_scale500_now_must_be_aware")
    scale, rollback, token, universe_path = _validate_paths(
        scale_state_root=scale_state_root,
        rollback30_state_root=rollback30_state_root,
        token_file=token_file,
        universe_source=universe_source,
    )
    _, universe_sha256 = _validated_universe(
        universe_path,
        expected_sha256=expected_universe_sha256,
    )
    trading_date = now.astimezone(SHANGHAI).date().isoformat()
    try:
        result = initializer(
            state_root=scale,
            token_file=token,
            now=now,
            universe_source=universe_path,
        )
        if (
            result.get("status") != "pass"
            or result.get("trading_date") != trading_date
            or result.get("symbol_count") != EXPECTED_UNIVERSE_COUNT
            or result.get("universe_sha256") != universe_sha256
            or result.get("authority_tier") != "non_production_fixture"
            or result.get("state_bundle_created") is not False
            or result.get("capital_authority") is not False
            or result.get("execution_authority") is not False
            or result.get("real_trading_enabled") is not False
        ):
            raise MinuteScale500RuntimeError(
                "minute_scale500_initializer_receipt_invalid"
            )
        _validate_published_session(
            scale_root=scale,
            trading_date=trading_date,
            universe_sha256=universe_sha256,
            require_no_state_bundle=True,
        )
        gate_path = _gate_path(scale, trading_date)
        if gate_path.exists():
            gate = _load_gate(
                gate_path,
                trading_date=trading_date,
                rollback_root=rollback,
                universe_sha256=universe_sha256,
            )
            if gate["status"] == "fallback30_selected":
                raise MinuteScale500RuntimeError(
                    "minute_scale500_fallback_already_selected"
                )
        else:
            gate = _new_gate(
                trading_date=trading_date,
                rollback_root=rollback,
                universe_sha256=universe_sha256,
            )
            _atomic_write_json(gate_path, gate)
    except Exception as exc:
        reason = _reason_code(exc)
        _select_rollback(
            scale_root=scale,
            trading_date=trading_date,
            rollback_root=rollback,
            universe_sha256=universe_sha256,
            reason=reason,
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
) -> None:
    if result.get("status") != "pass":
        raise MinuteScale500RuntimeError("minute_scale500_runtime_not_pass")
    if result.get("bar_end") != expected_bar_end:
        raise MinuteScale500RuntimeError("minute_scale500_bar_end_mismatch")
    if result.get("row_count") != EXPECTED_UNIVERSE_COUNT:
        raise MinuteScale500RuntimeError("minute_scale500_row_count_mismatch")
    if (
        result.get("authority_tier") != "non_production_fixture"
        or result.get("capital_authority") is not False
        or result.get("durable_capital") is not False
        or result.get("execution_authority") is not False
        or result.get("real_trading_enabled") is not False
    ):
        raise MinuteScale500RuntimeError("minute_scale500_authority_violation")
    if result.get("late_start") is True or result.get("gap_recovery") is True:
        raise MinuteScale500RuntimeError("minute_scale500_gap_or_late_start_forbidden")


def run_scale500_once(
    *,
    scale_state_root: Path | str,
    rollback30_state_root: Path | str,
    token_file: Path | str,
    universe_source: Path | str,
    expected_universe_sha256: str,
    now: datetime,
    runner: Runner = run_current_delayed_minute_paper,
) -> dict[str, object]:
    """Run one exact 500-symbol delayed-paper step or select rollback-30."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteScale500RuntimeError("real_trading_must_remain_disabled")
    if now.tzinfo is None or now.utcoffset() is None:
        raise MinuteScale500RuntimeError("minute_scale500_now_must_be_aware")
    scale, rollback, token, universe_path = _validate_paths(
        scale_state_root=scale_state_root,
        rollback30_state_root=rollback30_state_root,
        token_file=token_file,
        universe_source=universe_source,
    )
    _, universe_sha256 = _validated_universe(
        universe_path,
        expected_sha256=expected_universe_sha256,
    )
    target = expected_available_bar_end(now)
    if target is None:
        return {
            "status": "noop",
            "reason": "outside_delayed_session_window",
            "selected_mode": "scale500",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "execution_eligible": False,
            "training_eligible": False,
            "promotion_authorized": False,
            "real_trading_enabled": False,
        }
    trading_date = target.astimezone(SHANGHAI).date().isoformat()
    gate_path = _gate_path(scale, trading_date)
    try:
        gate = _load_gate(
            gate_path,
            trading_date=trading_date,
            rollback_root=rollback,
            universe_sha256=universe_sha256,
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
        _validate_published_session(
            scale_root=scale,
            trading_date=trading_date,
            universe_sha256=universe_sha256,
            require_no_state_bundle=False,
        )
        result = runner(
            state_root=scale,
            token_file=token,
            now=now,
            allow_late_start=False,
        )
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
            }
        expected_bar_end = target.strftime("%Y-%m-%d %H:%M:%S")
        _validate_runtime_receipt(result, expected_bar_end=expected_bar_end)
        validated = list(gate["validated_bar_ends"])
        if gate["status"] == "pending_two_live_snapshots":
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
            universe_sha256=universe_sha256,
            reason=reason,
        )
        raise MinuteScale500RuntimeError(reason) from exc
    return {
        **dict(result),
        "scale500_acceptance_status": gate["status"],
        "selected_mode": "scale500",
        "validated_bar_ends": list(gate["validated_bar_ends"]),
        "rollback30_state_root_preserved": True,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "execution_eligible": False,
        "training_eligible": False,
        "promotion_authorized": False,
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
        result = (
            initialize_scale500_session(**kwargs)
            if args.command == "initialize"
            else run_scale500_once(**kwargs)
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
    "canonical_universe_sha256",
    "initialize_scale500_session",
    "main",
    "run_scale500_once",
]
