#!/usr/bin/env python3
"""Run multiple simulated trade styles in parallel for one market."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.markets.safety import reject_real_execution_payload
from shared.markets.style_config import TradeStyle, load_trade_styles


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = TRADINGAGENT_ROOT / "shared" / "review"


class StyleRunner:
    """Apply every enabled JSON style to every signal using a simulator."""

    def __init__(
        self,
        market: str,
        simulator: Any,
        *,
        styles_dir: Path | str | None = None,
        review_root: Path | str | None = None,
    ) -> None:
        self.market = str(market or "").lower().strip()
        if not self.market:
            raise ValueError("market is required")
        self.simulator = simulator
        self.styles_dir = Path(styles_dir) if styles_dir is not None else None
        self.review_root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT

    def run(
        self,
        signals: list[dict[str, Any]],
        *,
        date: str,
        account: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run all enabled styles and write ``style_comparison.json``."""

        safe_account = self._sim_account(account, date)
        reject_real_execution_payload(safe_account, context=f"StyleRunner.{self.market}.account")
        styles = load_trade_styles(self.market, styles_dir=self.styles_dir)
        normalized_signals = [dict(signal or {}) for signal in signals]
        for signal in normalized_signals:
            reject_real_execution_payload(signal, context=f"StyleRunner.{self.market}.signal")

        runs: list[dict[str, Any]] = []
        for style in styles:
            style_runs = [
                self._run_one(style, signal, date=date, account=safe_account)
                for signal in normalized_signals
            ]
            runs.extend(style_runs)

        matrix = [self._style_metrics(style.name, runs) for style in styles]
        payload = {
            "market": self.market,
            "date": date,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "styles_loaded": len(styles),
            "signal_count": len(normalized_signals),
            "style_comparison": matrix,
            "runs": runs,
            "report_template": "shared/markets/style_comparison_report_template.md",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._write_report(payload)
        return payload

    def _run_one(
        self,
        style: TradeStyle,
        signal: dict[str, Any],
        *,
        date: str,
        account: dict[str, Any],
    ) -> dict[str, Any]:
        conviction = self._conviction(signal)
        order = self._style_order(style, signal, date=date, conviction=conviction, account=account)
        if conviction < style.conviction_min:
            return {
                "style_name": style.name,
                "market": self.market,
                "symbol": order.get("symbol") or order.get("market_id") or order.get("ts_code"),
                "status": "skipped_low_conviction",
                "conviction": round(conviction, 4),
                "conviction_min": style.conviction_min,
                "pnl": 0.0,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_execution": False,
            }

        try:
            fill = self.simulator.simulate(order, account)
            fill = dict(fill or {})
            status = str(fill.get("status", "failed"))
            error = ""
        except Exception as exc:
            fill = {}
            status = "error"
            error = str(exc)
        pnl = self._estimate_pnl(style, order, fill, conviction)
        return {
            "style_name": style.name,
            "style": asdict(style),
            "market": self.market,
            "symbol": order.get("symbol") or order.get("market_id") or order.get("ts_code"),
            "order_id": order.get("order_id"),
            "status": status,
            "conviction": round(conviction, 4),
            "quantity": order.get("quantity"),
            "price": order.get("price"),
            "pnl": pnl,
            "win": pnl > 0,
            "fill": self._sim_fill(fill),
            "error": error,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
        }

    def _style_order(
        self,
        style: TradeStyle,
        signal: dict[str, Any],
        *,
        date: str,
        conviction: float,
        account: dict[str, Any],
    ) -> dict[str, Any]:
        symbol = str(
            signal.get("symbol")
            or signal.get("ts_code")
            or signal.get("market_id")
            or signal.get("pair")
            or ""
        ).strip()
        side = str(signal.get("side") or signal.get("direction") or "buy").strip().lower()
        price = self._positive_price(signal)
        initial_capital = self._initial_capital(account)
        notional = initial_capital * style.position_pct
        quantity = self._quantity(notional, price)
        if self.market == "pm":
            order: dict[str, Any] = {
                "market_id": symbol,
                "symbol": symbol,
                "side": side if side in {"buy", "sell"} else "buy",
                "outcome": str(signal.get("outcome") or "yes").lower(),
                "quantity": max(1, int(round(quantity))),
                "price": max(0.01, min(0.99, price)),
            }
        else:
            order = {
                "symbol": symbol,
                "ts_code": symbol,
                "side": side if side in {"buy", "sell"} else "buy",
                "quantity": quantity,
                "price": price,
            }
        order.update(
            {
                "market": self.market,
                "trade_date": date,
                "date": date,
                "style_name": style.name,
                "strategy_name": f"{signal.get('strategy_name', self.market)}:{style.name}",
                "stop_loss_pct": style.stop_loss_pct,
                "take_profit_pct": style.take_profit_pct,
                "max_hold_days": style.max_hold_days,
                "pyramid": style.pyramid,
                "scale_in_steps": style.scale_in_steps,
                "conviction": round(conviction, 4),
                "capital_layer": "simulated",
                "account_type": "simulated",
                "direct_execution": False,
                "real_execution": False,
                "order_id": self._order_id(style.name, symbol, side, date),
            }
        )
        return order

    def _style_metrics(self, style_name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
        rows = [row for row in runs if row.get("style_name") == style_name]
        executed = [row for row in rows if row.get("status") not in {"skipped_low_conviction"}]
        pnls = [float(row.get("pnl", 0.0) or 0.0) for row in executed]
        wins = [row for row in executed if row.get("win")]
        return {
            "style_name": style_name,
            "trades": len(executed),
            "skipped": len(rows) - len(executed),
            "pnl": round(sum(pnls), 6),
            "win_rate": round(len(wins) / len(executed), 6) if executed else 0.0,
            "max_dd": round(self._max_drawdown(pnls), 6),
            "sharpe": round(self._sharpe(pnls), 6),
            "capital_layer": "simulated",
            "real_execution": False,
        }

    def _write_report(self, payload: dict[str, Any]) -> None:
        output_dir = self.review_root / self.market
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "style_comparison.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _sim_account(self, account: dict[str, Any] | None, date: str) -> dict[str, Any]:
        payload = dict(account or {})
        payload.setdefault("account_id", f"{self.market}_multi_style_sim")
        payload.setdefault("capital_layer", "simulated")
        payload.setdefault("account_type", "simulated")
        payload.setdefault("date", date)
        payload["real_execution"] = False
        payload["direct_execution"] = False
        return payload

    def _sim_fill(self, fill: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(fill or {})
        cleaned["capital_layer"] = "simulated"
        cleaned["account_type"] = "simulated"
        cleaned["real_execution"] = False
        return cleaned

    @staticmethod
    def _conviction(signal: dict[str, Any]) -> float:
        for key in ("conviction", "belief_score", "confidence", "score", "combined"):
            try:
                value = float(signal.get(key))
            except (TypeError, ValueError):
                continue
            if value == value:
                return max(0.0, min(1.0, value))
        return 0.5

    @staticmethod
    def _positive_price(signal: dict[str, Any]) -> float:
        for key in ("price", "latest_price", "close", "limit_price", "fill_price"):
            try:
                value = float(signal.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0 and value == value:
                return value
        return 0.5

    @staticmethod
    def _initial_capital(account: dict[str, Any]) -> float:
        for key in ("initial_capital", "equity", "cash", "capital", "balance"):
            try:
                value = float(account.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0 and value == value:
                return value
        return 100_000.0

    @staticmethod
    def _quantity(notional: float, price: float) -> float:
        if price <= 0:
            return 0.0
        return round(notional / price, 8)

    @staticmethod
    def _estimate_pnl(style: TradeStyle, order: dict[str, Any], fill: dict[str, Any], conviction: float) -> float:
        if str(fill.get("status", "")).lower() not in {"filled", "partial"}:
            return 0.0
        notional = float(fill.get("notional") or 0.0)
        if notional <= 0:
            quantity = float(fill.get("filled_qty", fill.get("quantity", order.get("quantity", 0))) or 0.0)
            price = float(fill.get("avg_price", fill.get("fill_price", order.get("price", 0))) or 0.0)
            notional = quantity * price
        expected_return = conviction * style.take_profit_pct + (1.0 - conviction) * style.stop_loss_pct
        fee = float(fill.get("fee", 0.0) or 0.0)
        return round((notional * expected_return) - fee, 6)

    @staticmethod
    def _max_drawdown(pnls: list[float]) -> float:
        peak = 0.0
        equity = 0.0
        max_dd = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return abs(max_dd)

    @staticmethod
    def _sharpe(pnls: list[float]) -> float:
        if len(pnls) < 2:
            return 0.0
        mean = sum(pnls) / len(pnls)
        variance = sum((item - mean) ** 2 for item in pnls) / (len(pnls) - 1)
        stdev = math.sqrt(variance)
        if stdev == 0:
            return 0.0
        return mean / stdev * math.sqrt(len(pnls))

    @staticmethod
    def _order_id(style_name: str, symbol: str, side: str, date: str) -> str:
        raw = f"SIM-{date}-{symbol}-{side}-{style_name}".replace("/", "-").replace(" ", "-")
        return raw[:120]


__all__ = ["StyleRunner"]
