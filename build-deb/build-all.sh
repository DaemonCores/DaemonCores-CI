#!/bin/bash
#
# build-all.sh - build every package of one architecture in a single job.
#
# Replaces one job per package: the packages run one after the other in the
# order of plan.py, each in its own subshell so no variable leaks from one to
# the next. Every step reuses the script the per-package job ran (pkg-def.sh,
# track.sh, build-deb.sh), so the content hashes are the ones the per-package
# jobs computed. A package whose hash matches the published one is restored
# from the APT repository without installing its dependencies or a compiler
# cache; only a package that really builds pays for them (the `prepare` mode,
# run by build-deb.sh through PRE_BUILD_CMD).
#
# Usage:
#   build-all.sh <expanded-manifest> <source-manifest>
#   build-all.sh prepare                      (internal, see PRE_BUILD_CMD)
#
# Environment:
#   ARCH            target architecture (amd64 or arm64)
#   SIGNING_ACTIVE  value of MODULE_SIGNING_ACTIVE for signing packages
#   GITHUB_OUTPUT   receives any_built=true|false
#   GITHUB_ENV is not touched; compiler cache variables are exported per package.
#   SECRETVAL_<NAME>, GITHUB_TOKEN, GITHUB_REPOSITORY as in build-one-deb.yml.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_ROOT="$(cd "${HERE}/.." && pwd)"
# Overridable so the unit tests can run in a sandbox.
DEBS_DIR="${DEBS_DIR:-/debs}"
BUILD_DIR="${BUILD_DIR:-/build}"

#######################################
# Read a GITHUB_OUTPUT style file (KEY=VALUE and KEY<<DELIM blocks) into the
# associative array OUT.
# Arguments:
#   $1 - file to read
#######################################
read_outputs() {
  local line key delim value
  OUT=()
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)\<\<(.+)$ ]]; then
      key="${BASH_REMATCH[1]}"
      delim="${BASH_REMATCH[2]}"
      value=""
      while IFS= read -r line; do
        [[ "${line}" == "${delim}" ]] && break
        value+="${line}"$'\n'
      done
      OUT["${key}"]="${value%$'\n'}"
    elif [[ "${line}" == *=* ]]; then
      OUT["${line%%=*}"]="${line#*=}"
    fi
  done <"$1"
}

