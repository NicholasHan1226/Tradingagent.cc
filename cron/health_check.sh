#!/bin/bash
# Cross-system health probe for TradingAgent, reported to SharedSignals watchdog.
TIMEOUT="${TRADINGAGENT_HEALTH_TIMEOUT:-120}"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-python3}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/health_check.log"
LOCK_FILE="${LOCK_DIR}/health_check.lock"

SHAREDSIGNALS_ROOT="${SHAREDSIGNALS_ROOT:-$(cd "${ROOT}/../SharedSignals" 2>/dev/null && pwd || printf "/opt/investment/SharedSignals")}"
MARKETGRAPH_ROOT="${MARKETGRAPH_ROOT:-$(cd "${ROOT}/../MarketGraph" 2>/dev/null && pwd || printf "/opt/investment/MarketGraph")}"
WATCHDOG_INPUT_DIR="${WATCHDOG_INPUT_DIR:-${SHAREDSIGNALS_ROOT}/logs/watchdog_inputs}"
OUTPUT_FILE="${WATCHDOG_INPUT_DIR}/tradingagent_health.json"
OUTPUT_JSONL="${WATCHDOG_INPUT_DIR}/tradingagent_health.jsonl"
SHAREDSIGNALS_HEALTH_URL="${SHAREDSIGNALS_API_HEALTH_URL:-http://127.0.0.1:${SHAREDSIGNALS_API_PORT:-8082}/health}"
MAX_SIM_OUTPUT_AGE_MIN="${TRADINGAGENT_SIM_OUTPUT_MAX_AGE_MIN:-180}"
MAX_MARKETGRAPH_AGE_MIN="${MARKETGRAPH_FRESHNESS_MAX_AGE_MIN:-1440}"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}" "${WATCHDOG_INPUT_DIR}"
exec 2>>"${LOG_FILE}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP health_check already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

{
  echo "[$(date -Iseconds)] START health_check"
  TRADINGAGENT_ROOT="${ROOT}" \
  SHAREDSIGNALS_ROOT="${SHAREDSIGNALS_ROOT}" \
  MARKETGRAPH_ROOT="${MARKETGRAPH_ROOT}" \
  SHAREDSIGNALS_HEALTH_URL="${SHAREDSIGNALS_HEALTH_URL}" \
  OUTPUT_FILE="${OUTPUT_FILE}" \
  OUTPUT_JSONL="${OUTPUT_JSONL}" \
  MAX_SIM_OUTPUT_AGE_MIN="${MAX_SIM_OUTPUT_AGE_MIN}" \
  MAX_MARKETGRAPH_AGE_MIN="${MAX_MARKETGRAPH_AGE_MIN}" \
  PYTHONPATH="${ROOT}" timeout "${TIMEOUT}" "${PYTHON_BIN}" - <<'PY'
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    return round(max(0.0, (time.time() - path.stat().st_mtime) / 60.0), 2)


def check_sharedsignals(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            body = resp.read(65536).decode("utf-8", errors="replace")
        status_code = getattr(resp, "status", 200)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw": body[:200]}
        ok = 200 <= int(status_code) < 300
        return {"status": "ok" if ok else "critical", "url": url, "status_code": status_code, "payload_status": payload.get("status") if isinstance(payload, dict) else None}
    except (OSError, urllib.error.URLError) as exc:
        return {"status": "critical", "url": url, "error": str(exc)}


def check_sim_output(root: Path, max_age: int) -> dict:
    candidates = sorted((root / "shared" / "review").glob("*/style_comparison.json"))
    if not candidates:
        return {"status": "critical", "reason": "style_comparison_missing", "latest_file": "", "age_minutes": None}
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    age = file_age_minutes(latest)
    status = "ok" if age is not None and age <= max_age else "degraded"
    return {"status": status, "latest_file": str(latest), "age_minutes": age, "max_age_minutes": max_age}


def check_marketgraph(root: Path, max_age: int) -> dict:
    candidates = [
        root / "data" / "all_weather_regime.csv",
        root / "data" / "market_knowledge_packages.csv",
        root / "data" / "intake" / "gate_health.csv",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return {"status": "critical", "reason": "marketgraph_freshness_files_missing", "latest_file": "", "age_minutes": None}
    latest = max(existing, key=lambda item: item.stat().st_mtime)
    age = file_age_minutes(latest)
    status = "ok" if age is not None and age <= max_age else "degraded"
    return {"status": status, "latest_file": str(latest), "age_minutes": age, "max_age_minutes": max_age}


def worse(statuses: list[str]) -> str:
    order = {"ok": 0, "degraded": 1, "critical": 2}
    return max(statuses, key=lambda item: order.get(item, 0))


root = Path(os.environ["TRADINGAGENT_ROOT"])
mg_root = Path(os.environ["MARKETGRAPH_ROOT"])
result = {
    "timestamp": now_iso(),
    "source": "tradingagent/cron/health_check.sh",
    "sharedsignals_api": check_sharedsignals(os.environ["SHAREDSIGNALS_HEALTH_URL"]),
    "tradingagent_sim_output": check_sim_output(root, int(os.environ["MAX_SIM_OUTPUT_AGE_MIN"])),
    "marketgraph_freshness": check_marketgraph(mg_root, int(os.environ["MAX_MARKETGRAPH_AGE_MIN"])),
}
result["status"] = worse([
    result["sharedsignals_api"]["status"],
    result["tradingagent_sim_output"]["status"],
    result["marketgraph_freshness"]["status"],
])

output_file = Path(os.environ["OUTPUT_FILE"])
output_file.parent.mkdir(parents=True, exist_ok=True)
output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
with Path(os.environ["OUTPUT_JSONL"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["status"] != "critical" else 2)
PY
  echo "[$(date -Iseconds)] OK health_check"
} >> "${LOG_FILE}" 2>&1
