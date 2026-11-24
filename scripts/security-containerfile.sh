#!/bin/bash
#
# security-containerfile.sh - print the Containerfile of a security rebuild.
#
# The rebuild always starts FROM the stable image by digest, never from the
# previous security image: nothing accumulates and each build is reproducible
# (stable + the state of the security archive). Only the packages reported by
# security-detect.sh are upgraded, each pinned to the exact version detected,
# so the build records what it installed.
#
# Arguments:
#   $1 - image reference without tag or digest (ghcr.io/<owner>/<repo>)
#   $2 - digest of the stable image (sha256:<hex>)
# Inputs:
#   stdin: output of security-detect.sh ("<package> <old> <new>" lines)
# Outputs:
#   The Containerfile on stdout.
# Returns:
#   1 when an argument is invalid or stdin lists no package.
set -euo pipefail

image="${1:?image required}"
digest="${2:?digest required}"

if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "::error::'${digest}' is not a sha256 digest" >&2
  exit 1
fi

pins=()
while read -r pkg _ new; do
  [[ -n "${pkg}" && -n "${new}" ]] || continue
  pins+=("${pkg}=${new}")
done

if ((${#pins[@]} == 0)); then
  echo "::error::no security upgrade given: nothing to rebuild" >&2
  exit 1
fi

cat <<EOF
FROM ${image}@${digest}
LABEL org.opencontainers.image.base.digest="${digest}"
LABEL org.daemoncores.security-upgrades="${pins[*]}"
RUN apt-get update \\
    && apt-get install --yes --no-install-recommends --only-upgrade \\
        ${pins[*]} \\
    && apt-get clean \\
    && rm -rf /var/lib/apt/lists/*
EOF
