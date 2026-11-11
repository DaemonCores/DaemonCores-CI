#!/bin/bash
#
# kernel_helpers.sh - shared kernel-build helpers for the bootc-debs builders
# (DaemonCores-VE kernel-pve, debian-bootc linux-image). Sourced by
# deb_helpers.sh in the SAME bash that runs the build: no import appears in
# any build block. Exported to the build via CI_SCRIPTS.
#
# The kernel patch format IS the canonical patches schema (Gabriel decision
# 30/10/2026): a declarative YAML/JSON manifest of name/url/stage (+ optional
# class/topic) entries, fetched from their official upstream at build time
# and applied in file order. The helpers accept the set from TWO shapes,
# same data:
#   - a patches.yml FILE (reference mode: the product repo passes its
#     canonical manifest path, e.g. debian-bootc kernel-src/patches/yml);
#   - a NATIVE entry key: packages.yml may carry `patches:` (identical structure, copy-pasted verbatim); build-deb exports it as compact JSON in PATCHES_JSON and a "-" manifest argument selects it.
#   manifest argument. An entry MAY also carry `sha256:` (hex): the file is
#   downloaded, checksummed against it and the build fails on mismatch — for
#   a set whose upstream does not guarantee immutable URLs. Entries without
#   sha256 keep the dry-run as their only drift guard (existing schema).
# The manifests stay DATA — consumed through yq/python3 (both present in
# the build env), never a bashified patch list.
#
# Functions (mode-agnostic primitives, parameterised):
#   kernel_fetch_source <version> [sha256]            -> kernel.org tarball
#   kernel_patch_list <manifest> <arch> [class]       -> name url lines (yq)
#   kernel_apply_patches <manifest> <arch> <ver> <tree> [class]
#                                                     -> dry-run ALL, apply ALL
#   kernel_dryrun_patches <manifest> <arch> <ver> <tree> [class]
#                                                     -> dry-run ALL only
#   kernel_merge_config <tree> <fragment>...          -> append + olddefconfig
#   kernel_flavour_kcflags <flavour>                  -> -march=x86-64-vN |
#                                                        -mcpu=<arm core>
#                                                        (owner ARM OPTI
#                                                        2026-11-09: the
#                                                        arm flavours carry
#                                                        -mcpu, the arch
#                                                        codegen analogue of
#                                                        -march=x86-64-vN)
#   kernel_build_debs <tree> <target> [KCFLAGS]       -> make -j <target>
# Mode pve only (the PVE packaging is a git packaging, not a kernel.org
# tarball):
#   kernel_pve_prepare <data-dir> [builddir]          -> SRCDIR/KERNEL_VERSION
# Module signing (kernel_signing.sh) and the out-of-tree module bootstrap
# (kernel_prepare.sh) are sourced below:
#   kernel_signing_setup / kernel_signing_verify / kernel_sign_efi <image>
#   kernel_prepare                                    -> COMMON_DEB/BUILT_VER/KVER/SRCDIR
#
# Globals understood:
#   CI_SCRIPTS   path to the shared DaemonCores-CI scripts
#   KERNEL_CDN   kernel.org archive root
set -euo pipefail

KERNEL_HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${KERNEL_HELPERS_DIR}/kernel_signing.sh"
. "${KERNEL_HELPERS_DIR}/kernel_prepare.sh"

KERNEL_CDN="${KERNEL_CDN:-https://cdn.kernel.org/pub/linux/kernel}"

# Download one URL to a file; exits 1 on failure.
kernel_download_tarball() {
  curl --retry 3 -fsSL "$1" -o "$2" \
    || {
      echo "ERROR: cannot download $1" >&2
      rm -f "$2"
      exit 1
    }
}

