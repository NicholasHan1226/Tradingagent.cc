#!/usr/bin/env python3
"""Shared market-tool configuration schema for shadow/simulated markets."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CapitalConfig:
    default_layer: str = "shadow"
    allowed_layers: tuple[str, ...] = ("shadow", "simulated")
    initial_capital: float = 0.0
    # Native account currency must be declared by each market lane.  A shared
    # USD default would silently mislabel A-share/CNFutures balances.
    currency: str = ""


@dataclass(frozen=True)
class SafetyConfig:
    real_money_enabled: bool = False
    live_broker_enabled: bool = False
    direct_execution_enabled: bool = False


@dataclass(frozen=True)
class DataConfig:
    reader: str = "tradingdatas_v1_catalog_query"
    daily_table: str = "market_bars_daily"
    intraday_table: str = "market_bars_intraday"
    events_table: str = "market_events"


@dataclass(frozen=True)
class UniverseConfig:
    max_symbols: int = 50
    min_close: float = 0.0
    active_only: bool = True


@dataclass(frozen=True)
class SessionConfig:
    timezone: str = "UTC"
    type: str = "regular"


@dataclass(frozen=True)
class RiskConfig:
    max_positions: int = 10
    max_single_position_pct: float = 0.15


@dataclass(frozen=True)
class FeesConfig:
    taker_bps: float = 0.0
    maker_bps: float = 0.0


@dataclass(frozen=True)
class ReportingConfig:
    daily_report_path: str = "shared/review/daily"
    notify_on_trigger_only: bool = True


@dataclass(frozen=True)
class PromotionConfig:
    min_shadow_trades: int = 30
    min_positive_days_pct: float = 0.55


def _coerce_dataclass(cls: type, value: Any):
    if isinstance(value, cls):
        return value
    if value is None:
        return cls()
    if not isinstance(value, dict):
        raise TypeError(f"{cls.__name__} must be built from a mapping")
    if cls is CapitalConfig and "allowed_layers" in value:
        value = {**value, "allowed_layers": tuple(value["allowed_layers"])}
    return cls(**value)


@dataclass(frozen=True)
class MarketToolConfig:
    market: str
    capital: CapitalConfig | dict[str, Any] = field(default_factory=CapitalConfig)
    safety: SafetyConfig | dict[str, Any] = field(default_factory=SafetyConfig)
    data: DataConfig | dict[str, Any] = field(default_factory=DataConfig)
    universe: UniverseConfig | dict[str, Any] = field(default_factory=UniverseConfig)
    session: SessionConfig | dict[str, Any] = field(default_factory=SessionConfig)
    risk: RiskConfig | dict[str, Any] = field(default_factory=RiskConfig)
    fees: FeesConfig | dict[str, Any] = field(default_factory=FeesConfig)
    reporting: ReportingConfig | dict[str, Any] = field(default_factory=ReportingConfig)
    promotion: PromotionConfig | dict[str, Any] = field(default_factory=PromotionConfig)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capital", _coerce_dataclass(CapitalConfig, self.capital)
        )
        object.__setattr__(self, "safety", _coerce_dataclass(SafetyConfig, self.safety))
        object.__setattr__(self, "data", _coerce_dataclass(DataConfig, self.data))
        object.__setattr__(
            self, "universe", _coerce_dataclass(UniverseConfig, self.universe)
        )
        object.__setattr__(
            self, "session", _coerce_dataclass(SessionConfig, self.session)
        )
        object.__setattr__(self, "risk", _coerce_dataclass(RiskConfig, self.risk))
        object.__setattr__(self, "fees", _coerce_dataclass(FeesConfig, self.fees))
        object.__setattr__(
            self, "reporting", _coerce_dataclass(ReportingConfig, self.reporting)
        )
        object.__setattr__(
            self, "promotion", _coerce_dataclass(PromotionConfig, self.promotion)
        )


def _config_path_for_market(market: str, root: Path) -> Path:
    candidates = [
        root / market / "config.yaml",
        root / market.lower() / "config.yaml",
        root / market.upper() / "config.yaml",
        root / market.capitalize() / "config.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path
    for child in root.iterdir() if root.exists() else []:
        if child.is_dir() and child.name.lower() == market.lower():
            path = child / "config.yaml"
            if path.exists():
                return path
    return candidates[0]


def load_market_config(market: str, root: Path | str | None = None) -> MarketToolConfig:
    """Load ``<market>/config.yaml`` into a validated MarketToolConfig."""

    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    path = _config_path_for_market(market, base)
    if not path.exists():
        raise FileNotFoundError(f"market config not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"market config must be a mapping: {path}")
    payload.setdefault("market", market.lower())
    config = MarketToolConfig(**payload)
    validate_market_config(config)
    return config


def validate_market_config(config: MarketToolConfig) -> None:
    """Validate values that protect market tools from unsafe defaults."""

    if not str(config.market).strip():
        raise ValueError("market is required")
    if config.safety.real_money_enabled:
        raise ValueError("safety.real_money_enabled must be false for market tools")
    if config.safety.live_broker_enabled:
        raise ValueError("safety.live_broker_enabled must be false for market tools")
    if config.safety.direct_execution_enabled:
        raise ValueError(
            "safety.direct_execution_enabled must be false for market tools"
        )
    if config.capital.default_layer not in config.capital.allowed_layers:
        raise ValueError(
            "capital.default_layer must be listed in capital.allowed_layers"
        )
    if not set(config.capital.allowed_layers).issubset({"shadow", "simulated"}):
        raise ValueError("capital.allowed_layers may only contain shadow/simulated")
    if config.capital.initial_capital < 0:
        raise ValueError("capital.initial_capital must be non-negative")
    if not str(config.capital.currency).strip():
        raise ValueError("capital.currency must declare the market-native currency")
    if config.data.reader != "tradingdatas_v1_catalog_query":
        raise ValueError(
            "data.reader must use the TradingDatas V1 catalog/query contract"
        )
    if config.universe.max_symbols <= 0:
        raise ValueError("universe.max_symbols must be positive")
    if config.universe.min_close < 0:
        raise ValueError("universe.min_close must be non-negative")
    if config.risk.max_positions <= 0:
        raise ValueError("risk.max_positions must be positive")
    if not 0 < config.risk.max_single_position_pct <= 1:
        raise ValueError("risk.max_single_position_pct must be within (0, 1]")
    if config.fees.taker_bps < 0 or config.fees.maker_bps < 0:
        raise ValueError("fees must be non-negative")
    if config.promotion.min_shadow_trades < 0:
        raise ValueError("promotion.min_shadow_trades must be non-negative")
    if not 0 <= config.promotion.min_positive_days_pct <= 1:
        raise ValueError("promotion.min_positive_days_pct must be within [0, 1]")
