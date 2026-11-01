#!/bin/bash
#
# lib.sh - functions of the image boot test, sourced by image-test.sh. It holds
# no top-level action: sourcing it only sets defaults and defines functions.
#
# `exit` is used inside these functions on purpose (set -e style harness): a
# caller that wants to test several images must run `image-test.sh test` as a
# separate child process (under `timeout`), never source this file in a loop.
#
# Consumed environment:
#   IMAGE       local image ref under test (set by `image-test.sh test`)
#   TESTS_FILE  path to the tests manifest (yaml)
#   MEM         VM memory in MiB (default 4096)
#   VCPUS       VM vCPUs (default 2)
#   ARCH        target architecture: amd64 (default) or arm64
#   IMAGE_TEST_STATE  state directory shared by prepare/test/teardown
#               (default ${RUNNER_TEMP:-/tmp}/image-test)
#   RAM_IDLE_MAX_MB  idle RAM budget in MiB: the boot-fresh measurement must
#               stay under it or the run fails (default 15; Gabriel 30/10:
#               the gate holds the idle footprint at 10-15 MiB).
#   SECURE_BOOT forces Secure Boot on (1) or off (0) and wins over the manifest
#   SSH_PORT    host port forwarded to the guest sshd (default 2222)
#   STOP_GRACE_S  seconds a guest gets to power off before QEMU is killed (60)

ARCH_FROM_ENV="${ARCH:-}"
ARCH="${ARCH:-amd64}"
RAM_IDLE_MAX_MB="${RAM_IDLE_MAX_MB:-15}"
IMAGE="${IMAGE:-}"
VARIANT="main"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# State shared by the sub-commands of one job: the SSH key, the Secure Boot
# tooling and key hierarchy, the varstore cache, the per-image scratch files.
STATE="${IMAGE_TEST_STATE:-${RUNNER_TEMP:-/tmp}/image-test}"
STATE_ENV="${STATE}/state.env"
LOGS="${STATE}/logs"
DISK="${STATE}/test-disk.raw"
KEY="${STATE}/test-key"
SERIAL="${STATE}/serial.log"
MONITOR="${STATE}/monitor.sock"
PIDFILE="${STATE}/qemu.pid"
VARS_RUN="${STATE}/OVMF_VARS.fd"
VARSTORES="${STATE}/varstore"
SBTOOL="${STATE}/sbtool"
VIRT_FW="${SBTOOL}/bin/virt-fw-vars"
SSH_PORT="${SSH_PORT:-2222}"
STOP_GRACE_S="${STOP_GRACE_S:-60}"
# OVMF firmware directory: Debian/Ubuntu ship real files under /usr/share/OVMF;
# Arch/edk2 puts them under /usr/share/edk2/x64 (on Arch /usr/share/OVMF is a
# symlink that find does not traverse without -L, so list both paths explicitly).
# The runner environment may already know the firmware tree (an artefact-only
# local replay points OVMF_DIR at the unpacked firmware instead of /usr/share).
OVMF_DIR="${OVMF_DIR:-}"
for _d in /usr/share/OVMF /usr/share/edk2/x64; do
  [ -n "${OVMF_DIR}" ] && break

  if find "${_d}" -maxdepth 2 -name 'OVMF_CODE*.fd' 2>/dev/null | grep -q .; then
    OVMF_DIR="${_d}"; break
  fi
done
unset _d
# SECURE_BOOT=1 boots the VM with UEFI Secure Boot enforced, using a varstore
# build_sb_vars generates from scratch (our own PK/KEK + the public Microsoft
# UEFI CAs so the MS-signed shim validates + our MOK for grub, no revocation
# dbx). The former 0x1A Security Violation was not a QEMU/OVMF defect: the
# shim's built-in SBAT policy revoked a grub carrying no (or a too old) .sbat
# section; the grub is now built with --sbat (see DEBUG.md). The MOK carries the
# image signing certificate (grub and kernel are signed with that key) and the
# certificates of the image's mok.d, as the installer enrolls them.
# The value comes from the `secure_boot:` of the manifest category the image
# belongs to (select_category); a SECURE_BOOT already in the environment wins,
# for a local run. A manifest with no `categories:` keeps the former default, 0.
SECURE_BOOT="${SECURE_BOOT:-}"
# Manifest category of the image under test and whether it is the reference one
# (set by select_category). A CATEGORY already in the environment selects that
# category among those matching the tag.
CATEGORY="${CATEGORY:-}"
# Multiplier applied to the manifest timeouts (tuned for KVM) when the guest is
# emulated (TCG). Back to 5: the 330 s boots came from -cpu max, a neoverse-n1
# arm64 guest reaches SSH in about 105 s (run 41191163442).
TCG_TIMEOUT_SCALE="${TCG_TIMEOUT_SCALE:-5}"
REFERENCE=0
# 1 once we know /dev/kvm is present and usable, 0 otherwise (set in
# install_deps, persisted by save_state). GitHub's hosted arm64 runners expose
# no /dev/kvm at all - nested virtualization is not available on that fleet - so
# the arm64 boot test runs under QEMU TCG emulation instead of KVM.
KVM=0
# Secure Boot key hierarchy, created once by setup_sb_tooling (prepare).
SB_GUID=""
SB_CERTS=""
SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=3
  -i "${KEY}"
  -p "${SSH_PORT}"
)

#######################################
# Create the state directory (private: it holds the SSH and PK/KEK keys) and
# its sub-directories. Idempotent.
# Globals:
#   STATE, LOGS, VARSTORES
#######################################
state_init() {
  case "${STATE}" in
    "" | /) echo "::error::refusing to use '${STATE}' as the image-test state directory"; return 1 ;;
  esac
  (umask 077 && mkdir -p "${STATE}" "${LOGS}" "${VARSTORES}")
}

#######################################
# Persist what prepare detected (arch, KVM, firmware dir, Secure Boot guid and
# certificate directory, manifest path) so `test` needs no re-detection.
# Values are written with printf %q and read back with `source`.
# Globals:
#   STATE_ENV, ARCH, KVM, OVMF_DIR, SB_GUID, SB_CERTS, TESTS_FILE
#######################################
save_state() {
  local v
  : >"${STATE_ENV}"
  for v in ARCH KVM OVMF_DIR SB_GUID SB_CERTS TESTS_FILE; do
    printf '%s=%q\n' "${v}" "${!v-}" >>"${STATE_ENV}"
  done
}

#######################################
# Load the state written by prepare. A TESTS_FILE or ARCH already in the
# environment wins over the persisted one.
# Globals:
#   STATE_ENV, ARCH, TESTS_FILE and everything save_state writes
# Returns:
#   1 when prepare was not run for this state directory.
#######################################
load_state() {
  local env_arch="${ARCH_FROM_ENV-}" env_tests="${TESTS_FILE-}"
  if [ ! -f "${STATE_ENV}" ]; then
    echo "::error::no prepared state in ${STATE} (run 'image-test.sh prepare' first)"
    return 1
  fi
  # shellcheck disable=SC1090
  source "${STATE_ENV}"
  [ -z "${env_arch}" ] || ARCH="${env_arch}"
  [ -z "${env_tests}" ] || TESTS_FILE="${env_tests}"
  if [ -z "${TESTS_FILE:-}" ]; then
    echo "::error::TESTS_FILE is empty (neither in the environment nor in the prepared state)"
    return 1
  fi
}

#######################################
# Create the SSH key the images are installed with, once per job.
# Globals:
#   KEY
#######################################
make_ssh_key() {
  # Overwrite any artefacts of a previous run non-interactively (a stale key
  # makes ssh-keygen prompt, which hangs the runner).
  rm -f "${KEY}" "${KEY}.pub"
  ssh-keygen -t ed25519 -N '' -f "${KEY}" -q
}

#######################################
# True when some image of this arch will boot with Secure Boot enforced, so
# prepare knows whether to build the Secure Boot tooling: SECURE_BOOT=1 in the
# environment, or a manifest category of this arch with secure_boot: 1.
# SECURE_BOOT=0 in the environment turns it off for every image.
# Globals:
#   SECURE_BOOT, ARCH, TESTS_FILE
#######################################
manifest_needs_secure_boot() {
  local n
  [ "${SECURE_BOOT}" = 0 ] && return 1
  [ "${SECURE_BOOT}" = 1 ] && return 0
  n=$(yq "[.categories[]? | select(.arch == \"${ARCH}\" and .secure_boot == 1)] | length" "${TESTS_FILE}")
  [ "${n:-0}" -gt 0 ]
}

