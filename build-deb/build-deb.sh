#!/bin/bash
#
# build-deb.sh - content-addressed build wrapper for a single bootc .deb.
#
# Computes a content hash from (overlay sources + upstream version + the build
# script itself) and compares it to the hash recorded in the published APT repo
# (GitHub Pages, debs-cache/hashes.json). If unchanged, the previously built
# .deb is downloaded from Pages instead of being rebuilt; otherwise the build
# script runs. Either way a per-package metadata file is written under
# /debs/.meta so the publish step can rebuild hashes.json and decide whether any
# package actually changed.
#
# Consumed environment (set by action.yml from the composite inputs):
#   PACKAGE     - logical package name (metadata key, default deb glob).
#   UPSTREAM    - apt package name to resolve for repacks; empty for custom.
#   SOURCES     - space/newline-separated files/dirs whose contents are hashed.
#   DEB_GLOB    - glob (relative to /debs) matching this package's output debs.
#   BUILD       - shell script that produces the .deb(s) into /debs.
#   EXTRA_ENV   - KEY=VALUE lines exported for the build (e.g. a package
#                 version, GH_TOKEN). Non-secret keys also feed the hash so a
#                 version bump rebuilds; secret-looking keys are excluded.
#   PAGES_URL   - APT repo base URL; derived from GITHUB_REPOSITORY if empty.
#   GITHUB_REPOSITORY, GITHUB_WORKSPACE, GITHUB_OUTPUT - provided by Actions.
set -euo pipefail

DEBS_DIR=/debs
META_DIR="${DEBS_DIR}/.meta"

# Env keys matching this (case-insensitive) are exported but kept out of the
# content hash: hashing a token/key would leak it into the public hashes.json
# and churn the cache on every rotation. Build-affecting vars (versions, pinned
# commits) deliberately do NOT match, so they still feed the hash.
SECRET_KEY_RE='(TOKEN|SECRET|PASSWORD|PAT|CRED|SIGNING_KEY|SIGNING_CERT)'

#######################################
# Ensure jq and curl are available (the build container may lack jq).
#######################################
ensure_tools() {
  command -v jq >/dev/null 2>&1 && command -v curl >/dev/null 2>&1 && return 0
  apt-get update >/dev/null 2>&1 || true
  apt-get install -y --no-install-recommends jq curl >/dev/null 2>&1
}

#######################################
# Print the APT repo base URL (no trailing slash).
# Globals:
#   PAGES_URL, GITHUB_REPOSITORY
# Outputs:
#   Base URL on stdout.
#######################################
pages_base() {
  if [[ -n "${PAGES_URL:-}" ]]; then
    echo "${PAGES_URL%/}"
    return 0
  fi
  local owner="${GITHUB_REPOSITORY%%/*}" repo="${GITHUB_REPOSITORY#*/}"
  echo "https://${owner,,}.github.io/${repo}"
}

#######################################
# Hash the contents (not mtimes) of the overlay sources, order-independent.
# Globals:
#   SOURCES
# Outputs:
#   Hex digest on stdout, or "none" when no sources are given.
#######################################
sources_hash() {
  [[ -n "${SOURCES// /}" ]] || {
    echo none
    return 0
  }
  # Word-splitting of SOURCES is intentional: it may list several paths.
  # shellcheck disable=SC2086
  find ${SOURCES} -type f -print0 2>/dev/null \
    | sort -z | xargs -0 sha256sum 2>/dev/null \
    | sha256sum | cut -d' ' -f1
}

#######################################
# Identify the exact upstream artifact for a repack (empty for custom pkgs).
# The apt "print-uris" line carries the versioned URL and checksum, so hashing
# it captures any upstream change without parsing a version string.
# Globals:
#   UPSTREAM
# Outputs:
#   Hex digest or "none" on stdout.
#######################################
upstream_id() {
  [[ -n "${UPSTREAM:-}" ]] || {
    echo none
    return 0
  }
  local uris
  uris=$(apt-get download --print-uris "${UPSTREAM}" 2>/dev/null || true)
  [[ -n "${uris}" ]] || {
    echo unresolved
    return 0
  }
  printf '%s' "${uris}" | sha256sum | cut -d' ' -f1
}

