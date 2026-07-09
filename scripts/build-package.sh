#!/bin/bash
# build-package.sh — Centralized per-package build dispatcher
# Usage: build-package.sh <package_name> [packages.yml path]
#
# Reads the package entry from packages.yml (default: ./packages.yml at the
# repo root already checked out by the caller workflow) and dispatches to the
# appropriate build handler based on the `type` field.
#
# Supported types:
#   build             — local Debian package with sources under
#                       workflows/bootc-debs-builder/<name>postinstall/
#   repack            — download the upstream .deb, extract it, apply the
#                       postinstall overlay from
#                       workflows/bootc-debs-builder/<name>postinstall/, then
#                       repack with the configured suffix.
#   build_from_source — delegate to a project-specific build script at
#                       workflows/bootc-debs-builder/build-<name>.sh. The
#                       script is invoked with the package's full entry from
#                       packages.yml exported as PKG_NAME, PKG_TYPE,
#                       PKG_VERSION, PKG_BRANCH, PKG_COMMIT, PKG_SUFFIX and
#                       PKG_VENDOR_SHA256 environment variables, plus the
#                       shared CARGO_HOME, RUSTUP_HOME, SAVE_PWD and
#                       /output, /debs, /build locations. If the script is
#                       absent the step fails with a clear message.
#
# Requires: yq (v4) for YAML parsing. Installed by the workflow before calling.

set -euo pipefail

PKG_NAME="${1:?Usage: $0 <package_name> [packages.yml]}"
MANIFEST="${2:-./packages.yml}"
SAVE_PWD="$(pwd)"

# Resolve the package entry from packages.yml.
if ! command -v yq >/dev/null 2>&1; then
  echo "ERROR: yq is required to parse ${MANIFEST}" >&2
  exit 1
fi

pkg_filter=".packages[] | select(.name == \"${PKG_NAME}\")"
PKG_ENTRY="$(yq -o=json -r "${pkg_filter}" "${MANIFEST}")"
if [ -z "${PKG_ENTRY}" ]; then
  echo "ERROR: package '${PKG_NAME}' not found in ${MANIFEST}" >&2
  exit 1
fi

PKG_TYPE="$(printf '%s' "${PKG_ENTRY}" | yq -r '.type')"
PKG_VERSION="$(printf '%s' "${PKG_ENTRY}" | yq -r '.version // ""')"
PKG_BRANCH="$(printf '%s' "${PKG_ENTRY}" | yq -r '.branch // ""')"
PKG_COMMIT="$(printf '%s' "${PKG_ENTRY}" | yq -r '.commit // ""')"
PKG_SUFFIX="$(printf '%s' "${PKG_ENTRY}" | yq -r '.suffix // ""')"
PKG_VENDOR_SHA256="$(printf '%s' "${PKG_ENTRY}" | yq -r '.vendor_sha256 // ""')"

export PKG_NAME PKG_TYPE PKG_VERSION PKG_BRANCH PKG_COMMIT PKG_SUFFIX \
  PKG_VENDOR_SHA256 SAVE_PWD
export CARGO_HOME="${CARGO_HOME:-/build/rust}"
export RUSTUP_HOME="${RUSTUP_HOME:-/build/rust}"

POSTINSTALL_DIR="${SAVE_PWD}/workflows/bootc-debs-builder/\
${PKG_NAME}postinstall"

