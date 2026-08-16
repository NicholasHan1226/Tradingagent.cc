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

link_tmp="$release_root/.current-${sha}-$$"
ln -s -- "$release_dir" "$link_tmp"
mv -Tf -- "$link_tmp" "$current_link"
link_tmp=''
switched=1

[[ "$(readlink -f -- "$current_link")" == "$(readlink -f -- "$release_dir")" ]] \
  || fail 'current does not resolve to the requested release'
[[ "$(cat "$release_dir/.deployed-sha")" == "$sha" ]] || fail 'deployed SHA verification failed'

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
