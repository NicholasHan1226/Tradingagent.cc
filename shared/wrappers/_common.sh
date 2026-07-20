#!/bin/bash
set -euo pipefail

timestamp() {
    date '+%Y-%m-%dT%H:%M:%S%z'
}

# The fresh A-share architecture is not allowed to fall back to the retired
# wrappers/readers.  This is intentionally not controlled by an environment
# opt-in: reactivation requires an explicit reviewed code change and a fresh
# retirement/cutover audit.
block_retired_ashare_runtime() {
    local job_name="${1:-legacy_ashare_job}"
    printf '[%s] %s blocked=retired_ashare_runtime action=use_fresh_day_loop_or_fixture\n' \
        "$(timestamp)" "${job_name}" >&2
    return 78
}

block_retired_legacy_runtime() {
    local job_name="${1:-legacy_tradingagent_job}"
    printf '[%s] %s blocked=retired_legacy_runtime action=wait_for_tradingdatas_fresh_handoff_and_reviewed_replacement\n' \
        "$(timestamp)" "${job_name}" >&2
    return 78
}

retired_legacy_runtime_job_for() {
    local entrypoint_name="${1##*/}"
    local market_arg="${2:-}"
    case "${entrypoint_name}" in
        auto_pipeline.sh|job_self_heal.sh|job_self_heal_night.sh|job_cn_futures_observation_report.sh|job_cn_futures_sample_ops.sh|job_cn_futures_calibration_report.sh|job_cn_futures_replay.sh|job_cn_futures_pre_open_validation.sh|job_cn_futures_opening_validation.sh|job_cn_futures_first_sample_alert.sh|job_crypto_sim.sh|job_cn_futures_sim.sh)
            printf '%s' "${entrypoint_name%.sh}"
            ;;
        job_market_capital_reconcile.sh)
            if [[ "${market_arg}" != "ashare" ]]; then
                printf '%s' "job_market_capital_reconcile_${market_arg:-unspecified}"
            fi
            ;;
    esac
}

# One shell authority for entrypoints that may no longer execute.  Keep this
# list limited to A-share-only wrappers plus generic jobs whose whole legacy
# behavior (old A-share readers or external email) is retired.  Mixed jobs with
# useful Crypto/CNFutures branches must filter A-share instead of joining this
# list. Retired US/PM/HK packages and their dedicated wrappers no longer exist.
retired_ashare_runtime_job_for() {
    local entrypoint_name="${1##*/}"
    local market_arg="${2:-}"
    case "${entrypoint_name}" in
        job_ashare_first_sample_alert.sh|job_ashare_health_check.sh|job_ashare_night_calibration.sh|job_ashare_opening_validation.sh|job_ashare_pre_open_validation.sh|job_ashare_preopen_dry_run.sh|job_ashare_research_evidence.sh|job_ashare_sample_ops.sh|job_ashare_sim_exec.sh|job_opportunity_funnel_sync.sh)
            printf '%s' "${entrypoint_name%.sh}"
            ;;
        job_premarket_signals.sh)
            printf '%s' "job_premarket_signals_legacy_ashare_data"
            ;;
        health_check.sh|daily_review.sh|job_opening_acceptance.sh)
            printf '%s' "${entrypoint_name%.sh}_mixed_legacy_data"
            ;;
        job_daily_brief_morning.sh|job_daily_brief_day.sh|job_daily_brief_night.sh|job_email_notify.sh)
            printf '%s' "${entrypoint_name%.sh}_legacy_data_and_email"
            ;;
        job_market_capital_reconcile.sh)
            if [[ "${market_arg}" == "ashare" ]]; then
                printf '%s' "job_market_capital_reconcile_ashare"
            fi
            ;;
    esac
}

_tradingagent_calling_entrypoint="${BASH_SOURCE[1]:-}"
_tradingagent_retired_job="$(
    retired_ashare_runtime_job_for \
        "${_tradingagent_calling_entrypoint}" \
        "${1:-}"
)"
if [[ -n "${_tradingagent_retired_job}" ]]; then
    block_retired_ashare_runtime "${_tradingagent_retired_job}"
    exit 78
fi
unset _tradingagent_calling_entrypoint _tradingagent_retired_job

_tradingagent_calling_entrypoint="${BASH_SOURCE[1]:-}"
_tradingagent_retired_job="$(
    retired_legacy_runtime_job_for \
        "${_tradingagent_calling_entrypoint}" \
        "${1:-}"
)"
if [[ -n "${_tradingagent_retired_job}" ]]; then
    block_retired_legacy_runtime "${_tradingagent_retired_job}"
    exit 78
fi
unset _tradingagent_calling_entrypoint _tradingagent_retired_job

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

if [[ -z "${TRADINGAGENT_ENV_LOADER_READY:-}" ]]; then
    # shellcheck disable=SC1091
    source "${SHARED_DIR}/env_loader.sh"
fi

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

tradingdatas_v1_runtime_gate() {
    local job_name="${1:-trading_job}"
    local phase="${2:-intraday}"
    local market="${3:-}"
    ensure_cron_paths
    local log_file="${TRADINGS_CRON_LOG_ROOT}/${job_name}.log"
    local -a required_config=(
        TRADINGDATAS_API_URL
        TRADINGDATAS_CATALOG_VERSION
        TRADINGDATAS_ACCESS_POLICY_ID
        TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON
        TRADINGDATAS_SCHEMA_MAJOR
        TRADINGDATAS_RUNTIME_TRANSPORT
    )
    local variable_name=""
    for variable_name in "${required_config[@]}"; do
        if [[ -z "${!variable_name:-}" ]]; then
            printf '[%s] %s blocked=missing_v1_config variable=%s phase=%s market=%s\n' \
                "$(timestamp)" "${job_name}" "${variable_name}" "${phase}" "${market}" \
                | tee -a "${log_file}" >&2
            return 78
        fi
    done

    local output=""
    local exit_code=0
    set +e
    output="$(PYTHONPATH="${TRADINGAGENT_ROOT}" "${PYTHON_BIN}" -m shared.runtime_test.sharedsignals_v1_gate --market "${market}" --json 2>&1)"
    exit_code=$?
    set -e
    if (( exit_code != 0 )); then
        printf '[%s] %s blocked=tradingdatas_v1_runtime_gate phase=%s market=%s detail=%q\n' \
            "$(timestamp)" "${job_name}" "${phase}" "${market}" "${output}" \
            | tee -a "${log_file}" >&2
        return 78
    fi
    printf '[%s] %s tradingdatas_v1_runtime_gate=%q phase=%s market=%s action=evidence_ready\n' \
        "$(timestamp)" "${job_name}" "${output}" "${phase}" "${market}" >> "${log_file}"
    return 0
}

block_unmigrated_tradingdatas_consumer() {
    local job_name="${1:-trading_job}"
    local market="${2:-}"
    ensure_cron_paths
    local log_file="${TRADINGS_CRON_LOG_ROOT}/${job_name}.log"
    printf '[%s] %s blocked=legacy_consumer_retirement_pending market=%s action=migrate_business_reader_to_v1_before_reenable\n' \
        "$(timestamp)" "${job_name}" "${market}" \
        | tee -a "${log_file}" >&2
    return 78
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
