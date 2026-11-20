#!/bin/bash
#
# content-hash.sh - content hash, package metadata and cache restore, shared by
# build-deb.sh (per-package build) and the deb plan job. Source it, do not run
# it. The hash formula must stay identical in both callers, which is why it
# lives here once.
# Consumed environment: PACKAGE, ARCH, SOURCES, UPSTREAM, BUILD, EXTRA_ENV,
# PAGES_URL, GITHUB_REPOSITORY (see build-deb.sh), and optionally
# CI_HELPER_SCRIPTS (files the build sources, default below; empty = none).

DEBS_DIR="${DEBS_DIR:-/debs}"
META_DIR="${DEBS_DIR}/.meta"

# Env keys matching this (case-insensitive) are exported but kept out of the
# content hash: hashing a token/key would leak it into the public hashes.json
# and churn the cache on every rotation. Build-affecting vars (versions, pinned
# commits) deliberately do NOT match, so they still feed the hash.
# Shared scripts every build sources (build-deb.sh runs `. deb_helpers.sh`, which
# sources kernel_helpers.sh; deb_src_overlay calls patch_guard.py). A change to
# them changes what a package contains, so they feed the hash like the sources:
# without them a fixed helper never rebuilds a deb built by the broken one.
if [[ -z "${CI_HELPER_SCRIPTS+x}" ]]; then
  _ch_scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)"
  CI_HELPER_SCRIPTS="${_ch_scripts}/deb_helpers.sh ${_ch_scripts}/kernel_helpers.sh ${_ch_scripts}/patch_guard.py"
  unset _ch_scripts
fi

SECRET_KEY_RE='(TOKEN|SECRET|PASSWORD|PAT|CRED|SIGNING_KEY|SIGNING_CERT)'

