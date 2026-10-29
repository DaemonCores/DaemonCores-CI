#!/bin/bash
#
# apply_overlays.sh - overlay a package tree onto an extracted deb tree.
#
# usage: apply_overlays.sh <overlay-dir> <extracted-tree>
#
# The overlay directory mirrors the deb tree exactly:
#   - new files are real files at their deb-tree path, copied as-is;
#   - files modified upstream are git-format .patch files, placed at the
#     MIRROR PATH of the file they patch (decision Gabriel 2026-10-30:
#     pve-manager/usr/share/pve-manager/js/pvemanagerlib.js.patch sits
#     beside the real file), applied with full deb-tree a/b paths:
#         for p in $(find <overlay-dir> -name '*.patch'); do
#             patch --fuzz=0 --forward -p1 -d <tree> < $p
#         done
#     The hunks ARE the patch anchors: drift is the patch tool refusing one.
#     `--fuzz=0` keeps that honest: a hunk whose context no longer matches
#     must fail to apply, not get nudged onto a neighbouring line.
#     `--forward` never prompts (an already-applied patch is skipped):
#     the build script must never hang on stdin.
#
# Drift handling: every patch is dry-run checked BEFORE the first real
# application, so the tree is never left half-patched. On any drift a
# ::error annotation and a GITHUB_STEP_SUMMARY report are emitted and the
# script exits 3; the caller (packages.yml build) then exits 0 without
# building, and build-deb.sh keeps the last-good +bootc artifact.
set -euo pipefail

OV="${1:?usage: apply_overlays.sh <overlay-dir> <extracted-tree>}"
T="${2:?usage: apply_overlays.sh <overlay-dir> <extracted-tree>}"

[[ -d "${OV}" ]] || { echo "apply_overlays: overlay dir missing: ${OV}" >&2; exit 1; }
[[ -d "${T}" ]] || { echo "apply_overlays: tree dir missing: ${T}" >&2; exit 1; }

#######################################
# Drift report: annotation, step summary, exit 3 (caller skips the build).
# Globals:
#   GITHUB_STEP_SUMMARY
# Arguments:
#   $1 - overlay dir (repository-relative, for the display)
#   $2 - patch file that did not apply
#######################################
skip_pkg() {
  local ov="$1" p="$2" rel
  rel="${p#"${OV}"/}"
  echo "::error title=${ov##*/} overlay drift::patch ${rel} does not apply"
  {
    echo "### ⛔ ${ov##*/} not rebuilt — overlay drift"
    echo
    echo "**\`${rel}\` no longer applies: an upstream refactor changed"
    echo "the patched file. The last-good \`+bootc\`/\`+dcve\` build is kept; the"
    echo "unpatched upstream is never shipped. Re-anchor:"
    echo "\`workflows/bootc-debs-builder/${ov##*/}/${rel}\`."
  } >>"${GITHUB_STEP_SUMMARY:-/dev/stderr}"
  exit 3
}

declare -a local_patches=()
# Mirror layout: walk the whole overlay for the .patch files, no mapping.
while IFS= read -r -d '' p; do
  local_patches+=("${p}")
done < <(find "${OV}" -name '*.patch' -print0 | sort -z)
local_patch_count=${#local_patches[@]}

# Dry-run the whole set on the pristine tree FIRST: on drift the tree is
# left exactly as upstream before anything is copied or applied.
for p in "${local_patches[@]}"; do
  patch --fuzz=0 --forward --dry-run -p1 -d "${T}" <"${p}" \
    || skip_pkg "${OV}" "${p}"
done
echo "apply_overlays: ${local_patch_count} patch(es) dry-run OK."

# The deb ships tree content only: copy the overlay, then drop the copies
# of .patch and patches.lock.json in the TREE ONLY - the overlay repo
# keeps its .patch files (Gabriel spec 2026-10-30).
if compgen -G "${OV}/*" >/dev/null; then
  cp -r "${OV}"/. "${T}/"
  find "${T}" -name '*.patch' -delete          # meta, never shipped
  find "${T}" -name patches.lock.json -delete  # meta, never shipped
fi
echo "apply_overlays: overlay copied."

for p in "${local_patches[@]}"; do
  patch --fuzz=0 --forward -p1 -d "${T}" <"${p}"
done

echo "apply_overlays: ${local_patch_count} patch(es) applied clean."