#######################################
# One-time Secure Boot tooling: the virt-firmware venv and our PK/KEK. The
# per-image part (certificates of the image) is build_sb_vars.
# Globals:
#   SBTOOL, STATE, SB_GUID, SB_CERTS
#######################################
setup_sb_tooling() {
  local k
  # virt-firmware builds the Secure Boot varstore (it bundles the Microsoft
  # UEFI CAs); openssl generates our own PK/KEK. Kept in a venv so the runner's
  # system Python is untouched (PEP 668).
  if ! python3 -c 'import ensurepip, venv' 2>/dev/null \
    || ! command -v openssl >/dev/null 2>&1; then
    sudo apt-get install -y --no-install-recommends python3-venv openssl
  fi
  python3 -m venv "${SBTOOL}"
  "${SBTOOL}/bin/pip" install --quiet virt-firmware
  SB_CERTS=$("${SBTOOL}/bin/python" -c \
    'import os,virt.firmware as v; print(os.path.join(
     os.path.dirname(v.__file__), "certs/microsoft.com"))')
  SB_GUID=$(cat /proc/sys/kernel/random/uuid)
  for k in PK KEK; do
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
      -subj "/CN=debian-bootc test ${k}/" \
      -keyout "${STATE}/${k}.key" -out "${STATE}/${k}.pem" 2>/dev/null
  done
}

#######################################
# Install the image to DISK, injecting the job's root SSH key. The disk is
# recreated for every image: a reused disk would carry the previous install.
# Globals:
#   IMAGE, DISK, KEY, SCRIPT_DIR, ARCH
#######################################
install_disk() {
  rm -f "${DISK}"
  # Sparse: only written blocks consume host space (~3G after install), but 10G
  # of virtual size is already ample for the ~3G deploy + boot + ESP.
  truncate -s 10G "${DISK}"
  # The disk is partitioned + installed by install-fs.sh, run privileged INSIDE
  # the image so it can use the image's own tools and bootc install
  # to-filesystem into the same layout as the ISO (btrfs pool + varlog quota).
  # bootc-build.yml builds with sudo podman, so this root podman sees the freshly
  # built image directly (no registry pull).
  # ARCH is forwarded so install-fs.sh can set the right console= kernel arg
  # (ttyS0 on x86, ttyAMA0 on arm64 QEMU virt machine). INSTALL_KARGS carries
  # the extra kernel arguments of the manifest category (select_category).
  sudo podman run --rm --privileged --pid=host --security-opt label=disable \
    -e ARCH="${ARCH}" -e INSTALL_KARGS="${INSTALL_KARGS:-}" \
    -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
    -v "${DISK}:/disk.raw" \
    -v "${KEY}.pub:/key.pub:ro" \
    -v "${SCRIPT_DIR}/install-fs.sh:/install-fs.sh:ro" \
    -v "${SCRIPT_DIR}/files:/files:ro" \
    "${IMAGE}" \
    bash /install-fs.sh
}

#######################################
# Copy the Secure Boot inputs out of an image: its signing certificate and the
# public certificates of its mok.d.
# Arguments:
#   $1 - destination directory (gets sb.crt and mok.d/)
# Globals:
#   IMAGE
#######################################
fetch_image_certs() {
  local dest="$1"
  mkdir -p "${dest}/mok.d"
  # Our image's own Secure Boot signing certificate (the key that signs grub).
  # The redirect target is writable by the invoking user, so the non-sudo
  # redirect is intended - sudo only elevates podman.
  # shellcheck disable=SC2024
  sudo podman run --rm "${IMAGE}" \
    cat /usr/share/debian-bootc/sb_signing.crt >"${dest}/sb.crt"
  # The certificates the installer's mok-stage enrolls besides the signing one:
  # the public ones dropped in the image's mok.d (a fork's own key).
  if sudo podman run --rm "${IMAGE}" test -d /usr/share/debian-bootc/mok.d; then
    sudo podman run --rm "${IMAGE}" tar -C /usr/share/debian-bootc -c mok.d \
      | tar -C "${dest}" -x
  fi
}

#######################################
# Fingerprint of everything the varstore depends on that comes from the image:
# sb.crt and the files of mok.d (names and contents), plus the arch. Two images
# with the same fingerprint get the same varstore.
# Arguments:
#   $1 - directory holding sb.crt and mok.d/
# Outputs:
#   The sha256 hex digest on stdout.
#######################################
sb_fingerprint() {
  local dir="$1"
  {
    printf 'arch %s\n' "${ARCH}"
    printf 'sb.crt %s\n' "$(sha256sum <"${dir}/sb.crt" | cut -d' ' -f1)"
    if [ -d "${dir}/mok.d" ]; then
      (cd "${dir}/mok.d" && find . -type f -print0 | LC_ALL=C sort -z \
        | xargs -0 -r sha256sum)
    fi
  } | sha256sum | cut -d' ' -f1
}

#######################################
# Build a UEFI Secure Boot varstore from scratch - our own key hierarchy, no
# distro *.ms template (those carry Microsoft's BlackLotus revocation dbx that
# rejects even a current, valid shim). Writes the given file:
#   PK/KEK  - generated once by prepare, ours
#   db      - public Microsoft UEFI CA 2011 + 2023 (bundled by virt-firmware) so
#             the MS-signed shim validates, plus our own signing cert
#   MokList - our debian-bootc signing cert so shim validates our grub, and the
#             public certificates of the image's mok.d, PEM or DER, never a file
#             holding a private key
# Arguments:
#   $1 - directory holding the image's sb.crt and mok.d/
#   $2 - output varstore path
# Globals:
#   ARCH, OVMF_DIR, VIRT_FW, STATE, SB_GUID, SB_CERTS
#######################################
build_sb_vars() {
  local certdir="$1" out="$2" base k conv
  local -a mok_args
  mok_args=(--add-mok "${SB_GUID}" "${certdir}/sb.crt")
  conv="${certdir}/mok-pem"
  mkdir -p "${conv}"
  for k in "${certdir}"/mok.d/*; do
    [ -f "${k}" ] || continue
    case "${k}" in *.crt | *.cer | *.pem | *.der) ;; *) continue ;; esac
    grep -q 'PRIVATE KEY' "${k}" && continue
    openssl x509 -in "${k}" -outform PEM -out "${conv}/${k##*/}.pem" 2>/dev/null \
      || openssl x509 -inform DER -in "${k}" -outform PEM -out "${conv}/${k##*/}.pem" \
      || { echo "::error::mok.d/${k##*/} is not a certificate"; exit 1; }
    mok_args+=(--add-mok "${SB_GUID}" "${conv}/${k##*/}.pem")
  done
  if [ "${ARCH}" = "arm64" ]; then
    base=$(find /usr/share/AAVMF -name 'AAVMF_VARS*.fd' ! -name '*.ms.*' \
      ! -name '*.snakeoil.*' | sort | head -1)
  else
    base=$(find "${OVMF_DIR}" -name 'OVMF_VARS*.fd' ! -name '*.ms.*' \
      ! -name '*secboot*' | head -1)
  fi
  if [ -z "${base}" ]; then
    echo "::error::no blank UEFI varstore to build the Secure Boot one from"
    exit 1
  fi
  "${VIRT_FW}" -i "${base}" -o "${out}" \
    --set-pk "${SB_GUID}" "${STATE}/PK.pem" \
    --add-kek "${SB_GUID}" "${STATE}/KEK.pem" \
    --add-db "${SB_GUID}" "${SB_CERTS}/ms-uefi-2011.pem" \
    --add-db "${SB_GUID}" "${SB_CERTS}/ms-uefi-2023.pem" \
    --add-db "${SB_GUID}" "${certdir}/sb.crt" \
    "${mok_args[@]}" \
    --secure-boot
}

