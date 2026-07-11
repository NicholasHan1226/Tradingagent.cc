#!/usr/bin/env python3
"""A-share portfolio-level evolution evidence.

The A-share server-local simulator does not run the multi-style evolution
engine used by Crypto/PM/US. This module records whether strategy-valid
portfolio samples reached the review/evolution layer without pretending that a
single A-share account has per-style attribution.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from Ashare.epoch_review import validate_review_epoch
from shared.execution.sim_account_epoch import epoch_capital_cny, read_epoch_state
from shared.review.pnl_summary import load_mark_prices_for_positions
from shared.review.pnl_summary import sim_ledger_pnl_summary
from shared.review.sample_quality import evolution_eligible_trades, strategy_valid_trades, summarize_sample_quality
from shared.review.sim_ledger_reader import DEFAULT_LOCAL_SIM_TRADES, load_sim_trades_between, load_sim_trades_for_date


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
LATEST_PATH = DEFAULT_REVIEW_DIR / "portfolio_evolution_latest.json"
LOG_PATH = DEFAULT_REVIEW_DIR / "portfolio_evolution_log.jsonl"
MIN_EVOLUTION_EVIDENCE_SAMPLES = 20


def _today_compact() -> str:
    return date.today().strftime("%Y%m%d")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _epoch_fields(epoch_state: dict[str, Any]) -> dict[str, Any]:
    epoch_id = int(epoch_state.get("current_epoch_id") or 1)
    return {
        "capital_epoch": epoch_id,
        "capital_cny": float(epoch_state.get("capital_cny") or epoch_capital_cny(epoch_id)),
        "epoch_cutover_timestamp": str(
            epoch_state.get("cutover_timestamp") or epoch_state.get("activated_at") or ""
        ),
    }


def _load_tier_manifest(review_dir: Path, epoch_fields: dict[str, Any]) -> dict[str, Any]:
    path = review_dir / "tier_experiments_latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if int(epoch_fields["capital_epoch"]) <= 1 and "capital_epoch" not in payload:
        return payload
    valid, _ = validate_review_epoch(
        payload,
        current_epoch_id=int(epoch_fields["capital_epoch"]),
        current_cutover_timestamp=str(epoch_fields["epoch_cutover_timestamp"]),
    )
    return payload if valid else {}


def _timestamp_at_or_after_cutover(row: dict[str, Any], cutover_timestamp: str) -> bool:
    raw = str(
        row.get("created_at")
        or row.get("trade_timestamp_bj")
        or row.get("timestamp_bj")
        or ""
    ).strip()
    try:
        row_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        cutover = datetime.fromisoformat(str(cutover_timestamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if row_time.tzinfo is None:
        row_time = row_time.replace(tzinfo=timezone.utc)
    if cutover.tzinfo is None:
        cutover = cutover.replace(tzinfo=timezone.utc)
    return row_time.astimezone(timezone.utc) >= cutover.astimezone(timezone.utc)


@contextmanager
def _current_epoch_trade_file(
    path: Path,
    epoch_fields: dict[str, Any],
):
    """Yield a temporary ledger containing only explicit current-epoch rows."""

    current_epoch = int(epoch_fields["capital_epoch"])
    accepted: list[dict[str, Any]] = []
    rejections: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if "capital_epoch" not in row:
            if current_epoch <= 1 or _timestamp_at_or_after_cutover(
                row, str(epoch_fields["epoch_cutover_timestamp"])
            ):
                accepted.append(row)
            else:
                rejections["missing_capital_epoch"] = rejections.get("missing_capital_epoch", 0) + 1
            continue
        try:
            row_epoch = int(row["capital_epoch"])
        except (TypeError, ValueError):
            rejections["invalid_capital_epoch"] = rejections.get("invalid_capital_epoch", 0) + 1
            continue
        if row_epoch != current_epoch:
            rejections["capital_epoch_mismatch"] = rejections.get("capital_epoch_mismatch", 0) + 1
            continue
        accepted.append(row)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix="ashare-current-epoch-",
    ) as handle:
        for row in accepted:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        yield Path(handle.name), rejections


def _tier_rankings(tier_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rankings: list[dict[str, Any]] = []
    accounts = tier_manifest.get("accounts") if isinstance(tier_manifest.get("accounts"), list) else []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        pnl = account.get("pnl") if isinstance(account.get("pnl"), dict) else {}
        capital_plan = account.get("capital_plan") if isinstance(account.get("capital_plan"), dict) else {}
        rankings.append(
            {
                "style_name": str(account.get("account") or ""),
                "trades": _safe_int(account.get("trade_count")),
                "pnl": round(_safe_float(pnl.get("total_pnl")), 6),
                "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
                "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
                "capital": _safe_float(account.get("capital")),
                "capital_plan": capital_plan,
                "pnl_source": "ashare_capital_tier_experiment",
            }
        )
    return rankings


def _refresh_local_sim_snapshot_for_review(local_trades_path: Path | None) -> dict[str, Any]:
    """Refresh default local sim snapshot before writing production review output."""
    if local_trades_path is not None:
        return {"status": "skipped", "reason": "custom_local_trades_path"}
    try:
        from shared.execution import local_sim_ledger

        positions = local_sim_ledger.get_local_sim_pnl(account=None, mark_prices=None).get("positions") or {}
        if not isinstance(positions, dict) or not positions:
            return {"status": "skipped", "reason": "no_open_positions"}
        mark_prices = load_mark_prices_for_positions(positions, "ashare")
        if not mark_prices:
            return {"status": "skipped", "reason": "no_mark_prices"}
        result = local_sim_ledger.refresh_local_sim_snapshot(mark_prices=mark_prices)
        result["mark_prices"] = mark_prices
        return result
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"{exc.__class__.__name__}: {exc}"}


def _action_for_samples(
    *,
    strategy_sample_count: int,
    eligible_sample_count: int,
    realized_round_trip_count: int,
    forward_label_count: int,
    pnl: dict[str, Any],
    min_samples: int,
    min_evolution_evidence_samples: int,
) -> tuple[str, str]:
    if strategy_sample_count <= 0:
        return "wait_for_strategy_samples", "no_strategy_valid_samples"
    if strategy_sample_count < min_samples:
        return "observe", "sample_insufficient"
    if eligible_sample_count < min_evolution_evidence_samples:
        return "observe", "insufficient_verified_execution_evidence"
    if realized_round_trip_count < max(1, min_evolution_evidence_samples // 2):
        return "observe", "insufficient_realized_round_trips"
    if forward_label_count < min_evolution_evidence_samples:
        return "observe", "insufficient_forward_validation"
    total_pnl = _safe_float(pnl.get("total_pnl"))
    realized_pnl = _safe_float(pnl.get("realized_pnl"))
    if total_pnl < 0:
        return "tighten_risk", "negative_mark_to_market_pnl"
    if realized_pnl <= 0:
        return "observe", "non_positive_realized_pnl"
    if total_pnl > 0:
        return "expand_risk", "positive_mark_to_market_pnl"
    return "observe", "flat_mark_to_market_pnl"


def _forward_label_count(review_dir: Path) -> int:
    try:
        payload = json.loads((review_dir / "forward_validation_latest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0
    labels = payload.get("labels") if isinstance(payload, dict) else []
    return sum(
        1
        for row in labels if isinstance(row, dict)
        and isinstance(row.get("labels"), dict)
        and isinstance(row["labels"].get("m60"), dict)
        and row["labels"]["m60"].get("status") == "labeled"
    )


def build_portfolio_evolution(
    *,
    trade_date: str | None = None,
    review_dir: Path | str | None = None,
    local_trades_path: Path | str | None = None,
    mark_prices: dict[str, float] | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Build read-only portfolio evolution evidence for A-share."""

    target_date = str(trade_date or _today_compact()).replace("-", "")[:8]
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    local_path = Path(local_trades_path) if local_trades_path is not None else DEFAULT_LOCAL_SIM_TRADES
    epoch_fields = _epoch_fields(read_epoch_state())
    with _current_epoch_trade_file(local_path, epoch_fields) as (filtered_path, epoch_rejections):
        no_style_ledgers = filtered_path.parent / "no-style-ledgers"
        day_trades = load_sim_trades_for_date(
            target_date,
            markets=("ashare",),
            local_trades_path=filtered_path,
            ledger_root=no_style_ledgers,
        )
        all_trades = load_sim_trades_between(
            "19000101",
            target_date,
            markets=("ashare",),
            local_trades_path=filtered_path,
            ledger_root=no_style_ledgers,
        )
        pnl_by_market = sim_ledger_pnl_summary(
            markets=("ashare",),
            local_trades_path=filtered_path,
            ashare_mark_prices=mark_prices,
        )
    day_quality = summarize_sample_quality(day_trades)
    cumulative_quality = summarize_sample_quality(all_trades)
    day_strategy_trades = strategy_valid_trades(day_trades)
    cumulative_strategy_trades = strategy_valid_trades(all_trades)
    cumulative_evolution_trades = evolution_eligible_trades(all_trades)
    pnl = pnl_by_market.get("ashare", {})
    tier_manifest = _load_tier_manifest(review_path, epoch_fields)
    tier_rankings = _tier_rankings(tier_manifest)
    strategy_sample_count = _safe_int(cumulative_quality.get("strategy_sample_valid_count"))
    eligible_sample_count = len(cumulative_evolution_trades)
    realized_round_trip_count = sum(1 for row in cumulative_evolution_trades if str(row.get("side") or "").lower() == "sell")
    forward_label_count = _forward_label_count(review_path)
    evolution_rejection_reasons = cumulative_quality.get("evolution_rejection_reasons") or {}
    evidence_blockers: list[str] = []
    if eligible_sample_count < strategy_sample_count:
        evidence_blockers.append("weak_fill_price_evidence")
        evidence_blockers.extend(sorted(evolution_rejection_reasons.keys()))
    if realized_round_trip_count <= 0:
        evidence_blockers.append("no_realized_round_trip")
    if forward_label_count <= 0:
        evidence_blockers.append("no_forward_validation_labels")
    min_evolution_evidence_samples = max(MIN_EVOLUTION_EVIDENCE_SAMPLES, int(min_samples))
    action, reason = _action_for_samples(
        strategy_sample_count=strategy_sample_count,
        eligible_sample_count=eligible_sample_count,
        realized_round_trip_count=realized_round_trip_count,
        forward_label_count=forward_label_count,
        pnl=pnl,
        min_samples=max(1, int(min_samples)),
        min_evolution_evidence_samples=min_evolution_evidence_samples,
    )
    state = "observed" if strategy_sample_count > 0 else "waiting"
    if reason in {
        "sample_insufficient",
        "insufficient_verified_execution_evidence",
        "insufficient_realized_round_trips",
        "insufficient_forward_validation",
    }:
        state = "evidence_pending"

    report = {
        **epoch_fields,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": "ashare",
        "trade_date": target_date,
        "state": state,
        "actions": [
            {
                "action": action,
                "reason": reason,
                "min_samples": max(1, int(min_samples)),
                "strategy_sample_count": strategy_sample_count,
                "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
            }
        ],
        "rankings": [
            {
                "style_name": "ashare_portfolio",
                "trades": strategy_sample_count,
                "pnl": round(_safe_float(pnl.get("total_pnl")), 6),
                "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
                "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
                "pnl_source": pnl.get("pnl_source", ""),
            }
        ] + tier_rankings,
        "weights": {
            "ashare_portfolio": {
                "status": "active",
                "weight": 1.0,
                "scope": "portfolio_account",
            }
        },
        "sample_quality": {
            "today": day_quality,
            "cumulative": cumulative_quality,
        },
        "evolution_evidence": {
            "eligible_sample_count": eligible_sample_count,
            "realized_round_trip_count": realized_round_trip_count,
            "forward_label_count": forward_label_count,
            "min_evolution_evidence_samples": min_evolution_evidence_samples,
            "blockers": evidence_blockers,
            "rejection_reasons": evolution_rejection_reasons,
        },
        "strategy_sample_count": strategy_sample_count,
        "today_strategy_sample_count": len(day_strategy_trades),
        "cumulative_strategy_sample_count": len(cumulative_strategy_trades),
        "validation_sample_count": _safe_int(cumulative_quality.get("validation_sample_count")),
        "tier_experiments": {
            "account_count": len(tier_rankings),
            "accounts": [
                {
                    "account": row.get("style_name"),
                    "capital": row.get("capital"),
                    "trades": row.get("trades"),
                    "pnl": row.get("pnl"),
                    "capital_plan": row.get("capital_plan"),
                }
                for row in tier_rankings
            ],
            "capital_plans": {
                str(row.get("style_name")): row.get("capital_plan")
                for row in tier_rankings
            },
        },
        "pnl": {
            "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
            "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
            "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
            "strategy_total_pnl": round(_safe_float(pnl.get("strategy_total_pnl")), 6),
            "market_value": round(_safe_float(pnl.get("market_value")), 6),
            "cash": pnl.get("cash"),
            "equity": pnl.get("equity"),
            "open_position_count": _safe_int(pnl.get("open_position_count")),
            "missing_mark_count": _safe_int(pnl.get("missing_mark_count")),
            "pnl_source": pnl.get("pnl_source", ""),
        },
        "read_only": True,
        "real_trading_enabled": False,
        "epoch_rejections": epoch_rejections,
    }
    report["latest_path"] = _display_path(review_path / LATEST_PATH.name)
    report["log_path"] = _display_path(review_path / LOG_PATH.name)
    return report


