#!/bin/bash
#
# deb_helpers.sh - shared deb-rebuild helpers for the bootc-debs builders
# (DaemonCores-VE, debian-bootc). Exported to the build via CI_SCRIPTS.
#
# The overlay model (Gabriel spec 2026-10-30): package dirs mirror the deb
# tree — new files are real files at their deb path; modified upstream files
# are git .patch at the MIRROR PATH of the file they patch; all patches are
# dry-run first (all-or-nothing); .patch is never shipped.
#
# Functions:
#   deb_overlay_apply <overlay-dir> <extracted-tree>          -> exit 3 on drift
#   deb_repack <pkg> <ver-suffix> <overlay-dir>               -> full repack
#   deb_overlay_tree <pkg> <ver-suffix> <tree-dir> [exec...]  -> tree-copy repack
#   deb_custom <pkg> <ver> <tree-dir>                         -> custom whole...
#   deb_source_build <pkg> <commit> <src-ver> <src-lane> [overlay...]    -> upstream source build, patch lanes applied on the source treeee
#
# Kernel build primitives live in scripts/kernel_helpers.sh (both kernel
# modes: VE kernel-pve and debian-bootc linux-image), sourced here so the
# build shells get them implicitly like the deb_* functions.
set -euo pipefail

KERNEL_HELPERS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/kernel_helpers.sh"
. "${KERNEL_HELPERS}"

deb_overlay_apply() {
  local ovl="${1:?}" tree="${2:?}"
  local tdir cand p tmp
  tdir=$(mktemp -d "/tmp/overlays.XXXXXX")
  cp -r "${ovl}" "${tdir}/overlay"
  # Debian multiarch: the overlay mirror paths name x86_64-linux-gnu; the
  # arm64 deb tree names aarch64-linux-gnu. Translate the triplet IN THE COPY
  # (the repo keeps the amd64 mirror; these .patch copies never ship).
  if [ "${BUILD_ARCH:-amd64}" = "arm64" ]; then
    find "${tdir}/overlay" -name '*.patch' -type f -exec sed -i \
      's/x86_64-linux-gnu/aarch64-linux-gnu/g' {} +
  fi
  # Mirror everything EXCEPT the patch/lock metadata onto the tree copy so a
  # dry-run sees the exact files the apply will create or replace.
  (cd "${tdir}/overlay" && find . -name '*.patch' -print) >"${tdir}/patchlist"
  find "${tdir}/overlay" -name '*.patch' -prune -o \
       -name 'patches.lock.json' -prune -o -type f -print |
    while read -r cand; do
      mkdir -p "${tdir}/tree/$(dirname "${cand}")"
      cp -a "${tdir}/overlay/${cand#./}" "${tdir}/tree/${cand#./}"
    done
  # Copy the pristine tree for the dry run of the patches it touches.
  rm -rf "${tdir}/tree-dry"
  cp -a "${tree?}" "${tdir}/tree-dry"
  while read -r p; do
    p="${p#./}"
    [ -n "${p}" ] || continue
    # GNU patch otherwise leaves a 0-byte file in the tree when the patch
    # deletes its content (--remove-empty-files makes the deletion real).
    patch --quiet --dry-run --forward --remove-empty-files -d "${tdir}/tree-dry" -p1 \
      <"${tdir}/overlay/${p}" \
      || { echo "ERROR: ${p} fails dry-run on ${tree} — overlay drift." >&2; rm -rf "${tdir}"; return 3; }
  done <"${tdir}/patchlist"
  # Real application on the real tree.
  cp -a "${tdir}/tree/." "${tree}/"
  while read -r p; do
    p="${p#./}"
    [ -n "${p}" ] || continue
    patch --quiet --forward --remove-empty-files -d "${tree}" -p1 \
      <"${tdir}/overlay/${p}" \
      || { echo "ERROR: ${p} failed to apply (unexpected: dry-run passed)." >&2; rm -rf "${tdir}"; return 3; }
  done <"${tdir}/patchlist"
  rm -rf "${tdir}"
  return 0
}

