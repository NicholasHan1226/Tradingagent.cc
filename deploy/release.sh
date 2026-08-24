#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

release_root=/opt/investment/releases/tradingagent
current_link="$release_root/current"
spool=/var/tmp/tradingagent-deploy
request_file="$spool/request"
front_unit=tradingagent-front-api.service
health_url=http://127.0.0.1:8787/healthz
installed_path=/usr/local/sbin/tradingagent-release
g5_units=(
  tradingagent-crypto-round-trip-g5-acceptance.service
  tradingagent-crypto-round-trip-g5-delayed-paper.service
  tradingagent-crypto-round-trip-g5-health.service
  tradingagent-crypto-round-trip-g5-learning.service
  tradingagent-crypto-round-trip-g5-learning-scrub.service
)
ashare_release_units=(
  tradingagent-ashare-minute-paper.service
)
release_units=("${g5_units[@]}" "${ashare_release_units[@]}")
g5_legacy_dropins=(
  /etc/systemd/system/tradingagent-crypto-round-trip-g5-acceptance.service.d/20-g5-release.conf
  /etc/systemd/system/tradingagent-crypto-round-trip-g5-delayed-paper.service.d/20-g5-release.conf
  /etc/systemd/system/tradingagent-crypto-round-trip-g5-delayed-paper.service.d/release.conf
  /etc/systemd/system/tradingagent-crypto-round-trip-g5-health.service.d/20-g5-release.conf
  /etc/systemd/system/tradingagent-crypto-round-trip-g5-health.service.d/release.conf
  /etc/systemd/system/tradingagent-crypto-round-trip-g5-learning.service.d/20-immutable-release.conf
  /etc/systemd/system/tradingagent-crypto-round-trip-g5-learning-scrub.service.d/20-immutable-release.conf
)
ashare_legacy_dropins=(
  /etc/systemd/system/tradingagent-ashare-minute-paper.service.d/20-ashare-release.conf
)
release_legacy_dropins=("${g5_legacy_dropins[@]}" "${ashare_legacy_dropins[@]}")
g5_dropin_name=99-tradingagent-release.conf

fail() {
  printf 'tradingagent-release: %s\n' "$*" >&2
  exit 1
}

