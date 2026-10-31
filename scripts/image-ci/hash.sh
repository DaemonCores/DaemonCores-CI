#!/usr/bin/env bash
# Content hash of the container images and the skip decision (library).
#
# The formula is the one the "Compute content hash" step of bootc-build.yml
# used before the single-job build: it must stay byte-identical, or every
# published image would look changed once and be rebuilt.
#
# Source this file, then call:
#   ci_hash_inputs           fetch the inputs shared by every image of an arch
#   ci_image_hash FILE TAG   print the content hash of one image
#   ci_twin_hash HASH        print the hash of the unsigned-kernel twin
#   ci_published_hash REF    print the content-hash label of a published image
#   ci_skip_decision PRIOR HASH   print "skip" or "build"
#
# Input (environment):
#   ARCH              target architecture (amd64, arm64)
#   GITHUB_REPOSITORY owner/repo, derives the debs cache URL
#   GLOBAL_HASH       hash over every Containerfile (discover job)
#   BOOTC_GPG_SHA256  APT key hash (build input)
#   COSIGN_PUBLIC_KEY signing public key (build input)
#   TESTS_FILE        boot-test manifest (default workflows/tests.yml)
#   SUDO              command prefix for skopeo (default sudo, empty for none)
#   GITHUB_EVENT_NAME, FORCE_NO_CACHE   a refresh run never skips
# Output (variables set by ci_hash_inputs): DEBS, BASE_DIGEST, SIGNING,
#   TESTS_HASH, DEBS_HASH, KERNEL_DEBS_HASH

_hash_sudo() {
  local -a prefix=()
  read -r -a prefix <<<"${SUDO-sudo}"
  "${prefix[@]}" "$@"
}

ci_hash_inputs() {
  local owner repo pages base
  owner="${GITHUB_REPOSITORY%%/*}"
  repo="${GITHUB_REPOSITORY#*/}"
  pages="https://${owner,,}.github.io/${repo}"
  DEBS=$(curl -fsSL "${pages}/debs-cache/hashes.json" 2>/dev/null \
    | jq -cS . 2>/dev/null || echo '{}')
  # The external base image is the one Containerfile.base starts from.
  base=$(grep -m1 -iE '^FROM ' Containerfile.base | awk '{ print $2 }')
  # Resolve the base image's per-arch digest (--override-arch) so amd64
  # and arm64 get distinct, correct digests even when the base is a
  # multi-arch manifest list.
  BASE_DIGEST=$(_hash_sudo skopeo inspect --override-arch "${ARCH}" \
    --format '{{.Digest}}' "docker://${base}" 2>/dev/null || echo none)
  # The APT key hash and the signing public key are build inputs too:
  # a rotation must not be mistaken for an identical image.
  SIGNING=$(printf '%s\n%s' "${BOOTC_GPG_SHA256:-}" "${COSIGN_PUBLIC_KEY:-}" \
    | sha256sum | cut -d' ' -f1)
  # The boot-test manifest is an input too: an image must be re-tested
  # when its tests change, even if its bytes do not.
  TESTS_HASH=$(sha256sum "${TESTS_FILE:-workflows/tests.yml}" 2>/dev/null \
    | cut -d' ' -f1 || echo none)
  # Per-arch fingerprints of the published debs, passed to the image build as
  # build args so its apt layers are not served stale from the layer cache.
  # Kernels and device modules are fingerprinted apart: a kernel rebuild
  # must re-run the kernel layer only, not the heavy package layer before it.
  DEBS_HASH=$(printf '%s' "${DEBS}" \
    | jq -cS --arg a "-${ARCH}" 'with_entries(select((.key | endswith($a)) and (.key | test("^linux-(image|modules)-") | not)))' \
    | sha256sum | cut -d' ' -f1)
  KERNEL_DEBS_HASH=$(printf '%s' "${DEBS}" \
    | jq -cS --arg a "-${ARCH}" 'with_entries(select((.key | endswith($a)) and (.key | test("^linux-(image|modules)-"))))' \
    | sha256sum | cut -d' ' -f1)
  export DEBS_HASH KERNEL_DEBS_HASH
}

ci_image_hash() {
  local cf_file="$1" cf_tag="$2" cf
  cf=$(sha256sum "${cf_file}" | cut -d' ' -f1)
  printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
    "${DEBS}" "${BASE_DIGEST}" "${cf}" "${GLOBAL_HASH}" "${cf_tag}" \
    "${ARCH}" "${SIGNING}" "${TESTS_HASH}" \
    | sha256sum | cut -d' ' -f1
}

# The twin differs from its primary image by the kernel only.
ci_twin_hash() {
  printf '%s\nnosign\n' "$1" | sha256sum | cut -d' ' -f1
}

# Content-hash label of the currently published per-arch image (empty when the
# image is not published yet).
ci_published_hash() {
  _hash_sudo skopeo inspect --override-arch "${ARCH}" \
    --format '{{ index .Labels "org.daemoncores.content-hash" }}' \
    "docker://$1" 2>/dev/null || echo ''
}

# "skip" when the published image carries the same hash, except on a refresh
# run (schedule, force_no_cache) whose job is to rebuild anyway.
ci_skip_decision() {
  local prior="$1" hash="$2"
  if [[ -n "${prior}" && "${prior}" == "${hash}" ]]; then
    if [[ "${GITHUB_EVENT_NAME:-}" == "schedule" ||
      "${FORCE_NO_CACHE:-}" == "true" ]]; then
      echo build
    else
      echo skip
    fi
  else
    echo build
  fi
}
