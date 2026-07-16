"""Validated per-market policy — Nicholas fresh-start approved.

Pinned decision: nicholas-fresh-start-019f5040-20260712
Source thread: 019f5040-76a7-7672-b2fc-91c1526312bf
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

POLICY_DIR = Path(__file__).resolve().parent
POLICY_FILENAMES: dict[str, str] = {
    "ashare": "ashare_capital_policy.yaml",
    "cn_futures": "cn_futures_capital_policy.yaml",
}
ALLOWED_MARKETS = frozenset(POLICY_FILENAMES)
CANONICAL_AUTHORITY_GENERATION = 1
CANONICAL_INITIAL_EQUITY_CNY = 50_000.0
CANONICAL_SINGLE_NAME_MAX_PCT = 0.15
CANONICAL_STOCK_GROSS_EXPOSURE_LIMIT_PCT = 0.90
CANONICAL_ASHARE_MAX_POSITIONS = 8
CANONICAL_ASHARE_BUY_LOT_SIZE_SHARES = 100
CANONICAL_ASHARE_MINIMUM_ECONOMIC_ORDER_CNY = 2_000.0
CANONICAL_ASHARE_NO_TRADE_BAND_CNY = 1_000.0
CANONICAL_MARGIN_UTILIZATION_LIMIT_PCT = 0.50
CANONICAL_DAILY_LOSS_PAUSE_PCT = 0.03
CANONICAL_DRAWDOWN_TIGHTEN_PCT = 0.05
CANONICAL_DRAWDOWN_TIGHTEN_RISK_MULTIPLIER = 0.75
CANONICAL_DRAWDOWN_HALT_PCT = 0.07
CANONICAL_MAX_CONSECUTIVE_LOSSES = 3
REQUIRED_CUTOVER_STATE = "fresh_start_approved"
PINNED_CUTOVER_DECISION_ID = "nicholas-fresh-start-019f5040-20260712"
PINNED_SOURCE_THREAD_ID = "019f5040-76a7-7672-b2fc-91c1526312bf"


class MarketPolicyError(ValueError):
    """Raised when policy is invalid."""


def _mapping(v: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise MarketPolicyError(f"invalid_{field}")
    return v


def _number(v: object, *, field: str, allow_zero: bool = True) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise MarketPolicyError(f"invalid_{field}")
    r = float(v)
    if not math.isfinite(r) or (r < 0.0 if allow_zero else r <= 0.0):
        raise MarketPolicyError(f"invalid_{field}")
    return r


def _integer(v: object, *, field: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise MarketPolicyError(f"invalid_{field}")
    return v


def _str_nonempty(v: object, *, field: str) -> str:
    r = str(v or "").strip()
    if not r:
        raise MarketPolicyError(f"missing_{field}")
    return r


@dataclass(frozen=True)
class MarketPolicy:
    schema_version: str
    capital_authority_id: str
    authority_generation: int
    account_name: str
    market: str
    currency: str
    cutover_state: str
    cutover_decision_id: str
    source_thread_id: str
    initial_equity_cny: float
    single_name_max_pct: float | None
    stock_gross_exposure_limit_pct: float | None
    margin_utilization_limit_pct: float | None
    max_positions: int | None
    buy_lot_size_shares: int | None
    minimum_economic_order_cny: float | None
    no_trade_band_cny: float | None
    daily_loss_pause_pct: float
    drawdown_tighten_pct: float
    drawdown_tighten_risk_multiplier: float
    drawdown_halt_pct: float
    max_consecutive_losses: int
    capital_layer: str
    real_trading_enabled: bool

    @property
    def single_name_cap_cny(self) -> float:
        return (
            round(self.initial_equity_cny * self.single_name_max_pct, 6)
            if self.single_name_max_pct
            else 0.0
        )

    @property
    def stock_gross_exposure_limit_cny(self) -> float:
        return (
            round(self.initial_equity_cny * self.stock_gross_exposure_limit_pct, 6)
            if self.stock_gross_exposure_limit_pct
            else 0.0
        )

    @property
    def margin_utilization_limit_cny(self) -> float:
        return (
            round(self.initial_equity_cny * self.margin_utilization_limit_pct, 6)
            if self.margin_utilization_limit_pct
            else 0.0
        )

    @classmethod
    def load(cls, market: str, *, path: str | Path | None = None) -> "MarketPolicy":
        mk = _normalize_market(market)
        if mk not in ALLOWED_MARKETS:
            raise MarketPolicyError(f"unsupported_market:{mk or 'empty'}")
        src = Path(path) if path else POLICY_DIR / POLICY_FILENAMES[mk]
        src = src.expanduser()
        if src.is_symlink():
            raise MarketPolicyError("market_capital_policy_symlink_not_allowed")
        try:
            raw = yaml.safe_load(src.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise MarketPolicyError("market_capital_policy_unreadable") from e
        p = _mapping(raw, field="policy")
        risk = _mapping(p.get("risk"), field="risk")
        exe = _mapping(p.get("execution"), field="execution")

        if str(p.get("schema_version") or "") != "market-capital-policy.v1":
            raise MarketPolicyError("unsupported_schema")

        caid = _str_nonempty(
            p.get("capital_authority_id"), field="capital_authority_id"
        )
        if mk == "ashare" and caid != "ashare-capital-v1":
            raise MarketPolicyError("invalid_capital_authority_id")
        if mk == "cn_futures" and caid != "cn-futures-capital-v1":
            raise MarketPolicyError("invalid_capital_authority_id")

        ag = p.get("authority_generation")
        if (
            not isinstance(ag, int)
            or isinstance(ag, bool)
            or ag != CANONICAL_AUTHORITY_GENERATION
        ):
            raise MarketPolicyError("unsupported_authority_generation")

        cs = str(p.get("cutover_state") or "").strip()
        if cs != REQUIRED_CUTOVER_STATE:
            raise MarketPolicyError(f"cutover_state_must_be_{REQUIRED_CUTOVER_STATE}")

        cdi = str(p.get("cutover_decision_id") or "").strip()
        if cdi != PINNED_CUTOVER_DECISION_ID:
            raise MarketPolicyError("pinned_cutover_decision_id_mismatch")

        sti = str(p.get("source_thread_id") or "").strip()
        if sti != PINNED_SOURCE_THREAD_ID:
            raise MarketPolicyError("pinned_source_thread_id_mismatch")

        an = str(p.get("account_name") or "").strip()
        if not an:
            raise MarketPolicyError("missing_account_name")
        if "account_epoch" in p:
            raise MarketPolicyError("account_epoch_removed")
        if "margin_limit_cny" in p:
            raise MarketPolicyError("margin_limit_cny_removed")

        dm = str(p.get("market") or "").strip().lower().replace("-", "_")
        if dm != mk:
            raise MarketPolicyError("market_field_mismatch")
        if str(p.get("currency") or "") != "CNY":
            raise MarketPolicyError("currency_must_be_cny")

        ie = _number(
            p.get("initial_equity_cny"), field="initial_equity_cny", allow_zero=False
        )
        if ie != CANONICAL_INITIAL_EQUITY_CNY:
            raise MarketPolicyError("initial_equity_must_equal_50000")

        snmp: float | None = None
        gelp: float | None = None
        mulp: float | None = None
        max_positions: int | None = None
        buy_lot_size_shares: int | None = None
        minimum_economic_order_cny: float | None = None
        no_trade_band_cny: float | None = None
        if mk == "ashare":
            snmp = _number(
                p.get("single_name_max_pct"),
                field="single_name_max_pct",
                allow_zero=False,
            )
            if snmp != CANONICAL_SINGLE_NAME_MAX_PCT:
                raise MarketPolicyError("single_name_limit_15pct")
            gelp = _number(
                p.get("stock_gross_exposure_limit_pct"),
                field="stock_gross_exposure_limit_pct",
                allow_zero=False,
            )
            if gelp != CANONICAL_STOCK_GROSS_EXPOSURE_LIMIT_PCT:
                raise MarketPolicyError("gross_exposure_limit_90pct")
            if p.get("margin_utilization_limit_pct") is not None:
                raise MarketPolicyError("ashare_no_margin_pct")
            portfolio = _mapping(p.get("portfolio"), field="portfolio")
            max_positions = _integer(
                portfolio.get("max_positions"), field="max_positions"
            )
            if max_positions != CANONICAL_ASHARE_MAX_POSITIONS:
                raise MarketPolicyError("ashare_max_positions_8")
            buy_lot_size_shares = _integer(
                portfolio.get("buy_lot_size_shares"),
                field="buy_lot_size_shares",
            )
            if buy_lot_size_shares != CANONICAL_ASHARE_BUY_LOT_SIZE_SHARES:
                raise MarketPolicyError("ashare_buy_lot_size_100")
            minimum_economic_order_cny = _number(
                portfolio.get("minimum_economic_order_cny"),
                field="minimum_economic_order_cny",
                allow_zero=False,
            )
            if (
                minimum_economic_order_cny
                != CANONICAL_ASHARE_MINIMUM_ECONOMIC_ORDER_CNY
            ):
                raise MarketPolicyError("ashare_minimum_economic_order_2000")
            no_trade_band_cny = _number(
                portfolio.get("no_trade_band_cny"),
                field="no_trade_band_cny",
                allow_zero=False,
            )
            if no_trade_band_cny != CANONICAL_ASHARE_NO_TRADE_BAND_CNY:
                raise MarketPolicyError("ashare_no_trade_band_1000")
            if minimum_economic_order_cny < no_trade_band_cny:
                raise MarketPolicyError("minimum_order_must_cover_no_trade_band")
        else:
            if p.get("portfolio") is not None:
                raise MarketPolicyError("cn_no_ashare_portfolio")
            mulp = _number(
                p.get("margin_utilization_limit_pct"),
                field="margin_utilization_limit_pct",
                allow_zero=False,
            )
            if mulp != CANONICAL_MARGIN_UTILIZATION_LIMIT_PCT:
                raise MarketPolicyError("margin_utilization_limit_50pct")
            if p.get("single_name_max_pct") is not None:
                raise MarketPolicyError("cn_no_single_name")
            if p.get("stock_gross_exposure_limit_pct") is not None:
                raise MarketPolicyError("cn_no_gross_exposure")

        for k in (
            "protected_cash_reserve_cny",
            "allocations",
            "ashare_notional_limit_cny",
            "cn_futures_margin_limit_cny",
        ):
            if k in p:
                raise MarketPolicyError(f"cross_market_field:{k}")

        dl = _number(
            risk.get("daily_loss_pause_pct"),
            field="daily_loss_pause_pct",
            allow_zero=False,
        )
        if dl != CANONICAL_DAILY_LOSS_PAUSE_PCT:
            raise MarketPolicyError("daily_loss_3pct")
        dt = _number(
            risk.get("drawdown_tighten_pct"),
            field="drawdown_tighten_pct",
            allow_zero=False,
        )
        if dt != CANONICAL_DRAWDOWN_TIGHTEN_PCT:
            raise MarketPolicyError("drawdown_tighten_5pct")
        dh = _number(
            risk.get("drawdown_halt_pct"), field="drawdown_halt_pct", allow_zero=False
        )
        if dh != CANONICAL_DRAWDOWN_HALT_PCT:
            raise MarketPolicyError("drawdown_halt_7pct")
        all_pcts = [v for v in (snmp, gelp, mulp, dl, dt, dh) if v is not None]
        if any(v > 1.0 for v in all_pcts):
            raise MarketPolicyError("pct_out_of_range")
        ml = _integer(
            risk.get("max_consecutive_losses"), field="max_consecutive_losses"
        )
        if ml != CANONICAL_MAX_CONSECUTIVE_LOSSES:
            raise MarketPolicyError("max_consecutive_losses_3")

        if str(exe.get("capital_layer") or "") != "simulated":
            raise MarketPolicyError("capital_layer_simulated")
        if exe.get("real_trading_enabled") is not False:
            raise MarketPolicyError("real_trading_disabled")

        return cls(
            schema_version="market-capital-policy.v1",
            capital_authority_id=caid,
            authority_generation=ag,
            account_name=an,
            market=mk,
            currency="CNY",
            cutover_state=cs,
            cutover_decision_id=cdi,
            source_thread_id=sti,
            initial_equity_cny=ie,
            single_name_max_pct=snmp,
            stock_gross_exposure_limit_pct=gelp,
            margin_utilization_limit_pct=mulp,
            max_positions=max_positions,
            buy_lot_size_shares=buy_lot_size_shares,
            minimum_economic_order_cny=minimum_economic_order_cny,
            no_trade_band_cny=no_trade_band_cny,
            daily_loss_pause_pct=dl,
            drawdown_tighten_pct=dt,
            drawdown_tighten_risk_multiplier=CANONICAL_DRAWDOWN_TIGHTEN_RISK_MULTIPLIER,
            drawdown_halt_pct=dh,
            max_consecutive_losses=ml,
            capital_layer="simulated",
            real_trading_enabled=False,
        )


def _normalize_market(v: str) -> str:
    return str(v or "").strip().lower().replace("-", "_")


__all__ = [
    "ALLOWED_MARKETS",
    "CANONICAL_ASHARE_BUY_LOT_SIZE_SHARES",
    "CANONICAL_ASHARE_MAX_POSITIONS",
    "CANONICAL_ASHARE_MINIMUM_ECONOMIC_ORDER_CNY",
    "CANONICAL_ASHARE_NO_TRADE_BAND_CNY",
    "CANONICAL_INITIAL_EQUITY_CNY",
    "CANONICAL_SINGLE_NAME_MAX_PCT",
    "CANONICAL_STOCK_GROSS_EXPOSURE_LIMIT_PCT",
    "MarketPolicy",
    "MarketPolicyError",
    "POLICY_DIR",
    "POLICY_FILENAMES",
    "REQUIRED_CUTOVER_STATE",
    "PINNED_CUTOVER_DECISION_ID",
    "PINNED_SOURCE_THREAD_ID",
]
