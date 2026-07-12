#!/usr/bin/env python3
"""Persist the independent, sim-only CNFutures maturity read model.

This bounded operation reads the append-only CNFutures review journal and the
current market-capital provider state, then atomically replaces exactly one
rebuildable projection: ``market_maturity_latest.json``.  It has no order,
broker, email, promotion, cron-installation, or live-transition path.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Optional

from CNFutures.forward_labels import materialize_cn_futures_forward_labels
from CNFutures.review import DEFAULT_REVIEW_PATH
from CNFutures.sample_maturity import (
    CNFuturesMaturityError,
    build_futures_maturity_projection,
    validate_futures_authority_state,
    validate_futures_review_safety,
)
from shared.capital.market_ledger import load_market_capital_provider_state
from shared.data.reader import TradingagentDataReader


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "cn_futures"
DEFAULT_FRESH_START_TRADE_DATE = "20260713"
MATURITY_LATEST = "market_maturity_latest.json"

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "live", "real", "production"}
_LIVE_ENV_KEYS = {
    "REAL_TRADING_ENABLED",
    "LIVE_TRADING_ENABLED",
    "LIVE_EXECUTION_ENABLED",
    "REAL_MONEY_ENABLED",
    "PRODUCTION_EXECUTION_ENABLED",
}


class CNFuturesSampleOpsError(RuntimeError):
    """Base error for the CNFutures sample/maturity operation."""


class CNFuturesSampleOpsSafetyError(CNFuturesSampleOpsError):
    """Raised before projection writes when evidence is unsafe."""


def _assert_sim_only(environ: Mapping[str, Any]) -> None:
    for key in _LIVE_ENV_KEYS:
        if str(environ.get(key) or "").strip().lower() in _TRUE_VALUES:
            raise CNFuturesSampleOpsSafetyError(f"live_environment_rejected:{key}")
    for key in ("TRADING_MODE", "EXECUTION_MODE", "ACCOUNT_TYPE", "CAPITAL_LAYER"):
        if str(environ.get(key) or "").strip().lower() in {
            "live",
            "real",
            "production",
            "real_money",
        }:
            raise CNFuturesSampleOpsSafetyError(f"live_environment_rejected:{key}")


def _check_no_symlink(path: Path, *, label: str) -> None:
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise CNFuturesSampleOpsSafetyError(
                f"{label}_symlink_not_allowed:{current}"
            )
        if current == current.parent:
            break
        current = current.parent


def _parse_as_of(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise CNFuturesSampleOpsSafetyError("as_of_required")
    try:
        parsed = datetime.fromisoformat(raw.replace(" ", "T", 1).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CNFuturesSampleOpsSafetyError("as_of_must_be_iso_timestamp") from exc
    if parsed.tzinfo is None:
        raise CNFuturesSampleOpsSafetyError("as_of_timezone_required")
    return parsed.isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _check_no_symlink(path, label=path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def run_cn_futures_sample_ops(
    *,
    review_path: str | Path = DEFAULT_REVIEW_PATH,
    review_dir: str | Path = DEFAULT_REVIEW_DIR,
    trade_date: Any,
    as_of: Any,
    authority_state: Optional[Mapping[str, Any]] = None,
    capital_root: str | Path | None = None,
    fresh_start_trade_date: Any = DEFAULT_FRESH_START_TRADE_DATE,
    environ: Optional[Mapping[str, Any]] = None,
    reader: Any | None = None,
) -> dict[str, Any]:
    """Build and persist one current CNFutures maturity projection."""

    active_environ = os.environ if environ is None else environ
    _assert_sim_only(active_environ)
    selected_review_path = Path(review_path)
    selected_review_dir = Path(review_dir)
    latest_path = selected_review_dir / MATURITY_LATEST
    _check_no_symlink(selected_review_path, label="review_path")
    _check_no_symlink(selected_review_dir, label="review_dir")
    _check_no_symlink(latest_path, label=MATURITY_LATEST)
    generated_at = _parse_as_of(as_of)

    selected_authority_state = authority_state
    if selected_authority_state is None:
        selected_authority_state = load_market_capital_provider_state(
            "cn_futures",
            str(trade_date),
            root=capital_root,
        )
    if not isinstance(selected_authority_state, Mapping):
        raise CNFuturesSampleOpsSafetyError("cn_futures_market_capital_unavailable")

    try:
        authority_scope = validate_futures_authority_state(selected_authority_state)
        validate_futures_review_safety(selected_review_path)
        label_materialization = materialize_cn_futures_forward_labels(
            review_path=selected_review_path,
            reader=reader if reader is not None else TradingagentDataReader(),
            authority_scope=authority_scope,
            as_of=generated_at,
        )
        maturity = build_futures_maturity_projection(
            review_path=selected_review_path,
            authority_state=selected_authority_state,
            fresh_start_trade_date=fresh_start_trade_date,
            trade_date=trade_date,
            generated_at=generated_at,
        )
    except (CNFuturesMaturityError, ValueError) as exc:
        raise CNFuturesSampleOpsSafetyError(str(exc)) from exc

    # All source, authority, and content validation completes before the one
    # rebuildable projection is replaced.
    _atomic_write_json(latest_path, maturity)
    warnings = list(maturity.get("blocking_reasons") or [])
    overall_status = "warn" if warnings else "pass"
    return {
        "operation": "cn_futures_sample_ops",
        "overall_status": overall_status,
        "status": overall_status,
        "reason": warnings[0] if warnings else None,
        "warning_reasons": warnings,
        "market": "CNFutures",
        "trade_date": maturity["trade_date"],
        "as_of": generated_at,
        "review_path": str(selected_review_path.absolute()),
        "review_dir": str(selected_review_dir.absolute()),
        "label_materialization": label_materialization,
        "market_maturity": maturity,
        "orders_created": 0,
        "emails_sent": 0,
        "accounts_created": 0,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Persist the sim-only CNFutures maturity projection."
    )
    parser.add_argument("--review-path", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--fresh-start-trade-date",
        default=DEFAULT_FRESH_START_TRADE_DATE,
    )
    parser.add_argument("--capital-root", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_cn_futures_sample_ops(
            review_path=args.review_path,
            review_dir=args.review_dir,
            trade_date=args.trade_date,
            as_of=args.as_of,
            fresh_start_trade_date=args.fresh_start_trade_date,
            capital_root=args.capital_root,
        )
        exit_code = 0
    except CNFuturesSampleOpsSafetyError as exc:
        report = {
            "operation": "cn_futures_sample_ops",
            "overall_status": "blocked",
            "status": "blocked",
            "reason": str(exc),
            "orders_created": 0,
            "emails_sent": 0,
            "accounts_created": 0,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "live_transition_authorized": False,
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }
        exit_code = 2
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CNFuturesSampleOpsError",
    "CNFuturesSampleOpsSafetyError",
    "DEFAULT_FRESH_START_TRADE_DATE",
    "DEFAULT_REVIEW_DIR",
    "MATURITY_LATEST",
    "run_cn_futures_sample_ops",
]
