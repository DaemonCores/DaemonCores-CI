#!/bin/bash
#
# fetch.sh - put the published debs of the dependencies that were NOT rebuilt in
# this run next to the artifacts of the ones that were. A package whose hash is
# unchanged gets no build job (deb-plan), so its debs exist only on Pages; a
# dependent that is rebuilt still needs them to link against.
#
# A dependency rebuilt in this run already has its debs in DEST (artifact
# download): fetching the older published ones next to them would put two
# versions in front of apt. Only the dependencies without any deb in DEST are
# fetched.
#
# Consumed environment: MAN (expanded manifest), DEPS (direct dependencies),
# ARCH, DEST (directory the debs land in), PAGES_URL / GITHUB_REPOSITORY.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=build-deb/content-hash.sh
. "${HERE}/../build-deb/content-hash.sh"

#######################################
# Succeed when a deb of the package is already in DEST.
# Arguments:
#   $1 - package name
#######################################
has_artifact() {
  local glob pat
  glob=$(yq ".packages[] | select(.name == \"$1\") | .\"deb-glob\" // \"$1_*\"" "${MAN}")
  read -ra pats <<<"${glob}"
  for pat in "${pats[@]}"; do
    # The pattern is a glob on purpose.
    compgen -G "${DEST}/${pat}.deb" >/dev/null && return 0
  done
  return 1
}

main() {
  : "${MAN:?}" "${ARCH:?}" "${DEST:?}"
  local prior base dep file sub seen=" " queue
  mkdir -p "${DEST}"
  read -ra queue <<<"${DEPS:-}"
  [[ "${#queue[@]}" -gt 0 ]] || return 0
  prior=$(fetch_prior_hashes)
  base=$(pages_base)
  while [[ "${#queue[@]}" -gt 0 ]]; do
    dep="${queue[0]}"
    queue=("${queue[@]:1}")
    [[ "${seen}" != *" ${dep} "* ]] || continue
    seen+="${dep} "
    while IFS= read -r sub; do
      [[ -n "${sub}" ]] && queue+=("${sub}")
    done < <(yq ".packages[] | select(.name == \"${dep}\") | (.deps // []) | .[]" "${MAN}")
    if has_artifact "${dep}"; then
      continue
    fi
    while IFS= read -r file; do
      [[ -n "${file}" ]] || continue
      curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        "${base}/debs-cache/${file}" -o "${DEST}/${file}" || {
        echo "::error::${dep}: cannot fetch the published ${file}" >&2
        return 1
      }
      echo "fetched published ${file} (${dep} was not rebuilt)" >&2
    done < <(prior_field "${prior}" "${dep}-${ARCH}" '.debs // [] | .[]')
  done
}

main "$@"
