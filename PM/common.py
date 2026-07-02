#!/usr/bin/env python3
"""PM Phase D common configuration — probability-domain market tool base.

This module is shadow/simulated only. It never enables real-money execution,
live brokers, or direct order routing. All probabilities are clamped [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from shared.markets.config_schema import (
    CapitalConfig,
    DataConfig,
    FeesConfig,
    MarketToolConfig,
    PromotionConfig,
    ReportingConfig,
    RiskConfig,
    SafetyConfig,
    SessionConfig,
    UniverseConfig,
    validate_market_config,
)
from shared.markets.safety import assert_no_real_execution, assert_public_data_only


# -- Probability helpers -------------------------------------------------------


def clamp_probability(value: float) -> float:
    """Clamp a value to the valid probability range [0, 1]."""
    return max(0.0, min(1.0, float(value)))


def assert_probability(value: Any, label: str = "value") -> float:
    """Validate and clamp a probability value. Raises on non-numeric input."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric, got {value!r}") from exc
    if f != f:  # NaN
        raise ValueError(f"{label} must not be NaN")
    return clamp_probability(f)


# -- PMConfig ------------------------------------------------------------------


@dataclass(frozen=True)
class PMConfig:
    """PM Phase D market-tool configuration.

    Immutable, validated at construction time. Always shadow/simulated only.
    Currency is USDC (Polymarket's native unit), not fiat USD.
    """

    market: str = "pm"
    capital: CapitalConfig = field(
        default_factory=lambda: CapitalConfig(
            default_layer="shadow",
            allowed_layers=("shadow", "simulated"),
            initial_capital=50000.0,
            currency="USDC",
        )
    )
    safety: SafetyConfig = field(
        default_factory=lambda: SafetyConfig(
            real_money_enabled=False,
            live_broker_enabled=False,
            direct_execution_enabled=False,
        )
    )
    data: DataConfig = field(
        default_factory=lambda: DataConfig(
            reader="shared.data.reader.TradingagentDataReader",
            daily_table="market_pm_prices",
            intraday_table="market_pm_prices",
            events_table="market_events",
        )
    )
    universe: UniverseConfig = field(
        default_factory=lambda: UniverseConfig(
            max_symbols=80,
            min_close=0.01,
            active_only=True,
        )
    )
    session: SessionConfig = field(
        default_factory=lambda: SessionConfig(
            timezone="UTC",
            type="24x7",
        )
    )
    risk: RiskConfig = field(
        default_factory=lambda: RiskConfig(
            max_positions=20,
            max_single_position_pct=0.10,
        )
    )
    fees: FeesConfig = field(
        default_factory=lambda: FeesConfig(
            taker_bps=0,
            maker_bps=0,
        )
    )
    reporting: ReportingConfig = field(
        default_factory=lambda: ReportingConfig(
            daily_report_path="shared/review/pm/daily",
            notify_on_trigger_only=True,
        )
    )
    promotion: PromotionConfig = field(
        default_factory=lambda: PromotionConfig(
            min_shadow_trades=50,
            min_positive_days_pct=0.55,
        )
    )

    def __post_init__(self) -> None:
        """Ensure market is 'pm' and currency is USDC."""
        if self.market != "pm":
            raise ValueError(f"PMConfig.market must be 'pm', got {self.market!r}")
        if self.capital.currency != "USDC":
            raise ValueError(
                f"PMConfig capital currency must be 'USDC', got {self.capital.currency!r}"
            )

    def to_market_tool_config(self) -> MarketToolConfig:
        """Convert to the shared MarketToolConfig for use with base classes."""
        return MarketToolConfig(
            market=self.market,
            capital=self.capital,
            safety=self.safety,
            data=self.data,
            universe=self.universe,
            session=self.session,
            risk=self.risk,
            fees=self.fees,
            reporting=self.reporting,
            promotion=self.promotion,
        )

    def validate(self) -> None:
        """Run full validation including safety assertions."""
        config = self.to_market_tool_config()
        validate_market_config(config)
        assert_public_data_only(config)
        assert_no_real_execution(config)


