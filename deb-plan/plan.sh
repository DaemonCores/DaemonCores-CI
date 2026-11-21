#!/bin/bash
#
# plan.sh - decide, BEFORE any build job starts, which packages have nothing to
# build. A package is skipped (no job, no runner, no download) when its content
# hash, recomputed here with the very formula of build-deb.sh, equals the hash
# published in debs-cache/hashes.json, or when the upstream election holds the
# candidate and a last-good build is published. Everything else is built: a
# changed package, a dependent of a rebuilt package, and a package that has
# never been published (its first build initialises it).
#
# The memory is the published hashes.json itself: no bot commit, no extra state.
# The skipped packages carry no .meta: the publish step carries them over.
#
# Consumed environment:
#   MAN             expanded manifest (yaml)
#   SOURCE_MAN      manifest it was expanded from (default: MAN)
#   ARCH            target architecture
#   WAVES           JSON {"wave0": [...], ..., "waveN": [...]} from setup
#   SIGNING_ACTIVE  forwarded module-signing flag
#   GH_TOKEN        token for the GitHub API (rate limit only)
#   GITHUB_OUTPUT   receives wave0..waveN (filtered) and skipped
#   PAGES_URL, GITHUB_REPOSITORY   see content-hash.sh
set -euo pipefail
export ARCH MAN

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_ROOT="$(cd "${HERE}/.." && pwd)"
# shellcheck source=build-deb/content-hash.sh
. "${CI_ROOT}/build-deb/content-hash.sh"

#######################################
# Print the value of a plain KEY=VALUE line or a KEY<<EOF block of a step
# output file.
# Arguments:
#   $1 - output file
#   $2 - key
#######################################
out_value() {
  awk -v k="$2" '
    inblock { if ($0 == eof) { exit } ; print; next }
    index($0, k "<<") == 1 { eof = substr($0, length(k) + 3); inblock = 1; next }
    index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }
  ' "$1"
}

#######################################
# Succeed when every deb the published entry lists is actually served: a skip
# keeps the published build, so it must exist.
# Arguments:
#   $1 - hashes.json content
#   $2 - cache key
# Outputs:
#   The first unserved file name on stdout.
#######################################
served() {
  local file base
  base=$(pages_base)
  while IFS= read -r file; do
    [[ -n "${file}" ]] || continue
    curl -fsSIL --retry 3 --retry-delay 3 --retry-all-errors \
      "${base}/debs-cache/${file}" >/dev/null 2>&1 || {
      echo "${file}"
      return 1
    }
  done < <(prior_field "$1" "$2" '.debs // [] | .[]')
}

