#!/bin/bash
#
# carry-over.sh - keep the published debs of the packages this run did not
# refresh.
#
# The APT repo and debs-cache/hashes.json are rebuilt from the artifact of the
# current run alone. When the build job ended early (failure, cancellation) the
# artifact is partial, and publishing it would drop every other package from
# the live repo and force a full rebuild on the next run. This script pulls the
# missing packages back from the published site and records them as reused.
#
# Environment:
#   DEBS_DIR           merged artifact directory (default debs)
#   PAGES_URL          base URL of the published site (default derived from
#                      GITHUB_REPOSITORY)
set -euo pipefail

DEBS_DIR="${DEBS_DIR:-debs}"
META_DIR="${DEBS_DIR}/.meta"

#######################################
# Print the base URL of the published site.
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
# Restore every package of the published hashes.json that has no metadata in
# this run.
#######################################
main() {
  local base prior key hash debs f ok carried=0
  base=$(pages_base)
  prior=$(curl -fsSL "${base}/debs-cache/hashes.json" 2>/dev/null) || {
    echo "carry-over: no published hashes.json at ${base}; nothing to keep"
    return 0
  }
  mkdir -p "${META_DIR}"
  while IFS= read -r key; do
    [[ ! -e "${META_DIR}/${key}.json" ]] || continue
    hash=$(jq -r --arg k "${key}" '.[$k].hash' <<<"${prior}")
    debs=$(jq -c --arg k "${key}" '.[$k].debs // []' <<<"${prior}")
    ok=true
    while IFS= read -r f; do
      [[ -n "${f}" ]] || continue
      [[ -e "${DEBS_DIR}/${f}" ]] \
        || curl -fsSL "${base}/debs-cache/${f}" -o "${DEBS_DIR}/${f}" || ok=false
    done < <(jq -r '.[]' <<<"${debs}")
    if [[ "${ok}" != true ]]; then
      echo "::warning::${key}: could not restore its published deb(s); dropped"
      continue
    fi
    jq -n --arg h "${hash}" --argjson d "${debs}" \
      '{hash: $h, skipped: true, debs: $d}' >"${META_DIR}/${key}.json"
    carried=$((carried + 1))
  done < <(jq -r 'keys[]' <<<"${prior}")
  echo "carry-over: ${carried} package(s) kept from the published site"
}

main "$@"
