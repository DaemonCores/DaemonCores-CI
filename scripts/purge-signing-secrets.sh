#!/bin/bash
#
# purge-signing-secrets.sh - drain the signing material from the runner.
#
# Every job that carried a signing secret calls this as its FINAL step,
# `if: always()`, so the material dies with the job even when the job fails:
#   - the key and passphrase arriving as step variables (COSIGN_PRIVATE_KEY,
#     COSIGN_PASSWORD, SB_SIGNING_KEY, SB_SIGNING_CERT, MODULE_SIGNING_KEY,
#     MODULE_SIGNING_CERT, APT_GPG_KEY) are unset in the shell of the step
#     that runs the purge. A GitHub runner re-injects job variables into each
#     new step, so the unset cannot reach earlier steps or the job definition;
#     it closes the last step's process env, which is the only shell that can
#     still observe the material at that point of the run.
#   - every private-key artifact the runs write to disk (cosign.key from the
#     secret generation, sb.key from the Secure Boot signing, mod.pem and
#     signing_key.pem from the kernel and module builds, and any other *.key
#     the temp roots carry) is overwritten with shred before the file is
#     removed. Public certificates (.crt, *.x509) are not secret: they are
#     deliberately left alone.
#
# Arguments: roots to sweep (default: the workspace and the runner temp dir).
#
# Environment: RUNNER_TEMP, GITHUB_WORKSPACE (both optional).
set -euo pipefail

KEY_NAMES=(-name '*.key' -o -name 'cosign.key' -o -name 'sb.key'
  -o -name 'mod.pem' -o -name 'signing_key.pem')

#######################################
# Print an error to stderr and exit 1.
# Arguments:
#   $*: message
#######################################
die() {
  echo "purge-signing-secrets: $*" >&2
  exit 1
}

#######################################
# Shred and remove one file without failing when shred (utils-extra) is
# absent — a plain unlink is the fallback, the secret never returns to the
# runner disk image unowned either way.
# Arguments:
#   $1: path
#######################################
destroy_file() {
  local path=$1
  if command -v shred >/dev/null 2>&1; then
    shred -uz "${path}" 2>/dev/null || rm -f "${path}"
  else
    rm -f "${path}"
  fi
}

#######################################
# Shred every private key under one sweep root.
# Arguments:
#   $*: sweep roots (existing directories)
#######################################
sweep() {
  local root file
  for root in "$@"; do
    [[ -d "${root}" ]] || continue
    while IFS= read -r -d '' file; do
      destroy_file "${file}"
    done < <(find "${root}" -type f \( "${KEY_NAMES[@]}" \) -print0 \
      2>/dev/null)
  done
}

#######################################
# Main.
#######################################
main() {
  set +x
  # Unset the step-shell copy of every signing secret (defensive: the material
  # must not outlive the step that consumed it).
  unset COSIGN_PRIVATE_KEY COSIGN_PASSWORD COSIGN_PUBLIC_KEY \
    SB_SIGNING_KEY SB_SIGNING_CERT \
    MODULE_SIGNING_KEY MODULE_SIGNING_CERT APT_GPG_KEY
  local roots=("$@")
  if ((${#roots[@]} == 0)); then
    roots=("${GITHUB_WORKSPACE:-$PWD}" "${RUNNER_TEMP:-/tmp}")
  fi
  sweep "${roots[@]}"
}

main "$@"
