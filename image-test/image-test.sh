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
#   RUNNER_TEMP scratch dir provided by Actions
set -euo pipefail

WORK="${RUNNER_TEMP:-/tmp}"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DISK="${WORK}/test-disk.raw"
KEY="${WORK}/test-key"
SERIAL="${WORK}/serial.log"
SSH_PORT=2222
# SECURE_BOOT=1 boots the VM with UEFI Secure Boot enforced, using a varstore
# build_sb_vars generates from scratch (our own PK/KEK + the public Microsoft
# UEFI CAs so the MS-signed shim validates + our MOK for grub, no revocation
# dbx). Default 0 (Setup Mode, SB not enforced) BY DESIGN: under OVMF — both the
# Arch and the Ubuntu builds — SB rejects our valid, unmodified MS-signed shim
# with a 0x1A Security Violation even with its exact CA in db and no aggressive
# dbx, a QEMU/OVMF behaviour we could not resolve after a long investigation.
# The shim is Debian's official signed shim (proven on real Secure Boot
# hardware), so the boot-test validates the OS boot with SB off and we rely on
# real hardware for the SB chain. The SB-on path (build_sb_vars) is kept for a
# future revisit — see the secureboot-boot-test-state memory.
SECURE_BOOT="${SECURE_BOOT:-0}"
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
#######################################
install_deps() {
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends qemu-system-x86 ovmf jq
  command -v yq >/dev/null 2>&1 || {
    sudo curl -fsSL \
      https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 \
      -o /usr/local/bin/yq
    sudo chmod +x /usr/local/bin/yq
  }
  echo 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' \
    | sudo tee /etc/udev/rules.d/99-kvm4all.rules >/dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger --name-match=kvm

  if [ "${SECURE_BOOT}" = 1 ]; then
    # virt-firmware builds the Secure Boot varstore (it bundles the Microsoft
    # UEFI CAs); openssl generates our own PK/KEK. Kept in a venv so the runner's
    # system Python is untouched (PEP 668).
    sudo apt-get install -y --no-install-recommends python3-venv openssl
    python3 -m venv "${WORK}/sbtool"
    "${WORK}/sbtool/bin/pip" install --quiet virt-firmware
  fi
}

#######################################
# Install the image to DISK, injecting a generated root SSH key.
# Globals:
#   IMAGE, DISK, KEY, WORK
#######################################
install_disk() {
  ssh-keygen -t ed25519 -N '' -f "${KEY}" -q
  # Sparse: only written blocks consume host space (~3G after install), but 10G
  # of virtual size is already ample for the ~3G deploy + boot + ESP.
  truncate -s 10G "${DISK}"
  # The disk is partitioned + installed by install-fs.sh, run privileged INSIDE
  # the image so it can use the image's own tools and bootc install
  # to-filesystem into the same layout as the ISO (btrfs pool + varlog quota).
  # bootc-build.yml builds with sudo podman, so this root podman sees the freshly
  # built image directly (no registry pull).
  sudo podman run --rm --privileged --pid=host --security-opt label=disable \
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
  base=$(find /usr/share/OVMF -name 'OVMF_VARS*.fd' ! -name '*.ms.*' \
    ! -name '*secboot*' | head -1)
  "${VIRT_FW}" -i "${base}" -o "${WORK}/OVMF_VARS.fd" \
    --set-pk "${guid}" "${WORK}/PK.pem" \
    --add-kek "${guid}" "${WORK}/KEK.pem" \
    --add-db "${guid}" "${certs}/ms-uefi-2011.pem" \
    --add-db "${guid}" "${certs}/ms-uefi-2023.pem" \
    --add-db "${guid}" "${WORK}/sb.crt" \
    --add-mok "${guid}" "${WORK}/sb.crt" \
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
  local code secure=off
  if [ "${SECURE_BOOT}" = 1 ]; then
    code=$(find /usr/share/OVMF -name 'OVMF_CODE*.fd' -name '*secboot*' \
      | head -1)
    secure=on
    # ${WORK}/OVMF_VARS.fd already written by build_sb_vars.
  else
    # Setup Mode: our grub's MOK is not enrolled in a fresh VM and distro *.ms
    # varstores carry a dbx that rejects the shim, so with SB off we validate the
    # OS boot path. Ref: Ubuntu UEFI/OVMF file naming.
    local vars
    code=$(find /usr/share/OVMF -name 'OVMF_CODE*.fd' ! -name '*secboot*' \
      | head -1)
    vars=$(find /usr/share/OVMF -name 'OVMF_VARS*.fd' ! -name '*.ms.*' \
      ! -name '*secboot*' | head -1)
    cp "${vars}" "${WORK}/OVMF_VARS.fd"
  fi
  sudo qemu-system-x86_64 \
    -enable-kvm -m "${MEM:-4096}" -smp "${VCPUS:-2}" -machine q35 \
    -global "driver=cfi.pflash01,property=secure,value=${secure}" \
    -drive "if=pflash,format=raw,unit=0,readonly=on,file=${code}" \
    -drive "if=pflash,format=raw,unit=1,file=${WORK}/OVMF_VARS.fd" \
    -drive "file=${DISK},format=raw,if=virtio" \
    -netdev "user,id=n0,hostfwd=tcp::${SSH_PORT}-:22" \
    -device virtio-net,netdev=n0 \
    -display none -serial "file:${SERIAL}" -monitor none \
    -daemonize -pidfile "${WORK}/qemu.pid"
}

