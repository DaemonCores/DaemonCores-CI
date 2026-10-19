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
  local auth=()
  [[ -n "${GH_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer ${GH_TOKEN}")
  curl --retry 3 -fsSL "${auth[@]}" \
    -H 'Accept: application/vnd.github+json' \
    "${API}/repos/$1/releases/latest"
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
  local repo release
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
  local pair denv name digest
  sel=".packages[] | select(.name == \"${pkg}\") | .track.\"${key}\""
  repo=$(yq "${sel} | .repo // \"\"" "${MAN}")
  strip=$(yq "${sel} | .\"strip-v\" // false" "${MAN}")
  [[ -n "${repo}" ]] || return 1
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
      tag=$(latest_tag "${pkg}" "${key}") || {
        echo "${pkg} ${key}: latest release unknown"
        behind=1
        continue
      }
      version="${tag#v}"
      if PK="${pkg}-${arch}" V="_${version}_" yq -p=json -e \
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
