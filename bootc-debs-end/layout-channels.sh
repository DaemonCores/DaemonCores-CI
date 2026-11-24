#!/bin/bash
#
# layout-channels.sh - give each release channel its own APT repository on the
# one GitHub Pages site.
#
# A Pages deployment replaces the whole site, so a run that publishes one
# channel must re-publish the others untouched. The repository this run built
# moves to <site>/<channel>/; every other channel is copied back from the live
# site (signed metadata verbatim + the pool files it lists, each checked against
# its published SHA256). The deb cache and the SBOM stay at the site root.
#
# Environment:
#   SITE_DIR   directory holding the freshly built repository (required)
#   CHANNEL    channel this run publishes (required, one of CHANNELS)
#   CHANNELS   all channels (default: edge stable security)
#   DROP_CHANNELS  channels to unpublish: not carried over, so the next
#              deployment removes them from the site (default: none)
#   SUITE      APT suite (default: trixie)
#   PAGES_URL  base URL of the published site (default derived from
#              GITHUB_REPOSITORY)
set -euo pipefail

here="$(dirname "${BASH_SOURCE[0]}")"
# The channel of the run comes from its branch unless the caller forces one.
CHANNEL="${CHANNEL:-$(bash "${here}/../scripts/release-channel.sh")}"
CHANNELS="${CHANNELS:-edge stable security}"
SUITE="${SUITE:-trixie}"
DROP_CHANNELS="${DROP_CHANNELS:-}"
ROOT_KEEP=(debs-cache sbom.json CHANGELOG.md)

# shellcheck source=build-deb/content-hash.sh
. "${here}/../build-deb/content-hash.sh"

#######################################
# Move the freshly built repository under its channel directory.
# Globals:
#   SITE_DIR, CHANNEL, ROOT_KEEP
#######################################
place_own_channel() {
  local entry name keep
  mkdir -p "${SITE_DIR}/${CHANNEL}"
  for entry in "${SITE_DIR}"/* "${SITE_DIR}"/.[!.]*; do
    [[ -e "${entry}" ]] || continue
    name=$(basename "${entry}")
    [[ "${name}" != "${CHANNEL}" ]] || continue
    keep=false
    for k in "${ROOT_KEEP[@]}"; do
      [[ "${name}" != "${k}" ]] || keep=true
    done
    [[ "${keep}" == true ]] || mv "${entry}" "${SITE_DIR}/${CHANNEL}/"
  done
}

#######################################
# Download one file of a published channel; any failure is an error, a partial
# copy of a sibling channel would break its clients.
# Arguments:
#   $1 - channel, $2 - path inside the channel
#######################################
fetch() {
  local ch="$1" rel="$2"
  mkdir -p "$(dirname "${SITE_DIR}/${ch}/${rel}")"
  curl --retry 3 -fsSL "$(pages_base)/${ch}/${rel}" -o "${SITE_DIR}/${ch}/${rel}"
}

#######################################
# Copy one published channel back into the site.
# Arguments:
#   $1 - channel
# Returns:
#   0 also when the channel was never published (404)
#######################################
carry_channel() {
  local ch="$1" dist="dists/${SUITE}" rel f want got n=0
  if ! curl --retry 3 -fsSL "$(pages_base)/${ch}/${dist}/Release" \
    -o /dev/null 2>/dev/null; then
    echo "layout: channel ${ch} not published yet; nothing to keep"
    return 0
  fi
  for rel in Release Release.gpg InRelease; do
    fetch "${ch}" "${dist}/${rel}" || [[ "${rel}" != Release ]]
  done
  fetch "${ch}" gpg.key
  while read -r _ _ rel; do
    [[ "${rel}" == */Packages* || "${rel}" == */Release ]] || continue
    fetch "${ch}" "${dist}/${rel}"
  done < <(sed -n '/^SHA256:/,/^[^ ]/p' "${SITE_DIR}/${ch}/${dist}/Release" \
    | grep '^ ')
  while IFS= read -r p; do
    f="${p%% *}"
    want="${p##* }"
    fetch "${ch}" "${f}"
    got=$(sha256sum "${SITE_DIR}/${ch}/${f}" | cut -d' ' -f1)
    if [[ "${got}" != "${want}" ]]; then
      echo "::error::layout: ${ch}/${f} differs from its published checksum" >&2
      return 1
    fi
    n=$((n + 1))
  done < <(find "${SITE_DIR}/${ch}/${dist}" -name Packages -exec awk \
    '/^Filename:/ {f=$2} /^SHA256:/ {print f " " $2}' {} +)
  echo "layout: channel ${ch} kept (${n} file(s))"
}

main() {
  local ch found=false
  for ch in ${CHANNELS}; do
    [[ "${ch}" != "${CHANNEL}" ]] || found=true
  done
  if [[ "${found}" != true ]]; then
    echo "::error::layout: channel '${CHANNEL}' is not one of: ${CHANNELS}" >&2
    return 1
  fi
  place_own_channel
  for ch in ${CHANNELS}; do
    [[ "${ch}" != "${CHANNEL}" ]] || continue
    if [[ " ${DROP_CHANNELS} " == *" ${ch} "* ]]; then
      echo "layout: channel ${ch} dropped from the site"
      continue
    fi
    carry_channel "${ch}"
  done
}

main "$@"
