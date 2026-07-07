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
SHAREDSIGNALS_API_BASE_URL="${SHAREDSIGNALS_API_BASE_URL:-http://127.0.0.1:${SHAREDSIGNALS_API_PORT:-8082}}"
SHAREDSIGNALS_HEALTH_URL="${SHAREDSIGNALS_API_HEALTH_URL:-${SHAREDSIGNALS_API_BASE_URL}/health}"
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

if [ -r "${ROOT}/.env" ]; then
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
  SHAREDSIGNALS_API_BASE_URL="${SHAREDSIGNALS_API_BASE_URL}" \
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


def http_json(url: str, timeout_seconds: float) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
        body = resp.read(65536).decode("utf-8", errors="replace")
        status_code = int(getattr(resp, "status", 200))
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw": body[:200]}
    return status_code, payload if isinstance(payload, dict) else {"data": payload}


def check_sharedsignals(base_url: str, health_url: str) -> dict:
    base = base_url.rstrip("/")
    cache_url = f"{base}/cache/status"
    capability_url = f"{base}/capabilities"
    errors: list[str] = []
    cache_status_code = 0
    cache_payload: dict = {}
    capability_status_code = 0
    capability_payload: dict = {}
    for attempt in range(1, 3):
        try:
            cache_status_code, cache_payload = http_json(cache_url, 3)
            capability_status_code, capability_payload = http_json(capability_url, 5)
            break
        except (OSError, urllib.error.URLError) as exc:
            errors.append(f"attempt={attempt} error={exc}")
            if attempt < 2:
                time.sleep(2)
    capability_data = capability_payload.get("data") if isinstance(capability_payload.get("data"), dict) else {}
    endpoint_count = len(capability_data.get("endpoints") or []) if isinstance(capability_data, dict) else 0
    cache_ok = 200 <= cache_status_code < 300 and int(cache_payload.get("functions_registered") or 0) > 0
    capability_ok = 200 <= capability_status_code < 300 and endpoint_count > 0
    if not (cache_ok and capability_ok):
        return {
            "status": "critical",
            "cache_url": cache_url,
            "cache_status_code": cache_status_code,
            "capability_url": capability_url,
            "capability_status_code": capability_status_code,
            "capability_endpoint_count": endpoint_count,
            "attempts": 2,
            "errors": errors[-3:],
        }
    health_status_code = 0
    health_payload: dict = {}
    health_error = ""
    try:
        health_status_code, health_payload = http_json(health_url, 2)
    except (OSError, urllib.error.URLError) as exc:
        health_error = str(exc)
    health_payload_status = str(health_payload.get("status") or "")
    health_ok = 200 <= health_status_code < 300 and health_payload_status in {"ok", "degraded", "healthy"}
    return {
        "status": "ok" if health_ok else "degraded",
        "cache_url": cache_url,
        "cache_status_code": cache_status_code,
        "functions_registered": cache_payload.get("functions_registered"),
        "capability_url": capability_url,
        "capability_status_code": capability_status_code,
        "capability_endpoint_count": endpoint_count,
        "health_url": health_url,
        "health_status_code": health_status_code,
        "health_payload_status": health_payload_status,
        "health_error": health_error,
    }


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


def check_combined_crontab(mg_root: Path) -> dict:
    script = mg_root / "deploy" / "install_combined_crontab.sh"
    if not script.exists():
        return {"status": "critical", "script": str(script), "reason": "combined_crontab_check_missing"}
    try:
        import subprocess

        result = subprocess.run(
            [str(script), "--check"],
            cwd=str(mg_root),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return {"status": "critical", "script": str(script), "error": f"{exc.__class__.__name__}: {exc}"}
    ok = result.returncode == 0
    return {
        "status": "ok" if ok else "critical",
        "script": str(script),
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }


def worse(statuses: list[str]) -> str:
    order = {"ok": 0, "degraded": 1, "critical": 2}
    return max(statuses, key=lambda item: order.get(item, 0))


root = Path(os.environ["TRADINGAGENT_ROOT"])
mg_root = Path(os.environ["MARKETGRAPH_ROOT"])
result = {
    "timestamp": now_iso(),
    "source": "tradingagent/cron/health_check.sh",
    "sharedsignals_api": check_sharedsignals(os.environ["SHAREDSIGNALS_API_BASE_URL"], os.environ["SHAREDSIGNALS_HEALTH_URL"]),
    "tradingagent_sim_output": check_sim_output(root, int(os.environ["MAX_SIM_OUTPUT_AGE_MIN"])),
    "marketgraph_freshness": check_marketgraph(mg_root, int(os.environ["MAX_MARKETGRAPH_AGE_MIN"])),
    "combined_crontab": check_combined_crontab(mg_root),
}
result["status"] = worse([
    result["sharedsignals_api"]["status"],
    result["tradingagent_sim_output"]["status"],
    result["marketgraph_freshness"]["status"],
    result["combined_crontab"]["status"],
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