#######################################
# Download, verify and extract the kernel.org tarball of one release.
# Globals:
#   KERNEL_CDN: read, root of the kernel.org archive
#   KERNEL_TARBALL_CACHE: read, optional directory keeping the tarball
#     between builds of one job; its sha256 is verified on every use
#   SRCDIR: set, the extracted source tree
# Arguments:
#   $1 - kernel version, e.g. 6.12 or 6.1.90
#   $2 - optional sha256 of the tarball; read from sha256sums.asc when empty
# Returns:
#   Exits 1 when the download fails or the checksum does not match.
#######################################
kernel_fetch_source() {
  local version="$1" want="${2:-}" dir tarball got cache fresh=1
  dir="${KERNEL_CDN}/v${version%%.*}.x"
  tarball="linux-${version}.tar.xz"
  cache="${KERNEL_TARBALL_CACHE:-}"
  if [[ -n "${cache}" ]]; then
    mkdir -p "${cache}" || return 1
    cd "${cache}" || return 1
  else
    cd /tmp || return 1
  fi
  if [[ -z "${want}" ]]; then
    want=$(curl --retry 3 -fsSL "${dir}/sha256sums.asc" \
      | awk -v n="${tarball}" '$2 == n { print $1 }')
  fi
  [[ "${want}" =~ ^[0-9a-f]{64}$ ]] \
    || {
      echo "ERROR: no valid sha256 for ${tarball} (got '${want}')" >&2
      exit 1
    }
  if [[ -s "${tarball}" ]]; then
    fresh=0
  else
    kernel_download_tarball "${dir}/${tarball}" "${tarball}"
  fi
  got=$(sha256sum "${tarball}" | cut -d' ' -f1)
  if [[ "${got}" != "${want}" && "${fresh}" == 0 ]]; then
    # A cached copy that no longer matches is discarded, fetched once more.
    echo "WARNING: cached ${tarball} has sha256 ${got}; downloading again" >&2
    rm -f "${tarball}"
    kernel_download_tarball "${dir}/${tarball}" "${tarball}"
    got=$(sha256sum "${tarball}" | cut -d' ' -f1)
  fi
  [[ "${got}" == "${want}" ]] \
    || {
      echo "ERROR: sha256 of ${tarball} is ${got}, expected '${want}'" >&2
      rm -f "${tarball}"
      exit 1
    }
  rm -rf "/tmp/linux-${version}"
  tar -C /tmp -xf "${tarball}"
  [[ -n "${cache}" ]] || rm -f "${tarball}"
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
# class for a shared manifest (entries carry no class).
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
  # {series}: the kernel series this build patches, e.g. 7.0 or 6.12. The
  # GPU driver manifests (patches-gpu/) patch a DIFFERENT tree: the nvidia
  # entry applies its manifest to the NVIDIA open-module tree but resolves
  # {series} as the KERNEL series (CachyOS publishes its nvidia set per
  # kernel series) — it overrides via PATCHES_SERIES.
  local series="${version%.*}"
  [[ "${version}" == *.*.* ]] || series="${version}"
  # PATCHES_SERIES overrides (see kernel_apply_patches); else the computed one.
  series="${PATCHES_SERIES:-$series}"
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
        | select(.exclude-series == null or ((.exclude-series) | any_c(. == strenv(SERIES)) | not))
        | . as $e | .name + " " +
          (.url | sub("{seriesdir}"; strenv(SDIR))
                  | sub("{series}"; strenv(SERIES))
                  | sub("{version}"; strenv(VERSION))
                  | sub("{topic}"; ($e.topic // "")))'
  done
}

#######################################
# Fuzz tolerance of one manifest patch: its optional `fuzz:` field, else the
# global KERNEL_PATCH_FUZZ, else 0. A per-patch value is the visible, reviewed
# exception for a third-party patch whose context drifted but whose placement
# was checked; every other patch stays strict.
# Arguments:
#   $1 - manifest path (patches.yml) or - for the native PATCHES_JSON set
#   $2 - patch name
# Outputs:
#   The fuzz level on stdout.
#######################################
kernel_patch_fuzz() {
  local manifest="$1" name="$2" doc filter value=""
  if [[ "${manifest}" == "-" ]]; then
    doc="${PATCHES_JSON:-[]}"
    filter='.[]'
  else
    doc=$(cat "${manifest}")
    filter='.patches[]'
  fi
  value=$(printf '%s\n' "${doc}" | NAME="${name}" yq -r \
    "${filter}"' | select(.name == strenv(NAME)) | .fuzz // ""' | head -n1)
  echo "${value:-${KERNEL_PATCH_FUZZ:-0}}"
}

#######################################
# sha256 pin of one manifest patch: its optional `sha256:` field. Empty when
# the entry carries no pin (then only the dry-run guards upstream drift).
# Arguments:
#   $1 - manifest path (patches.yml) or - for the native PATCHES_JSON set
#   $2 - patch name
# Outputs:
#   The 64-hex sha256 on stdout, empty when unpinned.
#######################################
kernel_patch_sha() {
  local manifest="$1" name="$2" doc filter value
  if [[ "${manifest}" == "-" ]]; then
    doc="${PATCHES_JSON:-[]}"
    filter='.[]'
  else
    doc=$(cat "${manifest}")
    filter='.patches[]'
  fi
  value=$(printf '%s\n' "${doc}" | NAME="${name}" yq -r \
    "${filter}"' | select(.name == strenv(NAME)) | .sha256 // ""' | head -n1)
  echo "${value}"
}

#######################################
# DRY-RUN ONLY gate of one manifest against a tree (owner order 2026-11-10:
# the kernel election judges every candidate release by THE dry-run, not by
# a URL-presence probe). Fetches every patch (the sha256 pin still applies),
# then dry-runs the WHOLE set against an untouched tree. Nothing is applied.
# On success the GLOBAL arrays KERNEL_PATCH_NAMES/FILES/FUZZES hold the set
# (kernel_apply_patches uses them to apply); a caller that only probes can
# ignore them. FAILURE RETURNS 1 (the election moves on to the next
# candidate) — it never exits.
# Arguments:
#   $1 - manifest path (patches.yml)
#   $2 - architecture/stage (amd64 or arm64)
#   $3 - kernel version, e.g. 6.12.107 or 7.3.0
#   $4 - kernel source tree (never modified)
#   $5 - optional class (kernel or module)
# Globals:
#   KERNEL_PATCH_NAMES / KERNEL_PATCH_FILES / KERNEL_PATCH_FUZZES (set)
#######################################
kernel_dryrun_patches() {
  local manifest="$1" arch="$2" version="$3" tree="$4" class="${5:-}"
  local series="${version%.*}" name url dir file
  local saved_series="${PATCHES_SERIES:-}"
  export PATCHES_SERIES="${PATCHES_SERIES:-$series}"
  local -a names=() files=() fuzzes=()
  [[ "${version}" == *.*.* ]] || series="${version}"
  dir=$(mktemp -d)
  while read -r name url; do
    [[ -n "${name}" ]] || continue
    file="${dir}/${#files[@]}.patch"
    if ! curl --retry 3 -fsSL "${url}" -o "${file}"; then
      echo "cannot download patch ${name}: ${url}" >&2
      [[ -n "${saved_series}" ]] && export PATCHES_SERIES="${saved_series}"
      return 1
    fi
    local want sha
    want=$(kernel_patch_sha "${manifest}" "${name}")
    if [[ -n "${want}" ]]; then
      [[ "${want}" =~ ^[0-9a-f]{64}$ ]] \
        || { echo "ERROR: ${name}: sha256 pin '${want}' is not a 64-hex digest" >&2; exit 1; }
      sha=$(sha256sum "${file}" | cut -d' ' -f1)
      if [[ "${sha}" != "${want}" ]]; then
        echo "patch ${name} sha256 ${sha} != pinned ${want}" >&2
        return 1
      fi
    fi
    names+=("${name}")
    files+=("${file}")
    fuzzes+=("$(kernel_patch_fuzz "${manifest}" "${name}")")
  done < <(kernel_patch_list "${manifest}" "${arch}" "${version}" "${class}")
  if [[ "${saved_series}" ]]; then
    export PATCHES_SERIES="${saved_series}"
  else
    unset PATCHES_SERIES
  fi
  KERNEL_PATCH_NAMES=("${names[@]+"${names[@]}"}")
  KERNEL_PATCH_FILES=("${files[@]+"${files[@]}"}")
  KERNEL_PATCH_FUZZES=("${fuzzes[@]+"${fuzzes[@]}"}")
  ((${#files[@]})) || return 0
  local i dry
  for i in "${!files[@]}"; do
    if ! dry=$(patch -d "${tree}" -p1 --fuzz="${fuzzes[i]}" \
      --no-backup-if-mismatch --dry-run <"${files[i]}" 2>&1); then
      echo "patch ${names[i]} does not apply to ${version}" >&2
      printf '%s\n' "${dry}" >&2
      return 1
    fi
    if ((fuzzes[i] > 0)); then
      if patch -d "${tree}" -p1 --fuzz=0 --no-backup-if-mismatch --dry-run \
        <"${files[i]}" >/dev/null 2>&1; then
        echo "::warning::patch ${names[i]}: fuzz ${fuzzes[i]} is no longer needed on ${version}, remove it from the manifest" >&2
      else
        echo "::warning::patch ${names[i]} applies on ${version} only with fuzz ${fuzzes[i]}: $(grep -c 'with fuzz' <<<"${dry}") hunk(s) placed by fuzz, re-check the placement" >&2
      fi
    fi
  done
  return 0
}

#######################################
# Download the patches of one manifest, dry-run the WHOLE set
# (all-or-nothing), then apply them in manifest order.
# Nothing is tolerated: a patch that cannot be downloaded or does not apply
# fails the build and the last good deb is kept. The guard against upstream
# drift is the strict dry-run itself: patch runs with --fuzz=0, so a hunk
# whose context moved (not just its line numbers) is refused instead of being
# applied to the wrong place; no committed hash list is kept. A manifest entry
# may carry `fuzz: N` (see kernel_patch_fuzz) for a third-party patch whose
# placement was verified; KERNEL_PATCH_FUZZ sets a global default.
# Arguments:
#   $1 - manifest path (patches.yml)
#   $2 - architecture/stage (amd64 or arm64)
#   $3 - kernel version, e.g. 6.12.107 or 7.3.0
#   $4 - kernel source tree
#   $5 - optional class (kernel or module)
# Globals:
#   KERNEL_PATCH_NAMES / KERNEL_PATCH_FILES / KERNEL_PATCH_FUZZES
#     (filled by kernel_dryrun_patches)
#######################################
kernel_apply_patches() {
  local manifest="$1" arch="$2" version="$3" tree="$4" class="${5:-}"
  local saved_series="${PATCHES_SERIES:-}"
  # The dry-run gate downloads, checksums and dry-runs every patch BEFORE
  # any touch of the tree (all-or-nothing); on failure the last good deb is
  # kept. The set stays loaded in the KERNEL_PATCH_* arrays for the apply.
  kernel_dryrun_patches "${manifest}" "${arch}" "${version}" "${tree}" "${class}" \
    || { echo "ERROR: the patch set ${manifest} fails the dry-run gate on ${version}" >&2; exit 1; }
  ((${#KERNEL_PATCH_FILES[@]})) || return 0

  local i
  for i in "${!KERNEL_PATCH_FILES[@]}"; do
    patch -d "${tree}" -p1 --fuzz="${KERNEL_PATCH_FUZZES[i]}" --no-backup-if-mismatch \
      <"${KERNEL_PATCH_FILES[i]}" >/dev/null
    echo "applied patch ${KERNEL_PATCH_NAMES[i]}"
  done
  rm -rf "$(dirname "${KERNEL_PATCH_FILES[0]}")"
  if [[ -n "${saved_series}" ]]; then
    export PATCHES_SERIES="${saved_series}"
  else
    unset PATCHES_SERIES
  fi
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
    # arm64 flavours (owner mandate ARM OPTI 2026-11-09: -mcpu, not
    # -march — the kernel arm64 Makefile never passes a C -march (only
    # -Wa,-march for asm), KCFLAGS lands LAST in the top Makefile
    # (KBUILD_CFLAGS += $(KCFLAGS)), so the flavour flag is the single
    # C codegen lever. -mcpu=<core> pins the SAME ISA floor the old
    # -march=armv8.2-a+lse / armv9-a+sve2 carried (gcc 13 aarch64 -Q:
    # cortex-a76 => .arch armv8.2-a, neoverse-v2 => .arch armv9-a+sve2)
    # PLUS the microarch scheduler tuning the report names as the
    # principal arm64 codegen gain — the flavours are LEVELS, the two
    # cores below are the level representatives the matrix declares
    # (Cortex-A76 class, Neoverse V2 class). armv8.0 stays the bare
    # baseline: the flavour spans Cortex-A53 AND A72, pinning one CPU
    # would mis-tune the other.
    armv8.2-lse) echo "-mcpu=cortex-a76" ;;
    armv9-sve2)  echo "-mcpu=neoverse-v2" ;;
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
# Globals:
#   KERNEL_MAKE_ARGS: optional, extra make arguments (word-split, e.g. a
#     command-line variable override the tree needs)
#######################################
kernel_build_debs() {
  local tree="$1" target="$2" kcflags="${3:-}"
  local -a build_cmd=(make -j"$(nproc)")
  # shellcheck disable=SC2206  # word-splitting of the make arguments is intended
  [[ -z "${KERNEL_MAKE_ARGS:-}" ]] || build_cmd+=(${KERNEL_MAKE_ARGS})
  [[ -z "${kcflags}" ]] || build_cmd+=(KCFLAGS="${kcflags}")
  # The deb-pkg source package is built from `git archive` HEAD: without a
  # commit of the patch set, dpkg-source would have to embed the whole
  # worktree diff over the unpatched orig tarball (fuzz/refusal — first real
  # run). Committing makes the source package carry the patch set and keeps
  # the flavour passes re-runnable over the same tree. Trees without git
  # linkage (bindeb-pkg mode) are untouched.
  if git -C "${tree}" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "${tree}" add -A
    git -C "${tree}" -c user.email="pve-build@debian.local" \
      -c user.name="pve xanmod build" commit -qam \
      "patch set ${KERNEL_SERIES:-} over the prepared tree"
  fi
  cd "${tree}"
  "${build_cmd[@]}" "${target}"
  cd /
}

#######################################
# kernel_build_module — the ONE module pair gate (build + deb via the
# composed helpers; kept as the canonical name, owner 05/11 midi).
# Arguments (positional, see the two composable helpers below):
#   $1  prepared kernel tree (SYSSRC)   $2  module workdir
#   $3  build command string            $4  deb package name
#   $5  deb version                     $6  kernel release
#   $7  in-kernel layout                $8  Depends line
#   $9  description (\n escapes)        $10.. .ko paths to ship
#######################################
kernel_build_module() {
  local syssrc="${1:?}" workdir="${2:?}" cmd="${3:?}"
  local pkg ver release layout depends desc
  pkg="${4:?}" ver="${5:?}" release="${6:?}" layout="${7:?}"
  depends="${8:?}" desc="${9:?}"
  shift 9
  kernel_module_run_build "${syssrc}" "${workdir}" "${cmd}"
  kernel_modules_deb "${pkg}" "${ver}" "${release}" "${layout}" \
    "${depends}" "${desc}" "$@"
}

#######################################
# BUILD a kernel module against a prepared kernel tree (shared by every
# module pair: the nvidia pair and the zfs pair build ONCE, the deb is
# consumed by both lanes of the same release — owner 05/11 midi point c).
# The command string may reference $SYSSRC/$SYSOUT (absolute, resolved).
# Globals:
#   (none)
# Arguments:
#   $1 - the prepared kernel tree (SYSSRC: kbuild, .config, symvers)
#   $2 - the module workdir (already cloned/configured)
#   $3 - the build command, run with set -e from the workdir
# Returns:
#   Non-zero on build failure.
#######################################
kernel_module_run_build() {
  local syssrc="${1:?}" workdir="${2:?}" cmd="${3:?}"
  test -s "${syssrc}/.config" \
    || { echo "ERROR: kernel_module_run_build: ${syssrc} is not a kernel tree (.config missing)" >&2; return 1; }
  SYSSRC="$(readlink -e "${syssrc}")" SYSOUT="$(readlink -e "${syssrc}")" \
    bash --noprofile --norc -eo pipefail -c "${cmd}"
}

#######################################
# ASSEMBLE a kernel-modules deb (shared layout: /lib/modules/<release>/kernel/
# <layout>/, depmod postinst, md5sums, strip --strip-debug on every .ko) —
# the deb is flavour-free beside its release string and consumed by every
# lane publishing the same release (owner 05/11 midi points c/e).
# Globals:
#   (none)
# Arguments:
#   $1 - deb package name
#   $2 - deb version
#   $3 - kernel release string (the /lib/modules dir)
#   $4 - in-tree layout of the .kos (e.g. zfs, drivers/video)
#   $5 - the Depends line
#   $6 - the description body (\n escapes allowed)
#   $7,... - the absolute .ko file paths shipped, in order
# Returns:
#   Exits non-zero when a .ko is missing or dpkg-deb fails.
#######################################
kernel_modules_deb() {
  local pkg="${1:?}" ver="${2:?}" release="${3:?}" layout="${4:?}"
  local depends="${5:?}" desc="${6:?}"
  shift 6
  local out="/output/${pkg}" ko arch
  arch="$(dpkg --print-architecture)"
  rm -rf "${out}"
  mkdir -p "${out}/DEBIAN" "${out}/lib/modules/${release}/kernel/${layout}"
  for ko in "$@"; do
    test -s "${ko}" \
      || { echo "ERROR: kernel_modules_deb: ${ko} missing" >&2; exit 1; }
    strip --strip-debug "${ko}"
    cp "${ko}" "${out}/lib/modules/${release}/kernel/${layout}/"
  done
  printf 'Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: DaemonCores <packaging@daemoncores.org>\nDepends: %s\nDescription: %s\nSection: kernel\nPriority: optional\n' \
    "${pkg}" "${ver}" "${arch}" "${depends}" "$(printf '%b' "${desc}")" \
    > "${out}/DEBIAN/control"
  printf '#!/bin/sh\nset -e\nif command -v depmod >/dev/null 2>&1; then\n  depmod -a %s\nfi\n' \
    "${release}" > "${out}/DEBIAN/postinst"
  chmod 755 "${out}/DEBIAN/postinst"
  ( cd "${out}" \
    && find . -type f ! -path './DEBIAN/*' | sed 's|^\./||' >> DEBIAN/md5sums )
  dpkg-deb --build "${out}" "/debs/${pkg}_${ver}_${arch}.deb"
  ls -la "/debs/${pkg}_${ver}_${arch}.deb"
}

#######################################
# Clone the PVE kernel packaging with its submodules at the newest commit
# whose submodule pins are all fetchable. Upstream occasionally pins a
# submodule commit that is not (yet) on its mirror; a plain recursive clone
# of master then aborts ("not our ref"). The full history is cloned so
# walking back is possible, and each commit touching the submodules is tried
# newest first.
# Globals:
#   PVE_PACKAGING_MAX_BACK: optional, commits to walk back (default 20)
# Arguments:
#   $1 - packaging repository URL
#   $2 - destination directory
# Returns:
#   0 when a commit with fetchable pins is checked out, 1 otherwise.
#######################################
kernel_pve_clone_packaging() {
  local url="${1:?}" dest="${2:?}"
  local max="${PVE_PACKAGING_MAX_BACK:-20}" commit tried=0
  git clone --quiet --no-checkout "${url}" "${dest}" \
    || return 1
  while read -r commit; do
    # 2026-11-04: the packaged kernel carries the objtool klp-checksum tool
    # whose C does not compile against ANY of the packaged kernel trees
    # (insn_reloc et al. are missing there) — proven on 41925723483 /
    # 41930175340 / 41936105671.
    # 2026-11-05 walk-back path VERIFIED against the real repos: the tool is
    # NOT an object of git.proxmox.com/pve-kernel.git itself (rev-list
    # --all --objects: zero klp-checksum entries in the whole history) — the
    # kernel tree arrives via the submodules/ubuntu-kernel GITLINK, so a
    # cat-file of "${commit}:pkg-src/kernel/tools/objtool/klp-checksum.c"
    # (the old path: dest-name-prefixed on top of it) could NEVER match and
    # the walk-back never fired. The correct check is the on-disk file under
    # the submodule, testable only AFTER submodule update checks it out.
    # Walk back past every state that carries it: with the tool in the tree
    # upstream is unbuildable, with ANY kernel.
    tried=$((tried + 1))
    git -C "${dest}" checkout --quiet --force "${commit}" || return 1
    if git -C "${dest}" submodule update --init --depth 1 --recommend-shallow; then
      if [[ -f "${dest}/submodules/ubuntu-kernel/tools/objtool/klp-checksum.c" ]]; then
        echo "WARN: packaging ${commit} pins a kernel tree carrying the uncompilable klp-checksum tool; walking back past it"
        git -C "${dest}" submodule deinit --all --force >/dev/null 2>&1 || true
        rm -rf "${dest}/.git/modules"
        continue
      fi
      [[ "${tried}" -eq 1 ]] \
        || echo "WARN: packaging master has unfetchable submodule pins; using ${commit} (${tried} submodule update(s) back)"
      return 0
    fi
    echo "WARN: submodule pins of ${commit} not fetchable" >&2
    git -C "${dest}" submodule deinit --all --force >/dev/null 2>&1 || true
    rm -rf "${dest}/.git/modules"
  done < <(git -C "${dest}" rev-list --first-parent -n "${max}" HEAD -- submodules)
  echo "ERROR: no packaging commit with fetchable submodule pins in the last ${max} submodule updates" >&2
  return 1
}

#######################################
# PVE MODE: prepare the PVE kernel source tree. The pve-no-subscription
# repo carries no source index, so the packaging is fetched as the
# mirror_ubuntu-kernels submodule of git.proxmox.com/git/pve-kernel.git;
# the seed .config is the Ubuntu mirror tree config plus the PVE packaging
# deltas (the same scripts/config pass debian/rules runs).
# Globals:
#   KERNEL_DATA_DIR: set before the call, the repo's shared kernel data dir
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
  kernel_pve_clone_packaging "https://git.proxmox.com/git/pve-kernel.git" \
    "${builddir}/pkg-src" || exit 1
  local pkg_ver
  pkg_ver=$(sed -n 's/^proxmox-kernel-.* (\([^ )]*\)).*/\1/p' \
    "${builddir}/pkg-src/debian/changelog" | head -1)
  [[ "${pkg_ver}" == "${KERNEL_VERSION}-"* ]] \
    || echo "WARN: packaging master is ${pkg_ver}, requested kernel version ${KERNEL_VERSION}"
  SRCDIR="${builddir}/linux-${KERNEL_VERSION}"
  # cp -a, NOT mv: the module git config core.worktree stays relative to the
  # original tree position, and `git config core.worktree` must chdir to it
  # (through the OLD relative path) before the repoint lands — verbatim from
  # the lib prepare (which copies for the same reason).
  [[ -e "${SRCDIR}" ]] || cp -a "${builddir}/pkg-src/submodules/ubuntu-kernel" "${SRCDIR}"
  # Keep the git worktree linkage: `make deb-pkg` builds the source package
  # with check-git and refuses to run outside a git repository (first real
  # run: "creating source package requires git repository"). The pointer is
  # made absolute because the tree is copied/moved out of pkg-src; the
  # XanMod setlocalversion patch (0017) drops the git tag suffix, so the
  # release stays 7.0.14-<LOCALVERSION>.
  if [[ -f "${SRCDIR}/.git" ]]; then
      local gd
      gd=$(sed 's/^gitdir: *//' "${SRCDIR}/.git")
      # The relative pointer was written for the ORIGINAL location
      # (pkg-src/submodules/ubuntu-kernel, base ${builddir}/pkg-src):
      # ../../.git from there is pkg-src/.git. Map it to its absolute spot;
      # the module git dir travels with pkg-src, the worktree stays the
      # moved tree.
      [[ "${gd:0:1}" == "/" ]] || gd="${builddir}/pkg-src/${gd#../../}"
      if [[ -d "${gd}" ]]; then
        echo "gitdir: ${gd}" > "${SRCDIR}/.git"
        # The module config carries core.worktree pointing at the original
        # location (check-git needs `rev-parse --show-cdup` empty from the
        # tree top): repoint it at the moved tree.
        git --git-dir="${gd}" config core.worktree "${SRCDIR}"
        git -C "${SRCDIR}" rev-parse --git-dir >/dev/null 2>&1 \
          || rm -f "${SRCDIR}/.git"   # give up quietly; deb-pkg fails below with the real error
      else
        rm -f "${SRCDIR}/.git"
      fi
  fi
  PVE_SEED_CONFIG=""
  if [[ -f "${builddir}/.config.pve-seed" ]]; then
    # Pre-mounted seed (captured from the host: /boot/config-<kernel> of the
    # running PVE kernel) wins; the PVE packaging deltas are already baked
    # into a running PVE kernel config. Ported verbatim from the lib.
    PVE_SEED_CONFIG="${builddir}/.config.pve-seed"
    echo "seed config: pre-mounted host config"
  elif [[ -f "${SRCDIR}/.config" ]]; then
    cp "${SRCDIR}/.config" "${builddir}/.config.pve-seed"
    # shellcheck disable=SC2046  # the opts files are word-split on purpose
    "${SRCDIR}/scripts/config" --file "${builddir}/.config.pve-seed" \
      $(cat "${builddir}/pkg-src/debian/rules.d/config-common.opts") \
      $(cat "${builddir}/pkg-src/debian/rules.d/config-amd64.opts")
    # shellcheck disable=SC2034  # PVE_SEED_CONFIG is consumed by the caller
    PVE_SEED_CONFIG="${builddir}/.config.pve-seed"
    echo "seed config: ubuntu base + PVE packaging deltas"
  else
    echo "WARN: no seed and no base .config in the kernel tree; build starts from defconfig"
  fi
  export PVE_SEED_CONFIG SRCDIR
}