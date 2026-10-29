#!/bin/bash
#
# kernel_helpers.sh - shared kernel-build helpers for the bootc-debs builders
# (DaemonCores-VE kernel-pve, debian-bootc linux-image). Sourced by
# deb_helpers.sh in the SAME bash that runs the build: no import appears in
# any build block. Exported to the build via CI_SCRIPTS (patch_guard.py).
#
# The kernel patch format IS the canonical patches schema (Gabriel decision
# 30/10/2026): a declarative YAML/JSON manifest of name/url/stage (+ optional
# class/topic) entries, fetched from their official upstream at build time
# and applied in file order. The helpers accept the set from TWO shapes,
# same data:
#   - a patches.yml FILE (e.g. debian-bootc kernel/patches.yml): passed as
#     the manifest path argument;
#   - a NATIVE entry key: packages.yml may carry `patches:` (identical
#     structure, copy-pasted verbatim); build-deb exports it as compact
#     JSON in PATCHES_JSON and a "-" manifest argument selects it.
# The manifests stay DATA — consumed through yq/python3 (both present in
# the build env), never a bashified patch list.
#
# Functions (mode-agnostic primitives, parameterised):
#   kernel_fetch_source <version> [sha256]            -> kernel.org tarball
#   kernel_patch_list <manifest> <arch> [class]       -> name url lines (yq)
#   kernel_apply_patches <manifest> <arch> <ver> <tree> [class]
#                                                     -> dry-run ALL, apply ALL
#   kernel_merge_config <tree> <fragment>...          -> append + olddefconfig
#   kernel_flavour_kcflags <flavour>                  -> -march=x86-64-vN
#   kernel_build_debs <tree> <target> [KCFLAGS]       -> make -j <target>
# Mode pve only (the PVE packaging is a git packaging, not a kernel.org
# tarball — kernel_pve_prepare ported verbatim from the VE lib):
#   kernel_pve_prepare <data-dir> [builddir]          -> SRCDIR/KERNEL_VERSION
#
# Globals understood:
#   CI_SCRIPTS   path to the shared DaemonCores-CI scripts (patch_guard.py)
#   KERNEL_CDN   kernel.org archive root
#   PATCH_GUARD_UPDATE  1 = rewrite the lock instead of enforcing it
set -euo pipefail

KERNEL_CDN="${KERNEL_CDN:-https://cdn.kernel.org/pub/linux/kernel}"

#######################################
# Download, verify and extract the kernel.org tarball of one release.
# Globals:
#   KERNEL_CDN: read, root of the kernel.org archive
#   SRCDIR: set, the extracted source tree
# Arguments:
#   $1 - kernel version, e.g. 6.12 or 6.1.90
#   $2 - optional sha256 of the tarball; read from sha256sums.asc when empty
# Returns:
#   Exits 1 when the download fails or the checksum does not match.
#######################################
kernel_fetch_source() {
  local version="$1" want="${2:-}" dir tarball got
  dir="${KERNEL_CDN}/v${version%%.*}.x"
  tarball="linux-${version}.tar.xz"
  cd /tmp || return 1
  curl --retry 3 -fsSLO "${dir}/${tarball}" \
    || {
      echo "ERROR: cannot download ${dir}/${tarball}" >&2
      exit 1
    }
  if [[ -z "${want}" ]]; then
    want=$(curl --retry 3 -fsSL "${dir}/sha256sums.asc" \
      | awk -v n="${tarball}" '$2 == n { print $1 }')
  fi
  got=$(sha256sum "${tarball}" | cut -d' ' -f1)
  [[ "${want}" =~ ^[0-9a-f]{64}$ && "${got}" == "${want}" ]] \
    || {
      echo "ERROR: sha256 of ${tarball} is ${got}, expected '${want}'" >&2
      exit 1
    }
  rm -rf "/tmp/linux-${version}"
  tar -xf "${tarball}"
  rm -f "${tarball}"
  SRCDIR="/tmp/linux-${version}"
  test -d "${SRCDIR}" \
    || {
      echo "ERROR: ${tarball} did not extract to ${SRCDIR}" >&2
      exit 1
    }
}