#######################################
# Give the VM about to boot a PRISTINE copy of the Secure Boot varstore for the
# image's certificates. The cache is keyed by sb_fingerprint and rebuilt with
# build_sb_vars only on a miss; the VM never touches the cached file, because
# a reused NVRAM would leak BootOrder / MokListRT from one image to the next.
# Arguments:
#   $1 - directory holding the image's sb.crt and mok.d/
# Globals:
#   VARSTORES, VARS_RUN, VIRT_FW
#######################################
prepare_sb_varstore() {
  local certdir="$1" fp cached tmp
  if [ ! -x "${VIRT_FW}" ]; then
    echo "::error::Secure Boot needed but ${VIRT_FW} is missing (prepare found no secure_boot category for ${ARCH})"
    return 1
  fi
  fp=$(sb_fingerprint "${certdir}")
  cached="${VARSTORES}/${fp}/OVMF_VARS.fd"
  if [ -f "${cached}" ]; then
    echo "Secure Boot varstore: cache hit (${fp})"
  else
    echo "Secure Boot varstore: cache miss, building (${fp})"
    mkdir -p "${VARSTORES}/${fp}"
    tmp="${cached}.tmp.$$"
    build_sb_vars "${certdir}" "${tmp}"
    mv -f "${tmp}" "${cached}"
  fi
  rm -f "${VARS_RUN}"
  cp "${cached}" "${VARS_RUN}"
}

#######################################
# Install QEMU/OVMF/yq and grant the runner access to /dev/kvm.
# On Debian/Ubuntu: uses apt-get. On other distros: skips if already present.
#######################################
install_deps() {
  local need_qemu=0 need_jq=0
  if [ "${ARCH}" = "arm64" ]; then
    command -v qemu-system-aarch64 >/dev/null 2>&1 || need_qemu=1
  else
    command -v qemu-system-x86_64 >/dev/null 2>&1 || need_qemu=1
  fi
  command -v jq >/dev/null 2>&1 || need_jq=1
  if [ "${need_qemu}" = 1 ] || [ "${need_jq}" = 1 ]; then
    sudo apt-get update
    if [ "${ARCH}" = "arm64" ]; then
      sudo apt-get install -y --no-install-recommends qemu-system-arm qemu-efi-aarch64 jq
    else
      sudo apt-get install -y --no-install-recommends qemu-system-x86 ovmf jq
    fi
  fi
  command -v yq >/dev/null 2>&1 || {
    # Pinned version with SHA256 checksum — downloading from latest/download/
    # with no checksum lets a compromised upstream binary run in the runner.
    YQ_VERSION="v4.44.3"
    case "${ARCH}" in
      arm64)
        YQ_SHA256="0e7e1524f68d91b3ff9b089872d185940ab0fa020a5a9052046ef10547023156"
        ;;
      *)
        YQ_SHA256="a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7"
        ;;
    esac
    sudo curl -fsSL \
      "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${ARCH}" \
      -o /usr/local/bin/yq
    echo "${YQ_SHA256}  /usr/local/bin/yq" | sha256sum -c -
    sudo chmod +x /usr/local/bin/yq
  }
  # /dev/kvm must exist BEFORE udevadm trigger --name-match=kvm: that option is
  # resolved through find_device("kvm", "/dev"), which fails with "Failed to open
  # the device 'kvm': Invalid argument" when the node is absent — and under the
  # `set -e` above that killed the whole script, which is why every arm64 job
  # died right here with no usable message.
  if [ -c /dev/kvm ]; then
    echo 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' \
      | sudo tee /etc/udev/rules.d/99-kvm4all.rules >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=kvm
    KVM=1
  else
    KVM=0
    if [ "${ARCH}" != "arm64" ]; then
      # GitHub's x86_64 fleet always exposes KVM; its absence means the runner
      # is not what we expect. TCG-emulating an x86_64 boot test would be far too
      # slow to be meaningful, so fail loudly rather than limp along.
      echo "::error::/dev/kvm is missing on an amd64 runner; the boot test needs KVM acceleration"
      exit 1
    fi
    # Hosted arm64 runners expose no /dev/kvm: nested virtualization is not
    # available on that fleet (actions/runner-images#14062). Boot the VM under
    # QEMU TCG emulation instead — slower, but the only path on this hardware.
    echo "::warning::/dev/kvm unavailable on arm64; booting the VM under QEMU TCG emulation"
  fi

  # Refresh OVMF_DIR after ovmf may have been installed above: the global
  # detection at the top of the script runs before install_deps, so on a CI
  # runner where ovmf is not pre-installed OVMF_DIR would be empty. An
  # env-provided OVMF_DIR (artefact-only local replay) always wins.
  if [ "${ARCH}" != "arm64" ] && [ -z "${OVMF_DIR}" ]; then
    for _d in /usr/share/OVMF /usr/share/edk2/x64; do
      if find "${_d}" -maxdepth 2 -name 'OVMF_CODE*.fd' 2>/dev/null | grep -q .; then
        OVMF_DIR="${_d}"; break
      fi
    done
    unset _d
  fi
}

