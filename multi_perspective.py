#!/usr/bin/env python3
"""Multi-researcher analysis from SharedSignals read APIs.

The module produces a research-only report with four scored perspectives and a
weighted synthesis. It does not write signal queues or execute trades.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import pstdev
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.screening.fundamental_analyzer import analyze_fundamentals


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _date_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    date_part = raw.split("T", 1)[0].split(" ", 1)[0]
    if "-" in date_part or "/" in date_part:
        sep = "-" if "-" in date_part else "/"
        parts = date_part.split(sep)
        if len(parts) >= 3:
            return f"{parts[0].zfill(4)}{parts[1].zfill(2)}{parts[2].zfill(2)}"
    digits = "".join(ch for ch in date_part if ch.isdigit())
    return digits[:8]


def _lookback(as_of: str, days: int) -> str:
    try:
        dt = datetime.strptime(_date_key(as_of), "%Y%m%d")
    except ValueError:
        dt = datetime.now()
    return (dt - timedelta(days=days)).strftime("%Y%m%d")


def _unwrap_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data", [rows])
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = row.get("data")
        result.append(dict(data) if isinstance(data, dict) else dict(row))
    return result


def _close(row: dict[str, Any]) -> float:
    return _safe_float(row.get("adjusted_close", row.get("close")), 0.0)


def _volume(row: dict[str, Any]) -> float:
    return _safe_float(row.get("volume", row.get("vol")), 0.0)


def _latest_bars(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: _date_key(row.get("trade_date") or row.get("date") or row.get("timestamp")))


def _pct_return(closes: list[float], window: int) -> float:
    if len(closes) <= window or closes[-window - 1] <= 0:
        return 0.0
    return closes[-1] / closes[-window - 1] - 1.0


def _ma(values: list[float], window: int) -> float:
    if not values:
        return 0.0
    sample = values[-window:] if len(values) >= window else values
    return sum(sample) / len(sample)


class MultiPerspectiveAnalyzer:
    """Run bull, bear, macro, and technical researchers then synthesize."""

    WEIGHTS = {"bull": 0.30, "bear_inverse": 0.25, "macro": 0.20, "technical": 0.25}

    def __init__(self, reader: Any | None = None):
        self.reader = reader or TradingagentDataReader()

    def analyze(
        self,
        ts_code: str,
        as_of: str | None = None,
        market: str = "ashare",
        fundamental_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        as_of = _date_key(as_of) or datetime.now().strftime("%Y%m%d")
        bars = self._market_data(ts_code, as_of, market)
        events = self._events(ts_code, as_of, market)
        sentiment = self._sentiment(ts_code, as_of)
        macro = self._macro(as_of)
        industry = self._industry(ts_code)
        capital_flow = self._capital_flow(ts_code, as_of)
        fundamental_report = fundamental_report or analyze_fundamentals(
            ts_code, as_of=as_of, reader=self.reader
        )

        technical_snapshot = self._technical_snapshot(bars)
        perspectives = {
            "bull": self._bull_case(
                ts_code, fundamental_report, technical_snapshot, events, sentiment, capital_flow
            ),
            "bear": self._bear_case(ts_code, fundamental_report, technical_snapshot, events, sentiment),
            "macro": self._macro_case(ts_code, industry, macro, events),
            "technical": self._technical_case(ts_code, technical_snapshot),
        }
        consensus = self._synthesize(perspectives)
        return {
            "ts_code": ts_code,
            "as_of": as_of,
            "source": "SharedSignals API/DB via TradingagentDataReader",
            "capital_layer": "research_only",
            "individual_scores": {
                name: item["score"] for name, item in perspectives.items()
            },
            "perspectives": perspectives,
            "consensus": consensus,
            "disagreement_areas": consensus["disagreement_areas"],
            "data_coverage": {
                "market_data_rows": len(bars),
                "event_rows": len(events),
                "sentiment_rows": len(sentiment),
                "macro_rows": len(macro),
                "capital_flow_rows": len(capital_flow),
                "fundamental_score": fundamental_report.get("scores", {}).get("composite"),
            },
        }

    def _call(self, method_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        method = getattr(self.reader, method_name, None)
        if not callable(method):
            return []
        try:
            return _unwrap_rows(method(**kwargs))
        except TypeError:
            return []
        except Exception:
            return []

    def _market_data(self, ts_code: str, as_of: str, market: str) -> list[dict[str, Any]]:
        rows = self._call(
            "get_market_data",
            ts_code=ts_code,
            start=_lookback(as_of, 220),
            end=as_of,
            freq="daily",
        )
        if rows:
            return _latest_bars(rows)
        symbol = ts_code.split(".", 1)[0]
        rows = self._call("get_bars_daily", market=market, symbol=symbol, start=_lookback(as_of, 220), end=as_of)
        return _latest_bars(rows)

    def _events(self, ts_code: str, as_of: str, market: str) -> list[dict[str, Any]]:
        symbol = ts_code.split(".", 1)[0]
        rows = self._call("get_events", market=market, symbol=symbol, start=_lookback(as_of, 30), end=as_of)
        return [
            row
            for row in rows
            if not row.get("subject_code")
            or str(row.get("subject_code")) in {ts_code, symbol}
            or str(row.get("symbol")) in {ts_code, symbol}
        ]

    def _sentiment(self, ts_code: str, as_of: str) -> list[dict[str, Any]]:
        rows = self._call("get_sentiment", start=_lookback(as_of, 14), end=as_of)
        symbol = ts_code.split(".", 1)[0]
        return [
            row
            for row in rows
            if not row.get("subject_code") or str(row.get("subject_code")) in {ts_code, symbol}
        ]

    def _macro(self, as_of: str) -> list[dict[str, Any]]:
        return self._call("get_macro_factors", start=_lookback(as_of, 120), end=as_of)

    def _industry(self, ts_code: str) -> dict[str, Any]:
        rows = self._call("get_industry", ts_code=ts_code)
        return rows[0] if rows else {}

    def _capital_flow(self, ts_code: str, as_of: str) -> list[dict[str, Any]]:
        return self._call("get_capital_flow", ts_code=ts_code, start=_lookback(as_of, 10), end=as_of)

    def _technical_snapshot(self, bars: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [_close(row) for row in bars if _close(row) > 0]
        volumes = [_volume(row) for row in bars if _volume(row) > 0]
        highs = [_safe_float(row.get("high"), _close(row)) for row in bars if _close(row) > 0]
        lows = [_safe_float(row.get("low"), _close(row)) for row in bars if _close(row) > 0]
        latest_close = closes[-1] if closes else 0.0
        ma20 = _ma(closes, 20)
        ma60 = _ma(closes, 60)
        ret20 = _pct_return(closes, 20)
        ret60 = _pct_return(closes, 60)
        avg_volume20 = _ma(volumes, 20)
        volume_ratio = volumes[-1] / avg_volume20 if volumes and avg_volume20 > 0 else 0.0
        support = min(lows[-60:]) if lows else None
        resistance = max(highs[-60:]) if highs else None
        trend_strength = 50.0
        trend_strength += min(max(ret20 * 180.0, -25.0), 25.0)
        trend_strength += min(max(ret60 * 120.0, -25.0), 25.0)
        if latest_close > ma20 > ma60 > 0:
            trend_strength += 12.0
        elif latest_close < ma20 < ma60:
            trend_strength -= 12.0
        if volume_ratio >= 1.2 and ret20 > 0:
            trend_strength += 6.0
        if volume_ratio >= 1.2 and ret20 < 0:
            trend_strength -= 6.0
        return {
            "latest_close": latest_close or None,
            "ma20": ma20 or None,
            "ma60": ma60 or None,
            "return_20d": ret20,
            "return_60d": ret60,
            "volume_ratio_20d": volume_ratio or None,
            "support": support,
            "resistance": resistance,
            "trend_strength": round(_clamp(trend_strength), 2),
            "sample_size": len(closes),
        }

    def _direction_signal(self, row: dict[str, Any]) -> float:
        text = str(
            row.get("proposed_impact_hint")
            or row.get("impact_hint")
            or row.get("direction")
            or row.get("sentiment")
            or ""
        ).lower()
        if "negative" in text or "bear" in text or text in {"-1", "sell"}:
            return -1.0
        if "positive" in text or "bull" in text or text in {"1", "buy"}:
            return 1.0
        return 0.0

    def _event_balance(self, events: list[dict[str, Any]], sentiment: list[dict[str, Any]]) -> float:
        weighted = 0.0
        total = 0.0
        for row in events + sentiment:
            conf = _safe_float(row.get("confidence", row.get("score")), 0.5)
            conf = max(conf, 0.2)
            weighted += self._direction_signal(row) * conf
            total += conf
        return weighted / total if total > 0 else 0.0

    def _bull_case(
        self,
        ts_code: str,
        fundamental: dict[str, Any],
        technical: dict[str, Any],
        events: list[dict[str, Any]],
        sentiment: list[dict[str, Any]],
        capital_flow: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del ts_code
        score = 45.0
        evidence: list[dict[str, Any]] = []
        metrics = fundamental.get("metrics", {})
        fundamental_score = _safe_float(fundamental.get("scores", {}).get("composite"), 50.0)
        if fundamental_score >= 70:
            score += 14.0
            evidence.append({"type": "quality", "detail": f"fundamental score {fundamental_score:.1f}"})
        if _safe_float(metrics.get("revenue_growth_yoy"), 0.0) >= 15:
            score += 10.0
            evidence.append({"type": "growth", "detail": f"revenue YoY {metrics.get('revenue_growth_yoy'):.2f}%"})
        if _safe_float(metrics.get("roe"), 0.0) >= 12:
            score += 8.0
            evidence.append({"type": "profitability", "detail": f"ROE {metrics.get('roe'):.2f}%"})
        if _safe_float(metrics.get("gross_margin_trend"), 0.0) > 1.0:
            score += 5.0
            evidence.append({"type": "margin", "detail": f"gross margin trend {metrics.get('gross_margin_trend'):.2f}pp"})
        if technical.get("trend_strength", 50) >= 65:
            score += 10.0
            evidence.append({"type": "momentum", "detail": f"trend strength {technical.get('trend_strength')}"})
        event_balance = self._event_balance(events, sentiment)
        if event_balance > 0.15:
            score += 8.0
            evidence.append({"type": "catalyst", "detail": f"positive event balance {event_balance:.2f}"})
        net_flow = sum(_safe_float(row.get("net_mf_amount", row.get("value")), 0.0) for row in capital_flow)
        if net_flow > 0:
            score += 4.0
            evidence.append({"type": "capital_flow", "detail": f"net inflow {net_flow:.2f}"})
        return {
            "researcher": "bull_case",
            "score": round(_clamp(score), 2),
            "stance": "bullish",
            "evidence": evidence,
            "missing_evidence": [] if evidence else ["no strong growth/catalyst/momentum evidence"],
        }

    def _bear_case(
        self,
        ts_code: str,
        fundamental: dict[str, Any],
        technical: dict[str, Any],
        events: list[dict[str, Any]],
        sentiment: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del ts_code
        risk_score = 25.0
        evidence: list[dict[str, Any]] = []
        for flag in fundamental.get("red_flags", []):
            severity = str(flag.get("severity") or "low")
            risk_score += {"high": 12.0, "medium": 7.0, "low": 3.0}.get(severity, 3.0)
            evidence.append({"type": flag.get("flag"), "detail": flag.get("detail"), "severity": severity})
        valuation = fundamental.get("valuation", {})
        if _safe_float(valuation.get("pe_percentile_5y"), 0.0) >= 85:
            risk_score += 8.0
            evidence.append({"type": "overvaluation", "detail": f"PE percentile {valuation.get('pe_percentile_5y')}"})
        if technical.get("trend_strength", 50) <= 35:
            risk_score += 10.0
            evidence.append({"type": "negative_divergence", "detail": f"trend strength {technical.get('trend_strength')}"})
        event_balance = self._event_balance(events, sentiment)
        if event_balance < -0.15:
            risk_score += 8.0
            evidence.append({"type": "negative_event_balance", "detail": f"{event_balance:.2f}"})
        return {
            "researcher": "bear_case",
            "score": round(_clamp(risk_score), 2),
            "stance": "bearish_risk",
            "evidence": evidence,
            "missing_evidence": [] if evidence else ["no major valuation/risk/negative divergence evidence"],
        }

    def _macro_case(
        self,
        ts_code: str,
        industry: dict[str, Any],
        macro_rows: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del ts_code
        score = 50.0
        evidence: list[dict[str, Any]] = []
        sw_l1 = str(industry.get("sw_l1_name") or industry.get("sw_l1") or "")
        sw_l3 = str(industry.get("sw_l3_name") or industry.get("sw_l3") or "")
        sector_text = f"{sw_l1}/{sw_l3}".strip("/")
        if sector_text:
            evidence.append({"type": "sector_exposure", "detail": sector_text})
        high_rate_sensitive = any(key in sector_text for key in ("房地产", "电力", "公用", "成长", "电子", "计算机"))
        financial_sensitive = any(key in sector_text for key in ("银行", "保险", "金融"))
        latest_macro = macro_rows[-1] if macro_rows else {}
        macro_text = " ".join(str(value).lower() for value in latest_macro.values())
        rising_rate = any(key in macro_text for key in ("rate_up", "yield_up", "tightening", "加息", "利率上行"))
        easing = any(key in macro_text for key in ("easing", "rate_down", "宽松", "降息"))
        if rising_rate and high_rate_sensitive:
            score -= 10.0
            evidence.append({"type": "rate_sensitivity", "detail": "rising-rate pressure"})
        if rising_rate and financial_sensitive:
            score += 6.0
            evidence.append({"type": "rate_sensitivity", "detail": "financial sector may benefit from rising rates"})
        if easing:
            score += 6.0
            evidence.append({"type": "cycle_position", "detail": "easing macro signal"})
        if any(self._direction_signal(row) > 0 for row in events):
            score += 4.0
            evidence.append({"type": "sector_catalyst", "detail": "positive event exposure"})
        return {
            "researcher": "macro",
            "score": round(_clamp(score), 2),
            "stance": "macro_favorable" if score >= 60 else "macro_adverse" if score <= 40 else "macro_neutral",
            "evidence": evidence,
            "missing_evidence": [] if macro_rows else ["macro factor rows unavailable"],
        }

    def _technical_case(self, ts_code: str, technical: dict[str, Any]) -> dict[str, Any]:
        del ts_code
        score = _safe_float(technical.get("trend_strength"), 50.0)
        evidence: list[dict[str, Any]] = []
        if technical.get("sample_size", 0) < 60:
            evidence.append({"type": "sample_warning", "detail": "fewer than 60 daily bars"})
        if technical.get("latest_close") and technical.get("ma20") and technical["latest_close"] > technical["ma20"]:
            evidence.append({"type": "trend", "detail": "close above MA20"})
        if technical.get("volume_ratio_20d") and technical["volume_ratio_20d"] >= 1.2:
            evidence.append({"type": "volume_confirmation", "detail": f"{technical['volume_ratio_20d']:.2f}x avg volume"})
        if technical.get("support") is not None:
            evidence.append({"type": "support", "detail": technical["support"]})
        if technical.get("resistance") is not None:
            evidence.append({"type": "resistance", "detail": technical["resistance"]})
        return {
            "researcher": "technical",
            "score": round(_clamp(score), 2),
            "stance": "uptrend" if score >= 60 else "downtrend" if score <= 40 else "rangebound",
            "evidence": evidence,
            "snapshot": technical,
            "missing_evidence": [] if technical.get("sample_size") else ["daily bars unavailable"],
        }

    def _synthesize(self, perspectives: dict[str, dict[str, Any]]) -> dict[str, Any]:
        bull = _safe_float(perspectives["bull"]["score"], 50.0)
        bear_risk = _safe_float(perspectives["bear"]["score"], 50.0)
        macro = _safe_float(perspectives["macro"]["score"], 50.0)
        technical = _safe_float(perspectives["technical"]["score"], 50.0)
        favorable_scores = {
            "bull": bull,
            "bear_inverse": 100.0 - bear_risk,
            "macro": macro,
            "technical": technical,
        }
        consensus_score = sum(favorable_scores[key] * self.WEIGHTS[key] for key in self.WEIGHTS)
        values = list(favorable_scores.values())
        dispersion = pstdev(values) if len(values) > 1 else 0.0
        disagreement = self._disagreements(favorable_scores, perspectives)
        distance = abs(consensus_score - 50.0)
        if distance >= 20 and dispersion <= 18 and not disagreement:
            conviction = "high"
        elif distance >= 10 and dispersion <= 25:
            conviction = "medium"
        else:
            conviction = "low"
        if consensus_score >= 60:
            direction = "bullish"
        elif consensus_score <= 40:
            direction = "bearish"
        else:
            direction = "neutral"
        return {
            "score": round(_clamp(consensus_score), 2),
            "direction": direction,
            "conviction_level": conviction,
            "weighted_inputs": {k: round(v, 2) for k, v in favorable_scores.items()},
            "dispersion": round(dispersion, 2),
            "weights": dict(self.WEIGHTS),
            "disagreement_areas": disagreement,
        }

    def _disagreements(
        self,
        favorable_scores: dict[str, float],
        perspectives: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        disagreements: list[dict[str, Any]] = []
        values = favorable_scores
        if max(values.values()) - min(values.values()) >= 35:
            disagreements.append({"area": "score_dispersion", "detail": "perspectives differ by >=35 points"})
        if values["bull"] >= 65 and values["bear_inverse"] <= 45:
            disagreements.append({"area": "bull_vs_bear", "detail": "strong upside evidence but high bear risk"})
        if values["macro"] >= 60 and values["technical"] <= 40:
            disagreements.append({"area": "macro_vs_technical", "detail": "macro backdrop positive but chart is weak"})
        if values["macro"] <= 40 and values["technical"] >= 60:
            disagreements.append({"area": "macro_vs_technical", "detail": "chart positive but macro backdrop is adverse"})
        if perspectives["bear"].get("evidence") and values["bull"] >= 70:
            disagreements.append({"area": "quality_vs_risk", "detail": "quality/catalyst case coexists with material risks"})
        return disagreements


def analyze_multi_perspective(
    ts_code: str,
    as_of: str | None = None,
    reader: Any | None = None,
    market: str = "ashare",
    fundamental_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a multi-perspective research report for one stock."""

    return MultiPerspectiveAnalyzer(reader=reader).analyze(
        ts_code,
        as_of=as_of,
        market=market,
        fundamental_report=fundamental_report,
    )