#######################################
# Print the manifest entries to apply, in order, as "name url" lines:
# stage common first, then the architecture. The {} placeholders of the
# manifest URLs ({seriesdir}, {series}, {version}, {topic}) are resolved
# INSIDE yq (strenv, never a sed round-trip). The source is a patches.yml
# FILE (manifest argument) or the native PATCHES_JSON entry data ("-" as
# the manifest argument; an array whose items share the file schema).
# class filters the debian-bootc manifests (.class field); pass an empty
# class for the VE kernel-pve manifest (entries carry no class).
# Arguments:
#   $1 - manifest path (patches.yml), or "-" for the PATCHES_JSON data
#   $2 - architecture/stage filter (amd64 or arm64)
#   $3 - kernel version (resolves {version} and {series})
#   $4 - optional class (kernel or module)
# Globals:
#   PATCHES_JSON: read when $1 is "-"
#######################################
kernel_patch_list() {
  local manifest="$1" arch="$2" version="$3" class="${4:-}"
  local series="${version%.*}"
  [[ "${version}" == *.*.* ]] || series="${version}"
  # {seriesdir}: the xanmod upstream series directory. Archived series
  # (e.g. the 7.0.x the PVE packaging tracks) live under eol/.
  local sdir="linux-${series}.y-xanmod"
  [[ "${series}" == "7.0" ]] && sdir="eol/linux-7.0.y-xanmod"
  # Doc and filter: a patches.yml FILE parses to a map whose .patches is the
  # list; the native PATCHES_JSON set IS the list (both yq-parseable).
  local doc filter
  if [[ "${manifest}" == "-" ]]; then
    [[ -n "${PATCHES_JSON:-}" ]] || return 0
    doc="${PATCHES_JSON}"
    filter='.[]'
  else
    [[ -f "${manifest}" ]] || { echo "ERROR: patch manifest ${manifest} missing" >&2; exit 1; }
    doc=$(cat "${manifest}")
    filter='.patches[]'
  fi
  for stage in common "${arch}"; do
    printf '%s\n' "${doc}" | \
      CLASS="${class:--}" STAGE="${stage}" SERIES="${series}" VERSION="${version}" \
        SDIR="${sdir}" \
        yq -r '
      '"${filter}"' | select(.stage == strenv(STAGE))
        | select(strenv(CLASS) == "-" or .class == strenv(CLASS))
        | . as $e | .name + " " +
          (.url | sub("\{seriesdir\}"; strenv(SDIR))
                  | sub("\{series\}"; strenv(SERIES))
                  | sub("\{version\}"; strenv(VERSION))
                  | sub("\{topic\}"; ($e.topic // "")))'
  done
}

#######################################
# Download the patches of one manifest, guard the files they touch, dry-run
# the WHOLE set (all-or-nothing), then apply them in manifest order.
# Nothing is tolerated: a patch that cannot be downloaded or does not apply
# fails the build and the last good deb is kept.
# Guard: when the manifest directory carries a patches.lock.json (the
# debian-bootc model), the sha256 of every touched file is checked on the
# pristine tree (patch_guard.py); a changed upstream file refuses the build
# until the lock is refreshed. Without a lock (the VE kernel-pve manifest,
# remote XanMod patches), the all-or-nothing dry-run IS the guard.
# PATCH_GUARD_UPDATE=1 rewrites the lock instead of enforcing it.
# Arguments:
#   $1 - manifest path (patches.yml)
#   $2 - architecture/stage (amd64 or arm64)
#   $3 - kernel version, e.g. 6.12.107 or 7.3.0
#   $4 - kernel source tree
#   $5 - optional class (kernel or module)
# Globals:
#   CI_SCRIPTS: read, path to patch_guard.py
#   PATCH_GUARD_UPDATE: read, optional
#######################################
kernel_apply_patches() {
  local manifest="$1" arch="$2" version="$3" tree="$4" class="${5:-}"
  local series="${version%.*}" name url dir file lock
  local -a names=() files=()
  [[ "${version}" == *.*.* ]] || series="${version}"
  dir=$(mktemp -d)
  while read -r name url; do
    [[ -n "${name}" ]] || continue
    file="${dir}/${#files[@]}.patch"
    curl --retry 3 -fsSL "${url}" -o "${file}" \
      || {
        echo "ERROR: cannot download patch ${name}: ${url}" >&2
        exit 1
      }
    names+=("${name}")
    files+=("${file}")
  done < <(kernel_patch_list "${manifest}" "${arch}" "${version}" "${class}")
  ((${#files[@]})) || return 0

  lock="$(dirname "${manifest}")/patches.lock.json"
  # A native PATCHES_JSON set ("-") carries no lock by definition: the
  # all-or-nothing dry-run is the guard (same as the lock-less VE manifest).
  if [[ "${manifest}" != "-" && -f "${lock}" ]]; then
    local guard=()
    [[ "${PATCH_GUARD_UPDATE:-0}" != 1 ]] || guard=(--update)
    python3 "${CI_SCRIPTS}/patch_guard.py" --tree "${tree}" \
      --lock "${lock}" "${guard[@]}" "${files[@]}" \
      || {
        echo "ERROR: a file touched by the patches changed upstream" >&2
        exit 1
      }
  fi

  local i
  # Every patch must dry-run before ANY touch of the tree.
  for i in "${!files[@]}"; do
    patch -d "${tree}" -p1 --no-backup-if-mismatch --dry-run <"${files[i]}" >/dev/null \
      || {
        echo "ERROR: patch ${names[i]} does not apply to ${version}" >&2
        exit 1
      }
  done
  for i in "${!files[@]}"; do
    patch -d "${tree}" -p1 --no-backup-if-mismatch <"${files[i]}" >/dev/null
    echo "applied patch ${names[i]}"
  done
  rm -rf "${dir}"
}

#######################################
# Merge kconfig fragments into a tree's .config: the fragment wins
# (append + olddefconfig — the house merge pattern, same final .config as
# scripts/kconfig/merge_config.sh -m). The build dependencies (bc, flex…)
# must be installed by the caller before the first make.
# Arguments:
#   $1 - kernel source tree
#   $2... - fragment paths, merged in order (the last one wins)
#######################################
kernel_merge_config() {
  local tree="$1"; shift
  local frag
  for frag in "$@"; do
    cat "${frag}" >> "${tree}/.config"
  done
  make -C "${tree}" olddefconfig
}

#######################################
# Print the KCFLAGS of one XanMod microarch flavour (empty for x64v1).
# Arguments:
#   $1 - flavour: x64v1 (baseline), x64v2, x64v3, x64v4
#######################################
kernel_flavour_kcflags() {
  case "$1" in
    x64v2) echo "-march=x86-64-v2" ;;
    x64v3) echo "-march=x86-64-v3" ;;
    x64v4) echo "-march=x86-64-v4" ;;
    *)     echo "" ;;
  esac
}

#######################################
# Build the Debian binary package(s) of a prepared tree
# (bindeb-pkg for the debian-bootc mode, deb-pkg for the pve mode — the
# debs land one directory above the tree in both cases).
# Arguments:
#   $1 - kernel source tree
#   $2 - make target (bindeb-pkg or deb-pkg)
#   $3 - optional KCFLAGS (e.g. the flavour -march, from kernel_flavour_kcflags)
#######################################
kernel_build_debs() {
  local tree="$1" target="$2" kcflags="${3:-}"
  local -a build_cmd=(make -j"$(nproc)")
  [[ -z "${kcflags}" ]] || build_cmd+=(KCFLAGS="${kcflags}")
  cd "${tree}"
  "${build_cmd[@]}" "${target}"
  cd /
}

#######################################
# PVE MODE: prepare the PVE kernel source tree. The pve-no-subscription
# repo carries no source index, so the packaging is fetched as the
# mirror_ubuntu-kernels submodule of git.proxmox.com/git/pve-kernel.git;
# the seed .config is the Ubuntu mirror tree config plus the PVE packaging
# deltas (the same scripts/config pass debian/rules runs). Ported verbatim
# from DaemonCores-VE workflows/bootc-debs-builder/kernel-pve/lib/
# kernel-pve-build.sh (which is being retired in favour of this helper).
# Globals:
#   KERNEL_DATA_DIR: set before the call, the repo's kernel-pve data dir
#   KERNEL_VERSION: read, resolved from the Proxmox repo when empty
#   PVE_SEED_CONFIG: set, the seed config path (empty when the tree has none)
#   SRCDIR: set, the extracted kernel source tree
# Arguments:
#   $1 - build directory (scratch for the packaging clone, e.g. /build)
# Returns:
#   Exits 1 when no kernel source tree survives the preparation.
#######################################
kernel_pve_prepare() {
  # shellcheck disable=SC2034  # PVE_SEED_CONFIG is consumed by the caller
  local builddir="${1:?}"
  export DEBIAN_FRONTEND=noninteractive
  export ARCH="${KERNEL_ARCH}"
  cd "${builddir}"
  rm -rf "${builddir}/pkg-src"
  git clone --depth 1 --recurse-submodules --shallow-submodules \
    https://git.proxmox.com/git/pve-kernel.git "${builddir}/pkg-src"
  local pkg_ver
  pkg_ver=$(sed -n 's/^proxmox-kernel-.* (\([^ )]*\)).*/\1/p' \
    "${builddir}/pkg-src/debian/changelog" | head -1)
  [[ "${pkg_ver}" == "${KERNEL_VERSION}-"* ]] \
    || echo "WARN: packaging master is ${pkg_ver}, requested kernel version ${KERNEL_VERSION}"
  SRCDIR="${builddir}/linux-${KERNEL_VERSION}"
  [[ -e "${SRCDIR}" ]] || mv "${builddir}/pkg-src/submodules/ubuntu-kernel" "${SRCDIR}"
  # git pointer: setlocalversion must not append a suffix
  rm -f "${SRCDIR}/.git" 2>/dev/null || true
  PVE_SEED_CONFIG=""
  if [[ -f "${SRCDIR}/.config" ]]; then
    cp "${SRCDIR}/.config" "${builddir}/.config.pve-seed"
    # shellcheck disable=SC2046  # the opts files are word-split on purpose
    "${SRCDIR}/scripts/config" --file "${builddir}/.config.pve-seed" \
      $(cat "${builddir}/pkg-src/debian/rules.d/config-common.opts") \
      $(cat "${builddir}/pkg-src/debian/rules.d/config-amd64.opts")
    # shellcheck disable=SC2034  # PVE_SEED_CONFIG is consumed by the caller
    PVE_SEED_CONFIG="${builddir}/.config.pve-seed"
    echo "seed config: ubuntu base + PVE packaging deltas"
  else
    echo "WARN: no base .config in the kernel tree; build starts from defconfig"
  fi
}