#######################################
# Decide one package. Prints "build <why>" or "skip <why>" on stdout.
# Arguments:
#   $1 - package name
#   $2 - hashes.json content
#   $3 - space separated names of the packages that will be built
#######################################
decide() {
  local pkg="$1" prior="$2" building=" $3 "
  local key="${pkg}-${ARCH}" work def trk dep deps prior_hash deps_hash
  work=$(mktemp -d)
  def="${work}/def"
  trk="${work}/trk"
  : >"${def}"
  : >"${trk}"
  if ! PKG="${pkg}" GITHUB_OUTPUT="${def}" \
    bash "${CI_ROOT}/pkg-def/pkg-def.sh" >/dev/null 2>"${work}/def.err"; then
    echo "build the package definition cannot be read"
    return 0
  fi
  deps=$(out_value "${def}" deps)
  for dep in ${deps}; do
    if [[ "${building}" == *" ${dep} "* ]]; then
      echo "build its dependency ${dep} is rebuilt"
      return 0
    fi
  done
  prior_hash=$(prior_field "${prior}" "${key}" '.hash // empty')
  deps_hash=""
  if [[ -n "${deps}" ]]; then
    deps_hash=$(prior_field "${prior}" "${key}" '.deps_hash // empty')
    if [[ -z "${deps_hash}" ]]; then
      echo "build the published entry has no deps_hash yet"
      return 0
    fi
  fi
  local source_man="${SOURCE_MAN:-${MAN}}"
  if ! PKG="${pkg}" SOURCE_MAN="${source_man}" \
    BASE_ENV="$(out_value "${def}" env)" STRICT=false \
    GITHUB_OUTPUT="${trk}" ELECTION_DIR="${work}/election" \
    bash "${CI_ROOT}/upstream-track/track.sh" resolve >/dev/null 2>"${work}/trk.err"; then
    echo "build the upstream resolution failed (the build job reports why)"
    return 0
  fi
  local missing
  if [[ "$(out_value "${trk}" skip)" == true ]]; then
    if [[ -n "${prior_hash}" ]] && missing=$(served "${prior}" "${key}"); then
      echo "skip the upstream election holds the candidate; the published build stays"
    elif [[ -n "${prior_hash}" ]]; then
      echo "build the election holds but ${missing} is not served"
    else
      echo "build nothing is published and the election holds: the job reports it"
    fi
    return 0
  fi
  local hash ci_env="" build_raw
  # BUILD must reach the hash byte for byte as the build job receives it: a step
  # output heredoc loses exactly the one newline before its delimiter, while a
  # command substitution would strip every trailing newline and change the hash
  # of every package whose build script ends with one (all of them).
  build_raw=$(out_value "${def}" build; printf x)
  build_raw="${build_raw%x}"
  build_raw="${build_raw%$'\n'}"
  if [[ "$(out_value "${def}" signs)" == yes ]]; then
    ci_env="MODULE_SIGNING_ACTIVE=${SIGNING_ACTIVE:-}"
  fi
  if [[ -n "${deps_hash}" ]]; then
    ci_env+="${ci_env:+$'\n'}DEPS_HASH=${deps_hash}"
  fi
  hash=$(
    PACKAGE="${pkg}" \
      SOURCES="$(out_value "${def}" sources)" \
      UPSTREAM="$(out_value "${def}" upstream)" \
      BUILD="${build_raw}" \
      EXTRA_ENV="$(out_value "${trk}" env)" EXTRA_ENV_CI="${ci_env}"
    export PACKAGE ARCH SOURCES UPSTREAM BUILD EXTRA_ENV EXTRA_ENV_CI
    merge_extra_env
    content_hash 2>"${work}/hash.err"
  ) || {
    echo "build the content hash cannot be computed"
    return 0
  }
  if [[ -z "${prior_hash}" ]]; then
    echo "build never published: first build"
  elif [[ "${prior_hash,,}" == "${hash,,}" ]]; then
    if missing=$(served "${prior}" "${key}"); then
      echo "skip unchanged (${hash:0:12})"
    else
      echo "build unchanged but the published ${missing} is not served"
    fi
  else
    echo "build changed (${prior_hash:0:12} -> ${hash:0:12})"
  fi
}

main() {
  local prior names pkg verdict building="" skipped=() kept w
  : "${ARCH:?ARCH is required}" "${MAN:?MAN is required}" "${WAVES:?WAVES is required}"
  prior=$(fetch_prior_hashes)
  while IFS= read -r w; do
    names=$(jq -r --arg w "${w}" '.[$w] // [] | .[]' <<<"${WAVES}")
    kept=()
    for pkg in ${names}; do
      verdict=$(decide "${pkg}" "${prior}" "${building}")
      echo "plan ${pkg}/${ARCH}: ${verdict}" >&2
      if [[ "${verdict}" == skip* ]]; then
        skipped+=("${pkg}")
      else
        kept+=("${pkg}")
        building+=" ${pkg}"
      fi
    done
    printf '%s=%s\n' "${w}" \
      "$(printf '%s\n' "${kept[@]:-}" | jq -R . | jq -sc 'map(select(. != ""))')" \
      >>"${GITHUB_OUTPUT:-/dev/stdout}"
  done < <(jq -r 'keys[]' <<<"${WAVES}" | sort)
  printf 'skipped=%s\n' \
    "$(printf '%s\n' "${skipped[@]:-}" | jq -R . | jq -sc 'map(select(. != ""))')" \
    >>"${GITHUB_OUTPUT:-/dev/stdout}"
}

main "$@"