#######################################
# Boot the disk under QEMU/KVM with UEFI and an SSH port-forward. Enforces
# Secure Boot when SECURE_BOOT=1 (using the varstore copy prepare_sb_varstore
# put at VARS_RUN),
# otherwise runs in Setup Mode (SB not enforced).
# Globals:
#   DISK, SERIAL, SSH_PORT, VARS_RUN, PIDFILE, MEM, VCPUS, SECURE_BOOT
#######################################
boot_vm() {
  local code vars accel cpu
  if [ "${KVM}" = "1" ]; then
    accel="-enable-kvm"
    cpu="host"
  else
    # TCG: -cpu host is meaningless without KVM, so ask for the widest emulated
    # CPU instead (-cpu max is the aarch64 spelling of the same idea).
    accel=""
    cpu="max"
    # arm64 + QEMU 8.2.x TCG: -cpu max stalls the 16k kernel right after
    # "PM: genpd: Disabling unused power domains" (reproduced with that exact
    # QEMU; pauth/sve/sme/lpa2 off does not help, neoverse-n1/n2 and
    # cortex-a76 boot). Use a real server core model there; override with
    # TCG_CPU_ARM64 (set it to "max" to get the old behaviour).
    [ "${ARCH}" = "arm64" ] && cpu="${TCG_CPU_ARM64:-neoverse-n1}"
  fi
  if [ "${ARCH}" = "arm64" ]; then
    # arm64: QEMU virt machine, AAVMF (arm64 UEFI from qemu-efi-aarch64 package).
    # Ubuntu's package ships several flavours side by side (.secboot, .ms,
    # .snakeoil, .no-secboot), so the firmware is picked by name, never by
    # directory order: the enforcing build for SECURE_BOOT=1 (paired with the
    # varstore from build_sb_vars), the plain one otherwise.
    if [ "${SECURE_BOOT}" = 1 ]; then
      code=$(find /usr/share/AAVMF -name 'AAVMF_CODE.secboot.fd' | sort | head -1)
      vars="${VARS_RUN}"
    else
      code=$(find /usr/share/AAVMF -name 'AAVMF_CODE*.fd' ! -name '*.secboot.*' \
        ! -name '*.ms.*' ! -name '*.snakeoil.*' | sort | head -1)
      vars=$(find /usr/share/AAVMF -name 'AAVMF_VARS*.fd' ! -name '*.secboot.*' \
        ! -name '*.ms.*' ! -name '*.snakeoil.*' | sort | head -1)
    fi
    if [ -z "${code}" ] || [ -z "${vars}" ]; then
      echo "::error::no matching AAVMF firmware pair under /usr/share/AAVMF"
      exit 1
    fi
    echo "AAVMF CODE: ${code}"
    echo "AAVMF VARS: ${vars}"
    [ "${SECURE_BOOT}" = 1 ] || cp "${vars}" "${VARS_RUN}"
    sudo qemu-system-aarch64 \
      ${accel} -m "${MEM:-4096}" -smp "${VCPUS:-2}" \
      -machine virt -cpu "${cpu}" \
      -drive "if=pflash,format=raw,unit=0,readonly=on,file=${code}" \
      -drive "if=pflash,format=raw,unit=1,file=${VARS_RUN}" \
      -drive "file=${DISK},format=raw,if=virtio" \
      -netdev "user,id=n0,hostfwd=tcp::${SSH_PORT}-:22" \
      -device "virtio-net,netdev=n0,romfile=" \
      -display none -serial "file:${SERIAL}" -monitor "unix:${MONITOR},server,nowait" \
      -daemonize -pidfile "${PIDFILE}"
  else
    local secure=off
    if [ "${SECURE_BOOT}" = 1 ]; then
      code=$(find "${OVMF_DIR}" -name 'OVMF_CODE*.fd' -name '*secboot*' \
        | head -1)
      secure=on
      # ${VARS_RUN} already copied by prepare_sb_varstore.
    else
      # Setup Mode: our grub's MOK is not enrolled in a fresh VM and distro *.ms
      # varstores carry a dbx that rejects the shim, so with SB off we validate the
      # OS boot path. Ref: Ubuntu UEFI/OVMF file naming.
      # Prefer the 4M firmware variants: OVMF_CODE_4M.fd (4 MB) includes the
      # VirtIO-BLK and VirtIO-SCSI UEFI drivers that let OVMF discover the
      # VirtIO disk and boot from it. The 2 MB OVMF_CODE.fd (legacy) omits those
      # drivers; OVMF then reports "No bootable option or device was found" because
      # it cannot enumerate the disk at all. Fallback to any non-secboot CODE if
      # the 4M variant is absent (unlikely on Ubuntu 24.04+).
      code=$(find "${OVMF_DIR}" -name 'OVMF_CODE_4M*.fd' ! -name '*secboot*' \
        ! -name '*.ms.*' | head -1)
      code="${code:-$(find "${OVMF_DIR}" -name 'OVMF_CODE*.fd' ! -name '*secboot*' \
        ! -name '*.ms.*' | head -1)}"
      vars=$(find "${OVMF_DIR}" -name 'OVMF_VARS_4M*.fd' ! -name '*.ms.*' \
        ! -name '*secboot*' ! -name '*snakeoil*' | sort | head -1)
      vars="${vars:-$(find "${OVMF_DIR}" -name 'OVMF_VARS*.fd' ! -name '*.ms.*' \
        ! -name '*secboot*' ! -name '*snakeoil*' | sort | head -1)}"
      if [ -z "${code}" ] || [ -z "${vars}" ]; then
        echo "::error::no OVMF firmware found (OVMF_DIR=${OVMF_DIR:-<empty>});" \
          "install ovmf or point OVMF_DIR at the unpacked firmware files"
        exit 1
      fi
      echo "OVMF CODE: ${code}"
      echo "OVMF VARS: ${vars}"
      cp "${vars}" "${VARS_RUN}"
    fi
    sudo qemu-system-x86_64 \
      ${accel} -m "${MEM:-4096}" -smp "${VCPUS:-2}" -machine "q35,smm=${secure}" \
      -global "driver=cfi.pflash01,property=secure,value=${secure}" \
      -drive "if=pflash,format=raw,unit=0,readonly=on,file=${code}" \
      -drive "if=pflash,format=raw,unit=1,file=${VARS_RUN}" \
      -drive "file=${DISK},format=raw,if=virtio" \
      -netdev "user,id=n0,hostfwd=tcp::${SSH_PORT}-:22" \
      -device "virtio-net,netdev=n0,romfile=" \
      -display none -serial "file:${SERIAL}" -monitor "unix:${MONITOR},server,nowait" \
      -daemonize -pidfile "${PIDFILE}"
  fi
}

#######################################
# Poll SSH until the VM answers or the timeout elapses.
# Arguments:
#   $1 - timeout in seconds
#   $2 - optional boot id of the previous boot: a guest still reporting it is
#        the one being rebooted, not the new boot, and does not count
# Returns:
#   0 when reachable, 1 on timeout.
#######################################
wait_ssh() {
  local timeout="$1" old_boot="${2:-}" resets deadline _ssh_log boot
  # timeout is a ceiling, not a fixed sleep: probe until SSH answers. A single
  # probe after a fixed grace period failed on emulated (TCG) guests that were
  # still busy finishing boot: sshd listened, but the 3 s banner exchange of
  # SSH_OPTS expired ("Connection timed out during banner exchange"). Each
  # probe gets a longer ConnectTimeout (first -o wins over SSH_OPTS).
  echo "  … waiting up to ${timeout}s for SSH"
  local started=${SECONDS}
  deadline=$((SECONDS + timeout))
  _ssh_log=$(mktemp)
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    # Fail fast on definitive firmware failures.
    if grep -q 'Security Violation' "${SERIAL}" 2>/dev/null; then
      echo "::error::Secure Boot rejected a boot binary (Security Violation)"
      rm -f "${_ssh_log}"
      return 3
    fi
    resets=$(grep -c 'Reset System' "${SERIAL}" 2>/dev/null || echo 0)
    if [ "${resets}" -ge 3 ]; then
      echo "::error::guest firmware reboot loop (${resets}x Reset System)"
      rm -f "${_ssh_log}"
      return 2
    fi
    if boot=$(ssh -o ConnectTimeout=30 "${SSH_OPTS[@]}" -v root@localhost \
      cat /proc/sys/kernel/random/boot_id 2>"${_ssh_log}") \
      && [ -n "${boot}" ] && [ "${boot}" != "${old_boot}" ]; then
      rm -f "${_ssh_log}"
      echo "  … SSH reachable after $((SECONDS - started))s"
      return 0
    fi
    sleep 5
  done
  echo "  … no SSH answer within ${timeout}s; guest console tail:"
  sudo tail -n 100 "${SERIAL}" 2>/dev/null | sed 's/^/    | /' || true
  if nc -z -w 3 localhost "${SSH_PORT}" 2>/dev/null; then
    echo "  … port ${SSH_PORT} open"
  else
    echo "  … port ${SSH_PORT} CLOSED (no TCP connection)"
  fi
  echo "  … last SSH attempt — verbose output:"
  sed 's/^/    | /' "${_ssh_log}" || true
  rm -f "${_ssh_log}"
  return 1
}

#######################################
# Print what the guest CPU is doing: run state, then the program counter
# sampled three times one second apart. A fixed PC is a CPU parked idle (the
# guest waits for an event); a moving PC is a CPU still executing.
# Globals:
#   MONITOR, SCRIPT_DIR
#######################################
guest_state() {
  [[ -S "${MONITOR}" ]] || return 0
  echo "::group::guest state (QEMU monitor)"
  sudo python3 -I "${SCRIPT_DIR}/hmp.py" "${MONITOR}" \
    "info status" "info cpus" "info registers" 2>&1 | grep -aE '^(>|VM status|\*? ?CPU|.*PC=|PSTATE)' || true
  # A stalled guest: the whole register file (LR, SP give the call chain) and
  # the code around the program counter, to be matched against the kernel image.
  # This QEMU monitor does not know $pc/$sp as expressions: read the numbers
  # from the register dump and pass them as addresses.
  local regs pc sp
  regs=$(sudo python3 -I "${SCRIPT_DIR}/hmp.py" "${MONITOR}" "info registers" 2>&1)
  grep -aE '^(X[0-9]+|.* X[0-9]+=)' <<<"${regs}" || true
  pc=$(grep -aoE ' PC=[0-9a-f]+' <<<"${regs}" | head -n 1 | cut -d= -f2)
  sp=$(grep -aoE ' SP=[0-9a-f]+' <<<"${regs}" | head -n 1 | cut -d= -f2)
  if [ -n "${pc}" ] && [ -n "${sp}" ]; then
    sudo python3 -I "${SCRIPT_DIR}/hmp.py" "${MONITOR}" "gva2gpa 0x${pc}" "gva2gpa 0x${sp}" \
      "x /32xw 0x${pc}" "x /16xg 0x${sp}" 2>&1 \
      | grep -aE '^(gpa:|[0-9a-f]{16}:|Cannot|Unable|unknown|Try)' | cut -c1-200 || true
  fi
  local i
  for i in 1 2; do
    sleep 1
    sudo python3 -I "${SCRIPT_DIR}/hmp.py" "${MONITOR}" "info registers" 2>&1 \
      | grep -aE 'PC=|PSTATE' || true
  done
  echo "::endgroup::"
}