#######################################
# Poll SSH until the VM answers or the timeout elapses.
# Arguments:
#   $1 - timeout in seconds
# Returns:
#   0 when reachable, 1 on timeout.
#######################################
wait_ssh() {
  local timeout="$1" i resets
  for ((i = 0; i < timeout; i++)); do
    ssh "${SSH_OPTS[@]}" root@localhost true 2>/dev/null && return 0
    # Fail fast on definitive firmware failures instead of waiting out the whole
    # timeout — a healthy boot reaches sshd in seconds, so any of these means SSH
    # is never coming:
    #   - Secure Boot rejected a boot binary (Verification failed / 0x1A);
    #   - the guest EFI app keeps resetting in a loop.
    if grep -q 'Security Violation' "${SERIAL}" 2>/dev/null; then
      echo "::error::Secure Boot rejected a boot binary (Verification failed:" \
        "Security Violation) — not booting; failing immediately"
      return 3
    fi
    resets=$(grep -c 'Reset System' "${SERIAL}" 2>/dev/null || echo 0)
    if [ "${resets}" -ge 3 ]; then
      echo "::error::guest firmware reboot loop (${resets}x Reset System) —" \
        "not booting; failing immediately"
      return 2
    fi
    if ((i % 30 == 0)); then
      echo "  … waiting for SSH (${i}/${timeout}s); serial tail:"
      sudo tail -n 15 "${SERIAL}" 2>/dev/null | sed 's/^/    | /' || true
    fi
    sleep 1
  done
  return 1
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
  local n i name cmd expect diag out rc fail=0 pass
  n=$(yq '.tests | length' "${TESTS_FILE}")
  for ((i = 0; i < n; i++)); do
    name=$(yq ".tests[${i}].name" "${TESTS_FILE}")
    cmd=$(yq ".tests[${i}].run" "${TESTS_FILE}")
    expect=$(yq ".tests[${i}].expect // \"\"" "${TESTS_FILE}")
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
      echo "::error::image test failed: ${name}"
      printf '%s\n' "${out}" | sed 's/^/      /'
      # Run this test's own diagnostic so the failure is self-explaining. The
      # manifest is validated up front, so diag is always present.
      diag=$(yq ".tests[${i}].diag" "${TESTS_FILE}")
      echo "      ----- diag: ${name} -----"
      # shellcheck disable=SC2029
      ssh "${SSH_OPTS[@]}" root@localhost "${diag}" 2>&1 | sed 's/^/      | /' || true
      fail=1
    fi
  done
  return "${fail}"
}

#######################################
# Enforce that every test ships its own `diag:` command. A test with no diag is
# a manifest error and fails the run BEFORE the VM is even booted — so a test
# can never regress into a blind, undiagnosable failure later. Also checks the
# mandatory name/run fields.
# Globals:
#   TESTS_FILE
# Returns:
#   Exits 1 if any test is missing name, run, or diag.
#######################################
validate_manifest() {
  local n i name run diag missing=0
  n=$(yq '.tests | length' "${TESTS_FILE}")
  for ((i = 0; i < n; i++)); do
    name=$(yq ".tests[${i}].name // \"\"" "${TESTS_FILE}")
    run=$(yq ".tests[${i}].run // \"\"" "${TESTS_FILE}")
    diag=$(yq ".tests[${i}].diag // \"\"" "${TESTS_FILE}")
    if [ -z "${name}" ] || [ "${name}" = "null" ]; then
      echo "::error::tests[${i}] has no name"
      missing=1
    fi
    if [ -z "${run}" ] || [ "${run}" = "null" ]; then
      echo "::error::test '${name}' (index ${i}) has no run:"
      missing=1
    fi
    if [ -z "${diag}" ] || [ "${diag}" = "null" ]; then
      echo "::error::test '${name}' (index ${i}) has no diag: — every test MUST ship its own diagnostic so a failure is never undiagnosable"
      missing=1
    fi
  done
  if [ "${missing}" -ne 0 ]; then
    echo "::error::${TESTS_FILE}: one or more tests are missing name/run/diag"
    exit 1
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
  if [ -f "${WORK}/qemu.pid" ]; then
    sudo kill "$(cat "${WORK}/qemu.pid")" 2>/dev/null || true
  fi
}

main() {
  trap cleanup EXIT
  install_deps
  # Fail fast on an incomplete manifest (missing diag) before paying for a boot.
  validate_manifest
  install_disk
  if [ "${SECURE_BOOT}" = 1 ]; then
    build_sb_vars
  fi
  boot_vm
  local timeout
  timeout=$(yq '.boot_timeout // 600' "${TESTS_FILE}")
  if ! wait_ssh "${timeout}"; then
    echo "::error::VM did not become reachable within ${timeout}s"
    sudo tail -n 50 "${SERIAL}" 2>/dev/null || true
    exit 1
  fi
  wait_boot
  run_tests
}

main "$@"
