#!/usr/bin/env python3
"""Operate per-market simulated capital authorities — Nicholas fresh-start approved.

v2: init requires --root (reject default), --confirm-fresh-start,
--opening-manifest PATH, --legacy-freeze-manifest PATH.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.capital.market_ledger import (  # noqa: E402
    MarketCapitalLedger,
    MarketCapitalLedgerError,
    OpeningStateManifest,
    _is_default_production_root,
    load_market_capital_provider_state,
    market_capital_root,
)
from shared.capital.market_policy import (  # noqa: E402
    ALLOWED_MARKETS,
    MarketPolicy,
    MarketPolicyError,
    REQUIRED_CUTOVER_STATE,
    PINNED_CUTOVER_DECISION_ID,
    PINNED_SOURCE_THREAD_ID,
)

BLOCKED = 2
TRUTHY = {"1", "true", "yes", "y", "on", "enabled", "enable"}
ALL_M = sorted(ALLOWED_MARKETS)


def _truthy(v: object) -> bool:
    return str(v or "").strip().lower() in TRUTHY


def _td(v: str | None) -> str:
    return str(v or datetime.now(timezone.utc).strftime("%Y%m%d")).replace("-", "")


def _emit(p: Mapping[str, Any]) -> None:
    print(json.dumps(dict(p), ensure_ascii=False, sort_keys=True))


def _efn(m: str) -> str:
    try:
        return f"{MarketPolicy.load(m).account_name}_capital_events.jsonl"
    except MarketPolicyError:
        return f"{m}_sim_capital_events.jsonl"


def _exists(m: str, r: Path) -> bool:
    return (r / _efn(m)).exists()


def _unavail(m: str, r: Path) -> dict:
    return {
        "status": "market_capital_unavailable",
        "market": m,
        "root": str(r),
        "real_trading_enabled": False,
    }


def _load_state(m: str, td: str, root=None):
    rp = root if root else market_capital_root(m)
    return load_market_capital_provider_state(m, td, root=rp)


def _opening_manifest_from_payload(payload: object) -> OpeningStateManifest:
    """Parse the operator's manifest verbatim; never synthesize economic state."""

    if not isinstance(payload, Mapping):
        raise ValueError("opening_manifest_not_object")
    expected_fields = set(OpeningStateManifest.__dataclass_fields__)
    if set(payload) != expected_fields:
        raise ValueError("opening_manifest_fields_invalid")

    string_fields = {
        "market",
        "authority_id",
        "cutover_decision_id",
        "mode",
        "as_of",
        "source",
        "source_sha256",
        "execution_lineage_id",
    }
    numeric_fields = {
        "cash_balance_cny",
        "opening_equity_cny",
        "active_reservations_cny",
        "inherited_high_water_equity_cny",
        "frozen_order_cash_cny",
        "realized_pnl_cny",
        "unrealized_pnl_cny",
    }
    if any(not isinstance(payload[field], str) for field in string_fields):
        raise ValueError("opening_manifest_string_field_invalid")
    if any(
        not isinstance(payload[field], (int, float))
        or isinstance(payload[field], bool)
        or not math.isfinite(float(payload[field]))
        for field in numeric_fields
    ):
        raise ValueError("opening_manifest_numeric_field_invalid")
    if not isinstance(payload["consecutive_losses"], int) or isinstance(
        payload["consecutive_losses"], bool
    ):
        raise ValueError("opening_manifest_consecutive_losses_invalid")
    if not isinstance(payload["positions_by_risk_unit"], dict):
        raise ValueError("opening_manifest_positions_invalid")
    if not isinstance(payload["position_margin_by_risk_unit"], dict):
        raise ValueError("opening_manifest_margin_invalid")
    if not isinstance(payload["real"], bool):
        raise ValueError("opening_manifest_real_invalid")
    try:
        return OpeningStateManifest(**dict(payload))
    except TypeError as exc:
        raise ValueError("opening_manifest_fields_invalid") from exc