health_check() {
  local i
  for i in $(seq 1 50); do
    if curl -fsS --max-time 1 "$health_url" >/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

validate_immutable_release() {
  local root="$1"
  local bad_owner bad_write bad_dir bad_file

  [[ -d "$root" && ! -L "$root" ]] || fail "immutable release is not a directory: $root"
  [[ "$(stat -c '%U:%G' -- "$root")" == 'root:root' ]] || fail "immutable release root is not root:root: $root"

  bad_owner="$(find "$root" \( ! -user root -o ! -group root \) -print -quit)"
  [[ -z "$bad_owner" ]] || fail "non-root-owned release member: $bad_owner"

  bad_write="$(find "$root" -perm /0022 -print -quit)"
  [[ -z "$bad_write" ]] || fail "group/other-writable release member: $bad_write"

  bad_dir="$(find "$root" -type d ! -perm 0755 -print -quit)"
  [[ -z "$bad_dir" ]] || fail "release directory is not mode 0755: $bad_dir"

  bad_file="$(find "$root" -type f ! -perm 0444 ! -perm 0555 -print -quit)"
  [[ -z "$bad_file" ]] || fail "release file has unexpected mode: $bad_file"

  runuser -u tradingagent -- test -x "$root/front"
  runuser -u tradingagent -- test -r "$root/front/dist-server/server/tradingAgentSnapshotHttp.js"
  runuser -u tradingagent -- test -r "$root/tools/audit_ashare_worker_runtime.py"
  runuser -u tradingagent -- test -r "$root/Crypto/delayed_paper_round_trip_runtime.py"
  runuser -u tradingagent -- test -r "$root/Crypto/delayed_paper_round_trip_report.py"
  runuser -u tradingagent -- test -r "$root/Crypto/delayed_paper_round_trip_health.py"
  runuser -u tradingagent -- test -r "$root/Crypto/delayed_paper_round_trip_learning_worker.py"
}

validate_managed_g5_dropin() {
  local path="$1"

  [[ -f "$path" && ! -L "$path" ]] || fail "managed G5 drop-in is not a regular file: $path"
  [[ "$(stat -c '%U:%G' -- "$path")" == root:root ]] \
    || fail "managed G5 drop-in is not root:root: $path"
  [[ "$(stat -c '%h' -- "$path")" == 1 ]] || fail "managed G5 drop-in has multiple hard links: $path"
  local mode
  mode="$(stat -c '%a' -- "$path")"
  (( (8#$mode & 0022) == 0 )) || fail "managed G5 drop-in is group/other writable: $path"

  python3 - "$path" "$release_root" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
release_root = sys.argv[2]
section = None
release_binding = False
allowed = {
    "Unit": {"AssertPathExists"},
    "Service": {"WorkingDirectory", "Environment", "ReadOnlyPaths", "TimeoutStartSec"},
}
for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith(("#", ";")):
        continue
    if line.startswith("[") and line.endswith("]"):
        section = line[1:-1]
        if section not in allowed:
            raise SystemExit(f"unsupported section at {path}:{number}: {section}")
        continue
    if section is None or "=" not in line:
        raise SystemExit(f"invalid directive at {path}:{number}")
    key, value = line.split("=", 1)
    if key not in allowed[section]:
        raise SystemExit(f"unsupported directive at {path}:{number}: {key}")
    if key == "Environment" and value and not value.startswith("PYTHONPATH="):
        raise SystemExit(f"unsupported environment at {path}:{number}")
    if key == "TimeoutStartSec" and not re.fullmatch(
        r"(?:[0-9]+(?:us|ms|s|min|h|d|w|month|y)?|infinity)", value
    ):
        raise SystemExit(f"invalid timeout at {path}:{number}")
    if release_root in value:
        release_binding = True
if not release_binding:
    raise SystemExit(f"managed G5 drop-in has no release binding: {path}")
PY
}

require_g5_unit_stopped() {
  local unit="$1" phase="$2" state main_pid control_pid

  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  if [[ "$state" == inactive ]]; then
    return 0
  fi
  if [[ "$state" == failed ]]; then
    main_pid="$(systemctl show -p MainPID --value "$unit")"
    control_pid="$(systemctl show -p ControlPID --value "$unit")"
    [[ "$main_pid" =~ ^[0-9]+$ && "$control_pid" =~ ^[0-9]+$ ]] \
      || fail "G5 failed unit returned invalid process identifiers: $unit main_pid=$main_pid control_pid=$control_pid"
    [[ "$main_pid" == 0 && "$control_pid" == 0 ]] \
      || fail "G5 failed unit still has a process during $phase: $unit main_pid=$main_pid control_pid=$control_pid"
    return 0
  fi
  fail "G5 unit must be stopped during $phase: $unit state=$state"
}

prepare_g5_release_reconciliation() {
  local unit timeout path rel canonical

  g5_dropin_backup_dir="$(mktemp -d /var/tmp/tradingagent-g5-dropins.XXXXXX)"
  g5_dropin_manifest="$g5_dropin_backup_dir/manifest"
  : > "$g5_dropin_manifest"
  g5_timeouts=()

  for unit in "${release_units[@]}"; do
    systemctl cat "$unit" >/dev/null
    require_g5_unit_stopped "$unit" "release preflight"
    timeout="$(systemctl show -p TimeoutStartUSec --value "$unit")"
    [[ "$timeout" =~ ^([0-9]+(us|ms|s|min|h|d|w|month|y)([[:space:]]+[0-9]+(us|ms|s|min|h|d|w|month|y))*)$|^infinity$ ]] \
      || fail "G5 unit has an unsupported start timeout: $unit timeout=$timeout"
    g5_timeouts+=("$timeout")
  done

  for path in "${release_legacy_dropins[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
      validate_managed_g5_dropin "$path"
      rel="${path#/}"
      install -D -o root -g root -m 0644 "$path" "$g5_dropin_backup_dir/$rel"
      printf '%s\n' "$path" >> "$g5_dropin_manifest"
    fi
  done
  for unit in "${release_units[@]}"; do
    canonical="/etc/systemd/system/$unit.d/$g5_dropin_name"
    if [[ -e "$canonical" || -L "$canonical" ]]; then
      validate_managed_g5_dropin "$canonical"
      rel="${canonical#/}"
      install -D -o root -g root -m 0644 "$canonical" "$g5_dropin_backup_dir/$rel"
      printf '%s\n' "$canonical" >> "$g5_dropin_manifest"
    fi
  done
}

restore_g5_release_dropins() {
  local unit path rel

  [[ -n "${g5_dropin_backup_dir:-}" && -d "$g5_dropin_backup_dir" ]] || return 0
  for unit in "${release_units[@]}"; do
    rm -f -- "/etc/systemd/system/$unit.d/$g5_dropin_name"
  done
  for path in "${release_legacy_dropins[@]}"; do
    rm -f -- "$path"
  done
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    rel="${path#/}"
    install -D -o root -g root -m 0644 "$g5_dropin_backup_dir/$rel" "$path"
  done < "$g5_dropin_manifest"
  systemctl daemon-reload
}

reconcile_g5_release_dropins() {
  local release_dir="$1"
  local index unit unit_dir unit_dir_mode canonical tmp timeout working environment dropins legacy

  g5_dropins_changed=1
  for index in "${!release_units[@]}"; do
    unit="${release_units[$index]}"
    timeout="${g5_timeouts[$index]}"
    unit_dir="/etc/systemd/system/$unit.d"
    [[ -d "$unit_dir" && ! -L "$unit_dir" ]] || fail "G5 drop-in directory is missing or unsafe: $unit_dir"
    [[ "$(stat -c '%U:%G' -- "$unit_dir")" == root:root ]] \
      || fail "G5 drop-in directory is not root:root: $unit_dir"
    unit_dir_mode="$(stat -c '%a' -- "$unit_dir")"
    (( (8#$unit_dir_mode & 0022) == 0 )) \
      || fail "G5 drop-in directory is group/other writable: $unit_dir"
    tmp="$(mktemp "$unit_dir/.${g5_dropin_name}.XXXXXX")"
    printf '[Service]\nWorkingDirectory=%s\nEnvironment=PYTHONPATH=%s\nReadOnlyPaths=%s\nTimeoutStartSec=%s\n' \
      "$release_dir" "$release_dir" "$release_dir" "$timeout" > "$tmp"
    chown root:root "$tmp"
    chmod 0644 "$tmp"
    canonical="$unit_dir/$g5_dropin_name"
    mv -f -- "$tmp" "$canonical"
  done
  for legacy in "${release_legacy_dropins[@]}"; do
    rm -f -- "$legacy"
  done
  systemctl daemon-reload

  for unit in "${release_units[@]}"; do
    require_g5_unit_stopped "$unit" "release cutover"
    working="$(systemctl show -p WorkingDirectory --value "$unit")"
    [[ "$working" == "$release_dir" ]] || fail "G5 unit WorkingDirectory did not reconcile: $unit"
    environment="$(systemctl show -p Environment --value "$unit")"
    case " $environment " in
      *" PYTHONPATH=$release_dir "*) ;;
      *) fail "G5 unit PYTHONPATH did not reconcile: $unit" ;;
    esac
    dropins="$(systemctl show -p DropInPaths --value "$unit")"
    canonical="/etc/systemd/system/$unit.d/$g5_dropin_name"
    case " $dropins " in
      *" $canonical "*) ;;
      *) fail "G5 canonical release drop-in is not effective: $unit" ;;
    esac
    for legacy in "${release_legacy_dropins[@]}"; do
      case " $dropins " in
        *" $legacy "*) fail "legacy G5 release drop-in remains effective: $legacy" ;;
      esac
    done
  done
}

[[ "$EUID" -eq 0 ]] || fail 'must run as root through the scoped sudo gate'
[[ $# -eq 0 ]] || fail 'arguments are not accepted; deployment request is read from the fixed spool'
[[ "$(readlink -f -- "$0")" == "$installed_path" ]] || fail "must run from $installed_path"
[[ "$(stat -c '%U:%G' -- "$installed_path")" == 'root:root' ]] || fail 'installed helper must be root:root'
helper_mode="$(stat -c '%a' -- "$installed_path")"
(( (8#$helper_mode & 0022) == 0 )) || fail 'installed helper must not be group/other writable'

sudo_user="${SUDO_USER:-}"
[[ -n "$sudo_user" && "$sudo_user" != root ]] || fail 'missing non-root SUDO_USER'

[[ -d "$release_root" && ! -L "$release_root" ]] || fail "release root missing or unsafe: $release_root"
[[ "$(stat -c '%U:%G' -- "$release_root")" == 'root:root' ]] || fail 'release root must be root:root'
[[ -d "$spool" && ! -L "$spool" ]] || fail "deployment spool missing or unsafe: $spool"
[[ "$(stat -c '%U' -- "$spool")" == "$sudo_user" ]] || fail 'deployment spool owner must match SUDO_USER'
[[ "$(stat -c '%a' -- "$spool")" == '700' ]] || fail 'deployment spool mode must be 0700'

[[ -f "$request_file" && ! -L "$request_file" ]] || fail 'deployment request is missing or not a regular file'
[[ "$(stat -c '%U' -- "$request_file")" == "$sudo_user" ]] || fail 'request owner must match SUDO_USER'
[[ "$(stat -c '%h' -- "$request_file")" == '1' ]] || fail 'request must have one hard link'
request_mode="$(stat -c '%a' -- "$request_file")"
(( (8#$request_mode & 0022) == 0 )) || fail 'request must not be group/other writable'
[[ "$(wc -l < "$request_file")" -eq 1 ]] || fail 'request must contain exactly one line'

read -r sha expected_checksum extra < "$request_file"
[[ -z "${extra:-}" ]] || fail 'request contains unexpected fields'
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail 'request contains invalid git SHA'
[[ "$expected_checksum" =~ ^[0-9a-f]{64}$ ]] || fail 'request contains invalid package checksum'

archive="$spool/tradingagent-${sha}.tar.gz"
[[ -f "$archive" && ! -L "$archive" ]] || fail 'release archive is missing or not a regular file'
[[ "$(stat -c '%U' -- "$archive")" == "$sudo_user" ]] || fail 'archive owner must match SUDO_USER'
[[ "$(stat -c '%h' -- "$archive")" == '1' ]] || fail 'archive must have one hard link'
archive_mode="$(stat -c '%a' -- "$archive")"
(( (8#$archive_mode & 0022) == 0 )) || fail 'archive must not be group/other writable'

root_archive="$(mktemp "/var/tmp/tradingagent-${sha}.root.XXXXXX.tar.gz")"
staging_dir=''
link_tmp=''
previous_release=''
front_state_before=''
front_enabled_before=''
g5_dropin_backup_dir=''
g5_dropin_manifest=''
g5_dropins_changed=0
g5_timeouts=()
switched=0
committed=0

rollback_release() {
  set +e
  if [[ "$switched" -eq 1 && -n "$previous_release" && -d "$previous_release" ]]; then
    printf 'deployment failed after cutover; rolling current back to %s\n' "$previous_release" >&2
    local rollback_link="$release_root/.rollback-current-$$"
    rm -f -- "$rollback_link"
    ln -s -- "$previous_release" "$rollback_link"
    mv -Tf -- "$rollback_link" "$current_link"

    if [[ "$(readlink -f -- "$current_link" 2>/dev/null)" != "$previous_release" ]]; then
      printf 'SEVERE: current symlink did not restore to previous immutable release\n' >&2
    fi

    if [[ "$g5_dropins_changed" -eq 1 ]]; then
      restore_g5_release_dropins
    fi

    if [[ "$front_state_before" == active ]]; then
      systemctl restart "$front_unit"
      if ! health_check; then
        printf 'SEVERE: previous front API did not recover health after rollback\n' >&2
      fi
    fi
  fi
}

cleanup() {
  local rc=$?
  set +e
  if [[ "$rc" -ne 0 && "$committed" -eq 0 ]]; then
    rollback_release
  fi
  [[ -n "$link_tmp" ]] && rm -f -- "$link_tmp"
  [[ -n "$staging_dir" && -d "$staging_dir" ]] && rm -rf -- "$staging_dir"
  [[ -n "$g5_dropin_backup_dir" && -d "$g5_dropin_backup_dir" ]] && rm -rf -- "$g5_dropin_backup_dir"
  rm -f -- "$root_archive"
  exit "$rc"
}
trap cleanup EXIT

cp -- "$archive" "$root_archive"
chown root:root "$root_archive"
chmod 0600 "$root_archive"
actual_checksum="$(sha256sum "$root_archive" | awk '{print $1}')"
[[ "$actual_checksum" == "$expected_checksum" ]] || fail 'release archive checksum mismatch'

python3 - "$root_archive" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile

archive = sys.argv[1]
required = {
    '.source-sha',
    'AGENTS.md',
    'requirements.txt',
    'front/dist-server/server/tradingAgentSnapshotHttp.js',
}
seen = set()
with tarfile.open(archive, 'r:gz') as tf:
    for member in tf.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts:
            raise SystemExit(f'unsafe archive path: {member.name!r}')
        parts = [part for part in path.parts if part not in ('', '.')]
        normalized = '/'.join(parts)
        if normalized:
            if normalized in seen:
                raise SystemExit(f'duplicate archive path: {normalized!r}')
            seen.add(normalized)
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f'unsupported archive member type: {member.name!r}')
missing = sorted(required - seen)
if missing:
    raise SystemExit(f'missing required release members: {missing!r}')
PY

[[ -L "$current_link" ]] || fail 'current must already be an immutable-release symlink before automated deployment'
previous_release="$(readlink -f -- "$current_link")"
[[ -d "$previous_release" ]] || fail 'current symlink target is missing'
case "$previous_release" in
  "$release_root"/*) ;;
  *) fail 'current symlink points outside the release root' ;;
esac

systemctl cat "$front_unit" >/dev/null
front_state_before="$(systemctl is-active "$front_unit" 2>/dev/null || true)"
front_enabled_before="$(systemctl is-enabled "$front_unit" 2>/dev/null || true)"
if [[ "$front_state_before" == active ]]; then
  curl -fsS --max-time 2 "$health_url" >/dev/null \
    || fail 'front API is active but unhealthy before deployment'
elif [[ "$front_state_before" == inactive && "$front_enabled_before" == disabled ]]; then
  :
else
  fail "unexpected front API state before deployment: active=$front_state_before enabled=$front_enabled_before"
fi

release_dir="$release_root/$sha"
if [[ -e "$release_dir" ]]; then
  [[ -d "$release_dir" && ! -L "$release_dir" ]] || fail 'existing release path is not a directory'
  [[ -f "$release_dir/.deployed-sha" ]] || fail 'existing release lacks deployed SHA metadata'
  [[ -f "$release_dir/.release-package-sha256" ]] || fail 'existing release lacks package checksum metadata'
  [[ "$(cat "$release_dir/.deployed-sha")" == "$sha" ]] || fail 'existing release SHA metadata mismatch'
  [[ "$(cat "$release_dir/.release-package-sha256")" == "$expected_checksum" ]] || fail 'existing immutable release has a different package checksum'
else
  staging_dir="$(mktemp -d "$release_root/.staging-${sha}.XXXXXX")"
  tar --no-same-owner --no-same-permissions -xzf "$root_archive" -C "$staging_dir"

  source_sha="$(tr -d '\r\n' < "$staging_dir/.source-sha")"
  [[ "$source_sha" == "$sha" ]] || fail 'release source SHA does not match deployment request'
  [[ -f "$staging_dir/front/dist-server/server/tradingAgentSnapshotHttp.js" ]] \
    || fail 'front API build artifact is missing'

  printf '%s\n' "$sha" > "$staging_dir/.deployed-sha"
  printf '%s\n' "$expected_checksum" > "$staging_dir/.release-package-sha256"

  chown -R root:root "$staging_dir"
  find "$staging_dir" -type d -exec chmod 0755 {} +
  find "$staging_dir" -type f -perm /0111 -exec chmod 0555 {} +
  find "$staging_dir" -type f ! -perm /0111 -exec chmod 0444 {} +

  validate_immutable_release "$staging_dir"
  mv -- "$staging_dir" "$release_dir"
  staging_dir=''
fi

validate_immutable_release "$release_dir"
prepare_g5_release_reconciliation

link_tmp="$release_root/.current-${sha}-$$"
ln -s -- "$release_dir" "$link_tmp"
mv -Tf -- "$link_tmp" "$current_link"
link_tmp=''
switched=1

[[ "$(readlink -f -- "$current_link")" == "$(readlink -f -- "$release_dir")" ]] \
  || fail 'current does not resolve to the requested release'
[[ "$(cat "$release_dir/.deployed-sha")" == "$sha" ]] || fail 'deployed SHA verification failed'

reconcile_g5_release_dropins "$release_dir"

if [[ "$front_state_before" == active ]]; then
  systemctl restart "$front_unit"
  health_check || fail 'front API failed bounded health check after restart'

  front_pid="$(systemctl show -p MainPID --value "$front_unit")"
  [[ "$front_pid" =~ ^[1-9][0-9]*$ ]] || fail 'front API has no valid MainPID after restart'
  front_cwd="$(readlink -f -- "/proc/$front_pid/cwd")"
  [[ "$front_cwd" == "$(readlink -f -- "$release_dir/front")" ]] \
    || fail 'front API process is not running from the requested immutable release'
else
  [[ "$(systemctl is-active "$front_unit" 2>/dev/null || true)" == inactive ]] \
    || fail 'disabled front API unexpectedly changed state during deployment'
fi

committed=1
rm -f -- "$archive" "$request_file"
printf 'deployed tradingagent sha=%s release=%s front_state=%s\n' \
  "$sha" "$release_dir" "$front_state_before"
