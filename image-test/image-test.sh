#!/bin/bash
#
# image-test.sh - install the freshly built bootc image onto a virtual disk,
# boot it under QEMU/KVM (UEFI), and run the repo's image-tests manifest over
# SSH. Exits non-zero (blocking the image push) if any test fails.
#
# Consumed environment (set by action.yml):
#   IMAGE       local image ref to test (already in podman storage)
#   TESTS_FILE  path to the tests manifest (yaml)
#   MEM         VM memory in MiB (default 4096)
#   VCPUS       VM vCPUs (default 2)
#   ARCH        target architecture: amd64 (default) or arm64
#   RUNNER_TEMP scratch dir provided by Actions
#   RAM_IDLE_MAX_MB  idle RAM budget in MiB: the boot-fresh measurement must
#               stay under it or the run fails (default 15; Gabriel 30/10:
#               the gate holds the idle footprint at 10-15 MiB).
set -euo pipefail

ARCH="${ARCH:-amd64}"
RAM_IDLE_MAX_MB="${RAM_IDLE_MAX_MB:-15}"
# Variant derived from the image tag: tags containing "minimal" map to "minimal";
# everything else (latest, gallium, …) maps to "main". Used by run_tests to skip
# tests whose `variant:` field does not match the image under test.
VARIANT="main"
if echo "${IMAGE:-}" | grep -qE ':[^:]*minimal'; then
  VARIANT="minimal"
fi
WORK="${RUNNER_TEMP:-/tmp}"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
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
DISK="${WORK}/test-disk.raw"
KEY="${WORK}/test-key"
SERIAL="${WORK}/serial.log"
MONITOR="${WORK}/monitor.sock"
SSH_PORT=2222
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
# (set by select_category).
CATEGORY=""
REFERENCE=0
# 1 once we know /dev/kvm is present and usable, 0 otherwise (set in
# install_deps). GitHub's hosted arm64 runners expose no /dev/kvm at all —
# nested virtualization is not available on that fleet — so the arm64 boot
# test runs under QEMU TCG emulation instead of KVM. See boot_vm and main.
KVM=0
VIRT_FW="${WORK}/sbtool/bin/virt-fw-vars"
SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=3
  -i "${KEY}"
  -p "${SSH_PORT}"
)

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
# Install the image to DISK, injecting a generated root SSH key.
# Globals:
#   IMAGE, DISK, KEY, WORK
#######################################
install_disk() {
  # Overwrite any artefacts of a previous run non-interactively (a stale key
  # makes ssh-keygen prompt, which hangs the runner).
  rm -f "${KEY}" "${KEY}.pub"
  ssh-keygen -t ed25519 -N '' -f "${KEY}" -q
  # Sparse: only written blocks consume host space (~3G after install), but 10G
  # of virtual size is already ample for the ~3G deploy + boot + ESP.
  truncate -s 10G "${DISK}"
  # The disk is partitioned + installed by install-fs.sh, run privileged INSIDE
  # the image so it can use the image's own tools and bootc install
  # to-filesystem into the same layout as the ISO (btrfs pool + varlog quota).
  # bootc-build.yml builds with sudo podman, so this root podman sees the freshly
  # built image directly (no registry pull).
  # ARCH is forwarded so install-fs.sh can set the right console= kernel arg
  # (ttyS0 on x86, ttyAMA0 on arm64 QEMU virt machine).
  sudo podman run --rm --privileged --pid=host --security-opt label=disable \
    -e ARCH="${ARCH}" \
    -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
    -v "${DISK}:/disk.raw" \
    -v "${KEY}.pub:/key.pub:ro" \
    -v "${SCRIPT_DIR}/install-fs.sh:/install-fs.sh:ro" \
    "${IMAGE}" \
    bash /install-fs.sh
}

