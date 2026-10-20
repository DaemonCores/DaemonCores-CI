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
# `require-patches: kernel/patches.yml` on the entry (path relative to the
# package manifest) restricts the choice to the newest stable or longterm
# release whose series has every patch URL of that manifest upstream, so a
# kernel that a patch source (CachyOS, ROCKNIX) has not caught up with is
# skipped instead of built and failed. It never resolves a kernel older than the
# highest one already published (debs-cache/hashes.json): the build then fails
# and the last-good deb is kept, instead of a downgrade.
#
# {version} is the tag without its leading "v", {tag} is the tag as published.
# The SHA-256 of each asset comes from the GitHub release API (`digest`), so the
# build keeps verifying its downloads without a checksum committed per version.
#
# Modes (first argument):
#   resolve  Resolve the tracked variables of PKG and write the manifest env
#            with them merged in to the step output `env`.  An entry that cannot
#            be resolved is left unset: the build then fails and the last-good
#            published deb is kept.  With STRICT=true the step fails instead.
#   check    Report, for every tracked package, the entries whose latest
#            release is not what the published APT repository ships.  With
#            STRICT=true the exit status is 1 when one is behind.
#
# Environment:
#   MAN       manifest path (default workflows/bootc-debs-builder/packages.yml)
#   PKG       package name (resolve)
#   BASE_ENV  KEY=VALUE lines of the package env (resolve)
#   ARCH      architecture whose published debs are compared (check)
#   STRICT    true to turn unresolved / behind into a failure
#   GH_TOKEN  optional token, only to lift the API rate limit
#   KERNEL_ORG_URL  root of kernel.org (default https://www.kernel.org)
#   GITHUB_API_URL, PAGES_URL, GITHUB_REPOSITORY, GITHUB_OUTPUT as in Actions.
set -euo pipefail

MAN="${MAN:-workflows/bootc-debs-builder/packages.yml}"
API="${GITHUB_API_URL:-https://api.github.com}"

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
# Succeed when every patch URL of a manifest exists for one kernel series.
# Arguments:
#   $1 - patch manifest path
#   $2 - kernel version, e.g. 7.2.11
#######################################
patches_available() {
  local url series="${2%.*}"
  [[ "$2" == *.*.* ]] || series="$2"
  while IFS= read -r url; do
    [[ -n "${url}" ]] || continue
    url="${url//\{series\}/${series}}"
    url="${url//\{version\}/$2}"
    curl --retry 3 -fsSL -o /dev/null "${url}" || {
      echo "patch not available for ${2}: ${url}" >&2
      return 1
    }
  done < <(yq '.patches[].url' "$1")
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
  PK="$1-${arch}" yq -p=json '.[strenv(PK)].debs[]' <<<"${published}" 2>/dev/null \
    | grep -oE '_[0-9]+\.[0-9]+(\.[0-9]+)?-' | tr -d '_-' | sort -rV | head -n1 \
    || true
}

#######################################
# Print the releases.json entry to build, as JSON.  Without `require-patches`
# on the entry it is the first release of the moniker.  With it (a patch
# manifest path relative to the package manifest), it is the newest stable or
# longterm release whose series has every patch of the manifest upstream, so a
# mainline release that CachyOS or another patch source has not caught up with
# yet is skipped instead of failing the build.
# Arguments:
#   $1 - package name
#   $2 - env variable key
#   $3 - moniker
#######################################
select_kernel_release() {
  local req manifest version floor
  req=$(yq ".packages[] | select(.name == \"$1\") | .track.\"$2\".\"require-patches\" // \"\"" \
    "${MAN}")
  if [[ -z "${req}" ]]; then
    fetch_kernel_release "$3"
    return
  fi
  manifest="$(dirname "${MAN}")/${req}"
  floor=$(published_kernel_floor "$1")
  while IFS= read -r version; do
    [[ -n "${version}" ]] || continue
    # Never publish an older kernel than the one already out: when the newest
    # releases lack a patch, keep the published deb instead of downgrading.
    if [[ -n "${floor}" && "$(printf '%s\n%s\n' "${floor}" "${version}" \
      | sort -V | head -n1)" != "${floor}" ]]; then
      echo "kernel ${version} is older than the published ${floor}" >&2
      return 1
    fi
    if patches_available "${manifest}" "${version}"; then
      curl --retry 3 -fsSL "${KERNEL_ORG_URL:-https://www.kernel.org}/releases.json" \
        | V="${version}" yq -p=json -o=json \
          '[.releases[] | select(.version == strenv(V))] | .[0]'
      return
    fi
  done < <(curl --retry 3 -fsSL "${KERNEL_ORG_URL:-https://www.kernel.org}/releases.json" \
    | yq -p=json '.releases[] | select(.moniker == "stable" or .moniker == "longterm")
      | .version' | sort -rV)
  echo "no kernel release has every patch of ${manifest}" >&2
  return 1
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
  local pkg="$1" key="$2" moniker="$3" sel release version source out sums
  local pair denv name digest
  sel=".packages[] | select(.name == \"${pkg}\") | .track.\"${key}\""
  release=$(select_kernel_release "${pkg}" "${key}" "${moniker}") || return 1
  version=$(yq -p=json '.version // ""' <<<"${release}")
  source=$(yq -p=json '.source // ""' <<<"${release}")
  [[ -n "${version}" && -n "${source}" ]] || return 1
  out="${key}=${version}"
  sums=""
  while IFS= read -r pair; do
    [[ -n "${pair}" ]] || continue
    denv="${pair%%=*}"
    name="${pair#*=}"
    name="${name//\{version\}/${version}}"
    [[ -n "${sums}" ]] \
      || sums=$(curl --retry 3 -fsSL "${source%/*}/sha256sums.asc") || return 1
    digest=$(awk -v n="${name}" '$2 == n { print $1 }' <<<"${sums}")
    [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || {
      echo "no sha256 for ${name} in ${source%/*}/sha256sums.asc" >&2
      return 1
    }
    out+=$'\n'"${denv}=${digest}"
  done < <(yq "${sel} | (.digests // {}) | to_entries | .[] \
    | .key + \"=\" + .value" "${MAN}")
  printf '%s\n' "${out}"
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
  local key lines overrides="" failed=0
  while IFS= read -r key; do
    [[ -n "${key}" ]] || continue
    if lines=$(resolve_entry "${PKG}" "${key}"); then
      overrides+="${lines}"$'\n'
      echo "upstream ${PKG}: ${lines//$'\n'/ }" >&2
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
      if PK="${pkg}-${arch}" V="_${version}${sep}" yq -p=json -e \
        '.[strenv(PK)].debs[] | select(contains(strenv(V)))' \
        <<<"${published}" >/dev/null 2>&1; then
        echo "${pkg} ${key}: ${tag} is published"
      else
        echo "${pkg} ${key}: upstream ${tag} is not published"
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
