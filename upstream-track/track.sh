#!/bin/bash
#
# track.sh - rolling upstream versions for manifest packages.
#
# A package opts in with a `track:` map in its manifest entry:
#
#   track:
#     BOOTC_VER:                  # env variable that receives the version
#       repo: bootc-dev/bootc     # GitHub repository whose latest release wins
#       strip-v: false            # true: drop the leading "v" of the tag
#       digests:                  # env variable -> release asset name; the
#         BOOTC_VENDOR_SHA256: "bootc-{version}-vendor.tar.zstd"
#
# A package that follows a branch instead of releases (rolling) sets `head`:
#
#   track:
#     GRUB_COMMIT:                # env variable that receives the head commit
#       repo: rhboot/grub2
#       head: true                # follow the head of the default branch
#
# A package that follows a kernel.org release stream sets `kernel-org` to the
# moniker of releases.json (mainline, stable or longterm):
#
#   track:
#     KERNEL_VER:               # env variable that receives the version
#       kernel-org: stable
#       digests:                # env variable -> tarball name; the SHA-256
#         KERNEL_SHA256: "linux-{version}.tar.xz"   # is read from sha256sums.asc
#
#     An optional `series:` value ("7.2") restricts the candidates to that
#     STABLE STREAM (the patch-proven series, never a fixed version — the
#     owner order 2026-11-05: the version is resolved dynamically at build
#     time): the newest 7.2.x wins while it streams, and the entry
#     auto-advances when the series moves.
#
#     RETIRED (owner order 2026-11-10): the `series:` stream anchor is gone.
#     The election is the LATEST-PASSING-GATE, never a constant: the
#     candidates are probed from the NEWEST kernel.org stable/longterm
#     release downward, each judged by THE dry-run of the full patch set
#     (all-or-nothing, the manifest's declared fuzz levels, default 0) on
#     the real tarball tree; the first candidate whose whole set applies is
#     the elected version. Within a failing series the probe descends to
#     the next point release, and a series that fails whole moves on to the
#     next lower series. A patch that dry-runs but does not compile stays a
#     documented K-PORT, never a reason to pin a series.
#
# `require-patches` on the entry names the patch manifest(s) the dry-run
# election judges: `engine` resolves the canonical BUILD ENGINE set
# (patches-shared.yml + patches-pve.yml + the linux-tree gpu manifests next
# to this script's repository root — the debs builder runs the track step
# from the engine checkout, so the path is local, never a dist fetch between
# production repositories), `native` is the entry's `patches:` data, a path
# is a patches.yml relative to the package manifest. It never resolves a
# kernel older than the highest one already published (debs-cache/hashes.json):
# the build then fails and the last-good deb is kept, instead of a downgrade.
#
# {version} is the tag without its leading "v", {tag} is the tag as published.
# The SHA-256 of each asset comes from the GitHub release API (`digest`), so the
# build keeps verifying its downloads without a checksum committed per version.
#
# Modes (first argument):
#   resolve  Resolve the tracked variables of PKG and write the manifest env
#            with them merged in to the step output `env`, and `skip=true` when
#            a kernel entry has no eligible release yet (nothing to build).  An entry that cannot
#            be resolved is left unset: the build then fails and the last-good
#            published deb is kept.  With STRICT=true the step fails instead.
#   check    Report, for every tracked package, the entries whose latest
#            release is not what the published APT repository ships.  With
#            STRICT=true the exit status is 1 when one is behind.
#
# Environment:
#   MAN       manifest path (default workflows/bootc-debs-builder/packages.yml)
#   SOURCE_MAN  the manifest MAN was expanded from (default MAN); its directory
#             is what `require-patches` is relative to
#   PKG       package name (resolve)
#   BASE_ENV  KEY=VALUE lines of the package env (resolve)
#   ARCH      architecture whose published debs are compared (check)
#   STRICT    true to turn unresolved / behind into a failure
#   GH_TOKEN  optional token, only to lift the API rate limit
#   KERNEL_ORG_URL  root of kernel.org (default https://www.kernel.org)
#   GIT_TAGS_URL    stable git tree for the pass-2 point-release walk of a
#                   failing series (default the kernel.org stable tree)
#   KERNEL_ELECT_CACHE  scratch dir of the election (default
#                   ${RUNNER_TEMP:-/tmp}/dc-kernel-elect)
#   CI_ENGINE_ROOT  build-engine root for the `engine` patch set and the
#                   dry-run helpers (default beside this script)
#   GITHUB_API_URL, PAGES_URL, GITHUB_REPOSITORY, GITHUB_OUTPUT as in Actions.
set -euo pipefail

# The auto security gates judge every adopted candidate before the
# resolution feeds the build: CARE_DELAY (24 h default, 0 = hotfix) holds
# a freshly elected candidate; the OSV.dev/GHSA advisories hold on an
# open advisory on the source or the commit; a CISA KEV / linux-cve-
# announce CRITICAL kernel CVE bypasses the delay. Every verdict is
# logged to the election selection.log (ELECTION_DIR), which the publish
# step materializes as the SBOM source. See upstream-track/security_gate.sh
# for the consumed variables.
MAN="${MAN:-workflows/bootc-debs-builder/packages.yml}"
API="${GITHUB_API_URL:-https://api.github.com}"
# Default when UNSET only; an explicitly empty value disables the walk
# (tests) — the `+x` test honours the difference.
if [[ -z "${GIT_TAGS_URL+x}" ]]; then
  GIT_TAGS_URL="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