#######################################
# Build a UEFI Secure Boot varstore from scratch — our own key hierarchy, no
# distro *.ms template (those carry Microsoft's BlackLotus revocation dbx that
# rejects even a current, valid shim). Writes ${WORK}/OVMF_VARS.fd:
#   PK/KEK  - freshly generated, ours
#   db      - public Microsoft UEFI CA 2011 + 2023 (bundled by virt-firmware) so
#             the MS-signed shim validates, plus our own signing cert
#   MokList - our debian-bootc signing cert so shim validates our grub
# Globals:
#   IMAGE, WORK, VIRT_FW
#######################################
build_sb_vars() {
  local certs guid base k
  local -a mok_args
  # virt-firmware builds the Secure Boot varstore (it bundles the Microsoft
  # UEFI CAs); openssl generates our own PK/KEK. Kept in a venv so the runner's
  # system Python is untouched (PEP 668).
  if ! python3 -c 'import ensurepip, venv' 2>/dev/null \
    || ! command -v openssl >/dev/null 2>&1; then
    sudo apt-get install -y --no-install-recommends python3-venv openssl
  fi
  python3 -m venv "${WORK}/sbtool"
  "${WORK}/sbtool/bin/pip" install --quiet virt-firmware
  certs=$("${WORK}/sbtool/bin/python" -c \
    'import os,virt.firmware as v; print(os.path.join(
     os.path.dirname(v.__file__), "certs/microsoft.com"))')
  guid=$(cat /proc/sys/kernel/random/uuid)
  for k in PK KEK; do
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
      -subj "/CN=debian-bootc test ${k}/" \
      -keyout "${WORK}/${k}.key" -out "${WORK}/${k}.pem" 2>/dev/null
  done
  # Our image's own Secure Boot signing certificate (the key that signs grub).
  # The redirect target lives in ${WORK} (writable by the invoking user), so the
  # non-sudo redirect is intended — sudo only elevates podman.
  # shellcheck disable=SC2024
  sudo podman run --rm "${IMAGE}" \
    cat /usr/share/debian-bootc/sb_signing.crt >"${WORK}/sb.crt"
  # The certificates the installer's mok-stage enrolls besides the signing one:
  # the public ones dropped in the image's mok.d (a fork's own key), PEM or DER,
  # never a file holding a private key.
  mok_args=(--add-mok "${guid}" "${WORK}/sb.crt")
  mkdir -p "${WORK}/mok.d"
  if sudo podman run --rm "${IMAGE}" test -d /usr/share/debian-bootc/mok.d; then
    sudo podman run --rm "${IMAGE}" tar -C /usr/share/debian-bootc -c mok.d \
      | tar -C "${WORK}" -x
  fi
  for k in "${WORK}"/mok.d/*; do
    [ -f "${k}" ] || continue
    case "${k}" in *.crt | *.cer | *.pem | *.der) ;; *) continue ;; esac
    grep -q 'PRIVATE KEY' "${k}" && continue
    openssl x509 -in "${k}" -outform PEM -out "${k}.pem" 2>/dev/null \
      || openssl x509 -inform DER -in "${k}" -outform PEM -out "${k}.pem" \
      || { echo "::error::mok.d/${k##*/} is not a certificate"; exit 1; }
    mok_args+=(--add-mok "${guid}" "${k}.pem")
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
  "${VIRT_FW}" -i "${base}" -o "${WORK}/OVMF_VARS.fd" \
    --set-pk "${guid}" "${WORK}/PK.pem" \
    --add-kek "${guid}" "${WORK}/KEK.pem" \
    --add-db "${guid}" "${certs}/ms-uefi-2011.pem" \
    --add-db "${guid}" "${certs}/ms-uefi-2023.pem" \
    --add-db "${guid}" "${WORK}/sb.crt" \
    "${mok_args[@]}" \
    --secure-boot
}