deb_repack() {
  local pkg="${1:?}" suffix="${2:?}" ovl="${3:?}"
  local O SAVE_PWD
  SAVE_PWD="${SAVE_PWD:-$PWD}"
  O="/output/${pkg}"
  apt-get update
  cd /tmp
  apt-get download "${pkg}"
  mkdir -p "$O"
  # The glob part must stay OUTSIDE the quotes: bash never expands a glob
  # inside "...", and dpkg-deb would receive the literal asterisk.
  local UP_DEB
  set -- "/tmp/${pkg}"_*.deb
  [ -f "${1:-}" ] \
    || { echo "ERROR: no upstream .deb for ${pkg} left in /tmp after download" >&2; exit 1; }
  UP_DEB="$1"
  dpkg-deb -R "$UP_DEB" "$O"

  local ORIG_VER ARCH
  ORIG_VER=$(dpkg-deb -f "$UP_DEB" Version)
  ARCH=$(dpkg-deb -f "$UP_DEB" Architecture)

  # drift (exit 3) = skip the build: last-good deb retained by the caller
  set +e
  deb_overlay_apply "${SAVE_PWD}/workflows/bootc-debs-builder/${ovl}" "$O"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "${pkg} skipped (overlay drift) — last-good version retained."
    exit 0
  fi

  find "$O" -not -path '*/DEBIAN/*' -type f \
    | xargs md5sum \
    | sed "s|$O/||" \
    > "$O/DEBIAN/md5sums"
  sed -i "s/^Version: .*/Version: 1:${ORIG_VER}${suffix}/" "$O/DEBIAN/control"
  dpkg-deb --build "$O" \
    "/debs/${pkg}_${ORIG_VER}${suffix}_${ARCH}.deb"
}

deb_overlay_tree() {
  local pkg="${1:?}" suffix="${2:?}" tree="${3:?}"
  shift 3
  local O SAVE_PWD f ORIG_VER ARCH
  # Snapshot the exec list: the glob resolution below does `set --`.
  local EXEC_LIST=("$@")
  SAVE_PWD="${SAVE_PWD:-$PWD}"
  O="/output/${pkg}"
  apt-get update
  cd /tmp
  apt-get download "${pkg}"
  mkdir -p "$O"
  # Same quoted-glob discipline as deb_repack above.
  local UP_DEB
  set -- "/tmp/${pkg}"_*.deb
  [ -f "${1:-}" ] \
    || { echo "ERROR: no upstream .deb for ${pkg} left in /tmp after download" >&2; exit 1; }
  UP_DEB="$1"
  dpkg-deb -R "$UP_DEB" "$O"
  cp -r "${SAVE_PWD}/workflows/bootc-debs-builder/${tree:?}"/* "$O/"
  for f in "${EXEC_LIST[@]}"; do
    [ -e "$O/${f:?}" ] \
      || { echo "ERROR: ${pkg} tree entry ${f} missing in the overlay" >&2; exit 1; }
    test -x "$O/${f}" \
      || { echo "ERROR: ${pkg} ${f} not executable" >&2; exit 1; }
    chmod 0755 "$O/${f}"
  done

  local ORIG_VER ARCH
  ORIG_VER=$(dpkg-deb -f "$UP_DEB" Version)
  ARCH=$(dpkg-deb -f "$UP_DEB" Architecture)

  find "$O" -not -path '*/DEBIAN/*' -type f \
    | xargs md5sum \
    | sed "s|$O/||" \
    > "$O/DEBIAN/md5sums"
  sed -i "s/^Version: .*/Version: 1:${ORIG_VER}${suffix}/" "$O/DEBIAN/control"
  # Re-verify: a control without a Version: line made the sed a silent no-op.
  grep -qF "Version: 1:${ORIG_VER}${suffix}" "$O/DEBIAN/control" \
    || { echo "ERROR: ${pkg} rewritten Version missing/wrong" >&2; exit 1; }
  dpkg-deb --build "$O" \
    "/debs/${pkg}_${ORIG_VER}${suffix}_${ARCH}.deb"
}

