#!/usr/bin/env bash
# Sign image references with cosign. cosign must already be installed.
#
# Input (environment):
#   REFS                newline-separated references, pinned by digest
#   COSIGN_PRIVATE_KEY  key material (empty: keyless GitHub OIDC)
#   COSIGN_PASSWORD     key password
set -euo pipefail

: "${REFS:?REFS is required}"

args=(--yes)
if [ -n "${COSIGN_PRIVATE_KEY:-}" ]; then
  args+=(--key env://COSIGN_PRIVATE_KEY)
else
  echo "::warning::no COSIGN_PRIVATE_KEY secret: signing keyless." \
    "Hosts cannot enforce a keyless signature with containers-image."
fi

while IFS= read -r ref; do
  [ -n "${ref}" ] || continue
  # Registry references are lowercase; repository names may not be.
  ref="${ref,,}"
  if [[ ! "${ref}" =~ ^[a-z0-9._:/-]+@sha256:[0-9a-f]{64}$ ]]; then
    echo "::error::reference is not pinned by digest: ${ref}" >&2
    exit 1
  fi
  cosign sign "${args[@]}" "${ref}"
done <<< "${REFS}"
