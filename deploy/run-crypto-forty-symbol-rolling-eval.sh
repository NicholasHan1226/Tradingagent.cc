#!/usr/bin/env bash
# Existing daily research job: canonical store input, isolated output per attempt.
set -euo pipefail
umask 077
[[ $# -eq 0 ]] || exit 2
store=/var/lib/tradingagent/crypto-40-symbol-observation
output_root=/var/lib/tradingagent/crypto-40-symbol-rolling-eval
release=/opt/investment/releases/tradingagent/current
python=/opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1/bin/python3
test -d "$store"
test -d "$output_root"
test ! -L "$output_root"
test -x "$python"
cd "$release"
# Preserve failed attempts as diagnostics; never concatenate, repair or delete
# source segments, and never overwrite another invocation's research report.
attempt=$(mktemp -d "$output_root/entry-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
REAL_TRADING_ENABLED=false PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$release" \
  "$python" -B -m Crypto.forty_symbol_rolling_evaluation \
    --store-root "$store" \
    --out-json "$attempt/entry.json" \
    --report "$attempt/entry.md"
printf '%s rc=0 entry=%s authority=none tradeable_pit_verified=false\n' \
  "$(date -u +%FT%TZ)" "$attempt" >> "$output_root/run.log"
