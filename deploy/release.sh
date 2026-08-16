#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <archive.tar.gz> <40-char-git-sha>" >&2
  exit 64
}

[[ $# -eq 2 ]] || usage

archive="$1"
sha="$2"
release_root="${TRADINGAGENT_RELEASE_ROOT:-/opt/investment/releases/tradingagent}"
current_link="${release_root}/current"

[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "invalid git sha: $sha" >&2
  exit 65
}

[[ -f "$archive" ]] || {
  echo "archive not found: $archive" >&2
  exit 66
}

mkdir -p "$release_root"
release_dir="${release_root}/${sha}"
staging_dir=""
link_tmp=""

cleanup() {
  if [[ -n "$staging_dir" && -d "$staging_dir" ]]; then
    rm -rf -- "$staging_dir"
  fi
  if [[ -n "$link_tmp" && -L "$link_tmp" ]]; then
    rm -f -- "$link_tmp"
  fi
}
trap cleanup EXIT

if [[ ! -d "$release_dir" ]]; then
  staging_dir="$(mktemp -d "${release_root}/.staging-${sha}.XXXXXX")"
  tar -xzf "$archive" -C "$staging_dir"

  # Minimal release-shape checks. Application correctness is already enforced by CI.
  [[ -f "$staging_dir/AGENTS.md" ]] || {
    echo "release validation failed: AGENTS.md missing" >&2
    exit 67
  }
  [[ -f "$staging_dir/requirements.txt" ]] || {
    echo "release validation failed: requirements.txt missing" >&2
    exit 67
  }

  printf '%s\n' "$sha" > "$staging_dir/.deployed-sha"
  chmod -R a+rX,u+w "$staging_dir"
  mv -- "$staging_dir" "$release_dir"
  staging_dir=""
fi

# Switch the release atomically on the same filesystem. Existing releases remain
# available for rollback; this script intentionally performs no pruning.
link_tmp="${release_root}/.current-${sha}-$$"
ln -s -- "$release_dir" "$link_tmp"
mv -Tf -- "$link_tmp" "$current_link"
link_tmp=""

resolved_current="$(readlink -f -- "$current_link")"
resolved_release="$(readlink -f -- "$release_dir")"
[[ "$resolved_current" == "$resolved_release" ]] || {
  echo "release verification failed: current does not point to $release_dir" >&2
  exit 68
}

[[ "$(cat "$release_dir/.deployed-sha")" == "$sha" ]] || {
  echo "release verification failed: deployed sha mismatch" >&2
  exit 68
}

rm -f -- "$archive"
echo "deployed tradingagent sha=$sha release=$release_dir"
