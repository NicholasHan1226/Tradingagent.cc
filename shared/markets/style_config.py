#!/usr/bin/env python3
"""Data-driven trade style configuration for simulated market tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TradeStyle:
    name: str
    position_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_days: int
    pyramid: bool
    scale_in_steps: int
    conviction_min: float
    description: str
    status: str = "active"
    weight: float = 1.0
    created_at: str = ""
    last_modified: str = ""
    generation: int = 1
    auto_generate: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("TradeStyle.name is required")
        if self.status not in {"active", "paused", "deprecated"}:
            raise ValueError(f"{self.name}: status must be active, paused, or deprecated")
        if not 0.0 <= float(self.weight) <= 1.0:
            raise ValueError(f"{self.name}: weight must be within [0.0, 1.0]")
        if int(self.generation) < 1:
            raise ValueError(f"{self.name}: generation must be >= 1")
        if not 0.02 <= float(self.position_pct) <= 0.15:
            raise ValueError(f"{self.name}: position_pct must be within [0.02, 0.15]")
        if not -0.15 <= float(self.stop_loss_pct) <= -0.05:
            raise ValueError(f"{self.name}: stop_loss_pct must be within [-0.15, -0.05]")
        if not 0.05 <= float(self.take_profit_pct) <= 0.30:
            raise ValueError(f"{self.name}: take_profit_pct must be within [0.05, 0.30]")
        if not 1 <= int(self.max_hold_days) <= 30:
            raise ValueError(f"{self.name}: max_hold_days must be within [1, 30]")
        if not 1 <= int(self.scale_in_steps) <= 3:
            raise ValueError(f"{self.name}: scale_in_steps must be within [1, 3]")
        if not 0.3 <= float(self.conviction_min) <= 0.8:
            raise ValueError(f"{self.name}: conviction_min must be within [0.3, 0.8]")


STYLE_FIELDS = set(TradeStyle.__dataclass_fields__)
REQUIRED_STYLE_FIELDS = {
    "name",
    "position_pct",
    "stop_loss_pct",
    "take_profit_pct",
    "max_hold_days",
    "pyramid",
    "scale_in_steps",
    "conviction_min",
    "description",
}


def style_from_mapping(payload: dict[str, Any]) -> TradeStyle:
    """Build a validated ``TradeStyle`` from one JSON mapping."""

    missing = sorted(field for field in REQUIRED_STYLE_FIELDS if field not in payload)
    if missing:
        raise ValueError(f"style config missing fields: {missing}")
    values = {field: payload[field] for field in STYLE_FIELDS if field in payload}
    values.setdefault("status", style_status(payload))
    values.setdefault("weight", float(payload.get("weight", 1.0)))
    values.setdefault("created_at", str(payload.get("created_at", "")))
    values.setdefault("last_modified", str(payload.get("last_modified", "")))
    values.setdefault("generation", int(payload.get("generation", 1) or 1))
    values.setdefault("auto_generate", payload.get("auto_generate"))
    return TradeStyle(**values)


def style_status(payload: dict[str, Any]) -> str:
    """Return active/paused/deprecated while preserving legacy enabled flags."""

    status = str(payload.get("status") or "").strip().lower()
    if status in {"active", "paused", "deprecated"}:
        return status
    if bool(payload.get("paused", False)) or not bool(payload.get("enabled", True)):
        return "paused"
    return "active"


def is_style_enabled(payload: dict[str, Any]) -> bool:
    """Return whether a style can participate in simulated execution."""

    return bool(payload.get("enabled", True)) and style_status(payload) == "active"


def _market_dir(market: str, root: Path) -> Path:
    candidates = [
        root / market,
        root / market.lower(),
        root / market.upper(),
        root / market.capitalize(),
    ]
    for path in candidates:
        if path.exists():
            return path
    for child in root.iterdir() if root.exists() else []:
        if child.is_dir() and child.name.lower() == market.lower():
            return child
    return candidates[0]


def styles_dir_for_market(market: str, root: Path | str | None = None) -> Path:
    """Resolve ``<market>/styles`` case-insensitively."""

    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return _market_dir(market, base) / "styles"


def load_trade_styles(
    market: str,
    *,
    root: Path | str | None = None,
    styles_dir: Path | str | None = None,
    include_disabled: bool = False,
) -> list[TradeStyle]:
    """Load all JSON style configs for ``market``.

    Adding a new style is intentionally data-only: place a JSON file in
    ``<market>/styles`` with the ``TradeStyle`` fields and optional
    ``enabled`` flag.
    """

    directory = Path(styles_dir) if styles_dir is not None else styles_dir_for_market(market, root)
    if not directory.exists():
        return []

    styles: list[TradeStyle] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"style config must be a JSON object: {path}")
        if not include_disabled and not is_style_enabled(payload):
            continue
        styles.append(style_from_mapping(payload))
    return styles


__all__ = [
    "TradeStyle",
    "is_style_enabled",
    "load_trade_styles",
    "style_from_mapping",
    "style_status",
    "styles_dir_for_market",
]