#######################################
# Print why a guest never became reachable: every failed unit and error line
# of the serial console, then its tail, with terminal escape sequences removed
# so the GitHub log stays readable.
# Globals:
#   SERIAL
#######################################
dump_serial() {
  local clean
  guest_state
  clean=$(sudo cat "${SERIAL}" 2>/dev/null \
    | sed -e 's/\x1b\[[0-9;?]*[A-Za-z]//g' -e 's/\r//g') || true
  echo "::group::serial console: failed units and errors"
  printf '%s\n' "${clean}" | grep -aE 'FAILED|Failed|error|Error|panic|Kernel panic|Call Trace' \
    | sort -u | head -n 80 || true
  echo "::endgroup::"
  # Network state the guest printed about itself at boot (ci-ssh-debug.service):
  # tells a guest without an address or route from a sshd that does not answer.
  echo "::group::serial console: guest network state (ip addr, ip route)"
  printf '%s\n' "${clean}" | sed -n '/ci-dbg: ip addr/,/ci-dbg: \/etc\/network\/interfaces/p' \
    | head -n 60 || true
  echo "::endgroup::"
  # The tail alone hides how the guest was started: show the kernel command
  # line, the first lines and the size of the capture, to tell a guest that
  # stopped talking from a capture that stopped growing.
  echo "::group::serial console: kernel command line, first lines, size"
  printf '%s\n' "${clean}" | grep -a 'Kernel command line' || true
  printf '%s\n' "${clean}" | head -n 40
  printf 'serial capture: %s lines, %s bytes\n' \
    "$(printf '%s\n' "${clean}" | wc -l)" "$(sudo stat -c %s "${SERIAL}" 2>/dev/null || echo 0)"
  echo "::endgroup::"
  echo "::group::serial console: last 200 lines"
  printf '%s\n' "${clean}" | tail -n 200
  echo "::endgroup::"
}

