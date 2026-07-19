#!/usr/bin/env python3
"""A-share market adapter for the tradingagent shadow orchestrator."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.universe.policy import is_mainboard_tradable
from shared.execution.execution_reality import ashare_execution_reality
from shared.markets.base import MarketAdapter
from shared.markets.sim_capital import default_sim_capital
from Ashare import sim_executor as _sim_executor  # noqa: F401


MARKET = "ashare"
STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"
logger = logging.getLogger(__name__)
MAX_PORTFOLIO_POSITIONS = 8
EXECUTION_CANDIDATE_SCAN_LIMIT = MAX_PORTFOLIO_POSITIONS * 3
SAMPLE_DEBT_POLICY_VERSION = "ashare-sample-debt-v1"
MIN_STRATEGY_EXECUTION_SAMPLES = 5
DEFAULT_SAMPLE_JOURNAL_PATH = (
    Path(__file__).resolve().parent.parent
    / "shared"
    / "review"
    / "ashare"
    / "sample_journal.jsonl"
)

DEFAULT_UNIVERSE_FILTER: dict[str, Any] = {
    "exclude_st": True,
    "exclude_suspended": True,
    "exclude_delisted": True,
    "exclude_bse": True,
    "exclude_non_a_share": True,
    "min_list_days": 30,
    "min_liquidity_amount": 50_000_000.0,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _daily_amount_to_yuan(value: Any) -> float:
    raw = _safe_float(value, -1.0)
    if raw < 0.0:
        return -1.0
    # Tushare daily ``amount`` is stored in thousand CNY in the read model.
    return raw * 1000.0


def _parse_date(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:10], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _lookback_start(date: str, calendar_days: int = 14) -> str:
    target = _parse_date(date)
    if target is None:
        return ""
    return (target - timedelta(days=calendar_days)).strftime("%Y%m%d")


def _is_st(asset: dict[str, Any]) -> bool:
    name = str(asset.get("name") or "").upper()
    status = str(asset.get("status") or "").upper()
    return (
        "ST" in name
        or "*ST" in name
        or "退" in str(asset.get("name") or "")
        or "ST" in status
    )


def _is_delisted(asset: dict[str, Any]) -> bool:
    status = str(asset.get("status") or "").strip().lower()
    return status in {"delisted", "退市", "d", "inactive"} or "delist" in status


def _is_suspended(asset: dict[str, Any], coverage_status: str | None) -> bool:
    status = str(asset.get("status") or "").strip().lower()
    if status in {"suspended", "halted", "停牌"}:
        return True
    if coverage_status is None:
        return False
    normalized = coverage_status.strip().lower()
    return normalized not in {"normal", "ok", "active", "trading", "covered"}


def _is_bse(asset: dict[str, Any]) -> bool:
    exchange = str(asset.get("exchange") or "").strip().upper()
    symbol = str(asset.get("symbol") or "")
    return exchange in {"BSE", "BJ", "NORTH"} or symbol.startswith(("8", "4"))


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def _positions_from_pnl(account: str, positions: Any) -> list[dict[str, Any]]:
    if not isinstance(positions, dict):
        return []
    rows: list[dict[str, Any]] = []
    for symbol, position in positions.items():
        if not isinstance(position, dict):
            continue
        row = dict(position)
        row["account"] = account
        row["ts_code"] = str(symbol)
        row.setdefault("sellable_quantity", row.get("quantity", 0))
        row.setdefault("value", row.get("market_value", row.get("cost_basis", 0.0)))
        row.setdefault("capital_layer", "simulated")
        row.setdefault("account_type", "simulated")
        row.setdefault("source", "server_local_sim_strategy_view")
        row.setdefault("sample_classification", "strategy_sample")
        rows.append(row)
    return rows


def _strategy_view_from_local_sim_trades(
    account: str, default_capital: float
) -> dict[str, Any]:
    try:
        from shared.execution import local_sim_ledger
        from shared.review.sample_quality import (
            classify_trade_sample,
            summarize_sample_quality,
        )
    except Exception:
        return {}

    rows = _read_jsonl_dicts(local_sim_ledger.LOCAL_SIM_TRADES)
    if not rows:
        return {}
    account_rows = [
        row for row in rows if str(row.get("account") or account) == account
    ]
    if not account_rows:
        return {}

    def _valid_strategy_row(row: dict[str, Any]) -> bool:
        if str(row.get("account") or account) != account:
            return False
        return bool(classify_trade_sample(row).get("strategy_sample_valid"))

    try:
        strategy_pnl = local_sim_ledger.get_local_sim_pnl(
            account=None,
            trade_filter=_valid_strategy_row,
        )
    except Exception:
        return {}

    sample_quality = summarize_sample_quality(account_rows)
    strategy_positions = _positions_from_pnl(account, strategy_pnl.get("positions"))
    strategy_cash_available = _safe_float(
        strategy_pnl.get("cash_available"), default_capital
    )
    validation_count = int(sample_quality.get("validation_sample_count") or 0)
    strategy_count = int(sample_quality.get("strategy_sample_valid_count") or 0)
    return {
        "strategy_positions": strategy_positions,
        "strategy_cash_available": strategy_cash_available,
        "strategy_sample_quality": sample_quality,
        "capital_plan_sample_adjustment": {
            "view": "strategy_valid_samples_only",
            "ignored_validation_sample_count": validation_count,
            "strategy_sample_valid_count": strategy_count,
            "account_trade_count": len(account_rows),
            "reason": "chain_validation_samples_do_not_consume_strategy_capital",
        },
    }


def _prepare_adapter_position_authority(
    payload: dict[str, Any], trade_date: str
) -> dict[str, Any]:
    """Preserve a source-owned envelope; never manufacture current identity."""

    expected_date = "".join(ch for ch in str(trade_date or "") if ch.isdigit())
    required = (
        "source",
        "position_source_status",
        "positions",
        "authority_id",
        "authority_generation",
        "execution_lineage_id",
        "authority_checksum",
        "trade_date",
        "position_count",
        "positions_fingerprint",
    )
    missing = [field for field in required if field not in payload]
    if len(expected_date) != 8:
        return {
            **payload,
            "position_source_status": "blocked",
            "position_source_reason": "ashare_position_trade_date_missing",
        }
    if missing:
        return {
            **payload,
            "position_source_status": "blocked",
            "position_source_reason": "ashare_adapter_position_envelope_missing",
            "position_source_missing_fields": missing,
        }
    source_date = "".join(
        ch for ch in str(payload.get("trade_date") or "") if ch.isdigit()
    )
    if source_date != expected_date:
        return {
            **payload,
            "position_source_status": "blocked",
            "position_source_reason": "ashare_adapter_position_trade_date_mismatch",
        }
    if "strategy_positions" in payload and not isinstance(
        payload.get("strategy_position_envelope"), dict
    ):
        return {
            **payload,
            "position_source_status": "blocked",
            "position_source_reason": "ashare_strategy_position_envelope_missing",
        }
    return dict(payload)


def _current_sample_authority_scope() -> dict[str, Any]:
    from shared.execution.execution_lineage import (
        ASHARE_AUTHORITY_GENERATION,
        ASHARE_CAPITAL_AUTHORITY_ID,
        ASHARE_EXECUTION_LINEAGE_ID,
    )

    return {
        "capital_authority_id": ASHARE_CAPITAL_AUTHORITY_ID,
        "authority_generation": ASHARE_AUTHORITY_GENERATION,
        "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
    }


def _sample_policy(value: Any = None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    version = str(raw.get("policy_version") or SAMPLE_DEBT_POLICY_VERSION).strip()
    if version != SAMPLE_DEBT_POLICY_VERSION:
        version = SAMPLE_DEBT_POLICY_VERSION
    try:
        minimum = int(
            raw.get(
                "min_strategy_execution_samples",
                MIN_STRATEGY_EXECUTION_SAMPLES,
            )
        )
    except (TypeError, ValueError):
        minimum = MIN_STRATEGY_EXECUTION_SAMPLES
    if minimum <= 0:
        minimum = MIN_STRATEGY_EXECUTION_SAMPLES
    return {
        "policy_version": version,
        "min_strategy_execution_samples": minimum,
    }


def _is_execution_fill(record: dict[str, Any]) -> bool:
    kind = (
        str(
            record.get("record_type")
            or record.get("event_type")
            or record.get("type")
            or ""
        )
        .strip()
        .lower()
    )
    status = str(record.get("status") or "").strip().lower()
    intent = str(record.get("sample_intent") or "").strip().lower()
    return (
        kind in {"fill", "simulated_fill", "execution_fill"}
        or status in {"filled", "partial"}
    ) and intent in {"exploration", "exploitation"}


def _has_symlink_component(path: Path) -> bool:
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def build_current_sample_adjustment(
    *,
    journal_path: Path | None = None,
    sample_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read sample debt only from the current fresh-start SampleJournal.

    Local trade files and adapter-provided counters are diagnostic only.  A
    missing journal is the expected fresh-start state and therefore carries an
    explicit debt instead of silently disabling bounded exploration.
    """

    from shared.review.sample_journal import SampleJournal

    path = Path(journal_path or DEFAULT_SAMPLE_JOURNAL_PATH)
    policy = _sample_policy(sample_policy)
    minimum = policy["min_strategy_execution_samples"]
    authority = _current_sample_authority_scope()
    base = {
        "view": "current_sample_journal_execution_fills",
        "sample_policy_version": policy["policy_version"],
        "min_strategy_samples": minimum,
        "authority_scope": authority,
        "journal_path": str(path),
        "sample_authority_source": "sample_journal_kpi",
        "real_trading_enabled": False,
    }
    if _has_symlink_component(path):
        return {
            **base,
            "sample_authority_status": "sample_journal_unavailable",
            "sample_authority_reliable": False,
            "strategy_sample_valid_count": 0,
            "valid_exploration_fill_count": 0,
            "valid_exploitation_fill_count": 0,
            "excluded_wrong_authority_count": 0,
            "excluded_non_execution_eligible_count": 0,
            "sample_debt": True,
            "reason": "sample_journal_symlink_not_allowed",
        }
    if not path.exists():
        return {
            **base,
            "sample_authority_status": "fresh_start_journal_missing",
            "sample_authority_reliable": True,
            "strategy_sample_valid_count": 0,
            "valid_exploration_fill_count": 0,
            "valid_exploitation_fill_count": 0,
            "excluded_wrong_authority_count": 0,
            "excluded_non_execution_eligible_count": 0,
            "sample_debt": True,
            "reason": "fresh_start_has_no_execution_samples_yet",
        }

    try:
        journal = SampleJournal(path)
        records = journal.latest_sample_records()
        kpi = journal.build_kpi(authority_scope=authority)
        current_records = [
            row
            for row in records
            if str(row.get("capital_authority_id") or "")
            == authority["capital_authority_id"]
            and row.get("authority_generation") == authority["authority_generation"]
            and str(row.get("execution_lineage_id") or "")
            == authority["execution_lineage_id"]
        ]
        current_execution_fills = [
            row for row in current_records if _is_execution_fill(row)
        ]
        valid_fills = [
            row
            for row in current_execution_fills
            if row.get("execution_eligible") is True
            and _safe_float(row.get("maturity_weight"), 1.0) > 0.0
        ]
        exploration_count = sum(
            1
            for row in valid_fills
            if str(row.get("sample_intent") or "").strip().lower() == "exploration"
        )
        exploitation_count = sum(
            1
            for row in valid_fills
            if str(row.get("sample_intent") or "").strip().lower() == "exploitation"
        )
        valid_count = exploration_count + exploitation_count
        return {
            **base,
            "sample_authority_status": "ready",
            "sample_authority_reliable": True,
            "strategy_sample_valid_count": valid_count,
            "valid_exploration_fill_count": exploration_count,
            "valid_exploitation_fill_count": exploitation_count,
            "excluded_wrong_authority_count": int(
                kpi.get("excluded_legacy_count") or 0
            ),
            "excluded_non_execution_eligible_count": (
                len(current_execution_fills) - len(valid_fills)
            ),
            "sample_kpi_layer_totals": dict(
                kpi.get("sample_layer_totals")
                if isinstance(kpi.get("sample_layer_totals"), dict)
                else {}
            ),
            "sample_debt": valid_count < minimum,
            "reason": (
                "current_execution_sample_debt"
                if valid_count < minimum
                else "current_execution_sample_minimum_met"
            ),
        }
    except Exception as exc:
        logger.warning("Unable to read A-share SampleJournal authority: %s", exc)
        return {
            **base,
            "sample_authority_status": "sample_journal_unavailable",
            "sample_authority_reliable": False,
            "strategy_sample_valid_count": 0,
            "valid_exploration_fill_count": 0,
            "valid_exploitation_fill_count": 0,
            "excluded_wrong_authority_count": 0,
            "excluded_non_execution_eligible_count": 0,
            "sample_debt": True,
            "reason": "sample_journal_unavailable",
        }