#######################################
# Print the deb files of a package's dependency closure, one path per line.
# Arguments:
#   $1 - expanded manifest
#   $2 - space-separated direct dependencies
# Returns:
#   1 when a dependency has no deb in /debs.
#######################################
dep_debs() {
  local man="$1" dep glob f found
  local -A seen=()
  local -a queue=()
  read -ra queue <<<"$2"
  while ((${#queue[@]})); do
    dep="${queue[0]}"
    queue=("${queue[@]:1}")
    [[ -z "${seen[${dep}]:-}" ]] || continue
    seen["${dep}"]=1
    glob=$(yq ".packages[] | select(.name == \"${dep}\") \
      | .\"deb-glob\" // \"${dep}_*\"" "${man}")
    found=0
    for f in "${DEBS_DIR}"/${glob}.deb; do
      [[ -f "${f}" ]] || continue
      printf '%s\n' "${f}"
      found=1
    done
    if ((!found)); then
      echo "ERROR: no deb for dependency ${dep} (${glob}.deb) in ${DEBS_DIR}" >&2
      return 1
    fi
    while IFS= read -r sub; do
      [[ -n "${sub}" ]] && queue+=("${sub}")
    done < <(yq ".packages[] | select(.name == \"${dep}\") \
      | (.deps // []) | .[]" "${man}")
  done
}

#######################################
# Install what a build needs: the dependency debs and, for a kernel-compiling
# package, the compiler cache. Runs only when the package is really built.
# Environment:
#   DEP_DEBS  newline-separated deb paths
#   SIGNS     yes for a kernel-compiling package
#######################################
prepare() {
  apt-get update -qq
  if [[ "${SIGNS:-no}" == yes ]]; then
    apt-get install -y -qq --no-install-recommends ccache
  fi
  if [[ -n "${DEP_DEBS:-}" ]]; then
    local -a debs
    mapfile -t debs <<<"${DEP_DEBS}"
    apt-get install -y "${debs[@]}"
  fi
}

#######################################
# Open or update the "Broken build" issue of a package, or close it.
# Arguments:
#   $1 - package
#   $2 - open | close
#######################################
issue() {
  local pkg="$1" action="$2" title body api num
  title="⚠️ Broken build: ${pkg} (last valid .deb reused)"
  api="${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}"
  local -a auth=(-H "Authorization: Bearer ${GITHUB_TOKEN}"
    -H "Accept: application/vnd.github+json")
  num=$(printf '%s' "${OPEN_ISSUES}" \
    | jq -r --arg t "${title}" 'map(select(.title == $t)) | .[0].number // empty')
  if [[ "${action}" == open ]]; then
    body="The \`${pkg}\` build failed (compilation or post-patch content check). "
    body+="The last published .deb was reused to avoid breaking production. "
    body+="The cache hash is left unchanged so the fix will be retried on the next run. "
    body+="Run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
    if [[ -n "${num}" ]]; then
      curl -fsSL "${auth[@]}" -X POST "${api}/issues/${num}/comments" \
        -d "$(jq -n --arg b "${body}" '{body: $b}')" >/dev/null
    else
      curl -fsSL "${auth[@]}" -X POST "${api}/issues" \
        -d "$(jq -n --arg t "${title}" --arg b "${body}" '{title: $t, body: $b}')" >/dev/null
    fi
    echo "::warning::${pkg} build broken - issue opened/updated, last valid .deb reused"
  elif [[ -n "${num}" ]]; then
    body="Build of \`${pkg}\` succeeded in run ${GITHUB_RUN_ID}. Closing."
    curl -fsSL "${auth[@]}" -X POST "${api}/issues/${num}/comments" \
      -d "$(jq -n --arg b "${body}" '{body: $b}')" >/dev/null
    curl -fsSL "${auth[@]}" -X PATCH "${api}/issues/${num}" \
      -d '{"state":"closed"}' >/dev/null
    echo "Closed issue #${num} for ${pkg} (build recovered)"
  fi
}

#######################################
# Build or reuse one package. Runs in a subshell.
# Arguments:
#   $1 - package
#   $2 - expanded manifest
#   $3 - source manifest
# Outputs:
#   Writes "<built|reused|kept|failed>" to the file named by RESULT_FILE.
#######################################
one_package() {
  local pkg="$1" man="$2" srcman="$3" tmp deps depdebs deps_hash extra
  tmp=$(mktemp -d)
  # The caller runs this function as `( ... ) || failed`, which disables
  # errexit inside it: every step that can fail is checked explicitly.
  if ! GITHUB_OUTPUT="${tmp}/def" PKG="${pkg}" MAN="${man}" \
    bash "${CI_ROOT}/pkg-def/pkg-def.sh"; then
    echo failed >"${RESULT_FILE}"
    return 0
  fi
  declare -A OUT
  read_outputs "${tmp}/def"
  local upstream="${OUT[upstream]}" deb_glob="${OUT[deb_glob]}" sources="${OUT[sources]}"
  local signs="${OUT[signs]}" env_lines="${OUT[env]}" build="${OUT[build]}"
  deps="${OUT[deps]}"

  # Identity of the dependency debs this build consumes (the hash of the files,
  # not their paths): a dep rebuilt in this run changes it.
  deps_hash=""
  depdebs=""
  if [[ -n "${deps}" ]]; then
    if ! depdebs=$(dep_debs "${man}" "${deps}"); then
      echo failed >"${RESULT_FILE}"
      return 0
    fi
    # shellcheck disable=SC2086
    deps_hash=$(printf '%s\n' ${depdebs} | xargs sha256sum | awk '{ print $1 }' \
      | sort | sha256sum | cut -d' ' -f1)
  fi

  if ! GITHUB_OUTPUT="${tmp}/track" PKG="${pkg}" MAN="${man}" SOURCE_MAN="${srcman}" \
    BASE_ENV="${env_lines}" STRICT=false GH_TOKEN="${GITHUB_TOKEN:-}" \
    bash "${CI_ROOT}/upstream-track/track.sh" resolve; then
    echo failed >"${RESULT_FILE}"
    return 0
  fi
  read_outputs "${tmp}/track"
  env_lines="${OUT[env]}"

  extra=""
  [[ "${signs}" != yes ]] || extra+="MODULE_SIGNING_ACTIVE=${SIGNING_ACTIVE:-no}"$'\n'
  [[ -z "${deps_hash}" ]] || extra+="DEPS_HASH=${deps_hash}"$'\n'

  if [[ "${signs}" == yes ]]; then
    export CCACHE_DIR=/ccache CCACHE_BASEDIR=/tmp CCACHE_COMPILERCHECK=content \
      CCACHE_MAXSIZE=2G
    mkdir -p "${CCACHE_DIR}"
    PATH="/usr/lib/ccache:${PATH}"
  fi

  GITHUB_OUTPUT="${tmp}/build" PACKAGE="${pkg}" UPSTREAM="${upstream}" \
    SOURCES="${sources}" EXTRA_ENV="${env_lines}" EXTRA_ENV_CI="${extra%$'\n'}" \
    DEB_GLOB="${deb_glob}" BUILD="${build}" \
    DEP_DEBS="${depdebs}" SIGNS="${signs}" \
    PRE_BUILD_CMD="bash ${HERE}/build-all.sh prepare" \
    bash "${HERE}/build-deb.sh" || {
    echo failed >"${RESULT_FILE}"
    return 0
  }
  read_outputs "${tmp}/build"
  if [[ "${OUT[build_failed]:-}" == true ]]; then
    echo kept >"${RESULT_FILE}"
  elif [[ "${OUT[skipped]:-}" == true ]]; then
    echo reused >"${RESULT_FILE}"
  else
    echo built >"${RESULT_FILE}"
  fi
  rm -rf "${tmp}"
}

#######################################
# Build every package in plan order; fail at the end when one failed.
# Arguments:
#   $1 - expanded manifest
#   $2 - source manifest
#######################################
main() {
  local man="$1" srcman="$2" pkg result any_built=false failed=0
  mkdir -p "${DEBS_DIR}"
  OPEN_ISSUES=$(curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/issues?state=open&per_page=100" \
    2>/dev/null || echo '[]')
  RESULT_FILE=$(mktemp)
  export RESULT_FILE
  local -a order
  mapfile -t order < <(yq -o=json '.' "${man}" | python3 "${HERE}/plan.py" /dev/stdin)
  for pkg in "${order[@]}"; do
    echo "::group::${pkg}"
    : >"${RESULT_FILE}"
    ( one_package "${pkg}" "${man}" "${srcman}" ) || echo failed >"${RESULT_FILE}"
    result=$(<"${RESULT_FILE}")
    echo "::endgroup::"
    echo "result ${pkg}: ${result:-failed}"
    case "${result}" in
      built)
        any_built=true
        issue "${pkg}" close \
          || echo "::warning::${pkg}: could not update its issue (GitHub API)"
        # The build tree and the kernel sources are not needed by the next package.
        rm -rf "${BUILD_DIR:?}"/* "${TMPDIR:-/tmp}"/linux-*
        ;;
      reused) ;;
      kept)
        issue "${pkg}" open \
          || echo "::warning::${pkg}: could not open its issue (GitHub API)"
        failed=1
        ;;
      *)
        echo "::error::${pkg} failed and has no prior deb to fall back to"
        failed=1
        ;;
    esac
  done
  echo "any_built=${any_built}" >>"${GITHUB_OUTPUT:-/dev/null}"
  return "${failed}"
}

if [[ "${1:-}" == prepare ]]; then
  prepare
else
  main "${1:?expanded manifest}" "${2:?source manifest}"
fi