#######################################
# Boot the disk under QEMU/KVM with UEFI and an SSH port-forward. Enforces
# Secure Boot when SECURE_BOOT=1 (using the varstore from build_sb_vars),
# otherwise runs in Setup Mode (SB not enforced).
# Globals:
#   DISK, SERIAL, SSH_PORT, WORK, MEM, VCPUS, SECURE_BOOT
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
  fi
  if [ "${ARCH}" = "arm64" ]; then
    # arm64: QEMU virt machine, AAVMF (arm64 UEFI from qemu-efi-aarch64 package).
    # Ubuntu's package ships several flavours side by side (.secboot, .ms,
    # .snakeoil, .no-secboot), so the firmware is picked by name, never by
    # directory order: the enforcing build for SECURE_BOOT=1 (paired with the
    # varstore from build_sb_vars), the plain one otherwise.
    if [ "${SECURE_BOOT}" = 1 ]; then
      code=$(find /usr/share/AAVMF -name 'AAVMF_CODE.secboot.fd' | sort | head -1)
      vars="${WORK}/OVMF_VARS.fd"
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
    [ "${SECURE_BOOT}" = 1 ] || cp "${vars}" "${WORK}/OVMF_VARS.fd"
    sudo qemu-system-aarch64 \
      ${accel} -m "${MEM:-4096}" -smp "${VCPUS:-2}" \
      -machine virt -cpu "${cpu}" \
      -drive "if=pflash,format=raw,unit=0,readonly=on,file=${code}" \
      -drive "if=pflash,format=raw,unit=1,file=${WORK}/OVMF_VARS.fd" \
      -drive "file=${DISK},format=raw,if=virtio" \
      -netdev "user,id=n0,hostfwd=tcp::${SSH_PORT}-:22" \
      -device "virtio-net,netdev=n0,romfile=" \
      -display none -serial "file:${SERIAL}" -monitor "unix:${MONITOR},server,nowait" \
      -daemonize -pidfile "${WORK}/qemu.pid"
  else
    local secure=off
    if [ "${SECURE_BOOT}" = 1 ]; then
      code=$(find "${OVMF_DIR}" -name 'OVMF_CODE*.fd' -name '*secboot*' \
        | head -1)
      secure=on
      # ${WORK}/OVMF_VARS.fd already written by build_sb_vars.
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
      cp "${vars}" "${WORK}/OVMF_VARS.fd"
    fi
    sudo qemu-system-x86_64 \
      ${accel} -m "${MEM:-4096}" -smp "${VCPUS:-2}" -machine "q35,smm=${secure}" \
      -global "driver=cfi.pflash01,property=secure,value=${secure}" \
      -drive "if=pflash,format=raw,unit=0,readonly=on,file=${code}" \
      -drive "if=pflash,format=raw,unit=1,file=${WORK}/OVMF_VARS.fd" \
      -drive "file=${DISK},format=raw,if=virtio" \
      -netdev "user,id=n0,hostfwd=tcp::${SSH_PORT}-:22" \
      -device "virtio-net,netdev=n0,romfile=" \
      -display none -serial "file:${SERIAL}" -monitor "unix:${MONITOR},server,nowait" \
      -daemonize -pidfile "${WORK}/qemu.pid"
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
  sudo python3 -I "${SCRIPT_DIR}/hmp.py" "${MONITOR}" \
    "info registers" 'x /24xw $pc-48' 2>&1 | grep -aE '^(X[0-9]+|.* X[0-9]+=|[0-9a-f]{16}:|SP|ELR|ESR)' || true
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
# footprint. Fail (blocking the image push) when it exceeds the budget —
# the regression to catch is a service creeping into the boot path, visible
# as extra resident memory. The budget comes from RAM_IDLE_MAX_MB (default
# 15 MiB; Gabriel 30/10: hold the idle footprint at 10-15 MiB).
# Globals:
#   SSH_OPTS, RAM_IDLE_MAX_MB
# Returns:
#   Exits 1 when the idle footprint is over budget.
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
  if [ "${used}" -gt "${RAM_IDLE_MAX_MB}" ]; then
    # Self-explaining failure: dump what actually holds the memory before the
    # gate blocks the push. Everything runs on the guest over SSH and must not
    # assume extra packages (minimal image: awk+coreutils only).
    echo "----- ram gate breakdown (guest) -----"
    ssh "${SSH_OPTS[@]}" root@localhost '
      echo "[meminfo]"; grep -E "^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Slab|SReclaimable|SUnreclaim|KernelStack|PageTables|VmallocUsed|AnonPages|Mapped|Shmem):" /proc/meminfo;
      echo "[top process RSS (KiB, name)]";
      awk "/^Name:/{name=\$2} /^VmRSS:/{print \$2, name}" /proc/[0-9]*/status 2>/dev/null | sort -rn | head -15;
      echo "[top kernel slabs (bytes)]";
      awk "NR>2{print \$3*\$4, \$1}" /proc/slabinfo 2>/dev/null | sort -rn | head -15;
      echo "[loaded modules (size KiB, name)]";
      awk "{print \$2, \$1}" /proc/modules 2>/dev/null | sort -rn | head -15
    ' 2>/dev/null | sed 's/^/  | /' || echo "  | breakdown unavailable (SSH read failed)"
    echo "----- end ram gate breakdown -----"
    echo "::error::RAM idle budget exceeded: ${used} MiB at idle over the ${RAM_IDLE_MAX_MB} MiB budget"
    exit 1
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
# (arch, suffix) pairs are unique; each arch has exactly one `reference: true`
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
  msg=$(yq '[.categories[] | .arch + ":" + .suffix] | group_by(.) | .[] | select(length > 1) | .[0]' "${TESTS_FILE}")
  [ -z "${msg}" ] || { echo "::error::duplicate (arch:suffix) in categories: ${msg}"; bad=1; }
  msg=$(yq '.categories | group_by(.arch) | .[] | select(([.[] | select(.reference == true)] | length) != 1) | .[0].arch' "${TESTS_FILE}")
  [ -z "${msg}" ] || { echo "::error::categories of arch ${msg} must have exactly one reference: true"; bad=1; }
  for sec in tests reboot_tests; do
    msg=$(yq "[.categories[].name] as \$known | .${sec}[]?.category[]? | select(. as \$c | \$known | contains([\$c]) | not)" "${TESTS_FILE}")
    [ -z "${msg}" ] || { echo "::error::${sec} lists unknown categories: ${msg}"; bad=1; }
  done
  [ "${bad}" -eq 0 ] || exit 1
}

