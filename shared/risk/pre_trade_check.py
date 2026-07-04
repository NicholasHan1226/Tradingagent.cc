#!/usr/bin/env python3
"""事前风控 — 下单前检查仓位/相关性/板块/流动性。

降权不硬拒, 仅单股 >15% 硬拒。

check(order, portfolio) → {approved, adjustments, reasons}
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

try:
    from Ashare import t_plus_1 as _t_plus_1
except ImportError:  # pragma: no cover
    _t_plus_1 = None  # type: ignore[assignment]

# 风控参数默认值 (与 risk_limits.yaml 对齐)
_DEFAULT_LIMITS: dict[str, Any] = {
    "single_stock_max": 0.15,
    "sector_max": 0.40,
    "total_exposure_max": 0.80,
    "daily_loss_limit": 0.03,
    "max_positions": 5,
    "correlation_threshold": 0.70,
    "liquidity": {
        "min_turnover_wan": 5000,
        "max_pct_of_volume": 0.05,
    },
    "market_rules": {
        "ashare": {
            "t_plus_1": True,
            "daily_loss_limit": 0.03,
            "limit_up_down": True,
            "pre_market_auction": True,
            "max_positions": 5,
        },
        "crypto": {
            "t_plus_1": False,
            "daily_loss_limit": 0.10,
            "24/7": True,
            "no_limit_up_down": True,
            "max_positions": 10,
        },
        "us": {
            "t_plus_2": True,
            "daily_loss_limit": 0.05,
            "PDT": True,
            "extended_hours": True,
            "max_positions": 10,
        },
        "pm": {
            "t_plus_N": "none",
            "daily_loss_limit": 0.05,
            "single_market_max": 0.20,
            "max_positions": 20,
        },
    },
}

_LIMITS_PATH = Path(__file__).resolve().parent / "risk_limits.yaml"
_MARKET_ALIASES = {
    "a": "ashare",
    "a-share": "ashare",
    "a_share": "ashare",
    "ashare": "ashare",
    "cn": "ashare",
    "china": "ashare",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "digital_asset": "crypto",
    "us": "us",
    "usa": "us",
    "u.s.": "us",
    "equity_us": "us",
    "pm": "pm",
    "prediction": "pm",
    "prediction_market": "pm",
}


def _load_limits() -> dict[str, Any]:
    """加载风控参数。yaml 不可用时回退默认值。"""
    if yaml is None:
        return dict(_DEFAULT_LIMITS)
    try:
        with open(_LIMITS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            # 合并默认值 (补齐缺失字段)
            merged = dict(_DEFAULT_LIMITS)
            merged.update(data)
            return merged
    except (OSError, yaml.YAMLError):
        pass
    return dict(_DEFAULT_LIMITS)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default  # NaN check
    except (TypeError, ValueError):
        return default


def _normalize_market(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "ashare"
    return _MARKET_ALIASES.get(raw, raw if raw in _DEFAULT_LIMITS["market_rules"] else "ashare")


def _market_rule(limits: dict[str, Any], market: str) -> dict[str, Any]:
    all_rules = limits.get("market_rules", {})
    defaults = _DEFAULT_LIMITS.get("market_rules", {})
    rule: dict[str, Any] = {}
    if isinstance(defaults, dict) and isinstance(defaults.get(market), dict):
        rule.update(defaults[market])
    if isinstance(all_rules, dict) and isinstance(all_rules.get(market), dict):
        rule.update(all_rules[market])
    if not rule and isinstance(defaults, dict):
        rule.update(defaults.get("ashare", {}))
    return rule


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    raw_value = str(value).strip()
    if not raw_value:
        return None
    if raw_value.isdigit() and len(raw_value) == 8:
        try:
            return datetime.strptime(raw_value, "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return date.fromisoformat(raw_value[:10])
    except ValueError:
        return None


def _order_trade_date(order: dict[str, Any]) -> date:
    for key in ("trade_date", "current_date", "as_of", "as_of_date", "date"):
        parsed = _parse_date(order.get(key))
        if parsed is not None:
            return parsed
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    try:
        import zoneinfo; bj = zoneinfo.ZoneInfo("Asia/Shanghai")
    except Exception:
        bj = _tz(_td(hours=8))
    return _dt.now(bj).date()


def _is_sell_order(order: dict[str, Any]) -> bool:
    return str(order.get("side", "") or "").strip().lower() in {"sell", "exit", "close", "reduce"}


def _is_buy_order(order: dict[str, Any]) -> bool:
    side = str(order.get("side", "buy") or "buy").strip().lower()
    return side in {"buy", "open", "add", "long"}


def _entry_date_from_order(
    order: dict[str, Any],
    positions: list[Any],
    ts_code: str,
) -> date | None:
    for key in ("position_open_date", "entry_date", "open_date", "buy_date", "filled_date"):
        parsed = _parse_date(order.get(key))
        if parsed is not None:
            return parsed
    for position in positions:
        if not isinstance(position, dict) or position.get("ts_code") != ts_code:
            continue
        for key in ("entry_date", "position_open_date", "open_date", "buy_date", "filled_date"):
            parsed = _parse_date(position.get(key))
            if parsed is not None:
                return parsed
    return None


def _fallback_next_trading_day(open_day: date) -> date:
    current = open_day + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def _can_sell_by_market(rule: dict[str, Any], entry_day: date | None, trade_day: date) -> bool:
    if entry_day is None:
        return False
    if rule.get("24/7") or str(rule.get("t_plus_N", "")).lower() == "none":
        return True
    if rule.get("t_plus_1"):
        if _t_plus_1 is not None:
            return bool(_t_plus_1.can_sell(entry_day.isoformat(), trade_day.isoformat()))
        return trade_day >= _fallback_next_trading_day(entry_day)
    if rule.get("t_plus_2"):
        return trade_day >= entry_day + timedelta(days=2)
    return True


def _market_exposure(
    market: str,
    positions: list[Any],
    portfolio: dict[str, Any],
) -> float:
    market_exposure = portfolio.get("market_exposure", {})
    if isinstance(market_exposure, dict) and market in market_exposure:
        return _safe_float(market_exposure.get(market))
    if f"{market}_exposure" in portfolio:
        return _safe_float(portfolio.get(f"{market}_exposure"))

    exposure = 0.0
    for position in positions:
        if not isinstance(position, dict):
            continue
        position_market = _normalize_market(position.get("market", market))
        if position_market == market:
            exposure += _safe_float(position.get("weight", position.get("market_weight", 0.0)))
    return exposure


def _blocked_by_price_limit(order: dict[str, Any], rule: dict[str, Any]) -> str:
    if not rule.get("limit_up_down") or rule.get("no_limit_up_down"):
        return ""

    price = _safe_float(order.get("price", order.get("limit_price", 0.0)))
    if _is_buy_order(order):
        limit_up_price = _safe_float(order.get("limit_up_price", 0.0))
        if order.get("at_limit_up") or order.get("limit_up_hit"):
            return "硬拒: 涨停约束, 禁止追买"
        if price > 0 and limit_up_price > 0 and price >= limit_up_price:
            return f"硬拒: 买入价格 {price:.4f} 已触及涨停价 {limit_up_price:.4f}"
    if _is_sell_order(order):
        limit_down_price = _safe_float(order.get("limit_down_price", 0.0))
        if order.get("at_limit_down") or order.get("limit_down_hit"):
            return "硬拒: 跌停约束, 禁止卖出"
        if price > 0 and limit_down_price > 0 and price <= limit_down_price:
            return f"硬拒: 卖出价格 {price:.4f} 已触及跌停价 {limit_down_price:.4f}"
    return ""


def _pdt_block_reason(
    order: dict[str, Any],
    portfolio: dict[str, Any],
    entry_day: date | None,
    trade_day: date,
    rule: dict[str, Any],
) -> str:
    if not rule.get("PDT") or not _is_sell_order(order):
        return ""

    day_trade = bool(order.get("day_trade") or order.get("would_day_trade"))
    if not day_trade and entry_day is not None:
        day_trade = entry_day == trade_day
    if not day_trade:
        return ""

    day_trades = int(_safe_float(
        order.get(
            "day_trades_5d",
            order.get(
                "day_trades_last_5_days",
                portfolio.get("day_trades_5d", portfolio.get("day_trades_last_5_days", 0)),
            ),
        )
    ))
    account_equity = _safe_float(
        order.get("account_equity", portfolio.get("account_equity", portfolio.get("equity", 0.0)))
    )
    pdt_min_equity = _safe_float(rule.get("pdt_min_equity", 25000.0), 25000.0)
    if day_trades >= 3 and account_equity < pdt_min_equity:
        return (
            f"硬拒: PDT 限制, 5日内日内交易 {day_trades} 次且权益 "
            f"{account_equity:.2f} < {pdt_min_equity:.2f}"
        )
    return ""


def check(order: dict[str, Any], portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    """事前风控检查。

    Args:
        order: {
            "ts_code": str,
            "weight": float,        # 目标权重 (0-1)
            "sector": str,          # 板块
            "turnover_wan": float,  # 日均成交额(万), 可选
            "order_amount_wan": float,  # 下单金额(万), 可选
        }
        portfolio: {
            "positions": [
                {"ts_code": str, "weight": float, "sector": str, "cost": float, ...}
            ],
            "total_exposure": float,
            "daily_pnl_pct": float,  # 当日盈亏比例
            "correlations": {("A", "B"): float},  # 可选
        }

    Returns:
        {
            "approved": bool,
            "adjusted_weight": float,  # 调整后权重
            "adjustments": list[str],  # 调整说明
            "reasons": list[str],      # 拒绝/降权原因
        }
    """
    if not order or not isinstance(order, dict):
        raise ValueError("order must be a non-empty dict")
    if not order.get("ts_code"):
        raise ValueError("order.ts_code is required")

    if portfolio is None:
        portfolio = {}
    positions = portfolio.get("positions", []) or []
    if not isinstance(positions, list):
        positions = []

    limits = _load_limits()
    market = _normalize_market(order.get("market"))
    market_rule = _market_rule(limits, market)
    ts_code = order["ts_code"]
    target_weight = _safe_float(order.get("weight", 0.0))
    sector = str(order.get("sector", "unknown"))
    adjustments: list[str] = []
    reasons: list[str] = []
    adjusted_weight = target_weight
    approved = True

    # --- 硬限: 单股 max 15% ---
    single_max = _safe_float(limits.get("single_stock_max", 0.15))
    # 检查该标的在组合中已有权重
    existing_weight = 0.0
    for p in positions:
        if isinstance(p, dict) and p.get("ts_code") == ts_code:
            existing_weight += _safe_float(p.get("weight", 0.0))

    new_total_single = existing_weight + target_weight
    if new_total_single > single_max + 1e-9:
        # 硬拒: 单股超限
        approved = False
        reasons.append(
            f"硬拒: 单股 {ts_code} 总权重 {new_total_single:.4f} > 单股上限 {single_max:.4f}"
        )
        return {
            "approved": False,
            "adjusted_weight": 0.0,
            "adjustments": [],
            "reasons": reasons,
            "market": market,
        }

    limit_block = _blocked_by_price_limit(order, market_rule)
    if limit_block:
        reasons.append(limit_block)
        return {
            "approved": False,
            "adjusted_weight": 0.0,
            "adjustments": [],
            "reasons": reasons,
            "market": market,
        }

    # --- 软限: 板块 max 40% ---
    sector_max = _safe_float(limits.get("sector_max", 0.40))
    sector_exposure = existing_weight  # 同标的已有
    for p in positions:
        if isinstance(p, dict) and p.get("sector") == sector and p.get("ts_code") != ts_code:
            sector_exposure += _safe_float(p.get("weight", 0.0))

    new_sector_total = sector_exposure + target_weight
    if new_sector_total > sector_max + 1e-9:
        # 降权: 截断到板块上限
        allowed = max(0.0, sector_max - sector_exposure)
        if allowed < target_weight:
            adjustments.append(
                f"板块降权: {sector} 总敞口 {new_sector_total:.4f} > {sector_max:.4f}, "
                f"权重 {target_weight:.4f} → {allowed:.4f}"
            )
            adjusted_weight = allowed

    # --- 软限: 总敞口 max 80% ---
    total_max = _safe_float(limits.get("total_exposure_max", 0.80))
    current_exposure = _safe_float(portfolio.get("total_exposure", 0.0))
    new_total_exposure = current_exposure + adjusted_weight
    if new_total_exposure > total_max + 1e-9:
        allowed = max(0.0, total_max - current_exposure)
        if allowed < adjusted_weight:
            adjustments.append(
                f"总敞口降权: {new_total_exposure:.4f} > {total_max:.4f}, "
                f"权重 {adjusted_weight:.4f} → {allowed:.4f}"
            )
            adjusted_weight = allowed

    # --- 硬限: PM 单市场敞口 ---
    single_market_max = market_rule.get("single_market_max")
    if single_market_max is not None:
        market_max = _safe_float(single_market_max)
        current_market_exposure = _market_exposure(market, positions, portfolio)
        new_market_exposure = current_market_exposure + target_weight
        if market_max > 0 and new_market_exposure > market_max + 1e-9:
            approved = False
            reasons.append(
                f"硬拒: {market} 市场敞口 {new_market_exposure:.4f} > 单市场上限 {market_max:.4f}"
            )
            return {
                "approved": False,
                "adjusted_weight": 0.0,
                "adjustments": [],
                "reasons": reasons,
                "market": market,
            }

    # --- 软限: 持仓数 ---
    max_positions = int(market_rule.get("max_positions", limits.get("max_positions", 5)))
    # 已持有的不同标的数
    existing_codes = set()
    for p in positions:
        if isinstance(p, dict) and p.get("ts_code"):
            existing_codes.add(p["ts_code"])
    if ts_code not in existing_codes and len(existing_codes) >= max_positions:
        approved = False
        reasons.append(
            f"硬拒: 持仓数 {len(existing_codes)}已达上限 {max_positions}, 新增 {ts_code} 被拒"
        )
        return {
            "approved": False,
            "adjusted_weight": 0.0,
            "adjustments": [],
            "reasons": reasons,
            "market": market,
        }

    trade_day = _order_trade_date(order)
    entry_day = _entry_date_from_order(order, positions, ts_code)

    # --- 硬限: T+1/T+2 卖出窗口 ---
    if _is_sell_order(order) and not _can_sell_by_market(market_rule, entry_day, trade_day):
        label = "T+1" if market_rule.get("t_plus_1") else "T+2" if market_rule.get("t_plus_2") else "settlement"
        approved = False
        reasons.append(f"硬拒: {market} {label} 规则禁止当前卖出")
        return {
            "approved": False,
            "adjusted_weight": 0.0,
            "adjustments": [],
            "reasons": reasons,
            "market": market,
        }

    pdt_block = _pdt_block_reason(order, portfolio, entry_day, trade_day, market_rule)
    if pdt_block:
        reasons.append(pdt_block)
        return {
            "approved": False,
            "adjusted_weight": 0.0,
            "adjustments": [],
            "reasons": reasons,
            "market": market,
        }

    # --- 软限: 分市场日亏 → 暂停新增 ---
    daily_loss_limit = _safe_float(market_rule.get("daily_loss_limit", limits.get("daily_loss_limit", 0.03)))
    daily_pnl = _safe_float(portfolio.get("daily_pnl_pct", 0.0))
    if daily_pnl < -daily_loss_limit:
        if ts_code not in existing_codes:
            approved = False
            reasons.append(
                f"暂停新增: 当日亏损 {daily_pnl:.4f} < -{daily_loss_limit:.4f}, 禁止开新仓"
            )
            return {
                "approved": False,
                "adjusted_weight": 0.0,
                "adjustments": [],
                "reasons": reasons,
                "market": market,
            }
        else:
            adjustments.append(
                f"日亏警告: 当日亏损 {daily_pnl:.4f}, 仅允许已有持仓调整"
            )

    # --- 软限: 相关性 ---
    corr_threshold = _safe_float(limits.get("correlation_threshold", 0.70))
    correlations = portfolio.get("correlations", {})
    if isinstance(correlations, dict):
        for pair_key, corr_val in correlations.items():
            # pair_key 可能是 "A|B" 或 tuple
            if isinstance(pair_key, str):
                parts = pair_key.split("|")
            elif isinstance(pair_key, (list, tuple)):
                parts = list(pair_key)
            else:
                continue
            if len(parts) == 2 and ts_code in parts:
                corr = _safe_float(corr_val)
                if abs(corr) > corr_threshold:
                    # 高相关按 multiplicative 方式累计降权, 避免后一个覆盖前一个
                    reduction = 0.20
                    new_w = adjusted_weight * (1.0 - reduction)
                    adjustments.append(
                        f"相关性降权: {ts_code} 与 {parts} 相关性 {corr:.3f} > {corr_threshold:.3f}, "
                        f"权重 {adjusted_weight:.4f} → {new_w:.4f}"
                    )
                    adjusted_weight = new_w

    # --- 软限: 流动性 ---
    liq = limits.get("liquidity", {})
    if isinstance(liq, dict):
        min_turnover = _safe_float(liq.get("min_turnover_wan", 5000))
        turnover = _safe_float(order.get("turnover_wan", 0.0))
        if turnover > 0 and turnover < min_turnover:
            # 流动性不足降权 30%
            new_w = adjusted_weight * 0.7
            adjustments.append(
                f"流动性降权: {ts_code} 日均成交 {turnover:.0f}万 < {min_turnover:.0f}万, "
                f"权重 {adjusted_weight:.4f} → {new_w:.4f}"
            )
            adjusted_weight = new_w

        max_pct_vol = _safe_float(liq.get("max_pct_of_volume", 0.05))
        order_amount = _safe_float(order.get("order_amount_wan", 0.0))
        if turnover > 0 and order_amount > 0:
            pct_vol = order_amount / turnover
            if pct_vol > max_pct_vol:
                # 单笔占比过高, 降权
                scale = max_pct_vol / pct_vol
                new_w = adjusted_weight * scale
                adjustments.append(
                    f"流动性降权: 单笔占比 {pct_vol:.3f} > {max_pct_vol:.3f}, "
                    f"权重 {adjusted_weight:.4f} → {new_w:.4f}"
                )
                adjusted_weight = new_w

    # 权重不能为负
    adjusted_weight = max(0.0, adjusted_weight)

    if not adjustments and adjusted_weight == target_weight:
        adjustments.append(f"通过: 权重 {target_weight:.4f} 无需调整")

    return {
        "approved": approved,
        "adjusted_weight": round(adjusted_weight, 6),
        "adjustments": adjustments,
        "reasons": reasons,
        "market": market,
    }


def pre_trade_check(
    order: dict[str, Any],
    portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """兼容显式 pre_trade_check(order) 入口。

    若调用方未传 portfolio, 允许从 order["portfolio"] 读取; market 缺失时由 check()
    保守回落到 ashare 规则。
    """
    if portfolio is None and isinstance(order, dict) and isinstance(order.get("portfolio"), dict):
        portfolio = order.get("portfolio")
    return check(order, portfolio)


if __name__ == "__main__":
    import json
    test_order = {"ts_code": "600519.SH", "weight": 0.12, "sector": "白酒", "turnover_wan": 30000}
    test_portfolio = {
        "positions": [
            {"ts_code": "000858.SZ", "weight": 0.10, "sector": "白酒"},
            {"ts_code": "601318.SH", "weight": 0.15, "sector": "保险"},
        ],
        "total_exposure": 0.25,
        "daily_pnl_pct": -0.01,
    }
    r = check(test_order, test_portfolio)
    print(json.dumps(r, ensure_ascii=False, indent=2))
