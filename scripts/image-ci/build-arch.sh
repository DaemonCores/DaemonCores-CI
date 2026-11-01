#!/usr/bin/env bash
# Build, check, test, scan, push and sign every image of ONE architecture in a
# single job, on one shared local layer store.
#
# Usage: build-arch.sh [PHASE...]
#   no argument runs the whole pipeline: hash build lint test wave2 lint test
#   scan push sign (wave 2 = the images derived from another image of this
#   run, built once their parent passed). PHASE is one of
#   hash build wave2 lint test scan push sign, run in the order given on a
#   fresh state; each phase only acts on the images whose recorded state it
#   consumes.
#
# Every image has a state in a TSV file; a failure is recorded and the loop
# goes on with the next image. States:
#   pending  skipped-unchanged  built  linted  ok  pushed   (progress)
#   build-failed lint-failed test-failed blocked push-failed (failures)
# The report (results table, serial logs of failed images) is written by an
# EXIT trap; the exit status is 1 when any image failed or the signature did.
#
# Input (environment):
#   ARCH               target architecture (amd64, arm64)
#   ENTRIES            JSON array of {file, tag, kernel_suffix, twin_suffix}
#                      in build order (Containerfiles by name, -4k right
#                      after its parent)
#   GLOBAL_HASH        hash over every Containerfile (discover job)
#   GITHUB_REPOSITORY  owner/repo, derives the image name and the debs cache
#   IMAGE              image name without tag (default ghcr.io/<repo>, lower)
#   GITHUB_REF, GITHUB_EVENT_NAME, FORCE_NO_CACHE   push and refresh policy
#   BOOTC_GPG_SHA256, APT_URL, COSIGN_PUBLIC_KEY    build inputs
#   COSIGN_PRIVATE_KEY, COSIGN_PASSWORD             read by sign.sh
#   TESTS_FILE, MEM, VCPUS    boot test settings (passed to image-test.sh)
#   BOOT_TEST_TIMEOUT  per image boot test limit, timeout(1) syntax
#                      (default 2700)
#   BOOT_TEST_SKIP_TAGS  tags with no boot test (default iso: no SSH server)
#   RAM_GATE_TAG_PREFIX, RAM_GATE_MB   idle RAM target (default 15 MiB) of the
#                      tags starting with the prefix; the default prefix is
#                      empty: every tag is measured and reported (the figure
#                      only blocks with RAM_IDLE_ENFORCE=1); 0 elsewhere
#   TRIVY_SEVERITY     severities reported by the scan (default CRITICAL,HIGH)
#   SUDO               command prefix for privileged tools (default sudo)
#   RUNNER_TEMP, GITHUB_STEP_SUMMARY   Actions scratch dir and summary file
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CI_ROOT=$(cd "${HERE}/../.." && pwd)
# shellcheck source=scripts/image-ci/hash.sh
. "${HERE}/hash.sh"

FAILURE_STATES="build-failed lint-failed test-failed blocked push-failed"
SIGN_FAILED=no
PREPARED=no
PREPARE_FAILED=no

read -r -a SUDO_CMD <<<"${SUDO-sudo}"

podman_() {
  "${SUDO_CMD[@]}" podman "$@"
}

group() { echo "::group::$*"; }
endgroup() { echo "::endgroup::"; }

is_main() { [[ "${GITHUB_REF:-}" == "refs/heads/main" ]]; }
is_refresh() {
  [[ "${GITHUB_EVENT_NAME:-}" == "schedule" || "${FORCE_NO_CACHE:-}" == "true" ]]
}

# ---------------------------------------------------------------- state ----
# Columns: tag file kernel_suffix kind nocache parent hash state detail.
# An empty value is stored as "-" (tab is a whitespace separator for read).
col_file=2 col_suffix=3 col_kind=4 col_nocache=5
col_parent=6 col_hash=7 col_state=8 col_detail=9

