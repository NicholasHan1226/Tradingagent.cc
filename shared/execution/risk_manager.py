#!/usr/bin/env python3
"""Real-grade risk controls for simulated and future real execution."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from shared.execution.real_trading_gate import validate_real_trading_enabled
from shared.markets.safety import reject_real_execution_payload


DEFAULT_RISK_PROFILE: dict[str, Any] = {
    "market": "default",
    "max_order_size": 100_000_000.0,
    "max_position": 100_000_000.0,
    "max_notional": 1_000_000.0,
    "max_daily_loss": 10_000.0,
    "max_gross_exposure": 1_000_000.0,
    "max_net_exposure": 800_000.0,
    "max_delta_exposure": 800_000.0,
    "max_beta_exposure": 800_000.0,
    "single_symbol_concentration": 0.15,
    "sector_concentration": 0.40,
    "market_concentration": 0.80,
    "volatility_halt_bps": 800.0,
    "max_consecutive_losses": 3,
    "kill_switch": {"global": False, "markets": [], "symbols": [], "halt_files": []},
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _load_json(value: dict[str, Any] | str | Path | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (str, Path)):
        return json.loads(Path(value).read_text(encoding="utf-8"))
    return dict(value)


@dataclass
class RiskProfile:
    market: str = "default"
    max_order_size: float = 100_000_000.0
    max_position: float = 100_000_000.0
    max_notional: float = 1_000_000.0
    max_daily_loss: float = 10_000.0
    max_gross_exposure: float = 1_000_000.0
    max_net_exposure: float = 800_000.0
    max_delta_exposure: float = 800_000.0
    max_beta_exposure: float = 800_000.0
    single_symbol_concentration: float = 0.15
    sector_concentration: float = 0.40
    market_concentration: float = 0.80
    volatility_halt_bps: float = 800.0
    max_consecutive_losses: int = 3
    kill_switch: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RISK_PROFILE["kill_switch"]))

    @classmethod
    def from_json(cls, profile: dict[str, Any] | str | Path | None, *, market: str = "default") -> "RiskProfile":
        payload = dict(DEFAULT_RISK_PROFILE)
        payload.update(_load_json(profile))
        payload["market"] = str(payload.get("market") or market or "default").lower()
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_risk_profile_placeholder",
            "profile": asdict(self),
            "requires_hardware_halt_mapping": True,
        }


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    profile_market: str = "default"

    @property
    def rejected(self) -> bool:
        return not self.allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "rejected": self.rejected,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "profile_market": self.profile_market,
            "capital_layer": "simulated",
            "real_execution": False,
        }


class RiskManager:
    """Pre-trade and portfolio risk checks driven by JSON market profiles."""

    def __init__(self, profile: RiskProfile | dict[str, Any] | str | Path | None = None, *, market: str = "default") -> None:
        self.profile = profile if isinstance(profile, RiskProfile) else RiskProfile.from_json(profile, market=market)

    def check_order(self, order: dict[str, Any], state: dict[str, Any] | None = None) -> RiskDecision:
        state = dict(state or {})
        order_payload = dict(order or {})
        market = str(order_payload.get("market") or self.profile.market).lower()
        symbol = str(order_payload.get("symbol") or order_payload.get("ts_code") or order_payload.get("market_id") or "")
        quantity = _safe_float(order_payload.get("quantity", order_payload.get("shares")), 0.0)
        price = _safe_float(order_payload.get("limit_price", order_payload.get("price")), 0.0)
        notional = _safe_float(order_payload.get("notional", order_payload.get("amount")), quantity * price)
        sector = str(order_payload.get("sector") or "unknown")

        reasons: list[str] = []
        warnings: list[str] = []
        try:
            reject_real_execution_payload(order_payload, context=f"RiskManager.{market}.order")
        except RuntimeError as exc:
            reasons.append(str(exc))
        self._check_kill_switch(market, symbol, reasons)
        if quantity <= 0:
            reasons.append("quantity must be positive")
        if price <= 0:
            reasons.append("price must be positive")
        if quantity > self.profile.max_order_size:
            reasons.append(f"max_order_size exceeded: {quantity}>{self.profile.max_order_size}")
        if notional > self.profile.max_notional:
            reasons.append(f"max_notional exceeded: {notional}>{self.profile.max_notional}")

        current_position = _safe_float(self._position_value(state, symbol, "quantity"), 0.0)
        if current_position + quantity > self.profile.max_position:
            reasons.append(f"max_position exceeded: {current_position + quantity}>{self.profile.max_position}")

        daily_pnl = _safe_float(state.get("daily_pnl"), 0.0)
        if daily_pnl <= -abs(self.profile.max_daily_loss):
            reasons.append(f"max_daily_loss breached: {daily_pnl}<=-{abs(self.profile.max_daily_loss)}")

        volatility_bps = _safe_float(state.get("volatility_bps", order_payload.get("volatility_bps")), 0.0)
        if volatility_bps > self.profile.volatility_halt_bps:
            reasons.append(f"volatility circuit breaker: {volatility_bps}>{self.profile.volatility_halt_bps}")
        loss_streak = int(_safe_float(state.get("consecutive_losses"), 0.0))
        if loss_streak >= int(self.profile.max_consecutive_losses):
            reasons.append(f"consecutive loss circuit breaker: {loss_streak}>={self.profile.max_consecutive_losses}")

        exposure_reasons, exposure_warnings = self._check_exposure(state, market, symbol, sector, notional)
        reasons.extend(exposure_reasons)
        warnings.extend(exposure_warnings)
        return RiskDecision(
            allowed=not reasons,
            reasons=reasons,
            warnings=warnings,
            profile_market=self.profile.market,
        )

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_risk_manager_placeholder",
            "profile": asdict(self.profile),
            "requires_broker_pretrade_hook": True,
        }

    def _check_kill_switch(self, market: str, symbol: str, reasons: list[str]) -> None:
        kill = dict(self.profile.kill_switch or {})
        if kill.get("global"):
            reasons.append("global kill switch active")
        if market in {str(item).lower() for item in kill.get("markets", []) or []}:
            reasons.append(f"market kill switch active: {market}")
        if symbol in {str(item) for item in kill.get("symbols", []) or []}:
            reasons.append(f"symbol kill switch active: {symbol}")
        for raw_path in kill.get("halt_files", []) or []:
            if Path(raw_path).exists():
                reasons.append(f"hardware halt file active: {raw_path}")

    def _check_exposure(
        self,
        state: dict[str, Any],
        market: str,
        symbol: str,
        sector: str,
        order_notional: float,
    ) -> tuple[list[str], list[str]]:
        reasons: list[str] = []
        warnings: list[str] = []
        gross = _safe_float(state.get("gross_exposure"), 0.0) + abs(order_notional)
        net = _safe_float(state.get("net_exposure"), 0.0) + order_notional
        delta = _safe_float(state.get("delta_exposure"), 0.0) + _safe_float(state.get("order_delta"), order_notional)
        beta = _safe_float(state.get("beta_exposure"), 0.0) + _safe_float(state.get("order_beta"), order_notional)
        equity = max(_safe_float(state.get("equity"), 0.0), _safe_float(state.get("capital"), 0.0), 1.0)

        if gross > self.profile.max_gross_exposure:
            reasons.append(f"gross exposure exceeded: {gross}>{self.profile.max_gross_exposure}")
        if abs(net) > self.profile.max_net_exposure:
            reasons.append(f"net exposure exceeded: {net}>{self.profile.max_net_exposure}")
        if abs(delta) > self.profile.max_delta_exposure:
            reasons.append(f"delta exposure exceeded: {delta}>{self.profile.max_delta_exposure}")
        if abs(beta) > self.profile.max_beta_exposure:
            reasons.append(f"beta exposure exceeded: {beta}>{self.profile.max_beta_exposure}")

        symbol_exposure = self._bucket_value(state, "symbol_exposure", symbol) + order_notional
        sector_exposure = self._bucket_value(state, "sector_exposure", sector) + order_notional
        market_exposure = self._bucket_value(state, "market_exposure", market) + order_notional
        checks = (
            ("single_symbol_concentration", symbol_exposure / equity, self.profile.single_symbol_concentration),
            ("sector_concentration", sector_exposure / equity, self.profile.sector_concentration),
            ("market_concentration", market_exposure / equity, self.profile.market_concentration),
        )
        for name, ratio, limit in checks:
            if ratio > limit:
                reasons.append(f"{name} exceeded: {ratio:.6f}>{limit}")
            elif ratio > limit * 0.9:
                warnings.append(f"{name} near limit: {ratio:.6f}>{limit * 0.9:.6f}")
        return reasons, warnings

    @staticmethod
    def _bucket_value(state: dict[str, Any], field_name: str, key: str) -> float:
        bucket = state.get(field_name, {})
        if not isinstance(bucket, dict):
            return 0.0
        return _safe_float(bucket.get(key), 0.0)

    @staticmethod
    def _position_value(state: dict[str, Any], symbol: str, key: str) -> Any:
        positions = state.get("positions", {})
        if isinstance(positions, dict):
            value = positions.get(symbol, {})
            if isinstance(value, dict):
                return value.get(key)
            return value if key == "quantity" else 0.0
        return 0.0


__all__ = ["RiskDecision", "RiskManager", "RiskProfile"]