#######################################
# Pick the manifest category of the image under test: among the categories of
# this arch whose `suffix:` ends the tag name (the tag minus its `_<arch>`), the
# one with the longest suffix. Sets CATEGORY, REFERENCE and, unless already
# set, SECURE_BOOT from the category's `secure_boot:`. A manifest with no
# `categories:` leaves CATEGORY empty (every test runs) and SECURE_BOOT at 0.
# Globals:
#   IMAGE, ARCH, TESTS_FILE, CATEGORY, REFERENCE, SECURE_BOOT
# Returns:
#   Exits 1 when the manifest has categories but none matches this image.
#######################################
select_category() {
  local tag n i c_arch c_suffix best=-1 best_i=-1
  tag=${IMAGE##*:}
  tag=${tag%_"${ARCH}"}
  n=$(yq '.categories | length // 0' "${TESTS_FILE}")
  if [ "${n}" -eq 0 ]; then
    SECURE_BOOT="${SECURE_BOOT:-0}"
    return 0
  fi
  for ((i = 0; i < n; i++)); do
    c_arch=$(yq ".categories[${i}].arch" "${TESTS_FILE}")
    [ "${c_arch}" = "${ARCH}" ] || continue
    c_suffix=$(yq ".categories[${i}].suffix" "${TESTS_FILE}")
    case "${tag}" in *"${c_suffix}") ;; *) continue ;; esac
    if [ "${#c_suffix}" -gt "${best}" ]; then
      best=${#c_suffix}
      best_i=${i}
    fi
  done
  if [ "${best_i}" -lt 0 ]; then
    echo "::error::no category of ${TESTS_FILE} matches tag '${tag}' on ${ARCH}"
    exit 1
  fi
  CATEGORY=$(yq ".categories[${best_i}].name" "${TESTS_FILE}")
  [ "$(yq ".categories[${best_i}].reference // false" "${TESTS_FILE}")" = true ] && REFERENCE=1
  SECURE_BOOT="${SECURE_BOOT:-$(yq ".categories[${best_i}].secure_boot" "${TESTS_FILE}")}"
  echo "Category: ${CATEGORY} (tag ${tag}, reference=${REFERENCE}, secure_boot=${SECURE_BOOT})"
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
# Kill the QEMU VM on exit.
#######################################
cleanup() {
  # QEMU runs under sudo, so its pidfile is root-owned: read it with sudo too,
  # otherwise the VM is never killed and keeps the pidfile lock for the next
  # image-test invocation of the same job.
  local pid
  pid=$(sudo cat "${WORK}/qemu.pid" 2>/dev/null) || return 0
  [ -n "${pid}" ] || return 0
  sudo kill "${pid}" 2>/dev/null || return 0
  local i
  for i in $(seq 1 30); do
    sudo kill -0 "${pid}" 2>/dev/null || return 0
    sleep 1
  done
  sudo kill -KILL "${pid}" 2>/dev/null || true
}

#######################################
# Boot the image in QEMU and run the manifest tests, then the reboot tests.
# Globals:
#   TESTS_FILE
#   SECURE_BOOT
#   KVM
#   SERIAL
#   SSH_OPTS
# Returns:
#   Exits 1 when the VM is unreachable or a test fails.
#######################################
main() {
  trap cleanup EXIT
  install_deps
  # Fail fast on an incomplete manifest (missing diag) before paying for a boot.
  validate_manifest
  select_category
  install_disk
  if [ "${SECURE_BOOT}" = 1 ]; then
    build_sb_vars
  fi
  boot_vm
  local timeout
  timeout=$(yq '.boot_timeout // 600' "${TESTS_FILE}")
  # TCG emulation is an order of magnitude slower than KVM, so the manifest's
  # KVM-tuned boot timeout would trip on a boot that is merely slow. Scale it.
  if [ "${KVM}" != "1" ]; then
    timeout=$((timeout * 12))
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
  local reboot_n
  reboot_n=$(yq '.reboot_tests | length // 0' "${TESTS_FILE}" 2>/dev/null || echo 0)
  if [ "${reboot_n:-0}" -gt 0 ]; then
    echo "--- triggering VM reboot for post-reboot test phase ---"
    # reboot exits non-zero (connection reset) — that is expected.
    # The boot id tells the new boot apart from the guest still shutting down.
    local old_boot
    old_boot=$(ssh "${SSH_OPTS[@]}" root@localhost cat /proc/sys/kernel/random/boot_id)
    ssh "${SSH_OPTS[@]}" root@localhost "reboot" 2>/dev/null || true
    # Brief pause so the VM starts going down before we poll SSH.
    sleep 5
    local reboot_timeout
    reboot_timeout=$(yq '.reboot_timeout // 300' "${TESTS_FILE}")
    if [ "${KVM}" != "1" ]; then
      reboot_timeout=$((reboot_timeout * 5))
    fi
    if ! wait_ssh "${reboot_timeout}" "${old_boot}"; then
      echo "::error::VM did not come back after reboot within ${reboot_timeout}s"
      dump_serial
      exit 1
    fi
    wait_boot
    run_tests "reboot_tests"
  fi
}

main "$@"
