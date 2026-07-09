#!/bin/bash
# TradingAgent health probe. Cross-system checks use public HTTP APIs only.
TIMEOUT="${TRADINGAGENT_HEALTH_TIMEOUT:-120}"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-python3}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/health_check.log"
LOCK_FILE="${LOCK_DIR}/health_check.lock"

WATCHDOG_INPUT_DIR="${WATCHDOG_INPUT_DIR:-${ROOT}/shared/logs/health}"
OUTPUT_FILE="${WATCHDOG_INPUT_DIR}/tradingagent_health.json"
OUTPUT_JSONL="${WATCHDOG_INPUT_DIR}/tradingagent_health.jsonl"
SHAREDSIGNALS_API_BASE_URL="${SHAREDSIGNALS_API_BASE_URL:-http://127.0.0.1:${SHAREDSIGNALS_API_PORT:-8082}}"
SHAREDSIGNALS_HEALTH_URL="${SHAREDSIGNALS_API_HEALTH_URL:-${SHAREDSIGNALS_API_BASE_URL}/health}"
SHAREDSIGNALS_SOURCE_STATUS_URL="${SHAREDSIGNALS_SOURCE_STATUS_URL:-${SHAREDSIGNALS_API_BASE_URL}/source_status}"
MARKETGRAPH_API_BASE_URL="${MARKETGRAPH_API_BASE_URL:-${MARKETGRAPH_API_URL:-http://127.0.0.1:8080}}"
MARKETGRAPH_HEALTH_URL="${MARKETGRAPH_API_HEALTH_URL:-${MARKETGRAPH_API_BASE_URL%/}/health}"
MAX_SIM_OUTPUT_AGE_MIN="${TRADINGAGENT_SIM_OUTPUT_MAX_AGE_MIN:-180}"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}" "${WATCHDOG_INPUT_DIR}"
exec 2>>"${LOG_FILE}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP health_check already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

if [ -f "${ROOT}/.env" ] && head -c 0 "${ROOT}/.env" >/dev/null 2>&1; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

{
  echo "[$(date -Iseconds)] START health_check"
  TRADINGAGENT_ROOT="${ROOT}" \
  SHAREDSIGNALS_API_BASE_URL="${SHAREDSIGNALS_API_BASE_URL}" \
  SHAREDSIGNALS_HEALTH_URL="${SHAREDSIGNALS_HEALTH_URL}" \
  SHAREDSIGNALS_SOURCE_STATUS_URL="${SHAREDSIGNALS_SOURCE_STATUS_URL}" \
  MARKETGRAPH_HEALTH_URL="${MARKETGRAPH_HEALTH_URL}" \
  OUTPUT_FILE="${OUTPUT_FILE}" \
  OUTPUT_JSONL="${OUTPUT_JSONL}" \
  MAX_SIM_OUTPUT_AGE_MIN="${MAX_SIM_OUTPUT_AGE_MIN}" \
  PYTHONPATH="${ROOT}" timeout "${TIMEOUT}" "${PYTHON_BIN}" - <<'PY'
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from shared.runtime_test.sharedsignals_source_status import check_source_status


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
    source_status = check_source_status(base, timeout_seconds=3)
    overall_status = "ok" if health_ok else "degraded"
    if source_status["status"] == "critical":
        overall_status = "critical"
    elif source_status["status"] == "degraded" and overall_status == "ok":
        overall_status = "degraded"
    return {
        "status": overall_status,
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
        "source_status": source_status,
    }


def check_sim_output(root: Path, max_age: int) -> dict:
    health_path = root / "shared" / "runtime_test" / "sim_market_health_latest.json"
    health_age = file_age_minutes(health_path)
    if health_path.exists() and health_age is not None and health_age <= max_age:
        try:
            payload = json.loads(health_path.read_text(encoding="utf-8"))
        except Exception as exc:
            payload = {"overall_status": "warn", "error": f"{exc.__class__.__name__}: {exc}"}
        overall_status = str(payload.get("overall_status") or payload.get("status") or "warn").lower()
        status = "ok" if overall_status in {"pass", "ok"} else ("critical" if overall_status in {"fail", "critical"} else "degraded")
        return {
            "status": status,
            "source": "sim_market_health_latest",
            "latest_file": str(health_path),
            "age_minutes": health_age,
            "max_age_minutes": max_age,
            "overall_status": overall_status,
            "summary": payload.get("summary", {}),
        }
    candidates = sorted((root / "shared" / "review").glob("*/style_comparison.json"))
    if not candidates:
        return {"status": "critical", "reason": "style_comparison_missing", "latest_file": "", "age_minutes": None}
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    age = file_age_minutes(latest)
    status = "ok" if age is not None and age <= max_age else "degraded"
    return {"status": status, "latest_file": str(latest), "age_minutes": age, "max_age_minutes": max_age}


def check_marketgraph_api(health_url: str) -> dict:
    try:
        status_code, payload = http_json(health_url, 3)
    except (OSError, urllib.error.URLError) as exc:
        return {"status": "degraded", "health_url": health_url, "error": str(exc)}
    payload_status = str(payload.get("status") or payload.get("overall_status") or "").lower()
    ok = 200 <= status_code < 300 and payload_status in {"", "ok", "healthy", "degraded"}
    return {
        "status": "ok" if ok else "degraded",
        "health_url": health_url,
        "status_code": status_code,
        "payload_status": payload_status,
    }


def worse(statuses: list[str]) -> str:
    order = {"ok": 0, "degraded": 1, "critical": 2}
    return max(statuses, key=lambda item: order.get(item, 0))


root = Path(os.environ["TRADINGAGENT_ROOT"])
result = {
    "timestamp": now_iso(),
    "source": "tradingagent/cron/health_check.sh",
    "sharedsignals_api": check_sharedsignals(os.environ["SHAREDSIGNALS_API_BASE_URL"], os.environ["SHAREDSIGNALS_HEALTH_URL"]),
    "tradingagent_sim_output": check_sim_output(root, int(os.environ["MAX_SIM_OUTPUT_AGE_MIN"])),
    "marketgraph_api": check_marketgraph_api(os.environ["MARKETGRAPH_HEALTH_URL"]),
}
result["status"] = worse([
    result["sharedsignals_api"]["status"],
    result["tradingagent_sim_output"]["status"],
    result["marketgraph_api"]["status"],
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
