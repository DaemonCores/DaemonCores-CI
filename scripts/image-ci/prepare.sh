#!/usr/bin/env bash
# One apt install for everything a per-arch image job needs: registry
# inspection (skopeo, jq), the boot test (qemu and the UEFI firmware of the
# architecture, a python venv for the Secure Boot variable store, openssl).
#
# Usage: prepare.sh [--list]
#   --list  print the package names instead of installing them
# Input (environment):
#   ARCH  target architecture (amd64, arm64); detected from the host when unset
#   SUDO  command prefix for apt-get (default sudo, empty for none)
set -euo pipefail

detect_arch() {
  case "$(uname -m)" in
    x86_64) echo amd64 ;;
    aarch64 | arm64) echo arm64 ;;
    *)
      echo "::error::unsupported host architecture: $(uname -m)" >&2
      return 1
      ;;
  esac
}

packages() {
  local arch="${ARCH:-}"
  [[ -n "${arch}" ]] || arch=$(detect_arch)
  local -a pkgs=(skopeo jq python3-venv openssl)
  case "${arch}" in
    amd64) pkgs+=(qemu-system-x86 ovmf) ;;
    arm64) pkgs+=(qemu-system-arm qemu-efi-aarch64) ;;
    *)
      echo "::error::unsupported architecture: ${arch}" >&2
      return 1
      ;;
  esac
  printf '%s\n' "${pkgs[@]}"
}

main() {
  local -a pkgs sudo_cmd=()
  local list
  list=$(packages)
  mapfile -t pkgs <<<"${list}"
  if [[ "${1:-}" == "--list" ]]; then
    printf '%s\n' "${pkgs[@]}"
    return 0
  fi
  read -r -a sudo_cmd <<<"${SUDO-sudo}"
  "${sudo_cmd[@]}" apt-get update -qq
  "${sudo_cmd[@]}" apt-get install -y --no-install-recommends "${pkgs[@]}"
}

main "$@"
