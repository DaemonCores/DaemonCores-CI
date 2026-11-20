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

# pages_base and fetch_prior_hashes (404 = first publication, any other failure
# = error) are shared with the build and the plan.
# shellcheck source=build-deb/content-hash.sh
. "$(dirname "${BASH_SOURCE[0]}")/../build-deb/content-hash.sh"

#######################################
# Restore every package of the published hashes.json that has no metadata in
# this run.
#######################################
main() {
  local base prior key hash debs deps_hash f ok carried=0 failed=0
  base=$(pages_base)
  # An unreadable published state must stop the publication: publishing the
  # partial artifact would drop every package this run skipped.
  prior=$(fetch_prior_hashes) || return 1
  if [[ "${prior}" == '{}' ]]; then
    echo "carry-over: no published hashes.json at ${base}; nothing to keep"
    return 0
  fi
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
      echo "::error::${key}: its published deb(s) cannot be restored" >&2
      failed=$((failed + 1))
      continue
    fi
    deps_hash=$(jq -r --arg k "${key}" '.[$k].deps_hash // ""' <<<"${prior}")
    jq -n --arg h "${hash}" --argjson d "${debs}" --arg dh "${deps_hash}" \
      '{hash: $h, skipped: true, debs: $d}
       + (if $dh == "" then {} else {deps_hash: $dh} end)' \
      >"${META_DIR}/${key}.json"
    carried=$((carried + 1))
  done < <(jq -r 'keys[]' <<<"${prior}")
  echo "carry-over: ${carried} package(s) kept from the published site"
  [[ "${failed}" -eq 0 ]] || return 1
}

main "$@"
