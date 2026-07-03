#!/usr/bin/env python3
"""Active fundamental analysis from SharedSignals read APIs.

This module is read-only: it loads financial, industry, valuation, and market
data through TradingagentDataReader/SharedSignals and returns a report dict. It
does not create orders, write signal queues, or touch broker/executor paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from shared.data.reader import TradingagentDataReader


@dataclass(frozen=True)
class FundamentalInputs:
    income: list[dict[str, Any]]
    balancesheet: list[dict[str, Any]]
    cashflow: list[dict[str, Any]]
    fina_indicator: list[dict[str, Any]]
    daily_basic: list[dict[str, Any]]
    industry: list[dict[str, Any]]
    stock_industry_map: list[dict[str, Any]]


def _safe_float(value: Any, default: float | None = None) -> float | None:
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


def _years_ago(as_of: str, years: int) -> str:
    try:
        dt = datetime.strptime(_date_key(as_of), "%Y%m%d")
    except ValueError:
        dt = datetime.now()
    return (dt - timedelta(days=365 * years + 7)).strftime("%Y%m%d")


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
        if isinstance(data, dict):
            result.append(dict(data))
        else:
            result.append(dict(row))
    return result


def _sort_financial_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    date_fields = ("end_date", "period", "ann_date", "f_ann_date", "trade_date", "report_date")
    return sorted(
        rows,
        key=lambda row: max((_date_key(row.get(field)) for field in date_fields), default=""),
        reverse=True,
    )


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _sort_financial_rows(rows)
    return ordered[0] if ordered else {}


def _first_number(row: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for field in fields:
        value = _safe_float(row.get(field))
        if value is not None:
            return value
    return None


def _latest_number(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> float | None:
    for row in _sort_financial_rows(rows):
        value = _first_number(row, fields)
        if value is not None:
            return value
    return None


def _percentile(value: float | None, history: list[float]) -> float | None:
    values = sorted(v for v in history if v is not None and v == v and v > 0)
    if value is None or not values:
        return None
    below_or_equal = sum(1 for item in values if item <= value)
    return round(below_or_equal / len(values) * 100.0, 2)


def _score_high(value: float | None, low: float, high: float) -> float:
    if value is None:
        return 50.0
    if high <= low:
        return 50.0
    return _clamp((value - low) / (high - low) * 100.0)


def _score_low(value: float | None, low: float, high: float) -> float:
    if value is None:
        return 50.0
    if high <= low:
        return 50.0
    return _clamp((high - value) / (high - low) * 100.0)


def _score_current_ratio(value: float | None) -> float:
    if value is None:
        return 50.0
    if value < 0.8:
        return 15.0
    if value < 1.0:
        return 35.0
    if value <= 2.5:
        return _clamp(55.0 + (value - 1.0) / 1.5 * 35.0)
    return _clamp(90.0 - min((value - 2.5) * 8.0, 25.0))


def _score_peg(value: float | None) -> float:
    if value is None:
        return 50.0
    if value <= 0:
        return 20.0
    if value <= 1.0:
        return 90.0
    if value <= 2.0:
        return 90.0 - (value - 1.0) * 35.0
    return _clamp(55.0 - (value - 2.0) * 20.0)


class FundamentalAnalyzer:
    """Compute active fundamental reports from SharedSignals API/DB rows."""

    def __init__(self, reader: Any | None = None, peer_limit: int = 25):
        self.reader = reader or TradingagentDataReader()
        self.peer_limit = max(0, int(peer_limit))

    def analyze(self, ts_code: str, as_of: str | None = None) -> dict[str, Any]:
        as_of = _date_key(as_of) or datetime.now().strftime("%Y%m%d")
        inputs = self._load_inputs(ts_code, as_of)
        metrics = self._compute_metrics(inputs)
        valuation = self._compute_valuation(metrics, inputs.daily_basic)
        peer_comparison = self._peer_comparison(ts_code, metrics, inputs)
        red_flags = self._red_flags(metrics, valuation, peer_comparison, inputs)
        scores = self._score(metrics, valuation, peer_comparison, red_flags)

        return {
            "ts_code": ts_code,
            "as_of": as_of,
            "source": "SharedSignals API/DB via TradingagentDataReader",
            "capital_layer": "research_only",
            "scores": scores,
            "metrics": metrics,
            "valuation": valuation,
            "peer_comparison": peer_comparison,
            "red_flags": red_flags,
            "data_coverage": {
                "income_rows": len(inputs.income),
                "balancesheet_rows": len(inputs.balancesheet),
                "cashflow_rows": len(inputs.cashflow),
                "fina_indicator_rows": len(inputs.fina_indicator),
                "daily_basic_rows": len(inputs.daily_basic),
                "industry_rows": len(inputs.industry),
            },
        }

    def _load_inputs(self, ts_code: str, as_of: str) -> FundamentalInputs:
        start_5y = _years_ago(as_of, 5)
        income = self._tushare("income", ts_code, start_5y, as_of)
        balancesheet = self._tushare("balancesheet", ts_code, start_5y, as_of)
        cashflow = self._tushare("cashflow", ts_code, start_5y, as_of)
        fina_indicator = self._tushare("fina_indicator", ts_code, start_5y, as_of)
        fundamentals = self._call("get_fundamentals", ts_code=ts_code, end_date=as_of)
        if fundamentals:
            fina_indicator = _sort_financial_rows(fina_indicator + fundamentals)
        daily_basic = self._tushare("daily_basic", ts_code, start_5y, as_of)
        industry = self._call("get_industry", ts_code=ts_code)
        stock_industry_map = self._call("get_reference", table="stock_industry_map")
        return FundamentalInputs(
            income=income,
            balancesheet=balancesheet,
            cashflow=cashflow,
            fina_indicator=fina_indicator,
            daily_basic=daily_basic,
            industry=industry,
            stock_industry_map=stock_industry_map,
        )

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

    def _tushare(
        self, api_name: str, ts_code: str, start_date: str | None, end_date: str | None
    ) -> list[dict[str, Any]]:
        method = getattr(self.reader, "get_tushare", None)
        if not callable(method):
            return []
        try:
            rows = method(
                api_name=api_name,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
        except TypeError:
            rows = method(api_name, ts_code, start_date, end_date)
        except Exception:
            return []
        return _sort_financial_rows(_unwrap_rows(rows))

    def _compute_metrics(self, inputs: FundamentalInputs) -> dict[str, Any]:
        latest_fina = _latest(inputs.fina_indicator)
        latest_bs = _latest(inputs.balancesheet)
        latest_income = _latest(inputs.income)
        latest_cashflow = _latest(inputs.cashflow)
        latest_daily_basic = _latest(inputs.daily_basic)

        roe = _first_number(latest_fina, ("roe", "roe_dt", "roe_waa", "q_roe"))
        roa = _first_number(latest_fina, ("roa", "roa2", "q_roa"))

        total_liab = _first_number(latest_bs, ("total_liab", "total_ncl", "tot_liab"))
        equity = _first_number(
            latest_bs,
            ("total_hldr_eqy_exc_min_int", "total_hldr_eqy_inc_min_int", "total_equity"),
        )
        debt_equity = total_liab / equity if total_liab is not None and equity and equity > 0 else None

        current_assets = _first_number(latest_bs, ("total_cur_assets", "tot_cur_assets"))
        current_liab = _first_number(latest_bs, ("total_cur_liab", "tot_cur_liab"))
        current_ratio = (
            current_assets / current_liab
            if current_assets is not None and current_liab and current_liab > 0
            else None
        )

        gross_margin = _first_number(latest_fina, ("grossprofit_margin", "gross_margin"))
        gross_margins = [
            value
            for value in (
                _first_number(row, ("grossprofit_margin", "gross_margin"))
                for row in _sort_financial_rows(inputs.fina_indicator)
            )
            if value is not None
        ]
        gross_margin_trend = None
        if len(gross_margins) >= 4:
            gross_margin_trend = round(gross_margins[0] - median(gross_margins[1:4]), 4)

        revenue_growth_yoy = _first_number(
            latest_fina,
            ("or_yoy", "tr_yoy", "revenue_yoy", "op_yoy", "sales_yoy"),
        )
        if revenue_growth_yoy is None:
            revenue_growth_yoy = self._revenue_growth_yoy(inputs.income)

        operating_cashflow = _first_number(
            latest_cashflow,
            ("n_cashflow_act", "net_cash_flows_oper_act", "net_operate_cash_flow"),
        )
        capex = _first_number(
            latest_cashflow,
            ("c_pay_acq_const_fiolta", "capex", "cash_paid_for_fixed_assets"),
        )
        fcf = None
        if operating_cashflow is not None:
            fcf = operating_cashflow - abs(capex or 0.0)
        total_mv_wan = _first_number(latest_daily_basic, ("total_mv", "total_market_value"))
        fcf_yield = None
        if fcf is not None and total_mv_wan and total_mv_wan > 0:
            fcf_yield = fcf / (total_mv_wan * 10000.0) * 100.0

        pe_ttm = _first_number(latest_daily_basic, ("pe_ttm", "pe"))
        pb = _first_number(latest_daily_basic, ("pb", "pb_mrq"))
        peg = None
        if pe_ttm is not None and revenue_growth_yoy is not None and revenue_growth_yoy > 0:
            peg = pe_ttm / revenue_growth_yoy

        return {
            "roe": roe,
            "roa": roa,
            "debt_equity": debt_equity,
            "current_ratio": current_ratio,
            "gross_margin": gross_margin,
            "gross_margin_trend": gross_margin_trend,
            "revenue_growth_yoy": revenue_growth_yoy,
            "free_cash_flow": fcf,
            "fcf_yield": fcf_yield,
            "pe_ttm": pe_ttm,
            "pb": pb,
            "peg": peg,
            "latest_report_period": _date_key(
                latest_fina.get("end_date")
                or latest_income.get("end_date")
                or latest_bs.get("end_date")
                or latest_cashflow.get("end_date")
            ),
        }

    def _revenue_growth_yoy(self, income_rows: list[dict[str, Any]]) -> float | None:
        rows = _sort_financial_rows(income_rows)
        if len(rows) < 2:
            return None
        latest_row = rows[0]
        latest_revenue = _first_number(latest_row, ("total_revenue", "revenue", "oper_rev"))
        if latest_revenue is None or latest_revenue <= 0:
            return None
        latest_period = _date_key(latest_row.get("end_date") or latest_row.get("period"))
        peer_row = None
        if len(latest_period) == 8:
            target = f"{int(latest_period[:4]) - 1}{latest_period[4:]}"
            peer_row = next(
                (row for row in rows[1:] if _date_key(row.get("end_date") or row.get("period")) == target),
                None,
            )
        peer_row = peer_row or rows[1]
        previous_revenue = _first_number(peer_row, ("total_revenue", "revenue", "oper_rev"))
        if previous_revenue is None or previous_revenue <= 0:
            return None
        return (latest_revenue / previous_revenue - 1.0) * 100.0

    def _compute_valuation(
        self, metrics: dict[str, Any], daily_basic: list[dict[str, Any]]
    ) -> dict[str, Any]:
        pe_history = [
            value
            for value in (_first_number(row, ("pe_ttm", "pe")) for row in daily_basic)
            if value is not None and value > 0
        ]
        pb_history = [
            value
            for value in (_first_number(row, ("pb", "pb_mrq")) for row in daily_basic)
            if value is not None and value > 0
        ]
        return {
            "pe_ttm": metrics.get("pe_ttm"),
            "pb": metrics.get("pb"),
            "pe_percentile_5y": _percentile(metrics.get("pe_ttm"), pe_history),
            "pb_percentile_5y": _percentile(metrics.get("pb"), pb_history),
            "peg": metrics.get("peg"),
            "history_samples": {"pe": len(pe_history), "pb": len(pb_history)},
        }

    def _industry_row(self, ts_code: str, inputs: FundamentalInputs) -> dict[str, Any]:
        for row in inputs.industry + inputs.stock_industry_map:
            if str(row.get("ts_code") or "").strip().upper() == ts_code.upper():
                return row
        return inputs.industry[0] if inputs.industry else {}

    def _peer_comparison(
        self, ts_code: str, metrics: dict[str, Any], inputs: FundamentalInputs
    ) -> dict[str, Any]:
        industry_row = self._industry_row(ts_code, inputs)
        sw_l3 = str(industry_row.get("sw_l3_name") or industry_row.get("sw_l3") or "").strip()
        peers = [
            row
            for row in inputs.stock_industry_map
            if sw_l3
            and str(row.get("ts_code") or "").strip().upper() != ts_code.upper()
            and str(row.get("sw_l3_name") or row.get("sw_l3") or "").strip() == sw_l3
        ]
        peer_metrics: list[dict[str, Any]] = []
        for peer in peers[: self.peer_limit]:
            peer_code = str(peer.get("ts_code") or "").strip()
            if not peer_code:
                continue
            fina = self._call("get_fundamentals", ts_code=peer_code)
            if not fina:
                continue
            peer_metrics.append(self._compute_metrics(FundamentalInputs([], [], [], fina, [], [], [])))

        metric_keys = ("roe", "roa", "debt_equity", "gross_margin", "revenue_growth_yoy")
        summary: dict[str, Any] = {}
        for key in metric_keys:
            values = [
                _safe_float(peer_metric.get(key))
                for peer_metric in peer_metrics
                if _safe_float(peer_metric.get(key)) is not None
            ]
            current = _safe_float(metrics.get(key))
            if current is None or not values:
                summary[key] = {"current": current, "peer_median": None, "percentile": None}
                continue
            sorted_values = sorted(values)
            if key == "debt_equity":
                better_or_equal = sum(1 for value in sorted_values if value >= current)
            else:
                better_or_equal = sum(1 for value in sorted_values if value <= current)
            summary[key] = {
                "current": current,
                "peer_median": round(median(sorted_values), 6),
                "percentile": round(better_or_equal / len(sorted_values) * 100.0, 2),
            }
        return {
            "industry": {
                "sw_l1_name": industry_row.get("sw_l1_name") or industry_row.get("sw_l1"),
                "sw_l2_name": industry_row.get("sw_l2_name") or industry_row.get("sw_l2"),
                "sw_l3_name": sw_l3 or None,
            },
            "peer_sample_size": len(peer_metrics),
            "peer_universe_size": len(peers),
            "metrics": summary,
        }

    def _red_flags(
        self,
        metrics: dict[str, Any],
        valuation: dict[str, Any],
        peer_comparison: dict[str, Any],
        inputs: FundamentalInputs,
    ) -> list[dict[str, Any]]:
        flags: list[dict[str, Any]] = []

        def add(flag: str, severity: str, detail: str) -> None:
            flags.append({"flag": flag, "severity": severity, "detail": detail})

        missing_sources = [
            name
            for name, rows in (
                ("income", inputs.income),
                ("balancesheet", inputs.balancesheet),
                ("cashflow", inputs.cashflow),
                ("fina_indicator", inputs.fina_indicator),
            )
            if not rows
        ]
        if missing_sources:
            add("missing_financial_data", "medium", ",".join(missing_sources))
        if metrics.get("roe") is not None and metrics["roe"] < 5:
            add("low_roe", "high", f"ROE {metrics['roe']:.2f}%")
        if metrics.get("revenue_growth_yoy") is not None and metrics["revenue_growth_yoy"] < 0:
            add("negative_revenue_growth", "high", f"YoY {metrics['revenue_growth_yoy']:.2f}%")
        if metrics.get("debt_equity") is not None and metrics["debt_equity"] > 2.5:
            add("high_leverage", "high", f"debt/equity {metrics['debt_equity']:.2f}")
        if metrics.get("current_ratio") is not None and metrics["current_ratio"] < 1.0:
            add("weak_liquidity", "medium", f"current ratio {metrics['current_ratio']:.2f}")
        if metrics.get("gross_margin_trend") is not None and metrics["gross_margin_trend"] < -3.0:
            add("gross_margin_deterioration", "medium", f"{metrics['gross_margin_trend']:.2f}pp")
        if metrics.get("fcf_yield") is not None and metrics["fcf_yield"] < 0:
            add("negative_fcf_yield", "medium", f"{metrics['fcf_yield']:.2f}%")
        if valuation.get("pe_percentile_5y") is not None and valuation["pe_percentile_5y"] >= 85:
            add("high_pe_percentile", "medium", f"PE percentile {valuation['pe_percentile_5y']:.1f}")
        if valuation.get("pb_percentile_5y") is not None and valuation["pb_percentile_5y"] >= 90:
            add("high_pb_percentile", "medium", f"PB percentile {valuation['pb_percentile_5y']:.1f}")
        if valuation.get("peg") is not None and valuation["peg"] > 2.0:
            add("expensive_growth", "medium", f"PEG {valuation['peg']:.2f}")
        if peer_comparison.get("peer_sample_size", 0) < 3:
            add("insufficient_peer_sample", "low", "SW L3 peer comparison has fewer than 3 peers")
        return flags

    def _score(
        self,
        metrics: dict[str, Any],
        valuation: dict[str, Any],
        peer_comparison: dict[str, Any],
        red_flags: list[dict[str, Any]],
    ) -> dict[str, Any]:
        profitability = (
            _score_high(metrics.get("roe"), 0.0, 20.0) * 0.6
            + _score_high(metrics.get("roa"), 0.0, 12.0) * 0.4
        )
        balance = (
            _score_low(metrics.get("debt_equity"), 0.0, 3.0) * 0.55
            + _score_current_ratio(metrics.get("current_ratio")) * 0.45
        )
        growth = (
            _score_high(metrics.get("revenue_growth_yoy"), -10.0, 30.0) * 0.7
            + _score_high(metrics.get("gross_margin_trend"), -5.0, 5.0) * 0.3
        )
        cashflow = _score_high(metrics.get("fcf_yield"), -5.0, 8.0)
        valuation_score = (
            _score_low(valuation.get("pe_percentile_5y"), 0.0, 100.0) * 0.35
            + _score_low(valuation.get("pb_percentile_5y"), 0.0, 100.0) * 0.25
            + _score_peg(valuation.get("peg")) * 0.4
        )
        peer_percentiles = [
            metric.get("percentile")
            for metric in (peer_comparison.get("metrics") or {}).values()
            if metric.get("percentile") is not None
        ]
        peer_score = sum(peer_percentiles) / len(peer_percentiles) if peer_percentiles else 50.0

        composite = (
            profitability * 0.30
            + balance * 0.15
            + growth * 0.20
            + cashflow * 0.10
            + valuation_score * 0.15
            + peer_score * 0.10
        )
        severity_penalty = {"high": 8.0, "medium": 4.0, "low": 1.5}
        composite -= sum(severity_penalty.get(flag.get("severity"), 0.0) for flag in red_flags)
        composite = round(_clamp(composite), 2)
        return {
            "profitability": round(profitability, 2),
            "balance_sheet": round(balance, 2),
            "growth": round(growth, 2),
            "cashflow": round(cashflow, 2),
            "valuation": round(valuation_score, 2),
            "peer_relative": round(peer_score, 2),
            "composite": composite,
            "quality_score": composite,
            "scale": "0-100",
        }


def analyze_fundamentals(
    ts_code: str,
    as_of: str | None = None,
    reader: Any | None = None,
    peer_limit: int = 25,
) -> dict[str, Any]:
    """Return a production report dict for one stock's fundamentals."""

    return FundamentalAnalyzer(reader=reader, peer_limit=peer_limit).analyze(ts_code, as_of)

