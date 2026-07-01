#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_ashare_sim_exec"
PHASE="intraday"
LEVEL3_TARGET="next_tick_continue"
ENTRYPOINT="${WRAPPER_DIR}/tradings_cron_entry.py"

# Intraday simulated execution must not block on live LLM debate calls.
export TRADINGS_DEBATE_MODE="${TRADINGS_DEBATE_MODE:-fast}"
export TRADINGS_DEEPSEEK_TIMEOUT="${TRADINGS_DEEPSEEK_TIMEOUT:-15}"
export TRADINGS_DEEPSEEK_RETRIES="${TRADINGS_DEEPSEEK_RETRIES:-1}"

ensure_cron_paths
market_open="$(${PYTHON_BIN} - <<'PYSRV'
from datetime import datetime
now = datetime.now()
hm = now.strftime("%H:%M")
open_now = now.weekday() < 5 and (("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "14:57"))
print("Y" if open_now else "N")
PYSRV
)"
if [[ "${market_open}" != "Y" ]]; then
    printf '[%s] %s skipped=market_closed phase=%s\n' "$(timestamp)" "${JOB_NAME}" "${PHASE}" >> "${TRADINGS_CRON_LOG_ROOT}/${JOB_NAME}.log"
    exit 0
fi

MINI_HEALTH_URL="${ASHARE_SIM_MINI_HEALTH_URL:-http://127.0.0.1:9865/health}"
MINI_BUSY_LIMIT="${ASHARE_SIM_MINI_BUSY_LIMIT:-0}"
mini_health_state="$(${PYTHON_BIN} - "${MINI_HEALTH_URL}" <<'PY'
import json
import sys
import urllib.request

def clean(value):
    return str(value or "").replace("\t", " ").replace("\n", " ")[:240]

try:
    with urllib.request.urlopen(sys.argv[1], timeout=4) as resp:
        data = json.load(resp)
    busy = int(data.get("pending", 0)) + int(data.get("in_progress", 0))
    expired = int(data.get("expired_pending", 0))
    if data.get("halted") or data.get("execution_status") == "halted":
        print("\t".join([
            "HALTED",
            str(busy),
            str(expired),
            clean(data.get("halt_signal_id")),
            clean(data.get("halt_reason")),
        ]))
    else:
        print("\t".join(["READY", str(busy), str(expired), "", ""]))
except Exception as exc:
    print("\t".join(["ERR", clean(exc), "", "", ""]))
PY
)"
IFS=$'\t' read -r mini_status mini_busy mini_expired_pending mini_halt_signal mini_halt_reason <<< "${mini_health_state}"

ensure_cron_paths
if [[ "${mini_status}" == "ERR" ]]; then
    printf '[%s] %s skipped=mini_health_unavailable detail=%q\n' "$(timestamp)" "${JOB_NAME}" "${mini_busy}" >> "${TRADINGS_CRON_LOG_ROOT}/${JOB_NAME}.log"
    exit 0
fi
if [[ "${mini_status}" == "HALTED" ]]; then
    printf '[%s] %s skipped=mini_halted busy=%s expired_pending=%s signal=%q reason=%q\n' "$(timestamp)" "${JOB_NAME}" "${mini_busy}" "${mini_expired_pending}" "${mini_halt_signal}" "${mini_halt_reason}" >> "${TRADINGS_CRON_LOG_ROOT}/${JOB_NAME}.log"
    exit 0
fi
if (( mini_busy > MINI_BUSY_LIMIT )); then
    printf '[%s] %s skipped=mini_busy busy=%s expired_pending=%s limit=%s\n' "$(timestamp)" "${JOB_NAME}" "${mini_busy}" "${mini_expired_pending}" "${MINI_BUSY_LIMIT}" >> "${TRADINGS_CRON_LOG_ROOT}/${JOB_NAME}.log"
    exit 0
fi

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" "${ENTRYPOINT}" --job "${JOB_NAME}"
