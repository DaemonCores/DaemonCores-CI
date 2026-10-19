#!/bin/bash
#
# rotate-apt-key.sh - rotate the APT repository signing key from CI.
#
# Subcommands:
#   rotate           Generate a fresh key pair and store the private key as the
#                    APT_GPG_KEY secret.  The private key only ever travels
#                    through a pipe into `gh secret set`: it is never kept in a
#                    variable, a file outside the throw-away GNUPGHOME, a log
#                    or an artifact.  Prints the public fingerprint on stdout.
#
# The image build derives the pinned hash of the published key at build time
# (apt-key-hash.sh), so nothing has to be committed after a rotation.
#
# Environment:
#   GH_TOKEN          token with secrets:write (PAT_SECRETS)
#   GITHUB_REPOSITORY owner/name, passed to gh when set
#   UID_NAME          user id of the generated key (default: "APT repository")
set -euo pipefail

UID_NAME="${UID_NAME:-APT repository}"

#######################################
# Print an error to stderr and exit 1.
# Arguments:
#   $*: message
#######################################
die() {
  echo "rotate-apt-key: $*" >&2
  exit 1
}

#######################################
# Generate a key pair in a private GNUPGHOME and store the private key as the
# APT_GPG_KEY secret.  Nothing secret is printed.
# Globals:
#   UID_NAME
# Outputs:
#   The public key fingerprint on stdout.
#######################################
rotate() {
  local home fpr
  local -a repo=()
  [[ -z "${GITHUB_REPOSITORY:-}" ]] || repo=(--repo "${GITHUB_REPOSITORY}")
  home=$(mktemp -d)
  # shellcheck disable=SC2064  # expand $home now, it is fixed for this call
  trap "rm -rf '${home}'" RETURN
  chmod 700 "${home}"
  export GNUPGHOME="${home}"
  gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "${UID_NAME}" rsa4096 sign never 2>/dev/null
  fpr=$(gpg --batch --with-colons --list-keys \
    | awk -F: '/^fpr:/ {print $10; exit}')
  [[ -n "${fpr}" ]] || die "key generation produced no fingerprint"
  gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
    --armor --export-secret-keys "${fpr}" 2>/dev/null \
    | gh secret set APT_GPG_KEY "${repo[@]}" >/dev/null
  printf '%s\n' "${fpr}"
}

#######################################
# Main.
# Arguments:
#   $1: subcommand
#######################################
main() {
  set +x
  case "${1:-}" in
    rotate) rotate ;;
    *) die "usage: $0 rotate" ;;
  esac
}

main "$@"
