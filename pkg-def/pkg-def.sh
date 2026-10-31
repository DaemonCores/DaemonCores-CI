#!/bin/bash
#
# pkg-def.sh - read one package definition of the expanded manifest and append
# it as step outputs (KEY=VALUE, plus heredoc env and build) to GITHUB_OUTPUT.
# Shared by the per-package build job and the deb plan job so both see the same
# upstream, deb-glob, deps, sources, signs, env and build.
#
# Consumed environment: MAN (expanded manifest path), PKG (package name), ARCH,
# GITHUB_OUTPUT, and SECRETVAL_<NAME> for each secret-env entry.
set -euo pipefail
command -v yq >/dev/null 2>&1 || {
  # Arch-aware yq download: pick the binary matching the target arch
  # (amd64 or arm64). Falls back to the host arch when ARCH is unset.
  yarch="${ARCH:-$(dpkg --print-architecture 2>/dev/null || uname -m)}"
  case "$yarch" in
    aarch64) yarch=arm64 ;;
    x86_64)  yarch=amd64 ;;
  esac
  # Pin yq to a fixed version and verify the SHA256 checksum so a
  # compromised or tampered release cannot replace the binary
  # silently. Downloading from releases/latest/download/ with no
  # checksum lets a upstream-tagged malicious binary run as root
  # in the build container.
  YQ_VERSION="v4.44.3"
  YQ_BINARY="yq_linux_${yarch}"
  case "$yarch" in
    amd64)
      YQ_SHA256="a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7"
      ;;
    arm64)
      YQ_SHA256="0e7e1524f68d91b3ff9b089872d185940ab0fa020a5a9052046ef10547023156"
      ;;
    *)
      echo "ERROR: unsupported yq arch: ${yarch}" >&2
      exit 1
      ;;
  esac
  curl -fsSL \
    "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/${YQ_BINARY}" \
    -o /usr/local/bin/yq
  echo "${YQ_SHA256}  /usr/local/bin/yq" | sha256sum -c -
  chmod +x /usr/local/bin/yq
}
sel=".packages[] | select(.name == \"${PKG}\")"
{
  echo "upstream=$(yq "${sel} | .upstream // \"\"" "${MAN}")"
  echo "deb_glob=$(yq "${sel} | .\"deb-glob\" // \"\"" "${MAN}")"
  echo "deps=$(yq "${sel} | (.deps // []) | .[]" "${MAN}" \
    | paste -sd' ' -)"
  # sources are stored relative to the manifest dir; make them
  # workspace-relative for build-deb.
  echo "sources=$(yq "${sel} | .sources[]" "${MAN}" \
    | sed 's#^#workflows/bootc-debs-builder/#' | paste -sd' ' -)"
  # Only a package whose build calls kernel_signing_setup depends on
  # the signing flag, so only its content hash may carry it. That
  # call is also what marks a package compiling the kernel: it gets
  # the compiler cache (see "Enable ccache").
  if yq "${sel} | .build" "${MAN}" \
    | grep -q 'kernel_signing_setup'; then
    echo "signs=yes"
  else
    echo "signs=no"
  fi
} >>"$GITHUB_OUTPUT"
# env = non-secret KEY=VALUE lines, then resolved secret-env lines.
envlines=$(yq "${sel} | (.env // {}) | to_entries | .[] \
  | .key + \"=\" + .value" "${MAN}")
# Native patch data (Gabriel, 30/10/2026): an entry may carry a
# `patches:` key — the canonical declarative patch schema (same
# structure as a patches.yml file), as compact one-line JSON in
# PATCHES_JSON. It rides in EXTRA_ENV: exported for the build AND
# hashed into the content hash (non-secret). The kernel helper
# reads it as first-class data; a patches.yml file path stays
# equally supported.
patches_json=$(yq "${sel} | (.patches // []) | select(length > 0)" \
  -o=json -I=0 "${MAN}")
if [ "${patches_json}" != "[]" ] && [ -n "${patches_json}" ]; then
  envlines="${envlines}"$'\n'"PATCHES_JSON=${patches_json}"
fi
while IFS= read -r pair; do
  [ -n "${pair}" ] || continue
  key="${pair%%=*}"
  ref="SECRETVAL_${pair#*=}"
  val="${!ref}"
  # Mask the resolved secret value BEFORE it can appear in any log
  # output (the build-deb action logs its `env` input, and the value
  # is also written to $GITHUB_OUTPUT below). ::add-mask:: must be
  # emitted before the value is referenced anywhere visible.
  echo "::add-mask::${val}"
  envlines="${envlines}"$'\n'"${key}=${val}"
done < <(yq "${sel} | (.\"secret-env\" // {}) | to_entries | .[] \
  | .key + \"=\" + .value" "${MAN}")
{
  echo "env<<ENV_EOF"
  printf '%s\n' "${envlines}"
  echo "ENV_EOF"
  echo "build<<BUILD_EOF"
  yq "${sel} | .build" "${MAN}"
  echo "BUILD_EOF"
} >>"$GITHUB_OUTPUT"
