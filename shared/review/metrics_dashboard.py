#!/usr/bin/env python3
"""Signal funnel metrics for production readiness tracking."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / 'metrics_dashboard.json'

def compute():
    perf_dir = Path(__file__).resolve().parent
    metrics = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'coverage': {'candidates_scanned': 0, 'signals_generated': 0, 'coverage_pct': 0},
        'success': {'signals_fired': 0, 'successful': 0, 'success_rate': 0},
        'capital': {'deployed_pct': 0, 'idle_pct': 100, 'turnover': 0},
        'styles': {},
        'markets': {}
    }
    # Load style performance
    for m in ['Ashare','Crypto','PM','US']:
        perf_file = perf_dir / m / 'style_performance.jsonl'
        if perf_file.exists():
            raw = perf_file.read_text().strip()
            lines = raw.split('\n') if raw else []
            metrics['markets'][m] = {'total_runs': len(lines), 'latest': None}
            if lines and lines[-1].strip():
                metrics['markets'][m]['latest'] = json.loads(lines[-1])
    return metrics

if __name__ == '__main__':
    m = compute()
    OUT.write_text(json.dumps(m, indent=2, ensure_ascii=False))
    print(f'Metrics written to {OUT}')
