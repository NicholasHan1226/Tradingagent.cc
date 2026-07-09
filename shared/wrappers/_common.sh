#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

if [[ -z "${TRADINGAGENT_ENV_LOADER_READY:-}" ]]; then
    # shellcheck disable=SC1091
    source "${SHARED_DIR}/env_loader.sh"
fi

timestamp() {
    date '+%Y-%m-%dT%H:%M:%S%z'
}

ensure_cron_paths() {
    mkdir -p "${TRADINGS_CRON_LOG_ROOT}" "${TRADINGS_STATE_ROOT}" "$(dirname "${TRADINGS_REPAIR_QUEUE}")"
}

record_level3_queue() {
    local job_name="$1"
    local phase="$2"
    local fallback_target="$3"
    local exit_code="$4"
    ensure_cron_paths
    printf '{"ts":"%s","job":"%s","phase":"%s","fallback_target":"%s","exit_code":%s}\n' \
        "$(timestamp)" "${job_name}" "${phase}" "${fallback_target}" "${exit_code}" >> "${TRADINGS_REPAIR_QUEUE}"
}

sharedsignals_source_gate() {
    local job_name="${1:-trading_job}"
    local phase="${2:-intraday}"
    local market="${3:-}"
    local gate_enabled="${TRADINGAGENT_SOURCE_STATUS_GATE:-1}"
    if [[ "${gate_enabled}" == "0" ]]; then
        return 0
    fi

    ensure_cron_paths
    local log_file="${TRADINGS_CRON_LOG_ROOT}/${job_name}.log"
    local api_url="${SHAREDSIGNALS_API_URL:-http://127.0.0.1:8082}"
    local output=""
    local exit_code=0
    set +e
    output="$(PYTHONPATH="${TRADINGAGENT_ROOT}" "${PYTHON_BIN}" -m shared.runtime_test.sharedsignals_source_status --base-url "${api_url}" --market "${market}" --require-not-red --json 2>&1)"
    exit_code=$?
    set -e
    if (( exit_code != 0 )); then
        printf '[%s] %s blocked=sharedsignals_source_status phase=%s market=%s detail=%q\n' \
            "$(timestamp)" "${job_name}" "${phase}" "${market}" "${output}" >> "${log_file}"
        return "${exit_code}"
    fi
    printf '[%s] %s sharedsignals_source_status=%q phase=%s market=%s action=continue\n' \
        "$(timestamp)" "${job_name}" "${output}" "${phase}" "${market}" >> "${log_file}"
    return 0
}

run_job() {
    local job_name="$1"
    local phase="$2"
    local fallback_target="$3"
    shift 3

    ensure_cron_paths
    cd "${TRADINGAGENT_ROOT}"

    local log_file="${TRADINGS_CRON_LOG_ROOT}/${job_name}.log"
    local lock_file="${TRADINGS_STATE_ROOT}/${job_name}.lock"
    local -a backoff=(1 5 25)
    local attempts="${LEVEL1_RETRIES:-3}"
    local attempt=1
    local exit_code=0

    exec {lock_fd}>"${lock_file}"
    if command -v flock >/dev/null 2>&1; then
        if ! flock -n "${lock_fd}"; then
            printf '[%s] %s skipped=already_running phase=%s
' "$(timestamp)" "${job_name}" "${phase}" >> "${log_file}"
            return 0
        fi
    fi

    while (( attempt <= attempts )); do
        printf '[%s] %s attempt=%s phase=%s\n' "$(timestamp)" "${job_name}" "${attempt}" "${phase}" >> "${log_file}"
        set +e
        "$@" >> "${log_file}" 2>&1
        exit_code=$?
        set -e
        if (( exit_code == 0 )); then
            printf '[%s] %s success attempt=%s\n' "$(timestamp)" "${job_name}" "${attempt}" >> "${log_file}"
            return 0
        fi

        printf '[%s] %s failure attempt=%s exit_code=%s\n' \
            "$(timestamp)" "${job_name}" "${attempt}" "${exit_code}" >> "${log_file}"

        if (( attempt >= attempts )); then
            break
        fi

        sleep "${backoff[$((attempt - 1))]}"
        attempt=$((attempt + 1))
    done

    printf '[%s] %s level2_hint=retry_within_%s_or_merge_to_%s\n' \
        "$(timestamp)" "${job_name}" "${phase}" "${fallback_target}" >> "${log_file}"
    record_level3_queue "${job_name}" "${phase}" "${fallback_target}" "${exit_code}"
    return "${exit_code}"
}
