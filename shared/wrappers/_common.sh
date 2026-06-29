#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

if [[ -z "${TRADINGS_ENV_LOADER_READY:-}" ]]; then
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

run_job() {
    local job_name="$1"
    local phase="$2"
    local fallback_target="$3"
    shift 3

    ensure_cron_paths

    local log_file="${TRADINGS_CRON_LOG_ROOT}/${job_name}.log"
    local -a backoff=(1 5 25)
    local attempts="${LEVEL1_RETRIES:-3}"
    local attempt=1
    local exit_code=0

    while (( attempt <= attempts )); do
        printf '[%s] %s attempt=%s phase=%s\n' "$(timestamp)" "${job_name}" "${attempt}" "${phase}" >> "${log_file}"
        if "$@" >> "${log_file}" 2>&1; then
            printf '[%s] %s success attempt=%s\n' "$(timestamp)" "${job_name}" "${attempt}" >> "${log_file}"
            return 0
        fi

        exit_code=$?
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