fi

GATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=upstream-track/security_gate.sh
source "${GATE_DIR}/security_gate.sh"

#######################################
# Fetch the latest (non-prerelease) release of a repository as JSON.
# Arguments:
#   $1 - owner/name
#######################################
fetch_release() {
  fetch_api "repos/$1/releases/latest"
}

#######################################
# Print the releases.json entry of a kernel.org moniker as JSON.
# Arguments:
#   $1 - moniker (mainline, stable or longterm)
#######################################
fetch_kernel_release() {
  curl --retry 3 -fsSL "${KERNEL_ORG_URL:-https://www.kernel.org}/releases.json" \
    | MONIKER="$1" yq -p=json -o=json \
      '[.releases[] | select(.moniker == strenv(MONIKER))] | .[0] // ""'
}

#######################################
# Succeed when every patch URL (one per line) exists for one kernel series.
# Arguments:
#   $1 - url lines (one patch URL per line)
#   $2 - kernel version, e.g. 7.2.11
#######################################
patch_urls_available() {
  local url series="${2%.*}" sdir
  [[ "$2" == *.*.* ]] || series="$2"
  # {seriesdir}: the xanmod upstream series directory; archived series
  # (7.0.x) live under eol/ — same rule as kernel_patch_list.
  sdir="linux-${series}.y-xanmod"
  [[ "${series}" == "7.0" ]] && sdir="eol/linux-7.0.y-xanmod"
  while IFS= read -r url; do
    [[ -n "${url}" ]] || continue
    url="${url//\{seriesdir\}/${sdir}}"
    url="${url//\{series\}/${series}}"
    url="${url//\{version\}/$2}"
    curl --retry 3 -fsSL -o /dev/null "${url}" || {
      echo "patch not available for ${2}: ${url}" >&2
      return 1
    }
  done <<<"$1"
}

#######################################
# URL lines of one entry's patch set: the native `patches:` entries of the
# manifest (require-patches: native), a patches.yml path relative to the
# package manifest (the VE kernel-pve lane), or `engine` (require-patches:
# engine): the canonical shared set of the BUILD ENGINE
# (build-deb/kernel-shared/patches-shared.yml, resolved next to this
# script's own repository root — the debs builder runs the track step from
# the engine checkout, so the path is local, never a dist fetch between
# production repositories). Printed one per line.
# Arguments:
#   $1 - require-patches value
#   $2 - package name
#######################################
patch_urls() {
  if [[ "$1" == "native" ]]; then
    yq ".packages[] | select(.name == \"$2\") | (.patches // [])[].url" "${MAN}"
  elif [[ "$1" == "engine" ]]; then
    local engine
    engine="${CI_ENGINE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    [[ -r "${engine}/build-deb/kernel-shared/patches-shared.yml" ]] || {
      echo "engine patch manifest not found: ${engine}/build-deb/kernel-shared" >&2
      return 1
    }
    yq '.patches[] | . as $p | $p.url | sub("{topic}"; ($p.topic // ""))' \
      "${engine}/build-deb/kernel-shared/patches-shared.yml"
  else
    local manifest
    manifest="$(dirname "${SOURCE_MAN:-${MAN}}")/$1"
    [[ -r "${manifest}" ]] || {
      echo "patch manifest ${manifest} not found" >&2
      return 1
    }
    yq '.patches[].url' "${manifest}"
  fi
}

#######################################
# Print the debs-cache keys a package publishes on one architecture: the
# plain package name for a single entry, or `variant-name-prefix` + variant
# for an expanded one — the published cache indexes the per-variant deb
# names (linux-image-ia-x64v1-amd64), never the manifest family name, so
# looking up the family name can only report a false "not published".
# Prints nothing when the entry is declared for another architecture (its
# `when:` gate): the check of this arch leaves it to the lane building it.
# Globals:
#   MAN
# Arguments:
#   $1 - package name
#   $2 - arch
#######################################
published_keys() {
  local pkg="$1" arch="$2" when prefix variants variant vwhen
  when=$(yq ".packages[] | select(.name == \"$pkg\") | .when.arch // \"\"" "${MAN}")
  [[ -z "${when}" || "${when}" == "${arch}" ]] || return 0
  prefix=$(yq ".packages[] | select(.name == \"$pkg\") | .\"variant-name-prefix\" // \"\"" \
    "${MAN}")
  if [[ -z "${prefix}" ]]; then
    printf '%s-%s\n' "${pkg}" "${arch}"
    return 0
  fi
  variants=$(yq -o=json \
    ".packages[] | select(.name == \"$pkg\") | (.variants // {})" "${MAN}")
  while IFS= read -r variant; do
    [[ -n "${variant}" ]] || continue
    vwhen=$(yq -p=json ".\"${variant}\".when.arch // \"\"" <<<"${variants}")
    [[ -z "${vwhen}" || "${vwhen}" == "${arch}" ]] || continue
    printf '%s%s-%s\n' "${prefix}" "${variant}" "${arch}"
  done < <(yq -p=json 'keys | .[]' <<<"${variants}")
}

