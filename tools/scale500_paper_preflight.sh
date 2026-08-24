#!/bin/bash
# One bounded, simulation-only scale500 preflight.  It deliberately owns a
# throwaway state root and must never overlap an active production service.

set -eu

PYTHON="${PREFLIGHT_PYTHON:-/opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1/bin/python3}"
REPO_ROOT="${PREFLIGHT_REPO_ROOT:-/opt/investment/releases/tradingagent/current}"
TOKEN_FILE="${PREFLIGHT_TOKEN_FILE:-/run/secrets/tradingagent/tradingdatas-read.token}"
ENV_FILE="${PREFLIGHT_ENV_FILE:-/etc/tradingagent/ashare-minute-scale500.env}"
MAX_SECONDS="${PREFLIGHT_MAX_SECONDS:-150}"
SCALE500_TIMER="tradingagent-ashare-minute-scale500-paper.timer"
SCALE500_SERVICE="tradingagent-ashare-minute-scale500-paper.service"

fail2() { echo "PREFLIGHT_FAIL $1"; exit 2; }

case "$MAX_SECONDS" in
  ''|*[!0-9]*) fail2 "max_seconds_invalid" ;;
esac
[ "$MAX_SECONDS" -gt 0 ] || fail2 "max_seconds_invalid"

# The immutable-universe guard is enforced by minute_scale500_runtime under
# the service user; root cannot make a meaningful W_OK assertion itself.
RUN_USER="${PREFLIGHT_RUN_USER:-tradingagent}"
if [ "$(id -un)" != "$RUN_USER" ]; then
  exec sudo -u "$RUN_USER" \
    PREFLIGHT_PYTHON="$PYTHON" PREFLIGHT_REPO_ROOT="$REPO_ROOT" \
    PREFLIGHT_TOKEN_FILE="$TOKEN_FILE" PREFLIGHT_ENV_FILE="$ENV_FILE" \
    PREFLIGHT_MAX_SECONDS="$MAX_SECONDS" PREFLIGHT_RUN_USER="$RUN_USER" \
    bash "$0"
fi

[ -x "$PYTHON" ] || fail2 "python_missing:$PYTHON"
[ -f "$TOKEN_FILE" ] || fail2 "token_missing"
[ -f "$ENV_FILE" ] || fail2 "env_file_missing:$ENV_FILE"
if systemctl is-active --quiet "$SCALE500_TIMER"; then
  fail2 "production_timer_active_stop_it_first"
fi
if systemctl is-active --quiet "$SCALE500_SERVICE"; then
  fail2 "production_service_active_wait_for_completion"
fi

UNIVERSE_SOURCE=$(grep -h '^ASHARE_MINUTE_SCALE500_UNIVERSE_SOURCE=' "$ENV_FILE" | head -1 | cut -d= -f2-)
UNIVERSE_SHA=$(grep -h '^ASHARE_MINUTE_SCALE500_UNIVERSE_SHA256=' "$ENV_FILE" | head -1 | cut -d= -f2-)
[ -n "$UNIVERSE_SOURCE" ] && [ -n "$UNIVERSE_SHA" ] || fail2 "universe_env_unreadable"
PROD_STATE_ROOT=$(grep -h '^ASHARE_MINUTE_SCALE500_STATE_ROOT=' "$ENV_FILE" | head -1 | cut -d= -f2-)
[ -n "$PROD_STATE_ROOT" ] && [ -d "$PROD_STATE_ROOT" ] || fail2 "prod_state_root_unreadable:$PROD_STATE_ROOT"

STATE_ROOT=$(mktemp -d /tmp/scale500-preflight-state.XXXXXX)
ROLLBACK_ROOT=$(mktemp -d /tmp/scale500-preflight-rollback.XXXXXX)
trap 'rm -rf "$STATE_ROOT" "$ROLLBACK_ROOT"' EXIT

cd /tmp
TODAY=$(TZ=Asia/Shanghai date +%Y%m%d)
TEMPLATE_DAY=$(find "$PROD_STATE_ROOT" -maxdepth 1 -type d -name '20*' \
  | while read -r p; do [ "$(basename "$p")" -lt "$TODAY" ] && echo "$p"; done | sort | tail -1)
[ -n "$TEMPLATE_DAY" ] && [ -f "$TEMPLATE_DAY/reference-facts.json" ] \
  || fail2 "no_template_day_in_prod_state_root"
cp -r "$TEMPLATE_DAY" "$STATE_ROOT/" || fail2 "template_copy_failed"
echo "preflight: template day $(basename "$TEMPLATE_DAY") copied read-only"

export REAL_TRADING_ENABLED=false PYTHONPATH="$REPO_ROOT"
COMMON=(--rolling-eligible
  --scale-state-root "$STATE_ROOT"
  --rollback30-state-root "$ROLLBACK_ROOT"
  --token-file "$TOKEN_FILE"
  --universe-source "$UNIVERSE_SOURCE"
  --expected-universe-sha256 "$UNIVERSE_SHA")

echo "preflight: session init (throwaway state root) $(date '+%F %T')"
if ! "$PYTHON" -m Ashare.minute_scale500_runtime initialize "${COMMON[@]}"; then
  echo "PREFLIGHT_NO_GO init_failed"
  exit 1
fi

echo "preflight: timed single round $(date '+%F %T') max=${MAX_SECONDS}s"
START=$(date +%s)
if OUT=$("$PYTHON" -m Ashare.minute_scale500_runtime run "${COMMON[@]}" 2>&1); then
  RC=0
else
  RC=$?
fi
ELAPSED=$(( $(date +%s) - START ))

printf '%s\n' "$OUT"
echo "preflight: rc=$RC elapsed=${ELAPSED}s"

if printf '%s' "$OUT" | grep -q 'outside_delayed_session_window'; then
  fail2 "outside_session_window_run_inside_trading_hours"
fi
if [ "$RC" -ne 0 ]; then
  echo "PREFLIGHT_NO_GO runtime_failed rc=$RC elapsed=${ELAPSED}s"
  exit 1
fi
if [ "$ELAPSED" -le "$MAX_SECONDS" ]; then
  echo "PREFLIGHT_GO elapsed=${ELAPSED}s <= ${MAX_SECONDS}s"
  exit 0
fi
echo "PREFLIGHT_NO_GO elapsed=${ELAPSED}s > ${MAX_SECONDS}s"
exit 1
