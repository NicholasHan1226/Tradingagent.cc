#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

fail() {
  printf 'bootstrap-production-server: %s\n' "$*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || fail 'must run as root'
[[ $# -eq 1 ]] || fail 'usage: bootstrap-production-server.sh <deploy-user>'

deploy_user="$1"
[[ "$deploy_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || fail 'invalid deploy user name'
id "$deploy_user" >/dev/null 2>&1 || fail "deploy user does not exist: $deploy_user"

shell="$(getent passwd "$deploy_user" | cut -d: -f7)"
case "$shell" in
  ''|*/nologin|*/false) fail "deploy user requires an SSH-capable shell for scp/ssh: $shell" ;;
esac

deploy_group="$(id -gn "$deploy_user")"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source_helper="$script_dir/release.sh"
installed_helper=/usr/local/sbin/tradingagent-release
spool=/var/tmp/tradingagent-deploy
release_root=/opt/investment/releases/tradingagent
sudoers_file=/etc/sudoers.d/tradingagent-release

[[ -f "$source_helper" && ! -L "$source_helper" ]] || fail "release helper missing: $source_helper"
[[ -d "$release_root" && ! -L "$release_root" ]] || fail "existing immutable release root is required: $release_root"
[[ "$(stat -c '%U:%G' -- "$release_root")" == 'root:root' ]] || fail 'release root must already be root:root'
[[ -L "$release_root/current" ]] || fail 'current must already be an immutable-release symlink before enabling automation'
current_target="$(readlink -f -- "$release_root/current")"
case "$current_target" in
  "$release_root"/*) ;;
  *) fail 'current symlink points outside the immutable release root' ;;
esac
[[ -d "$current_target" ]] || fail 'current immutable release target is missing'

install -d -o "$deploy_user" -g "$deploy_group" -m 0700 "$spool"
rm -f -- "$spool/request" "$spool/request.incoming"
find "$spool" -maxdepth 1 -type f -name '*.incoming' -delete

install -o root -g root -m 0755 "$source_helper" "$installed_helper"

sudoers_tmp="$(mktemp /etc/sudoers.d/.tradingagent-release.XXXXXX)"
cleanup() {
  rm -f -- "$sudoers_tmp"
}
trap cleanup EXIT
printf '%s ALL=(root) NOPASSWD: %s\n' "$deploy_user" "$installed_helper" > "$sudoers_tmp"
chmod 0440 "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
mv -f -- "$sudoers_tmp" "$sudoers_file"
chmod 0440 "$sudoers_file"
chown root:root "$sudoers_file"
visudo -cf "$sudoers_file" >/dev/null
trap - EXIT

[[ "$(stat -c '%U:%G %a' -- "$installed_helper")" == 'root:root 755' ]] \
  || fail 'installed helper ownership/mode verification failed'
[[ "$(stat -c '%U %a' -- "$spool")" == "$deploy_user 700" ]] \
  || fail 'deployment spool ownership/mode verification failed'

printf 'bootstrap complete\n'
printf 'deploy_user=%s\n' "$deploy_user"
printf 'helper=%s\n' "$installed_helper"
printf 'spool=%s\n' "$spool"
printf 'current=%s\n' "$current_target"
printf 'Keep the repository variable DEPLOY_ENABLED=false until GitHub Environment secrets are configured.\n'