def _cmd_init(args: argparse.Namespace) -> int:
    m = args.market
    env_real = _truthy(os.environ.get("REAL_TRADING_ENABLED"))

    if args.root is None:
        _emit(
            {
                "status": "blocked",
                "blockers": ["root_required"],
                "market": m,
                "real_trading_enabled": False,
            }
        )
        return BLOCKED
    root = Path(args.root).expanduser()

    # Reject default production root
    if _is_default_production_root(m, root):
        _emit(
            {
                "status": "blocked",
                "blockers": ["default_production_root_rejected_for_init"],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED

    if not args.confirm_fresh_start:
        _emit(
            {
                "status": "confirmation_required",
                "required_flag": "--confirm-fresh-start",
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED
    if env_real:
        _emit(
            {
                "status": "blocked",
                "blockers": ["environment_real_trading_requested"],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED

    # Require manifest paths
    if not args.opening_manifest:
        _emit(
            {
                "status": "blocked",
                "blockers": ["opening_manifest_path_required"],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED
    if not args.legacy_freeze_manifest:
        _emit(
            {
                "status": "blocked",
                "blockers": ["legacy_freeze_manifest_path_required"],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED

    # Parse manifests from files
    try:
        om_raw = json.loads(Path(args.opening_manifest).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        _emit(
            {
                "status": "blocked",
                "blockers": ["opening_manifest_unreadable"],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED
    try:
        lm_raw = json.loads(Path(args.legacy_freeze_manifest).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        _emit(
            {
                "status": "blocked",
                "blockers": ["legacy_freeze_manifest_unreadable"],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED

    try:
        manifest = _opening_manifest_from_payload(om_raw)
    except ValueError as exc:
        _emit(
            {
                "status": "blocked",
                "blockers": [str(exc)],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED
    if args.trade_date is not None and _td(args.trade_date) != str(
        manifest.as_of
    ).replace("-", ""):
        _emit(
            {
                "status": "blocked",
                "blockers": ["opening_manifest_trade_date_mismatch"],
                "market": m,
                "root": str(root),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED

    policy = MarketPolicy.load(m)
    ledger = MarketCapitalLedger(root, policy=policy)
    result = ledger.initialize(
        manifest,
        cutover_manifest={
            "cutover_decision_id": PINNED_CUTOVER_DECISION_ID,
            "source_thread_id": PINNED_SOURCE_THREAD_ID,
            "cutover_state": REQUIRED_CUTOVER_STATE,
            "authority_generation": policy.authority_generation,
            "confirmed_by": "nicholas",
        },
        legacy_freeze_manifest=lm_raw,
    )
    result["market"] = m
    result["root"] = str(root)
    _emit(result)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    m = args.market
    r = Path(args.root).expanduser() if args.root else market_capital_root(m)
    td = _td(args.trade_date)
    if not _exists(m, r):
        _emit(_unavail(m, r))
        return BLOCKED
    s = _load_state(m, td, root=r)
    if s is None:
        _emit(_unavail(m, r))
        return BLOCKED
    _emit(
        {
            **s,
            "status": "market_capital_available",
            "market": m,
            "root": str(r),
            "real_trading_enabled": False,
        }
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    m = args.market
    r = Path(args.root).expanduser() if args.root else market_capital_root(m)
    if not _exists(m, r):
        _emit(_unavail(m, r))
        return BLOCKED
    try:
        res = MarketCapitalLedger(
            r, policy=MarketPolicy.load(m)
        ).validate_checksum_chain()
        res["market"] = m
        res["root"] = str(r)
        _emit(res)
        return 0 if res["status"] == "valid" else BLOCKED
    except MarketCapitalLedgerError as e:
        _emit(
            {
                "status": "invalid",
                "issues": [str(e)],
                "market": m,
                "root": str(r),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED


def _cmd_dry_run(args: argparse.Namespace) -> int:
    m = args.market
    r = Path(args.root).expanduser() if args.root else market_capital_root(m)
    td = _td(args.trade_date)
    if not _exists(m, r):
        _emit(_unavail(m, r))
        return BLOCKED
    try:
        s = MarketCapitalLedger(r, policy=MarketPolicy.load(m)).snapshot()
        _emit(
            {
                "status": "dry_run_ok",
                "market": m,
                "trade_date": td,
                "equity_cny": s.equity_cny,
                "cash_balance_cny": s.cash_balance_cny,
                "reserved_capital_cny": s.reserved_capital_cny,
                "reconciled": s.reconciled,
                "note": "Dry run — no events written.",
                "real_trading_enabled": False,
            }
        )
        return 0
    except MarketCapitalLedgerError as e:
        _emit(
            {
                "status": "blocked",
                "blockers": [str(e)],
                "market": m,
                "root": str(r),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED


def _cmd_cutover_audit(args: argparse.Namespace) -> int:
    m = args.market
    r = Path(args.root).expanduser() if args.root else market_capital_root(m)
    try:
        pol = MarketPolicy.load(m)
    except MarketPolicyError as e:
        _emit(
            {
                "status": "blocked",
                "blockers": [str(e)],
                "market": m,
                "root": str(r),
                "real_trading_enabled": False,
            }
        )
        return BLOCKED
    has = _exists(m, r)
    rep: dict = {
        "status": "cutover_audit",
        "market": m,
        "capital_authority_id": pol.capital_authority_id,
        "authority_generation": pol.authority_generation,
        "cutover_state": pol.cutover_state,
        "cutover_decision_id": pol.cutover_decision_id,
        "authority_initialized": has,
        "root": str(r),
        "real_trading_enabled": False,
    }
    if has:
        try:
            s = MarketCapitalLedger(r, policy=pol).snapshot()
            rep["equity_cny"] = s.equity_cny
            rep["event_count"] = len(
                MarketCapitalLedger(r, policy=pol)._load_events_unlocked()
            )
        except MarketCapitalLedgerError as e:
            rep["issues"] = [str(e)]
    _emit(rep)
    return 0


def _cmd_dual(args: argparse.Namespace) -> int:
    td = _td(args.trade_date)
    markets: dict = {}
    ec = 0
    for m in ALL_M:
        rt = market_capital_root(m)
        if not _exists(m, rt):
            markets[m] = _unavail(m, rt)
            ec = BLOCKED
            continue
        s = _load_state(m, td)
        if s is None:
            markets[m] = _unavail(m, rt)
            ec = BLOCKED
        else:
            markets[m] = {
                **s,
                "status": "market_capital_available",
                "market": m,
                "root": str(rt),
                "real_trading_enabled": False,
            }
    _emit(
        {
            "status": "dual_market_capital",
            "trade_date": td,
            "markets": markets,
            "note": "Independent markets. Do NOT sum.",
            "real_trading_enabled": False,
        }
    )
    return ec


def _cmd_migration(_args: argparse.Namespace) -> int:
    _emit(
        {
            "status": "migration_plan",
            "action": "read_only_legacy_source",
            "message": "Old pool decommissioned. Nicholas approved fresh-start.",
            "cutover_decision": {
                "id": PINNED_CUTOVER_DECISION_ID,
                "state": REQUIRED_CUTOVER_STATE,
            },
            "new_accounts": {
                "ashare": {
                    "policy": "ashare_capital_policy.yaml",
                    "env": "TRADINGAGENT_ASHARE_CAPITAL_ROOT",
                    "authority_id": "ashare-capital-v1",
                    "generation": 1,
                    "equity": 50000,
                    "gross_limit": 45000,
                },
                "cn_futures": {
                    "policy": "cn_futures_capital_policy.yaml",
                    "env": "TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT",
                    "authority_id": "cn-futures-capital-v1",
                    "generation": 1,
                    "equity": 50000,
                    "margin_limit": 25000,
                },
            },
            "real_trading_enabled": False,
        }
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    cmds = p.add_subparsers(dest="command", required=True)

    ic = cmds.add_parser("init")
    ic.add_argument("--market", required=True, choices=ALL_M)
    ic.add_argument("--root", type=Path, default=None)
    ic.add_argument("--confirm-fresh-start", action="store_true")
    ic.add_argument("--opening-manifest", type=str, default=None)
    ic.add_argument("--legacy-freeze-manifest", type=str, default=None)
    ic.add_argument("--trade-date", default=None)

    sc = cmds.add_parser("status")
    sc.add_argument("--market", required=True, choices=ALL_M)
    sc.add_argument("--trade-date", default=None)
    sc.add_argument("--root", type=Path, default=None)

    vc = cmds.add_parser("verify")
    vc.add_argument("--market", required=True, choices=ALL_M)
    vc.add_argument("--root", type=Path, default=None)

    rc = cmds.add_parser("reconcile-dry-run")
    rc.add_argument("--market", required=True, choices=ALL_M)
    rc.add_argument("--trade-date", default=None)
    rc.add_argument("--root", type=Path, default=None)

    cc = cmds.add_parser("cutover-audit")
    cc.add_argument("--market", required=True, choices=ALL_M)
    cc.add_argument("--root", type=Path, default=None)

    dc = cmds.add_parser("dual-status")
    dc.add_argument("--trade-date", default=None)

    cmds.add_parser("migration-plan")
    return p


def main(argv=None):
    args = _parser().parse_args(argv)
    d = {
        "init": _cmd_init,
        "status": _cmd_status,
        "verify": _cmd_verify,
        "reconcile-dry-run": _cmd_dry_run,
        "cutover-audit": _cmd_cutover_audit,
        "dual-status": _cmd_dual,
        "migration-plan": _cmd_migration,
    }
    h = d.get(args.command)
    if h is None:
        _emit({"status": "unknown_command"})
        return BLOCKED
    return h(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MarketCapitalLedgerError, MarketPolicyError, OSError, ValueError) as e:
        _emit(
            {"status": "blocked", "blockers": [str(e)], "real_trading_enabled": False}
        )
        raise SystemExit(BLOCKED)