def load_pm_config(root: Path | str | None = None) -> PMConfig:
    """Load PM configuration from PM/config.yaml or defaults.

    Falls back to YAML-backed config if the file exists; otherwise uses
    the safe PMConfig defaults that guarantee shadow/simulated-only mode.
    """
    import os

    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    config_path = base / "PM" / "config.yaml"

    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"PM config must be a mapping: {config_path}")

        capital = _build_capital(raw)
        safety = _build_safety(raw)
        data = _build_data(raw)
        universe = _build_universe(raw)
        session = _build_session(raw)
        risk = _build_risk(raw)
        fees = _build_fees(raw)
        reporting = _build_reporting(raw)
        promotion = _build_promotion(raw)

        cfg = PMConfig(
            market="pm",
            capital=capital,
            safety=safety,
            data=data,
            universe=universe,
            session=session,
            risk=risk,
            fees=fees,
            reporting=reporting,
            promotion=promotion,
        )
    else:
        # Production-safe defaults: no config file = strict shadow/simulated-only.
        cfg = PMConfig()

    cfg.validate()
    return cfg


def _build_capital(raw: dict[str, Any]) -> CapitalConfig:
    cap = raw.get("capital", {}) or {}
    if isinstance(cap, dict):
        allowed = cap.get("allowed_layers", ("shadow", "simulated"))
        if isinstance(allowed, list):
            allowed = tuple(allowed)
        return CapitalConfig(
            default_layer=str(cap.get("default_layer", "shadow")),
            allowed_layers=tuple(allowed),
            initial_capital=float(cap.get("initial_capital", 50000.0)),
            currency=str(cap.get("currency", "USDC")),
        )
    return CapitalConfig()


def _build_safety(raw: dict[str, Any]) -> SafetyConfig:
    saf = raw.get("safety", {}) or {}
    if isinstance(saf, dict):
        return SafetyConfig(
            real_money_enabled=bool(saf.get("real_money_enabled", False)),
            live_broker_enabled=bool(saf.get("live_broker_enabled", False)),
            direct_execution_enabled=bool(saf.get("direct_execution_enabled", False)),
        )
    return SafetyConfig()


def _build_data(raw: dict[str, Any]) -> DataConfig:
    dat = raw.get("data", {}) or {}
    if isinstance(dat, dict):
        return DataConfig(
            reader=str(dat.get("reader", "shared.data.reader.TradingagentDataReader")),
            daily_table=str(dat.get("daily_table", "market_pm_prices")),
            intraday_table=str(dat.get("intraday_table", "market_pm_prices")),
            events_table=str(dat.get("events_table", "market_events")),
        )
    return DataConfig()


def _build_universe(raw: dict[str, Any]) -> UniverseConfig:
    uni = raw.get("universe", {}) or {}
    if isinstance(uni, dict):
        return UniverseConfig(
            max_symbols=int(uni.get("max_symbols", 80)),
            min_close=float(uni.get("min_close", 0.01)),
            active_only=bool(uni.get("active_only", True)),
        )
    return UniverseConfig()


def _build_session(raw: dict[str, Any]) -> SessionConfig:
    sess = raw.get("session", {}) or {}
    if isinstance(sess, dict):
        return SessionConfig(
            timezone=str(sess.get("timezone", "UTC")),
            type=str(sess.get("type", "24x7")),
        )
    return SessionConfig()


def _build_risk(raw: dict[str, Any]) -> RiskConfig:
    rsk = raw.get("risk", {}) or {}
    if isinstance(rsk, dict):
        return RiskConfig(
            max_positions=int(rsk.get("max_positions", 20)),
            max_single_position_pct=float(rsk.get("max_single_position_pct", 0.10)),
        )
    return RiskConfig()


def _build_fees(raw: dict[str, Any]) -> FeesConfig:
    f = raw.get("fees", {}) or {}
    if isinstance(f, dict):
        return FeesConfig(
            taker_bps=float(f.get("taker_bps", 0)),
            maker_bps=float(f.get("maker_bps", 0)),
        )
    return FeesConfig()


def _build_reporting(raw: dict[str, Any]) -> ReportingConfig:
    rep = raw.get("reporting", {}) or {}
    if isinstance(rep, dict):
        return ReportingConfig(
            daily_report_path=str(rep.get("daily_report_path", "shared/review/pm/daily")),
            notify_on_trigger_only=bool(rep.get("notify_on_trigger_only", True)),
        )
    return ReportingConfig()


def _build_promotion(raw: dict[str, Any]) -> PromotionConfig:
    prom = raw.get("promotion", {}) or {}
    if isinstance(prom, dict):
        return PromotionConfig(
            min_shadow_trades=int(prom.get("min_shadow_trades", 50)),
            min_positive_days_pct=float(prom.get("min_positive_days_pct", 0.55)),
        )
    return PromotionConfig()


__all__ = [
    "PMConfig",
    "assert_probability",
    "clamp_probability",
    "load_pm_config",
]
