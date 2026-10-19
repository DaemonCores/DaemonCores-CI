#!/bin/bash
#
# apt-key-hash.sh - SHA-256 of the published APT public key, anchored on the
# APT_GPG_KEY secret.
#
# The image build pins the checksum of the repository public key it downloads
# (BOOTC_GPG_SHA256).  Instead of committing that checksum, the pipeline derives
# it here: the published key must carry the fingerprint of the private key held
# in the APT_GPG_KEY secret, and only then is the SHA-256 of the file exactly as
# served handed to the build.  Trust therefore rests on the secret, not on
# whatever GitHub Pages serves, and a key rotation needs no commit.
#
# Environment:
#   APT_GPG_KEY    armored private key (required)
#   KEY_URL        URL of the published public key (required)
#   WAIT_ATTEMPTS  polling attempts while the published key is stale (default 30)
#   WAIT_SECONDS   delay between attempts (default 20)
#
# Output: the SHA-256 on stdout.  The private key is never printed.
set -euo pipefail

#######################################
# Print an error to stderr and exit 1.
# Arguments:
#   $*: message
#######################################
die() {
  echo "apt-key-hash: $*" >&2
  exit 1
}

#######################################
# Print the fingerprint of the first key in a file.
# Arguments:
#   $1: path to a key (public or secret, armored or binary)
#######################################
fingerprint_of() {
  gpg --batch --with-colons --show-keys "$1" 2>/dev/null \
    | awk -F: '/^fpr:/ {print $10; exit}'
}

#######################################
# Main.
#######################################
main() {
  set +x
  local home want got tmp attempts delay i
  [[ -n "${APT_GPG_KEY:-}" ]] || die "APT_GPG_KEY is empty"
  [[ -n "${KEY_URL:-}" ]] || die "KEY_URL is required"
  attempts="${WAIT_ATTEMPTS:-30}"
  delay="${WAIT_SECONDS:-20}"
  home=$(mktemp -d)
  chmod 700 "${home}"
  tmp=$(mktemp)
  # shellcheck disable=SC2064  # expand now, both paths are fixed
  trap "rm -rf '${home}' '${tmp}'" EXIT
  export GNUPGHOME="${home}"
  printf '%s\n' "${APT_GPG_KEY}" >"${home}/secret.asc"
  want=$(fingerprint_of "${home}/secret.asc")
  rm -f "${home}/secret.asc"
  [[ "${want}" =~ ^[0-9A-F]{40}$ ]] || die "APT_GPG_KEY carries no usable key"
  got=""
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsSL "${KEY_URL}" -o "${tmp}" 2>/dev/null; then
      got=$(fingerprint_of "${tmp}")
      [[ "${got}" == "${want}" ]] && break
    fi
    got=""
    ((i == attempts)) || sleep "${delay}"
  done
  [[ "${got}" == "${want}" ]] \
    || die "the key published at ${KEY_URL} does not match the APT_GPG_KEY secret"
  sha256sum "${tmp}" | awk '{print $1}'
}

main "$@"