#######################################
# Print the highest kernel version already published for a package, empty
# when nothing is published or the publication cannot be read.
# Globals:
#   ARCH
#   PAGES_URL
#   GITHUB_REPOSITORY
# Arguments:
#   $1 - package name
#######################################
published_kernel_floor() {
  local arch="${ARCH:-amd64}" base published
  if [[ -n "${PAGES_URL:-}" ]]; then
    base="${PAGES_URL%/}"
  elif [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
    base="https://${GITHUB_REPOSITORY%%/*}.github.io/${GITHUB_REPOSITORY#*/}"
    base="${base,,}"
  else
    return 0
  fi
  published=$(curl --retry 3 -fsSL "${base}/debs-cache/hashes.json") || return 0
  local key versions="" v
  while IFS= read -r key; do
    [[ -n "${key}" ]] || continue
    v=$({
      PK="${key}" yq -p=json '.[strenv(PK)].debs[]' <<<"${published}" 2>/dev/null \
        | grep -oE '_[0-9]+\.[0-9]+(\.[0-9]+)?-' | tr -d '_-'
    } || true)
    [[ -n "${v}" ]] && versions+="${v}"$'\n'
  done < <(published_keys "$1" "${arch}")
  sort -rV <<<"${versions}" | head -n1 || true
}

#######################################
# Print DIGEST_ENV=<sha256> for each digest a tracked kernel entry declares,
# read from the sha256sums.asc next to the tarball.  Fails, naming the
# tarball, when one has no digest line (a release candidate, or a release
# whose checksums are not published yet).
# Arguments:
#   $1 - package name
#   $2 - env variable key
#   $3 - kernel version
#   $4 - tarball URL (kernel.org `source`)
#######################################
kernel_digests() {
  local sel pair denv name digest sums=""
  sel=".packages[] | select(.name == \"$1\") | .track.\"$2\""
  while IFS= read -r pair; do
    [[ -n "${pair}" ]] || continue
    denv="${pair%%=*}"
    name="${pair#*=}"
    name="${name//\{version\}/$3}"
    [[ -n "${sums}" ]] \
      || sums=$(curl --retry 3 -fsSL "${4%/*}/sha256sums.asc") || return 1
    digest=$(awk -v n="${name}" '$2 == n { print $1 }' <<<"${sums}")
    [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || {
      echo "no sha256 for ${name} in ${4%/*}/sha256sums.asc" >&2
      return 1
    }
    printf '%s=%s\n' "${denv}" "${digest}"
  done < <(yq "${sel} | (.digests // {}) | to_entries | .[] \
    | .key + \"=\" + .value" "${MAN}")
}

#######################################
# Patch manifests the dry-run election judges, colon-separated. Resolved
# from the `require-patches` value: `engine` = the build-engine canonical
# kernel-tree set (the nvidia manifest is NOT in the set — it patches the
# NVIDIA module tree, and its series rides the elected kernel anyway).
# Arguments:
#   $1 - require-patches value
#   $2 - package name
#######################################
election_manifests() {
  local req="$1" pkg="$2" root
  if [[ "${req}" == "engine" ]]; then
    root="${CI_ENGINE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    local shared="${root}/build-deb/kernel-shared"
    [[ -r "${shared}/patches-shared.yml" ]] || {
      echo "engine patch manifest not found: ${shared}" >&2
      return 1
    }
    printf '%s\n' \
      "${shared}/patches-shared.yml:${shared}/patches-pve.yml:${shared}/patches-gpu/amd.yml:${shared}/patches-gpu/intel.yml"
  else
    patch_urls "${req}" "${pkg}" >/dev/null || return 1
    # native / path: the single manifest named by the entry.
    local man
    if [[ "${req}" == "native" ]]; then
      man="${MAN}"
    else
      man="$(dirname "${SOURCE_MAN:-${MAN}}")/${req}"
    fi
    printf '%s\n' "${man}"
  fi
}

#######################################
# Point releases of one stable series from the released tags of the stable
# tree (a releases.json entry names only the newest point of a series; a
# descender probe of a failing series walks its older points too). Versions
# whose tarball has no line in sha256sums.asc are filtered later by
# kernel_digests. Empty when GIT_TAGS_URL is unset.
# Arguments:
#   $1 - series, e.g. 7.3
#######################################
series_point_releases() {
  local series="$1" refs
  [[ -n "${GIT_TAGS_URL:-}" ]] || return 0
  refs=$(git ls-remote --refs --tags "${GIT_TAGS_URL}" "refs/tags/v${series}.*" \
    2>/dev/null | awk '{print $2}' | sed 's#refs/tags/##' | grep -E '^v[0-9.]+$' || true)
  printf '%s\n' "${refs}" | sed 's/^v//' | sort -rV
  return 0
}

#######################################
# THE dry-run judge (owner order 2026-11-10): resolve a tarball, fetch its
# sha256 from kernel.org sha256sums.asc, extract and dry-run EVERY manifest
# of the election set (common + arm64 stages — the patch matching is
# arch-blind, one tree serves both arches). The first candidate whose FULL
# set applies fuzz-0 wins; a candidate is judged on an untouched tree and
# never modified by the probe.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#   $3 - kernel version
#   $4 - tarball URL (kernel.org source)
#######################################
election_dryrun() {
  local pkg="$1" key="$2" version="$3" source="$4" cache tree sha arch manifest
  cache="${KERNEL_ELECT_CACHE:-${RUNNER_TEMP:-/tmp}/dc-kernel-elect}"
  mkdir -p "${cache}"
  tree="${cache}/tree-${version}"
  if [[ ! -f "${tree}/.elected-tree" ]]; then
    # The digest comes from the SAME sha256sums.asc the resolution pins —
    # a candidate with an unpublished checksum is NOT READY (rc 2: nothing
    # to build), never a dry-run verdict.
    sha=$(kernel_digests "${pkg}" "${key}" "${version}" "${source}") || return 2
    (cd "${cache}" && curl --retry 3 -fsSL "${source}" -o "linux-${version}.tar.xz")
    [[ "$(sha256sum "${cache}/linux-${version}.tar.xz" | cut -d' ' -f1)" == "$(cut -d'=' -f2 <<<"${sha}")" ]] || return 1
    rm -rf "${tree}"
    mkdir -p "${tree}"
    tar -xJf "${cache}/linux-${version}.tar.xz" -C "${tree}" --strip-components=1
    touch "${tree}/.elected-tree"
  fi
  # Engine helpers: they LIVE in this script's own checkout (the debs
  # builder runs the track step from the engine tree); CI_ENGINE_ROOT only
  # re-points the patch manifests for the fixture tests.
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [[ -r "${root}/scripts/kernel_helpers.sh" ]] \
    || {
      echo "engine helpers missing: ${root}/scripts/kernel_helpers.sh" >&2
      return 1
    }
  # shellcheck source=/dev/null
  source "${root}/scripts/kernel_helpers.sh"
  local manifests
  manifests=$(election_manifests "$(election_require "$1" "$2")" "${pkg}") || return 1
  IFS=':' read -r -a manifests <<<"${manifests}"
  for manifest in "${manifests[@]}"; do
    for arch in amd64 arm64; do
      if kernel_dryrun_patches "${manifest}" "${arch}" "${version}" "${tree}"; then
        echo "election: ${manifest} [${arch}] dry-run PASS on ${version}" >&2
      else
        echo "election: ${manifest} [${arch}] dry-run FAIL on ${version}" >&2
        return 1
      fi
    done
  done
  return 0
}

#######################################
# Print the `require-patches` value of a tracked entry, empty when it has
# none.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#######################################
election_require() {
  yq ".packages[] | select(.name == \"$1\") | .track.\"$2\".\"require-patches\" // \"\"" \
    "${MAN}"
}

#######################################
# The security gates over one adopted resolution: the candidate age is
# judged by CARE_DELAY (24 h default, 0 = hotfix; a CISA KEV / linux-cve-
# announce CRITICAL kernel CVE bypasses the delay for a kernel candidate),
# then the OSV.dev + GHSA advisories of the source (an open advisory on
# the SOURCE or the COMMIT holds). The adopted candidate is appended to
# the election selection.log and the package record file either way.
# Globals:
#   PKG
#   GATE_JUDGE_REASON the hold reason, read by resolve()
# Arguments:
#   $1 - env variable key
#   $2 - the resolved KEY=VALUE lines (first line KEY=<version|commit>)
# Returns:
#   0 = adopted (record updated); 3 = hold (nothing adopted)
#######################################
gate_judge() {
  local key="$1" lines="$2" id sha moniker repo date url entry osv ghr
  local care_verdict flux_verdict rc kind
  id=$(printf '%s\n' "${lines}" | head -n1 | cut -d= -f2-)
  [[ -n "${id}" ]] || return 0
  sha=$(printf '%s\n' "${lines}" | grep -m1 'SHA256=' | cut -d= -f2- || true)
  moniker=$(kernel_moniker "${PKG}" "${key}")
  if [[ -n "${moniker}" ]]; then
    # kernel: the release date + tarball source of the elected version
    kind="kernel"
    entry=$(releases_entry "${id}" 2>/dev/null || true)
    date=$(yq -p=json -r '.date // ""' <<<"${entry}" 2>/dev/null) || date=""
    url=$(yq -p=json -r '.source // ""' <<<"${entry}" 2>/dev/null) || url=""
    osv=$(printf '{"package":{"name":"linux_kernel","ecosystem":"Linux"},"version":"%s"}' "${id}")
    ghr="${GATE_KERNEL_GHSA_REPO:-torvalds/linux}"
  elif is_head "${PKG}" "${key}"; then
    kind="commit"
    repo=$(PKG="${PKG}" TRACK_KEY="${key}" yq -r \
      '.packages[] | select(.name == strenv(PKG)) | .track[strenv(TRACK_KEY)].repo // ""' "${MAN}")
    url="${repo}/${id}"
    date=$(fetch_api "repos/${repo}/commits/${id}" 2>/dev/null \
      | yq -p=json -r '.commit.committer.date // ""') || date=""
    osv=$(printf '{"commit":"%s"}' "${id}")
    ghr="${repo}"
  else
    kind="release"
    repo=$(PKG="${PKG}" TRACK_KEY="${key}" yq -r \
      '.packages[] | select(.name == strenv(PKG)) | .track[strenv(TRACK_KEY)].repo // ""' "${MAN}")
    date=$(fetch_release "${repo}" 2>/dev/null \
      | yq -p=json -r '.published_at // ""') || date=""

    url=""
    # A release id has no OSV query shape and no commit: the source GHSA
    # advisory stream carries the flux verdict alone.
    osv=""
    ghr="${repo}"
  fi
  GATE_JUDGE_REASON=""
  care_verdict=""
  flux_verdict=""
  if [[ -n "${moniker}" ]] && gate_bypass; then
    # The bypass log line cites the CVE ids and the feed that listed them.
    care_verdict="bypass"
  elif gate_care_delay "${date}" "${id} (key ${key})"; then
    care_verdict="pass"
  else
    care_verdict="hold"
    GATE_JUDGE_REASON="the candidate is younger than CARE_DELAY and the bypass judges found no cited active CVE"
    gate_log hold kernel_election "${kind:-candidate} ${id}" \
      "HOLD ${PKG} ${key}: ${GATE_JUDGE_REASON} (id ${id})"
    return 3
  fi
  if gate_flux "${osv}" "${ghr}"; then
    flux_verdict="pass"
  else
    rc=$?
    if [[ "${rc}" -eq 3 ]]; then
      flux_verdict="hold"
      GATE_JUDGE_REASON="an open OSV/GHSA advisory on the source or the commit"
      gate_log hold kernel_election "${kind:-candidate} ${id}" \
        "HOLD ${PKG} ${key}: ${GATE_JUDGE_REASON} (id ${id})"
      return 3
    fi
    # rc 1 = the advisory source itself is unreachable: loud pass, logged.
    flux_verdict="unreachable"
  fi
  gate_record "${key}" "${kind}" "${id}" "${url}" "${sha}" "${date}" \
    "${care_verdict}" "${flux_verdict}"
  gate_log election "${kind}" "${id}" \
    "ELECTED ${PKG} ${key}: ${id} (care=${care_verdict} flux=${flux_verdict})"
  return 0
}

#######################################
# Print the releases.json entry to build, as JSON. THE LATEST-PASSING-GATE
# (owner order 2026-11-10): the candidates are probed from the newest
# stable/longterm release downward — each judged by the dry-run of the
# entry's patch set on the real tarball tree; the FIRST candidate whose
# whole set applies is elected. Within a failing series the probe descends
# to the older point releases (the stable git tags), and a series that
# fails whole moves on to the next lower series. The URL-availability probe
# stays as the cheap prefilter (a patch upstream cannot serve is a failed
# download anyway). No series anchor and no fixed version ever select.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#   $3 - moniker
# Returns:
#   0 with the entry on stdout; 2 when no release is eligible yet (nothing
#   passes, nothing is published: a legitimate "nothing to build"); 1 on
#   any other error.
#######################################
select_kernel_release() {
  local pkg="$1" key="$2" moniker="$3"
  local req urls version floor entry series
  req=$(election_require "${pkg}" "${key}")
  local urls=""
  if [[ -n "${req}" ]]; then
    urls=$(patch_urls "${req}" "${pkg}") || return 1
  fi
  floor=$(published_kernel_floor "${pkg}")
  local judged=""
  local pass
  # Two passes: the releases.json stream first (the newest point of every
  # stable/longterm series); when nothing passes, the older point releases
  # of the failing series are walked from the stable git tags (never an
  # -rc, never a downgrade past the published floor).
  for pass in 1 2; do
    while IFS= read -r version; do
      [[ -n "${version}" ]] || continue
      # Never publish an older kernel than the one already out: when the
      # newest releases fail, keep the published deb instead of
      # downgrading.
      if [[ -n "${floor}" && "$(printf '%s\n%s\n' "${floor}" "${version}" \
        | sort -V | head -n1)" != "${floor}" ]]; then
        echo "kernel ${version} is older than the published ${floor}" >&2
        return 2
      fi
      # Nothing to judge (no require-patches) = the newest wins (digests
      # still gate the resolution below).
      if [[ -n "${req}" ]]; then
        patch_urls_available "${urls}" "${version}" || continue
        # Already probed and failed (pass 1) — only NEW candidates
        # (the pass-2 point releases) get judged here.
        [[ "$(grep -c "${version//./\\.} " <<<"${judged}")" -eq 0 ]] || continue
        local jrc=0
        local source
        source="$(releases_entry_source "${version}")"
        [[ -n "${source}" ]] \
          || source="$(release_source_for "${version}")"
        election_dryrun "${pkg}" "${key}" "${version}" "${source}" || jrc=$?
        if [[ "${jrc}" -eq 2 ]]; then
          continue # not ready (checksum unpublished): skip silently
        fi
        if [[ "${jrc}" -ne 0 ]]; then
          judged+="${version} "$'\n'
          continue
        fi
      fi
      entry=$(releases_entry "${version}")
      # A pass-2 point release is not IN releases.json: build the entry the
      # resolution consumes from the version and the source the judge used.
      if [[ -z "${entry}" || "${entry}" == "null" ]]; then
        entry=$(printf '{"moniker": "stable", "version": "%s", "source": "%s"}' \
          "${version}" "${source}")
      fi
      kernel_digests "${pkg}" "${key}" "${version}" \
        "$(yq -p=json '.source // ""' <<<"${entry}")" \
        >/dev/null || continue
      printf '%s\n' "${entry}"
      return
    done < <(election_candidates "${pass}" "${judged}")
  done
  echo "no kernel release passes the dry-run of the patch set of the entry '$1'" >&2
  return 2
}

#######################################
# Print the source URL of a releases.json version.
# Arguments:
#   $1 - kernel version
#######################################
releases_entry_source() {
  curl --retry 3 -fsSL "${KERNEL_ORG_URL:-https://www.kernel.org}/releases.json" \
    | V="$1" yq -p=json -r '.releases[] | select(.version == strenv(V)) | .source // ""'
}

#######################################
# Print the full releases.json entry of a version.
# Arguments:
#   $1 - kernel version
#######################################
releases_entry() {
  curl --retry 3 -fsSL "${KERNEL_ORG_URL:-https://www.kernel.org}/releases.json" \
    | V="$1" yq -p=json -o=json \
      '[.releases[] | select(.version == strenv(V))] | .[0]'
}

#######################################
# Print the tarball source URL of a version absent from releases.json (a
# pass-2 point release walked from the stable tags): the same directory as
# any release of the same major series — kernel.org serves every point in
# the same v<MAJOR>.x archive directory.
# Arguments:
#   $1 - kernel version
#######################################
release_source_for() {
  local dir
  dir=$(curl --retry 3 -fsSL \
    "${KERNEL_ORG_URL:-https://www.kernel.org}/releases.json" \
    | yq -p=json -r '.releases[] | select(.moniker == "stable" or .moniker == "longterm") | .source' \
    | head -n1 | xargs -r dirname)
  [[ -n "${dir}" ]] || return 0
  printf '%s/linux-%s.tar.xz\n' "${dir}" "$1"
}

#######################################
# Election candidates: pass 1 = every stable/longterm version in
# releases.json (newest first); pass 2 = the same list plus the older point
# releases of the series already probed and failed (from the stable git
# tags) — the probe descends inside a series before leaving it.
# Arguments:
#   $1 - pass (1 or 2)
#   $2 - failed versions probed in pass 1 ("7.3.3 \n7.3.2 \n")
#######################################
election_candidates() {
  local pass="$1" judged="$2" all
  all=$(curl --retry 3 -fsSL "${KERNEL_ORG_URL:-https://www.kernel.org}/releases.json" \
    | yq -p=json '.releases[] | select(.moniker == "stable" or .moniker == "longterm")
      | .version' | sort -rV)
  if [[ "${pass}" == 1 ]]; then
    printf '%s\n' "${all}"
    return
  fi
  local version series
  while IFS= read -r version; do
    [[ -n "${version}" ]] || continue
    version="${version// /}"
    printf '%s\n' "${version}"
  done <<<"${all}"
  # Descend inside the series that failed whole (oldest point first here —
  # sort -rV restores the newest-first probe order over the merged list).
  while IFS= read -r version; do
    [[ -n "${version}" ]] || continue
    version="${version// /}"
    [[ "$(grep -c "${version//./\\.} " <<<"${judged}")" -eq 1 ]] || continue
    series_point_releases "${version%.*}"
  done <<<"${judged}" | sort -rV | uniq
}

#######################################
# Print KEY=<version> plus the tarball digests for a kernel.org entry.
# The SHA-256 comes from the sha256sums.asc next to the tarball.  A version
# without a digest line (a release candidate) is not resolved.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#   $3 - moniker
#######################################
resolve_kernel() {
  local pkg="$1" key="$2" moniker="$3" release version source digests
  release=$(select_kernel_release "${pkg}" "${key}" "${moniker}") || return $?
  version=$(yq -p=json '.version // ""' <<<"${release}")
  source=$(yq -p=json '.source // ""' <<<"${release}")
  [[ -n "${version}" && -n "${source}" ]] || return 1
  digests=$(kernel_digests "${pkg}" "${key}" "${version}" "${source}") \
    || return 1
  printf '%s=%s\n' "${key}" "${version}"
  [[ -z "${digests}" ]] || printf '%s\n' "${digests}"
}

#######################################
# Print the kernel.org moniker of a tracked entry, empty when it has none.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#######################################
kernel_moniker() {
  yq ".packages[] | select(.name == \"$1\") | .track.\"$2\".\"kernel-org\" // \"\"" \
    "${MAN}"
}

#######################################
# Fetch a GitHub API path as JSON.
# Arguments:
#   $1 - path below the API root
#######################################
fetch_api() {
  local auth=()
  [[ -n "${GH_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer ${GH_TOKEN}")
  curl --retry 3 -fsSL "${auth[@]}" \
    -H 'Accept: application/vnd.github+json' \
    "${API}/$1"
}

#######################################
# Print KEY=<head commit> for a branch-following entry (the default branch).
# Arguments:
#   $1 - env variable key
#   $2 - owner/name
#######################################
resolve_head() {
  local commit
  commit=$(fetch_api "repos/$2/commits/HEAD" | yq -p=json '.sha // ""') \
    || return 1
  [[ -n "${commit}" ]] || return 1
  printf '%s=%s\n' "$1" "${commit}"
}

#######################################
# Succeed when a tracked entry follows a branch head, not releases.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#######################################
is_head() {
  [[ $(yq ".packages[] | select(.name == \"$1\") | .track.\"$2\".head // false" \
    "${MAN}") == true ]]
}

#######################################
# Print the manifest keys under .track of a package, one per line.
# Arguments:
#   $1 - package name
#######################################
track_keys() {
  yq ".packages[] | select(.name == \"$1\") | (.track // {}) | keys | .[]" \
    "${MAN}"
}

#######################################
# Print the tag of the latest release of one tracked entry.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#######################################
latest_tag() {
  local repo release moniker
  moniker=$(kernel_moniker "$1" "$2")
  if [[ -n "${moniker}" ]]; then
    select_kernel_release "$1" "$2" "${moniker}" | yq -p=json '.version // ""'
    return
  fi
  repo=$(yq ".packages[] | select(.name == \"$1\") | .track.\"$2\".repo // \"\"" \
    "${MAN}")
  [[ -n "${repo}" ]] || return 1
  release=$(fetch_release "${repo}") || return 1
  yq -p=json '.tag_name // ""' <<<"${release}"
}

#######################################
# Print KEY=VALUE lines for one tracked entry: its version and the digests.
# Prints nothing and fails if any part cannot be resolved.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#######################################
resolve_entry() {
  local pkg="$1" key="$2" sel repo strip release tag version value out
  local pair denv name digest moniker
  moniker=$(kernel_moniker "${pkg}" "${key}")
  if [[ -n "${moniker}" ]]; then
    resolve_kernel "${pkg}" "${key}" "${moniker}"
    return
  fi
  sel=".packages[] | select(.name == \"${pkg}\") | .track.\"${key}\""
  repo=$(yq "${sel} | .repo // \"\"" "${MAN}")
  strip=$(yq "${sel} | .\"strip-v\" // false" "${MAN}")
  [[ -n "${repo}" ]] || return 1
  if is_head "${pkg}" "${key}"; then
    resolve_head "${key}" "${repo}"
    return
  fi
  release=$(fetch_release "${repo}") || return 1
  tag=$(yq -p=json '.tag_name // ""' <<<"${release}")
  [[ -n "${tag}" ]] || return 1
  version="${tag#v}"
  value="${tag}"
  [[ "${strip}" == true ]] && value="${version}"
  out="${key}=${value}"
  while IFS= read -r pair; do
    [[ -n "${pair}" ]] || continue
    denv="${pair%%=*}"
    name="${pair#*=}"
    name="${name//\{version\}/${version}}"
    name="${name//\{tag\}/${tag}}"
    digest=$(NAME="${name}" yq -p=json \
      '.assets[] | select(.name == strenv(NAME)) | .digest // ""' \
      <<<"${release}")
    [[ "${digest}" == sha256:* ]] || {
      echo "no sha256 digest for asset ${name} of ${repo} ${tag}" >&2
      return 1
    }
    out+=$'\n'"${denv}=${digest#sha256:}"
  done < <(yq "${sel} | (.digests // {}) | to_entries | .[] \
    | .key + \"=\" + .value" "${MAN}")
  printf '%s\n' "${out}"
}

#######################################
# Merge override lines into the base env: an overridden key is replaced, the
# other keys keep their order.
# Arguments:
#   $1 - base KEY=VALUE lines
#   $2 - override KEY=VALUE lines
#######################################
merge_env() {
  local line key merged=""
  while IFS= read -r line; do
    [[ "${line}" == *=* ]] || continue
    key="${line%%=*}"
    grep -q "^${key}=" <<<"$2" && continue
    merged+="${line}"$'\n'
  done <<<"$1"
  printf '%s' "${merged}"
  [[ -z "$2" ]] || printf '%s\n' "$2"
}

#######################################
# Resolve the tracked upstream versions of one package.
# Globals:
#   PKG
#   BASE_ENV
#   STRICT
#   GITHUB_OUTPUT
# Outputs:
#   Step output env: the package env with the resolved variables merged in.
# Returns:
#   Exits 1 when an entry is unresolved and STRICT is true.
#######################################
resolve() {
  local key lines overrides="" failed=0 skip=false rc
  while IFS= read -r key; do
    [[ -n "${key}" ]] || continue
    rc=0
    lines=$(resolve_entry "${PKG}" "${key}") || rc=$?
    if [[ "${rc}" -eq 0 ]]; then
      gate_judge "${key}" "${lines}" || rc=$?
      [[ "${rc}" -eq 3 ]] && rc=3
    fi
    if [[ "${rc}" -eq 0 ]]; then
      overrides+="${lines}"$'\n'
      echo "upstream ${PKG}: ${lines//$'\n'/ }" >&2
    elif [[ "${rc}" -eq 2 ]]; then
      skip=true
      echo "::notice::${PKG}: no upstream release is eligible for ${key} yet;" \
        "nothing to build, the last-good deb is kept" >&2
    elif [[ "${rc}" -eq 3 ]]; then
      # Security hold (care delay or open OSV/GHSA advisory): keep the
      # last-good deb and surface the hold in a deduped issue.
      skip=true
      echo "::error::${PKG}: security HOLD on ${key} (${GATE_JUDGE_REASON:-gates});" \
        "the last-good deb is kept, the election selection.log carries the reason" >&2
      gate_hold_issue "Security hold: ${PKG} (${key})" \
        "**${PKG}** ${key}: the security gates held the candidate. Reason: ${GATE_JUDGE_REASON:-unknown}. Run: ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
    else
      failed=1
      echo "::warning::${PKG}: cannot resolve the latest upstream for ${key};" \
        "it stays unset, so the build fails and the last-good deb is kept" >&2
    fi
  done < <(track_keys "${PKG}")
  [[ "${failed}" -eq 0 || "${STRICT:-false}" != true ]] || exit 1
  {
    echo "env<<TRACK_ENV_EOF"
    merge_env "${BASE_ENV:-}" "${overrides%$'\n'}"
    echo "TRACK_ENV_EOF"
    echo "skip=${skip}"
    [[ ! -s "${ELECTION_DIR}/election-${PKG}.json" ]] \
      || echo "election=${ELECTION_DIR}/election-${PKG}.json"
  } >>"${GITHUB_OUTPUT:-/dev/stdout}"
}

#######################################
# Report tracked entries whose latest release is not published.
# Globals:
#   ARCH
#   PAGES_URL
#   GITHUB_REPOSITORY
#   MAN
#   STRICT
# Outputs:
#   One line per tracked entry on stdout.
# Returns:
#   Exits 1 when an entry is behind and STRICT is true.
#######################################
check() {
  local arch="${ARCH:-amd64}" base published pkg key tag version behind=0
  if [[ -n "${PAGES_URL:-}" ]]; then
    base="${PAGES_URL%/}"
  else
    base="https://${GITHUB_REPOSITORY%%/*}.github.io/${GITHUB_REPOSITORY#*/}"
    base="${base,,}"
  fi
  published=$(curl --retry 3 -fsSL "${base}/debs-cache/hashes.json")
  while IFS= read -r pkg; do
    [[ -n "${pkg}" ]] || continue
    while IFS= read -r key; do
      [[ -n "${key}" ]] || continue
      is_head "${pkg}" "${key}" && continue
      tag=$(latest_tag "${pkg}" "${key}") || {
        echo "${pkg} ${key}: latest release unknown"
        behind=1
        continue
      }
      version="${tag#v}"
      # A kernel deb is versioned <kernel>-<revision>, any other <version>.
      sep='_'
      [[ -z "$(kernel_moniker "${pkg}" "${key}")" ]] || sep='-'
      # Every published key of the family (each expanded variant on this
      # arch) must ship the elected release: one missing variant build is
      # the same "latest release did not build" signal as a family-wide one.
      local count=0 good=0 missing=""
      local ck
      while IFS= read -r ck; do
        [[ -n "${ck}" ]] || continue
        count=$((count + 1))
        if PK="${ck}" V="_${version}${sep}" yq -p=json -e \
          '.[strenv(PK)].debs[] | select(contains(strenv(V)))' \
          <<<"${published}" >/dev/null 2>&1; then
          good=$((good + 1))
        else
          missing+="${ck} "
        fi
      done < <(published_keys "${pkg}" "${arch}")
      ((count)) || continue
      if ((good == count)); then
        echo "${pkg} ${key}: ${tag} is published"
      else
        echo "${pkg} ${key}: upstream ${tag} is not published (${missing% })"
        behind=1
      fi
    done < <(track_keys "${pkg}")
  done < <(yq '.packages[] | select(.track) | .name' "${MAN}")
  [[ "${behind}" -eq 0 || "${STRICT:-false}" != true ]] || exit 1
}

case "${1:-}" in
  resolve) resolve ;;
  check) check ;;
  *)
    echo "Usage: $0 resolve|check" >&2
    exit 2
    ;;
esac
