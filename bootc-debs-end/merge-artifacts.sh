#!/bin/bash
#
# merge-artifacts.sh - merge the per-package deb artifacts into one directory.
#
# An architecture-independent package (Architecture: all) is built once per
# architecture and every build uploads a file with the SAME name but different
# bytes (builds are not reproducible). Letting the artifact download merge them
# writes both into the same path concurrently and publishes a file made of
# chunks of both builds. Each artifact is downloaded into its own directory;
# this script keeps the first copy of a name (artifacts sorted by name, so the
# choice is deterministic), reports every dropped duplicate and refuses to
# publish a deb that does not unpack.
#
# Environment:
#   DEBS_IN   directory holding one sub-directory per artifact (default debs-in)
#   DEBS_DIR  merged output directory (default debs)
set -euo pipefail

DEBS_IN="${DEBS_IN:-debs-in}"
DEBS_DIR="${DEBS_DIR:-debs}"

#######################################
# Merge every artifact directory into DEBS_DIR, first copy of a path wins.
#######################################
merge() {
  local art file rel
  mkdir -p "${DEBS_DIR}"
  while IFS= read -r art; do
    while IFS= read -r -d '' file; do
      rel="${file#"${art}"/}"
      if [[ -e "${DEBS_DIR}/${rel}" ]]; then
        echo "duplicate ${rel}: kept the copy of an earlier artifact, dropped ${art##*/}"
        continue
      fi
      mkdir -p "${DEBS_DIR}/$(dirname "${rel}")"
      cp -- "${file}" "${DEBS_DIR}/${rel}"
    done < <(find "${art}" -type f -print0 | sort -z)
  done < <(find "${DEBS_IN}" -mindepth 1 -maxdepth 1 -type d | LC_ALL=C sort)
}

#######################################
# Fail when a merged deb does not unpack.
#######################################
verify() {
  local deb bad=0
  while IFS= read -r -d '' deb; do
    if ! dpkg-deb --contents "${deb}" >/dev/null 2>&1; then
      echo "ERROR: ${deb} does not unpack" >&2
      bad=1
    fi
  done < <(find "${DEBS_DIR}" -name '*.deb' -type f -print0)
  return "${bad}"
}

merge
verify
