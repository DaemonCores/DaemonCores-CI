#!/bin/bash
#
# e2e-local.sh - run the CI boot test (workflows/tests.yml of the calling
# repository) against a local image, using the same harness as CI
# (image-test/image-test.sh of this repository).
#
# Usage: e2e-local.sh IMAGE [ARCH]    (run from the calling repository root)
#   IMAGE  image already present in local podman storage (e.g. x:latest)
#   ARCH   amd64 (default) or arm64
#
# Environment:
#   ROOT     repository holding workflows/tests.yml (default: current directory)
#   CI_REPO  DaemonCores-CI checkout (default: the one holding this script)
#
# The harness needs sudo and /dev/kvm. On Debian/Ubuntu it installs QEMU with
# apt-get when the tool is absent; on other distros (Arch, Fedora, …) QEMU must
# be pre-installed. It is not usable inside the dev container.
set -euo pipefail

ROOT=$(cd "${ROOT:-.}" && pwd)
readonly ROOT
CI_REPO=$(cd "${CI_REPO:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)
readonly CI_REPO

#######################################
# Print an error to stderr and exit 1.
# Arguments:
#   $* - message
#######################################
die() {
  echo "e2e-local: $*" >&2
  exit 1
}

#######################################
# Check that every prerequisite is in place before paying for a boot.
# Arguments:
#   $1 - image ref
#   $2 - architecture
#######################################
preflight() {
  local image="$1" arch="$2" tool
  [[ "${arch}" == amd64 || "${arch}" == arm64 ]] \
    || die "ARCH must be amd64 or arm64"
  for tool in podman sudo; do
    command -v "${tool}" >/dev/null || die "missing required tool: ${tool}"
  done
  # apt-get is only required on Debian/Ubuntu (the harness uses it to install
  # QEMU when absent). On other distros QEMU must already be installed.
  local qemu_bin="qemu-system-x86_64"
  [[ "${arch}" == arm64 ]] && qemu_bin="qemu-system-aarch64"
  if ! command -v "${qemu_bin}" >/dev/null 2>&1; then
    command -v apt-get >/dev/null \
      || die "missing required tool: apt-get (or pre-install ${qemu_bin})"
  fi
  [[ -r "${CI_REPO}/image-test/image-test.sh" ]] \
    || die "harness not found, set CI_REPO (tried ${CI_REPO})"
  if [[ "${arch}" == amd64 && ! -e /dev/kvm ]]; then
    die "/dev/kvm is absent: the amd64 boot test needs hardware virtualization"
  fi
  podman image exists "${image}" \
    || die "image not in local podman storage: ${image}"
}

#######################################
# Run the image boot tests against a local podman image.
# Globals:
#   ROOT
#   CI_REPO
# Arguments:
#   $1: image reference in local podman storage
#   $2: architecture, amd64 (default) or arm64
# Returns:
#   Exits 1 on a usage error or a failed preflight, else the harness status.
#######################################
main() {
  local image="${1:-}" arch="${2:-amd64}"
  [[ -n "${image}" ]] || die "usage: $0 IMAGE [ARCH]"
  preflight "${image}" "${arch}"
  IMAGE="${image}" ARCH="${arch}" TESTS_FILE="${ROOT}/workflows/tests.yml" \
    bash "${CI_REPO}/image-test/image-test.sh"
}

main "$@"
