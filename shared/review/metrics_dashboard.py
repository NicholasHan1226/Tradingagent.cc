#!/usr/bin/env python3
"""Signal funnel metrics for production readiness tracking."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / 'metrics_dashboard.json'

def compute(review_root: Path | str | None = None):
    perf_dir = Path(review_root) if review_root is not None else Path(__file__).resolve().parent
    metrics = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'coverage': {'candidates_scanned': 0, 'signals_generated': 0, 'coverage_pct': 0},
        'success': {'signals_fired': 0, 'successful': 0, 'success_rate': 0},
        'capital': {'deployed_pct': 0, 'idle_pct': 100, 'turnover': 0},
        'styles': {},
        'markets': {}
    }
    try:
        from shared.review.pnl_summary import sim_ledger_pnl_summary
        ledger_pnl = sim_ledger_pnl_summary()
    except Exception:  # noqa: BLE001
        ledger_pnl = {}
    # Load style performance
    for display_name, dir_name in [
        ('Ashare', 'Ashare'),
        ('Crypto', 'Crypto'),
        ('PM', 'PM'),
        ('US', 'US'),
        ('CNFutures', 'cn_futures'),
    ]:
        perf_file = perf_dir / dir_name / 'style_performance.jsonl'
        market_key = display_name.lower()
        market_metrics = {'total_runs': 0, 'latest': None}
        if perf_file.exists():
            raw = perf_file.read_text().strip()
            lines = raw.split('\n') if raw else []
            market_metrics['total_runs'] = len(lines)
            if lines and lines[-1].strip():
                market_metrics['latest'] = json.loads(lines[-1])
        ledger = ledger_pnl.get(market_key)
        if ledger:
            market_metrics['ledger_pnl'] = {
                'realized_pnl': ledger.get('realized_pnl'),
                'unrealized_pnl': ledger.get('unrealized_pnl'),
                'total_pnl': ledger.get('total_pnl'),
                'market_value': ledger.get('market_value'),
                'open_position_count': ledger.get('open_position_count'),
                'missing_mark_count': ledger.get('missing_mark_count'),
                'pnl_source': ledger.get('pnl_source'),
            }
        metrics['markets'][display_name] = market_metrics
    return metrics

if __name__ == '__main__':
    m = compute()
    OUT.write_text(json.dumps(m, indent=2, ensure_ascii=False))
    print(f'Metrics written to {OUT}')