class AshareAdapter(MarketAdapter):
    """Mainboard-only stock adapter for A-share screening and simulation."""

    def __init__(
        self,
        reader: Any | None = None,
        *,
        universe_filter: dict[str, Any] | None = None,
        strategy_dir: Path | None = None,
    ) -> None:
        self.reader = reader if reader is not None else TradingagentDataReader()
        self.universe_filter = {
            **DEFAULT_UNIVERSE_FILTER,
            **dict(universe_filter or {}),
        }
        self.strategy_dir = strategy_dir or STRATEGY_DIR

    def get_market(self) -> str:
        return MARKET

    def get_universe(self, date: str) -> list[str]:
        assets = self._get_assets()
        if not assets:
            return []
        coverage = self._coverage_by_symbol(date)
        ranked: list[tuple[float, str]] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("symbol") or "").strip()
            if not symbol:
                continue
            liquidity = self._latest_liquidity(symbol, date)
            if self._exclude_asset(
                asset, coverage.get(symbol), date, liquidity=liquidity
            ):
                continue
            amount = liquidity[1] if liquidity[1] is not None else 0.0
            ranked.append((amount, symbol))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [symbol for _, symbol in ranked]

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        raw = str(symbol or "").strip().upper()
        return MARKET, raw

    def get_strategy_config(self) -> dict[str, Any]:
        strategies = self._load_strategies()
        market_rules = ashare_execution_reality().as_contract()
        market_rules["idle_cash_reverse_repo"] = "204001"
        return {
            "market": MARKET,
            "sim_capital": default_sim_capital(MARKET),
            "shadow_capital": default_sim_capital(MARKET),
            "portfolio_method": "conviction_weighted",
            "regime": "ashare_default",
            "score_universe_limit": 500,
            "max_candidates": EXECUTION_CANDIDATE_SCAN_LIMIT,
            "max_portfolio_positions": MAX_PORTFOLIO_POSITIONS,
            "sample_collection_policy": _sample_policy(),
            "default_price": 0.0,
            "default_volatility": 0.28,
            "strategies": strategies,
            "market_rules": market_rules,
            "universe_filter": dict(self.universe_filter),
        }

    def get_shadow_account(self) -> str:
        return "ashare_shadow"

    def get_sim_account(
        self,
        *,
        trade_date: str = "",
        position_authority: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = "ashare_sim"
        default_capital = default_sim_capital(MARKET)
        current_sample_adjustment = build_current_sample_adjustment()
        fallback = {
            "account": account,
            "sim_capital": default_capital,
            "cash_available": default_capital,
            "positions": [],
            "source": "ashare_adapter_empty_sim_account",
            "position_source_status": "blocked",
            "position_source_reason": "ashare_adapter_position_snapshot_unavailable",
            "capital_plan_sample_adjustment": current_sample_adjustment,
        }
        if isinstance(position_authority, dict):
            try:
                from shared.execution import local_sim_ledger

                snapshot_source = local_sim_ledger.get_local_sim_account_snapshot(
                    account,
                    trade_date=trade_date,
                    starting_cash=default_capital,
                    position_authority=position_authority,
                )
                pnl_source = local_sim_ledger.get_local_sim_pnl(
                    account,
                    trade_date=trade_date,
                    position_authority=position_authority,
                )
            except Exception as exc:
                logger.warning(
                    "Unable to load current A-share local position sources: %s", exc
                )
                return _prepare_adapter_position_authority(fallback, trade_date)
            envelope_fields = (
                "authority_id",
                "authority_generation",
                "execution_lineage_id",
                "authority_checksum",
                "trade_date",
                "position_count",
                "positions_fingerprint",
            )
            if (
                snapshot_source.get("position_source_status") != "ready"
                or pnl_source.get("position_source_status") != "ready"
                or any(
                    snapshot_source.get(field) != pnl_source.get(field)
                    for field in envelope_fields
                )
            ):
                return _prepare_adapter_position_authority(
                    {
                        **fallback,
                        "source": "ashare_adapter_live_local_sim",
                        "position_source_reason": (
                            "ashare_local_position_sources_mismatch"
                        ),
                    },
                    trade_date,
                )
            snapshot_positions = snapshot_source.get("positions")
            snapshot_positions = (
                snapshot_positions if isinstance(snapshot_positions, dict) else {}
            )
            positions = _positions_from_pnl(account, pnl_source.get("positions"))
            for position in positions:
                lots = snapshot_positions.get(position.get("ts_code"))
                lots = lots if isinstance(lots, dict) else {}
                position["sellable_quantity"] = lots.get(
                    "sellable_quantity", position.get("sellable_quantity", 0)
                )
            snapshot_cash = _safe_float(snapshot_source.get("cash_available"), -1.0)
            pnl_cash = _safe_float(pnl_source.get("cash_available"), -1.0)
            if (
                snapshot_cash < 0.0
                or pnl_cash < 0.0
                or abs(snapshot_cash - pnl_cash) > 0.01
            ):
                return _prepare_adapter_position_authority(
                    {
                        **fallback,
                        "source": "ashare_adapter_live_local_sim",
                        "position_source_reason": "ashare_local_cash_sources_mismatch",
                    },
                    trade_date,
                )
            result = {
                "account": account,
                "sim_capital": default_capital,
                "cash_available": snapshot_cash,
                "available_cash": snapshot_cash,
                "positions": positions,
                "pnl": pnl_source,
                "source": "ashare_adapter_live_local_sim",
                "position_source_status": "ready",
                **{field: snapshot_source[field] for field in envelope_fields},
                "strategy_positions": [dict(row) for row in positions],
                "strategy_cash_available": snapshot_cash,
                "strategy_position_envelope": {
                    "source": "ashare_strategy_live_local_sim",
                    "position_source_status": "ready",
                    "positions": [dict(row) for row in positions],
                    **{field: pnl_source[field] for field in envelope_fields},
                },
                "capital_plan_sample_adjustment": current_sample_adjustment,
            }
            return _prepare_adapter_position_authority(result, trade_date)
        try:
            from shared.execution.local_sim_ledger import LOCAL_SIM_POSITIONS_SNAPSHOT

            if not LOCAL_SIM_POSITIONS_SNAPSHOT.exists():
                return _prepare_adapter_position_authority(fallback, trade_date)
            payload = json.loads(
                LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("Unable to load A-share local sim snapshot: %s", exc)
            return _prepare_adapter_position_authority(fallback, trade_date)
        if not isinstance(payload, dict):
            return _prepare_adapter_position_authority(fallback, trade_date)

        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            return _prepare_adapter_position_authority(
                {
                    **fallback,
                    "source": str(
                        payload.get("source") or "ashare_adapter_position_snapshot"
                    ),
                    "position_source_reason": "ashare_adapter_positions_missing",
                },
                trade_date,
            )
        positions: list[dict[str, Any]] = []
        for row in raw_positions:
            if not isinstance(row, dict):
                return _prepare_adapter_position_authority(
                    {
                        **fallback,
                        "source": str(
                            payload.get("source") or "ashare_adapter_position_snapshot"
                        ),
                        "position_source_reason": "ashare_adapter_position_row_invalid",
                    },
                    trade_date,
                )
            row_account = str(row.get("account") or account)
            if row_account != account:
                continue
            position = dict(row)
            position.setdefault("account", account)
            position.setdefault("sellable_quantity", position.get("quantity", 0))
            position.setdefault("value", position.get("market_value", 0.0))
            positions.append(position)

        pnl = payload.get("pnl") if isinstance(payload.get("pnl"), dict) else {}
        account_pnl = pnl.get(account) if isinstance(pnl.get(account), dict) else {}
        cash_available = _safe_float(
            account_pnl.get("cash_available", payload.get("cash_available")),
            default_capital,
        )
        strategy_view = _strategy_view_from_local_sim_trades(account, default_capital)
        legacy_sample_diagnostics = strategy_view.pop(
            "capital_plan_sample_adjustment", {}
        )
        sample_adjustment = {
            key: value
            for key, value in (
                legacy_sample_diagnostics.items()
                if isinstance(legacy_sample_diagnostics, dict)
                else []
            )
            if key
            not in {
                "strategy_sample_valid_count",
                "min_strategy_samples",
                "sample_debt",
                "sample_authority_status",
                "sample_authority_reliable",
                "authority_scope",
            }
        }
        sample_adjustment.update(current_sample_adjustment)
        result = {
            "account": account,
            "sim_capital": default_capital,
            "cash_available": cash_available,
            "available_cash": cash_available,
            "positions": positions,
            "pnl": account_pnl,
            "source": str(payload.get("source") or "server_local_sim_backup"),
            "snapshot_synced_at": str(payload.get("synced_at") or ""),
            **strategy_view,
            "capital_plan_sample_adjustment": sample_adjustment,
        }
        for field in (
            "position_source_status",
            "authority_id",
            "authority_generation",
            "execution_lineage_id",
            "authority_checksum",
            "trade_date",
            "position_count",
            "positions_fingerprint",
            "strategy_position_envelope",
        ):
            if field in payload:
                result[field] = payload[field]
        return _prepare_adapter_position_authority(result, trade_date)

    def _get_assets(self) -> list[dict[str, Any]]:
        get_assets = getattr(self.reader, "get_assets", None)
        if callable(get_assets):
            rows = get_assets(market=MARKET)
            if rows:
                return list(rows)
            rows = get_assets(market="Ashare")
            if rows:
                return list(rows)
        return []

    def _coverage_by_symbol(self, date: str) -> dict[str, str]:
        get_coverage = getattr(self.reader, "get_coverage", None)
        if not callable(get_coverage):
            return {}
        for market in (MARKET, "Ashare"):
            rows = get_coverage(market, date)
            if rows:
                return {
                    str(row.get("symbol") or ""): str(row.get("coverage_status") or "")
                    for row in rows
                    if row.get("symbol")
                }
        return {}

    def _latest_amount(self, symbol: str, date: str) -> float | None:
        has_close, amount = self._latest_liquidity(symbol, date)
        del has_close
        return amount

    def _latest_liquidity(self, symbol: str, date: str) -> tuple[bool, float | None]:
        get_bars = getattr(self.reader, "get_bars_daily", None)
        if not callable(get_bars):
            return False, None
        has_positive_close = False
        start_date = _lookback_start(date)
        for market in (MARKET, "Ashare"):
            rows = get_bars(market, symbol, start_date, date)
            if not rows:
                continue
            for row in reversed(rows):
                if _safe_float(row.get("close"), 0.0) <= 0.0:
                    continue
                has_positive_close = True
                amount_yuan = _daily_amount_to_yuan(row.get("amount"))
                if amount_yuan >= 0:
                    return True, amount_yuan
        return has_positive_close, None

    def _exclude_asset(
        self,
        asset: dict[str, Any],
        coverage_status: str | None,
        date: str,
        *,
        liquidity: tuple[bool, float | None] | None = None,
    ) -> bool:
        cfg = self.universe_filter
        if not str(asset.get("name") or "").strip():
            return True
        if cfg.get("exclude_st", True) and _is_st(asset):
            return True
        if cfg.get("exclude_delisted", True) and _is_delisted(asset):
            return True
        if cfg.get("exclude_suspended", True) and _is_suspended(asset, coverage_status):
            return True
        if cfg.get("exclude_bse", True) and _is_bse(asset):
            return True
        # Phase 0-3 范围门禁不可被配置放宽；配置只能继续收紧其它条件。
        if not is_mainboard_tradable(
            asset.get("symbol"),
            exchange=asset.get("exchange"),
            instrument_type=(
                asset.get("instrument_type")
                or asset.get("security_type")
                or asset.get("asset_type")
                or "common_stock"
            ),
        ):
            return True

        list_date = _parse_date(asset.get("list_date"))
        target_date = _parse_date(date)
        min_days = int(cfg.get("min_list_days", 30))
        if list_date is not None and target_date is not None:
            if (target_date - list_date).days < min_days:
                return True

        min_amount = _safe_float(cfg.get("min_liquidity_amount"), 50_000_000.0)
        has_close, amount = (
            liquidity
            if liquidity is not None
            else self._latest_liquidity(str(asset.get("symbol") or ""), date)
        )
        if not has_close:
            return True
        if amount is None:
            # A-share execution candidates need explicit liquidity evidence.
            # Keeping unknown-liquidity assets lets the pipeline fall back to
            # ordered asset-table samples, which is not a tradable signal.
            logger.warning(
                "_exclude_asset: no liquidity data for %s on %s — excluding from executable universe",
                asset.get("symbol"),
                date,
            )
            return True
        if amount < min_amount:
            return True
        return False

    def _load_strategies(self) -> dict[str, Any]:
        strategies: dict[str, Any] = {}
        if not self.strategy_dir.exists():
            return strategies
        for path in sorted(self.strategy_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = str(payload.get("name") or path.stem)
            strategies[name] = payload
        return strategies


__all__ = [
    "AshareAdapter",
    "DEFAULT_SAMPLE_JOURNAL_PATH",
    "MIN_STRATEGY_EXECUTION_SAMPLES",
    "SAMPLE_DEBT_POLICY_VERSION",
    "build_current_sample_adjustment",
]