dash() { if [[ -z "$1" ]]; then echo "-"; else echo "$1"; fi; }
undash() { if [[ "$1" == "-" ]]; then echo ""; else echo "$1"; fi; }

row_get() {
  ROW_TAG="$1" ROW_COL="$2" awk -F'\t' \
    '$1 == ENVIRON["ROW_TAG"] { print $(ENVIRON["ROW_COL"] + 0) }' "${STATE}"
}

row_set() {
  local tmp="${STATE}.tmp"
  ROW_TAG="$1" ROW_COL="$2" ROW_VAL="$3" awk -F'\t' -v OFS='\t' '
    $1 == ENVIRON["ROW_TAG"] { $(ENVIRON["ROW_COL"] + 0) = ENVIRON["ROW_VAL"] }
    { print }' "${STATE}" >"${tmp}"
  mv "${tmp}" "${STATE}"
}

set_state() {
  local detail="${3:-}"
  row_set "$1" "${col_state}" "$2"
  row_set "$1" "${col_detail}" "$(dash "${detail//$'\t'/ }")"
  echo "image ${1}: ${2}${detail:+ (${detail})}"
}

all_tags() { awk -F'\t' '{ print $1 }' "${STATE}"; }

tags_in_state() {
  STATE_WANTED="$1" awk -F'\t' \
    '$8 == ENVIRON["STATE_WANTED"] { print $1 }' "${STATE}"
}

is_derived() { [[ "$(row_get "$1" "${col_parent}")" != "-" ]]; }
ref_of() { echo "${IMAGE}:${1}_${ARCH}"; }

# The image the Containerfile starts from, when it is an image of this very
# repository: its tag without the architecture suffix (empty otherwise).
# `FROM ${VAR}` is resolved through the default of `ARG VAR=...`.
parent_tag() {
  local file="$1" from var ref repo tag
  from=$(awk '/^[[:space:]]*[Ff][Rr][Oo][Mm][[:space:]]/ {
    for (i = 2; i <= NF; i++) if ($i !~ /^--/) { print $i; exit } }' "${file}")
  if [[ "${from}" =~ ^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$ ]]; then
    var="${BASH_REMATCH[1]}"
    from=$(sed -nE "s/^[[:space:]]*ARG[[:space:]]+${var}=//p" "${file}" \
      | head -n1 | tr -d "\"'")
  fi
  ref="${from//\$\{ARCH\}/${ARCH}}"
  ref="${ref//\$ARCH/${ARCH}}"
  repo="${GITHUB_REPOSITORY#*/}"
  repo="${repo,,}"
  local re="(^|/)${repo//./\\.}:([^:@/]+)\$"
  if [[ "${ref,,}" =~ ${re} ]]; then
    tag="${BASH_REMATCH[2]}"
    echo "${tag%_"${ARCH}"}"
  fi
}

init_state() {
  STATE_DIR="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/image-ci"
  STATE="${STATE_DIR}/state.tsv"
  rm -rf "${STATE_DIR}"
  mkdir -p "${STATE_DIR}/failed-logs"
  : >"${STATE}"
  local -A seen=()
  local file tag ks ts nocache parent
  while IFS=$'\t' read -r file tag ks ts; do
    ks=$(undash "${ks}")
    ts=$(undash "${ts}")
    if [[ -n "$(row_get "${tag}" 1)" ]]; then
      echo "::error::duplicate image tag in ENTRIES: ${tag}" >&2
      return 1
    fi
    # --no-cache applies to the first image of a Containerfile only: its
    # -4k sibling and the twin share the layers before the kernel.
    nocache=no
    if [[ -z "${seen[${file}]:-}" ]]; then
      nocache=yes
      seen[${file}]=1
    fi
    parent=$(parent_tag "${file}")
    printf '%s\t%s\t%s\tprimary\t%s\t%s\t-\tpending\t-\n' \
      "${tag}" "${file}" "$(dash "${ks}")" "${nocache}" \
      "$(dash "${parent}")" >>"${STATE}"
    if [[ -n "${ts}" ]]; then
      printf '%s\t%s\t%s\ttwin\tno\t-\t-\tpending\t-\n' \
        "${tag}-nosign" "${file}" "${ts}" >>"${STATE}"
    fi
  done < <(printf '%s' "${ENTRIES}" | jq -r '.[]
    | [.file, .tag, (.kernel_suffix // ""), (.twin_suffix // "")]
    | map(if . == "" then "-" else . end) | @tsv')
}

# ----------------------------------------------------------------- hash ----
phase_hash() {
  group "Content hashes"
  ci_hash_inputs
  local tag file kind hash prior primary_hash=""
  while IFS= read -r tag; do
    file=$(row_get "${tag}" "${col_file}")
    kind=$(row_get "${tag}" "${col_kind}")
    if [[ "${kind}" == "twin" ]]; then
      hash=$(ci_twin_hash "${primary_hash}")
    else
      hash=$(ci_image_hash "${file}" "${tag}")
      primary_hash="${hash}"
    fi
    row_set "${tag}" "${col_hash}" "${hash}"
    prior=$(ci_published_hash "$(ref_of "${tag}")")
    if [[ "$(ci_skip_decision "${prior}" "${hash}")" == "skip" ]]; then
      set_state "${tag}" skipped-unchanged "hash ${hash}"
    else
      echo "image ${tag}: content-hash=${hash} prior=${prior:-none}"
    fi
  done < <(all_tags)
  endgroup
}

# ---------------------------------------------------------------- build ----
product_name() {
  local name="${GITHUB_REPOSITORY##*/}"
  echo "${name//-/ }"
}

cache_args() {
  # Layer cache in the registry: read on every run, rewritten only on main.
  CACHE_ARGS=(--layers "--cache-from=${IMAGE}-cache-${ARCH}")
  CACHE_RO_ARGS=("${CACHE_ARGS[@]}")
  if is_main; then
    CACHE_ARGS+=("--cache-to=${IMAGE}-cache-${ARCH}")
  fi
}

# podman build with the registry layer cache; a stale cache blob makes the
# cache push fail after a good build, in which case it is retried read-only.
# $1 = log file, then the podman build arguments.
run_build() {
  local log="$1" rc=0
  shift
  podman_ build "${CACHE_ARGS[@]}" "$@" 2>&1 | tee "${log}" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    if grep -qE "image not known|locating image with ID" "${log}"; then
      echo "::warning::buildah cache push failed (stale storage blob), retrying without --cache-to"
      podman_ build "${CACHE_RO_ARGS[@]}" "$@" || return 1
      return 0
    fi
    return "${rc}"
  fi
}

apt_build_args() {
  APT_ARGS=()
  # APT repository trust: the key hash is derived by the pipeline from the
  # APT_GPG_KEY secret; when absent the Containerfile defaults apply.
  [[ -z "${BOOTC_GPG_SHA256:-}" ]] \
    || APT_ARGS+=(--build-arg "BOOTC_GPG_SHA256=${BOOTC_GPG_SHA256}")
  [[ -z "${APT_URL:-}" ]] \
    || APT_ARGS+=(--build-arg "BOOTC_APT_URL=${APT_URL}")
}

# The shared base (SSL prerequisites, trusted APT repository) is built once.
# It stays local to the runner and is never pushed.
build_base() {
  local flags=()
  # --no-cache (refresh run) skips the cache lookup so the packages are
  # refreshed, and the fresh layers still replace the cache.
  if is_refresh; then flags+=(--no-cache); fi
  apt_build_args
  run_build "${STATE_DIR}/build-base.log" \
    "${flags[@]}" \
    --build-arg "ARCH=${ARCH}" \
    --build-arg "PRODUCT_NAME=$(product_name)" \
    "${APT_ARGS[@]}" \
    --tag localhost/debian-bootc-base \
    -f Containerfile.base \
    --format docker \
    .
}

# Boot stack and firmware set of an image, from the architecture and the boot
# profile a Containerfile declares with LABEL org.daemoncores.boot-profile.
#   amd64            grub-efi-amd64-signed
#   arm64 generic    grub-efi-arm64-signed (UEFI PCs, servers, VMs)
#   arm64 embedded   u-boot-tools, no GRUB (boards and phones booting U-Boot)
# Sets PROFILE BOOT_PKG APT_HOLD_PKG FIRMWARE_PKGS ARM_DEVICES.
boot_stack() {
  local file="$1" kernel_suffix="$2"
  PROFILE=generic
  if grep -qE '^[[:space:]]*LABEL[[:space:]]+org\.daemoncores\.boot-profile=.?embedded' "${file}"; then
    PROFILE=embedded
  fi
  if [[ "${ARCH}" == "arm64" ]]; then
    FIRMWARE_PKGS="firmware-linux-free firmware-linux firmware-misc-nonfree"
    if [[ "${PROFILE}" == "embedded" ]]; then
      BOOT_PKG="u-boot-tools"
      APT_HOLD_PKG=""
    else
      BOOT_PKG="grub-efi-arm64-signed"
      APT_HOLD_PKG="grub-efi-arm64-signed"
    fi
  else
    BOOT_PKG="grub-efi-amd64-signed"
    FIRMWARE_PKGS="intel-microcode amd64-microcode firmware-linux-free firmware-linux firmware-misc-nonfree"
    APT_HOLD_PKG="grub-efi-amd64-signed"
  fi
  # arm64 ships two kernels: the 16 KB one on the plain tag for recent
  # hardware, the 4 KB one (-4k image) for the older devices. ARM_DEVICES
  # lists the device module packages built against that kernel.
  ARM_DEVICES="rpi3 rpi4"
  if [[ "${kernel_suffix}" == -16k* ]]; then
    ARM_DEVICES="rpi5 rk3588"
  fi
}

pubkey_b64() { printf '%s' "${COSIGN_PUBLIC_KEY:-}" | base64 -w0; }

# Tags carried by the canonical combo (amd64) for retrocompat with the
# single-arch era: bare tag, commit tag, and the monthly tag of a scheduled
# run. Prints one tag per line.
canonical_tags() {
  local tag="$1" sha
  sha=$(git rev-parse --short HEAD) || return 1
  if [[ "${tag}" == "latest" ]]; then
    echo "${IMAGE}:${sha}"
    if [[ "${GITHUB_EVENT_NAME:-}" == "schedule" ]]; then
      echo "${IMAGE}:monthly-$(date -u +%Y%m%d)"
    fi
  else
    echo "${IMAGE}:${tag}-${sha}"
    if [[ "${GITHUB_EVENT_NAME:-}" == "schedule" ]]; then
      echo "${IMAGE}:${tag}-monthly-$(date -u +%Y%m%d)"
    fi
  fi
  echo "${IMAGE}:${tag}"
}

build_image() {
  local tag="$1" file suffix kind hash nocache stamp_tag sha
  file=$(row_get "${tag}" "${col_file}")
  suffix=$(undash "$(row_get "${tag}" "${col_suffix}")")
  kind=$(row_get "${tag}" "${col_kind}")
  hash=$(row_get "${tag}" "${col_hash}")
  nocache=$(row_get "${tag}" "${col_nocache}")
  local flags=()
  if [[ "${nocache}" == "yes" ]] && is_refresh; then flags+=(--no-cache); fi
  local log="${STATE_DIR}/build-${tag}.log"
  sha=$(git rev-parse --short HEAD) || return 1

  if is_derived "${tag}"; then
    # An image built on another image of this run: the parent is the local
    # image when it was rebuilt, the published one otherwise (pull policy
    # missing).
    local parent
    parent=$(row_get "${tag}" "${col_parent}")
    run_build "${log}" \
      "${flags[@]}" \
      --pull=missing \
      --arch "${ARCH}" \
      --build-arg "BASE_IMAGE=$(ref_of "${parent}")" \
      --build-arg "ARCH=${ARCH}" \
      --build-arg "COSIGN_PUBKEY_B64=$(pubkey_b64)" \
      --build-arg "IMAGE_REF=${IMAGE,,}" \
      --build-arg "IMAGE_REGISTRY=${IMAGE}" \
      --build-arg "PRODUCT_NAME=$(product_name)" \
      --label "org.daemoncores.content-hash=${hash}" \
      --tag "$(ref_of "${tag}")" \
      --file "${file}" \
      .
    return
  fi

  if [[ "${ARCH}" == "amd64" ]]; then
    local t canonical
    canonical=$(canonical_tags "${tag}") || return 1
    while IFS= read -r t; do
      flags+=(--tag "${t}")
    done <<<"${canonical}"
  fi
  boot_stack "${file}" "${suffix}"
  # The twin carries the version stamp of its primary image.
  stamp_tag="${tag}"
  if [[ "${kind}" == "twin" ]]; then stamp_tag="${tag%-nosign}"; fi
  apt_build_args
  run_build "${log}" \
    "${flags[@]}" \
    --build-arg "BASE_IMAGE=localhost/debian-bootc-base" \
    --build-arg "ARCH=${ARCH}" \
    --build-arg "KERNEL_SUFFIX=${suffix}" \
    --build-arg "ARM_DEVICES=${ARM_DEVICES}" \
    --build-arg "BOOT_PKG=${BOOT_PKG}" \
    --build-arg "BOOT_PROFILE=${PROFILE}" \
    --build-arg "FIRMWARE_PKGS=${FIRMWARE_PKGS}" \
    --build-arg "APT_HOLD_PKG=${APT_HOLD_PKG}" \
    --build-arg "DEBS_HASH=${DEBS_HASH}" \
    --build-arg "KERNEL_DEBS_HASH=${KERNEL_DEBS_HASH}" \
    --build-arg "COSIGN_PUBKEY_B64=$(pubkey_b64)" \
    --build-arg "IMAGE_REF=${IMAGE,,}" \
    --build-arg "IMAGE_VERSION=${stamp_tag}-${sha}" \
    --build-arg "IMAGE_CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --label "org.daemoncores.content-hash=${hash}" \
    --tag "$(ref_of "${tag}")" \
    -f "${file}" \
    --format docker \
    .
}

# Build the given images (one tag per line on stdin), in order.
build_rows() {
  local tag
  while IFS= read -r tag; do
    group "Build ${tag}"
    if build_image "${tag}"; then
      set_state "${tag}" built
    else
      set_state "${tag}" build-failed "see build-${tag}.log"
      cp -f "${STATE_DIR}/build-${tag}.log" "${STATE_DIR}/failed-logs/" 2>/dev/null \
        || echo "no build log for ${tag}"
    fi
    endgroup
  done
}

pending_rows() {
  # $1 = derived (yes) or not (no)
  local tag
  while IFS= read -r tag; do
    if is_derived "${tag}"; then
      [[ "$1" == "yes" ]] || continue
    else
      [[ "$1" == "no" ]] || continue
    fi
    echo "${tag}"
  done < <(tags_in_state pending)
}

phase_build() {
  local rows base_ok=yes
  rows=$(pending_rows no)
  [[ -n "${rows}" ]] || {
    echo "no image to build"
    return 0
  }
  cache_args
  group "Build base"
  if ! build_base; then
    base_ok=no
    cp -f "${STATE_DIR}/build-base.log" "${STATE_DIR}/failed-logs/" 2>/dev/null \
      || echo "no base build log"
  fi
  endgroup
  if [[ "${base_ok}" == "no" ]]; then
    local tag
    while IFS= read -r tag; do
      set_state "${tag}" build-failed "base build failed"
    done <<<"${rows}"
    return 0
  fi
  build_rows <<<"${rows}"
}

# Images built on another image of this run, once that image passed: the
# parent must be ok (rebuilt, local) or skipped-unchanged (published).
phase_wave2() {
  local tag parent pstate own
  local -a todo=()
  while IFS= read -r tag; do
    is_derived "${tag}" || continue
    own=$(row_get "${tag}" "${col_state}")
    [[ "${own}" == "pending" || "${own}" == "skipped-unchanged" ]] || continue
    parent=$(row_get "${tag}" "${col_parent}")
    pstate=$(row_get "${parent}" "${col_state}")
    case "${pstate}" in
      ok) todo+=("${tag}") ;;
      skipped-unchanged)
        # Unchanged on its own and on its parent: nothing to rebuild.
        [[ "${own}" == "pending" ]] && todo+=("${tag}")
        ;;
      *)
        set_state "${tag}" blocked "parent ${parent:-?} is ${pstate:-absent}"
        ;;
    esac
  done < <(all_tags)
  [[ "${#todo[@]}" -gt 0 ]] || return 0
  cache_args
  build_rows < <(printf '%s\n' "${todo[@]}")
}

# ----------------------------------------------------------------- lint ----
lint_image() {
  local tag="$1" ref
  ref=$(ref_of "${tag}")
  if [[ "$(row_get "${tag}" "${col_kind}")" == "primary" ]]; then
    podman_ run --rm "${ref}" \
      sh -c "ldd /usr/bin/bootc && ldd /usr/lib/libostree-1.so*" || return 1
  fi
  podman_ run --rm "${ref}" bootc container lint
}

phase_lint() {
  local tag
  while IFS= read -r tag; do
    group "Lint ${tag}"
    if lint_image "${tag}"; then
      set_state "${tag}" linted
    else
      set_state "${tag}" lint-failed
    fi
    endgroup
  done < <(tags_in_state built)
}

# ------------------------------------------------------------------ test ---
IMAGE_TEST="${IMAGE_TEST:-${CI_ROOT}/image-test/image-test.sh}"

in_list() {
  local item
  for item in $2; do
    [[ "${item}" == "$1" ]] && return 0
  done
  return 1
}

ram_budget() {
  local gate_prefix="${RAM_GATE_TAG_PREFIX:-}"
  if [[ "$1" == "${gate_prefix}"* ]]; then
    echo "${RAM_GATE_MB:-15}"
  else
    echo 0
  fi
}

# Run an image-test.sh sub-command in a child process: its `exit` must not
# end this loop, and a hung VM must not hold the job.
image_test() {
  local limit="${BOOT_TEST_TIMEOUT:-2700}"
  timeout "${limit}" env IMAGE_TEST_STATE="${STATE_DIR}/image-test" "$@"
}

# Serial log image-test.sh writes for a reference (lib.sh derives the name the
# same way: every character outside [A-Za-z0-9._-] becomes an underscore).
serial_log_of() {
  local ref
  ref=$(ref_of "$1")
  local base="${STATE_DIR}/image-test/logs/${ref//[^A-Za-z0-9._-]/_}"
  # A tag booted once per category leaves one log per category: the most
  # recently written one is the failing boot (the harness stops at none).
  local f newest=""
  for f in "${base}.serial.log" "${base}".*.serial.log; do
    [[ -f "${f}" ]] || continue
    if [[ -z "${newest}" || "${f}" -nt "${newest}" ]]; then newest=${f}; fi
  done
  echo "${newest:-${base}.serial.log}"
}

ensure_prepared() {
  [[ "${PREPARED}" == "no" ]] || return 0
  [[ "${PREPARE_FAILED}" == "no" ]] || return 1
  group "Boot test preparation"
  if image_test env ARCH="${ARCH}" bash "${IMAGE_TEST}" prepare; then
    PREPARED=yes
  else
    PREPARE_FAILED=yes
  fi
  endgroup
  [[ "${PREPARED}" == "yes" ]]
}

phase_test() {
  local tag ref rc cat failed
  local -a cats
  while IFS= read -r tag; do
    if in_list "${tag}" "${BOOT_TEST_SKIP_TAGS:-iso}"; then
      set_state "${tag}" ok "no boot test for this image"
      continue
    fi
    if ! ensure_prepared; then
      set_state "${tag}" test-failed "image-test prepare failed"
      continue
    fi
    ref=$(ref_of "${tag}")
    group "Boot test ${tag}"
    # One boot per manifest category matching the tag (each may add kernel
    # args at install). Zero or one category: a single boot, the category being
    # left to the harness (manifests without categories, the -nosign twins).
    mapfile -t cats < <(image_test env ARCH="${ARCH}" bash "${IMAGE_TEST}" categories "${ref}")
    [[ "${#cats[@]}" -gt 1 ]] || cats=("")
    failed=""
    for cat in "${cats[@]}"; do
      rc=0
      # The harness stops its VM (and removes the PID file) when each run ends,
      # so the boots never share a live QEMU PID lock.
      image_test env ARCH="${ARCH}" IMAGE="${ref}" CATEGORY="${cat}" \
        bash "${IMAGE_TEST}" test "${ref}" "$(ram_budget "${tag}")" || rc=$?
      [[ "${rc}" -eq 0 ]] && continue
      if [[ "${rc}" -eq 124 ]]; then
        failed+="${failed:+; }${cat:+${cat}: }timeout after ${BOOT_TEST_TIMEOUT:-2700}"
      else
        failed+="${failed:+; }${cat:+${cat}: }exit ${rc}"
      fi
    done
    if [[ -z "${failed}" ]]; then
      set_state "${tag}" ok
    else
      set_state "${tag}" test-failed "${failed}"
    fi
    endgroup
  done < <(tags_in_state linted)
}

teardown_tests() {
  [[ "${PREPARED}" == "yes" ]] || return 0
  PREPARED=no
  if ! image_test env ARCH="${ARCH}" bash "${IMAGE_TEST}" teardown; then
    echo "::warning::image-test teardown failed"
  fi
}

# ------------------------------------------------------------------ scan ---
# Report only: a finding or a scanner failure never fails the build.
phase_scan() {
  local tag archive
  archive="${STATE_DIR}/scan-image.tar"
  while IFS= read -r tag; do
    [[ "$(row_get "${tag}" "${col_kind}")" == "primary" ]] || continue
    group "Scan ${tag}"
    if podman_ save --format docker-archive -o "${archive}" "$(ref_of "${tag}")" \
      && "${SUDO_CMD[@]}" chmod 644 "${archive}"; then
      if ! trivy image --input "${archive}" --format table \
        --severity "${TRIVY_SEVERITY:-CRITICAL,HIGH}" --ignore-unfixed \
        --exit-code 0; then
        echo "::warning::vulnerability scan of ${tag} failed (report only)"
      fi
    else
      echo "::warning::could not save ${tag} for the vulnerability scan"
    fi
    "${SUDO_CMD[@]}" rm -f "${archive}"
    endgroup
  done < <(tags_in_state ok)
}

# ------------------------------------------------------------------ push ---
push_image() {
  local tag="$1" digestfile t
  digestfile="${STATE_DIR}/digest-${tag}.txt"
  podman_ push --digestfile "${digestfile}" "$(ref_of "${tag}")" || return 1
  echo "${IMAGE,,}@$(cat "${digestfile}")" >>"${REFS_FILE}"
  if [[ "${ARCH}" == "amd64" ]] && ! is_derived "${tag}"; then
    local canonical
    canonical=$(canonical_tags "${tag}") || return 1
    while IFS= read -r t; do
      podman_ push "${t}" || return 1
    done <<<"${canonical}"
  fi
}

phase_push() {
  REFS_FILE="${STATE_DIR}/push_refs.txt"
  : >"${REFS_FILE}"
  is_main || {
    echo "not the main branch: nothing is pushed"
    return 0
  }
  local tag
  while IFS= read -r tag; do
    group "Push ${tag}"
    if push_image "${tag}"; then
      set_state "${tag}" pushed
    else
      set_state "${tag}" push-failed
    fi
    endgroup
  done < <(tags_in_state ok)
}

phase_sign() {
  REFS_FILE="${STATE_DIR}/push_refs.txt"
  [[ -s "${REFS_FILE}" ]] || {
    echo "no pushed image to sign"
    return 0
  }
  group "Sign"
  if ! REFS=$(cat "${REFS_FILE}") bash "${CI_ROOT}/sign-image/sign.sh"; then
    SIGN_FAILED=yes
    echo "::error::signing failed"
  fi
  endgroup
}

# ---------------------------------------------------------------- report ---
has_failures() {
  local tag state
  [[ "${SIGN_FAILED}" == "no" ]] || return 0
  while IFS= read -r tag; do
    state=$(row_get "${tag}" "${col_state}")
    in_list "${state}" "${FAILURE_STATES}" && return 0
  done < <(all_tags)
  return 1
}

report() {
  [[ -f "${STATE:-}" ]] || return 0
  local out="${GITHUB_STEP_SUMMARY:-/dev/stdout}" tag state detail log
  {
    echo "### Images (${ARCH})"
    echo
    echo "| Image | State | Detail |"
    echo "| --- | --- | --- |"
    while IFS= read -r tag; do
      state=$(row_get "${tag}" "${col_state}")
      detail=$(undash "$(row_get "${tag}" "${col_detail}")")
      echo "| \`$(ref_of "${tag}")\` | ${state} | ${detail} |"
    done < <(all_tags)
    if [[ "${SIGN_FAILED}" == "yes" ]]; then
      echo
      echo "Signature: **failed**"
    fi
  } >>"${out}"
  while IFS= read -r tag; do
    state=$(row_get "${tag}" "${col_state}")
    [[ "${state}" == "test-failed" ]] || continue
    log=$(serial_log_of "${tag}")
    [[ -f "${log}" ]] || continue
    cp -f "${log}" "${STATE_DIR}/failed-logs/serial-${tag}.log"
    {
      echo
      echo "<details><summary>Serial console of ${tag} (last 100 lines)</summary>"
      echo
      echo '```'
      tail -n 100 "${log}"
      echo '```'
      echo "</details>"
    } >>"${out}"
  done < <(all_tags)
}

finish() {
  local rc=$?
  trap - EXIT
  teardown_tests
  report || rc=1
  if has_failures; then rc=1; fi
  exit "${rc}"
}

# ------------------------------------------------------------------ main ---
main() {
  : "${ARCH:?ARCH is required}"
  : "${ENTRIES:?ENTRIES is required}"
  : "${GLOBAL_HASH:?GLOBAL_HASH is required}"
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
  IMAGE="${IMAGE:-ghcr.io/${GITHUB_REPOSITORY,,}}"
  export TESTS_FILE="${TESTS_FILE:-workflows/tests.yml}"
  init_state
  trap finish EXIT
  local -a phases=("$@")
  if [[ "${#phases[@]}" -eq 0 ]]; then
    phases=(hash build lint test wave2 lint test scan push sign)
  fi
  local phase
  for phase in "${phases[@]}"; do
    case "${phase}" in
      hash | build | wave2 | lint | test | scan | push | sign)
        "phase_${phase}"
        ;;
      *)
        echo "::error::unknown phase: ${phase}" >&2
        return 2
        ;;
    esac
  done
}

main "$@"
