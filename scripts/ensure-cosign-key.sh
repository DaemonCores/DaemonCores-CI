#!/bin/bash
#
# ensure-cosign-key.sh - make sure the repository owns an image signing key.
#
# Every repository that uses the shared pipeline signs its own images with its
# own cosign key pair, kept as repository secrets:
#   COSIGN_PRIVATE_KEY  encrypted private key
#   COSIGN_PASSWORD     its password (random, generated here)
#   COSIGN_PUBLIC_KEY   public key, baked into the images so hosts can verify
#
# When COSIGN_PRIVATE_KEY is absent the pair is generated and stored.  The
# private key and password only travel through pipes into `gh secret set`.
# Secrets created during a run are visible to workflows only from the next
# run, so the images signed by key start with the run after the creation.
#
# Environment:
#   GH_TOKEN             token with secrets:write (PAT_SECRETS)
#   GITHUB_REPOSITORY    owner/name of the repository
#   GITHUB_OUTPUT        step output file (optional)
#
# Outputs (step outputs): created=yes|no
set -euo pipefail

SECRET=COSIGN_PRIVATE_KEY

#######################################
# Print an error to stderr and exit 1.
# Arguments:
#   $*: message
#######################################
die() {
  echo "ensure-cosign-key: $*" >&2
  exit 1
}

#######################################
# Record the step output.
# Arguments:
#   $1: yes|no, whether a key was created
#######################################
output() {
  echo "created=$1" >>"${GITHUB_OUTPUT:-/dev/null}"
}

#######################################
# Generate the key pair and store the three secrets.
# Globals:
#   GITHUB_REPOSITORY
#######################################
generate() {
  local dir password
  dir=$(mktemp -d)
  # shellcheck disable=SC2064  # expand $dir now, it is fixed for this call
  trap "rm -rf '${dir}'" RETURN
  password=$(openssl rand -hex 24)
  (cd "${dir}" && COSIGN_PASSWORD="${password}" cosign generate-key-pair >/dev/null)
  gh secret set COSIGN_PRIVATE_KEY <"${dir}/cosign.key" >/dev/null \
    || die "cannot write secrets: GH_TOKEN (PAT_SECRETS) needs secrets:write"
  printf '%s' "${password}" | gh secret set COSIGN_PASSWORD >/dev/null
  gh secret set COSIGN_PUBLIC_KEY <"${dir}/cosign.pub" >/dev/null
}

#######################################
# Main.
#######################################
main() {
  [[ -n "${GH_TOKEN:-}" ]] || {
    echo "::warning::PAT_SECRETS is not set: images are signed keyless." \
      "Store a PAT with secrets:write as PAT_SECRETS to get a per-repository" \
      "signing key created automatically."
    output no
    return 0
  }
  [[ -n "${GITHUB_REPOSITORY:-}" ]] || die "GITHUB_REPOSITORY is not set"
  if gh secret list --repo "${GITHUB_REPOSITORY}" --json name --jq '.[].name' \
    | grep -qx "${SECRET}"; then
    echo "${SECRET} exists" >&2
    output no
    return 0
  fi
  echo "${SECRET} absent: generating the repository signing key" >&2
  generate
  echo "::notice::Image signing key created. Images are signed by key from" \
    "the next CI run."
  output yes
}

main "$@"