#######################################
# Merge the CI-injected extra env (not from the manifest) into EXTRA_ENV.
# These lines ARE hashed, so toggling MODULE_SIGNING_ACTIVE=yes invalidates the
# cache and forces a fresh build with the signing code active.
# Globals:
#   EXTRA_ENV, EXTRA_ENV_CI
#######################################
merge_extra_env() {
  if [[ -n "${EXTRA_ENV_CI:-}" ]]; then
    EXTRA_ENV="${EXTRA_ENV}${EXTRA_ENV:+$'\n'}${EXTRA_ENV_CI}"
  fi
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
# Print the published hashes.json (the only memory of what was built).
# A repository that has not published one yet (HTTP 404) is a FIRST build: the
# empty object is printed and every package initialises itself. Any other
# failure (host unavailable, 5xx after the retries) is an error: an unreadable
# memory must never be read as "nothing was built", that would rebuild every
# package, kernels included.
# Globals:
#   PAGES_URL, GITHUB_REPOSITORY
# Outputs:
#   The JSON document on stdout.
# Returns:
#   0 on success or 404, 1 when the document cannot be read.
#######################################
fetch_prior_hashes() {
  local url out code rc=0
  url="$(pages_base)/debs-cache/hashes.json"
  out=$(mktemp)
  code=$(curl -sSL --retry 5 --retry-delay 5 --retry-all-errors \
    -o "${out}" -w '%{http_code}' "${url}") || rc=$?
  if [[ "${rc}" -eq 37 || "${code}" == 404 ]]; then
    rm -f "${out}"
    echo '{}'
    return 0
  fi
  if [[ "${rc}" -ne 0 || ! ( "${code}" == 200 || "${code}" == 000 ) ]]; then
    rm -f "${out}"
    echo "::error::cannot read ${url} (curl rc=${rc}, http ${code})" >&2
    return 1
  fi
  jq -e 'type == "object"' "${out}" >/dev/null || {
    rm -f "${out}"
    echo "::error::${url} is not a JSON object" >&2
    return 1
  }
  cat "${out}"
  rm -f "${out}"
}

#######################################
# Print one field of a package entry of hashes.json. Package names are matched
# case-insensitively (Debian names are lower case, a manifest may not be).
# Arguments:
#   $1 - hashes.json content
#   $2 - cache key (<package>-<arch>)
#   $3 - jq path of the field, e.g. .hash or '.debs // []'
#######################################
prior_field() {
  printf '%s' "$1" | jq -cr --arg k "$2" \
    "(to_entries | map(select((.key | ascii_downcase) == (\$k | ascii_downcase))) | .[0].value // {}) | $3"
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
# Hash the contents of the shared build scripts, in the listed order.
# Globals:
#   CI_HELPER_SCRIPTS
# Outputs:
#   Hex digest on stdout, or "none" when the list is empty.
#######################################
helpers_hash() {
  [[ -n "${CI_HELPER_SCRIPTS// /}" ]] || {
    echo none
    return 0
  }
  # Word-splitting of the list is intentional.
  # shellcheck disable=SC2086
  cat ${CI_HELPER_SCRIPTS} | sha256sum | cut -d' ' -f1
}

#######################################
# Identify the upstream artifact for a repack (empty for custom pkgs). Uses the
# versioned .deb FILENAME from apt (e.g. pve-manager_9.0.0-1_amd64.deb), which
# changes with the upstream version but NOT with the mirror: hashing the full
# print-uris URL would vary across runs (mirror rotation) and defeat the cache.
# Globals:
#   UPSTREAM
# Outputs:
#   The upstream .deb filename, or "unresolved"/"none" on stdout.
#######################################
upstream_id() {
  [[ -n "${UPSTREAM:-}" ]] || {
    echo none
    return 0
  }
  # apt lists are purged in the :buildenv image; refresh so print-uris resolves.
  apt-get update >/dev/null 2>&1 || true
  local file
  file=$(apt-get download --print-uris "${UPSTREAM}" 2>/dev/null \
    | awk 'NR == 1 { print $2 }')
  [[ -n "${file}" ]] || {
    echo unresolved
    return 0
  }
  printf '%s' "${file}"
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
# The shared kernel data hash: GONE (owner 11/11 — the kernel package data
# lives in the product repos; a stale term would rebuild endlessly on the
# repo-side changes). Kept as "none" for the formula stability.
# Outputs:
#   "none"
#######################################
shared_data_hash() {
  echo none
}

#######################################
# Compute the combined content hash for this package.
# ARCH is included so amd64 and arm64 builds of the same custom package
# (upstream=none, same sources) get distinct hashes and distinct cache entries.
# Without this, both arches compute the same hash, their .meta files overwrite
# each other on merge-multiple, and the losing arch rebuilds every run.
# Outputs:
#   Hex digest on stdout.
#######################################
content_hash() {
  local src up env build arch helpers shared
  src=$(sources_hash)
  up=$(upstream_id)
  env=$(env_hash)
  build=$(printf '%s' "${BUILD:-}" | sha256sum | cut -d' ' -f1)
  arch="${ARCH:-amd64}"
  helpers=$(helpers_hash)
  shared=none
  echo "content-hash(${PACKAGE}/${arch}): src=${src} upstream=${up} env=${env} helpers=${helpers} shared=${shared}" >&2
  # Published formula stays byte-identical for the builds that consume no
  # shared data: the extra terms are dropped exactly like the old script
  # dropped "none" terms (5-term legacy shape, helpers term when present,
  # shared term appended only when consumed).
  {
    printf '%s\n' "${src}" "${up}" "${env}" "${build}" "${arch}"
    [[ "${helpers}" == none ]] || printf '%s\n' "${helpers}"
    [[ "${shared}" == none ]] || printf '%s\n' "${shared}"
  } | sha256sum | cut -d' ' -f1
}

#######################################
# Print the DEPS_HASH line value of EXTRA_ENV (digest of the dependency debs the
# build consumed), empty when the package has no dependency. It is published
# next to the content hash so the skip plan can recompute the hash of a package
# whose dependencies were themselves skipped, without downloading them.
# Globals:
#   EXTRA_ENV
#######################################
deps_hash_of_env() {
  printf '%s\n' "${EXTRA_ENV:-}" | sed -n 's/^DEPS_HASH=//p' | head -n1
}

#######################################
# Write /debs/.meta/<pkg>-<arch>.json describing this package's result.
# The file is arch-qualified so amd64 and arm64 artifacts don't overwrite
# each other when merge-multiple merges them in the publish job.
# Arguments:
#   $1 - content hash
#   $2 - skipped ("true"/"false")
#   $3 - JSON array of deb filenames
#######################################
write_meta() {
  mkdir -p "${META_DIR}"
  local arch="${ARCH:-amd64}"
  # The election record (the SBOM source the track/sibling gates wrote)
  # rides the package metadata: it is publish data, NEVER a content-hash
  # input (it changes only when the election resolves a new candidate).
  if [[ -n "${ELECTION_RECORD:-}" && -s "${ELECTION_RECORD}" ]]; then
    jq -n --arg h "$1" --argjson s "$2" --argjson d "$3" \
      --arg dh "$(deps_hash_of_env)" \
      --slurpfile e "${ELECTION_RECORD}" \
      '{hash: $h, skipped: $s, debs: $d, election: $e[0]}
        + (if $dh == "" then {} else {deps_hash: $dh} end)' \
      >"${META_DIR}/${PACKAGE}-${arch}.json"
  else
    jq -n --arg h "$1" --argjson s "$2" --argjson d "$3" \
      --arg dh "$(deps_hash_of_env)" \
      '{hash: $h, skipped: $s, debs: $d}
        + (if $dh == "" then {} else {deps_hash: $dh} end)' \
      >"${META_DIR}/${PACKAGE}-${arch}.json"
  fi
}

#######################################
# Restore the previously built debs for this package from Pages.
# Arguments:
#   $1 - base URL
#   $2 - JSON array of deb filenames to fetch
# Returns:
#   0 if every deb downloaded and unpacks, 1 otherwise.
#######################################
restore_cached() {
  local base="$1" files f
  files=$(printf '%s' "$2" | jq -r '.[]')
  [[ -n "${files}" ]] || return 1
  while IFS= read -r f; do
    [[ -n "${f}" ]] || continue
    curl -fsSL "${base}/debs-cache/${f}" -o "${DEBS_DIR}/${f}" || return 1
    # A published deb that does not unpack must never be reused: reusing it
    # would republish the same corrupt file forever. Treat it as a cache miss
    # so the package is rebuilt.
    if ! dpkg-deb --contents "${DEBS_DIR}/${f}" >/dev/null 2>&1; then
      echo "::warning::cached ${f} does not unpack; discarding it" >&2
      rm -f -- "${DEBS_DIR}/${f}"
      return 1
    fi
  done <<<"${files}"
  return 0
}