#######################################
# Idle RAM budget gate, measured boot-fresh: the VM just settled (wait_boot)
# and NOTHING else ran yet, so MemTotal - MemAvailable IS the idle
# footprint. The figure is always measured, broken down and added to the run
# summary so every image can be compared; exceeding RAM_IDLE_MAX_MB (default
# 15 MiB, an aspiration the owner keeps as optimisation pressure) is a warning,
# never a failure, unless RAM_IDLE_ENFORCE=1 turns the budget into a gate.
# Globals:
#   SSH_OPTS, RAM_IDLE_MAX_MB, RAM_IDLE_ENFORCE, IMAGE, ARCH
# Returns:
#   0, or 1 when RAM_IDLE_ENFORCE=1 and the idle footprint is over budget.
#######################################
ram_gate() {
  local readout used
  # 0 disables the gate (e.g. an image lane still far above the budget).
  if [ "${RAM_IDLE_MAX_MB}" = "0" ]; then
    echo "RAM idle gate disabled (RAM_IDLE_MAX_MB=0)"
    return 0
  fi
  # MemTotal (line 1) then MemAvailable (line 2), the fixed order of
  # /proc/meminfo. The awk program is single-quoted for the REMOTE shell:
  # $2 is escaped for the LOCAL shell so it only expands on the guest.
  readout=$(ssh "${SSH_OPTS[@]}" root@localhost \
    'awk "/^(MemTotal|MemAvailable):/{print \$2}" /proc/meminfo' 2>/dev/null \
    || true)
  if [ "$(echo "${readout}" | wc -l)" -lt 2 ]; then
    echo "::error::ram gate: MemTotal/MemAvailable not read back from the guest"
    echo "::error::ram gate (raw): ${readout:-<empty>}"
    exit 1
  fi
  # kB → MiB, ceil so a marginal over-budget never rounds its way past the gate.
  used=$(echo "${readout}" | awk 'NR==1{t=$1} NR==2{a=$1} END{print t-a}')
  used=$(( (used + 1023) / 1024 ))
  echo "RAM idle (boot-fresh): ${used} MiB MemTotal-MemAvailable used; budget: ${RAM_IDLE_MAX_MB} MiB"
  # Composition of the figure, computed on the guest (awk + coreutils only):
  # process RSS, kernel slabs, the min_free_kbytes reserve MemAvailable
  # subtracts, and what is left. One line per image in the run summary.
  local parts procs_kib slab_b reserve_kib
  parts=$(ssh "${SSH_OPTS[@]}" root@localhost '
    p=$(awk "/^VmRSS:/{s+=\$2} END{print s+0}" /proc/[0-9]*/status 2>/dev/null)
    sl=$(awk "NR>2{s+=\$3*\$4} END{print s+0}" /proc/slabinfo 2>/dev/null)
    r=$(cat /proc/sys/vm/min_free_kbytes 2>/dev/null || echo 0)
    echo "$p $sl $r"' 2>/dev/null || true)
  read -r procs_kib slab_b reserve_kib <<<"${parts:-0 0 0}"
  local procs_mib=$(((${procs_kib:-0} + 1023) / 1024))
  local slab_mib=$(((${slab_b:-0} + 1048575) / 1048576))
  local reserve_mib=$(((${reserve_kib:-0} + 1023) / 1024))
  local rest_mib=$((used - procs_mib - slab_mib - reserve_mib))
  echo "RAM idle composition: processes ${procs_mib} MiB, kernel slabs ${slab_mib} MiB, min_free_kbytes reserve ${reserve_mib} MiB, remainder ${rest_mib} MiB"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    if ! grep -q '^| image | arch | idle RAM' "${GITHUB_STEP_SUMMARY}" 2>/dev/null; then
      {
        echo "### Idle RAM per image (MemTotal-MemAvailable, boot-fresh)"
        echo "| image | arch | idle RAM (MiB) | processes (sum of RSS) | kernel slabs | min_free_kbytes reserve | remainder | target |"
        echo "|---|---|---|---|---|---|---|---|"
      } >>"${GITHUB_STEP_SUMMARY}"
    fi
    echo "| ${IMAGE:-?} | ${ARCH:-?} | ${used} | ${procs_mib} | ${slab_mib} | ${reserve_mib} | ${rest_mib} | ${RAM_IDLE_MAX_MB} |" >>"${GITHUB_STEP_SUMMARY}"
  fi
  if [ "${used}" -gt "${RAM_IDLE_MAX_MB}" ]; then
    # Self-explaining report: dump what actually holds the memory. Everything
    # runs on the guest over SSH and must not assume extra packages (minimal
    # image: awk+coreutils only).
    echo "----- ram gate breakdown (guest) -----"
    ssh "${SSH_OPTS[@]}" root@localhost '
      echo "[meminfo]"; grep -E "^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Slab|SReclaimable|SUnreclaim|KernelStack|PageTables|VmallocUsed|AnonPages|Mapped|Shmem):" /proc/meminfo;
      echo "[reserves: MemAvailable is below MemFree when these are large]";
      grep -E "^(Hugepagesize|HugePages_Total|HugePages_Free|CmaTotal|CmaFree|Percpu|AnonHugePages|Unevictable|Mlocked):" /proc/meminfo;
      echo "min_free_kbytes=$(cat /proc/sys/vm/min_free_kbytes) watermark_scale_factor=$(cat /proc/sys/vm/watermark_scale_factor)";
      awk "/^Node/{z=\$0} /^ +(min|low|high|managed|present) /{print z, \$1, \$2*4, \"KiB\"}" /proc/zoneinfo;
      echo "cmdline: $(cat /proc/cmdline)";
      echo "[top process RSS (KiB, name)]";
      awk "/^Name:/{name=\$2} /^VmRSS:/{print \$2, name}" /proc/[0-9]*/status 2>/dev/null | sort -rn | head -15;
      echo "[top kernel slabs (bytes)]";
      awk "NR>2{print \$3*\$4, \$1}" /proc/slabinfo 2>/dev/null | sort -rn | head -15;
      echo "[loaded modules (size KiB, name)]";
      awk "{print \$2, \$1}" /proc/modules 2>/dev/null | sort -rn | head -15
    ' 2>/dev/null | sed 's/^/  | /' || echo "  | breakdown unavailable (SSH read failed)"
    echo "----- end ram gate breakdown -----"
    if [ "${RAM_IDLE_ENFORCE:-0}" = "1" ]; then
      echo "::error::RAM idle budget exceeded: ${used} MiB at idle over the ${RAM_IDLE_MAX_MB} MiB budget"
      exit 1
    fi
    echo "::warning::idle RAM ${used} MiB is over the ${RAM_IDLE_MAX_MB} MiB target (report only; RAM_IDLE_ENFORCE=1 makes it a gate)"
  fi
}

#######################################
# Run every test in TESTS_FILE over SSH. A test passes on exit 0, or when its
# optional `expect` string is found in the output.
# Globals:
#   TESTS_FILE, SSH_OPTS
# Returns:
#   0 if all tests pass, 1 otherwise.
#######################################
run_tests() {
  local section="${1:-tests}"
  local n i name cmd expect diag test_arch test_variant test_cats out rc fail=0 pass
  n=$(yq ".${section} | length" "${TESTS_FILE}")
  for ((i = 0; i < n; i++)); do
    name=$(yq ".${section}[${i}].name" "${TESTS_FILE}")
    # Skip tests that are pinned to a different arch.
    test_arch=$(yq ".${section}[${i}].arch // \"\"" "${TESTS_FILE}")
    if [ -n "${test_arch}" ] && [ "${test_arch}" != "${ARCH}" ]; then
      echo "  SKIP  ${name} (arch: ${test_arch}, running: ${ARCH})"
      continue
    fi
    # Skip tests that are pinned to a different image variant.
    test_variant=$(yq ".${section}[${i}].variant // \"\"" "${TESTS_FILE}")
    if [ -n "${test_variant}" ] && [ "${test_variant}" != "${VARIANT}" ]; then
      echo "  SKIP  ${name} (variant: ${test_variant}, running: ${VARIANT})"
      continue
    fi
    # Skip tests of another category; a test with no category is global and
    # only runs on the reference one. Without `categories:` everything runs.
    if [ -n "${CATEGORY}" ]; then
      test_cats=$(yq ".${section}[${i}].category[]" "${TESTS_FILE}")
      if [ -n "${test_cats}" ]; then
        if ! printf '%s\n' "${test_cats}" | grep -qxF "${CATEGORY}"; then
          echo "  SKIP  ${name} (category: ${test_cats//$'\n'/,}, running: ${CATEGORY})"
          continue
        fi
      elif [ "${REFERENCE}" != 1 ]; then
        echo "  SKIP  ${name} (global test, running: ${CATEGORY})"
        continue
      fi
    fi
    cmd=$(yq ".${section}[${i}].run" "${TESTS_FILE}")
    expect=$(yq ".${section}[${i}].expect // \"\"" "${TESTS_FILE}")
    # cmd is the manifest's test command; expanding it here into the ssh
    # argument (to run on the VM) is intentional.
    # shellcheck disable=SC2029
    out=$(ssh "${SSH_OPTS[@]}" root@localhost "${cmd}" 2>&1) && rc=0 || rc=$?
    pass=0
    if [ -n "${expect}" ]; then
      printf '%s' "${out}" | grep -qF "${expect}" && pass=1
    elif [ "${rc}" -eq 0 ]; then
      pass=1
    fi
    if [ "${pass}" -eq 1 ]; then
      echo "  PASS  ${name}"
    else
      echo "::error::image test failed [${section}]: ${name}"
      printf '%s\n' "${out}" | sed 's/^/      /'
      # Run this test's own diagnostic so the failure is self-explaining. The
      # manifest is validated up front, so diag is always present.
      diag=$(yq ".${section}[${i}].diag" "${TESTS_FILE}")
      echo "      ----- diag: ${name} -----"
      # shellcheck disable=SC2029
      ssh "${SSH_OPTS[@]}" root@localhost "${diag}" 2>&1 | sed 's/^/      | /' || true
      fail=1
    fi
  done
  return "${fail}"
}

#######################################
# Validate one named section of the manifest (tests or reboot_tests). Exits 1
# if any entry is missing name, run, or diag.
# Arguments:
#   $1 - YAML section key (tests | reboot_tests)
#######################################
_validate_section() {
  local section="$1" n i name run diag missing=0
  n=$(yq ".${section} | length" "${TESTS_FILE}")
  for ((i = 0; i < n; i++)); do
    name=$(yq ".${section}[${i}].name // \"\"" "${TESTS_FILE}")
    run=$(yq ".${section}[${i}].run // \"\"" "${TESTS_FILE}")
    diag=$(yq ".${section}[${i}].diag // \"\"" "${TESTS_FILE}")
    if [ -z "${name}" ] || [ "${name}" = "null" ]; then
      echo "::error::${section}[${i}] has no name"
      missing=1
    fi
    if [ -z "${run}" ] || [ "${run}" = "null" ]; then
      echo "::error::${section} test '${name}' (index ${i}) has no run:"
      missing=1
    fi
    if [ -z "${diag}" ] || [ "${diag}" = "null" ]; then
      echo "::error::${section} test '${name}' (index ${i}) has no diag: — every test MUST ship its own diagnostic so a failure is never undiagnosable"
      missing=1
    fi
  done
  if [ "${missing}" -ne 0 ]; then
    echo "::error::${TESTS_FILE} [${section}]: one or more tests are missing name/run/diag"
    exit 1
  fi
}

#######################################
# Check the manifest's `categories:` (when present): every entry has a name, an
# arch, a `suffix:` key (empty = the plain tags) and a `secure_boot:` of 0 or 1;
# an optional `kargs:` is a list of kernel arguments of the strict form
# name[=value] (added at install); (arch, suffix, name) triples are unique, so
# several categories may share a suffix and the tag is booted once per category; each arch has exactly one `reference: true`
# category (the one running the global tests); every `category:` a test lists
# names a defined category.
# Globals:
#   TESTS_FILE
# Returns:
#   Exits 1 on the first inconsistent category definition.
#######################################
_validate_categories() {
  local n bad=0 msg sec
  n=$(yq '.categories | length // 0' "${TESTS_FILE}")
  [ "${n}" -gt 0 ] || return 0
  msg=$(yq '.categories[] | select(
    (.name // "") == ""
    or ((.arch // "") != "amd64" and (.arch // "") != "arm64")
    or (has("suffix") | not)
    or ((.secure_boot // -1) != 0 and (.secure_boot // -1) != 1)
  ) | .name' "${TESTS_FILE}")
  [ -z "${msg}" ] || {
    echo "::error::categories need name, arch (amd64|arm64), suffix and secure_boot (0|1): ${msg}"
    bad=1
  }
  msg=$(yq '[.categories[] | .arch + ":" + .suffix + ":" + .name] | group_by(.) | .[] | select(length > 1) | .[0]' "${TESTS_FILE}")
  [ -z "${msg}" ] || { echo "::error::duplicate (arch:suffix:name) in categories: ${msg}"; bad=1; }
  msg=$(yq '.categories[] | select(has("kargs")) | select(
    (.kargs | tag) != "!!seq"
    or ([.kargs[] | select((tag) != "!!str" or (test("^[A-Za-z0-9._-]+(=[A-Za-z0-9.,_:/-]+)?$") | not))] | length) > 0
  ) | .name' "${TESTS_FILE}")
  [ -z "${msg}" ] || { echo "::error::kargs must be a list of name[=value] kernel arguments ([A-Za-z0-9._-] names, [A-Za-z0-9.,_:/-] values): ${msg}"; bad=1; }
  msg=$(yq '.categories | group_by(.arch) | .[] | select(([.[] | select(.reference == true)] | length) != 1) | .[0].arch' "${TESTS_FILE}")
  [ -z "${msg}" ] || { echo "::error::categories of arch ${msg} must have exactly one reference: true"; bad=1; }
  for sec in tests reboot_tests; do
    msg=$(yq "[.categories[].name] as \$known | .${sec}[]?.category[]? | select(. as \$c | \$known | contains([\$c]) | not)" "${TESTS_FILE}")
    [ -z "${msg}" ] || { echo "::error::${sec} lists unknown categories: ${msg}"; bad=1; }
  done
  [ "${bad}" -eq 0 ] || exit 1
}

#######################################
# Indices of the manifest categories matching the image: those of this arch
# whose `suffix:` ends the tag name (the tag minus its `_<arch>`), restricted
# to the longest matching suffix. Several categories may share that suffix:
# the image is then booted once per category.
# Globals:
#   IMAGE, ARCH, TESTS_FILE
# Outputs:
#   One index per line on stdout (none when no category matches).
#######################################
_matching_category_indices() {
  local tag n i c_arch c_suffix best=-1
  local -a idx=() len=()
  tag=${IMAGE##*:}
  tag=${tag%_"${ARCH}"}
  n=$(yq '.categories | length // 0' "${TESTS_FILE}")
  for ((i = 0; i < n; i++)); do
    c_arch=$(yq ".categories[${i}].arch" "${TESTS_FILE}")
    [ "${c_arch}" = "${ARCH}" ] || continue
    c_suffix=$(yq ".categories[${i}].suffix" "${TESTS_FILE}")
    case "${tag}" in *"${c_suffix}") ;; *) continue ;; esac
    idx+=("${i}")
    len+=("${#c_suffix}")
    [ "${#c_suffix}" -le "${best}" ] || best=${#c_suffix}
  done
  for i in "${!idx[@]}"; do
    [ "${len[i]}" -ne "${best}" ] || echo "${idx[i]}"
  done
}

#######################################
# Names of every manifest category the image is tested under (see
# _matching_category_indices), one per line. Empty when the manifest has no
# `categories:` or none matches.
# Globals:
#   IMAGE, ARCH, TESTS_FILE
#######################################
list_categories() {
  local i
  while IFS= read -r i; do
    yq ".categories[${i}].name" "${TESTS_FILE}"
  done < <(_matching_category_indices)
}

#######################################
# Pick the manifest category of the image under test: among the categories
# matching the tag (_matching_category_indices) the CATEGORY already in the
# environment when set (one boot per category of a tag), else the first. Sets
# CATEGORY, REFERENCE, INSTALL_KARGS (the category's `kargs:`, space separated;
# exported for the installer) and, unless already set, SECURE_BOOT from the
# category's `secure_boot:`. A manifest with no `categories:` leaves CATEGORY
# empty (every test runs) and SECURE_BOOT at 0.
# Globals:
#   IMAGE, ARCH, TESTS_FILE, CATEGORY, REFERENCE, SECURE_BOOT, INSTALL_KARGS
# Returns:
#   Exits 1 when the manifest has categories but none matches this image, or
#   the requested CATEGORY is not one of the matching ones.
#######################################
select_category() {
  local tag n i best_i=-1 want="${CATEGORY}"
  tag=${IMAGE##*:}
  tag=${tag%_"${ARCH}"}
  INSTALL_KARGS=""
  export INSTALL_KARGS
  n=$(yq '.categories | length // 0' "${TESTS_FILE}")
  if [ "${n}" -eq 0 ]; then
    SECURE_BOOT="${SECURE_BOOT:-0}"
    return 0
  fi
  while IFS= read -r i; do
    if [ -z "${want}" ] || [ "$(yq ".categories[${i}].name" "${TESTS_FILE}")" = "${want}" ]; then
      best_i=${i}
      break
    fi
  done < <(_matching_category_indices)
  if [ "${best_i}" -lt 0 ]; then
    echo "::error::no category${want:+ \"${want}\"} of ${TESTS_FILE} matches tag '${tag}' on ${ARCH}"
    exit 1
  fi
  CATEGORY=$(yq ".categories[${best_i}].name" "${TESTS_FILE}")
  [ "$(yq ".categories[${best_i}].reference // false" "${TESTS_FILE}")" = true ] && REFERENCE=1
  SECURE_BOOT="${SECURE_BOOT:-$(yq ".categories[${best_i}].secure_boot" "${TESTS_FILE}")}"
  INSTALL_KARGS=$(yq ".categories[${best_i}].kargs // [] | join(\" \")" "${TESTS_FILE}")
  echo "Category: ${CATEGORY} (tag ${tag}, reference=${REFERENCE}, secure_boot=${SECURE_BOOT}, kargs='${INSTALL_KARGS}')"
}

#######################################
# Enforce that every test ships its own `diag:` command. A test with no diag is
# a manifest error and fails the run BEFORE the VM is even booted — so a test
# can never regress into a blind, undiagnosable failure later. Also checks the
# mandatory name/run fields. Validates both `tests:` and `reboot_tests:` if
# the latter section is present.
# Globals:
#   TESTS_FILE
# Returns:
#   Exits 1 if any test is missing name, run, or diag.
#######################################
validate_manifest() {
  _validate_categories
  _validate_section "tests"
  local reboot_n
  reboot_n=$(yq '.reboot_tests | length // 0' "${TESTS_FILE}" 2>/dev/null || echo 0)
  if [ "${reboot_n:-0}" -gt 0 ]; then
    _validate_section "reboot_tests"
  fi
}

#######################################
# Wait for the guest to FINISH booting before testing. sshd answers within ~1s
# (it starts early), but PVE services come up much later — pveproxy is delayed
# ~20s by an apt-update task it runs at startup, and dc-zramctl by its zram
# calibration (~23s). Running the tests the instant SSH answers therefore reads
# those slow-to-start services as false 'inactive'/'activating' failures.
# `systemctl is-system-running --wait` blocks until systemd leaves the 'starting'
# state; it exits non-zero when the result is 'degraded' (some unit failed),
# which the tests then report, so a non-zero here is not fatal. Capped by a
# timeout in case startup never settles.
# Globals:
#   SSH_OPTS
#######################################
wait_boot() {
  echo "Waiting for guest systemd startup to settle before testing..."
  local state
  state=$(ssh "${SSH_OPTS[@]}" root@localhost \
    'timeout 180 systemctl is-system-running --wait >/dev/null 2>&1; \
     systemctl is-system-running' 2>/dev/null || true)
  echo "  guest is-system-running: ${state:-unknown}"
}

#######################################
# True when something listens on the forwarded SSH port.
# Globals:
#   SSH_PORT
#######################################
port_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/${SSH_PORT}") 2>/dev/null
}

#######################################
# Power the VM off and make sure QEMU is gone: ask the guest to power off over
# SSH, give it STOP_GRACE_S seconds, then SIGTERM, then SIGKILL. QEMU runs
# under sudo, so its pidfile is root-owned: it is read (and removed) with sudo.
# Safe to call when no VM runs.
# Globals:
#   PIDFILE, SSH_OPTS, STOP_GRACE_S
#######################################
stop_vm() {
  local pid i rc
  pid=$(sudo cat "${PIDFILE}" 2>/dev/null) || pid=""
  [ -n "${pid}" ] || return 0
  if sudo kill -0 "${pid}" 2>/dev/null; then
    ssh "${SSH_OPTS[@]}" root@localhost poweroff 2>/dev/null && rc=0 || rc=$?
    # 255 is the expected "connection closed" of a guest going down.
    echo "  poweroff requested (ssh rc ${rc})"
    for ((i = 0; i < STOP_GRACE_S; i++)); do
      sudo kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
  fi
  if sudo kill -0 "${pid}" 2>/dev/null; then
    echo "  guest did not power off within ${STOP_GRACE_S}s: killing QEMU (pid ${pid})"
    sudo kill "${pid}" 2>/dev/null
    for ((i = 0; i < 30; i++)); do
      sudo kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
  fi
  if sudo kill -0 "${pid}" 2>/dev/null; then
    sudo kill -KILL "${pid}" 2>/dev/null
  fi
  sudo rm -f "${PIDFILE}" "${MONITOR}"
}

#######################################
# Wait until the forwarded SSH port is free again, so the next image can bind it.
# Arguments:
#   $1 - optional timeout in seconds (default 30)
# Returns:
#   1 when the port is still taken at the deadline.
#######################################
free_ssh_port() {
  local timeout="${1:-30}" i
  for ((i = 0; i < timeout; i++)); do
    port_open || return 0
    sleep 1
  done
  echo "::error::SSH port ${SSH_PORT} is still in use after ${timeout}s"
  return 1
}

#######################################
# Prepare sub-command, once per job: dependencies, KVM detection, manifest
# validation, one SSH key, the Secure Boot tooling (only if some category of
# this arch enforces Secure Boot) and the persisted state.
#######################################
cmd_prepare() {
  state_init
  if [ -z "${TESTS_FILE:-}" ]; then
    echo "::error::TESTS_FILE is not set"
    exit 1
  fi
  install_deps
  # Fail fast on an incomplete manifest (missing diag) before paying for a boot.
  validate_manifest
  make_ssh_key
  if manifest_needs_secure_boot; then
    setup_sb_tooling
  else
    echo "No Secure Boot image on ${ARCH}: skipping the key hierarchy and virt-firmware venv"
  fi
  save_state
}

#######################################
# Test sub-command, once per image: install to a fresh disk, boot, run the
# tests and the reboot phase in the same QEMU process, power off. Meant to run
# as its own process: it uses exit, and its status is the verdict.
# Arguments:
#   $1 - image ref
#   $2 - idle RAM budget in MiB (optional, default RAM_IDLE_MAX_MB)
#######################################
cmd_test() {
  local timeout reboot_n old_boot reboot_timeout
  IMAGE="$1"
  RAM_IDLE_MAX_MB="${2:-${RAM_IDLE_MAX_MB}}"
  load_state || exit 1
  # Tag containing "minimal" maps to the minimal variant; everything else
  # (latest, gallium, ...) to main. run_tests skips tests of another variant.
  VARIANT="main"
  if echo "${IMAGE}" | grep -qE ':[^:]*minimal'; then
    VARIANT="minimal"
  fi
  mkdir -p "${LOGS}"
  # One log per category when the caller forces one (several boots of a tag).
  SERIAL="${LOGS}/${IMAGE//[^A-Za-z0-9._-]/_}${CATEGORY:+.${CATEGORY//[^A-Za-z0-9._-]/_}}.serial.log"
  trap 'stop_vm; sudo chmod a+r "${SERIAL}" 2>/dev/null' EXIT
  stop_vm
  free_ssh_port 5 || exit 1
  sudo rm -f "${SERIAL}" "${MONITOR}"
  select_category
  install_disk
  if [ "${SECURE_BOOT}" = 1 ]; then
    local certdir="${STATE}/image-certs"
    rm -rf "${certdir}"
    fetch_image_certs "${certdir}"
    prepare_sb_varstore "${certdir}"
  fi
  boot_vm
  timeout=$(yq '.boot_timeout // 600' "${TESTS_FILE}")
  # TCG emulation is an order of magnitude slower than KVM, so the manifest's
  # KVM-tuned boot timeout would trip on a boot that is merely slow. Scale it.
  if [ "${KVM}" != "1" ]; then
    timeout=$((timeout * TCG_TIMEOUT_SCALE))
    echo "TCG emulation: boot timeout scaled to ${timeout}s"
  fi
  if ! wait_ssh "${timeout}"; then
    echo "::error::VM did not become reachable within ${timeout}s"
    dump_serial
    exit 1
  fi
  wait_boot
  ram_gate
  run_tests "tests"

  # Optional second phase: if the manifest defines reboot_tests, trigger a VM
  # reboot and run them. Used for rollback and other post-reboot checks.
  reboot_n=$(yq '.reboot_tests | length // 0' "${TESTS_FILE}" 2>/dev/null || echo 0)
  if [ "${reboot_n:-0}" -gt 0 ]; then
    echo "--- triggering VM reboot for post-reboot test phase ---"
    # reboot exits non-zero (connection reset) - that is expected.
    # The boot id tells the new boot apart from the guest still shutting down.
    old_boot=$(ssh "${SSH_OPTS[@]}" root@localhost cat /proc/sys/kernel/random/boot_id)
    ssh "${SSH_OPTS[@]}" root@localhost "reboot" 2>/dev/null || true
    # Brief pause so the VM starts going down before we poll SSH.
    sleep 5
    reboot_timeout=$(yq '.reboot_timeout // 300' "${TESTS_FILE}")
    if [ "${KVM}" != "1" ]; then
      reboot_timeout=$((reboot_timeout * TCG_TIMEOUT_SCALE))
    fi
    if ! wait_ssh "${reboot_timeout}" "${old_boot}"; then
      echo "::error::VM did not come back after reboot within ${reboot_timeout}s"
      dump_serial
      exit 1
    fi
    wait_boot
    run_tests "reboot_tests"
  fi
  stop_vm
  free_ssh_port || exit 1
  rm -f "${DISK}"
}

#######################################
# Teardown sub-command, once per job: stop any VM still running and delete the
# disk, keys, venv and varstores. The per-image serial logs (logs/) are kept
# for the caller to upload.
#######################################
cmd_teardown() {
  case "${STATE}" in
    "" | /) echo "::error::refusing to clean '${STATE}'"; return 1 ;;
  esac
  [ -d "${STATE}" ] || return 0
  stop_vm
  # QEMU's files are root-owned.
  sudo rm -rf "${DISK}" "${SERIAL}" "${MONITOR}" "${PIDFILE}" "${VARS_RUN}" \
    "${KEY}" "${KEY}.pub" "${STATE}/PK.key" "${STATE}/PK.pem" \
    "${STATE}/KEK.key" "${STATE}/KEK.pem" "${SBTOOL}" "${VARSTORES}" \
    "${STATE}/image-certs" "${STATE_ENV}"
}

usage() {
  cat <<USAGE
usage: image-test.sh [prepare | test <image-ref> [<ram-max-mb>] | categories <image-ref> | teardown]

  prepare   once per job: dependencies, KVM detection, manifest validation,
            SSH key, Secure Boot tooling; state goes to \$IMAGE_TEST_STATE
            (default \${RUNNER_TEMP:-/tmp}/image-test)
  test      once per image: install, boot, run the tests, power off. Run it as
            a child process (under timeout): the exit code is the verdict
  categories  print the manifest categories the image is tested under, one per
            line (run 'test' once per line with CATEGORY=<name> when several)
  teardown  once per job: remove the disk, keys and varstores, keep the logs
  (none)    prepare, test \$IMAGE \$RAM_IDLE_MAX_MB, teardown (the action)
USAGE
}

#######################################
# Dispatch the sub-commands.
# Arguments:
#   the command line of image-test.sh
#######################################
dispatch() {
  local rc=0 self="${SCRIPT_DIR}/image-test.sh"
  case "${1:-}" in
    prepare)
      [ "$#" -eq 1 ] || { usage >&2; return 2; }
      cmd_prepare
      ;;
    test)
      if [ "$#" -lt 2 ] || [ "$#" -gt 3 ] || [ -z "$2" ]; then
        usage >&2
        return 2
      fi
      if [ "$#" -eq 3 ] && ! [[ "$3" =~ ^[0-9]+$ ]]; then
        echo "::error::ram-max must be a non-negative integer (MiB), got '$3'" >&2
        return 2
      fi
      cmd_test "${@:2}"
      ;;
    categories)
      if [ "$#" -ne 2 ] || [ -z "$2" ]; then
        usage >&2
        return 2
      fi
      IMAGE="$2"
      load_state || return 1
      list_categories
      ;;
    teardown)
      [ "$#" -eq 1 ] || { usage >&2; return 2; }
      cmd_teardown
      ;;
    "")
      if [ -z "${IMAGE}" ]; then
        echo "::error::IMAGE is not set (or pass a sub-command)" >&2
        usage >&2
        return 2
      fi
      # Each phase is its own process: exit codes stay meaningful and errexit
      # stays active (it is suspended inside an `||` list in the same shell).
      if bash "${self}" prepare; then
        bash "${self}" test "${IMAGE}" "${RAM_IDLE_MAX_MB}" || rc=$?
      else
        rc=$?
      fi
      bash "${self}" teardown
      return "${rc}"
      ;;
    -h | --help)
      usage
      ;;
    *)
      echo "::error::unknown sub-command '$1'" >&2
      usage >&2
      return 2
      ;;
  esac
}
