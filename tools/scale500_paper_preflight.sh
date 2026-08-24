#!/bin/bash
# scale500 纸面管道单轮干跑演练（preflight / go-no-go gate）。
#
# 解决的重复问题：08-18 宇宙扩容后单轮真实耗时 13–22 分钟、塞不进 5 分钟节拍，
# 连续两个交易日全天空转；既有周一预检只查单元健康与配置，不测查询负载，
# 缺口由此脚本补上——开盘前用「生产 runner + 一次性状态根」实测一轮，
# 以实测耗时裁决当天是否启动生产 timer。
#
# 安全性：状态根全部指向临时目录（读生产 API，零生产状态写入）；
# REAL_TRADING_ENABLED 强制 false；结束自动清理临时目录。
#
# 用法（在应用服务器上、交易时段内运行；生产 timer 须处于停止态以免双跑）：
#   tools/scale500_paper_preflight.sh
#   PREFLIGHT_MAX_SECONDS=150 tools/scale500_paper_preflight.sh
#
# 裁决：exit 0 = GO（可启动 timer）；exit 1 = NO-GO（保持停机）；exit 2 = 前置条件不满足。

set -u

PYTHON="${PREFLIGHT_PYTHON:-/opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1/bin/python3}"
REPO_ROOT="${PREFLIGHT_REPO_ROOT:-/opt/investment/releases/tradingagent/current}"
TOKEN_FILE="${PREFLIGHT_TOKEN_FILE:-/run/secrets/tradingagent/tradingdatas-read.token}"
ENV_FILE="${PREFLIGHT_ENV_FILE:-/etc/tradingagent/ashare-minute-scale500.env}"
MAX_SECONDS="${PREFLIGHT_MAX_SECONDS:-150}"

fail2() { echo "PREFLIGHT_FAIL $1"; exit 2; }
[ -x "$PYTHON" ] || fail2 "python_missing:$PYTHON"
[ -f "$TOKEN_FILE" ] || fail2 "token_missing"
[ -f "$ENV_FILE" ] || fail2 "env_file_missing:$ENV_FILE"
if [ "$(systemctl is-active tradingagent-ashare-minute-scale500-paper.timer 2>/dev/null)" = "active" ]; then
  fail2 "production_timer_active_stop_it_first"
fi

UNIVERSE_SOURCE=$(grep -h '^ASHARE_MINUTE_SCALE500_UNIVERSE_SOURCE=' "$ENV_FILE" | head -1 | cut -d= -f2-)
UNIVERSE_SHA=$(grep -h '^ASHARE_MINUTE_SCALE500_UNIVERSE_SHA256=' "$ENV_FILE" | head -1 | cut -d= -f2-)
[ -n "$UNIVERSE_SOURCE" ] && [ -n "$UNIVERSE_SHA" ] || fail2 "universe_env_unreadable"

STATE_ROOT=$(mktemp -d /tmp/scale500-preflight-state.XXXXXX)
ROLLBACK_ROOT=$(mktemp -d /tmp/scale500-preflight-rollback.XXXXXX)
trap 'rm -rf "$STATE_ROOT" "$ROLLBACK_ROOT"' EXIT

export REAL_TRADING_ENABLED=false PYTHONPATH="$REPO_ROOT"
COMMON=(--rolling-eligible
  --scale-state-root "$STATE_ROOT"
  --rollback30-state-root "$ROLLBACK_ROOT"
  --token-file "$TOKEN_FILE"
  --universe-source "$UNIVERSE_SOURCE"
  --expected-universe-sha256 "$UNIVERSE_SHA")

echo "preflight: session init (throwaway state root) $(date '+%F %T')"
"$PYTHON" -m Ashare.minute_scale500_runtime initialize "${COMMON[@]}"
INIT_RC=$?
[ $INIT_RC -eq 0 ] || { echo "PREFLIGHT_NO_GO init_failed rc=$INIT_RC"; exit 1; }

echo "preflight: timed single round $(date '+%F %T') max=${MAX_SECONDS}s"
START=$(date +%s)
OUT=$("$PYTHON" -m Ashare.minute_scale500_runtime run "${COMMON[@]}" 2>&1)
RC=$?
ELAPSED=$(( $(date +%s) - START ))

printf '%s\n' "$OUT"
echo "preflight: rc=$RC elapsed=${ELAPSED}s"

if printf '%s' "$OUT" | grep -q 'outside_delayed_session_window'; then
  fail2 "outside_session_window_run_inside_trading_hours"
fi
if [ $RC -ne 0 ]; then
  echo "PREFLIGHT_NO_GO runtime_failed rc=$RC elapsed=${ELAPSED}s"
  exit 1
fi
if [ "$ELAPSED" -le "$MAX_SECONDS" ]; then
  echo "PREFLIGHT_GO elapsed=${ELAPSED}s <= ${MAX_SECONDS}s"
  exit 0
fi
echo "PREFLIGHT_NO_GO elapsed=${ELAPSED}s > ${MAX_SECONDS}s"
exit 1