case "${PKG_TYPE}" in
  build)
    echo "[build-package] type=build pkg=${PKG_NAME} version=${PKG_VERSION}"
    [ -n "${PKG_VERSION}" ] || {
      echo "ERROR: 'build' package ${PKG_NAME} requires a version \
field in packages.yml" >&2
      exit 1
    }
    [ -d "${POSTINSTALL_DIR}" ] || {
      echo "ERROR: postinstall dir ${POSTINSTALL_DIR} not found" >&2
      exit 1
    }

    mkdir -p "/output/${PKG_NAME}"
    cp -r "${POSTINSTALL_DIR}/"* "/output/${PKG_NAME}/"

    # Mark executables under usr/local/bin or usr/sbin as executable.
    find "/output/${PKG_NAME}" -path '*/bin/*' -o -path '*/sbin/*' \
      -type f -exec chmod 755 {} +

    sed -i \
      -e "s|{{ VER }}|${PKG_VERSION}|g" \
      -e "s|, {{ DEPS }}||g" \
      "/output/${PKG_NAME}/DEBIAN/control"

    dpkg-deb --build "/output/${PKG_NAME}" \
      "/debs/${PKG_NAME}_${PKG_VERSION}_all.deb"
    ;;

  repack)
    echo "[build-package] type=repack pkg=${PKG_NAME} suffix=${PKG_SUFFIX}"
    [ -n "${PKG_SUFFIX}" ] || {
      echo "ERROR: 'repack' package ${PKG_NAME} requires a suffix \
field in packages.yml" >&2
      exit 1
    }
    [ -d "${POSTINSTALL_DIR}" ] || {
      echo "ERROR: postinstall dir ${POSTINSTALL_DIR} not found" >&2
      exit 1
    }

    apt update
    cd /tmp
    apt-get download "${PKG_NAME}"

    mkdir -p "/output/${PKG_NAME}"
    dpkg-deb -R "/tmp/${PKG_NAME}"_*.deb "/output/${PKG_NAME}"

    ORIG_VER="$(dpkg-deb -f /tmp/"${PKG_NAME}"_*.deb Version)"
    ARCH="$(dpkg-deb -f /tmp/"${PKG_NAME}"_*.deb Architecture)"

    cp -r "${POSTINSTALL_DIR}/"* "/output/${PKG_NAME}/"

    # If the project ships a custom build hook for this package, run it after
    # the postinstall overlay has been applied. The hook receives the same
    # environment as build_from_source scripts.
    HOOK="${SAVE_PWD}/workflows/bootc-debs-builder/build-${PKG_NAME}.sh"
    if [ -x "${HOOK}" ]; then
      echo "[build-package] running custom repack hook: ${HOOK}"
      "${HOOK}"
    fi

    # Regenerate md5sums after content modification.
    # find | xargs relies on whitespace-free package file names; switching to
    # -print0/-0 would change how odd names are handled, so behavior is kept.
    # shellcheck disable=SC2038
    find "/output/${PKG_NAME}" -not -path '*/DEBIAN/*' -type f \
      | xargs md5sum \
      | sed "s|/output/${PKG_NAME}/||" \
        >"/output/${PKG_NAME}/DEBIAN/md5sums"

    sed -i "s/^Version: .*/Version: 1:${ORIG_VER}${PKG_SUFFIX}/" \
      "/output/${PKG_NAME}/DEBIAN/control"

    dpkg-deb --build "/output/${PKG_NAME}" \
      "/debs/${PKG_NAME}_${ORIG_VER}${PKG_SUFFIX}_${ARCH}.deb"
    ;;

  build_from_source)
    echo "[build-package] type=build_from_source pkg=${PKG_NAME} \
version=${PKG_VERSION}"
    SCRIPT="${SAVE_PWD}/workflows/bootc-debs-builder/build-${PKG_NAME}.sh"
    if [ ! -x "${SCRIPT}" ]; then
      echo "ERROR: build_from_source package '${PKG_NAME}' requires \
an executable build script at:" >&2
      echo "       ${SCRIPT}" >&2
      echo "       The script receives PKG_NAME, PKG_TYPE, \
PKG_VERSION, PKG_BRANCH," >&2
      echo "       PKG_COMMIT, PKG_SUFFIX, PKG_VENDOR_SHA256, \
CARGO_HOME, RUSTUP_HOME," >&2
      echo "       SAVE_PWD env vars and must drop the built .deb \
into /debs/." >&2
      exit 1
    fi
    "${SCRIPT}"
    ;;

  *)
    echo "ERROR: unknown package type '${PKG_TYPE}' for package \
'${PKG_NAME}'" >&2
    echo "       Supported types: build, repack, build_from_source" >&2
    exit 1
    ;;
esac

echo "[build-package] done: ${PKG_NAME}"