def write_portfolio_evolution(
    *,
    trade_date: str | None = None,
    review_dir: Path | str | None = None,
    local_trades_path: Path | str | None = None,
    mark_prices: dict[str, float] | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    review_path.mkdir(parents=True, exist_ok=True)
    from Ashare.tier_experiments import write_tier_ledgers
    epoch_fields = _epoch_fields(read_epoch_state())

    if mark_prices:
        refresh_result: dict[str, Any] = {
            "status": "provided",
            "reason": "caller_supplied_mark_prices",
            "mark_prices": mark_prices,
        }
    else:
        refresh_result = _refresh_local_sim_snapshot_for_review(Path(local_trades_path) if local_trades_path else None)
        mark_prices = refresh_result.get("mark_prices") if isinstance(refresh_result, dict) else None

    tier_refresh: dict[str, Any]
    if mark_prices:
        source_path = Path(local_trades_path) if local_trades_path else DEFAULT_LOCAL_SIM_TRADES
        with _current_epoch_trade_file(source_path, epoch_fields) as (filtered_path, _):
            tier_manifest = write_tier_ledgers(
                source_trades_path=filtered_path,
                review_dir=review_path,
                mark_prices=mark_prices,
            )
        tier_manifest.update(
            {
                **epoch_fields,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source_trades": _display_path(source_path),
            }
        )
        (review_path / "tier_experiments_latest.json").write_text(
            json.dumps(tier_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tier_refresh = {
            "status": "refreshed",
            "mark_price_count": len(mark_prices),
            "account_count": len(tier_manifest.get("accounts") or []),
        }
    else:
        tier_refresh = {
            "status": "skipped",
            "reason": str(refresh_result.get("reason") or "no_mark_prices"),
        }
    report = build_portfolio_evolution(
        trade_date=trade_date,
        review_dir=review_path,
        local_trades_path=local_trades_path,
        mark_prices=mark_prices,
        min_samples=min_samples,
    )
    report["local_sim_snapshot_refresh"] = refresh_result
    report["tier_experiment_refresh"] = tier_refresh
    report["valuation_status"] = "current" if mark_prices else "unavailable"
    if not mark_prices:
        report["rankings"] = [
            row for row in report.get("rankings", [])
            if str(row.get("style_name") or "") == "ashare_portfolio"
        ]
        report["tier_experiments"] = {
            "account_count": 0,
            "accounts": [],
            "capital_plans": {},
            "status": "skipped",
            "reason": tier_refresh["reason"],
        }
        blockers = report.setdefault("evolution_evidence", {}).setdefault("blockers", [])
        if "mark_prices_unavailable" not in blockers:
            blockers.append("mark_prices_unavailable")
        report["state"] = "evidence_pending"
    try:
        from Ashare.evolution_controller import build_evolution_decision

        decision = build_evolution_decision(
            report,
            target_trade_date=report.get("trade_date"),
            min_strategy_samples=min_samples,
            current_epoch_id=int(epoch_fields["capital_epoch"]),
        )
        decision.update(epoch_fields)
        report["evolution_decision"] = decision
        decision_latest = review_path / "evolution_decision_latest.json"
        decision_log = review_path / "evolution_decision_log.jsonl"
        decision_latest.write_text(
            json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with decision_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        report["evolution_decision"] = {
            "state": "degraded",
            "recommended_action": "observe",
            "error": f"{exc.__class__.__name__}: {exc}",
            "real_trading_enabled": False,
        }
    latest = review_path / LATEST_PATH.name
    log = review_path / LOG_PATH.name
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--local-trades-path", type=Path, default=None)
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    if args.write:
        report = write_portfolio_evolution(
            trade_date=args.trade_date or None,
            review_dir=args.review_dir,
            local_trades_path=args.local_trades_path,
            min_samples=args.min_samples,
        )
    else:
        report = build_portfolio_evolution(
            trade_date=args.trade_date or None,
            review_dir=args.review_dir,
            local_trades_path=args.local_trades_path,
            min_samples=args.min_samples,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