#######################################
# Export every KEY=VALUE line of EXTRA_ENV so the build script can read them.
# Globals:
#   EXTRA_ENV
#######################################
export_env() {
  [[ -n "${EXTRA_ENV:-}" ]] || return 0
  local line key val
  while IFS= read -r line; do
    [[ "${line}" == *=* ]] || continue
    key=${line%%=*}
    val=${line#*=}
    [[ -n "${key// /}" ]] || continue
    printf -v "${key}" '%s' "${val}"
    export "${key?}"
  done <<<"${EXTRA_ENV}"
}

#######################################
# Hash the build-affecting env (secret-looking keys excluded), order-stable.
# Globals:
#   EXTRA_ENV, SECRET_KEY_RE
# Outputs:
#   Hex digest or "none" on stdout.
#######################################
env_hash() {
  [[ -n "${EXTRA_ENV:-}" ]] || {
    echo none
    return 0
  }
  local line key filtered
  filtered=$(
    while IFS= read -r line; do
      [[ "${line}" == *=* ]] || continue
      key=${line%%=*}
      printf '%s' "${key}" | grep -qiE "${SECRET_KEY_RE}" && continue
      printf '%s\n' "${line}"
    done <<<"${EXTRA_ENV}" | sort
  )
  [[ -n "${filtered}" ]] || {
    echo none
    return 0
  }
  printf '%s' "${filtered}" | sha256sum | cut -d' ' -f1
}

#######################################
# Compute the combined content hash for this package.
# Outputs:
#   Hex digest on stdout.
#######################################
content_hash() {
  local src up env build
  src=$(sources_hash)
  up=$(upstream_id)
  env=$(env_hash)
  build=$(printf '%s' "${BUILD:-}" | sha256sum | cut -d' ' -f1)
  echo "content-hash(${PACKAGE}): src=${src} upstream=${up} env=${env}" >&2
  printf '%s\n%s\n%s\n%s\n' "${src}" "${up}" "${env}" "${build}" \
    | sha256sum | cut -d' ' -f1
}

#######################################
# Write /debs/.meta/<pkg>.json describing this package's result.
# Arguments:
#   $1 - content hash
#   $2 - skipped ("true"/"false")
#   $3 - JSON array of deb filenames
#######################################
write_meta() {
  mkdir -p "${META_DIR}"
  jq -n --arg h "$1" --argjson s "$2" --argjson d "$3" \
    '{hash: $h, skipped: $s, debs: $d}' >"${META_DIR}/${PACKAGE}.json"
}

#######################################
# Emit a GitHub Actions step output.
# Arguments:
#   $1 - key
#   $2 - value
#######################################
set_output() {
  [[ -n "${GITHUB_OUTPUT:-}" ]] && echo "$1=$2" >>"${GITHUB_OUTPUT}"
}

#######################################
# Restore the previously built debs for this package from Pages.
# Arguments:
#   $1 - base URL
#   $2 - JSON array of deb filenames to fetch
# Returns:
#   0 if every deb downloaded, 1 otherwise.
#######################################
restore_cached() {
  local base="$1" files f
  files=$(printf '%s' "$2" | jq -r '.[]')
  [[ -n "${files}" ]] || return 1
  while IFS= read -r f; do
    [[ -n "${f}" ]] || continue
    curl -fsSL "${base}/debs-cache/${f}" -o "${DEBS_DIR}/${f}" || return 1
  done <<<"${files}"
  return 0
}

#######################################
# Run the build script and print the produced deb filenames as a JSON array.
# Outputs:
#   JSON array on stdout.
#######################################
run_build() {
  export_env
  (
    cd "${GITHUB_WORKSPACE:-.}" || exit 1
    bash --noprofile --norc -eo pipefail -c "${BUILD}"
  ) >&2
  local glob="${DEB_GLOB:-${PACKAGE}_*}"
  (
    cd "${DEBS_DIR}" || exit 0
    # Glob expansion of DEB_GLOB is intentional to list the produced debs.
    # shellcheck disable=SC2086
    ls ${glob}.deb 2>/dev/null || true
  ) | jq -R . | jq -s .
}

main() {
  ensure_tools
  local base hash prior prior_hash debs
  base=$(pages_base)
  hash=$(content_hash)
  set_output hash "${hash}"

  prior=$(curl -fsSL "${base}/debs-cache/hashes.json" 2>/dev/null || echo '{}')
  prior_hash=$(printf '%s' "${prior}" \
    | jq -r --arg p "${PACKAGE}" '.[$p].hash // empty')

  if [[ -n "${prior_hash}" && "${prior_hash}" == "${hash}" ]]; then
    local cached_debs
    cached_debs=$(printf '%s' "${prior}" \
      | jq -c --arg p "${PACKAGE}" '.[$p].debs // []')
    if restore_cached "${base}" "${cached_debs}"; then
      echo "::notice::${PACKAGE} unchanged; reused cached deb(s)"
      write_meta "${hash}" true "${cached_debs}"
      set_output skipped true
      return 0
    fi
    echo "::warning::${PACKAGE} cache download failed; rebuilding"
  fi

  debs=$(run_build)
  write_meta "${hash}" false "${debs}"
  set_output skipped false
  echo "::notice::${PACKAGE} built (${debs})"
}

main "$@"