deb_source_build() {
  local pkg="${1:?}" commit="${2:?}" src_ver="${3:?}" lane="${4:?}"
  shift 4
  local O SAVE_PWD o p sp
  SAVE_PWD="${SAVE_PWD:-$PWD}"
  O="/output/${pkg}-src"
  mkdir -p "$O"

  # Upstream source tree, pinned to the commit the patch lane is anchored on.
  git clone --quiet https://git.proxmox.com/git/pve-cluster.git "$O/src" \
    || return 1
  git -C "$O/src" checkout --quiet "${commit:?}"

  # Lock guard on the pristine tree (pre-patch sha256), then all-or-nothing
  # dry run before the real application. Drift (exit 3) = skip the build:
  # last-good deb retained by the caller. C-lane patches mirror paths of the
  # source tree root (src/pmxcfs/...).
  python3 "${CI_SCRIPTS}/patch_guard.py" --tree "$O/src" \
    --lock "${SAVE_PWD}/workflows/bootc-debs-builder/${lane}/patches.lock.json" \
    "${SAVE_PWD}/workflows/bootc-debs-builder/${lane}"/src/pmxcfs/*.patch \
    || exit 3
  for p in "${SAVE_PWD}/workflows/bootc-debs-builder/${lane}"/src/pmxcfs/*.patch; do
    patch --quiet --dry-run --forward --remove-empty-files -p1 -d "$O/src" <"$p" \
      || { echo "ERROR: pve-src patch ${p} fails dry-run — lane drift." >&2; exit 3; }
  done
  for p in "${SAVE_PWD}/workflows/bootc-debs-builder/${lane}"/src/pmxcfs/*.patch; do
    patch --quiet --forward --remove-empty-files -p1 -d "$O/src" <"$p"
  done

  # deb-tree overlay lanes (usr/share/perl5/...) ride the same source tree:
  # rewrite the deb path to the source path (usr/share/perl5/PVE/... ->
  # src/PVE/...) and strip the .patch extension, then apply with -p3 as in
  # the overlay model.
  for o in "${@:?}"; do
    while read -r p; do
      p="${p#./}"; [ -n "${p}" ] || continue
      sp=$(echo "${p%.patch}" | sed 's|^usr/share/perl5/|src/|')
      patch --quiet --dry-run --forward -p3 -d "$O/src" "$sp" \
        <"${SAVE_PWD}/workflows/bootc-debs-builder/${o}/${p}" \
        || { echo "ERROR: overlay ${o}/${p} fails dry-run — overlay drift." >&2; exit 3; }
    done < <(cd "${SAVE_PWD}/workflows/bootc-debs-builder/${o}" && find . -name '*.patch' -print)
  done
  for o in "${@}"; do
    while read -r p; do
      p="${p#./}"; [ -n "${p}" ] || continue
      sp=$(echo "${p%.patch}" | sed 's|^usr/share/perl5/|src/|')
      patch --quiet --forward -p3 -d "$O/src" "$sp" \
        <"${SAVE_PWD}/workflows/bootc-debs-builder/${o}/${p}"
    done < <(cd "${SAVE_PWD}/workflows/bootc-debs-builder/${o}" && find . -name '*.patch' -print)
  done

  # Version from the manifest (NO epoch: the upstream top Makefile cannot
  # carry a ':' through its filename rules).
  sed -i "1s/(.*)/(${src_ver:?})/" "$O/src/debian/changelog"

  # Upstream build flow: build deps from debian/control, then the staged
  # `make deb` (not raw dpkg-buildpackage on the git root — its Makefile
  # stages src + debian into a build dir first). Debs land in $O/src.
  (cd "$O/src" && apt-get build-dep -y . && dpkg-checkbuilddeps >/dev/null)
  # The upstream Makefile runs lintian after every dpkg-buildpackage; the
  # buildenv image does not bake it, so install it on demand (quietly).
  command -v lintian >/dev/null 2>&1 || apt-get install -y -qq lintian >/dev/null
  make -C "$O/src" deb

  cp -v "$O/src"/*.deb /debs/
}

deb_custom() {
  local pkg="${1:?}" ver="${2:?}" tree="${3:?}"
  local O SAVE_PWD key src
  SAVE_PWD="${SAVE_PWD:-$PWD}"
  key="$(echo "${pkg^^}" | tr '[:lower:]-' '[:upper:]_')_VER"
  O="/output/${pkg}"
  mkdir -p "$O"
  cp -r "${SAVE_PWD}/workflows/bootc-debs-builder/${tree:?}"/* "$O/"
  if [ -e "$O/usr/sbin/${pkg}" ]; then chmod 0755 "$O/usr/sbin/${pkg}"; fi
  if [ -e "$O/DEBIAN/postinst" ]; then chmod 0755 "$O/DEBIAN/postinst"; fi

  sed -i \
    -e "s|{{ VER }}|${ver}|g" \
    -e "s|, {{ DEPS }}||g" \
    "$O/DEBIAN/control"

  ! grep -q '{{' "$O/DEBIAN/control" \
    || { echo "ERROR: ${pkg} control has unexpanded template" >&2; exit 1; }
  src="$O/usr/sbin/${pkg}"
  if [ -e "${src}" ]; then
    test -x "${src}" \
      || { echo "ERROR: ${pkg} ${src} missing/not exec" >&2; exit 1; }
    if head -1 "${src}" | grep -qE "^#!.*sh"; then
      bash -n "${src}" \
        || { echo "ERROR: ${pkg} ${src} shell syntax" >&2; exit 1; }
    fi
  fi
  if head -1 "$O/DEBIAN/postinst" | grep -qE "^#!.*sh"; then
    bash -n "$O/DEBIAN/postinst" \
      || { echo "ERROR: ${pkg} postinst shell syntax" >&2; exit 1; }
  fi

  find "$O" -not -path '*/DEBIAN/*' -type f \
    | xargs md5sum \
    | sed "s|$O/||" \
    > "$O/DEBIAN/md5sums"
  dpkg-deb --build "$O" \
    "/debs/${pkg}_${ver}_all.deb"
}
