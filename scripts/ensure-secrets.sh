#!/bin/bash
#
# ensure-secrets.sh - create the signing secrets a repository does not have yet.
#
# A fork should run the shared pipeline without hunting for keys: every secret
# that can be generated is generated here, once, and stored as a repository
# secret.  Groups (arguments):
#   cosign   COSIGN_PRIVATE_KEY, COSIGN_PASSWORD, COSIGN_PUBLIC_KEY
#            image signing key pair, public half baked into the images
#   apt      APT_GPG_KEY
#            key that signs the generated APT repository
#   sb       SB_SIGNING_KEY, SB_SIGNING_CERT (both base64)
#            self-signed Secure Boot key for the custom GRUB package
#   modules  MODULE_SIGNING_KEY, MODULE_SIGNING_CERT
#            kernel module signing key
#
# An existing secret is never touched: regenerating a key invalidates what was
# signed with it.  Private keys only travel through pipes and a private
# temporary directory into `gh secret set`; nothing secret is printed.
#
# A secret created during a run is only visible to the NEXT run, so the caller
# must start a new run when `created` is not empty.
#
# Environment:
#   GH_TOKEN           token with secrets:write (PIPELINE_PAT)
#   GITHUB_REPOSITORY  owner/name of the repository
#   GITHUB_OUTPUT      step output file (optional)
#
# Outputs (step outputs): created=<space-separated groups created by this run>
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KNOWN_SECRETS=""
CREATED=()

#######################################
# Print an error to stderr and exit 1.
# Arguments:
#   $*: message
#######################################
die() {
  echo "ensure-secrets: $*" >&2
  exit 1
}

#######################################
# Store stdin as a repository secret.
# Arguments:
#   $1: secret name
#######################################
store() {
  gh secret set "$1" --repo "${GITHUB_REPOSITORY}" >/dev/null \
    || die "cannot write $1: GH_TOKEN (PIPELINE_PAT) needs secrets:write"
}

#######################################
# Succeed when a secret already exists.
# Arguments:
#   $1: secret name
#######################################
has() {
  grep -qx "$1" <<<"${KNOWN_SECRETS}"
}

#######################################
# Create a private scratch directory removed when the caller returns.
# Outputs:
#   The directory path on stdout.
#######################################
scratch() {
  local dir
  dir=$(mktemp -d)
  chmod 700 "${dir}"
  printf '%s' "${dir}"
}

#######################################
# Generate the cosign key pair and its random password.
#######################################
gen_cosign() {
  local dir password
  dir=$(scratch)
  # shellcheck disable=SC2064  # expand $dir now, it is fixed for this call
  trap "rm -rf '${dir}'" RETURN
  password=$(openssl rand -hex 24)
  (cd "${dir}" && COSIGN_PASSWORD="${password}" cosign generate-key-pair >/dev/null)
  store COSIGN_PRIVATE_KEY <"${dir}/cosign.key"
  printf '%s' "${password}" | store COSIGN_PASSWORD
  store COSIGN_PUBLIC_KEY <"${dir}/cosign.pub"
}

#######################################
# Generate the APT repository signing key.
# Globals:
#   GITHUB_REPOSITORY
#######################################
gen_apt() {
  UID_NAME="${GITHUB_REPOSITORY} APT repository" \
    "${SCRIPT_DIR}/rotate-apt-key.sh" rotate >/dev/null
}

#######################################
# Generate a self-signed Secure Boot key and certificate.  The Secure Boot
# firmware only trusts enrolled certificates: the certificate ships in the
# GRUB package so users can enrol it (MOK or db).
# Globals:
#   GITHUB_REPOSITORY
#######################################
gen_sb() {
  local dir
  dir=$(scratch)
  # shellcheck disable=SC2064
  trap "rm -rf '${dir}'" RETURN
  openssl req -new -x509 -newkey rsa:2048 -nodes -days 36500 \
    -keyout "${dir}/sb.key" -out "${dir}/sb.crt" \
    -subj "/CN=Secure Boot signing key/O=${GITHUB_REPOSITORY}/OU=CI" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=codeSigning" 2>/dev/null
  base64 -w0 <"${dir}/sb.key" | store SB_SIGNING_KEY
  base64 -w0 <"${dir}/sb.crt" | store SB_SIGNING_CERT
}

#######################################
# Generate the persistent kernel module signing key and certificate.
# Globals:
#   GITHUB_REPOSITORY
#######################################
gen_modules() {
  local dir
  dir=$(scratch)
  # shellcheck disable=SC2064
  trap "rm -rf '${dir}'" RETURN
  openssl req -new -x509 -newkey rsa:4096 -nodes -days 36500 \
    -keyout "${dir}/mod.pem" -out "${dir}/mod.x509" \
    -subj "/CN=Module Signing Key/O=${GITHUB_REPOSITORY}/OU=CI" 2>/dev/null
  store MODULE_SIGNING_KEY <"${dir}/mod.pem"
  store MODULE_SIGNING_CERT <"${dir}/mod.x509"
}

#######################################
# Create one group when its marker secret is absent.
# Arguments:
#   $1: group name
#######################################
ensure_group() {
  local group="$1" marker
  case "${group}" in
    cosign) marker=COSIGN_PRIVATE_KEY ;;
    apt) marker=APT_GPG_KEY ;;
    sb) marker=SB_SIGNING_KEY ;;
    modules) marker=MODULE_SIGNING_KEY ;;
    *) die "unknown group: ${group}" ;;
  esac
  if has "${marker}"; then
    echo "${marker} exists" >&2
    return 0
  fi
  echo "${marker} absent: generating the '${group}' secrets" >&2
  "gen_${group}"
  CREATED+=("${group}")
}

#######################################
# Main.
# Arguments:
#   $@: groups to ensure
#######################################
main() {
  set +x
  local group
  (($# > 0)) || die "usage: $0 cosign|apt|sb|modules..."
  if [[ -z "${GH_TOKEN:-}" ]]; then
    echo "::warning::PIPELINE_PAT is not set: missing signing secrets cannot be" \
      "generated (images are signed keyless, modules stay unsigned)." \
      "Store a PAT with secrets:write as PIPELINE_PAT to generate them" \
      "automatically."
    echo "created=" >>"${GITHUB_OUTPUT:-/dev/null}"
    return 0
  fi
  [[ -n "${GITHUB_REPOSITORY:-}" ]] || die "GITHUB_REPOSITORY is not set"
  KNOWN_SECRETS=$(gh secret list --repo "${GITHUB_REPOSITORY}" --json name \
    --jq '.[].name')
  for group in "$@"; do
    ensure_group "${group}"
  done
  echo "created=${CREATED[*]:-}" >>"${GITHUB_OUTPUT:-/dev/null}"
  if ((${#CREATED[@]} > 0)); then
    echo "::notice::Created signing secrets: ${CREATED[*]}. They are visible" \
      "to the next run, which is started automatically."
  fi
}

main "$@"
